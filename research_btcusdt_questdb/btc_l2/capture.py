"""Bounded live capture of Binance USD-M BTCUSDT public L2 depth, bookTicker and aggTrade.

    python -m btc_l2.capture --seconds 180 --out-dir data/capture \
        --questdb-url http://127.0.0.1:9000

Streams (documented routed endpoints, checked 2026-11-15):
  wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker
  wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade
  GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000   (snapshot)

Public L2 only: there is no order-level (L3) feed for USD-M, so this captures
aggregated price-level depth. Every websocket frame is archived verbatim first,
then parsed and written to QuestDB. Hard caps: 600 s and 1 GiB archive bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.request

import websockets

from . import records
from .archive import ArchiveWriter
from .ilp import IlpWriter, TABLE_PREFIX, assert_loopback, ensure_tables, ilp_line
from .parse import (
    ParseError, classify_frame, parse_agg_trade, parse_book_ticker, parse_depth, parse_snapshot,
)
from .sequencer import DepthOut, DepthSequencer, Ref, SnapshotOut, SyncOut

log = logging.getLogger("btc_l2.capture")

SYMBOL = "BTCUSDT"
PUBLIC_URL = "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker"
MARKET_URL = "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"
SNAPSHOT_URL = "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000"
DEPTH_STREAM = "btcusdt@depth@100ms"
BOOK_STREAM = "btcusdt@bookTicker"
AGG_STREAM = "btcusdt@aggTrade"
MAX_SECONDS = 600
MAX_BYTES = 1 << 30
HTTP_TIMEOUT_S = 10


def _utc_now_ns() -> int:
    return time.time_ns()


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "btc-l2-capture/0.1"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 fixed public URL
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        body = resp.read().decode("utf-8")
    if "\n" in body or "\r" in body:
        raise RuntimeError("snapshot body not single-line; refusing to archive altered bytes")
    return body


def _versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0], "websockets": websockets.__version__, "questdb": "10.2.0"}
    try:
        out["git_head"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5,
                                         cwd=Path(__file__).resolve().parent).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out["git_head"] = "unknown"
    return out


class Capture:
    def __init__(self, seconds: int, out_dir: Path, questdb_url: str | None, max_bytes: int, snapshot_limit_note: str = ""):
        if not 1 <= seconds <= MAX_SECONDS:
            raise ValueError(f"--seconds must be 1..{MAX_SECONDS}")
        self.seconds = seconds
        self.max_bytes = max_bytes
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.dir = out_dir / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.archives = {
            "public": ArchiveWriter(self.dir / "public.ndjson.gz"),
            "market": ArchiveWriter(self.dir / "market.ndjson.gz"),
            "rest": ArchiveWriter(self.dir / "rest.ndjson.gz"),
        }
        self.seq = DepthSequencer(SYMBOL)
        self.counts: Counter[str] = Counter()
        self.last_lag_ms: int | None = None
        self.max_lag_ms: int | None = None
        self.stop_event = asyncio.Event()
        self.stop_reason: str | None = None
        self.writer = IlpWriter(questdb_url, spool_path=self.dir / "ilp_spool.txt")
        self.tasks: list[asyncio.Task] = []
        self.snapshot_tasks: set[asyncio.Task] = set()
        self.started_ns = _utc_now_ns()
        self.last_public_conn = ""
        self.last_public_ref: Ref | None = None
        self.last_market_conn = ""
        self.snapshot_limit_note = snapshot_limit_note

    # ----- output helpers --------------------------------------------------
    def emit(self, lines: list[str]) -> None:
        for ln in lines:
            self.writer.put(ln)

    def emit_outs(self, outs: list[Any], sync_route: str) -> None:
        for o in outs:
            if isinstance(o, DepthOut):
                self.counts["depth_out_" + o.status] += 1
                self.emit(records.depth_out_lines(o, self.session_id, "public", DEPTH_STREAM))
            elif isinstance(o, SnapshotOut):
                self.emit(records.snapshot_lines(o, self.session_id))
            elif isinstance(o, SyncOut):
                self.emit([records.sync_line(o, self.session_id, sync_route)])

    def event(self, route: str, conn_id: str, event_type: str, detail: str, archive_line: int | None = None,
              archive_file: str | None = None) -> None:
        self.emit([records.event_line(self.session_id, route, conn_id or None, event_type, detail,
                                      _utc_now_ns(), archive_file, archive_line)])

    def archive(self, route: str, recv_ns: int, text: str) -> Ref:
        arch = self.archives[route]
        line = arch.append(recv_ns, text)
        return Ref(arch.name, line, recv_ns, conn_id=self.last_public_conn if route == "public" else self.last_market_conn)

    # ----- depth snapshot ---------------------------------------------------
    def request_snapshot_if_needed(self, need: bool) -> None:
        if not need:
            return
        epoch = self.seq.on_snapshot_request(_utc_now_ns())
        t = asyncio.create_task(self.fetch_snapshot(epoch))
        self.snapshot_tasks.add(t)
        t.add_done_callback(self.snapshot_tasks.discard)

    async def fetch_snapshot(self, epoch: int) -> None:
        try:
            body = await asyncio.to_thread(_http_get, SNAPSHOT_URL)
            recv_ns = _utc_now_ns()
            arch = self.archives["rest"]
            line = arch.append(recv_ns, body)
            ref = Ref(arch.name, line, recv_ns, conn_id=f"rest-epoch-{epoch}")
            snap = parse_snapshot(body)
            outs, need = self.seq.on_snapshot(epoch, snap, ref)
            self.emit_outs(outs, sync_route="rest")
            self.counts["snapshots"] += 1
            self.request_snapshot_if_needed(need)
        except Exception as exc:  # noqa: BLE001 - recorded, then retried by the state machine
            self.counts["snapshot_failures"] += 1
            recv_ns = _utc_now_ns()
            ref = Ref("", 0, recv_ns, conn_id=f"rest-epoch-{epoch}")
            self.emit_outs(self.seq.on_snapshot_failed(epoch, f"{type(exc).__name__}: {exc}", ref), sync_route="rest")
            await asyncio.sleep(2.0)
            if self.seq.epoch == epoch and self.seq.state != "live" and not self.stop_event.is_set():
                self.request_snapshot_if_needed(self.seq._want_snapshot(_utc_now_ns()))

    # ----- websocket loops --------------------------------------------------
    async def public_loop(self) -> None:
        conn_no = 0
        backoff = 1.0
        while not self.stop_event.is_set():
            conn_no += 1
            conn_id = f"public-{conn_no}"
            self.last_public_conn = conn_id
            try:
                async with websockets.connect(PUBLIC_URL, compression=None, max_size=2**22, open_timeout=15) as ws:
                    backoff = 1.0
                    self.event("public", conn_id, "connect", PUBLIC_URL)
                    async for msg in ws:
                        if self.stop_event.is_set():
                            break
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", "replace")
                        self.handle_public(msg, conn_id)
                    reason = "stream_ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
            if self.stop_event.is_set():
                break
            self.counts["public_disconnects"] += 1
            ref = self.last_public_ref or Ref("", 0, _utc_now_ns(), conn_id)
            self.event("public", conn_id, "disconnect", reason)
            self.emit_outs(self.seq.on_connection_reset(ref, f"public disconnect: {reason}"), sync_route="public")
            log.warning("public disconnected (%s); reconnecting in %.0fs", reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle_public(self, text: str, conn_id: str) -> None:
        recv_ns = _utc_now_ns()
        ref = self.archive("public", recv_ns, text)
        self.last_public_ref = ref
        try:
            env = classify_frame(text)
            if env.kind == "depth":
                ev = parse_depth(env.payload)
                self.counts["depth_msgs"] += 1
                self._lag(recv_ns, ev.event_time_ms)
                outs, need = self.seq.on_depth(ev, ref)
                self.emit_outs(outs, sync_route="public")
                self.request_snapshot_if_needed(need)
            elif env.kind == "book_ticker":
                bt = parse_book_ticker(env.payload)
                self.counts["book_ticker_msgs"] += 1
                self._lag(recv_ns, bt.event_time_ms)
                outs, check = self.seq.on_book_ticker(bt, ref)
                self.emit_outs(outs, sync_route="public")
                self.counts["book_top_" + check] += 1
                self.emit([records.book_ticker_line(bt, ref, self.session_id, conn_id, BOOK_STREAM, check)])
            elif env.kind == "control":
                self.counts["control_msgs"] += 1
            else:
                self.counts["unknown_msgs"] += 1
                self.event("public", conn_id, "unknown_payload", text[:200], ref.archive_line, ref.archive_file)
        except ParseError as exc:
            self.counts["parse_errors"] += 1
            self.event("public", conn_id, "parse_error", f"{exc}", ref.archive_line, ref.archive_file)

    async def market_loop(self) -> None:
        conn_no = 0
        backoff = 1.0
        while not self.stop_event.is_set():
            conn_no += 1
            conn_id = f"market-{conn_no}"
            self.last_market_conn = conn_id
            try:
                async with websockets.connect(MARKET_URL, compression=None, max_size=2**22, open_timeout=15) as ws:
                    backoff = 1.0
                    self.event("market", conn_id, "connect", MARKET_URL)
                    async for msg in ws:
                        if self.stop_event.is_set():
                            break
                        if isinstance(msg, bytes):
                            msg = msg.decode("utf-8", "replace")
                        self.handle_market(msg, conn_id)
                    reason = "stream_ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
            if self.stop_event.is_set():
                break
            self.counts["market_disconnects"] += 1
            self.event("market", conn_id, "disconnect", reason)
            log.warning("market disconnected (%s); reconnecting in %.0fs", reason, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle_market(self, text: str, conn_id: str) -> None:
        recv_ns = _utc_now_ns()
        ref = self.archive("market", recv_ns, text)
        try:
            env = classify_frame(text)
            if env.kind == "agg_trade":
                t = parse_agg_trade(env.payload)
                self.counts["agg_trade_msgs"] += 1
                self._lag(recv_ns, t.event_time_ms)
                self.emit([records.agg_trade_line(t, ref, self.session_id, conn_id, AGG_STREAM)])
            elif env.kind == "control":
                self.counts["control_msgs"] += 1
            else:
                self.counts["unknown_msgs"] += 1
                self.event("market", conn_id, "unknown_payload", text[:200], ref.archive_line, ref.archive_file)
        except ParseError as exc:
            self.counts["parse_errors"] += 1
            self.event("market", conn_id, "parse_error", f"{exc}", ref.archive_line, ref.archive_file)

    def _lag(self, recv_ns: int, event_ms: int) -> None:
        lag = int(recv_ns // 1_000_000) - event_ms
        self.last_lag_ms = lag
        self.max_lag_ms = lag if self.max_lag_ms is None else max(self.max_lag_ms, lag)

    # ----- health / limits --------------------------------------------------
    def archive_bytes(self) -> int:
        return sum(os.path.getsize(p) for p in self.dir.glob("*.gz") if p.exists())

    def health_line(self) -> str:
        c = self.counts
        s = self.seq.counters
        now = _utc_now_ns()
        return ilp_line(TABLE_PREFIX + "health", {"session_id": self.session_id, "sync_state": self.seq.state}, {
            "depth_msgs": c["depth_msgs"], "book_ticker_msgs": c["book_ticker_msgs"],
            "agg_trade_msgs": c["agg_trade_msgs"], "control_msgs": c["control_msgs"],
            "unknown_msgs": c["unknown_msgs"], "parse_errors": c["parse_errors"],
            "gaps": s["gaps"], "resyncs": s["resyncs"], "snapshots_ok": s["snapshots_ok"],
            "snapshot_failures": c["snapshot_failures"],
            "book_top_match": s["book_top_match"], "book_top_mismatch": s["book_top_mismatch"],
            "archive_bytes": self.archive_bytes(), "ilp_rows_written": self.writer.rows_written,
            "ilp_write_errors": self.writer.write_errors, "ilp_spooled_lines": self.writer.spooled_lines,
            "max_recv_lag_ms": self.max_lag_ms or 0, "last_recv_lag_ms": self.last_lag_ms or 0,
        }, now)

    async def monitor_loop(self) -> None:
        tick = 0
        while not self.stop_event.is_set():
            await asyncio.sleep(1.0)
            tick += 1
            for a in self.archives.values():
                a.flush()
            if self.archive_bytes() > self.max_bytes:
                self.stop_reason = "size_cap_reached"
                self.stop_event.set()
                return
            if tick % 10 == 0:
                self.emit([self.health_line()])
                log.info("t=%ds depth=%d book=%d trades=%d gaps=%d state=%s archive=%dB queue=%d",
                         tick, self.counts["depth_msgs"], self.counts["book_ticker_msgs"],
                         self.counts["agg_trade_msgs"], self.seq.counters["gaps"], self.seq.state,
                         self.archive_bytes(), self.writer.queue_depth())

    async def timer(self) -> None:
        await asyncio.sleep(self.seconds)
        if self.stop_reason is None:
            self.stop_reason = "duration_elapsed"
        self.stop_event.set()

    # ----- lifecycle --------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        self.writer.start()
        self.event("public", "", "session_start", json.dumps({
            "seconds": self.seconds, "symbol": SYMBOL, "public_url": PUBLIC_URL, "market_url": MARKET_URL,
            "snapshot_url": SNAPSHOT_URL, "versions": _versions(),
        }))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)
        self.tasks = [
            asyncio.create_task(self.public_loop(), name="public"),
            asyncio.create_task(self.market_loop(), name="market"),
            asyncio.create_task(self.monitor_loop(), name="monitor"),
            asyncio.create_task(self.timer(), name="timer"),
        ]
        await self.stop_event.wait()
        for t in self.tasks + list(self.snapshot_tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, *self.snapshot_tasks, return_exceptions=True)
        # Buffered depth events that never aligned are kept, labelled as unsynchronised.
        self.emit_outs(self.seq.finish(), sync_route="public")
        self.event("public", self.last_public_conn, "session_stop", self.stop_reason or "unknown")
        self.emit([self.health_line()])
        for a in self.archives.values():
            a.flush()
        self.writer.close()
        for a in self.archives.values():
            a.close()
        summary = self.summary()
        (self.dir / "session_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        return summary

    def _on_signal(self, sig: signal.Signals) -> None:
        self.stop_reason = f"signal_{sig.name}"
        self.stop_event.set()

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stop_reason": self.stop_reason,
            "requested_seconds": self.seconds,
            "elapsed_s": round((_utc_now_ns() - self.started_ns) / 1e9, 2),
            "counts": dict(self.counts),
            "depth_counters": dict(self.seq.counters),
            "final_sync_state": self.seq.state,
            "final_last_update_id": self.seq.last_u,
            "ilp_rows_written": self.writer.rows_written,
            "ilp_write_errors": self.writer.write_errors,
            "ilp_spooled_lines": self.writer.spooled_lines,
            "archive_files": {k: {"lines": v.lines, "path": str(v.path)} for k, v in self.archives.items()},
            "archive_bytes": self.archive_bytes(),
            "max_recv_lag_ms": self.max_lag_ms,
            "dir": str(self.dir),
            "versions": _versions(),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bounded BTCUSDT USD-M public L2 capture to archive + QuestDB")
    ap.add_argument("--seconds", type=int, default=180, help=f"capture duration (1..{MAX_SECONDS})")
    ap.add_argument("--out-dir", type=Path, default=Path("data/capture"))
    ap.add_argument("--questdb-url", default="http://127.0.0.1:9000",
                    help="loopback QuestDB HTTP URL; pass '' to archive only")
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES, help="archive byte cap (hard max 1 GiB)")
    ap.add_argument("--no-create-tables", action="store_true", help="skip DDL (tables must exist)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.max_bytes > MAX_BYTES:
        ap.error("--max-bytes above the 1 GiB hard cap")
    qurl = None
    if args.questdb_url:
        qurl = assert_loopback(args.questdb_url)
        if not args.no_create_tables:
            created = ensure_tables(qurl)
            log.info("ensured tables: %s", ", ".join(created))
    cap = Capture(args.seconds, args.out_dir, qurl, args.max_bytes)
    summary = asyncio.run(cap.run())
    print(json.dumps(summary, indent=2, sort_keys=True))
    # Explicit session handoff for scripts/run_proof.sh (no mtime-based selection).
    print(f"SESSION_ID={summary['session_id']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
