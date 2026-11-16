"""Parse Binance USD-M public market-data payloads.

Exact arithmetic: prices and quantities arrive as decimal strings. They are
kept verbatim (string form) and converted to fixed-point integers scaled by
1e8 (``*_e8``). Any value with more than 8 decimal places is rejected rather
than silently rounded. A DOUBLE copy is stored only for convenience.

Stream/endpoint notes (verified against the official USD-M docs, 2026-11):
  * diff depth ``<symbol>@depth@100ms`` is on the routed ``/public`` endpoint.
  * ``<symbol>@bookTicker`` is on ``/public``.
  * ``<symbol>@aggTrade`` is on ``/market``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

SCALE_DIGITS = 8
SCALE = 10**SCALE_DIGITS
_QUANT = Decimal(1).scaleb(-SCALE_DIGITS)


class ParseError(ValueError):
    """Malformed or unexpected payload."""


def to_e8(text: str) -> int:
    """Exact decimal string -> integer units of 1e-8. Rejects extra precision."""
    try:
        d = Decimal(text)
    except (ArithmeticError, TypeError) as exc:
        raise ParseError(f"not a decimal: {text!r}") from exc
    if not d.is_finite() or d < 0:
        raise ParseError(f"invalid decimal: {text!r}")
    if d != d.quantize(_QUANT):
        raise ParseError(f"more than {SCALE_DIGITS} decimals: {text!r}")
    return int(d * SCALE)


def to_float(text: str) -> float:
    return float(text)


@dataclass(frozen=True)
class Level:
    side: str  # "bid" | "ask"
    index: int
    price_str: str
    qty_str: str
    price_e8: int
    qty_e8: int

    @property
    def is_remove(self) -> bool:
        return self.qty_e8 == 0


def _levels(side: str, rows: Any) -> list[Level]:
    if not isinstance(rows, list):
        raise ParseError(f"{side} levels not a list")
    out = []
    for i, row in enumerate(rows):
        if not (isinstance(row, list) and len(row) == 2 and all(isinstance(x, str) for x in row)):
            raise ParseError(f"{side} level {i} malformed")
        out.append(Level(side, i, row[0], row[1], to_e8(row[0]), to_e8(row[1])))
    return out


@dataclass(frozen=True)
class DepthDiff:
    symbol: str
    event_time_ms: int  # E
    tx_time_ms: int  # T
    first_update_id: int  # U
    final_update_id: int  # u
    prev_final_update_id: int  # pu
    bids: list[Level] = field(repr=False)
    asks: list[Level] = field(repr=False)

    @property
    def levels(self) -> list[Level]:
        return self.bids + self.asks


@dataclass(frozen=True)
class DepthSnapshot:
    last_update_id: int
    event_time_ms: int | None
    tx_time_ms: int | None
    bids: list[Level] = field(repr=False)
    asks: list[Level] = field(repr=False)


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    update_id: int
    event_time_ms: int
    tx_time_ms: int
    bid_price_str: str
    bid_qty_str: str
    ask_price_str: str
    ask_qty_str: str
    bid_e8: int
    bid_qty_e8: int
    ask_e8: int
    ask_qty_e8: int


@dataclass(frozen=True)
class AggTrade:
    symbol: str
    agg_trade_id: int  # a
    price_str: str  # p
    qty_str: str  # q
    price_e8: int
    qty_e8: int
    first_trade_id: int  # f
    last_trade_id: int  # l
    event_time_ms: int  # E
    trade_time_ms: int  # T
    buyer_is_maker: bool  # m: True => buyer was the maker => aggressor (taker) SELL

    @property
    def aggressor_side(self) -> str:
        # Binance convention: m=true means the buyer is the passive side, so the
        # aggressor/taker sold into the bid. Recorded explicitly for queries.
        return "SELL" if self.buyer_is_maker else "BUY"


@dataclass(frozen=True)
class Envelope:
    """One websocket text frame, classified. ``raw`` is the exact wire text."""

    kind: str  # depth | book_ticker | agg_trade | control | unknown
    stream: str | None
    payload: dict
    raw: str


def _int(obj: dict, key: str) -> int:
    v = obj.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ParseError(f"field {key} missing or not int")
    return v


def _str(obj: dict, key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str):
        raise ParseError(f"field {key} missing or not str")
    return v


def classify_frame(raw: str) -> Envelope:
    """Classify a combined-stream or control frame without losing the raw text."""
    if "\n" in raw or "\r" in raw:
        raise ParseError("frame contains newline; archive format requires one line")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ParseError("frame is not a JSON object")
    if "result" in obj and "id" in obj and "data" not in obj:
        return Envelope("control", None, obj, raw)
    stream = obj.get("stream")
    data = obj.get("data")
    if not isinstance(stream, str) or not isinstance(data, dict):
        return Envelope("unknown", None, obj, raw)
    event = data.get("e")
    kind = {"depthUpdate": "depth", "bookTicker": "book_ticker", "aggTrade": "agg_trade"}.get(event, "unknown")
    return Envelope(kind, stream, data, raw)


def parse_depth(data: dict) -> DepthDiff:
    if data.get("e") != "depthUpdate":
        raise ParseError("not a depthUpdate")
    ev = DepthDiff(
        symbol=_str(data, "s"),
        event_time_ms=_int(data, "E"),
        tx_time_ms=_int(data, "T"),
        first_update_id=_int(data, "U"),
        final_update_id=_int(data, "u"),
        prev_final_update_id=_int(data, "pu"),
        bids=_levels("bid", data.get("b", [])),
        asks=_levels("ask", data.get("a", [])),
    )
    if ev.first_update_id > ev.final_update_id:
        raise ParseError("U > u")
    if ev.prev_final_update_id >= ev.first_update_id:
        raise ParseError("pu >= U")
    return ev


def parse_snapshot(text: str) -> DepthSnapshot:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"snapshot invalid JSON: {exc}") from exc
    if not isinstance(obj, dict) or "lastUpdateId" not in obj:
        raise ParseError("snapshot missing lastUpdateId")
    return DepthSnapshot(
        last_update_id=_int(obj, "lastUpdateId"),
        event_time_ms=obj.get("E") if isinstance(obj.get("E"), int) else None,
        tx_time_ms=obj.get("T") if isinstance(obj.get("T"), int) else None,
        bids=_levels("bid", obj.get("bids", [])),
        asks=_levels("ask", obj.get("asks", [])),
    )


def parse_book_ticker(data: dict) -> BookTicker:
    if data.get("e") != "bookTicker":
        raise ParseError("not a bookTicker")
    bp, bq, ap, aq = _str(data, "b"), _str(data, "B"), _str(data, "a"), _str(data, "A")
    return BookTicker(
        symbol=_str(data, "s"),
        update_id=_int(data, "u"),
        event_time_ms=_int(data, "E"),
        tx_time_ms=_int(data, "T"),
        bid_price_str=bp,
        bid_qty_str=bq,
        ask_price_str=ap,
        ask_qty_str=aq,
        bid_e8=to_e8(bp),
        bid_qty_e8=to_e8(bq),
        ask_e8=to_e8(ap),
        ask_qty_e8=to_e8(aq),
    )


def parse_agg_trade(data: dict) -> AggTrade:
    if data.get("e") != "aggTrade":
        raise ParseError("not an aggTrade")
    p, q = _str(data, "p"), _str(data, "q")
    m = data.get("m")
    if not isinstance(m, bool):
        raise ParseError("field m missing or not bool")
    return AggTrade(
        symbol=_str(data, "s"),
        agg_trade_id=_int(data, "a"),
        price_str=p,
        qty_str=q,
        price_e8=to_e8(p),
        qty_e8=to_e8(q),
        first_trade_id=_int(data, "f"),
        last_trade_id=_int(data, "l"),
        event_time_ms=_int(data, "E"),
        trade_time_ms=_int(data, "T"),
        buyer_is_maker=m,
    )
