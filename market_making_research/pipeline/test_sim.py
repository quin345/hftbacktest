"""Synthetic unit tests of the fill simulator's explicit assumptions. Run: python test_sim.py"""
import numpy as np

from sim import OPTIMISTIC, QUEUE_CONS, QUEUE_PROP, simulate

MS = 1_000_000


def book(n, bid=100.0, ask=100.01, bq0=5.0, aq0=5.0):
    bp = np.array([[bid - 0.01 * k for k in range(10)]] * n)
    ap = np.array([[ask + 0.01 * k for k in range(10)]] * n)
    bq = np.full((n, 10), 1.0); bq[:, 0] = bq0
    aq = np.full((n, 10), 1.0); aq[:, 0] = aq0
    return bp, bq, ap, aq


def run(tb, bp, bq, ap, aq, tt, tpx, tq, tside, model, lat=10, wb=None, wa=None, oq=1.0):
    n = len(tb)
    wb = np.ones(n, np.bool_) if wb is None else wb
    wa = np.zeros(n, np.bool_) if wa is None else wa
    return simulate(np.array(tb, np.int64), bp, bq, ap, aq, np.zeros(n, np.int32),
                    np.array(tt, np.int64), np.array(tpx, float), np.array(tq, float), np.array(tside, np.int8),
                    wb, wa, lat * MS, model, oq, 10.0, 0.01)


def test_queue_back_of_line():
    bp, bq, ap, aq = book(3)
    tb = [0, 100 * MS, 200 * MS]
    # order live at 10 ms with 5 ahead; sells of 3 then 3 at the bid -> 1 unit excess fills our 1
    f = run(tb, bp, bq, ap, aq, [50 * MS, 60 * MS], [100.0, 100.0], [3.0, 3.0], [-1, -1], QUEUE_CONS)
    assert len(f[0]) == 1 and abs(f[3][0] - 1.0) < 1e-12 and f[0][0] == 60 * MS, f
    f = run(tb, bp, bq, ap, aq, [50 * MS], [100.0], [3.0], [-1], QUEUE_CONS)
    assert len(f[0]) == 0


def test_optimistic_fills_on_touch():
    bp, bq, ap, aq = book(2)
    f = run([0, 100 * MS], bp, bq, ap, aq, [50 * MS], [100.0], [0.001], [-1], OPTIMISTIC)
    assert len(f[0]) == 1 and f[3][0] == 1.0


def test_latency_blocks_early_trade():
    bp, bq, ap, aq = book(2)
    f = run([0, 100 * MS], bp, bq, ap, aq, [5 * MS], [99.9], [10.0], [-1], OPTIMISTIC, lat=10)
    assert len(f[0]) == 0  # trade before the order reached the exchange


def test_trade_through_fills():
    bp, bq, ap, aq = book(2)
    f = run([0, 100 * MS], bp, bq, ap, aq, [50 * MS], [99.98], [0.1], [-1], QUEUE_CONS)
    assert len(f[0]) == 1 and f[3][0] == 1.0


def test_book_cross_fills():
    bp, bq, ap, aq = book(2)
    bp[1] -= 0.05; ap[1] -= 0.05  # ask moved to 99.96 < our bid 100.00
    f = run([0, 100 * MS], bp, bq, ap, aq, [], [], [], [], QUEUE_CONS)
    assert len(f[0]) == 1 and f[2][0] == 100.0


def test_level_shrink_caps_queue_and_prop_cancels():
    bp, bq, ap, aq = book(3)
    bq[1, 0] = 1.0  # level shrinks 5 -> 1 with no trades
    tb = [0, 100 * MS, 200 * MS]
    f = run(tb, bp, bq, ap, aq, [150 * MS], [100.0], [1.5], [-1], QUEUE_CONS)
    assert len(f[0]) == 1 and abs(f[3][0] - 0.5) < 1e-12  # queue capped at 1 -> 0.5 excess
    bq[1, 0] = 3.0  # 2 cancelled of 5; prop model removes 2*(5/5)=2 ahead -> queue 3 (cons: 3 too)
    f = run(tb, bp, bq, ap, aq, [150 * MS], [100.0], [3.5], [-1], QUEUE_PROP)
    assert len(f[0]) == 1 and abs(f[3][0] - 0.5) < 1e-12


def test_post_only_reject():
    bp, bq, ap, aq = book(3)
    # at decision the bid is 100.00; by arrival (snapshot 1 at 5 ms) the ask is 99.99 -> reject
    bp[1:] -= 0.02; ap[1:] -= 0.02
    f = run([0, 5 * MS, 50 * MS], bp, bq, ap, aq, [], [], [], [], QUEUE_CONS, lat=10)
    assert f[6] >= 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
