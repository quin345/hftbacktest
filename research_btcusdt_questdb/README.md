# BTCUSDT USD-M public L2 → raw archive + QuestDB hot tables

Bounded live capture of Binance USD-M **BTCUSDT perpetual** public market data
(diff depth `@depth@100ms`, `bookTicker`, `aggTrade`) into:

1. a durable **raw archive** (exact wire frames, one gzip file per stream), and
2. **QuestDB** hot tables `btc_um_l2_*` (loopback only).

Scope limits (read first):

- Public **L2** only. Binance USD-M does not publish an order-level (L3) feed, so
  depth is aggregated price-level data.
- A complete reconstructed order book is **not** claimed. The local book is partial
  (REST snapshot limited to 1000 levels per side) and its top-of-book is cross-checked
  against `bookTicker` only when the update IDs coincide (see the report for coverage).
- No credentials, no trading or order endpoints, no paid services.

Pinned facts (verified 2026-11-15 against the official Binance USD-M docs; the full
report is `../../../research/btcusdt-questdb-live-report.md` relative to this directory,
i.e. `coordinator/research/btcusdt-questdb-live-report.md`):

| Item | Value |
|---|---|
| Depth + bookTicker | `wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker` |
| Trades | `wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade` |
| Snapshot | `GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000` |
| QuestDB | 10.2.0 `rt-linux-x86-64` (bundled JRE), vendored in `vendor/`, SHA-256 `2548a01aed2b30cb67aaa78f1bcbb49b507c36f9f44b7d39f31e5fce7268ad3a` |

## Layout

| Path | Purpose |
|---|---|
| `btc_l2/parse.py` | Frame classification, exact decimal → fixed-point (`*_e8`), strict field checks |
| `btc_l2/sequencer.py` | Official local-order-book procedure (pure state machine, no I/O) |
| `btc_l2/archive.py` | Raw archive writer/reader (`<recv_ns> <wire text>` lines) |
| `btc_l2/records.py` | Maps parsed events and sequencer outputs to ILP lines |
| `btc_l2/ilp.py` | Task-only DDL (`btc_um_l2_*`), loopback guard, batched ILP writer with disk spool |
| `btc_l2/capture.py` | Bounded capture CLI (websockets, reconnect, snapshot fetch, health rows) |
| `scripts/questdb_ctl.sh` | Start/stop/status of the project-local QuestDB |
| `scripts/run_proof.sh` | One-shot: start QuestDB → capture N s → verify → stop QuestDB |
| `scripts/verify_questdb.py` | Read-only SQL verification + archive reconciliation |
| `questdb/server.loopback.conf` | QuestDB config: HTTP bound to 127.0.0.1, ILP TCP / PG wire / min-HTTP / telemetry disabled |
| `tests/` | 24 unit tests; **all fixtures are SYNTHETIC and labelled as such** |
| `data/` | Captured archives, spool files, QuestDB data root (gitignored) |

## Setup (already done in this checkout)

```bash
# Python deps are installed into the project venv only (no global installs)
~/.local/bin/uv pip install --python ../.venv/bin/python websockets   # websockets 17.4
# QuestDB: vendor/questdb-10.2.0-rt-linux-x86-64 extracted from the verified tarball
```

## Run a bounded capture with verification

```bash
cd research_btcusdt_questdb
./scripts/run_proof.sh 180        # 1..600 seconds; ~4 min including startup and verify
```

`run_proof.sh` starts QuestDB, waits for readiness, runs the capture
(`python -m btc_l2.capture --seconds N`), runs `scripts/verify_questdb.py` on the new
session, and stops QuestDB on exit. Outputs:

- `data/capture/<session_id>/{public,market,rest}.ndjson.gz` — raw archive
- `data/capture/<session_id>/session_summary.json` — counts and stop reason
- `data/verify_<session_id>.json` — SQL results and archive reconciliation

Hard caps: `--seconds` ≤ 600 and `--max-bytes` ≤ 1 GiB archive bytes (the capture stops
with `size_cap_reached` otherwise).

### Lifecycle note (observed)

QuestDB started from an exec/tool session was terminated when that session ended (three
attempts: plain start, `setsid`, `nohup`). It therefore runs in the foreground of
`run_proof.sh`, which stops it on exit. To keep QuestDB running for ad-hoc queries, run
`QDB_DISABLE_HUP_HANDLER=true ./scripts/questdb_ctl.sh start` from a long-lived
supervisor. Stop with `./scripts/questdb_ctl.sh stop`. Data persists in
`data/questdb-root` either way.

### Query examples (loopback HTTP, read-only)

```bash
curl -G 'http://127.0.0.1:9000/exec' --data-urlencode \
  "query=SELECT count() FROM btc_um_l2_depth_diff WHERE session_id='<SID>' AND side='header'"
```

Tables (all `TIMESTAMP(ts)` = local receipt time in ns, `PARTITION BY DAY`, WAL):

- Timestamp precision: the ILP writer sends nanoseconds (`precision=n`), but QuestDB
  stores `TIMESTAMP` at **microsecond** resolution. Sub-microsecond digits are truncated in
  the hot tables, and the hot tables have **no** exact `recv_ns` column. Exact `recv_ns` is
  preserved in the raw archive (`<recv_ns> <frame>` lines). Order by `archive_file,
  archive_line` or `sync_epoch`, not by `ts` alone (same-microsecond collisions are possible;
  not measured). No migration was done.
- `btc_um_l2_depth_diff` — one `header` row per depth event (`side='header'`) plus one row per
  price level (`side` = `bid`/`ask`). `apply_status`: `applied`, `applied_aligned`,
  `stale_before_snapshot`, `gap_trigger`, `discarded_gap`, `pending_unsynced`.
  Columns: `event_time_ms` (E), `tx_time_ms` (T), `first_update_id` (U),
  `final_update_id` (u), `prev_final_update_id` (pu), `price_str`/`qty_str` (verbatim),
  `price_e8`/`qty_e8` (exact integer, 1e-8), `is_remove` (qty = 0), `sync_epoch`,
  `archive_file`/`archive_line` (exact raw frame).
