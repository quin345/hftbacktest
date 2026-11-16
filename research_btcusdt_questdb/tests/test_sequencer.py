"""Sequencer tests on SYNTHETIC update sequences (not captured market data).

Each scenario encodes a rule from the official Binance USD-M local-order-book
procedure: alignment on U <= lastUpdateId <= u, pu == previous u continuity,
gap -> resync, and reconnect -> discard buffered state.
"""

import json

from btc_l2.parse import BookTicker, DepthDiff, DepthSnapshot, Level, parse_book_ticker, parse_depth, parse_snapshot
from btc_l2.parse import to_e8
from btc_l2.sequencer import AWAIT, LIVE, DepthOut, DepthSequencer, Ref, SnapshotOut, SyncOut


def _lv(side, i, price, qty):
    return Level(side, i, price, qty, to_e8(price), to_e8(qty))


def _ev(U, u, pu, bids=(), asks=()):
    """SYNTHETIC depth diff."""
    return DepthDiff(
        symbol="BTCUSDT", event_time_ms=1700000000000 + u, tx_time_ms=1700000000000 + u,
        first_update_id=U, final_update_id=u, prev_final_update_id=pu,
        bids=[_lv("bid", i, p, q) for i, (p, q) in enumerate(bids)],
        asks=[_lv("ask", i, p, q) for i, (p, q) in enumerate(asks)],
    )


def _ref(n):
    return Ref("public.ndjson.gz", n, 1_700_000_000_000_000_000 + n * 1_000_000_000, "public-1")


def _snap(lid, bids, asks):
    """SYNTHETIC REST snapshot."""
    return DepthSnapshot(lid, None, None,
                         [_lv("bid", i, p, q) for i, (p, q) in enumerate(bids)],
                         [_lv("ask", i, p, q) for i, (p, q) in enumerate(asks)])


def _statuses(outs, cls=DepthOut):
    return [(o.ev.first_update_id, o.ev.final_update_id, o.status) for o in outs if isinstance(o, cls)]


def test_alignment_drops_stale_and_labels_first_aligned_event():
    s = DepthSequencer("BTCUSDT")
    # Buffer: contiguous chain 90..95, 96..99, 100..105 (SYNTHETIC).
    s.on_depth(_ev(90, 95, 89), _ref(1))
    s.on_depth(_ev(96, 99, 95), _ref(2))
    _, need = s.on_depth(_ev(100, 105, 99, bids=[("65910.10", "1.0")]), _ref(3))
    assert need is True  # first buffered event -> request a snapshot
    epoch = s.on_snapshot_request(_ref(4).recv_ns)
    outs, _ = s.on_snapshot(epoch, _snap(102, [("65910.00", "2.0")], [("65911.00", "3.0")]), _ref(5))
    assert s.state == LIVE and s.last_u == 105
    st = _statuses(outs)
    # 90..95 and 96..99 are fully covered (u < 102) -> stale; 100..105 covers 102 -> aligned.
    assert st == [(90, 95, "stale_before_snapshot"), (96, 99, "stale_before_snapshot"), (100, 105, "applied_aligned")]
    assert any(isinstance(o, SnapshotOut) and o.aligned for o in outs)
    assert s.bids[to_e8("65910.10")][0] == to_e8("1.0")


def test_snapshot_older_than_buffer_requires_refetch():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(200, 205, 199), _ref(1))  # SYNTHETIC: buffer starts at U=200
    epoch = s.on_snapshot_request(_ref(2).recv_ns)
    outs, need = s.on_snapshot(epoch, _snap(150, [], []), _ref(3))  # lastUpdateId 150 < U 200
    assert s.state == AWAIT and not s.synchronised
    assert any(isinstance(o, SyncOut) and o.event_type == "align_failed" for o in outs)
    assert len(s.buffer) == 1  # buffer kept for the next snapshot attempt
    assert isinstance(outs[0], SnapshotOut) and outs[0].aligned is False


