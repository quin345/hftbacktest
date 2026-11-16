"""Synthetic unit tests of the factor-ladder code (factors.py, ladder.py helpers). Run: python test_factors.py"""
import numpy as np

from factors import GRIDS, LADDER, applicable, combined_sides, depth_change, factor_sides, trade_flow
from ladder import need_folds

S = 1_000_000_000


def frame(**cols):
    n = len(next(iter(cols.values())))
    base = dict(imb1=np.zeros(n), flow_1s=np.zeros(n), rv_10s=np.zeros(n), spread_ticks=np.ones(n),
                dchg_b_1s=np.zeros(n), dchg_a_1s=np.zeros(n), btc_ret_1s=np.zeros(n))
    base.update({k: np.asarray(v, float) for k, v in cols.items()})
    return base


def test_grids_start_with_off_and_ladder_order():
    assert LADDER == ["imb1", "flow_1s", "rv_10s", "spread", "depth_chg", "btc_ll"]
    for k, g in GRIDS.items():
        assert g[0] == np.inf, k  # "off" first -> ties resolve to the less restrictive choice


def test_off_allows_everything():
    f = frame(imb1=[-1, 0, 1], flow_1s=[-1, 0, 1], rv_10s=[99, 0, 99])
    for fac in LADDER:
        b, a = factor_sides(fac, np.inf, f)
        assert b.all() and a.all(), fac


def test_directional_filters_pull_the_threatened_side():
    f = frame(imb1=[-0.9, 0.0, 0.9], flow_1s=[-0.9, 0.0, 0.9], btc_ret_1s=[-2.0, 0.0, 2.0])
    for fac in ("imb1", "flow_1s", "btc_ll"):
        th = 0.5 if fac != "btc_ll" else 1.0
        b, a = factor_sides(fac, th, f)
        # negative signal (sell pressure / thin bid / BTC down) -> pull the bid only
        assert list(b) == [False, True, True] and list(a) == [True, True, False], fac


def test_regime_filters_pull_both_sides():
    f = frame(rv_10s=[1.0, 6.0], spread_ticks=[1, 4])
    b, a = factor_sides("rv_10s", 5.0, f)
    assert list(b) == [True, False] and list(a) == [True, False]
    b, a = factor_sides("spread", 2.0, f)
    assert list(b) == [True, False] and list(a) == [True, False]


def test_depth_change_filter_is_per_side():
    f = frame(dchg_b_1s=[-0.6, 0.0], dchg_a_1s=[0.0, -0.6])
    b, a = factor_sides("depth_chg", 0.5, f)
    assert list(b) == [False, True] and list(a) == [True, False]


def test_nan_is_neutral():
    f = frame(btc_ret_1s=[np.nan, 5.0])
    b, a = factor_sides("btc_ll", 1.0, f)
    assert list(b) == [True, True] and list(a) == [True, False]


def test_combined_is_logical_and():
    f = frame(imb1=[-0.9, 0.0, 0.0], flow_1s=[0.0, 0.9, 0.0], rv_10s=[0.0, 0.0, 9.0])
    b, a = combined_sides({"imb1": 0.5, "flow_1s": 0.5, "rv_10s": 5.0}, f)
    assert list(b) == [False, True, False] and list(a) == [True, False, False]
    b, a = combined_sides({}, f)
    assert b.all() and a.all()


def test_depth_change_values_and_segments():
    t = np.array([0, S // 2, S, 2 * S, 3 * S], np.int64)
    q = np.array([10.0, 8.0, 5.0, 10.0, 20.0])
    seg = np.array([0, 0, 0, 0, 1], np.int32)
    d = depth_change(t, q, seg, S)
    # t=0 and t=0.5s have no value 1 s earlier -> 0; t=1s vs t=0: 5/10-1; t=2s vs t=1s: 10/5-1;
    # t=3s is in a new segment (gap) -> 0 (history unknown)
    assert np.allclose(d, [0.0, 0.0, -0.5, 1.0, 0.0]), d


def test_trade_flow_window_is_left_open():
    tt = np.array([0, S // 2, S], np.int64)
    side = np.array([1.0, -1.0, 1.0])
    qty = np.array([1.0, 3.0, 2.0])
    fl = trade_flow(tt, side, qty, np.array([S, 2 * S + 1, 5 * S], np.int64), S)
    # window (0, 1s]: -3 + 2 over 5 -> -0.2 (the trade at exactly t-1s is excluded); (1s+1, 2s+1]: none -> 0
    assert np.allclose(fl, [-0.2, 0.0, 0.0]), fl


def test_btc_leadlag_not_applicable_to_btc_and_fold_rule():
    assert not applicable("btc_ll", "BTCUSDT") and applicable("btc_ll", "ETHUSDT") and applicable("imb1", "BTCUSDT")
    assert need_folds(5) == 4 and need_folds(6) == 5 and need_folds(10) == 8 and need_folds(1) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
