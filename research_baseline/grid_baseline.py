"""Reproducible USD-M BTCUSDT grid market-making baseline on bundled sample data.

Subcommands:
  prepare  Convert the bundled raw Binance USD-M file to hftbacktest events (.npz).
  run      Run one backtest and write recorder data, metrics, and hashes.
  compare  Compare two run directories for exact equality.

This is a research baseline. It is not a profitability claim, not a live
trading tool, and makes no exchange connection.

Run from the project root, e.g.:
  .venv/bin/python research_baseline/grid_baseline.py prepare
  .venv/bin/python research_baseline/grid_baseline.py run --out research_baseline/output/run1
  .venv/bin/python research_baseline/grid_baseline.py compare research_baseline/output/run1 research_baseline/output/run2
"""

import argparse
import hashlib
import json
import math
import platform
import sys
import tomllib
from pathlib import Path

import numpy as np
from numba import njit, uint64, float64
from numba.typed import Dict

import hftbacktest as hbt_pkg
from hftbacktest import (
    BUY, SELL, GTX, LIMIT, BacktestAsset, HashMapMarketDepthBacktest, Recorder,
)
from hftbacktest.types import DEPTH_CLEAR_EVENT, BUY_EVENT

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / 'research_baseline' / 'config' / 'baseline.toml'
DATA_DIR = PROJECT_ROOT / 'research_baseline' / 'data'
EVENT_TYPE_MASK = (1 << 28) - 1  # low bits hold the event type; BUY/SELL/EXCH/LOCAL flags sit above


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, 'rb') as f:
        return tomllib.load(f)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def sha256_array(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr).tobytes()).hexdigest()


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def convert_raw(raw_path: Path, cfg: dict, out_path: Path) -> dict:
    """Convert one raw stream file and trim events before its first DEPTH_CLEAR.

    Each sample file is run as its own session. The first DEPTH_CLEAR row is the
    REST snapshot the file opens with. Later DEPTH_CLEAR rows (see
    count_depth_clears) are side-level clears emitted by the converter and are
    kept in the data unchanged.

    Files are not concatenated in the baseline. Design choice: keeping sessions
    separate avoids an unverified joint book state across an ~87 s gap between
    the trimmed files. Concatenation of the trimmed arrays does pass
    validate_event_order (checked in research_baseline/review_evidence/).
    """
    from hftbacktest.data.utils.binancefutures import convert

    events = convert(
        str(raw_path),
        output_filename=None,
        opt='',
        base_latency=float(cfg['data']['base_latency_ns']),
        combined_stream=True,
        buffer_size=20_000_000,
    )
    n_total = len(events)
    dropped = 0
    if cfg['data']['trim_before_first_snapshot']:
        type_bits = events['ev'] & EVENT_TYPE_MASK
        clear_idx = np.flatnonzero(type_bits == DEPTH_CLEAR_EVENT)
        if len(clear_idx) == 0:
            raise RuntimeError('no DEPTH_CLEAR (snapshot) event found; cannot trim')
        dropped = int(clear_idx[0])
        events = events[dropped:]

    # Save with the 'data' key expected by BacktestAsset.data().
    np.savez_compressed(out_path, data=events)

    return {
        'raw_file': _rel(raw_path),
        'raw_sha256': sha256_file(raw_path),
        'conversion_sha256': conversion_hash(cfg),
        'events_converted': n_total,
        'events_dropped_before_snapshot': dropped,
        'events_kept': int(len(events)),
        'depth_clear_rows_kept': count_depth_clears(events),
        'local_ts_first': int(events['local_ts'][0]),
        'local_ts_last': int(events['local_ts'][-1]),
        'normalized_file': _rel(out_path),
        'normalized_sha256_content': sha256_array(events),
    }


def count_depth_clears(events: np.ndarray) -> dict:
    """DEPTH_CLEAR rows by side, with their row indices (0-based, in the kept array)."""
    type_bits = events['ev'] & EVENT_TYPE_MASK
    idx = np.flatnonzero(type_bits == DEPTH_CLEAR_EVENT)
    sides = ['BUY' if events['ev'][i] & BUY_EVENT else 'SELL' for i in idx]
    return {'count': int(len(idx)), 'rows': [int(i) for i in idx], 'sides': sides}


def conversion_hash(cfg: dict) -> str:
    """Hash of the config values that affect conversion and trimming."""
    keys = {
        'raw_files': cfg['data']['raw_files'],
        'base_latency_ns': cfg['data']['base_latency_ns'],
        'trim_before_first_snapshot': cfg['data']['trim_before_first_snapshot'],
        'convert_opt': '',
        'combined_stream': True,
        'buffer_size': 20_000_000,
        'convert_function': 'hftbacktest.data.utils.binancefutures.convert',
    }
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()


