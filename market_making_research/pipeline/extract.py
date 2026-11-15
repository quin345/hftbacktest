"""Stage 1: extract spot_trade and spot_depth10 from QuestDB (SELECT only) into parquet.

Pulls the frozen window per symbol in CHUNK_MINUTES chunks, checks each chunk's row
count against a count() query, and writes data/raw/<table>/<symbol>.parquet.
Depth JSON levels are expanded into fixed columns b_p0..b_p9, b_q0..., a_p0..., a_q0...
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config import CHUNK_MINUTES, RAW, SYMBOLS, WINDOW_END, WINDOW_START
from qdb import select

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def chunks():
    t0 = datetime.strptime(WINDOW_START, FMT).replace(tzinfo=timezone.utc)
    t1 = datetime.strptime(WINDOW_END, FMT).replace(tzinfo=timezone.utc)
    step = timedelta(minutes=CHUNK_MINUTES)
    while t0 < t1:
        yield t0.strftime(FMT), min(t0 + step, t1).strftime(FMT)
        t0 += step


def _ts_ns(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True, format="ISO8601").dt.as_unit("ns").astype("int64").to_numpy()


def pull(table: str, cols: str, sym: str, a: str, b: str) -> pd.DataFrame:
    where = f"symbol = '{sym}' AND timestamp >= '{a}' AND timestamp < '{b}'"
    _, cnt = select(f"SELECT count() FROM {table} WHERE {where}")
    names, rows = select(f"SELECT {cols} FROM {table} WHERE {where} ORDER BY timestamp")
    if len(rows) != cnt[0][0]:
        raise RuntimeError(f"{table} {sym} {a}: got {len(rows)} rows, count() says {cnt[0][0]}")
    return pd.DataFrame(rows, columns=names)


def expand_levels(df: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for side, col in (("b", "bids_json"), ("a", "asks_json")):
        p = np.full((len(df), 10), np.nan)
        q = np.full((len(df), 10), np.nan)
        for i, s in enumerate(df[col].to_numpy()):
            lv = json.loads(s)
            for k, (pp, qq) in enumerate(lv[:10]):
                p[i, k] = float(pp)
                q[i, k] = float(qq)
        for k in range(10):
            out[f"{side}_p{k}"] = p[:, k]
            out[f"{side}_q{k}"] = q[:, k]
    return pd.DataFrame(out, index=df.index)


def main() -> None:
    (RAW / "trade").mkdir(parents=True, exist_ok=True)
    (RAW / "depth").mkdir(parents=True, exist_ok=True)
    manifest = []
    for sym in SYMBOLS:
        tr, dp = [], []
        for a, b in chunks():
            t = pull("spot_trade", "trade_id, price, qty, trade_time_ms, buyer_is_maker, timestamp", sym, a, b)
            d = pull("spot_depth10", "last_update_id, bid1_p, bid1_q, ask1_p, ask1_q, bids_json, asks_json, timestamp", sym, a, b)
            tr.append(t)
            if len(d):
                dp.append(pd.concat([d.drop(columns=["bids_json", "asks_json"]), expand_levels(d)], axis=1))
            manifest.append((sym, a, len(t), len(d)))
        t = pd.concat(tr, ignore_index=True)
        t["ts_ns"] = _ts_ns(t.pop("timestamp"))
        t["buyer_is_maker"] = t["buyer_is_maker"].astype(bool)
        d = pd.concat(dp, ignore_index=True)
        d["ts_ns"] = _ts_ns(d.pop("timestamp"))
        t.to_parquet(RAW / "trade" / f"{sym}.parquet", index=False)
        d.to_parquet(RAW / "depth" / f"{sym}.parquet", index=False)
        print(f"{sym}: trades={len(t)} depth={len(d)}", flush=True)
    pd.DataFrame(manifest, columns=["symbol", "chunk_start", "trade_rows", "depth_rows"]).to_csv(RAW / "manifest.csv", index=False)


if __name__ == "__main__":
    sys.exit(main())