def test_buffer_internal_gap_discards_prefix():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(10, 12, 9), _ref(1))
    s.on_depth(_ev(13, 15, 12), _ref(2))
    s.on_depth(_ev(20, 22, 19), _ref(3))  # SYNTHETIC gap: pu=19 != previous u=15
    epoch = s.on_snapshot_request(_ref(4).recv_ns)
    outs, _ = s.on_snapshot(epoch, _snap(14, [], []), _ref(5))
    assert s.state == AWAIT
    st = _statuses(outs)
    assert (10, 12, "stale_before_snapshot") in st or (10, 12, "discarded_gap") in st
    assert (10, 12, "discarded_gap") in st and (13, 15, "discarded_gap") in st
    assert [p.ev.first_update_id for p in s.buffer] == [20]


def test_live_gap_triggers_resync_and_does_not_apply_gap_event():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(1, 3, 0), _ref(1))
    epoch = s.on_snapshot_request(_ref(2).recv_ns)
    s.on_snapshot(epoch, _snap(2, [("1.0", "1.0")], [("2.0", "1.0")]), _ref(3))
    assert s.state == LIVE and s.last_u == 3
    outs, need = s.on_depth(_ev(10, 12, 9, bids=[("1.0", "5.0")]), _ref(4))  # pu=9 != 3 -> gap
    kinds = [type(o).__name__ for o in outs]
    assert "SyncOut" in kinds and any(isinstance(o, DepthOut) and o.status == "gap_trigger" for o in outs)
    assert s.epoch == 1 and s.state == AWAIT and s.counters["gaps"] == 1
    # Book is cleared on resync and the gap-triggering event must NOT have been applied.
    assert s.bids == {} and s.asks == {}
    assert [p.ev.first_update_id for p in s.buffer] == [10]
    assert need is True


def test_stale_snapshot_after_epoch_change_is_ignored():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(1, 3, 0), _ref(1))
    old_epoch = s.on_snapshot_request(_ref(2).recv_ns)
    s.on_connection_reset(_ref(3), "test reconnect")  # epoch advances
    outs, _ = s.on_snapshot(old_epoch, _snap(2, [], []), _ref(4))
    assert s.state == AWAIT
    assert any(isinstance(o, SyncOut) and o.event_type == "snapshot_ignored" for o in outs)


def test_reconnect_discards_buffer_as_discarded_gap():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(1, 3, 0), _ref(1))
    s.on_depth(_ev(4, 6, 3), _ref(2))
    outs = s.on_connection_reset(_ref(3), "disconnect")
    assert _statuses(outs) == [(1, 3, "discarded_gap"), (4, 6, "discarded_gap")]
    assert s.buffer == [] and s.state == AWAIT and s.epoch == 1


def test_finish_labels_unsynchronised_pending_events():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(1, 3, 0), _ref(1))
    assert _statuses(s.finish()) == [(1, 3, "pending_unsynced")]
    assert not s.synchronised


def test_book_top_cross_check_match_and_mismatch():
    s = DepthSequencer("BTCUSDT")
    s.on_depth(_ev(1, 3, 0), _ref(1))
    epoch = s.on_snapshot_request(_ref(2).recv_ns)
    s.on_snapshot(epoch, _snap(3, [("65910.10", "1.5")], [("65910.20", "2.0")]), _ref(3))
    bt_ok = BookTicker("BTCUSDT", 3, 1, 1, "65910.10", "1.500", "65910.20", "2.000",
                       to_e8("65910.10"), to_e8("1.5"), to_e8("65910.20"), to_e8("2.0"))
    _, check = s.on_book_ticker(bt_ok, _ref(4))
    assert check == "match"
    bt_bad = BookTicker("BTCUSDT", 3, 1, 1, "65910.10", "9.000", "65910.20", "2.000",
                        to_e8("65910.10"), to_e8("9"), to_e8("65910.20"), to_e8("2.0"))
    outs, check = s.on_book_ticker(bt_bad, _ref(5))
    assert check == "mismatch" and outs and outs[0].event_type == "book_top_mismatch"
    # Different update id than the book -> not checked (no false claim).
    _, check = s.on_book_ticker(BookTicker("BTCUSDT", 99, 1, 1, "1", "1", "2", "1", 1, 1, 2, 1), _ref(6))
    assert check == "not_checked"