def normalized_name(raw_file: str) -> str:
    return f'{Path(raw_file).stem}_normalized.npz'


def cmd_prepare(args) -> int:
    cfg = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    infos = []
    for raw in cfg['data']['raw_files']:
        out_path = DATA_DIR / normalized_name(raw)
        info = convert_raw(PROJECT_ROOT / raw, cfg, out_path)
        infos.append(info)
    prov_path = DATA_DIR / 'prepare_provenance.json'
    prov_path.write_text(json.dumps({'files': infos}, indent=2) + '\n')
    print(json.dumps(infos, indent=2))
    return 0


# ---------------------------------------------------------------------------
# strategy (ported from the repo's notebook; see docstring for source)
# ---------------------------------------------------------------------------

@njit
def grid_strategy(hbt, recorder, mid_price_chg, params):
    """Grid market making with volatility-scaled half spread and inventory skew.

    Ported from "High-Frequency Grid Trading - Simplified from GLFT.ipynb",
    cell 1 (function `gridtrading`). Changes from the notebook:
      * `mid_price_chg` is passed in and sized to the data span (the notebook
        hard-codes 300,000,000 elements).
      * Parameters are passed as one float64 array `params`.
    Behaviour is otherwise unchanged.

    params indices:
      0 vol_to_half_spread, 1 grid_num, 2 skew, 3 max_notional_position,
      4 notional_qty, 5 vol_window, 6 vol_update_every, 7 vol_annualization
    """
    vol_to_half_spread = params[0]
    grid_num = int(params[1])
    skew = params[2]
    max_notional_position = params[3]
    notional_qty = params[4]
    vol_window = int(params[5])
    vol_update_every = int(params[6])
    vol_annualization = params[7]

    asset_no = 0
    tick_size = hbt.depth(asset_no).tick_size
    lot_size = hbt.depth(asset_no).lot_size

    t = 0
    prev_mid_price_tick = np.nan
    mid_price_tick = np.nan
    volatility = np.nan

    # Running interval in nanoseconds (100 ms, matching config strategy.sample_interval_ns).
    while hbt.elapse(100_000_000) == 0:
        # Clears cancelled, filled or expired orders.
        hbt.clear_inactive_orders(asset_no)

        depth = hbt.depth(asset_no)
        position = hbt.position(asset_no)
        orders = hbt.orders(asset_no)

        best_bid = depth.best_bid
        best_ask = depth.best_ask
        best_bid_qty = depth.best_bid_qty
        best_ask_qty = depth.best_ask_qty

        micro_price = (best_bid * best_ask_qty + best_ask * best_bid_qty) / (best_bid_qty + best_ask_qty)
        mid_price = (best_bid + best_ask) / 2.0

        mid_price_tick = mid_price / tick_size

        # Records the mid-price change for volatility calculation.
        if t >= len(mid_price_chg):
            raise IndexError('mid_price_chg too short; increase sizing')
        mid_price_chg[t] = mid_price_tick - prev_mid_price_tick
        prev_mid_price_tick = mid_price_tick

        # Updates the volatility every vol_update_every samples over a vol_window window.
        if t % vol_update_every == 0:
            if t >= vol_window - 1:
                volatility = np.nanstd(mid_price_chg[t + 1 - vol_window:t + 1]) * np.sqrt(vol_annualization)

        notional_position = position * mid_price
        normalized_position = notional_position / max_notional_position

        half_spread_tick = volatility * vol_to_half_spread

        bid_depth_tick = half_spread_tick * (1 + skew * normalized_position)
        ask_depth_tick = half_spread_tick * (1 - skew * normalized_position)

        forecast_mid_price = micro_price

        # Sets the order quantity to be equivalent to a notional value of notional_qty.
        order_qty = max(round((notional_qty / mid_price) / lot_size), 1) * lot_size

        # Limits prices to the best bid/ask so the quotes do not cross the spread.
        bid_price = np.minimum(forecast_mid_price - bid_depth_tick * tick_size, best_bid)
        ask_price = np.maximum(forecast_mid_price + ask_depth_tick * tick_size, best_ask)

        # min grid step = tick size (notebook: min_grid_step = tick_size in its Binance call).
        min_grid_step = tick_size
        grid_interval = max(np.round(half_spread_tick * tick_size / min_grid_step) * min_grid_step, min_grid_step)

        # Aligns the prices to the grid.
        bid_price = np.floor(bid_price / grid_interval) * grid_interval
        ask_price = np.ceil(ask_price / grid_interval) * grid_interval

        # Creates a new grid for buy orders.
        new_bid_orders = Dict.empty(uint64, float64)
        if normalized_position < 1 and np.isfinite(bid_price):
            for i in range(grid_num):
                bid_price_tick = round(bid_price / tick_size)
                new_bid_orders[uint64(bid_price_tick)] = bid_price
                bid_price -= grid_interval

        # Creates a new grid for sell orders.
        new_ask_orders = Dict.empty(uint64, float64)
        if normalized_position > -1 and np.isfinite(ask_price):
            for i in range(grid_num):
                ask_price_tick = round(ask_price / tick_size)
                new_ask_orders[uint64(ask_price_tick)] = ask_price
                ask_price += grid_interval

        order_values = orders.values()
        while order_values.has_next():
            order = order_values.get()
            # Cancels if a working order is not in the new grid.
            if order.cancellable:
                if (
                    (order.side == BUY and order.order_id not in new_bid_orders)
                    or (order.side == SELL and order.order_id not in new_ask_orders)
                ):
                    hbt.cancel(asset_no, order.order_id, False)

        for order_id, order_price in new_bid_orders.items():
            # Posts a new buy order if there is no working order at the price on the new grid.
            if order_id not in orders:
                hbt.submit_buy_order(asset_no, order_id, order_price, order_qty, GTX, LIMIT, False)

        for order_id, order_price in new_ask_orders.items():
            # Posts a new sell order if there is no working order at the price on the new grid.
            if order_id not in orders:
                hbt.submit_sell_order(asset_no, order_id, order_price, order_qty, GTX, LIMIT, False)

        t += 1

        recorder.record(hbt)

    return t


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def build_asset(cfg: dict, data_path: Path) -> BacktestAsset:
    ins = cfg['instrument']
    fees = cfg['fees']
    lat = cfg['latency']
    q = cfg['queue']
    if q['model'] != 'power_prob_queue_model3':
        raise ValueError('only power_prob_queue_model3 is wired in this baseline')
    return (
        BacktestAsset()
        .data(str(data_path))
        .linear_asset(float(ins['contract_size']))
        .constant_order_latency(int(lat['entry_ns']), int(lat['resp_ns']))
        .power_prob_queue_model3(float(q['n']))
        .no_partial_fill_exchange()
        .trading_value_fee_model(float(fees['maker_fee']), float(fees['taker_fee']))
        .tick_size(float(ins['tick_size']))
        .lot_size(float(ins['lot_size']))
    )


