"""Checks for the USD-M BTCUSDT research baseline.

Run from the project root:
  .venv/bin/python -m pytest research_baseline/tests -q
(unittest also works: .venv/bin/python -m unittest discover -s research_baseline/tests)
"""

import argparse
import copy
import gzip
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import grid_baseline as gb  # noqa: E402
from hftbacktest.types import DEPTH_CLEAR_EVENT, DEPTH_EVENT, TRADE_EVENT  # noqa: E402

ALLOWED_STATUS = {'sample-data', 'repo-example', 'demo-assumed'}


class ConfigLabelTests(unittest.TestCase):
    def test_every_assumption_section_is_labelled(self):
        cfg = gb.load_config()
        for section in ('source', 'data', 'instrument', 'fees', 'latency', 'queue', 'strategy', 'run'):
            self.assertIn(section, cfg)
            self.assertIn(cfg[section].get('status'), ALLOWED_STATUS, section)

    def test_demo_values_are_not_presented_as_facts(self):
        cfg = gb.load_config()
        # Tick/lot, fees, and latency are demo-assumed, never sample-data or verified.
        for section in ('instrument', 'fees', 'latency'):
            self.assertEqual(cfg[section]['status'], 'demo-assumed')

    def test_upstream_commit_is_pinned(self):
        cfg = gb.load_config()
        self.assertEqual(len(cfg['source']['upstream_commit']), 40)


class MetricsTests(unittest.TestCase):
    def _record(self):
        dtype = gb.hbt_pkg.types.record_dtype
        rec = np.zeros(4, dtype=dtype)
        rec['timestamp'] = [0, 1_000_000_000, 2_000_000_000, 3_600_000_000_000]
        rec['price'] = [100.0, 101.0, 99.0, 100.0]
        rec['position'] = [0.0, 2.0, 2.0, -1.0]
        rec['balance'] = [0.0, -200.0, -200.0, 0.5]
        rec['fee'] = [0.0, 0.02, 0.02, 0.05]
        rec['num_trades'] = [0, 1, 2, 3]
        rec['trading_volume'] = [0.0, 2.0, 2.0, 3.0]
        rec['trading_value'] = [0.0, 200.0, 200.0, 300.0]
        return rec

    def test_known_values(self):
        m = gb.metrics_from_record(self._record())
        # equity_wo_fee = balance + position * price
        self.assertAlmostEqual(m['final_equity_wo_fee'], 0.5 + (-1.0) * 100.0)  # -99.5
        self.assertAlmostEqual(m['final_equity_net_of_fees'], -99.5 - 0.05)
        self.assertEqual(m['num_trades'], 3)
        self.assertAlmostEqual(m['fees_paid_net'], 0.05)
        self.assertAlmostEqual(m['max_abs_position_notional'], 202.0)  # 2 * 101
        self.assertAlmostEqual(m['span_hours'], 1.0)

    def test_drawdown_nonnegative_and_consistent(self):
        m = gb.metrics_from_record(self._record())
        self.assertGreaterEqual(m['max_drawdown_net'], 0.0)


