# USD-M BTCUSDT grid market-making research baseline

A small, reproducible backtest baseline built on the `quin345/hftbacktest` fork
at commit `5f3ec40b2afb764e0fea112f941ed85523ef4e88` (master, 2025-12-23).
It uses only the sample data bundled in `examples/usdm/`.

**This is a research baseline, not a profitability claim.** It has no live
trading, no exchange connection, no API keys, and no parameter optimisation.
Its numbers are produced by demo assumptions (see below) and must not be read
as Binance facts or as strategy performance.

## Contents

| Path | Purpose |
|---|---|
| `grid_baseline.py` | CLI: `prepare`, `run`, `compare` |
| `config/baseline.toml` | All assumptions, each labelled `sample-data`, `repo-example`, or `demo-assumed` |
| `tests/test_grid_baseline.py` | Config labels, metric arithmetic, conversion checks, run repeatability |
| `locks/Cargo.lock.msrv-fallback` | Cargo lockfile used to build (MSRV-compatible resolution, see below) |
| `locks/requirements-freeze.txt` | `uv pip freeze` of the project venv |
| `data/` | Generated normalized events (gitignored) |
| `output/` | Run outputs: `recorder.npz`, `metrics.json` (gitignored) |

Upstream files are unchanged. The only tracked-file change is an appended
block in the root `.gitignore` (venv, generated data and outputs, env files).

## Setup (already done in this checkout)

The venv and build live inside the project; nothing global was changed.

```bash
# user-local tooling (not system-wide)
#   uv 0.13.8 -> ~/.local/bin/uv
#   Python 3.12.16 via uv (numba 0.61+ pins; the system Python is 3.14)
#   Rust 1.91.1 via rustup -> ~/.rustup, ~/.cargo (matches crate rust-version)

uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install "maturin~=1.7"
# Cargo lockfile: the repo gitignores Cargo.lock, so resolution is generated.
# Without MSRV-aware resolution, the latest crates require rustc 1.94.1 and the build fails.
CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS=fallback cargo generate-lockfile   # at repo root
(cd py-hftbacktest && uv pip install .)     # builds the Rust extension with maturin
uv pip install pytest
```

Resolved versions: hftbacktest 2.4.4 (built from this fork), numba 0.68.1,
numpy 2.2.6, polars 2.1.0, maturin 1.16.0, Python 3.12.16, rustc 1.91.1.
Full list: `locks/requirements-freeze.txt`.

## Usage (from the project root)

```bash
.venv/bin/python research_baseline/grid_baseline.py prepare
.venv/bin/python research_baseline/grid_baseline.py run --out research_baseline/output/run1 --label run1 \
    --file examples/usdm/btcusdt_20240808.gz
.venv/bin/python research_baseline/grid_baseline.py run --out research_baseline/output/run2 --label run2 \
    --file examples/usdm/btcusdt_20240808.gz
.venv/bin/python research_baseline/grid_baseline.py compare research_baseline/output/run1 research_baseline/output/run2
.venv/bin/python -m pytest research_baseline/tests -q
```

`prepare` converts each raw file to `data/<stem>_normalized.npz` and writes
`data/prepare_provenance.json`. Each file is run as its own session. Events
before the file's first `DEPTH_CLEAR` row (its opening REST snapshot) are
dropped. The sample files are not concatenated in the baseline (design choice,
see "Session design" below).

`run` verifies the prepared file against `prepare_provenance.json` before it
simulates anything. It checks the normalized content hash, the raw file hash,
and a hash of the conversion settings in `config/baseline.toml`. Any mismatch
exits with status 3 and writes no output. `run --file` selects one of the two
sessions.

`compare` exits 0 only if the input, record, and config hashes match and all
metrics are equal.

## Session design

- **Two independent sessions**, not one. Each raw file opens with its own REST
  snapshot, so each is trimmed from its first `DEPTH_CLEAR` row.