def metrics_from_record(rec: np.ndarray) -> dict:
    """Final-state and path metrics from the recorder array (single asset).

    Equity follows the repo's LinearAssetRecord definition: balance + position * mid,
    less fees paid. Max drawdown is computed on net equity.
    """
    ts = rec['timestamp'].astype(np.int64)
    price = rec['price'].astype(np.float64)
    pos = rec['position'].astype(np.float64)
    bal = rec['balance'].astype(np.float64)
    fee = rec['fee'].astype(np.float64)
    equity_wo_fee = bal + pos * price  # linear contract: position * mid
    equity = equity_wo_fee - fee
    running_max = np.maximum.accumulate(equity)
    drawdown = running_max - equity
    span_h = (ts[-1] - ts[0]) / 1e9 / 3600.0
    return {
        'records': int(len(rec)),
        'start_ts_ns': int(ts[0]),
        'end_ts_ns': int(ts[-1]),
        'span_hours': round(span_h, 6),
        'final_position': float(pos[-1]),
        'max_abs_position_notional': float(np.max(np.abs(pos * price))),
        'num_trades': int(rec['num_trades'][-1]),
        'trading_volume': float(rec['trading_volume'][-1]),
        'trading_value': float(rec['trading_value'][-1]),
        'fees_paid_net': float(fee[-1]),
        'final_equity_wo_fee': float(equity_wo_fee[-1]),
        'final_equity_net_of_fees': float(equity[-1]),
        'max_drawdown_net': float(np.max(drawdown)),
    }


class ProvenanceError(RuntimeError):
    pass


def verify_provenance(raw_file: str, cfg: dict, data_path: Path) -> dict:
    """Fail unless the normalized file and conversion settings match prepare_provenance.json.

    Checks: the prepared file exists in provenance, its content hash matches the
    file on disk (catches tampering or truncation), the raw file's hash still
    matches, and the conversion settings hash matches the current config.
    """
    prov_path = DATA_DIR / 'prepare_provenance.json'
    if not prov_path.exists():
        raise ProvenanceError(f'{prov_path} missing; run prepare')
    entries = json.loads(prov_path.read_text())['files']
    rel_out = _rel(data_path)
    entry = next((e for e in entries if e['normalized_file'] == rel_out), None)
    if entry is None:
        raise ProvenanceError(f'no provenance entry for {rel_out}')
    if conversion_hash(cfg) != entry['conversion_sha256']:
        raise ProvenanceError('conversion settings in config differ from those used by prepare; rerun prepare')
    raw_path = PROJECT_ROOT / raw_file
    if sha256_file(raw_path) != entry['raw_sha256']:
        raise ProvenanceError(f'raw file hash differs from provenance: {raw_file}')
    actual = sha256_array(np.load(data_path)['data'])
    if actual != entry['normalized_sha256_content']:
        raise ProvenanceError(
            f'normalized content hash mismatch for {rel_out}: expected '
            f'{entry["normalized_sha256_content"][:16]}..., got {actual[:16]}...'
        )
    return entry


