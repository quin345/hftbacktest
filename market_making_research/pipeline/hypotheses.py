"""Stage 5: test the three market-making hypotheses with walk-forward selection.

Strategies (all quote one post-only order of fixed notional joined at the touch):
  BASE : always quote both sides (control; no parameters, so it is out-of-sample by construction).
  H1   : queue-imbalance filter. Pull the bid when imb1 < -theta, pull the ask when imb1 > theta.
  H2   : BTC lead-lag filter (ETHUSDT, SOLUSDT only). Pull the bid when BTC's trailing 1 s return
         < -theta bps, pull the ask when > +theta bps.
  H3   : volatility-regime filter. Quote nothing while trailing 10 s realised vol > theta bps.
Each theta grid includes "off" (= BASE). Grids are fixed a priori (not fitted to the full sample).

Walk-forward: hourly test folds starting WF_FIRST_TEST_HOUR. For every fold, theta is chosen
by maximising the fee-inclusive 5 s markout P&L summed over ALL data before the fold
(expanding window), separately for each (fill model, latency, fee). The chosen theta is then
scored on the fold. Fees are a post-hoc charge (they do not change fills), so every fee level
reuses the same simulation runs.

Outputs (results/): sim_runs_hourly.csv (every run, per hour), walkforward_selection.csv,
walkforward_oos_summary.csv, insample_best.csv (FLAGGED in-sample), predictive_walkforward.csv,
public_trade_markouts_by_feature.csv, params.json.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from config import FEAT, RESULTS, SYMBOLS, TICK, WF_FIRST_TEST_HOUR, WF_FOLD_MINUTES, WINDOW_END, WINDOW_START
from sim import OPTIMISTIC, QUEUE_CONS, QUEUE_PROP, load_arrays, simulate

ORDER_NOTIONAL_USD = 1000.0
MAX_INV_ORDERS = 5
LATENCIES_MS = [20, 50, 200]
MODELS = {"optimistic": OPTIMISTIC, "queue_cons": QUEUE_CONS, "queue_prop": QUEUE_PROP}
# Maker fee scenarios in bps of notional. Negative = rebate. 10 = Binance regular spot maker
# fee (0.1%), 7.5 = with the 25% BNB discount. 1.0 / 0 / -0.5 are hypothetical VIP / MM-programme
# style levels used as scenarios, not claims about any account's actual fee.
FEES_BPS = [-0.5, 0.0, 1.0, 7.5, 10.0]
GRIDS = {
    "H1_imbalance": [np.inf, 0.9, 0.8, 0.6, 0.4, 0.2],
    "H2_btc_leadlag": [np.inf, 3.0, 2.0, 1.0, 0.5, 0.25],
    "H3_vol_regime": [np.inf, 10.0, 7.0, 5.0, 4.0, 3.0, 2.0, 1.5],
}
MARKOUT_S = [1, 5, 30]
SELECT_H = 5  # markout horizon used for selection


def ts(s: str) -> int:
    return int(pd.Timestamp(s).value)


def folds():
    a = ts(WF_FIRST_TEST_HOUR)
    end = ts(WINDOW_END)
    out = []
    while a < end:
        b = min(a + WF_FOLD_MINUTES * 60 * 10**9, end)
        out.append((a, b))
        a = b
    return out


def signals(strategy: str, theta: float, f: pd.DataFrame):
    n = len(f)
    if not np.isfinite(theta):
        return np.ones(n, np.bool_), np.ones(n, np.bool_)
    if strategy == "H1_imbalance":
        x = f["imb1"].to_numpy()
        return x > -theta, x < theta
    if strategy == "H2_btc_leadlag":
        x = np.nan_to_num(f["btc_ret_1s"].to_numpy(), nan=0.0)
        return x > -theta, x < theta
    if strategy == "H3_vol_regime":
        ok = f["rv_10s"].to_numpy() <= theta
        return ok.copy(), ok.copy()
    raise ValueError(strategy)


def asof_mid(tb, mid, t):
    i = np.searchsorted(tb, t, side="right") - 1
    return mid[np.clip(i, 0, None)]


def hourly_metrics(fills, tb, mid, bounds):
    """Per-bucket metrics for one run. bounds: list of (a, b) ns intervals."""
    ft, fs, fp, fq, fbi, fwhy = fills
    notional = fp * fq
    rows = []
    # mark-to-market value path V(t) = cash(t) + inv(t) * mid(t), fees excluded (charged post hoc)
    cash = np.cumsum(-fs * fp * fq)
    inv = np.cumsum(fs * fq)
    mk = {h: fs * (asof_mid(tb, mid, ft + h * 10**9) - fp) / fp * 1e4 for h in MARKOUT_S}
    edge = fs * (mid[fbi] - fp) / fp * 1e4

    def value_at(t):
        k = np.searchsorted(ft, t, side="right") - 1
        c = cash[k] if k >= 0 else 0.0
        q = inv[k] if k >= 0 else 0.0
        return c + q * asof_mid(tb, mid, np.array([t]))[0]

    for a, b in bounds:
        m = (ft >= a) & (ft < b)
        w = notional[m]
        r = {"t0": a, "n_fills": int(m.sum()), "notional": float(w.sum()),
             "n_buy": int((fs[m] > 0).sum()), "n_sell": int((fs[m] < 0).sum()),
             "mtm_pnl_gross": value_at(b - 1) - value_at(a - 1)}
        r["edge_sum"] = float((edge[m] * w).sum())
        for code, nm in ((0, "queue"), (1, "through"), (2, "bookcross"), (3, "touch")):
            r[f"notional_{nm}"] = float(w[fwhy[m] == code].sum())
            r[f"mk5_sum_{nm}"] = float((mk[5][m] * w)[fwhy[m] == code].sum())
        for h in MARKOUT_S:
            r[f"mk{h}_sum"] = float((mk[h][m] * w).sum())  # bps * USD
        k = np.searchsorted(ft, b, side="right") - 1
        r["inv_end_usd"] = float((inv[k] if k >= 0 else 0.0) * asof_mid(tb, mid, np.array([b - 1]))[0])
        rows.append(r)
    return rows


def run_all() -> pd.DataFrame:
    hours = []
    a, end = ts(WINDOW_START), ts(WINDOW_END)
    # buckets: [start, first fold) is split hourly too, aligned to clock hours
    edges = [a] + [int(x.value) for x in pd.date_range(pd.Timestamp(WINDOW_START).ceil("h"), pd.Timestamp(WINDOW_END), freq="h", inclusive="left")] + [end]
    edges = sorted(set(e for e in edges if a <= e <= end))
    hours = list(zip(edges[:-1], edges[1:]))
    rows = []
    for sym in SYMBOLS:
        book, trades, f, _ = load_arrays(sym)
        tb, mid = book["tb"], f["mid"].to_numpy()
        oq = ORDER_NOTIONAL_USD / mid[0]
        for strat, grid in GRIDS.items():
            if strat == "H2_btc_leadlag" and sym == "BTCUSDT":
                continue
            for theta in grid:
                wb, wa = signals(strat, theta, f)
                for mname, model in MODELS.items():
                    for lat in LATENCIES_MS:
                        t0 = time.time()
                        out = simulate(book["tb"], book["bp"], book["bq"], book["ap"], book["aq"], book["seg"],
                                       trades["tt"], trades["tpx"], trades["tqty"], trades["tside"], wb, wa,
                                       lat * 1_000_000, model, oq, MAX_INV_ORDERS * oq, TICK[sym])
                        fills, n_msgs, n_rej = out[:5] + (out[7],), out[5], out[6]
                        for r in hourly_metrics(fills, tb, mid, hours):
                            r.update(symbol=sym, strategy=strat, theta=theta, model=mname, latency_ms=lat,
                                     quote_up_bid=float(wb.mean()), quote_up_ask=float(wa.mean()))
                            rows.append(r)
                        rows[-1]["n_msgs_total"] = int(n_msgs)
                        rows[-1]["n_reject_total"] = int(n_rej)
                print(sym, strat, theta, f"{time.time() - t0:.2f}s/run", flush=True)
    return pd.DataFrame(rows)


def aggregate(df: pd.DataFrame, fee: float) -> pd.DataFrame:
    g = df.groupby(["symbol", "strategy", "theta", "model", "latency_ms"], dropna=False)
    reasons = [f"{a}_{b}" for a in ("notional", "mk5_sum") for b in ("queue", "through", "bookcross", "touch")]
    o = g[["n_fills", "notional", "mtm_pnl_gross", "edge_sum"] + [f"mk{h}_sum" for h in MARKOUT_S] + reasons].sum()
    o["fees"] = o["notional"] * fee / 1e4
    o["net_mk5_usd"] = (o[f"mk{SELECT_H}_sum"] / 1e4) - o["fees"]
    o["mtm_pnl_net"] = o["mtm_pnl_gross"] - o["fees"]
    for h in MARKOUT_S:
        o[f"mk{h}_bps"] = o[f"mk{h}_sum"] / o["notional"].where(o["notional"] > 0)
    o["edge_bps"] = o["edge_sum"] / o["notional"].where(o["notional"] > 0)
    o["fee_bps"] = fee
    return o.reset_index()


def walk_forward(df: pd.DataFrame):
    sel_rows, oos_rows = [], []
    fl = folds()
    keys = ["symbol", "strategy", "model", "latency_ms"]
    for fee in FEES_BPS:
        for (a, b) in fl:
            tr = aggregate(df[df["t0"] < a], fee)
            te = aggregate(df[(df["t0"] >= a) & (df["t0"] < b)], fee)
            for k, grp in tr.groupby(keys):
                best = grp.sort_values(["net_mk5_usd", "theta"], ascending=[False, False]).iloc[0]
                base = grp[~np.isfinite(grp["theta"])].iloc[0]
                tsel = te.set_index(keys + ["theta"]).loc[tuple(k) + (best["theta"],)]
                tbase = te.set_index(keys + ["theta"]).loc[tuple(k) + (np.inf,)]
                sel_rows.append(dict(zip(keys, k), fee_bps=fee, fold_start=pd.Timestamp(a, tz="UTC"),
                                     theta=best["theta"], train_net_mk5=best["net_mk5_usd"],
                                     train_base_net_mk5=base["net_mk5_usd"]))
                oos_rows.append(dict(zip(keys, k), fee_bps=fee, fold_start=pd.Timestamp(a, tz="UTC"), theta=best["theta"],
                                     fold_mk5_bps=tsel["mk5_bps"], fold_base_mk5_bps=tbase["mk5_bps"],
                                     **{f"oos_{c}": tsel[c] for c in ("n_fills", "notional", "net_mk5_usd", "mtm_pnl_net", "mtm_pnl_gross", "mk1_sum", "mk5_sum", "mk30_sum", "edge_sum", "fees")},
                                     **{f"base_{c}": tbase[c] for c in ("n_fills", "notional", "net_mk5_usd", "mtm_pnl_net", "mk5_sum")}))
    sel, oos = pd.DataFrame(sel_rows), pd.DataFrame(oos_rows)
    g = oos.groupby(keys + ["fee_bps"])
    s = g[[c for c in oos.columns if c.startswith(("oos_", "base_"))]].sum()
    s["folds"] = g.size()
    s["folds_net_mk5_pos"] = g["oos_net_mk5_usd"].apply(lambda x: int((x > 0).sum()))
    s["folds_beat_base_mk5"] = (oos["oos_net_mk5_usd"] > oos["base_net_mk5_usd"]).groupby([oos[k] for k in keys + ["fee_bps"]]).sum()
    s["folds_mk5_bps_beat_base"] = (oos["fold_mk5_bps"] > oos["fold_base_mk5_bps"]).groupby([oos[k] for k in keys + ["fee_bps"]]).sum()
    s["oos_mk5_bps"] = s["oos_mk5_sum"] / s["oos_notional"].where(s["oos_notional"] > 0)
    s["oos_mk1_bps"] = s["oos_mk1_sum"] / s["oos_notional"].where(s["oos_notional"] > 0)
    s["oos_mk30_bps"] = s["oos_mk30_sum"] / s["oos_notional"].where(s["oos_notional"] > 0)
    s["oos_edge_bps"] = s["oos_edge_sum"] / s["oos_notional"].where(s["oos_notional"] > 0)
    s["base_mk5_bps"] = s["base_mk5_sum"] / s["base_notional"].where(s["base_notional"] > 0)
    s["thetas_chosen"] = g["theta"].apply(lambda x: ",".join(str(v) for v in x))
    oos.to_csv(RESULTS / "walkforward_oos_by_fold.csv", index=False)
    return sel, s.reset_index()


def predictive(f_by_sym) -> pd.DataFrame:
    rows = []
    edges = [ts(WINDOW_START)] + [a for a, _ in folds()] + [ts(WINDOW_END)]
    for sym, f in f_by_sym.items():
        for a, b in zip(edges[:-1], edges[1:]):
            m = f[(f["t_ex"] >= a) & (f["t_ex"] < b)].dropna(subset=["fwd_1s", "fwd_5s"])
            for feat, lab in (("imb1", "fwd_1s"), ("imb1", "fwd_5s"), ("btc_ret_1s", "fwd_1s"), ("btc_ret_1s", "fwd_5s"),
                              ("flow_1s", "fwd_1s"), ("rv_10s", "abs_fwd_1s")):
                if m[feat].isna().all():
                    continue
                y = m["fwd_1s"].abs() if lab == "abs_fwd_1s" else m[lab]
                rows.append(dict(symbol=sym, period_start=pd.Timestamp(a, tz="UTC"), feature=feat, label=lab,
                                 spearman=float(m[feat].rank().corr(y.rank())), n=len(m)))
    return pd.DataFrame(rows)


def trade_markout_tables() -> pd.DataFrame:
    out = []
    for sym in SYMBOLS:
        t = pd.read_parquet(FEAT / f"trades_{sym}.parquet").dropna(subset=["maker_markout_30s_bps"])
        w = t["qty"] * t["price"]
        conds = {
            "all": pd.Series("all", index=t.index),
            # imbalance on the side the passive order rests: side=+1 (buyer aggressor) hits asks
            "imb1_vs_maker_side": pd.cut(-t["side"] * t["imb1"], [-1.01, -0.6, -0.2, 0.2, 0.6, 1.01]),
            "rv_10s_bps": pd.cut(t["rv_10s"], [-0.01, 1.5, 3, 5, 7, 10, 1e9]),
            "btc_ret_1s_vs_aggressor": pd.cut(t["side"] * t["btc_ret_1s"], [-1e9, -1, -0.25, 0.25, 1, 1e9]) if sym != "BTCUSDT" else None,
        }
        for cname, c in conds.items():
            if c is None:
                continue
            for lvl, idx in t.groupby(c, observed=True).groups.items():
                ww = w.loc[idx]
                r = dict(symbol=sym, conditioner=cname, bucket=str(lvl), n_trades=len(idx), notional=float(ww.sum()))
                for h in ("1s", "5s", "30s"):
                    r[f"vw_maker_markout_{h}_bps"] = float(np.average(t.loc[idx, f"maker_markout_{h}_bps"], weights=ww))
                r["vw_eff_half_spread_bps"] = float(np.average(t.loc[idx, "eff_half_spread_bps"], weights=ww))
                out.append(r)
    return pd.DataFrame(out)


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    params = dict(order_notional_usd=ORDER_NOTIONAL_USD, max_inv_orders=MAX_INV_ORDERS, latencies_ms=LATENCIES_MS,
                  models=list(MODELS), fees_bps=FEES_BPS, grids={k: [str(x) for x in v] for k, v in GRIDS.items()},
                  markout_s=MARKOUT_S, select_markout_s=SELECT_H, window=[WINDOW_START, WINDOW_END],
                  folds=[[str(pd.Timestamp(a, tz="UTC")), str(pd.Timestamp(b, tz="UTC"))] for a, b in folds()],
                  selection="argmax over theta of sum(notional*(markout_5s - fee)) on all data before the fold")
    (RESULTS / "params.json").write_text(json.dumps(params, indent=2))
    df = run_all()
    df.to_csv(RESULTS / "sim_runs_hourly.csv", index=False)
    full = pd.concat([aggregate(df, fee) for fee in FEES_BPS])
    full.to_csv(RESULTS / "full_period_by_theta.csv", index=False)
    ins = full.sort_values("net_mk5_usd", ascending=False).groupby(["symbol", "strategy", "model", "latency_ms", "fee_bps"]).head(1)
    ins.insert(0, "FLAG", "IN-SAMPLE (theta chosen on the full window; not evidence)")
    ins.to_csv(RESULTS / "insample_best.csv", index=False)
    sel, oos = walk_forward(df)
    sel.to_csv(RESULTS / "walkforward_selection.csv", index=False)
    oos.to_csv(RESULTS / "walkforward_oos_summary.csv", index=False)
    fb = {s: pd.read_parquet(FEAT / f"book_{s}.parquet") for s in SYMBOLS}
    predictive(fb).to_csv(RESULTS / "predictive_walkforward.csv", index=False)
    trade_markout_tables().to_csv(RESULTS / "public_trade_markouts_by_feature.csv", index=False)
    print("done")


if __name__ == "__main__":
    main()
