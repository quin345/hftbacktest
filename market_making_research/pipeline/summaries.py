"""Stage 6: report tables from the simulation outputs (no new fitting).

results/tables/
  oos_period_by_theta.csv   every a-priori theta on the out-of-sample period only (16:00-20:40),
                            with hourly consistency vs BASE. No theta is selected here.
  latency_fill_sensitivity.csv  BASE and walk-forward-selected strategy per model x latency (fee 0).
  breakeven_fee.csv         maker fee (bps) at which the 5 s markout P&L is zero = mk5_bps.
                            Negative values mean a rebate of that size would be required.
  fill_reason_mix.csv       share of filled notional by fill reason (queue / through / book-cross / touch).
  headline.csv              compact per-hypothesis summary used in report.md.
  hash_manifest.txt         SHA-256 of every results CSV (for rerun comparison).
"""
from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from config import RESULTS, WF_FIRST_TEST_HOUR

TAB = RESULTS / "tables"
REASONS = ("queue", "through", "bookcross", "touch")


def main() -> None:
    TAB.mkdir(parents=True, exist_ok=True)
    h = pd.read_csv(RESULTS / "sim_runs_hourly.csv")
    h["theta"] = h["theta"].astype(float)
    t0 = pd.Timestamp(WF_FIRST_TEST_HOUR).value
    o = h[h["t0"] >= t0].copy()
    keys = ["symbol", "strategy", "model", "latency_ms", "theta"]
    hr = o.copy()
    hr["mk5_bps"] = hr["mk5_sum"] / hr["notional"].where(hr["notional"] > 0)
    base = hr[~np.isfinite(hr["theta"])][["symbol", "strategy", "model", "latency_ms", "t0", "mk5_bps"]].rename(columns={"mk5_bps": "base_mk5_bps"})
    hr = hr.merge(base, on=["symbol", "strategy", "model", "latency_ms", "t0"])
    hr["beat"] = hr["mk5_bps"] > hr["base_mk5_bps"]
    g = o.groupby(keys)
    a = g[["n_fills", "notional", "mtm_pnl_gross", "edge_sum", "mk1_sum", "mk5_sum", "mk30_sum"] +
          [f"notional_{r}" for r in REASONS]].sum()
    for c in ("edge", "mk1", "mk5", "mk30"):
        a[f"{c}_bps"] = a[f"{c}_sum"] / a["notional"].where(a["notional"] > 0)
    a["hours_mk5_bps_beat_base"] = hr.groupby(keys)["beat"].sum()
    a["hours"] = hr.groupby(keys).size()
    a = a.reset_index()
    a["quote_up_bid"] = g["quote_up_bid"].first().to_numpy()
    a["quote_up_ask"] = g["quote_up_ask"].first().to_numpy()
    a.to_csv(TAB / "oos_period_by_theta.csv", index=False)

    mix = a[["symbol", "strategy", "model", "latency_ms", "theta", "notional"] + [f"notional_{r}" for r in REASONS]].copy()
    for r in REASONS:
        mix[f"share_{r}"] = mix[f"notional_{r}"] / mix["notional"].where(mix["notional"] > 0)
    mix[~np.isfinite(mix["theta"]) & (mix["strategy"] == "H1_imbalance")].drop(columns=[f"notional_{r}" for r in REASONS]).to_csv(TAB / "fill_reason_mix.csv", index=False)

    w = pd.read_csv(RESULTS / "walkforward_oos_summary.csv")
    w0 = w[w["fee_bps"] == 0.0]
    lat = w0[["symbol", "strategy", "model", "latency_ms", "oos_notional", "oos_mk5_bps", "base_notional", "base_mk5_bps",
              "oos_mtm_pnl_net", "base_mtm_pnl_net", "folds_mk5_bps_beat_base", "folds", "thetas_chosen"]]
    lat.to_csv(TAB / "latency_fill_sensitivity.csv", index=False)

    be = a[["symbol", "strategy", "model", "latency_ms", "theta", "notional", "mk5_bps", "mk30_bps"]].copy()
    be["breakeven_maker_fee_bps_5s"] = be["mk5_bps"]
    be["breakeven_maker_fee_bps_30s"] = be["mk30_bps"]
    be.to_csv(TAB / "breakeven_fee.csv", index=False)

    # headline: latency 50 ms, per model, fee 0 and fee 7.5; walk-forward selected vs BASE
    hd = w[(w["latency_ms"] == 50) & (w["fee_bps"].isin([0.0, 7.5]))][
        ["symbol", "strategy", "model", "fee_bps", "oos_notional", "oos_mk5_bps", "base_mk5_bps", "oos_net_mk5_usd",
         "base_net_mk5_usd", "oos_mtm_pnl_net", "base_mtm_pnl_net", "folds_net_mk5_pos", "folds_mk5_bps_beat_base", "thetas_chosen"]]
    hd.to_csv(TAB / "headline.csv", index=False)

    lines = []
    for p in sorted(RESULTS.rglob("*.csv")) + sorted(RESULTS.rglob("*.json")):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(RESULTS)}")
    (TAB / "hash_manifest.txt").write_text("\n".join(lines) + "\n")
    print("tables written:", len(lines), "files hashed")


if __name__ == "__main__":
    main()
