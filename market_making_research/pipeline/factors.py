"""Factor ladder: candidate quote filters and their side-permission rules (pure numpy).

Every factor is a FILTER that can only withdraw a side; it never adds quotes. A side is quoted
at a snapshot only if every active factor allows it (logical AND). Each factor has one
threshold theta from a grid that is FIXED HERE, before any result is seen. `np.inf` means
"off" (the factor allows everything), and is always the first grid value so ties resolve to
the less restrictive choice.

Pre-declared ladder order (factor added one at a time):
  0 imb1      queue imbalance (base H1). Pull bid if imb1 < -theta, pull ask if imb1 > theta.
  1 flow_1s   trade-flow imbalance = signed aggressor volume / total volume over the last 1 s,
              in [-1, 1]. Pull bid if flow < -theta (sellers aggressive), ask if flow > theta.
  2 rv_10s    realised vol of mid log-returns over the last 10 s (bps). Pull BOTH sides if
              rv_10s > theta.
  3 spread    spread in ticks. Pull BOTH sides if spread_ticks > theta (wide = unstable book).
  4 depth_chg relative change of top-5 displayed quantity per side over the last 1 s,
              dchg = Q5(t)/Q5(t-1s) - 1. Pull bid if bid dchg < -theta (bid depth draining),
              pull ask if ask dchg < -theta.
  5 btc_ll    BTC lead-lag: BTC mid return over the last 1 s (bps). Pull bid if < -theta,
              pull ask if > theta. ETHUSDT and SOLUSDT only (not applicable to BTCUSDT).
"""
from __future__ import annotations

import numpy as np

INF = np.inf

LADDER = ["imb1", "flow_1s", "rv_10s", "spread", "depth_chg", "btc_ll"]

GRIDS = {
    "imb1": [INF, 0.9, 0.8, 0.6, 0.4, 0.2],
    "flow_1s": [INF, 0.9, 0.75, 0.5, 0.25],
    "rv_10s": [INF, 10.0, 7.0, 5.0, 4.0, 3.0, 2.0, 1.5],
    "spread": [INF, 5.0, 3.0, 2.0, 1.0],
    "depth_chg": [INF, 0.5, 0.3, 0.2, 0.1],
    "btc_ll": [INF, 3.0, 2.0, 1.0, 0.5, 0.25],
}

# Columns of data/features/book_<SYM>.parquet each factor needs.
COLUMNS = {
    "imb1": ["imb1"],
    "flow_1s": ["flow_1s"],
    "rv_10s": ["rv_10s"],
    "spread": ["spread_ticks"],
    "depth_chg": ["dchg_b_1s", "dchg_a_1s"],
    "btc_ll": ["btc_ret_1s"],
}


def applicable(factor: str, sym: str) -> bool:
    return not (factor == "btc_ll" and sym == "BTCUSDT")


def _col(f, c):
    x = np.asarray(f[c], dtype=float)
    return np.nan_to_num(x, nan=0.0)  # missing value -> neutral (filter allows quoting)


def factor_sides(factor: str, theta: float, f) -> tuple[np.ndarray, np.ndarray]:
    """(allow_bid, allow_ask) boolean arrays for one factor at one threshold."""
    n = len(f[COLUMNS[factor][0]])
    if not np.isfinite(theta):
        return np.ones(n, np.bool_), np.ones(n, np.bool_)
    if factor in ("imb1", "flow_1s", "btc_ll"):
        x = _col(f, COLUMNS[factor][0])
        return x >= -theta, x <= theta
    if factor == "rv_10s":
        ok = _col(f, "rv_10s") <= theta
        return ok, ok.copy()
    if factor == "spread":
        ok = _col(f, "spread_ticks") <= theta
        return ok, ok.copy()
    if factor == "depth_chg":
        return _col(f, "dchg_b_1s") >= -theta, _col(f, "dchg_a_1s") >= -theta
    raise ValueError(factor)


def combined_sides(thetas: dict, f) -> tuple[np.ndarray, np.ndarray]:
    """AND of every factor in `thetas` ({factor: theta})."""
    n = len(f[next(iter(f.keys()))])
    wb, wa = np.ones(n, np.bool_), np.ones(n, np.bool_)
    for fac, th in thetas.items():
        b, a = factor_sides(fac, th, f)
        wb &= b
        wa &= a
    return wb, wa


def depth_change(t: np.ndarray, q: np.ndarray, seg: np.ndarray, win_ns: int) -> np.ndarray:
    """Relative change q(t)/q(t - win) - 1, with q(t - win) the last value at or before t - win.
    Returns 0 where no earlier value exists in the same book segment (gap = unknown history)."""
    i = np.searchsorted(t, t - win_ns, side="right") - 1
    ok = i >= 0
    j = np.clip(i, 0, None)
    ok &= seg[j] == seg
    prev = q[j]
    ok &= prev > 0
    out = np.zeros(len(t))
    out[ok] = q[ok] / prev[ok] - 1.0
    return out


def trade_flow(t_trade: np.ndarray, side: np.ndarray, qty: np.ndarray, t_q: np.ndarray, win_ns: int) -> np.ndarray:
    """Signed aggressor volume / total volume of trades with t_q - win < t <= t_q; 0 if no trades."""
    c_s = np.concatenate([[0.0], np.cumsum(side * qty)])
    c_v = np.concatenate([[0.0], np.cumsum(qty)])
    hi = np.searchsorted(t_trade, t_q, side="right")
    lo = np.searchsorted(t_trade, t_q - win_ns, side="right")
    sv, tv = c_s[hi] - c_s[lo], c_v[hi] - c_v[lo]
    return np.where(tv > 0, sv / np.where(tv > 0, tv, 1.0), 0.0)
