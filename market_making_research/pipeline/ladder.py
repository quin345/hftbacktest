"""Stage 5b: factor ladder (walk-forward, out-of-sample).

Starts from H1 (queue-imbalance filter) and adds the factors in factors.LADDER ONE AT A TIME,
in that fixed order, per symbol. Same fold design (hourly folds from WF_FIRST_TEST_HOUR,
expanding training window), same simulator, same fill models and same post-hoc fee charge as
hypotheses.py. Reference latency 50 ms, $1,000 orders, inventory cap 5 orders.

Per (symbol, fill model, fold), thresholds are fitted IN-SAMPLE on the fold's training window
only (all hours before the fold):
  * the thresholds of already-kept factors are those chosen for that same fold at the previous
    accepted step (coordinate-wise forward selection; earlier thresholds are not refitted);
  * the new factor's theta is the grid value (including "off") that maximises the training
    notional-weighted 5 s markout per fill (bps), subject to keeping at least MIN_ACTIVITY of
    the previous step's training notional. Ties go to the earlier, less restrictive grid value.
    Per-fill markout is used (not total net $) because when every configuration loses, total
    net $ is maximised simply by trading less.
Decision (per symbol): KEEP the factor only if, under BOTH queue fill models (queue_cons and
queue_prop), the candidate's OOS per-fill 5 s markout net of fees is strictly better than the
previous accepted step's in at least ceil(0.8 * n_folds) folds (= 4 of 5 for the frozen
window). The fee is a constant per filled dollar, so per-fill improvement is the same at every
fee level; net-of-fee levels are reported for every fee scenario. A fold with zero candidate
fills does not count as an improvement. Rejected factors are recorded and the state is unchanged.

Outputs (results/ladder/): ladder_table.csv, ladder_final.csv (final accepted model, stitched OOS), ladder_folds.csv, ladder_decisions.csv, ladder_params.json.
"""
from __future__ import annotations

import json
import math
from multiprocessing import get_context

import numpy as np
import pandas as pd

from config import RESULTS, SYMBOLS, TICK, WINDOW_END, WINDOW_START
from factors import COLUMNS, GRIDS, LADDER, applicable, combined_sides
from hypotheses import FEES_BPS, MAX_INV_ORDERS, MODELS, ORDER_NOTIONAL_USD, folds, hourly_metrics, ts
from sim import load_arrays, simulate

LATENCY_MS = 50
MIN_ACTIVITY = 0.2
QUEUE_MODELS = ("queue_cons", "queue_prop")
SUMS = ["n_fills", "notional", "mk1_sum", "mk5_sum", "mk30_sum"]
OUT = RESULTS / "ladder"


def hour_buckets():
    a, end = ts(WINDOW_START), ts(WINDOW_END)
    edges = [a] + [int(x.value) for x in pd.date_range(pd.Timestamp(WINDOW_START).ceil("h"), pd.Timestamp(WINDOW_END),
                                                        freq="h", inclusive="left")] + [end]
    edges = sorted(set(e for e in edges if a <= e <= end))
    return list(zip(edges[:-1], edges[1:]))


def need_folds(n: int) -> int:
    return (4 * n + 4) // 5  # ceil(0.8 n) in integer arithmetic


def bps(s):
    return s["mk5_sum"] / s["notional"] if s["notional"] > 0 else np.nan


class Runner:
    """Simulates (thetas, model) once over the whole window and caches the hourly sums."""

    def __init__(self, sym: str):
        self.sym = sym
        self.book, self.trades, f, _ = load_arrays(sym)
        self.f = f
        self.mid = f["mid"].to_numpy()
        self.oq = ORDER_NOTIONAL_USD / self.mid[0]
        self.hours = hour_buckets()
        self.cache: dict = {}

    def hourly(self, thetas: tuple, model: str) -> pd.DataFrame:
        key = (thetas, model)
        if key not in self.cache:
            wb, wa = combined_sides(dict(thetas), self.f)
            b, t = self.book, self.trades
            out = simulate(b["tb"], b["bp"], b["bq"], b["ap"], b["aq"], b["seg"], t["tt"], t["tpx"], t["tqty"], t["tside"],
                           wb, wa, LATENCY_MS * 1_000_000, MODELS[model], self.oq, MAX_INV_ORDERS * self.oq, TICK[self.sym])
            rows = hourly_metrics(out[:5] + (out[7],), b["tb"], self.mid, self.hours)
            self.cache[key] = pd.DataFrame(rows)[["t0"] + SUMS].assign(quote_bid=float(wb.mean()), quote_ask=float(wa.mean()))
        return self.cache[key]

    def sums(self, thetas: tuple, model: str, a: int, b: int | None) -> dict:
        h = self.hourly(thetas, model)
        m = (h["t0"] < a) if b is None else ((h["t0"] >= a) & (h["t0"] < b))
        return h.loc[m, SUMS].sum().to_dict()


