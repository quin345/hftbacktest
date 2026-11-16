# Market-making hypotheses on Binance spot (BTCUSDT, ETHUSDT, SOLUSDT): research report

Prepared 2026-11-15. This is research only: no orders, no trading, and nothing published.
Data: the local QuestDB tables `spot_trade` and `spot_depth10`, read with SELECT only.
Study window: **2026-11-15 13:20:00 to 20:40:00 UTC**. That is 7 h 20 min, one session on a
Sunday. Out-of-sample (OOS) period: 16:00 to 20:40, in five hourly walk-forward folds (the
last fold is 40 min).

## Bottom line

**No strategy here is profitable after fees on out-of-sample data with a realistic fill
model.** In the queue-based fill models, every strategy and every symbol loses money before
fees. The 5 s markout per filled dollar is about -0.9 to -1.6 bps. In the OOS period, the
break-even maker fee would be a rebate of roughly 0.9 to 1.6 bps (`results/tables/breakeven_fee.csv`).
Positive pre-fee markouts appear only in the **optimistic** fill model, which ignores the queue.
That model is known to overstate profit. Even there, the positive results disappear at a maker
fee of 1 bp. Two of the three hypotheses have real predictive content, but this data and
simulator do not show that the content becomes market-making profit.

*Update (2026-11-15, later the same evening):* a factor ladder was added on top of H1 (see
"Factor ladder" below). Only the volatility pause (`rv_10s`) passes the keep rule, and only on
BTC and ETH. The conclusion above stays the same: nothing is profitable after fees under the queue
fill models. The pipeline now also runs daily on a cumulative window (see "Dated runs and run
index").

Ranking, with one-line verdicts:

1. **H1, queue-imbalance quote filter.** The signal is strong and stable OOS: Spearman 0.27 to
   0.49 with the 1 s forward mid move, in every fold and every symbol. It makes fills better
   only under optimistic fills (BTC, SOL: positive before fees). Under queue models it makes
   fills *worse*. **Verdict: the predictive part is supported; profitability is not supported
   (fails after fees and under queue fills).**
2. **H3, volatility-regime pause.** The signal is weak to moderate: rv_10s predicts |move| with
   Spearman 0.07 to 0.27. Pausing in high volatility improved per-fill markout in BTC in 5/5
   folds under both queue models, by about +0.13 bps. Results for ETH and SOL are mixed. Net
   results are still negative. **Verdict: a small, consistent loss reduction on BTC only. Not
   a profitable strategy.**
3. **H2, BTC lead-lag quote filter (ETH, SOL).** BTC's 1 s return predicts ETH's next 1 s
   (Spearman about 0.21) but SOL's only weakly (0.06 to 0.11). The filter improves optimistic
   markouts a little and leaves queue-model results unchanged (within ±0.04 bps, 2 to 3 of 5
   folds). **Verdict: the signal is real for ETH but too small and too slow at 100 ms book
   resolution to matter. Not supported as an edge.**

## Facts vs assumptions

**Facts (from the data, see `checks.md`):**
* All three symbols have a 1-tick spread 99.8 to 99.99% of the time. One tick is about
  0.0015 bps of price for BTC, 0.055 bps for ETH and 1.40 bps for SOL. Most of a maker's gross
  edge per fill therefore cannot come from the spread on BTC or ETH.
* Public trades are adversely selected against the passive side. Volume-weighted maker
  markouts are -0.76 / -0.89 / -1.02 bps (BTC, 1/5/30 s), -0.77 / -0.90 / -0.77 bps (ETH) and
  -1.33 / -1.14 / -0.77 bps (SOL), measured against 100 ms-conflated book mids
  (`results/public_trade_markouts_by_feature.csv`).
* The depth stream is `depth10@100ms`: top-10 snapshots, conflated to 100 ms, with no exchange
  timestamp. At the best-fit book lag (10 ms), 57% of BTC trades, 61% of ETH trades and 19% of
  SOL trades print *through* the last snapshot's touch (`results/audit/clock_lag_scan.csv`). Decisions
  based on this feed are stale compared with the trade flow.

**Assumptions (needed by the simulator; none can be verified with this data):**
* Queue position: we join the back of the visible queue. Trades at our price move us forward.
  Cancellations either never come from ahead of us (`queue_cons`) or come from ahead in
  proportion to our position (`queue_prop`). `optimistic` ignores the queue.
* A snapshot whose opposite best price has moved to or through our resting price counts as a
  fill (a trade-through inferred from the book).
* Latency (snapshot to order live at the exchange) is 20, 50 or 200 ms. Book lag is 10 ms, as
  estimated in the audit.
* Orders are $1,000 post-only, joined at the touch. Inventory cap is ±5 orders. Our orders
  have no market impact. Fills are partial when the excess traded volume is below our size.
* Maker fee scenarios: -0.5, 0, 1, 7.5, 10 bps. 10 bps and 7.5 bps are Binance's regular spot
  maker fee and the BNB-discounted fee **as I understand the public schedule; I did not check
  them in this session**. -0.5, 0 and 1 bps are hypothetical VIP or market-maker levels.
* The primary metric is the notional-weighted 5 s markout per fill. MTM P&L is also reported,
  but it is dominated by inventory drift over a few hours.

## Common test design (all hypotheses)

* Strategy: a post-only order of $1,000 on each side, joined at the best bid and ask of each
  book snapshot. A decision at snapshot time *t* takes effect at *t* + latency. At most one
  message can be in flight per side. A filter can only *withdraw* a side.
* Control: `BASE` always quotes both sides.
* Parameter grids were fixed in advance (`results/params.json`). Walk-forward: for each hourly
  fold, θ maximises Σ notional × (5 s markout − fee) over **all earlier data**, separately for
  each (fill model, latency, fee). That θ is then scored on the fold. Stitched OOS results are in
  `results/walkforward_oos_summary.csv`, with folds in `walkforward_oos_by_fold.csv`.
* The selection objective is net P&L. When every θ loses, it chooses the θ that trades least.
  So a "better than BASE" net P&L can come from **trading less** rather than from better
  fills. I therefore also report per-fill markout (bps) and the number of folds where per-fill
  markout beat BASE (`folds_mk5_bps_beat_base`).
* `results/insample_best.csv` is labelled **IN-SAMPLE** and is not used as evidence.
* Every θ in the grid is also shown on the OOS period without any selection
  (`results/tables/oos_period_by_theta.csv`).

Reference results, OOS period, latency 50 ms, 5 s markout per filled dollar (bps, before fees):

| Symbol | Model | BASE | H1 (WF θ) | H2 (WF θ) | H3 (WF θ) |
|---|---|---|---|---|---|
| BTC | optimistic | -0.30 | **+0.12** (0.2) | n/a | -0.23 (1.5) |
| BTC | queue_prop | -1.04 | -1.28 (0.2) | n/a | -0.91 (1.5) |
| BTC | queue_cons | -1.09 | -1.32 | n/a | -0.95 |
| ETH | optimistic | -0.58 | -0.06 (0.2) | -0.46 (0.25) | -0.51 (1.5) |
| ETH | queue_prop | -1.18 | -1.39 | -1.15 | -1.16 |
| ETH | queue_cons | -1.21 | -1.42 | -1.19 | -1.20 |
| SOL | optimistic | -0.08 | **+0.22** (0.6) | -0.01 (0.25) | +0.07 (2.0) |
| SOL | queue_prop | -1.26 | -1.56 (0.2) | -1.30 (0.25) | -1.41 (1.5) |
| SOL | queue_cons | -1.32 | -1.57 | -1.32 | -1.47 |

*Sources: `results/tables/latency_fill_sensitivity.csv` and `results/tables/oos_period_by_theta.csv`.
The θ shown was chosen at fee 0. Every fold chose the same θ except where noted in `thetas_chosen`.*

Fee-inclusive net (5 s markout P&L minus fees, OOS, 50 ms). Only optimistic fills are ever
positive, and only at fees ≤ 0:
BTC H1 optimistic: +$111 at fee 0, -$855 at 1 bp, -$7,133 at 7.5 bps, on $9.7M notional.
SOL H1 optimistic: +$173 at fee 0, -$426 at 1 bp (θ re-selected = 0.2), -$4,245 at 7.5 bps.
Every queue-model configuration is negative at every fee, including the -0.5 bp rebate
(`results/tables/headline.csv`).

---

## H1: queue-imbalance quote filter

* **Hypothesis.** The top-of-book queue imbalance `imb1 = (bid_q1 − ask_q1)/(bid_q1 + ask_q1)`
  predicts the next mid move. A maker who withdraws the side facing the thin queue avoids the
  fills that are most adversely selected.
* **Mechanism.** On large-tick books, a thin queue on one side is likely to be depleted, which
  moves the mid toward that side. Resting orders on the thin side are filled just before the
  move.
* **Features.** `imb1` (also checked: imb5, imb10, microprice deviation. For BTC and ETH they
  are nearly identical to imb1. For SOL, imb5 and imb10 are weaker: 0.15 and 0.11 vs 0.26).
   Rule: pull the bid when imb1 < −θ, pull the ask when imb1 > θ.
  θ ∈ {off, 0.9, 0.8, 0.6, 0.4, 0.2}.
* **Predictive evidence (OOS, model-free).** Spearman(imb1, 1 s forward mid return) by period
  (training block, then five OOS folds): BTC 0.40 to 0.49, ETH 0.44 to 0.47, SOL 0.27 to 0.29.
  It is positive and similar in size in every fold (`results/predictive_walkforward.csv`).
  **This is the strongest and most stable finding in the study.**
* **Simulated market making (walk-forward).** Optimistic fills: per-fill 5 s markout rises from
  BASE to the H1 filter by +0.41 bps (BTC), +0.51 (ETH) and +0.30 (SOL), in 5/5 folds for each
  symbol. Results are positive before fees for BTC (+0.12) and SOL (+0.22). Queue models: H1
  makes per-fill markout **worse** by 0.2 to 0.3 bps (0 to 1 of 5 folds beat BASE). It also cuts
  filled notional by 77 to 89%.
* **Sensitivity.** Latency 20 to 200 ms moves optimistic H1 markouts by up to 0.15 bps (ETH
  −0.02 → −0.17; BTC +0.12 → +0.06; SOL +0.25 → +0.18) and queue results by less than 0.1 bps. Clock shifts of ±40 ms change markout by ≤0.1 bps. Order size
  from $100 to $10k changes markout by ≤0.05 bps (`results/tables/robustness.csv`). Fees: a
  maker fee of 1 bp removes the optimistic profit. 7.5 or 10 bps is far beyond any edge here.
* **Why the queue models disagree (inference).** At 50 ms under `queue_prop`/`queue_cons`, trade-throughs
  or book-cross fills make up 62 to 72% of BTC's filled notional, 72 to 82% of ETH's and
  78 to 91% of SOL's. In those cases the level is consumed and the price moves through us
  (`results/tables/fill_reason_mix.csv`). Withdrawing the thin side mostly removes the *benign*
  fills, the ones where we were at the front of a shrinking queue. The toxic fills, where the
  heavy side we stay on gets swept, remain. The optimistic model counts every touch as a fill,
  which credits the strategy with fills it would not get at the back of a queue.
* **Failure modes.** Back-of-queue position. A 100 ms conflated, stale feed (the signal is
  already in the price before our order arrives). Strategies quote one side most of the time
  (bid quoted 55 to 72% of the time at θ = 0.2 to 0.6), so this becomes a directional strategy
  with inventory risk.
* **What would falsify it.** For the predictive claim: the imb1 vs forward-return correlation
  falling to ≈0 on other days or regimes. For the MM claim, which is already not supported:
  a queue-aware replay with real queue position (L3 or diff-depth data) showing that H1 fills
  still do not beat BASE.

## H3: volatility-regime pause

* **Hypothesis.** Adverse selection is concentrated in high short-horizon volatility.
  Pausing all quotes when trailing 10 s realised volatility exceeds θ removes the worst fills
  for a smaller loss of spread.
* **Mechanism.** Fast markets carry more informed flow, and stale quotes are picked off more
  often.
* **Features.** `rv_10s` (realised volatility of snapshot mid log-returns over the trailing
  10 s, bps). θ ∈ {off, 10, 7, 5, 4, 3, 2, 1.5} bps (an absolute grid fixed in advance).
* **Predictive evidence.** Spearman(rv_10s, |1 s forward return|) ranges from 0.07 to 0.27 and
  varies by fold. Public-trade maker markouts do **not** grow monotonically with rv_10s in any
  symbol (`public_trade_markouts_by_feature.csv`). That is weak support for the mechanism.
* **Simulated MM (walk-forward; θ = 1.5 bps was chosen in almost every fold, i.e. quote only in
  calm markets).** BTC: per-fill 5 s markout −1.04 → −0.91 bps (queue_prop) and −1.09 → −0.95
  (queue_cons), better than BASE in 5/5 folds. This is the **only** improvement under the queue
  models that is consistent across all folds in the study. ETH: about 0.02 bps better, 2/5
  folds. SOL: worse under the queue models (−1.26 → −1.41). It is +0.07 under optimistic fills,
  but only about $3.5M of notional was filled.
* **Sensitivity.** Latency effects are under 0.03 bps. The improvement shows up only in
  per-fill markout. Net P&L is still negative at every fee level.
* **Failure modes.** Single day. The chosen θ sits at the edge of the grid (1.5 bps, the
  smallest value), so the optimum may lie below the grid, i.e. "quote even less". Pausing also
  makes the remaining fills cluster in calm periods, which may be a different regime on other
  days.
* **What would falsify it.** The BTC improvement disappearing on other days, or failing to
  scale when θ is chosen on more history. A sign flip in queue-aware replays.

## H2: BTC lead-lag quote filter (ETHUSDT, SOLUSDT)

* **Hypothesis.** BTC mid moves lead ETH and SOL by up to a second. Withdrawing the ETH/SOL
  side that a fresh BTC move would pick off reduces adverse selection.
* **Mechanism.** Price discovery happens in BTC first, and slower market makers in the alts
  are picked off by cross-asset arbitrageurs.
* **Features.** `btc_ret_1s` (BTC mid return over the trailing 1 s, from the same websocket
  connection, so the clocks are comparable). Pull the bid when btc_ret_1s < −θ, pull the ask
  when > θ. θ ∈ {off, 3, 2, 1, 0.5, 0.25} bps.
* **Predictive evidence.** Spearman(btc_ret_1s, alt 1 s forward return): ETH 0.20 to 0.23
  (stable), SOL 0.06 to 0.11.
* **Simulated MM (walk-forward; θ = 0.25 in all folds).** Optimistic: ETH −0.58 → −0.46, SOL
  −0.08 → −0.01 bps, better than BASE in 5/5 folds but never positive. Queue models: ETH
  −1.18 → −1.15 (3/5 folds), SOL −1.26 → −1.30 (2/5). That is no meaningful effect.
* **Failure modes.** The 100 ms conflation of both books plus order latency means the lead
  is mostly gone by the time an order would move. The ETH signal overlaps with ETH's own imb1
  (not orthogonalised here).
* **What would falsify it.** It is already not supported as an edge. The predictive claim
  would be falsified if the cross-correlation disappeared on other days. A test with
  millisecond data (bookTicker or diff-depth) is needed before the MM version is dismissed
  entirely.

---

## Results that rest only on in-sample fitting

* `results/insample_best.csv` and `results/full_period_by_theta.csv` (best θ chosen on the full
  window). These are not used for any conclusion.
* Thresholds that the walk-forward almost always picked at the grid boundary (H1 θ = 0.2,
  H2 θ = 0.25, H3 θ = 1.5). The grid was fixed in advance, but the true optimum may lie
  outside it. A finer grid tuned on this same day would be an in-sample fit.
* Clock-lag estimate (10 ms) and the 2 s gap threshold come from the full window. They are
  descriptive constants, and the ±40 ms robustness check shows the results do not depend on them.

## What this does not show

* Profitability on any day other than this one. The data covers one 7 h 20 min Sunday session.
* Anything about strategies that rely on queue priority (resting early and staying at the
  front). The data has no order-level information, so we cannot see our own queue position.
* Effects of faster market data. The `depth10@100ms` feed is a structural handicap for any
  maker simulated on it.

## Smallest next steps

1. Collect `@depth@100ms` diffs or `@bookTicker` (event-time stamped) for several days, then
   rerun `run_all.sh` with a wider `WINDOW_*`.
2. Rerun H1 and H3 on more days. With five hourly folds, "5/5 folds" is weak evidence.
3. If real fee tiers become relevant, confirm the actual account maker fee. The study shows
   that anything above about 0 bps removes every optimistic-model edge found here.

---

## Factor ladder (added 2026-11-15, after the original report)

**Question.** Starting from H1, does adding one factor at a time improve out-of-sample (OOS)
results net of fees under the queue fill models? Code: `pipeline/factors.py` (rules and grids,
fixed before any ladder result was seen) and `pipeline/ladder.py`. Tests: `pipeline/test_factors.py`.

**Design (same folds, simulator, fill models and fee charge as above).**
* Order: imb1 (H1 base) → trade-flow imbalance (`flow_1s`) → realised vol (`rv_10s`) → spread in
  ticks → depth change (`depth_chg`, top-5 quantity vs 1 s earlier, per side) → BTC lead-lag
  (`btc_ll`, ETH and SOL only). Each factor can only withdraw quotes. A side is quoted only if all
  kept factors allow it.
* Latency 50 ms, $1,000 orders, inventory cap 5 orders, the three fill models, fees -0.5 / 0 / 1 /
  7.5 / 10 bps charged per filled dollar.
* Thresholds are fitted **in-sample only**, per symbol, fill model and fold, on all hours before
  the fold. Kept factors keep the thresholds chosen for that fold at the earlier step. The new
  factor's θ (grid includes "off") maximises the training per-fill 5 s markout. It must keep at
  least 20% of the earlier step's training notional.
* **Keep rule**: OOS per-fill 5 s markout net of fees beats the earlier accepted step in at least
  4 of 5 folds (generally ceil(0.8 × folds)), under **both** queue_cons and queue_prop. A fee is
  a fixed charge per filled dollar, so a per-fill improvement holds at every fee level. A fold
  where the in-sample fit picks "off" (no change) does not count as an improvement. Rejected
  factors are recorded and the state stays unchanged. Per-fill markout is the criterion, not
  total net $. When everything loses, total net $ improves just by trading less.

**Ladder on the frozen window** (13:20 to 20:40, 5 hourly OOS folds 16:00 to 20:40; from
`results/runs/2026-11-15/ladder/`). Folds improved = OOS per-fill net markout better than the
earlier accepted step. The OOS per-fill 5 s markout of the model *after* the step is shown for
queue_cons / queue_prop, before fees (subtract the fee in bps for net):

| Step | Factor | BTC | ETH | SOL |
|---|---|---|---|---|
| 0 | imb1 (H1 base) | base. In-sample fit turned it **off** in every fold under both queue models (= BASE: −1.09 / −1.04 bps) | base. Off in 9 of 10 queue-model fold fits (−1.21 / −1.20) | base. θ = 0.6 in 2 folds under queue_cons, off otherwise (−1.36 / −1.26) |
| 1 | flow_1s | **REJECT** (0/5, 0/5; in-sample fit always "off") | **REJECT** (0/5, 0/5; always "off") | **REJECT** (1/5, 0/5) |
| 2 | rv_10s | **KEEP** (5/5, 5/5): −0.95 / −0.96 bps | **KEEP** (4/5, 5/5): −1.17 / −1.12 bps | **REJECT** (1/5, 0/5) |
| 3 | spread | **REJECT** (3/5, 3/5; changes within ±0.007 bps) | **REJECT** (2/5, 2/5) | **REJECT** (0/5, 0/5) |
| 4 | depth_chg | **REJECT** (0/5, 0/5; always "off") | **REJECT** (3/5, 1/5) | **REJECT** (1/5, 0/5) |
| 5 | btc_ll | not applicable | **REJECT** (2/5, 1/5) | **REJECT** (2/5, 2/5) |

Final ladder models (frozen window, OOS, queue_cons / queue_prop): BTC imb1+rv_10s −0.95 / −0.96
bps per fill; ETH imb1+rv_10s −1.17 / −1.12; SOL imb1 alone −1.36 / −1.26. Net of a 7.5 bp fee
they are −8.4 to −8.9 bps per fill. The OOS 5 s markout P&L at fee 0 is −$326 / −$440 (BTC),
−$753 / −$753 (ETH) and −$439 / −$677 (SOL). Even with the −0.5 bp rebate all six numbers stay
negative (`ladder/ladder_final.csv`). **No factor combination is profitable under the queue
models at any fee level tested.** Under the optimistic model the final models make +0.12 (BTC),
+0.00 (ETH) and +0.27 (SOL) bps before fees. Those turn negative at a 1 bp fee and are not
evidence of profit.

What the ladder shows (inference):
* **Only the volatility pause survives**, on BTC and ETH. It is the same effect H3 found:
  quoting only when trailing 10 s realised vol is ≤ 1.5 to 2 bps (BTC), or ≤ 2 to 10 bps (ETH, the
  chosen θ varies by fold). BTC gains +0.03 to +0.24 bps per fill in every fold. ETH is weaker:
  queue_cons improves in 4 of 5 folds, one of them by only +0.002 bps, and is −0.08 bps in
  the 16:00 fold. Treat ETH's KEEP as marginal.
* **Under the queue models, the "H1 base" is mostly BASE.** The in-sample per-fill fit switched the
  imbalance filter off, because it makes queue-model fills worse (as reported above). So the
  kept BTC/ETH model is effectively "rv pause" alone under the queue models. Under optimistic
  fills, imb1 = 0.2 is chosen in every fold.
* Trade-flow imbalance and depth change carry information that the optimistic model rewards
  (they improve optimistic per-fill markout in 4 to 5 of 5 folds for BTC and ETH). Under the
  queue models the in-sample fit almost never turns them on. The pattern is the same as H1:
  pulling the threatened side removes the benign fills, not the toxic ones.
* Spread in ticks has almost no variation (1 tick 99.8 to 99.99% of the time), so the filter
  rarely binds and its effects are ±0.01 bps. It is testable but uninformative on this data.
* BTC lead-lag stays unhelpful under the queue models, as in H2.
* No factor was untestable: every factor could be computed for every symbol where it applies.

**Cumulative-window check** (run `2026-11-15-adhoc-2135`, window 13:20 to 21:35, 6 folds, need ≥ 5):
the same decisions in every cell. BTC rv_10s 6/6, 6/6. ETH rv_10s 5/6, 6/6. BTC spread 4/6, 3/6
(rejected). SOL btc_ll 2/6, 3/6 (rejected). Final models: BTC −0.95 / −0.96 bps, ETH −1.18 /
−1.12, SOL −1.35 / −1.26 (queue_cons / queue_prop). This window **contains** the frozen window
plus 55 minutes, so it is not an independent replication.

Caveats specific to the ladder: forward selection is order-dependent. A factor rejected early is
never retried after later factors change the state. One session; 5 to 6 hourly folds are
autocorrelated. Thresholds of earlier factors are not refitted when a later factor is added.
Latency is fixed at 50 ms in the ladder (the earlier H1 to H3 results showed latency effects
below 0.15 bps).

## Dated runs and run index

Since 2026-11-15 each pipeline run writes `results/runs/<YYYY-MM-DD>/` and appends one row
to `results/runs/index.csv`. The row records the window, row counts per table and symbol, whether
the extract counts were consistent, the anomaly count and headline metrics. The daily entrypoint is
`pipeline/run_daily.sh` (see `pipeline/README.md`, "Daily run"). Runs so far:

| run_id | Window (UTC) | OOS folds | Trades BTC / ETH / SOL | Depth BTC / ETH / SOL | Comparable to cumulative runs? |
|---|---|---|---|---|---|
| `2026-11-15` | 13:20 to 20:40 | 5 | 1,691,750 / 753,949 / 252,109 | 262,711 / 259,099 / 217,670 | **No.** The frozen study window from the original report, kept for comparison. |
| `2026-11-15-adhoc-2135` | 13:20 to 21:35 | 6 | 1,805,134 / 829,789 / 269,222 | 295,494 / 291,026 / 243,513 | Yes. A manual verification run of `run_daily.sh`. It is named `-adhoc-2135` because `2026-11-15` is taken by the frozen run. |

Headline metrics in the index (latency 50 ms, OOS, per-fill 5 s markout before fees, queue_prop):
BASE −1.04 / −1.18 / −1.26 bps vs final ladder model −0.96 / −1.12 / −1.26 bps (BTC / ETH / SOL),
in both runs to within 0.01 bps. The first scheduled run, `2026-11-16`, will cover 13:20 on
11-15 to 00:00 on 11-16. Every later run extends the window from the same start. Numbers from
different runs share data and are not independent.
