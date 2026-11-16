"""Depth-diff synchronisation state machine (pure, no I/O).

Implements the official Binance USD-M "How to manage a local order book
correctly" procedure:

  1. buffer diff events (``U``, ``u``, ``pu``) from the stream;
  2. fetch a REST depth snapshot with ``lastUpdateId``;
  3. drop buffered events with ``u < lastUpdateId``;
  4. the first processed event must satisfy ``U <= lastUpdateId <= u``;
     otherwise the snapshot is too old and must be refetched;
  5. afterwards every event must satisfy ``pu == previous u``; otherwise a
     gap is declared and the book is re-initialised from step 2.

The sequencer never performs network I/O. It returns ``Out`` objects that the
capture loop writes to storage. ``DepthOut.status`` labels every diff event so
that buffered, stale, gap-triggering and applied events stay distinguishable.

The in-memory book is a *partial* view: the REST snapshot is limited to 1000
levels per side, so levels outside that window are unknown. A book is only
claimed "synchronised" between a successful alignment and the next gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .parse import DepthDiff, DepthSnapshot, BookTicker, Level

AWAIT = "await_snapshot"
LIVE = "live"

# Minimum seconds between snapshot REST requests for one symbol.
SNAPSHOT_MIN_INTERVAL_NS = 1_000_000_000


@dataclass(frozen=True)
class Ref:
    """Where a message lives in the raw archive, and which connection delivered it."""

    archive_file: str
    archive_line: int
    recv_ns: int
    conn_id: str = ""


@dataclass(frozen=True)
class DepthOut:
    ev: DepthDiff
    status: str  # applied | applied_aligned | stale_before_snapshot | gap_trigger | discarded_gap | pending_unsynced | stale_live
    epoch: int
    ref: Ref
    book_top_check: str = "not_checked"


@dataclass(frozen=True)
class SnapshotOut:
    snap: DepthSnapshot
    epoch: int
    ref: Ref
    aligned: bool


@dataclass(frozen=True)
class SyncOut:
    event_type: str  # gap | snapshot_request | snapshot_ok | align_failed | resync_reset | discard | book_top_mismatch
    detail: str
    epoch: int
    ref: Ref | None = None


Out = Any  # DepthOut | SnapshotOut | SyncOut


@dataclass
class _Pending:
    ev: DepthDiff
    ref: Ref


@dataclass
class DepthSequencer:
    symbol: str
    epoch: int = 0
    state: str = AWAIT
    last_u: int | None = None
    snapshot_inflight: bool = False
    last_snapshot_request_ns: int = -10**18
    buffer: list[_Pending] = field(default_factory=list)
    bids: dict[int, tuple[int, str, str]] = field(default_factory=dict)  # price_e8 -> (qty_e8, price_str, qty_str)
    asks: dict[int, tuple[int, str, str]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=lambda: {
        "applied": 0, "gaps": 0, "resyncs": 0, "snapshots_ok": 0, "align_failed": 0,
        "stale": 0, "book_top_match": 0, "book_top_mismatch": 0, "book_top_not_checked": 0,
    })

    # ----- helpers -------------------------------------------------------
    def _book(self, side: str) -> dict[int, tuple[int, str, str]]:
        return self.bids if side == "bid" else self.asks

    def _apply_levels(self, levels: list[Level]) -> None:
        for lv in levels:
            book = self._book(lv.side)
            if lv.qty_e8 == 0:
                book.pop(lv.price_e8, None)
            else:
                book[lv.price_e8] = (lv.qty_e8, lv.price_str, lv.qty_str)

    def _clear_book(self) -> None:
        self.bids.clear()
        self.asks.clear()

    def _go_await(self) -> None:
        self.state = AWAIT
        self.last_u = None
        self._clear_book()
        self.snapshot_inflight = False

    def _want_snapshot(self, now_ns: int) -> bool:
        if self.state != AWAIT or self.snapshot_inflight:
            return False
        return now_ns - self.last_snapshot_request_ns >= SNAPSHOT_MIN_INTERVAL_NS

    def _mark_requested(self, now_ns: int) -> None:
        self.snapshot_inflight = True
        self.last_snapshot_request_ns = now_ns

    # ----- public API ----------------------------------------------------
    def on_connection_reset(self, ref: Ref, reason: str) -> list[Out]:
        """Websocket reconnect or process-level restart of the stream.

        Continuity cannot be assumed across a connection boundary, so the book
        is discarded and the buffered events are finalised as ``discarded_gap``.
        """
        outs: list[Out] = []
        for p in self.buffer:
            outs.append(DepthOut(p.ev, "discarded_gap", self.epoch, p.ref))
        self.buffer = []
        self.epoch += 1
        self.counters["resyncs"] += 1
        self._go_await()
        outs.append(SyncOut("resync_reset", reason, self.epoch, ref))
        return outs

    def on_depth(self, ev: DepthDiff, ref: Ref) -> tuple[list[Out], bool]:
        """Returns (outputs, need_snapshot)."""
        outs: list[Out] = []
        if self.state == LIVE:
            assert self.last_u is not None
            if ev.prev_final_update_id == self.last_u:
                if ev.final_update_id <= self.last_u:
                    self.counters["stale"] += 1
                    outs.append(DepthOut(ev, "stale_live", self.epoch, ref))
                    return outs, False
                self._apply_levels(ev.levels)
                self.last_u = ev.final_update_id
                self.counters["applied"] += 1
                outs.append(DepthOut(ev, "applied", self.epoch, ref))
                return outs, False
            # Gap: the stream skipped updates. Start over from a fresh snapshot.
            self.counters["gaps"] += 1
            outs.append(SyncOut("gap", f"pu={ev.prev_final_update_id} expected_prev_u={self.last_u} U={ev.first_update_id} u={ev.final_update_id}", self.epoch, ref))
            outs.append(DepthOut(ev, "gap_trigger", self.epoch, ref))
            self.epoch += 1
            self.counters["resyncs"] += 1
            self._go_await()
            # The gap-triggering event is NOT applied; it remains the first buffered event.
            self.buffer = [_Pending(ev, ref)]
            return outs, self._want_snapshot(ref.recv_ns)
        # AWAIT: buffer everything since the last reset.
        self.buffer.append(_Pending(ev, ref))
        return outs, self._want_snapshot(ref.recv_ns)

    def on_snapshot_request(self, now_ns: int) -> int:
        """Mark a snapshot as requested; returns the epoch the response must carry."""
        self._mark_requested(now_ns)
        return self.epoch

    def on_snapshot_failed(self, epoch: int, detail: str, ref: Ref) -> list[Out]:
        self.snapshot_inflight = False
        return [SyncOut("snapshot_failed", detail, epoch, ref)]

    def on_snapshot(self, epoch: int, snap: DepthSnapshot, ref: Ref) -> tuple[list[Out], bool]:
        """Returns (outputs, need_snapshot)."""
        self.snapshot_inflight = False
        if epoch != self.epoch or self.state != AWAIT:
            return [SyncOut("snapshot_ignored", "stale epoch", epoch, ref), SnapshotOut(snap, epoch, ref, aligned=False)], False

        outs: list[Out] = [SnapshotOut(snap, epoch, ref, aligned=False)]
        lid = snap.last_update_id
        buf = self.buffer
        if not buf:
            return outs, self._want_snapshot(ref.recv_ns)

        # Stage 3: drop events fully covered by the snapshot (u < lastUpdateId).
        first_cover = next((i for i, p in enumerate(buf) if p.ev.final_update_id >= lid), None)
        if first_cover is None:
            # Snapshot is newer than everything buffered; wait for newer events.
            outs.append(SyncOut("align_failed", f"snapshot_newer_than_buffer lastUpdateId={lid}", epoch, ref))
            self.counters["align_failed"] += 1
            return outs, self._want_snapshot(ref.recv_ns) or False

        covering = buf[first_cover].ev
        if not (covering.first_update_id <= lid <= covering.final_update_id):
            # Stage 4 failure: a gap sits between the snapshot and the buffer.
            outs.append(SyncOut("align_failed", f"snapshot_older_than_buffer lastUpdateId={lid} U={covering.first_update_id}", epoch, ref))
            self.counters["align_failed"] += 1
            for p in buf[:first_cover]:
                outs.append(DepthOut(p.ev, "stale_before_snapshot", epoch, p.ref))
            self.buffer = buf[first_cover:]
            return outs, self._want_snapshot(ref.recv_ns)

        # Stage 5 pre-check over the whole aligned tail (no mutation yet).
        for j in range(first_cover + 1, len(buf)):
            if buf[j].ev.prev_final_update_id != buf[j - 1].ev.final_update_id:
                # Buffered gap: keep only events from the broken point on, then retry.
                outs.append(SyncOut("align_failed", f"buffer_gap at U={buf[j].ev.first_update_id}", epoch, ref))
                self.counters["align_failed"] += 1
                for p in buf[:j]:
                    outs.append(DepthOut(p.ev, "discarded_gap", epoch, p.ref))
                self.buffer = buf[j:]
                return outs, self._want_snapshot(ref.recv_ns)

        # Commit alignment.
        for p in buf[:first_cover]:
            outs.append(DepthOut(p.ev, "stale_before_snapshot", epoch, p.ref))
        self._clear_book()
        self._apply_levels(snap.bids)
        self._apply_levels(snap.asks)
        self.last_u = lid
        self.state = LIVE
        self.counters["snapshots_ok"] += 1
        outs[0] = SnapshotOut(snap, epoch, ref, aligned=True)
        outs.append(SyncOut("snapshot_ok", f"lastUpdateId={lid} bids={len(snap.bids)} asks={len(snap.asks)} buffered_aligned={len(buf) - first_cover}", epoch, ref))
        for k, p in enumerate(buf[first_cover:]):
            self._apply_levels(p.ev.levels)
            self.last_u = p.ev.final_update_id
            self.counters["applied"] += 1
            outs.append(DepthOut(p.ev, "applied_aligned" if k == 0 else "applied", epoch, p.ref))
        self.buffer = []
        return outs, False

    def on_book_ticker(self, bt: BookTicker, ref: Ref) -> tuple[list[Out], str]:
        """Compare reconstructed top-of-book with bookTicker at the same update id."""
        if self.state != LIVE or self.last_u != bt.update_id or not self.bids or not self.asks:
            self.counters["book_top_not_checked"] += 1
            return [], "not_checked"
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        bq, _, _ = self.bids[best_bid]
        aq, _, _ = self.asks[best_ask]
        ok = best_bid == bt.bid_e8 and bq == bt.bid_qty_e8 and best_ask == bt.ask_e8 and aq == bt.ask_qty_e8
        if ok:
            self.counters["book_top_match"] += 1
            return [], "match"
        self.counters["book_top_mismatch"] += 1
        return [SyncOut("book_top_mismatch", f"u={bt.update_id} book_bid={best_bid}/{bq} bt_bid={bt.bid_e8}/{bt.bid_qty_e8} book_ask={best_ask}/{aq} bt_ask={bt.ask_e8}/{bt.ask_qty_e8}", self.epoch, ref)], "mismatch"

    def finish(self) -> list[Out]:
        """Finalise buffered events at shutdown (never claimed as synchronised)."""
        outs: list[Out] = [DepthOut(p.ev, "pending_unsynced", self.epoch, p.ref) for p in self.buffer]
        self.buffer = []
        return outs

    @property
    def synchronised(self) -> bool:
        return self.state == LIVE

    def top(self) -> tuple[int | None, int | None]:
        if not self.bids or not self.asks:
            return None, None
        return max(self.bids), min(self.asks)
