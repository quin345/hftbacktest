# Checks, data quality and open uncertainty

Date: 2026-11-15. Study window: 2026-11-15 13:20:00 to 20:40:00 UTC, frozen. The source tables
are still being appended to by the live collector.

## 1. Verification performed

| Check | Result |
|---|---|
| Read-only access | `qdb.py` allows only single `SELECT` statements and rejects DDL/DML keywords. Every query used it or was a manual SELECT with LIMIT or aggregation. No writes to QuestDB or to the server files. |
| Extract completeness | For every 15-min chunk, the rows returned equal a `count()` query on the same chunk (`data/raw/manifest.csv`). If they differ, the run fails. |
| Simulator unit tests | `test_sim.py`: 7 synthetic cases pass. They cover back-of-queue fill timing, optimistic touch fill, latency blocking an early trade, trade-through fill, book-cross fill, queue capping on level shrink and proportional cancels, and post-only reject. |
| **End-to-end clean rerun** | `MMR_OUT=../verify_run ./run_all.sh` ran from an empty folder and exited 0. All 20 result files (CSV/JSON) are **byte-identical** by SHA-256 (`results/tables/hash_manifest.txt` vs `verify_run/results/tables/hash_manifest.txt`; `diff` is empty). Raw and clean parquet for all 6 (table, symbol) pairs have equal row counts and equal content (`DataFrame.equals`). |
| Late-write stability | The rerun happened about 20 min after the first extract and returned identical rows, so no late out-of-order writes landed inside the frozen window during that time. |
| Citation check | I checked the numbers in `report.md` against `results/tables/*.csv` and `results/*.csv`. Six figures were wrong in the first draft (fill-reason shares, through-touch share, latency range, notional cut, bid-quote share, imb5 note) and are corrected. |
| Robustness | `results/tables/robustness.csv`: book clock shifted by ±40 ms, and order size of $100, $1k and $10k. No conclusion changes. |
| Overstatement guard | The optimistic fill model is reported next to two queue models everywhere. Every positive number in the report comes from optimistic fills and is labelled that way. In-sample tables are flagged in the file itself. |

## 2. Data audit (`results/audit/`)

| | BTCUSDT | ETHUSDT | SOLUSDT |
|---|---|---|---|
| Trades in window | 1,691,750 | 753,949 | 252,109 |
| Depth snapshots in window | 262,711 | 259,099 | 217,670 |
| Duplicate trade_id / duplicate last_update_id | 0 / 0 | 0 / 0 | 0 / 0 |
| Missing trade_ids (one gap each) | 1,053 (0.062%) | 121 (0.016%) | 178 (0.071%) |
| Depth gaps > 1 s | 2 (6.0 s, 1.37 s) | 2 (6.0 s, 1.37 s) | 2 (6.0 s, 1.49 s) |
| Crossed/locked books, non-positive size or price, unsorted levels, top-of-book ≠ JSON level 1 | 0 | 0 | 0 |
| Timestamps non-monotonic; update id decreasing | 0; 0 | 0; 0 | 0; 0 |
| Median / p99 snapshot interval | 100 / 103 ms | 100 / 200 ms | 100 / 400 ms |
| Identical consecutive snapshots | 70,448 | 53,110 | 53,955 |
| 1-tick spread share | 99.86% | 99.80% | 99.997% |
| Off-tick trade prices | 0 | 0 | 0 |

Issues found and how they were handled:

* **Reconnect at 13:20:49 to 13:20:55 UTC.** All three symbols lose about 6 s of depth and
  trades at the same moment (one shared websocket). Missing trade IDs: 1,053 BTC, 121 ETH,
  178 SOL. The simulator treats the gap as a new book segment and cancels resting orders.
  It falls in the training block, not in OOS.
* **Short depth stall at 18:11:28 to 18:11:30** (1.4 to 1.5 s, all symbols). No trade IDs are
  missing there. It stays inside one segment, below the 2 s threshold.
* **Two clocks.** Trades carry the exchange time (ms). Depth carries the local receive time
  (ns) and no exchange timestamp (`collector.py`). A lag scan (`clock_lag_scan.csv`) gives
  the largest at-touch match at 10 ms for all three symbols. Books are shifted by −10 ms. The
  peak is flat over 0 to 25 ms, and ±40 ms robustness shows little effect.
* **Stale books.** `depth10@100ms` is conflated. At the best lag, 57% of BTC trades, 61% of ETH
  trades and 19% of SOL trades print through the touch of the last snapshot. Queue position and
  sub-100 ms dynamics cannot be observed.
* **Outlier snapshots.** BTC's maximum spread is 3,691 ticks ($36.91), in a single snapshot.
  Rows are kept because nothing structural is wrong with them. They affect only a few
  feature values.
* **Usable data.** Everything in the window is usable. The data covers **one day** (7 h 20 min,
  a Sunday). There are no days to split by and no weekday or US-hours coverage.

