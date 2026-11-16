# Market-making research pipeline (Binance spot, local QuestDB)

Read-only research code. It issues **SELECT statements only** over QuestDB HTTP
(`qdb.py` rejects anything else). It sends no orders and does no trading. It writes only under
`projects/hftbacktest/market_making_research/` (moved from `projects/market_making_research/` on 2026-11-15).

## Setup (one time)

```bash
cd /root/.openclaw/workspace/coordinator/projects/hftbacktest/market_making_research
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
| tests | `test_sim.py`, `test_factors.py` | 7 synthetic tests of the fill rules; 10 tests of the factor-ladder filters |
| 1 extract | `extract.py` | `data/raw/{trade,depth}/<SYM>.parquet`, `data/raw/manifest.csv` (15-min chunks; each chunk's row count is checked against `count()`) |
| 2 audit + clean | `audit_clean.py` | `results/audit/*.csv`, `audit_summary.json`, `data/clean/...` |
| 3 features | `features.py` | `data/features/book_<SYM>.parquet`, `trades_<SYM>.parquet` |
| 4 simulator | `sim.py` | library: `simulate()` (numba) and `load_arrays()` |
| 5 hypotheses | `hypotheses.py` | `results/sim_runs_hourly.csv`, `full_period_by_theta.csv`, `insample_best.csv` (in-sample; flagged), `walkforward_*.csv`, `predictive_walkforward.csv`, `public_trade_markouts_by_feature.csv`, `params.json` |
| 5b factor ladder | `ladder.py` (rules in `factors.py`) | `results/ladder/ladder_{table,folds,decisions,final}.csv`, `ladder_params.json` |
| 6 robustness | `robustness.py` | `results/tables/robustness.csv` (clock shift ±40 ms, order size $100/$1k/$10k) |
| 7 tables | `summaries.py` | `results/tables/*.csv`, `hash_manifest.txt` (SHA-256 of every result file) |

## Key settings (`config.py`, `hypotheses.py`)

* `run_all.sh` uses the frozen window **2026-11-15 13:20:00 to 20:40:00 UTC** (default of
  `WINDOW_END`). `run_daily.sh` sets `MMR_WINDOW_END` to the run time, so its window is cumulative
  from 13:20. The two are **not the same window**; do not compare their numbers as like for like.
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

## Daily run (cumulative window)

Exact command (run from anywhere; no arguments, no environment needed):

```bash
/root/.openclaw/workspace/coordinator/projects/hftbacktest/market_making_research/pipeline/run_daily.sh
```

What it does: waits until 60 s after the window end (invocation time floored to the minute),
then runs tests → extract → audit → features → hypotheses → factor ladder → robustness → tables
for the window **[2026-11-15 13:20:00 UTC, window end)**. It is SELECT-only against QuestDB on
127.0.0.1:9000 (the URL is refused unless it is localhost), makes no other network calls, runs
no git commands and writes only inside this project folder. It takes a lock so two runs cannot
overlap. Exit code 0 means success; any failure exits non-zero (1 pipeline/index failure,
2 setup error, 3 run folder already exists, 4 another run holds the lock).

Expected outputs for a run at 2026-11-16 00:00 UTC:

| Path | Content |
|---|---|
| `results/runs/2026-11-16/` | full results tree of that run (same files as `results/`, plus `ladder/`) |
| `results/runs/2026-11-16/audit/extract_counts.csv` | count() per table and symbol before and after the extract, rows extracted, `consistent` (the run fails if they differ) |
| `results/runs/2026-11-16/audit/anomalies.csv`, `coverage.csv` | data audit findings: duplicates, crossed books, gaps, trade-id gaps, silences at the window edges |
| `results/runs/2026-11-16/ladder/ladder_decisions.csv` | keep/reject per factor and symbol |
| `results/runs/2026-11-16/tables/hash_manifest.txt` | SHA-256 of every result CSV/JSON (for rerun comparison) |
| `results/runs/2026-11-16/run_meta.json`, `run.log` | invocation time, window, stage log (not hashed: they contain the run time) |
| `results/runs/index.csv` | one appended row per run: window end, row counts per table and symbol, anomaly count, headline metrics |

The folder name is the UTC date of the window end. If it already exists the run stops with exit
code 3; nothing is ever overwritten. A failed run leaves only `results/runs/.incomplete-<id>-<pid>/`
for inspection (no dated folder, no index row). Intermediate parquet is written under
`data/runs/<id>-<pid>/` and deleted after a successful run (set `MMR_KEEP_DATA=1` to keep it).
`results/runs/2026-11-15/` is the frozen 13:20 to 20:40 study (copied from `results/`), kept for
comparison; it is **not** a cumulative-window run.

Manual or verification runs can pin the window and the destination, e.g. an exact rerun of a
past run into an empty folder:

```bash
MMR_WINDOW_END=2026-11-16T00:00:00.000000Z MMR_RUN_ID=2026-11-16 MMR_RUNS_DIR=$PWD/../verify_daily/rerun ./run_daily.sh
diff ../results/runs/2026-11-16/tables/hash_manifest.txt ../verify_daily/rerun/2026-11-16/tables/hash_manifest.txt
```

Runtime and memory grow with the cumulative window (every run re-extracts and re-simulates from
13:20). About 5 to 6 min for the first ~8 h of data on 8 cores (measured 2026-11-15), plus the 60 s settle wait; expect it to grow roughly
linearly with the number of days.