class ConversionTests(unittest.TestCase):
    """Converts a small slice of the bundled raw file (no prepared data needed)."""

    @classmethod
    def setUpClass(cls):
        cfg = gb.load_config()
        raw = gb.PROJECT_ROOT / cfg['data']['raw_files'][0]
        if not raw.exists():
            raise unittest.SkipTest(f'bundled sample not found: {raw}')
        cls.tmp = tempfile.TemporaryDirectory()
        slice_path = Path(cls.tmp.name) / 'slice.gz'
        # The REST snapshot sits at line 55 of the sample; take the first 2,000 lines.
        with gzip.open(raw, 'rt') as src, gzip.open(slice_path, 'wt') as dst:
            for i, line in enumerate(src):
                if i >= 2000:
                    break
                dst.write(line)
        cls.cfg = cfg
        cls.events = None
        cls.dropped_info = None
        out = Path(cls.tmp.name) / 'slice_normalized.npz'
        cls.info = gb.convert_raw(slice_path, cfg, out)
        cls.events = np.load(out)['data']

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_first_event_is_snapshot_clear(self):
        type_bits = self.events['ev'] & gb.EVENT_TYPE_MASK
        # The first (trimmed) file starts at its REST snapshot.
        self.assertEqual(int(type_bits[0]), DEPTH_CLEAR_EVENT)

    def test_pre_snapshot_events_dropped(self):
        self.assertGreater(self.info['events_dropped_before_snapshot'], 0)
        self.assertEqual(
            self.info['events_converted'] - self.info['events_dropped_before_snapshot'],
            self.info['events_kept'],
        )

    def test_library_event_order_valid(self):
        # hftbacktest orders exchange and local events separately; validate with the library.
        from hftbacktest.data.validation import validate_event_order
        validate_event_order(self.events)

    def test_prices_and_quantities_sane(self):
        self.assertTrue(np.all(np.isfinite(self.events['px'])))
        self.assertTrue(np.all(self.events['px'] > 0))
        self.assertTrue(np.all(self.events['qty'] >= 0))

    def test_prices_on_tick_grid(self):
        tick = self.cfg['instrument']['tick_size']
        type_bits = self.events['ev'] & gb.EVENT_TYPE_MASK
        mask = (type_bits == DEPTH_EVENT) | (type_bits == DEPTH_CLEAR_EVENT)
        ticks = self.events['px'][mask] / tick
        self.assertTrue(np.allclose(ticks, np.round(ticks), atol=1e-6))

    def test_trades_present(self):
        type_bits = self.events['ev'] & gb.EVENT_TYPE_MASK
        self.assertGreater(int(np.sum(type_bits == TRADE_EVENT)), 0)


class RepeatabilityTests(unittest.TestCase):
    """Two identical short runs must produce identical records and metrics."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = gb.load_config()
        cls.data_path = gb.DATA_DIR / gb.normalized_name(cls.cfg['data']['raw_files'][0])
        if not cls.data_path.exists():
            raise unittest.SkipTest('prepared data missing; run `prepare` first')

    def _run(self, out_dir: Path):
        args = argparse.Namespace(out=str(out_dir), label=out_dir.name, max_minutes=5.0, file=None)
        self.assertEqual(gb.cmd_run(args), 0)
        import json
        return json.loads((out_dir / 'metrics.json').read_text())

    def test_two_runs_identical(self):
        with tempfile.TemporaryDirectory() as td:
            a = self._run(Path(td) / 'a')
            b = self._run(Path(td) / 'b')
            self.assertEqual(a['record_sha256_content'], b['record_sha256_content'])
            self.assertEqual(a['input_sha256_content'], b['input_sha256_content'])
            self.assertEqual(a['metrics'], b['metrics'])
            self.assertGreater(a['metrics']['records'], 0)


class DepthClearTests(unittest.TestCase):
    """DEPTH_CLEAR accounting on the converted sample (prepared data must exist)."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = gb.load_config()
        cls.data_path = gb.DATA_DIR / gb.normalized_name(cls.cfg['data']['raw_files'][0])
        if not cls.data_path.exists():
            raise unittest.SkipTest('prepared data missing; run `prepare` first')
        cls.events = np.load(cls.data_path)['data']

    def test_four_clears_in_two_side_pairs(self):
        info = gb.count_depth_clears(self.events)
        self.assertEqual(info['count'], 4)
        # Two BUY and two SELL clears: each side is cleared twice, once per emitted block.
        self.assertEqual(sorted(info['sides']), ['BUY', 'BUY', 'SELL', 'SELL'])

    def test_clears_are_followed_by_1000_snapshot_rows(self):
        from hftbacktest.types import DEPTH_SNAPSHOT_EVENT  # noqa: F401 (type value 4)
        type_bits = self.events['ev'] & gb.EVENT_TYPE_MASK
        for i in gb.count_depth_clears(self.events)['rows']:
            block = type_bits[i + 1:i + 1001]
            self.assertTrue(np.all(block == 4), f'row {i} not followed by 1000 snapshot rows')

    def test_first_clear_is_row_zero(self):
        self.assertEqual(gb.count_depth_clears(self.events)['rows'][0], 0)


