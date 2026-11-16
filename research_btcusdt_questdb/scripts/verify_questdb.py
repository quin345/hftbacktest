#!/usr/bin/env python3
"""Live SQL verification of one capture session against QuestDB (loopback only).

    python scripts/verify_questdb.py --session <session_id> [--out data/verify.json]

Checks, all read-only SELECTs against the btc_um_l2_* tables:
  * row counts, time ranges, per-status and per-side distributions;
  * reconciliation of DB counts with an independent parse of the raw archive
    (every depthUpdate / bookTicker / aggTrade frame must map to one DB row set);
  * duplicate detection (update IDs and agg trade IDs within the session);
  * gap / resync / snapshot events and book-top cross-check results;
  * example queries with actual result rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from btc_l2.archive import read_archive  # noqa: E402
from btc_l2.ilp import TABLE_PREFIX, assert_loopback, exec_sql  # noqa: E402
from btc_l2.parse import classify_frame  # noqa: E402

T = TABLE_PREFIX


def q(base: str, sql: str) -> list[list]:
    return exec_sql(base, sql).get("dataset", [])


def scalar(base: str, sql: str):
    rows = q(base, sql)
    return rows[0][0] if rows else None


def dup_count(base: str, table: str, where: str, col: str) -> int:
    """Number of duplicate key occurrences: total rows minus distinct keys."""
    rows = q(base, f"SELECT count(), count_distinct({col}) FROM {table} WHERE {where}")
    total, distinct = rows[0]
    return int(total) - int(distinct)


def archive_counts(session_dir: Path) -> dict:
    counts = {"public_depth_frames": 0, "public_bookticker_frames": 0, "market_aggtrade_frames": 0,
              "rest_snapshots": 0, "control_frames": 0, "unknown_frames": 0,
              "depth_levels": 0, "depth_first_final_u": None, "depth_last_final_u": None,
              "agg_first_id": None, "agg_last_id": None}
    depth_us: list[int] = []
    agg_ids: list[int] = []
    for name, route in (("public.ndjson.gz", "public"), ("market.ndjson.gz", "market")):
        p = session_dir / name
        if not p.exists():
            continue
        for _, _, payload in read_archive(p):
            env = classify_frame(payload)
            if env.kind == "depth":
                counts["public_depth_frames"] += 1
                counts["depth_levels"] += len(env.payload["b"]) + len(env.payload["a"])
                depth_us.append(env.payload["u"])
            elif env.kind == "book_ticker":
                counts["public_bookticker_frames"] += 1
            elif env.kind == "agg_trade":
                counts["market_aggtrade_frames"] += 1
                agg_ids.append(env.payload["a"])
            elif env.kind == "control":
                counts["control_frames"] += 1
            else:
                counts["unknown_frames"] += 1
    p = session_dir / "rest.ndjson.gz"
    if p.exists():
        counts["rest_snapshots"] = sum(1 for _ in read_archive(p))
    if depth_us:
        counts["depth_first_final_u"], counts["depth_last_final_u"] = depth_us[0], depth_us[-1]
    if agg_ids:
        counts["agg_first_id"], counts["agg_last_id"] = agg_ids[0], agg_ids[-1]
    counts["agg_duplicate_ids_in_archive"] = len(agg_ids) - len(set(agg_ids))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--questdb-url", default="http://127.0.0.1:9000")
    ap.add_argument("--capture-dir", type=Path, default=ROOT / "data" / "capture")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    base = assert_loopback(args.questdb_url)
    sid = args.session
    sess = f"session_id = '{sid}'"
    res: dict = {"session_id": sid, "questdb_url": base}

    # 1. Row counts and time ranges.
    res["row_counts"] = {}
    res["time_ranges_ns"] = {}
    for tbl in ["depth_diff", "depth_snapshot", "book_ticker", "agg_trade", "ingest_event", "health"]:
        res["row_counts"][tbl] = scalar(base, f"SELECT count() FROM {T}{tbl} WHERE {sess}")
        rng = q(base, f"SELECT min(ts), max(ts) FROM {T}{tbl} WHERE {sess}")
        res["time_ranges_ns"][tbl] = rng[0] if rng else None

    # 2. Depth: headers = events; levels; statuses; sides; duplicate update IDs.
    res["depth_headers_by_status"] = q(base, f"SELECT apply_status, count() FROM {T}depth_diff WHERE {sess} AND side='header' GROUP BY apply_status")
    res["depth_level_rows_by_side"] = q(base, f"SELECT side, count() FROM {T}depth_diff WHERE {sess} AND side <> 'header' GROUP BY side")
    res["depth_duplicate_final_update_ids"] = dup_count(base, f"{T}depth_diff", f"{sess} AND side='header'", "final_update_id")
    res["depth_remove_levels"] = scalar(base, f"SELECT count() FROM {T}depth_diff WHERE {sess} AND side <> 'header' AND is_remove = true")

    # 3. Snapshots and sync events.
    res["snapshot_headers"] = q(base, f"SELECT aligned, count(), min(last_update_id), max(last_update_id) FROM {T}depth_snapshot WHERE {sess} AND side='header' GROUP BY aligned")
    res["ingest_event_types"] = q(base, f"SELECT event_type, count() FROM {T}ingest_event WHERE {sess} GROUP BY event_type")
    res["ingest_sync_samples"] = q(base, f"SELECT ts, event_type, detail FROM {T}ingest_event WHERE {sess} AND event_type IN ('gap','snapshot_ok','align_failed','resync_reset','book_top_mismatch','snapshot_failed') LIMIT 10")

    # 4. Book ticker and top-of-book cross-check.
    res["book_top_check"] = q(base, f"SELECT book_top_check, count() FROM {T}book_ticker WHERE {sess} GROUP BY book_top_check")
    res["book_ticker_duplicate_update_ids"] = dup_count(base, f"{T}book_ticker", sess, "update_id")

    # 5. Trades: IDs, aggressor convention.
    res["agg_trade_by_aggressor"] = q(base, f"SELECT aggressor_side, buyer_is_maker, count() FROM {T}agg_trade WHERE {sess} GROUP BY aggressor_side, buyer_is_maker")
    res["agg_trade_duplicate_ids"] = dup_count(base, f"{T}agg_trade", sess, "agg_trade_id")

    # 6. Archive reconciliation.
    sdir = args.capture_dir / sid
    arch = archive_counts(sdir) if sdir.exists() else {"error": f"no archive dir {sdir}"}
    res["archive_counts"] = arch
    rc = res["row_counts"]
    depth_headers = scalar(base, f"SELECT count() FROM {T}depth_diff WHERE {sess} AND side='header'")
    res["reconciliation"] = {
        "depth_frames_archive_vs_db_headers": [arch.get("public_depth_frames"), depth_headers],
        "bookticker_frames_archive_vs_db": [arch.get("public_bookticker_frames"), rc["book_ticker"]],
        "aggtrade_frames_archive_vs_db": [arch.get("market_aggtrade_frames"), rc["agg_trade"]],
    }

    # 7. Example query results (actual rows).
    res["example_query_depth_last_rows"] = q(base, f"SELECT ts, side, apply_status, price_str, qty_str, final_update_id, prev_final_update_id, archive_file, archive_line FROM {T}depth_diff WHERE {sess} AND side <> 'header' ORDER BY ts DESC LIMIT 3")
    res["example_query_book_ticker_last"] = q(base, f"SELECT ts, update_id, bid_price_str, bid_qty_str, ask_price_str, ask_qty_str, book_top_check FROM {T}book_ticker WHERE {sess} ORDER BY ts DESC LIMIT 2")
    res["example_query_agg_trade_last"] = q(base, f"SELECT ts, agg_trade_id, price_str, qty_str, aggressor_side FROM {T}agg_trade WHERE {sess} ORDER BY ts DESC LIMIT 2")
    res["example_query_health_last"] = q(base, f"SELECT ts, sync_state, depth_msgs, book_ticker_msgs, agg_trade_msgs, gaps, resyncs, snapshots_ok, ilp_rows_written, ilp_write_errors, ilp_spooled_lines, archive_bytes FROM {T}health WHERE {sess} ORDER BY ts DESC LIMIT 1")

    text = json.dumps(res, indent=2, sort_keys=True, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