## 3. Unresolved uncertainty

1. **Queue position is unobservable.** The fill models bracket the truth: optimistic is an
   upper bound, queue_cons a pessimistic case. The conclusions depend on which is closer.
   Every positive pre-fee result needs optimistic fills.
2. **One session, five hourly folds.** "5/5 folds" is weak evidence. Hourly folds are
   autocorrelated, and no standard errors or bootstrap were computed.
3. **Fee levels.** 10 bps and 7.5 bps are my understanding of Binance's public regular and
   BNB-discount maker fees. I did not check them in this session. The other fee levels are
   hypothetical. No account fee tier was available.
4. **Markout reference is a 100 ms conflated mid.** Markouts at 1 s are partly measurement
   noise. 5 s is the selection horizon. 30 s is reported but noisy.
5. **Book-cross fill rule.** It infers fills from snapshots (69% of SOL queue_cons notional).
   That is realistic for a slow maker, but it may overstate fills if a faster cancel would
   have happened inside the 100 ms window.
6. **Own-order impact ignored.** Size effects ($100 to $10k) are small, but our orders never
   change the replayed book or trades.
7. **Clock lag** has 10 to 25 ms resolution. The local clock offset to Binance (NTP) was not
   measured directly.
8. **Live data continues.** Rerunning with a different `WINDOW_*` will change every number.
   The frozen window is fully reproducible as long as nothing is rewritten inside it.

## 4. Scope and approvals

* SELECT-only access to QuestDB. QuestDB data, server files and other project folders were
  not changed. Files were created only under `projects/market_making_research/`.
  `verify_run/` (236 MB) is the clean rerun copy and can be deleted by the owner if space
  matters. I did not delete it.
* No orders, no trading, no external messages, nothing published.

---

# Checks for the factor ladder and the daily cumulative run (2026-11-15, 21:22 to 21:45 UTC)

## 5. Environment

* The project moved to `projects/hftbacktest/market_making_research/`. `.venv/bin/python` still
  works, because it is a symlink to the uv-managed CPython 3.12, and pandas 3.1.1 and numba import
  fine. **The venv was not recreated.** Console scripts in `.venv/bin/` (`numba`, `f2py`) still
  have shebangs pointing to the old path. The pipeline never uses them; it always calls
  `.venv/bin/python <script>`. If the venv ever breaks, rebuild it with the `uv venv` command
  in `pipeline/README.md`.
* The numba cache is recompiled under `pipeline/__pycache__/` (inside the project).

## 6. Verification performed