- **Actual durations (local_ts):**

  | File | Raw span | Dropped before first snapshot | Trimmed span |
  |---|---|---|---|
  | `btcusdt_20240808.gz` | 5.8 min (23:54:16.6 to 00:00:01.1 UTC) | 263 events | 344.5 s (5.7 min) |
  | `btcusdt_20240809.gz` | 5.0 min (00:00:00 to 00:05:02 UTC) | 200,417 events | 215.3 s (3.6 min) |

- **Gap between trimmed sessions:** 87.3 s (8/08 ends 00:00:01.105, 8/09 starts 00:01:28.445 UTC).
- **Concatenation was not used.** `review_evidence/check_concat_and_clears.py`
  shows that `np.concatenate` of the two trimmed arrays passes
  `validate_event_order`. An earlier note here said it failed. That was
  caused by an extra `correct_event_order` step applied after the concatenation,
  which is what fails. Sessions stay separate because the 87 s gap has no
  verified joint book state, not because of a validation failure.

## DEPTH_CLEAR events (disclosed, not trimmed)

Each file has **four** `DEPTH_CLEAR` rows, not one:

| File | Rows (kept array) | Sides |
|---|---|---|
| 8/08 | 0, 1001, 2072, 3073 | BUY, SELL, BUY, SELL |
| 8/09 | 0, 1001, 2358, 3359 | BUY, SELL, BUY, SELL |

- Each raw file contains **one** REST snapshot line (8/08 line 55, 8/09 line 44,234). The converter writes that snapshot as a BUY clear block and a SELL clear block, and each clear is followed by 1000 snapshot rows.
- The two pairs in each file are **identical** in price and quantity (checked for 8/08: block at rows 1–1000 equals rows 2073–3072). The converter therefore emits the same snapshot twice. This is a converter output property, not a second exchange snapshot.
- The `DEPTH_CLEAR` rows are **side-level** clears. Each clears one side only, up to the snapshot's price range, so none is a full book reset on its own.
- **Effect on fills and orders is not measured.** The baseline recorder only logs mid-price, position, balance, and fee per 100 ms step. It does not log individual fills or resting orders around each clear, so no statement about fill behaviour after the repeated clears is made.
- Trimming removes only the events before the first clear. The later clear rows are kept, as in the converter's output.

## Deviations from the notebook

The strategy function is ported without logic changes. The backtest setup
differs from the notebook in the following ways:

| Item | Notebook (`High-Frequency Grid Trading - Simplified from GLFT.ipynb`) | Baseline |
|---|---|---|
| Depth backend | `ROIVectorMarketDepthBacktest`, `roi_lb=0`, `roi_ub=mid*10` | `HashMapMarketDepthBacktest` (no ROI bounds) |
| Initial snapshot | `.initial_snapshot(<external eod npz>)` | **None.** The book comes from the file's own snapshot rows. |
| Order latency | `intp_order_latency(<external samples>)` | Constant 20 ms entry and response |
| Queue model | `power_prob_queue_model3(3.0)` | same |
| Fees | `trading_value_fee_model(-0.00005, 0.0007)` (rebate) | `trading_value_fee_model(0.0002, 0.0005)` (demo) |
| Tick, lot | per-asset from json | 0.10, 0.001 (demo) |
| Volatility window | 6000 samples | 600 samples |
| Data | Tardis-era npz files, not in repo | bundled raw `.gz` only |

Results from this baseline are therefore not comparable to the notebook's
figures.

## Strategy

`grid_baseline.grid_strategy` is a Numba port of `gridtrading` in
`examples/High-Frequency Grid Trading - Simplified from GLFT.ipynb` (cell 1).
The logic is unchanged except for these differences:
- The notebook's 300,000,000-element arrays are sized to the data span.
- Parameters are passed in as one array.
- Volatility window: 600 samples instead of 6000 (see deviations).