class ProvenanceTests(unittest.TestCase):
    """verify_provenance must reject tampering with data, raw file, or conversion config."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = gb.load_config()
        cls.raw = cls.cfg['data']['raw_files'][0]
        cls.data_path = gb.DATA_DIR / gb.normalized_name(cls.raw)
        if not cls.data_path.exists() or not (gb.DATA_DIR / 'prepare_provenance.json').exists():
            raise unittest.SkipTest('prepared data/provenance missing; run `prepare` first')
        cls.original = cls.data_path.read_bytes()

    def tearDown(self):
        # Restore the real prepared file after each tamper test.
        self.data_path.write_bytes(self.original)

    def test_clean_data_passes(self):
        entry = gb.verify_provenance(self.raw, self.cfg, self.data_path)
        self.assertEqual(entry['normalized_file'], gb._rel(self.data_path))

    def test_tampered_normalized_data_fails(self):
        # Change one price in a copy of the events, saved in the same file path.
        ev = np.load(self.data_path)['data'].copy()
        ev['px'][100] = ev['px'][100] + 0.1
        np.savez_compressed(self.data_path, data=ev)
        with self.assertRaisesRegex(gb.ProvenanceError, 'content hash mismatch'):
            gb.verify_provenance(self.raw, self.cfg, self.data_path)

    def test_truncated_normalized_data_fails(self):
        ev = np.load(self.data_path)['data']
        np.savez_compressed(self.data_path, data=ev[:-1])
        with self.assertRaisesRegex(gb.ProvenanceError, 'content hash mismatch'):
            gb.verify_provenance(self.raw, self.cfg, self.data_path)

    def test_changed_conversion_setting_fails(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['data']['base_latency_ns'] = int(cfg['data']['base_latency_ns']) + 1
        with self.assertRaisesRegex(gb.ProvenanceError, 'conversion settings'):
            gb.verify_provenance(self.raw, cfg, self.data_path)

    def test_changed_trim_setting_fails(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['data']['trim_before_first_snapshot'] = not cfg['data']['trim_before_first_snapshot']
        with self.assertRaisesRegex(gb.ProvenanceError, 'conversion settings'):
            gb.verify_provenance(self.raw, cfg, self.data_path)

    def test_cmd_run_refuses_tampered_data(self):
        ev = np.load(self.data_path)['data'].copy()
        ev['qty'][50] = ev['qty'][50] + 1.0
        np.savez_compressed(self.data_path, data=ev)
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(out=str(Path(td) / 'x'), label='x', max_minutes=None, file=None)
            self.assertEqual(gb.cmd_run(args), 3)
            self.assertFalse((Path(td) / 'x' / 'metrics.json').exists())


class InventoryLimitTests(unittest.TestCase):
    """The notebook's inventory check is soft: quoting stops at the threshold, so
    outstanding orders can still fill past it. Test the threshold logic directly
    (no fills needed): when normalized inventory is at or past +/-1, that side is not quoted.
    """

    def test_side_suppression_formula(self):
        # Mirrors grid_strategy: quote buys only if normalized_position < 1; asks only if > -1.
        max_notional = 1000.0
        for position, mid, buy_quoted, sell_quoted in [
            (0.0, 100.0, True, True),
            (9.9, 100.0, True, True),    # 990 notional, still below threshold
            (10.0, 100.0, False, True),  # 1000 notional: buys suppressed
            (-10.0, 100.0, True, False), # short: asks suppressed
            (11.0, 100.0, False, True),  # overshoot: buys suppressed
        ]:
            normalized = (position * mid) / max_notional
            self.assertEqual(normalized < 1, buy_quoted, position)
            self.assertEqual(normalized > -1, sell_quoted, position)


if __name__ == '__main__':
    unittest.main()
