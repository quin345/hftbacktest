"""Parser, fixed-point, archive and ILP tests.

All JSON below is a SYNTHETIC fixture (hand-written, shaped like the documented
Binance USD-M payloads). It is NOT captured market data. Live evidence lives in
the QuestDB tables and the report, and is checked by scripts/verify_questdb.py.
"""

import gzip
import json
from pathlib import Path

import pytest

from btc_l2.archive import ArchiveWriter, read_archive
from btc_l2.ilp import assert_loopback, ilp_line
from btc_l2.parse import (
    ParseError, classify_frame, parse_agg_trade, parse_book_ticker, parse_depth, parse_snapshot, to_e8,
)

# SYNTHETIC fixture: depth diff shaped like documented USD-M payload.
SYN_DEPTH = json.dumps({"stream": "btcusdt@depth@100ms", "data": {
    "e": "depthUpdate", "E": 1700000000100, "T": 1700000000099, "s": "BTCUSDT",
    "U": 101, "u": 105, "pu": 100,
    "b": [["65910.10", "1.500"], ["65909.90", "0.000"]],
    "a": [["65910.20", "2.000"]],
}})
# SYNTHETIC fixture: bookTicker.
SYN_BOOK = json.dumps({"stream": "btcusdt@bookTicker", "data": {
    "e": "bookTicker", "u": 105, "s": "BTCUSDT", "b": "65910.10", "B": "1.500",
    "a": "65910.20", "A": "2.000", "T": 1700000000099, "E": 1700000000100,
}})
# SYNTHETIC fixture: aggTrade, buyer is maker -> aggressor SELL.
SYN_AGG = json.dumps({"stream": "btcusdt@aggTrade", "data": {
    "e": "aggTrade", "E": 1700000000200, "a": 42, "s": "BTCUSDT", "p": "65910.10", "q": "0.003",
    "f": 900, "l": 901, "T": 1700000000199, "m": True,
}})


def test_to_e8_exact_and_rejects_extra_precision():
    assert to_e8("65910.10") == 6591010000000
    assert to_e8("0.001") == 100000
    assert to_e8("0") == 0
    with pytest.raises(ParseError):
        to_e8("1.123456789")  # 9 decimals would be silently rounded -> rejected
    with pytest.raises(ParseError):
        to_e8("-1")
    with pytest.raises(ParseError):
        to_e8("abc")


def test_classify_and_parse_depth_book_agg():
    d = classify_frame(SYN_DEPTH)
    assert d.kind == "depth" and d.stream == "btcusdt@depth@100ms"
    ev = parse_depth(d.payload)
    assert (ev.first_update_id, ev.final_update_id, ev.prev_final_update_id) == (101, 105, 100)
    assert [lv.side for lv in ev.levels] == ["bid", "bid", "ask"]
    assert ev.bids[1].is_remove and not ev.bids[0].is_remove
    assert ev.asks[0].price_e8 == to_e8("65910.20")
    bt = parse_book_ticker(classify_frame(SYN_BOOK).payload)
    assert bt.update_id == 105 and bt.bid_e8 == to_e8("65910.10") and bt.ask_qty_e8 == to_e8("2.000")
    t = parse_agg_trade(classify_frame(SYN_AGG).payload)
    assert t.aggressor_side == "SELL" and t.agg_trade_id == 42 and (t.first_trade_id, t.last_trade_id) == (900, 901)


def test_aggressor_convention_buy():
    payload = json.loads(SYN_AGG)["data"]
    payload["m"] = False
    assert parse_agg_trade(payload).aggressor_side == "BUY"


def test_control_and_unknown_frames():
    assert classify_frame('{"result":null,"id":1}').kind == "control"
    assert classify_frame('{"stream":"x@foo","data":{"e":"markPrice"}}').kind == "unknown"


def test_malformed_frames_rejected():
    with pytest.raises(ParseError):
        classify_frame("not json")
    with pytest.raises(ParseError):
        classify_frame(SYN_DEPTH + "\n")  # archive is line-based
    bad = json.loads(SYN_DEPTH)
    bad["data"]["U"], bad["data"]["u"] = 200, 105
    with pytest.raises(ParseError):
        parse_depth(bad["data"])
    bad = json.loads(SYN_DEPTH)
    bad["data"]["pu"] = 101  # pu must be < U
    with pytest.raises(ParseError):
        parse_depth(bad["data"])


def test_snapshot_parse_keeps_levels():
    snap = parse_snapshot('{"lastUpdateId":1000,"E":1700000000300,"T":1700000000299,'
                          '"bids":[["65900.00","0.500"]],"asks":[["65901.00","0.250"]]}')
    assert snap.last_update_id == 1000 and len(snap.bids) == 1 and snap.asks[0].qty_e8 == to_e8("0.25")


def test_archive_roundtrip_and_truncation_tolerance(tmp_path: Path):
    p = tmp_path / "a.ndjson.gz"
    w = ArchiveWriter(p)
    assert w.append(1, SYN_DEPTH) == 1
    assert w.append(2, SYN_BOOK) == 2
    w.flush()
    w.close()
    rows = list(read_archive(p))
    assert [r[0] for r in rows] == [1, 2]
    assert rows[0][1] == 1 and rows[0][2] == SYN_DEPTH  # exact bytes preserved
    # Simulate a kill mid-member: truncate the gzip stream; reader must keep complete lines.
    raw = p.read_bytes()
    p.write_bytes(raw[: len(raw) - 12])
    got = list(read_archive(p))
    assert len(got) >= 1 and got[0][2] == SYN_DEPTH


def test_archive_rejects_multiline(tmp_path: Path):
    w = ArchiveWriter(tmp_path / "b.ndjson.gz")
    with pytest.raises(ValueError):
        w.append(1, "a\nb")
    w.close()


def test_ilp_line_encoding_and_escaping():
    line = ilp_line("tbl", {"symbol": "BTC USDT", "side": "bid,x"},
                    {"n": 3, "f": 1.5, "b": True, "s": 'q"x\\y', "none": None}, 1234)
    assert line == 'tbl,symbol=BTC\\ USDT,side=bid\\,x n=3i,f=1.5,b=t,s="q\\"x\\\\y" 1234'
    with pytest.raises(ValueError):
        ilp_line("t", {"a": "b"}, {"x": None}, 1)
    with pytest.raises(ValueError):
        ilp_line("t", {}, {"x": float("nan")}, 1)


@pytest.mark.parametrize("url", ["http://127.0.0.1:9000", "http://localhost:9000", "http://[::1]:9000"])
def test_loopback_accepted(url):
    assert assert_loopback(url).startswith("http://")


@pytest.mark.parametrize("url", ["http://0.0.0.0:9000", "http://139.180.205.23:9000", "https://questdb.example.com", "tcp://127.0.0.1:9009"])
def test_non_loopback_refused(url):
    with pytest.raises(ValueError):
        assert_loopback(url)
