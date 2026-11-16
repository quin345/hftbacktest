"""QuestDB access: DDL for task-specific tables, ILP line encoding, batched writer.

Only the ``btc_um_l2_*`` tables are created, with ``IF NOT EXISTS``. No existing
table is altered or dropped. Writes use ILP over HTTP on the loopback interface.
"""

from __future__ import annotations

import ipaddress
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

TABLE_PREFIX = "btc_um_l2_"

_COMMON_TAGS = "session_id SYMBOL, route SYMBOL, symbol SYMBOL, stream SYMBOL, conn_id SYMBOL"

DDL: dict[str, str] = {
    "depth_diff": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}depth_diff (
        ts TIMESTAMP, {_COMMON_TAGS}, side SYMBOL, apply_status SYMBOL,
        sync_epoch LONG, event_time_ms LONG, tx_time_ms LONG,
        first_update_id LONG, final_update_id LONG, prev_final_update_id LONG,
        level_count LONG, level_index LONG, price_e8 LONG, qty_e8 LONG, price DOUBLE, qty DOUBLE,
        price_str VARCHAR, qty_str VARCHAR, is_remove BOOLEAN,
        archive_file VARCHAR, archive_line LONG
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
    "depth_snapshot": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}depth_snapshot (
        ts TIMESTAMP, session_id SYMBOL, route SYMBOL, symbol SYMBOL, side SYMBOL, aligned BOOLEAN,
        sync_epoch LONG, last_update_id LONG, event_time_ms LONG, tx_time_ms LONG,
        level_count LONG, level_index LONG, price_e8 LONG, qty_e8 LONG, price DOUBLE, qty DOUBLE,
        price_str VARCHAR, qty_str VARCHAR, archive_file VARCHAR, archive_line LONG
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
    "book_ticker": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}book_ticker (
        ts TIMESTAMP, {_COMMON_TAGS}, book_top_check SYMBOL,
        update_id LONG, event_time_ms LONG, tx_time_ms LONG,
        bid_e8 LONG, bid_qty_e8 LONG, ask_e8 LONG, ask_qty_e8 LONG,
        bid_price DOUBLE, bid_qty DOUBLE, ask_price DOUBLE, ask_qty DOUBLE,
        bid_price_str VARCHAR, bid_qty_str VARCHAR, ask_price_str VARCHAR, ask_qty_str VARCHAR,
        archive_file VARCHAR, archive_line LONG
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
    "agg_trade": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}agg_trade (
        ts TIMESTAMP, {_COMMON_TAGS}, aggressor_side SYMBOL, buyer_is_maker BOOLEAN,
        agg_trade_id LONG, first_trade_id LONG, last_trade_id LONG,
        event_time_ms LONG, trade_time_ms LONG, price_e8 LONG, qty_e8 LONG,
        price DOUBLE, qty DOUBLE, price_str VARCHAR, qty_str VARCHAR,
        archive_file VARCHAR, archive_line LONG
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
    "ingest_event": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}ingest_event (
        ts TIMESTAMP, session_id SYMBOL, route SYMBOL, event_type SYMBOL, conn_id SYMBOL,
        sync_epoch LONG, archive_file VARCHAR, archive_line LONG, detail VARCHAR
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
    "health": f"""CREATE TABLE IF NOT EXISTS {TABLE_PREFIX}health (
        ts TIMESTAMP, session_id SYMBOL, sync_state SYMBOL,
        depth_msgs LONG, book_ticker_msgs LONG, agg_trade_msgs LONG, control_msgs LONG,
        unknown_msgs LONG, parse_errors LONG, gaps LONG, resyncs LONG, snapshots_ok LONG,
        snapshot_failures LONG, book_top_match LONG, book_top_mismatch LONG,
        archive_bytes LONG, ilp_rows_written LONG, ilp_write_errors LONG, ilp_spooled_lines LONG,
        max_recv_lag_ms LONG, last_recv_lag_ms LONG
    ) TIMESTAMP(ts) PARTITION BY DAY WAL""",
}


def assert_loopback(base_url: str) -> str:
    """Refuse any QuestDB endpoint that is not loopback. Returns the normalised base."""
    p = urlparse(base_url)
    if p.scheme != "http" or p.hostname is None:
        raise ValueError(f"QuestDB URL must be http://<loopback>:port, got {base_url!r}")
    host = p.hostname
    if host == "localhost":
        ok = True
    else:
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = False
    if not ok:
        raise ValueError(f"refusing non-loopback QuestDB host {host!r}")
    return f"http://{p.netloc}"


