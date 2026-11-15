# Market-making research pipeline (Binance spot, local QuestDB)

Read-only research code. It issues **SELECT statements only** over QuestDB HTTP
(`qdb.py` rejects anything else). It sends no orders and does no trading. It writes only under
`projects/market_making_research/`.

## Setup (one time)

```bash
cd /root/.openclaw/workspace/coordinator/projects/market_making_research
uv venv .venv && uv pip install --python .venv/bin/python pandas numpy pyarrow numba
```
Versions used: Python 3.12 venv, pandas 3.1.1, numpy 2.5.4, pyarrow 26.0.1, numba 0.68.1.

## Run

```bash
cd pipeline
./run_all.sh                                    # writes ../data/ and ../results/
MMR_OUT=../verify_run ./run_all.sh              # clean rerun into another folder
```
End-to-end runtime is about 3 to 4 minutes on 8 cores. Each stage can also run on its own:

| Stage | Script | Output |
|---|---|---|
| tests | `test_sim.py` | 7 synthetic tests of the fill rules |
| 1 extract | `extract.py` | `data/raw/{trade,depth}/<SYM>.parquet`, `data/raw/manifest.csv` (15-min chunks; each chunk's row count is checked against `count()`) |
| 2 audit + clean | `audit_clean.py` | `results/audit/*.csv`, `audit_summary.json`, `data/clean/...` |
| 3 features | `features.py` | `data/features/book_<SYM>.parquet`, `trades_<SYM>.parquet` |
| 4 simulator | `sim.py` | library: `simulate()` (numba) and `load_arrays()` |
| 5 hypotheses | `hypotheses.py` | `results/sim_runs_hourly.csv`, `full_period_by_theta.csv`, `insample_best.csv` (in-sample; flagged), `walkforward_*.csv`, `predictive_walkforward.csv`, `public_trade_markouts_by_feature.csv`, `params.json` |
| 6 robustness | `robustness.py` | `results/tables/robustness.csv` (clock shift ±40 ms, order size $100/$1k/$10k) |
| 7 tables | `summaries.py` | `results/tables/*.csv`, `hash_manifest.txt` (SHA-256 of every result file) |

## Key settings (`config.py`, `hypotheses.py`)

* The study window is frozen at **2026-11-15 13:20:00 to 20:40:00 UTC**, because the collector
  is still appending live data. Change `WINDOW_*` to study another period.
* Timestamps: `spot_trade.timestamp` is the exchange trade time. `spot_depth10.timestamp` is
  the local receive time. The audit estimates the book lag (10 ms) and moves book times onto
  the exchange clock.
* Walk-forward uses hourly test folds from 16:00. Each fold's parameter is chosen on all
  earlier data (expanding window).
* Simulator parameters: `latency_ms` ∈ {20, 50, 200}; fill model ∈ {optimistic, queue_cons,
  queue_prop}; maker fee ∈ {-0.5, 0, 1, 7.5, 10} bps; order size $1,000; inventory cap 5 orders.
  Fees are charged after the simulation, so one simulation serves every fee level.
* `sim.py` documents the fill assumptions in its docstring. Read them before using any number.

## Comparing two runs

```bash
diff <(cut -c1-64,66- ../results/tables/hash_manifest.txt) <(cut -c1-64,66- ../verify_run/results/tables/hash_manifest.txt)
```
