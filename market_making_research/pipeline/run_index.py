"""Run metadata and the dated-run index.

  python run_index.py meta  <run_dir> <invoked_at_utc> <run_id>   writes <run_dir>/run_meta.json
  python run_index.py index <run_dir> <index_csv>         appends one row for <run_dir> to <index_csv>

The index row holds the window, row counts per (table, symbol) (raw extracted and clean),
anomaly count, number of OOS folds, and headline metrics (latency 50 ms, fee 0 per-fill 5 s
markout in bps; subtract the fee in bps for any other fee level). An existing run_id in the
index is never overwritten: the script fails instead.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pandas as pd

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SHORT = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}


def columns() -> list[str]:
    c = ["run_id", "window_start", "window_end", "invoked_at_utc", "comparable_to_cumulative", "n_oos_folds",
         "n_anomalies", "extract_counts_consistent"]
    for s in SYMS:
        c += [f"trade_rows_{SHORT[s]}", f"depth_rows_{SHORT[s]}", f"trade_rows_clean_{SHORT[s]}", f"depth_rows_clean_{SHORT[s]}"]
    for s in SYMS:
        c += [f"{SHORT[s]}_base_mk5_bps_qprop", f"{SHORT[s]}_h1wf_mk5_bps_qprop",
              f"{SHORT[s]}_ladder_kept", f"{SHORT[s]}_ladder_mk5_bps_qcons", f"{SHORT[s]}_ladder_mk5_bps_qprop",
              f"{SHORT[s]}_ladder_net_usd_fee7.5_qprop"]
    return c + ["notes"]


def row_for(run: Path) -> dict:
    res = run
    meta = json.loads((res / "run_meta.json").read_text()) if (res / "run_meta.json").exists() else {}
    params = json.loads((res / "params.json").read_text())
    aud = json.loads((res / "audit" / "audit_summary.json").read_text())
    r = {"run_id": run.name, "window_start": params["window"][0], "window_end": params["window"][1],
         "invoked_at_utc": meta.get("invoked_at_utc", ""), "n_oos_folds": len(params["folds"]),
         "comparable_to_cumulative": meta.get("comparable_to_cumulative", "yes"), "notes": meta.get("notes", "")}
    an = res / "audit" / "anomalies.csv"
    r["n_anomalies"] = len(pd.read_csv(an)) if an.exists() else ""
    ec = res / "audit" / "extract_counts.csv"
    r["extract_counts_consistent"] = bool(pd.read_csv(ec)["consistent"].all()) if ec.exists() else "not recorded"
    for s in SYMS:
        a = aud[s]
        r[f"trade_rows_{SHORT[s]}"] = a["trade_rows_raw"]
        r[f"depth_rows_{SHORT[s]}"] = a["depth_rows_raw"]
        r[f"trade_rows_clean_{SHORT[s]}"] = a["trade_rows_clean"]
        r[f"depth_rows_clean_{SHORT[s]}"] = a["depth_rows_clean"]
    w = pd.read_csv(res / "walkforward_oos_summary.csv")
    w = w[(w["latency_ms"] == 50) & (w["fee_bps"] == 0.0) & (w["model"] == "queue_prop")]
    fin = res / "ladder" / "ladder_final.csv"
    lf = pd.read_csv(fin) if fin.exists() else None
    for s in SYMS:
        h1 = w[(w["symbol"] == s) & (w["strategy"] == "H1_imbalance")]
        r[f"{SHORT[s]}_base_mk5_bps_qprop"] = round(float(h1["base_mk5_bps"].iloc[0]), 4) if len(h1) else ""
        r[f"{SHORT[s]}_h1wf_mk5_bps_qprop"] = round(float(h1["oos_mk5_bps"].iloc[0]), 4) if len(h1) else ""
        if lf is not None:
            x = lf[lf["symbol"] == s].set_index("model")
            r[f"{SHORT[s]}_ladder_kept"] = x.loc["queue_prop", "kept_factors"]
            r[f"{SHORT[s]}_ladder_mk5_bps_qcons"] = round(float(x.loc["queue_cons", "oos_mk5_bps_gross"]), 4)
            r[f"{SHORT[s]}_ladder_mk5_bps_qprop"] = round(float(x.loc["queue_prop", "oos_mk5_bps_gross"]), 4)
            r[f"{SHORT[s]}_ladder_net_usd_fee7.5_qprop"] = round(float(x.loc["queue_prop", "oos_net_usd_fee7.5"]), 2)
    return r


def main() -> None:
    cmd, run = sys.argv[1], Path(sys.argv[2]).resolve()
    if cmd == "meta":
        p = json.loads((run / "params.json").read_text())
        meta = {"run_id": sys.argv[4], "invoked_at_utc": sys.argv[3], "window": p["window"],
                "comparable_to_cumulative": "yes",
                "notes": "cumulative window from 2026-11-15 13:20 UTC to the run time (floored to the minute)"}
        (run / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return
    if cmd == "index":
        idx = Path(sys.argv[3])
        cols = columns()
        if idx.exists():
            with idx.open() as fh:
                rd = csv.DictReader(fh)
                if rd.fieldnames != cols:
                    raise SystemExit(f"index header differs from the expected columns: {idx}")
                if any(x["run_id"] == run.name for x in rd):
                    raise SystemExit(f"run_id {run.name} already in {idx}; refusing to overwrite")
        row = row_for(run)
        new = not idx.exists()
        with idx.open("a", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols)
            if new:
                wr.writeheader()
            wr.writerow({c: row.get(c, "") for c in cols})
        print("index row appended:", run.name, "->", idx)
        return
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