Quotes are a 20-level grid on each side. Half-spread is volatility × 5; skew
follows the normalised inventory; order notional is 100 quote units; orders use
GTX (post-only). Quoting stops on one side when inventory reaches ±1000 quote units.

## Assumptions (all in `config/baseline.toml`)

| Item | Value | Status | Note |
|---|---|---|---|
| Data | `btcusdt_20240808.gz`, `btcusdt_20240809.gz` | sample-data | 5.8 and 5.0 min raw spans; trimmed 5.7 and 3.6 min (see Session design) |
| Contract | linear, size 1.0 | repo-example | |
| Tick / lot | 0.10 / 0.001 | **demo-assumed** | Not checked against exchangeInfo; the sample has none |
| Fees | maker 0.02%, taker 0.05% (positive = paid) | **demo-assumed** | Illustrative base tier, not verified. The repo's LP example uses a −0.005% maker rebate |
| Latency | constant 20 ms entry, 20 ms response | **demo-assumed** | No order-latency samples bundled |
| Queue model | `power_prob_queue_model3(3.0)` | repo-example | |
| Volatility window | 600 samples (1 min) | **demo deviation** | Notebook uses 6000 (10 min). 10 min would leave no trading time in these sessions |

## Metrics

`metrics.json` fields: records, span, final position, max |position| notional,
trade count, traded volume and value, net fees paid, equity before and after
fees, and maximum drawdown (net). Equity follows the repo's `LinearAssetRecord`
definition: balance + position × mid. Net equity is equity before fees minus
fees paid.

## Inventory limit (soft)

The notebook's inventory control stops quoting on a side when normalized
inventory reaches +1 (long) or −1 (short), which is ±1000 quote units of
notional. It is a threshold on new quotes, not a hard risk limit:

- Orders already resting are cancelled only when they fall out of the grid, and cancellations take the configured response latency to take effect. Fills that land during that window can take inventory past ±1000.
- The threshold logic is covered by a formula test only (`InventoryLimitTests`). No recorded run reached the limit: max |position| notional is 246.9 (8/08) and 0 (8/09).

## Zero-fill second session

The 8/09 session produced **no fills, no trades, and no PnL** (`metrics.json`: 0
trades, equity 0). It exercised the data path, not the fill path. It is not
evidence of anything about strategy behaviour.

## Provenance and tamper checks

`prepare` records each normalized file's content hash, the raw hash, and a hash
of the conversion settings in `prepare_provenance.json`. `run` checks these
before simulating. Tests cover: clean pass, tampered price, truncated data,
changed `base_latency_ns`, changed trim setting, and a CLI run that refuses
tampered data (exit 3). The check covers the conversion settings in the config
file. It does not cover code changes to `convert()` in the installed package.

## Limitations

- **Very short samples.** Trimmed sessions are 5.7 min (8/08) and 3.6 min (8/09). The 8/08 session has 2 trades; the 8/09 session has 0 trades and exercised no fills (see Zero-fill session). Metrics are a smoke test, not a performance estimate.
- **Funding omitted.** `convert()` is called with `opt=''`, so mark-price and funding events are dropped. The sample does not cross a settlement that was checked. Net economics are therefore incomplete.
- **No market impact**, and queue position is a model estimate (repo `docs/order_fill.rst`).
- **Constant latency only.** Interpolated latency samples are not bundled.
- **Demo parameters.** Fees, tick/lot, and latency are not Binance facts. Results change materially if they change.
- **No optimisation.** One parameter set was run; there is no sweep and no profitability claim.
- **Upstream tests.** `py-hftbacktest/tests/test_hftbacktest.py` needs `tmp_20240501.npz`, which is not in the repo, so it fails. Its code is unchanged. The baseline's own tests live in `research_baseline/tests/`.
- **Cargo.lock** is gitignored upstream. The MSRV-fallback lock is reproduced here, but a different resolver result would change the build.
