"""Stage 2: data audit + cleaning.

Writes results/audit/*.csv, results/audit/audit_summary.json and data/clean/<table>/<symbol>.parquet.

Key fact about timestamps (from spot_l2_trades/collector.py): spot_trade.timestamp is the
EXCHANGE trade time (T, ms), whereas spot_depth10.timestamp is the LOCAL RECEIVE time (ns).
The two clocks differ by network latency + local clock offset, estimated here by a lag scan
and stored as `book_lag_ms` (recv_time - exchange_time). Downstream stages shift book
timestamps by -book_lag_ms to put both streams on the exchange clock.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from config import CLEAN, RAW, RESULTS, SYMBOLS, TICK, WINDOW_END, WINDOW_START

AUD = RESULTS / "audit"
GAP_SEGMENT_S = 2.0  # depth silence longer than this starts a new book segment


def hour_counts(ts_ns: np.ndarray) -> pd.Series:
    return pd.Series(1, index=pd.to_datetime(ts_ns, utc=True).floor("h")).groupby(level=0).sum()


def lag_scan(d: pd.DataFrame, t: pd.DataFrame, lags_ms=range(-300, 601, 5)) -> pd.DataFrame:
    """Score = share of trades printed exactly at the touch of the latest book snapshot
    (buy aggressor at ask1, sell aggressor at bid1) when book times are shifted by -lag."""
    bt = d["ts_ns"].to_numpy()
    bid, ask = d["b_p0"].to_numpy(), d["a_p0"].to_numpy()
    tt = t["ts_ns"].to_numpy()
    px = t["price"].to_numpy()
    buy = ~t["buyer_is_maker"].to_numpy()
    rows = []
    for L in lags_ms:
        idx = np.searchsorted(bt - L * 1_000_000, tt, side="right") - 1
        ok = idx >= 0
        i = idx[ok]
        hit = np.where(buy[ok], np.abs(px[ok] - ask[i]) < 1e-6, np.abs(px[ok] - bid[i]) < 1e-6)
        through = np.where(buy[ok], px[ok] > ask[i] + 1e-9, px[ok] < bid[i] - 1e-9)
        inside = (px[ok] > bid[i] + 1e-9) & (px[ok] < ask[i] - 1e-9)
        rows.append((L, hit.mean(), through.mean(), inside.mean()))
    return pd.DataFrame(rows, columns=["lag_ms", "at_touch", "through_touch", "inside_spread"])


def audit_symbol(sym: str, summary: dict) -> None:
    t = pd.read_parquet(RAW / "trade" / f"{sym}.parquet")
    d = pd.read_parquet(RAW / "depth" / f"{sym}.parquet")
    s: dict = {"trade_rows_raw": len(t), "depth_rows_raw": len(d)}

    # ---------------- trades
    s["trade_dup_trade_id"] = int(t["trade_id"].duplicated().sum())
    t = t.drop_duplicates("trade_id").sort_values("trade_id", kind="stable").reset_index(drop=True)
    tid = t["trade_id"].to_numpy()
    dif = np.diff(tid)
    gaps = np.nonzero(dif > 1)[0]
    s["trade_id_gaps"] = int(len(gaps))
    s["trade_ids_missing"] = int((dif[gaps] - 1).sum())
    s["trade_id_range"] = int(tid[-1] - tid[0] + 1)
    s["trade_missing_share"] = s["trade_ids_missing"] / s["trade_id_range"]
    gl = pd.DataFrame({
        "after_trade_id": tid[gaps], "missing": dif[gaps] - 1,
        "t_before": pd.to_datetime(t["ts_ns"].to_numpy()[gaps], utc=True),
        "t_after": pd.to_datetime(t["ts_ns"].to_numpy()[gaps + 1], utc=True)})
    gl.insert(0, "symbol", sym)
    tts = t["ts_ns"].to_numpy()
    s["trade_ts_nonmonotonic_in_id_order"] = int((np.diff(tts) < 0).sum())
    s["trade_ts_ne_trade_time_ms"] = int((t["trade_time_ms"].to_numpy() * 1_000_000 != tts).sum())
    s["trade_nonpositive_price"] = int((t["price"] <= 0).sum())
    s["trade_nonpositive_qty"] = int((t["qty"] <= 0).sum())
    off = np.abs(t["price"] / TICK[sym] - np.round(t["price"] / TICK[sym]))
    s["trade_off_tick"] = int((off > 1e-6).sum())
    bad_t = (t["price"] <= 0) | (t["qty"] <= 0)
    t = t[~bad_t].sort_values(["ts_ns", "trade_id"], kind="stable").reset_index(drop=True)
    tg = np.diff(t["ts_ns"].to_numpy()) / 1e9
    s["trade_max_silence_s"] = float(tg.max())
    s["trade_silences_gt_10s"] = int((tg > 10).sum())

    # ---------------- depth
    s["depth_dup_exact_update_id"] = int(d["last_update_id"].duplicated().sum())
    d = d.sort_values("ts_ns", kind="stable").reset_index(drop=True)
    s["depth_ts_nonmonotonic_as_stored"] = int((np.diff(d["ts_ns"].to_numpy()) < 0).sum())
    s["depth_update_id_decreasing"] = int((np.diff(d["last_update_id"].to_numpy()) < 0).sum())
    d = d.drop_duplicates("last_update_id", keep="first").reset_index(drop=True)
    bp = d[[f"b_p{k}" for k in range(10)]].to_numpy()
    ap = d[[f"a_p{k}" for k in range(10)]].to_numpy()
    bq = d[[f"b_q{k}" for k in range(10)]].to_numpy()
    aq = d[[f"a_q{k}" for k in range(10)]].to_numpy()
    s["depth_missing_levels_rows"] = int((np.isnan(bp).any(1) | np.isnan(ap).any(1)).sum())
    crossed = d["b_p0"].to_numpy() >= d["a_p0"].to_numpy()
    s["depth_crossed_or_locked"] = int(crossed.sum())
    nonpos = (bq <= 0).any(1) | (aq <= 0).any(1) | (bp <= 0).any(1) | (ap <= 0).any(1)
    s["depth_nonpositive_size_or_price_rows"] = int(nonpos.sum())
    unsorted = (np.diff(bp, axis=1) >= 0).any(1) | (np.diff(ap, axis=1) <= 0).any(1)
    s["depth_levels_unsorted_rows"] = int(unsorted.sum())
    mism = (d["bid1_p"] != d["b_p0"]) | (d["ask1_p"] != d["a_p0"]) | \
        (d["bid1_q"] != d["b_q0"]) | (d["ask1_q"] != d["a_q0"])
    s["depth_top_vs_json_mismatch"] = int(mism.sum())
    bad = crossed | nonpos | unsorted | mism | np.isnan(bp).any(1) | np.isnan(ap).any(1)
    s["depth_rows_dropped"] = int(bad.sum())
    d = d[~bad].reset_index(drop=True)
    dg = np.diff(d["ts_ns"].to_numpy()) / 1e9
    s["depth_median_interval_ms"] = float(np.median(dg) * 1e3)
    s["depth_p99_interval_ms"] = float(np.quantile(dg, 0.99) * 1e3)
    s["depth_max_gap_s"] = float(dg.max())
    s["depth_gaps_gt_1s"] = int((dg > 1).sum())
    s["depth_gaps_gt_2s"] = int((dg > GAP_SEGMENT_S).sum())
    seg = np.concatenate([[0], np.cumsum(dg > GAP_SEGMENT_S)])
    d["seg"] = seg.astype(np.int32)
    big = np.nonzero(dg > 1)[0]
    dgl = pd.DataFrame({"symbol": sym, "gap_s": dg[big],
                        "t_before": pd.to_datetime(d["ts_ns"].to_numpy()[big], utc=True),
                        "t_after": pd.to_datetime(d["ts_ns"].to_numpy()[big + 1], utc=True)})
    s["depth_unchanged_consecutive_book"] = int((np.abs(np.diff(np.c_[bp, ap, bq, aq], axis=0)).sum(1) == 0).sum())
    spread_ticks = np.round((d["a_p0"] - d["b_p0"]).to_numpy() / TICK[sym]).astype(int)
    s["spread_ticks_share"] = {str(k): float(v) for k, v in pd.Series(spread_ticks).value_counts(normalize=True).sort_index().head(6).items()}
    s["mean_mid"] = float(((d["a_p0"] + d["b_p0"]) / 2).mean())
    s["one_tick_spread_bps_at_mean_mid"] = TICK[sym] / s["mean_mid"] * 1e4

    # ---------------- clock alignment
    ls = lag_scan(d, t)
    ls.insert(0, "symbol", sym)
    best = int(ls.loc[ls["at_touch"].idxmax(), "lag_ms"])
    s["book_lag_ms"] = best
    s["at_touch_share_at_best_lag"] = float(ls["at_touch"].max())
    s["at_touch_share_at_zero_lag"] = float(ls.loc[ls["lag_ms"] == 0, "at_touch"].iloc[0])

    hc = pd.DataFrame({"trades": hour_counts(t["ts_ns"].to_numpy()), "depth": hour_counts(d["ts_ns"].to_numpy())})
    hc.index.name = "hour_utc"
    hc.insert(0, "symbol", sym)

    (CLEAN / "trade").mkdir(parents=True, exist_ok=True)
    (CLEAN / "depth").mkdir(parents=True, exist_ok=True)
    s["trade_rows_clean"] = len(t)
    s["depth_rows_clean"] = len(d)
    t.to_parquet(CLEAN / "trade" / f"{sym}.parquet", index=False)
    d.to_parquet(CLEAN / "depth" / f"{sym}.parquet", index=False)
    summary[sym] = s
    w0, w1 = pd.Timestamp(WINDOW_START).value, pd.Timestamp(WINDOW_END).value
    cov = dict(trade_head_silence_s=(t["ts_ns"].iloc[0] - w0) / 1e9, trade_tail_silence_s=(w1 - t["ts_ns"].iloc[-1]) / 1e9,
               depth_head_silence_s=(d["ts_ns"].iloc[0] - w0) / 1e9, depth_tail_silence_s=(w1 - d["ts_ns"].iloc[-1]) / 1e9,
               max_spread_ticks=int(spread_ticks.max()))
    return gl, dgl, ls, hc, cov


# Thresholds for the anomaly report (anything at or above is listed in anomalies.csv).
ANOMALY_CHECKS = [
    ("trade_dup_trade_id", 1, "duplicate trade_id rows (dropped)"),
    ("trade_ids_missing", 1, "trade_id gaps (missing trades)"),
    ("trade_nonpositive_price", 1, "trades with price <= 0 (dropped)"),
    ("trade_nonpositive_qty", 1, "trades with qty <= 0 (dropped)"),
    ("trade_off_tick", 1, "trade prices off the tick grid"),
    ("trade_silences_gt_10s", 1, "trade silences > 10 s"),
    ("depth_dup_exact_update_id", 1, "duplicate depth last_update_id (dropped)"),
    ("depth_ts_nonmonotonic_as_stored", 1, "depth timestamps non-monotonic"),
    ("depth_update_id_decreasing", 1, "depth update id decreasing"),
    ("depth_crossed_or_locked", 1, "crossed or locked books (dropped)"),
    ("depth_nonpositive_size_or_price_rows", 1, "depth rows with size/price <= 0 (dropped)"),
    ("depth_levels_unsorted_rows", 1, "depth levels unsorted (dropped)"),
    ("depth_top_vs_json_mismatch", 1, "top-of-book columns != JSON level 1 (dropped)"),
    ("depth_missing_levels_rows", 1, "depth rows with < 10 levels (dropped)"),
    ("depth_gaps_gt_1s", 1, "depth gaps > 1 s (gaps > 2 s split book segments)"),
]
COVERAGE_LIMIT_S = 10.0  # head/tail silence at the window edges above this is an anomaly (collector stopped?)


def anomalies(summary: dict, cov: dict, depth_gaps: pd.DataFrame) -> None:
    rows = []
    for sym, s in summary.items():
        for key, thr, what in ANOMALY_CHECKS:
            if s.get(key, 0) >= thr:
                rows.append(dict(symbol=sym, check=key, value=s[key], description=what))
        for key, v in cov[sym].items():
            if key.endswith("silence_s") and v > COVERAGE_LIMIT_S:
                rows.append(dict(symbol=sym, check=key, value=round(v, 3),
                                 description=f"no rows for more than {COVERAGE_LIMIT_S:.0f} s at the window edge"))
        if cov[sym]["max_spread_ticks"] > 100:
            rows.append(dict(symbol=sym, check="max_spread_ticks", value=cov[sym]["max_spread_ticks"],
                             description="outlier spread snapshot (> 100 ticks; kept)"))
    pd.DataFrame(rows, columns=["symbol", "check", "value", "description"]).to_csv(AUD / "anomalies.csv", index=False)
    cv = pd.DataFrame(cov).T
    cv.index.name = "symbol"
    cv.to_csv(AUD / "coverage.csv")
    print(f"anomalies: {len(rows)} listed in {AUD / 'anomalies.csv'}")


def main() -> None:
    AUD.mkdir(parents=True, exist_ok=True)
    summary: dict = {}
    parts = [audit_symbol(s, summary) for s in SYMBOLS]
    for k, name in enumerate(["trade_id_gaps", "depth_gaps_gt_1s", "clock_lag_scan", "rows_per_hour"]):
        pd.concat([p[k] for p in parts]).to_csv(AUD / f"{name}.csv")
    # cross-symbol: depth for all symbols arrives on ONE websocket, so simultaneous gaps = connection drops
    (AUD / "audit_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    anomalies(summary, {sym: p[4] for sym, p in zip(SYMBOLS, parts)}, pd.concat([p[1] for p in parts]))
    for sym, s in summary.items():
        print(sym, {k: s[k] for k in ("trade_ids_missing", "trade_missing_share", "depth_rows_dropped", "depth_gaps_gt_2s", "depth_max_gap_s", "book_lag_ms", "at_touch_share_at_best_lag")})


if __name__ == "__main__":
    main()