| Check | Result |
|---|---|
| Unit tests | `test_sim.py` 7/7 pass. New `test_factors.py` 10/10 pass. They cover the fixed ladder order and "off"-first grids, "off" allowing everything, directional filters pulling the threatened side (imb1, flow, BTC lead-lag), regime filters pulling both sides (rv, spread), the per-side depth-change filter, NaN being neutral, AND-combination, depth change values and segment resets, the left-open trade-flow window, BTC lead-lag not applying to BTC, and the 4-of-5 (ceil 0.8n) fold rule. Both suites also run as the first stages of every daily run. |
| New pipeline reproduces the frozen study | `run_daily.sh` with `MMR_WINDOW_END=2026-11-15T20:40:00.000000Z` into `verify_daily/frozen/frozen-rerun/`. All **20 original result files are byte-identical** to `results/` (SHA-256 manifests; the only differences are 9 *added* files: ladder/*, audit/anomalies.csv, coverage.csv, extract_counts.csv, extract_manifest.csv). Extracted rows equal the frozen counts exactly. |
| Daily entrypoint, default invocation | `pipeline/run_daily.sh` at 21:3x exited **3**, because `results/runs/2026-11-15` (the frozen run) already exists. This is the intended never-overwrite behaviour. Nothing was written. |
| Daily entrypoint, end to end | `MMR_RUN_ID=2026-11-15-adhoc-2135 pipeline/run_daily.sh`, invoked 2026-11-15T21:35:06Z. **Window used: 2026-11-15T13:20:00.000000Z to 2026-11-15T21:35:00.000000Z** (invocation floored to the minute, 60 s settle wait). Exit 0 in about 5 min. The run wrote `results/runs/2026-11-15-adhoc-2135/` (29 hashed result files + run.log + run_meta.json), appended an index row and deleted its intermediate parquet (`data/runs/…`). |
| Rerun from an empty folder is identical | `MMR_WINDOW_END=2026-11-15T21:35:00.000000Z MMR_RUN_ID=2026-11-15-adhoc-2135 MMR_RUNS_DIR=$PWD/verify_daily/rerun pipeline/run_daily.sh` → exit 0. `tables/hash_manifest.txt` is **identical** (29 files). Every file in the two run folders is byte-identical except `run.log` and `run_meta.json`, which contain the invocation time and are deliberately not hashed. The index rows are identical except `invoked_at_utc`. |
| Row counts before and after extract | `audit/extract_counts.csv`: for every (table, symbol), count() before = rows extracted = count() after (`consistent=True`), plus per-15-min-chunk count checks (`extract_manifest.csv`). Re-querying the 13:20 to 21:35 window at about 21:45 returned the same counts. |
| Read-only / scope | Only SELECT queries through `qdb.py` (guarded) and manual SELECT count/min/max checks via curl to 127.0.0.1:9000. No changes to QuestDB or the collector. No git commands. Files written only inside `market_making_research/`. `projects/hftbacktest/.gitignore`, `research_baseline/` and `research_btcusdt_questdb/` were not touched. The project's own `.gitignore` gained `verify_daily/`, `results/runs/.lock` and `results/runs/.incomplete-*/`. |
| Citation check | Every ladder and index number in `report.md` was checked against `results/runs/2026-11-15/ladder/*.csv`, `results/runs/2026-11-15-adhoc-2135/ladder/*.csv`, `ladder_folds.csv` and `results/runs/index.csv`. |

## 7. Row counts (rows in window; raw = clean for every series, nothing dropped)

| run_id | Window end | Trades BTC / ETH / SOL | Depth BTC / ETH / SOL |
|---|---|---|---|
| 2026-11-15 (frozen) | 20:40:00 | 1,691,750 / 753,949 / 252,109 | 262,711 / 259,099 / 217,670 |
| 2026-11-15-adhoc-2135 | 21:35:00 | 1,805,134 / 829,789 / 269,222 | 295,494 / 291,026 / 243,513 |

## 8. Anomalies (cumulative run 13:20 to 21:35; `audit/anomalies.csv`, `coverage.csv`)

* Duplicates (trade_id, depth update id), crossed or locked books, non-positive sizes or prices,
  unsorted levels, top-of-book vs JSON mismatches, off-tick prices, non-monotonic timestamps: **0**
  for all symbols.
* Trade-id gaps: BTC 1,053, ETH 121, SOL 178 missing IDs. These are the same 13:20:49 reconnect as
  before; no new gaps after 20:40.
* Depth gaps > 1 s: 2 per symbol (13:20:49 reconnect, 6 s; 18:11:28 stall, 1.4 to 1.5 s). None new.
* Trade silences > 10 s: ETH 2, SOL 7, BTC 0 (max 14.3 s ETH, 13.3 s SOL). Trade IDs are
  contiguous across them, so they are quiet markets, not data loss.
* Window edges: trades end 3 to 4 s before 21:35:00 in all three symbols. I checked trade IDs
  21:34:50 to 21:35:10: they are contiguous (BTC 366 IDs = 366 rows) and the next trades are
  at 21:35:00.005 to 21:35:02.1. This is a genuine lull, not a missing tail. Depth runs to within
  0.3 s of the end.
* BTC outlier snapshot of 3,691 ticks spread (kept, as before).

## 9. Unresolved uncertainty (new)

1. **Ladder decisions rest on one session and 5 to 6 autocorrelated hourly folds.** ETH's rv_10s
   KEEP is marginal (one qualifying queue_cons fold improved by only +0.002 bps).
2. **Forward selection is order-dependent**, and earlier thresholds are not refitted. A factor
   rejected at step k is never retried. A different pre-declared order could give different
   decisions.
3. **The selection objective (per-fill markout, ≥20% of earlier notional)** is a choice I made in
   advance so that "trade less" does not count as improvement. A total-net-$ objective would favour
   the most restrictive filters, and would make nearly every filter look "better" under the
   losing queue models.
4. **The fold rule for cumulative runs** is generalised to ceil(0.8 × folds). The fold count
   grows by about 24 per day, so later runs judge the ladder on many more folds than the 5-fold
   rule in the brief.
5. **Scaling.** Each daily run re-extracts and re-simulates from 13:20 on 11-15, so runtime and
   memory grow linearly with the days of data. That is about 5 min for 8 h now; extrapolated (not
   measured), roughly 15 min for 1 day and several hours after a few weeks. The 4 h per-stage
   timeout will eventually trip. The smallest fix is per-day cached extracts. Not done.
6. **Settle margin.** The run waits 60 s after the window end and fails if counts change during
   the extract. If the collector ever writes rows more than 60 s late, a run could miss rows that
   arrived after the "after" count. That cannot be detected inside the run.
7. **The run folder is named by the UTC date of the window end.** A second run on the same date
   needs `MMR_RUN_ID`, otherwise it exits 3. A failed run leaves `results/runs/.incomplete-<id>-<pid>/`
   and `data/runs/<id>-<pid>/` for inspection; the pipeline does not clean these up.
8. `data/runs/frozen-rerun-48685/` (243 MB, the frozen-window rerun data kept with
   `MMR_KEEP_DATA=1`) and `verify_daily/` (verification outputs) were left in place. The owner can
   delete them; I did not.
9. Fee levels are still unverified (as in §3.3).
