"""Stage 7: robustness of the simulator conclusions to assumptions that are not grid parameters.

  * clock alignment: shift the book clock by -40/+0/+40 ms around the audited book_lag_ms
  * order size: $100 / $1,000 / $10,000 notional per quote
Scope: BASE and H1 theta=0.2 / 0.6 (fixed a priori, no selection), latency 50 ms, all fill models,
out-of-sample period only (WF_FIRST_TEST_HOUR .. WINDOW_END). Writes results/tables/robustness.csv.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import RESULTS, SYMBOLS, TICK, WF_FIRST_TEST_HOUR, WINDOW_END
from hypotheses import MAX_INV_ORDERS, MODELS, asof_mid, signals
from sim import load_arrays, simulate


def main() -> None:
    a, b = pd.Timestamp(WF_FIRST_TEST_HOUR).value, pd.Timestamp(WINDOW_END).value
    rows = []
    for sym in SYMBOLS:
        book, trades, f, _ = load_arrays(sym)
        mid = f["mid"].to_numpy()
        for shift_ms in (-40, 0, 40):
            tb = book["tb"] + shift_ms * 1_000_000
            for notional in (100.0, 1000.0, 10000.0):
                if shift_ms != 0 and notional != 1000.0:
                    continue
                oq = notional / mid[0]
                for theta in (np.inf, 0.6, 0.2):
                    wb, wa = signals("H1_imbalance", theta, f)
                    for mname, model in MODELS.items():
                        out = simulate(tb, book["bp"], book["bq"], book["ap"], book["aq"], book["seg"],
                                       trades["tt"], trades["tpx"], trades["tqty"], trades["tside"], wb, wa,
                                       50 * 1_000_000, model, oq, MAX_INV_ORDERS * oq, TICK[sym])
                        ft, fs, fp, fq = out[0], out[1], out[2], out[3]
                        m = (ft >= a) & (ft < b)
                        w = (fp * fq)[m]
                        mk5 = (fs * (asof_mid(tb, mid, ft + 5 * 10**9) - fp) / fp * 1e4)[m]
                        rows.append(dict(symbol=sym, clock_shift_ms=shift_ms, order_notional=notional,
                                         strategy="BASE" if not np.isfinite(theta) else f"H1_theta_{theta}",
                                         model=mname, notional=float(w.sum()),
                                         mk5_bps=float((mk5 * w).sum() / w.sum()) if w.sum() > 0 else np.nan))
    df = pd.DataFrame(rows)
    (RESULTS / "tables").mkdir(parents=True, exist_ok=True)
    df.to_csv(RESULTS / "tables" / "robustness.csv", index=False)
    print(df.pivot_table(index=["symbol", "strategy", "model"], columns=["clock_shift_ms", "order_notional"], values="mk5_bps").round(3).to_string())


if __name__ == "__main__":
    main()