def fmt(thetas: tuple) -> str:
    return ";".join(f"{k}={v}" for k, v in thetas) or "BASE"


def run_symbol(sym: str):
    r = Runner(sym)
    fl = folds()
    n_f = len(fl)
    need = need_folds(n_f)
    state = {m: [tuple() for _ in fl] for m in MODELS}  # accepted thetas per model and fold
    kept: list[str] = []
    table, fold_rows, decisions = [], [], []
    for step, fac in enumerate(LADDER):
        if not applicable(fac, sym):
            decisions.append(dict(symbol=sym, step=step, factor=fac, decision="not applicable",
                                  reason="BTC lead-lag is defined for ETHUSDT/SOLUSDT only", kept_set=",".join(kept)))
            continue
        cols = [c for c in COLUMNS[fac] if c in r.f.columns]
        if len(cols) < len(COLUMNS[fac]) or not np.isfinite(r.f[cols].to_numpy(float)).any():
            decisions.append(dict(symbol=sym, step=step, factor=fac, decision="untestable",
                                  reason=f"feature column(s) {COLUMNS[fac]} missing or all NaN", kept_set=",".join(kept)))
            continue
        new_state = {}
        improved = {}
        for model in MODELS:
            new_state[model] = []
            improved[model] = 0
            for k, (a, b) in enumerate(fl):
                prev = state[model][k]
                ptr = r.sums(prev, model, a, None)
                best, best_obj, best_tr = prev + ((fac, np.inf),), -np.inf, None
                for th in GRIDS[fac]:
                    cand = prev + ((fac, th),)
                    tr = r.sums(cand, model, a, None)
                    if tr["notional"] <= 0 or tr["notional"] < MIN_ACTIVITY * ptr["notional"]:
                        continue
                    obj = bps(tr)
                    if obj > best_obj + 1e-12:
                        best, best_obj, best_tr = cand, obj, tr
                new_state[model].append(best)
                te_c, te_p = r.sums(best, model, a, b), r.sums(prev, model, a, b)
                c_bps, p_bps = bps(te_c), bps(te_p)
                imp = bool(te_c["notional"] > 0 and np.isfinite(p_bps) and c_bps > p_bps + 1e-12)
                improved[model] += imp
                fold_rows.append(dict(symbol=sym, step=step, factor=fac, model=model, fold_start=pd.Timestamp(a, tz="UTC"),
                                      theta=dict(best)[fac], thetas=fmt(best), train_mk5_bps=best_obj,
                                      oos_fills=int(te_c["n_fills"]), oos_mk5_bps=c_bps,
                                      prev_thetas=fmt(prev), prev_oos_fills=int(te_p["n_fills"]), prev_oos_mk5_bps=p_bps,
                                      delta_bps=c_bps - p_bps, improved=imp))
        if step == 0:
            decision, reason = "base (kept by design)", "H1 is the ladder base; fold counts are vs BASE (always quote)"
        else:
            ok = all(improved[m] >= need for m in QUEUE_MODELS)
            decision = "KEEP" if ok else "REJECT"
            reason = (f"queue_cons {improved['queue_cons']}/{n_f}, queue_prop {improved['queue_prop']}/{n_f} folds improved; "
                      f"need >= {need} under both")
        old_state = state
        if step == 0 or decision == "KEEP":
            state = new_state
            kept.append(fac)
        decisions.append(dict(symbol=sym, step=step, factor=fac, decision=decision, reason=reason,
                              folds_improved_queue_cons=improved["queue_cons"], folds_improved_queue_prop=improved["queue_prop"],
                              folds_improved_optimistic=improved["optimistic"], n_folds=n_f, need=need, kept_set=",".join(kept)))
        # stitched OOS rows (candidate as fitted, previous accepted step, BASE)
        for model in MODELS:
            for which in ("candidate", "previous", "BASE"):
                tot = dict.fromkeys(SUMS, 0.0)
                for k, (a, b) in enumerate(fl):
                    th = {"candidate": new_state[model][k], "previous": old_state[model][k], "BASE": tuple()}[which]
                    s = r.sums(th, model, a, b)
                    for c in SUMS:
                        tot[c] += s[c]
                row = dict(symbol=sym, step=step, factor_added=fac, decision=decision, model=model, latency_ms=LATENCY_MS,
                           series=which, oos_fills=int(tot["n_fills"]), oos_notional=tot["notional"],
                           oos_mk1_bps=tot["mk1_sum"] / tot["notional"] if tot["notional"] > 0 else np.nan,
                           oos_mk5_bps_gross=bps(tot),
                           oos_mk30_bps=tot["mk30_sum"] / tot["notional"] if tot["notional"] > 0 else np.nan)
                for fee in FEES_BPS:
                    row[f"oos_mk5_bps_net_fee{fee:g}"] = row["oos_mk5_bps_gross"] - fee
                    row[f"oos_net_usd_fee{fee:g}"] = tot["mk5_sum"] / 1e4 - tot["notional"] * fee / 1e4
                row["folds_improved"] = improved[model] if which == "candidate" else np.nan
                row["n_folds"] = n_f
                src = {"candidate": new_state, "previous": old_state}.get(which)
                row["thetas_by_fold"] = "|".join(fmt(x) for x in src[model]) if src else "BASE"
                table.append(row)
    final = []
    for model in MODELS:
        tot = dict.fromkeys(SUMS, 0.0)
        for k, (a, b) in enumerate(fl):
            s = r.sums(state[model][k], model, a, b)
            for c in SUMS:
                tot[c] += s[c]
        row = dict(symbol=sym, model=model, latency_ms=LATENCY_MS, kept_factors=",".join(kept), oos_fills=int(tot["n_fills"]),
                   oos_notional=tot["notional"], oos_mk5_bps_gross=bps(tot), n_folds=n_f,
                   thetas_by_fold="|".join(fmt(x) for x in state[model]))
        for fee in FEES_BPS:
            row[f"oos_mk5_bps_net_fee{fee:g}"] = row["oos_mk5_bps_gross"] - fee
            row[f"oos_net_usd_fee{fee:g}"] = tot["mk5_sum"] / 1e4 - tot["notional"] * fee / 1e4
        final.append(row)
    print(sym, "ladder done;", len(r.cache), "simulations; kept:", kept, flush=True)
    return table, fold_rows, decisions, final


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    params = dict(ladder=LADDER, grids={k: [str(x) for x in v] for k, v in GRIDS.items()}, latency_ms=LATENCY_MS,
                  models=list(MODELS), queue_models_for_decision=list(QUEUE_MODELS), fees_bps=FEES_BPS,
                  order_notional_usd=ORDER_NOTIONAL_USD, max_inv_orders=MAX_INV_ORDERS, min_activity=MIN_ACTIVITY,
                  window=[WINDOW_START, WINDOW_END],
                  folds=[[str(pd.Timestamp(a, tz="UTC")), str(pd.Timestamp(b, tz="UTC"))] for a, b in folds()],
                  need_folds=need_folds(len(folds())),
                  selection="per fold, in-sample on all hours before the fold: argmax training 5 s markout per fill "
                            "(bps) over the new factor's grid, earlier factors fixed at their fold-specific thresholds, "
                            f"training notional >= {MIN_ACTIVITY} x previous step's",
                  decision="KEEP iff OOS per-fill 5 s markout net of fee beats the previous accepted step in >= need_folds "
                           "folds under both queue_cons and queue_prop")
    (OUT / "ladder_params.json").write_text(json.dumps(params, indent=2))
    with get_context("fork").Pool(len(SYMBOLS)) as pool:
        res = pool.map(run_symbol, SYMBOLS)
    pd.DataFrame([x for r in res for x in r[0]]).to_csv(OUT / "ladder_table.csv", index=False)
    pd.DataFrame([x for r in res for x in r[1]]).to_csv(OUT / "ladder_folds.csv", index=False)
    pd.DataFrame([x for r in res for x in r[3]]).to_csv(OUT / "ladder_final.csv", index=False)
    dec = pd.DataFrame([x for r in res for x in r[2]])
    dec.to_csv(OUT / "ladder_decisions.csv", index=False)
    print(dec[["symbol", "step", "factor", "decision", "reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