def cmd_run(args) -> int:
    cfg = load_config()
    if args.file is None:
        args.file = cfg['data']['raw_files'][0]
    if args.file not in cfg['data']['raw_files']:
        print(f'{args.file} is not in data.raw_files', file=sys.stderr)
        return 2
    data_path = DATA_DIR / normalized_name(args.file)
    if not data_path.exists():
        print(f'missing {data_path}; run prepare first', file=sys.stderr)
        return 2
    try:
        verify_provenance(args.file, cfg, data_path)
    except ProvenanceError as e:
        print(f'provenance check failed: {e}', file=sys.stderr)
        return 3
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    events = np.load(data_path)['data']
    if args.max_minutes is not None:
        start = events['local_ts'][0]
        cutoff = start + int(args.max_minutes * 60e9)
        events = events[events['local_ts'] <= cutoff]
        tmp_path = out / 'input_window.npz'
        np.savez_compressed(tmp_path, data=events)
        data_path = tmp_path

    span_ns = int(events['local_ts'][-1] - events['local_ts'][0])
    interval = int(cfg['strategy']['sample_interval_ns'])
    n_steps = span_ns // interval + 10_000

    asset = build_asset(cfg, data_path)
    # HashMap depth backend, no initial_snapshot: the book is built from the
    # file's own DEPTH_CLEAR/snapshot rows. The notebook uses ROIVector with
    # roi bounds and an external initial_snapshot; see README "Deviations".
    hbt = HashMapMarketDepthBacktest([asset])
    recorder = Recorder(1, n_steps)
    mid_price_chg = np.full(n_steps, np.nan, np.float64)

    s = cfg['strategy']
    params = np.array([
        s['vol_to_half_spread'], s['grid_num'], s['skew'], s['max_notional_position'],
        s['notional_qty'], s['vol_window_samples'], s['vol_update_every_samples'],
        s['vol_annualization'],
    ], dtype=np.float64)

    steps = grid_strategy(hbt, recorder.recorder, mid_price_chg, params)
    hbt.close()

    rec_path = out / 'recorder.npz'
    recorder.to_npz(str(rec_path))
    rec = np.load(rec_path)['0']
    rec = rec[rec['timestamp'] != 0]  # keep rows actually written

    metrics = metrics_from_record(rec)
    metrics['strategy_steps'] = int(steps)
    result = {
        'run_label': args.label or out.name,
        'python': platform.python_version(),
        'numpy': np.__version__,
        'numba': __import__('numba').__version__,
        'hftbacktest_package_file': hbt_pkg.__file__,
        'input_events': int(len(events)),
        'input_sha256_content': sha256_array(events),
        'record_sha256_content': sha256_array(rec),
        'config_sha256': sha256_file(CONFIG_PATH),
        'metrics': metrics,
    }
    (out / 'metrics.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

def cmd_compare(args) -> int:
    a = json.loads((Path(args.a) / 'metrics.json').read_text())
    b = json.loads((Path(args.b) / 'metrics.json').read_text())
    keys = ['input_sha256_content', 'record_sha256_content', 'config_sha256']
    same_hashes = all(a[k] == b[k] for k in keys)
    metric_diffs = {
        k: {'a': a['metrics'][k], 'b': b['metrics'][k]}
        for k in a['metrics'] if a['metrics'][k] != b['metrics'].get(k)
    }
    report = {
        'hashes_equal': {k: a[k] == b[k] for k in keys},
        'metric_differences': metric_diffs,
        'identical': same_hashes and not metric_diffs,
    }
    print(json.dumps(report, indent=2))
    return 0 if report['identical'] else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('prepare', help='convert bundled raw sample to normalized events')

    r = sub.add_parser('run', help='run one backtest')
    r.add_argument('--out', required=True, help='output directory')
    r.add_argument('--label', default=None)
    r.add_argument('--file', default=None,
                   help='raw file from config data.raw_files (default: first entry)')
    r.add_argument('--max-minutes', type=float, default=None,
                   help='optional: truncate input to first N minutes (smoke tests)')

    c = sub.add_parser('compare', help='compare two run directories')
    c.add_argument('a')
    c.add_argument('b')

    args = p.parse_args(argv)
    return {'prepare': cmd_prepare, 'run': cmd_run, 'compare': cmd_compare}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