def exec_sql(base_url: str, sql: str, timeout: float = 30.0) -> dict[str, Any]:
    import json

    url = f"{base_url}/exec?" + urllib.parse.urlencode({"query": sql})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 loopback only
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"QuestDB HTTP {exc.code} for SQL {sql[:120]!r}: {detail}") from exc
    obj = json.loads(body)
    if "error" in obj:
        raise RuntimeError(f"QuestDB error for SQL: {obj['error']}")
    return obj


def ensure_tables(base_url: str) -> list[str]:
    created = []
    for name, sql in DDL.items():
        exec_sql(base_url, sql)
        created.append(TABLE_PREFIX + name)
    return created


# ----- ILP encoding -----------------------------------------------------------

def _esc_name(s: str) -> str:
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _esc_str_field(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _field(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("non-finite float not allowed")
        return repr(v)
    if isinstance(v, str):
        return _esc_str_field(v)
    raise TypeError(f"unsupported ILP field type {type(v)!r}")


def ilp_line(table: str, tags: dict[str, Any], fields: dict[str, Any], ts_ns: int) -> str:
    """Build one ILP line. ts_ns is sent with precision=n; QuestDB stores TIMESTAMP at microseconds, so sub-microsecond digits are truncated in hot tables. Exact recv_ns lives in the raw archive."""
    tag_part = ",".join(
        f"{_esc_name(k)}={_esc_name(str(v))}" for k, v in tags.items() if v is not None and v != ""
    )
    field_items = []
    for k, v in fields.items():
        enc = _field(v)
        if enc is not None:
            field_items.append(f"{_esc_name(k)}={enc}")
    if not field_items:
        raise ValueError(f"ILP line for {table} has no fields")
    head = _esc_name(table) + ("," + tag_part if tag_part else "")
    return f"{head} {','.join(field_items)} {int(ts_ns)}"


class IlpWriter:
    """Background batched ILP-over-HTTP writer with retry and disk spool.

    Lines that cannot be delivered after retries are appended to ``spool_path``
    so the data is kept on disk for later manual ingestion; they are counted,
    never silently dropped.
    """

    def __init__(self, base_url: str, spool_path: Path | None, batch_max: int = 3000, flush_interval_s: float = 0.5):
        self.base_url = assert_loopback(base_url) if base_url else None
        self.spool_path = spool_path
        self.batch_max = batch_max
        self.flush_interval_s = flush_interval_s
        self._q: queue.Queue[str | None] = queue.Queue(maxsize=500_000)
        self._thread = threading.Thread(target=self._run, name="ilp-writer", daemon=True)
        self.rows_written = 0
        self.write_errors = 0
        self.spooled_lines = 0
        self.enqueued = 0
        self._started = False

    def start(self) -> None:
        if self.base_url is None:
            return
        self._thread.start()
        self._started = True

    def put(self, line: str) -> None:
        if self.base_url is None:
            return
        self.enqueued += 1
        self._q.put(line)

    def queue_depth(self) -> int:
        return self._q.qsize()

    def _post(self, lines: list[str]) -> None:
        body = ("\n".join(lines) + "\n").encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/write?precision=n", data=body, method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 loopback only
            if resp.status not in (200, 204):
                raise RuntimeError(f"ILP HTTP {resp.status}")

    def _deliver(self, lines: list[str]) -> None:
        delay = 0.5
        for attempt in range(4):
            try:
                self._post(lines)
                self.rows_written += len(lines)
                return
            except (urllib.error.URLError, RuntimeError, OSError) as exc:
                self.write_errors += 1
                log.warning("ILP write failed (attempt %d): %s", attempt + 1, exc)
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
        if self.spool_path is None:
            raise RuntimeError("ILP delivery failed and no spool path configured")
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.spool_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.spooled_lines += len(lines)

    def _run(self) -> None:
        batch: list[str] = []
        last = time.monotonic()
        while True:
            timeout = max(0.0, self.flush_interval_s - (time.monotonic() - last))
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = ""
            if item is None:  # sentinel
                if batch:
                    self._deliver(batch)
                return
            if item:
                batch.append(item)
            if len(batch) >= self.batch_max or (batch and time.monotonic() - last >= self.flush_interval_s):
                self._deliver(batch)
                batch = []
                last = time.monotonic()
            elif not batch:
                last = time.monotonic()

    def close(self, timeout: float = 120.0) -> None:
        if not self._started:
            return
        self._q.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("ILP writer did not drain within timeout")


def iter_lines(lines: Iterable[str]) -> Iterable[str]:
    for ln in lines:
        yield ln
