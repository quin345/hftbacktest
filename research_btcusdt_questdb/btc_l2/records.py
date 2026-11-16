"""Map parsed events and sequencer outputs to ILP lines (no I/O here)."""

from __future__ import annotations

from .ilp import TABLE_PREFIX, ilp_line
from .parse import AggTrade, BookTicker, DepthDiff, DepthSnapshot
from .sequencer import DepthOut, Ref, SnapshotOut, SyncOut

EXCHANGE = "binance"
MARKET = "usdm-perp"


def _ctx(session_id: str, route: str, stream: str, conn_id: str | None) -> dict:
    return {"session_id": session_id, "route": route, "stream": stream, "conn_id": conn_id, "symbol": "BTCUSDT"}


def depth_out_lines(out: DepthOut, session_id: str, route: str, stream: str) -> list[str]:
    ev, ref = out.ev, out.ref
    base_tags = _ctx(session_id, route, stream, ref.conn_id or None)
    base_tags.update({"apply_status": out.status})
    common = {
        "sync_epoch": out.epoch,
        "event_time_ms": ev.event_time_ms,
        "tx_time_ms": ev.tx_time_ms,
        "first_update_id": ev.first_update_id,
        "final_update_id": ev.final_update_id,
        "prev_final_update_id": ev.prev_final_update_id,
        "archive_file": ref.archive_file,
        "archive_line": ref.archive_line,
    }
    lines = [
        # Header row: one per depth event, so event counts are exact even if it has no levels.
        ilp_line(TABLE_PREFIX + "depth_diff", {**base_tags, "side": "header"},
                 {**common, "level_index": -1, "level_count": len(ev.levels)}, ref.recv_ns)
    ]
    for lv in ev.levels:
        fields = {
            **common,
            "level_index": lv.index,
            "price_e8": lv.price_e8,
            "qty_e8": lv.qty_e8,
            "price": float(lv.price_str),
            "qty": float(lv.qty_str),
            "price_str": lv.price_str,
            "qty_str": lv.qty_str,
            "is_remove": lv.is_remove,
        }
        lines.append(ilp_line(TABLE_PREFIX + "depth_diff", {**base_tags, "side": lv.side}, fields, ref.recv_ns))
    return lines


def snapshot_lines(out: SnapshotOut, session_id: str) -> list[str]:
    snap: DepthSnapshot = out.snap
    ref = out.ref
    tags = {"session_id": session_id, "route": "rest", "symbol": "BTCUSDT", "aligned": None}
    common = {
        "aligned": out.aligned,
        "sync_epoch": out.epoch,
        "last_update_id": snap.last_update_id,
        "event_time_ms": snap.event_time_ms,
        "tx_time_ms": snap.tx_time_ms,
        "archive_file": ref.archive_file,
        "archive_line": ref.archive_line,
    }
    lines = [ilp_line(TABLE_PREFIX + "depth_snapshot", {**tags, "side": "header"},
                      {**common, "level_index": -1, "level_count": len(snap.bids) + len(snap.asks)}, ref.recv_ns)]
    for lv in snap.bids + snap.asks:
        lines.append(ilp_line(
            TABLE_PREFIX + "depth_snapshot", {**tags, "side": lv.side},
            {**common, "level_index": lv.index, "price_e8": lv.price_e8, "qty_e8": lv.qty_e8,
             "price": float(lv.price_str), "qty": float(lv.qty_str),
             "price_str": lv.price_str, "qty_str": lv.qty_str},
            ref.recv_ns))
    return lines


def book_ticker_line(bt: BookTicker, ref: Ref, session_id: str, conn_id: str, stream: str, check: str) -> str:
    tags = {**_ctx(session_id, "public", stream, conn_id), "book_top_check": check}
    return ilp_line(TABLE_PREFIX + "book_ticker", tags, {
        "update_id": bt.update_id,
        "event_time_ms": bt.event_time_ms,
        "tx_time_ms": bt.tx_time_ms,
        "bid_e8": bt.bid_e8, "bid_qty_e8": bt.bid_qty_e8,
        "ask_e8": bt.ask_e8, "ask_qty_e8": bt.ask_qty_e8,
        "bid_price": float(bt.bid_price_str), "bid_qty": float(bt.bid_qty_str),
        "ask_price": float(bt.ask_price_str), "ask_qty": float(bt.ask_qty_str),
        "bid_price_str": bt.bid_price_str, "bid_qty_str": bt.bid_qty_str,
        "ask_price_str": bt.ask_price_str, "ask_qty_str": bt.ask_qty_str,
        "archive_file": ref.archive_file, "archive_line": ref.archive_line,
    }, ref.recv_ns)


def agg_trade_line(t: AggTrade, ref: Ref, session_id: str, conn_id: str, stream: str) -> str:
    tags = {**_ctx(session_id, "market", stream, conn_id), "aggressor_side": t.aggressor_side}
    return ilp_line(TABLE_PREFIX + "agg_trade", tags, {
        "buyer_is_maker": t.buyer_is_maker,
        "agg_trade_id": t.agg_trade_id,
        "first_trade_id": t.first_trade_id,
        "last_trade_id": t.last_trade_id,
        "event_time_ms": t.event_time_ms,
        "trade_time_ms": t.trade_time_ms,
        "price_e8": t.price_e8, "qty_e8": t.qty_e8,
        "price": float(t.price_str), "qty": float(t.qty_str),
        "price_str": t.price_str, "qty_str": t.qty_str,
        "archive_file": ref.archive_file, "archive_line": ref.archive_line,
    }, ref.recv_ns)


def sync_line(out: SyncOut, session_id: str, route: str) -> str:
    ref = out.ref
    ts = ref.recv_ns if ref else 0
    conn_id = (ref.conn_id or None) if ref else None
    return ilp_line(TABLE_PREFIX + "ingest_event", {
        "session_id": session_id, "route": route, "event_type": out.event_type, "conn_id": conn_id,
    }, {
        "sync_epoch": out.epoch,
        "archive_file": ref.archive_file if ref else None,
        "archive_line": ref.archive_line if ref else None,
        "detail": out.detail,
    }, ts)


def event_line(session_id: str, route: str, conn_id: str | None, event_type: str, detail: str,
               recv_ns: int, archive_file: str | None = None, archive_line: int | None = None) -> str:
    return ilp_line(TABLE_PREFIX + "ingest_event", {
        "session_id": session_id, "route": route, "event_type": event_type, "conn_id": conn_id,
    }, {"archive_file": archive_file, "archive_line": archive_line, "detail": detail}, recv_ns)