- `btc_um_l2_depth_snapshot` — REST snapshot levels; `aligned` flag; `last_update_id`.
  **Filter `aligned = true`.** A session can contain rejected candidates: the smoke session
  `20261115T092206Z-11ce09d9` has one `aligned=false` snapshot (`align_failed`:
  `snapshot_newer_than_buffer`) followed by an accepted `aligned=true` snapshot
  (`snapshot_ok`). Rejected rows are candidates, not second initialisations.
- `btc_um_l2_book_ticker` — `update_id`, best bid/ask with exact strings, `book_top_check`
  (`match` / `mismatch` / `not_checked`).
- `btc_um_l2_agg_trade` — `agg_trade_id` (a), `first_trade_id` (f), `last_trade_id` (l),
  `buyer_is_maker` (m), `aggressor_side`: **`m=true` → `SELL`**, `m=false` → `BUY`.
- `btc_um_l2_ingest_event` — connect/disconnect, gap, resync, snapshot, parse errors, session start/stop.
- `btc_um_l2_health` — periodic counters, archive bytes, ILP rows/errors/spool, recv lag.

Timestamps: QuestDB `ts` is local receipt time. Exchange times are `*_time_ms` (ms).
Prices and quantities are kept as strings and as exact 1e-8 integers; a DOUBLE copy is
included for convenience only. Values with more than 8 decimals are rejected, not rounded.

## Gap, snapshot and restart handling

- Snapshot alignment follows the official procedure: buffer → snapshot →
  drop `u < lastUpdateId` → first event must satisfy `U ≤ lastUpdateId ≤ u` → then
  every event must satisfy `pu == previous u`.
- A `pu` mismatch in live mode logs a `gap` event, marks the triggering event
  `gap_trigger` (not applied), clears the local book and requests a new snapshot
  (new `sync_epoch`).
- Websocket disconnect or reconnect discards the local book and buffered events
  (`discarded_gap`). Reconnect uses exponential backoff up to 30 s.
- **Trade-ID continuity across reconnects is not implemented.** `market_loop` does not mark
  or check `a[n] == a[n-1] + 1` across a reconnect. Trade completeness is claimed only for
  the contiguous window of one session, where the IDs were checked (5,920 IDs, no gaps).
- Restarts create a new `session_id`. Cross-session duplicates can be found with
  `GROUP BY symbol, first_update_id, final_update_id` over `depth_diff` headers.
  Restart duplicates are **not exercised live** in the recorded proof.

## Replay: scope and limits (parser compatibility only)

This project is a **live capture and storage proof**. It is not a replay, book
reconstruction or production service. The archive is in the same `<recv_ns> <json>`
line format as `collector/`. The existing `binancefutures.convert()` parses it, but that
is **parser compatibility only**:

- It converts the depth-diff stream only (`public.ndjson.gz`). It does **not** convert
  trades (`market.ndjson.gz`, `aggTrade`), and it ignores bookTicker when `opt=''`.
- It does **not** initialise the book from a snapshot. The output starts from an empty book.
- A `.npz` produced this way is **UNSAFE for standalone backtest**. It is a parser
  check artifact, not a reconstructed book.

A correct replay must follow this order, which is the sequencer's application order and
not a raw receipt-time merge:

1. Filter `rest.ndjson.gz` to the **`aligned=true`** snapshot (`snapshot_ok`).
2. Apply the snapshot as the initial book (clear, then insert all levels).
3. Apply the covering diff (`applied_aligned`) and then the later `applied` events, in
   sequencer order. Diffs with `u < lastUpdateId` must not be applied.
4. Trades are a separate stream and must be merged explicitly, if needed.

A naive merge by receipt time would apply a diff that was received before the snapshot.
In the main session the first covering diff (receipt 09:23:11.525229) arrived before the
accepted snapshot (09:23:11.541138). This step is **not implemented** here.

## Verification scope (counts, not per-row)

`scripts/verify_questdb.py` checks **row counts, time ranges, duplicate keys and
frame-count reconciliation** against the archive. It does **not** join each DB row to its
`archive_file`/`archive_line`, and it does not compare price or quantity content. Treat it
as count-level evidence only.

`btc_um_l2_health` rows are emitted before the writer drains, so the **last health row
excludes its own and final in-flight writes** (e.g. 417,752 vs 417,754 in the main
session). Use `session_summary.json` / the summary SQL total as authoritative.

## Tests

```bash
../.venv/bin/python -m pytest tests -q        # 24 passed at time of writing
```

Tests cover fixed-point conversion and rejection, frame classification, strict field
validation, archive round-trip and truncation tolerance, ILP escaping, the loopback guard,
and the sequencer (alignment, stale snapshot, buffer gap, live gap, reconnect, stale
epoch, book-top check). Fixtures are synthetic and labelled. Live evidence comes from
`verify_questdb.py`, not from the unit tests.

## Known limits

- Gap, resync and snapshot-misalignment paths were exercised by synthetic tests only;
  the recorded live runs had no gaps.
- `book_top_check` coverage is low: bookTicker `u` rarely equals the depth book's last
  update ID. Mismatches are reported, not hidden.
- No reconstructed-book claim beyond the aligned, gap-free interval of each session.
- Clock: receipt times come from the host clock; no NTP verification was performed.
- Binance routed-endpoint docs and the legacy `wss://fstream.binance.com/stream` path
  disagree about availability (see the report). This project uses the documented routed
  endpoints only.
