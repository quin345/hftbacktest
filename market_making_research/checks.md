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
