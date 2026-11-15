"""Stage 3: feature build.

Book-level features (one row per depth snapshot, on the EXCHANGE clock:
t_ex = recv_ts - book_lag_ms from the audit):
  mid, spread_ticks, spread_bps, micro_dev_ticks (microprice - mid),
  imb1 (top-of-book queue imbalance), imb5, imb10 (depth imbalance over 5/10 levels),
  flow_1s / flow_5s (aggressor volume imbalance of trades in the trailing window, in [-1, 1]),
  ntr_1s (trade count trailing 1 s), rv_10s / rv_60s (realised vol of mid log-returns, bps),
  btc_ret_1s / btc_ret_5s (BTC mid return over trailing window, bps; cross-asset feature),
  fwd_1s / fwd_5s / fwd_30s (forward mid change, bps; LABELS ONLY, never used as inputs).
Trade-level features (one row per public trade):
  side (+1 buyer aggressor, -1 seller aggressor), mid_pre (mid of last snapshot before the trade),
  eff_half_spread_bps = side*(price - mid_pre)/mid_pre,
  maker_markout_{h}_bps = -side*(mid(t+h) - price)/price  (P&L of the passive side, before fees),
  adverse_{h}_bps = side*(mid(t+h) - mid_pre)/mid_pre      (adverse-selection component),
  plus the book features at mid_pre (imb1, rv_10s, flow_1s, btc_ret_1s) for conditioning.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from config import CLEAN, FEAT, RESULTS, SYMBOLS, TICK

HORIZONS = {"1s": 1.0, "5s": 5.0, "30s": 30.0}


def trailing_sum(t_ev: np.ndarray, v: np.ndarray, t_q: np.ndarray, win_ns: int) -> np.ndarray:
    """Sum of v over events with t_q - win < t_ev <= t_q (events sorted)."""
    c = np.concatenate([[0.0], np.cumsum(v)])
    hi = np.searchsorted(t_ev, t_q, side="right")
    lo = np.searchsorted(t_ev, t_q - win_ns, side="right")
    return c[hi] - c[lo]


def asof(t_src: np.ndarray, v: np.ndarray, t_q: np.ndarray) -> np.ndarray:
    """Value of the last source row with t_src <= t_q (NaN before the first)."""
    i = np.searchsorted(t_src, t_q, side="right") - 1
    out = v[np.clip(i, 0, None)].astype(float)
    out[i < 0] = np.nan
    return out


def book_lag_ns(sym: str) -> int:
    s = json.loads((RESULTS / "audit" / "audit_summary.json").read_text())
    return int(s[sym]["book_lag_ms"]) * 1_000_000


def build_symbol(sym: str, btc_mid: tuple[np.ndarray, np.ndarray] | None):
    d = pd.read_parquet(CLEAN / "depth" / f"{sym}.parquet")
    t = pd.read_parquet(CLEAN / "trade" / f"{sym}.parquet")
    tick = TICK[sym]
    tb = d["ts_ns"].to_numpy() - book_lag_ns(sym)
    bp0, ap0 = d["b_p0"].to_numpy(), d["a_p0"].to_numpy()
    bq = d[[f"b_q{k}" for k in range(10)]].to_numpy()
    aq = d[[f"a_q{k}" for k in range(10)]].to_numpy()
    mid = (bp0 + ap0) / 2
    f = pd.DataFrame({"t_ex": tb, "seg": d["seg"].to_numpy(), "mid": mid})
    f["spread_ticks"] = np.round((ap0 - bp0) / tick)
    f["spread_bps"] = (ap0 - bp0) / mid * 1e4
    micro = (bp0 * aq[:, 0] + ap0 * bq[:, 0]) / (bq[:, 0] + aq[:, 0])
    f["micro_dev_ticks"] = (micro - mid) / tick
    for n in (1, 5, 10):
        b, a = bq[:, :n].sum(1), aq[:, :n].sum(1)
        f[f"imb{n}"] = (b - a) / (b + a)
    tt = t["ts_ns"].to_numpy()
    side = np.where(t["buyer_is_maker"].to_numpy(), -1.0, 1.0)
    qty = t["qty"].to_numpy()
    for w in (1, 5):
        sv = trailing_sum(tt, side * qty, tb, w * 1_000_000_000)
        tv = trailing_sum(tt, qty, tb, w * 1_000_000_000)
        f[f"flow_{w}s"] = np.where(tv > 0, sv / np.where(tv > 0, tv, 1), 0.0)
    f["ntr_1s"] = trailing_sum(tt, np.ones_like(qty), tb, 1_000_000_000)
    lr = np.concatenate([[0.0], np.diff(np.log(mid))]) * 1e4
    lr[np.concatenate([[True], np.diff(f["seg"].to_numpy()) != 0])] = 0.0
    for w in (10, 60):
        f[f"rv_{w}s"] = np.sqrt(trailing_sum(tb, lr ** 2, tb, w * 1_000_000_000))
    if btc_mid is not None:
        bt, bm = btc_mid
        now = asof(bt, bm, tb)
        for w in (1, 5):
            f[f"btc_ret_{w}s"] = (now / asof(bt, bm, tb - w * 1_000_000_000) - 1) * 1e4
    else:
        f["btc_ret_1s"] = np.nan
        f["btc_ret_5s"] = np.nan
    for h, sec in HORIZONS.items():
        fm = asof(tb, mid, tb + int(sec * 1e9))
        fm[tb + int(sec * 1e9) > tb[-1]] = np.nan
        f[f"fwd_{h}"] = (fm / mid - 1) * 1e4

    # ---------------- trade-level markouts
    px = t["price"].to_numpy()
    tr = pd.DataFrame({"t_ex": tt, "price": px, "qty": qty, "side": side})
    ib = np.searchsorted(tb, tt, side="right") - 1
    ok = ib >= 0
    tr = tr[ok].reset_index(drop=True)
    ib = ib[ok]
    tt, px, side = tr["t_ex"].to_numpy(), tr["price"].to_numpy(), tr["side"].to_numpy()
    mpre = mid[ib]
    tr["mid_pre"] = mpre
    tr["eff_half_spread_bps"] = side * (px - mpre) / mpre * 1e4
    for h, sec in HORIZONS.items():
        mh = asof(tb, mid, tt + int(sec * 1e9))
        mh[tt + int(sec * 1e9) > tb[-1]] = np.nan
        tr[f"maker_markout_{h}_bps"] = -side * (mh - px) / px * 1e4
        tr[f"adverse_{h}_bps"] = side * (mh - mpre) / mpre * 1e4
    for c in ("imb1", "imb5", "rv_10s", "flow_1s", "btc_ret_1s", "spread_ticks"):
        tr[c] = f[c].to_numpy()[ib]
    tr["book_age_ms"] = (tt - tb[ib]) / 1e6
    return f, tr


def main() -> None:
    FEAT.mkdir(parents=True, exist_ok=True)
    btc = None
    for sym in SYMBOLS:  # BTCUSDT first, so its mid is available as a cross-asset input
        f, tr = build_symbol(sym, btc if sym != "BTCUSDT" else None)
        if sym == "BTCUSDT":
            btc = (f["t_ex"].to_numpy(), f["mid"].to_numpy())
        f.to_parquet(FEAT / f"book_{sym}.parquet", index=False)
        tr.to_parquet(FEAT / f"trades_{sym}.parquet", index=False)
        print(sym, len(f), len(tr), flush=True)


if __name__ == "__main__":
    main()
