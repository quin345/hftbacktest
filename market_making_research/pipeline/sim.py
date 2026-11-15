"""Stage 4: event-driven passive-quote fill simulator (numba).

Events: depth10 snapshots (exchange clock, ~100 ms conflated) and individual trades, merged
in time order. On equal timestamps trades are processed before the snapshot (a snapshot is
the state AFTER events up to its time).

Strategy interface: two boolean arrays per snapshot, want_bid[i] / want_ask[i]. If true the
strategy wants one post-only order of `order_qty` JOINED at the best bid / best ask of that
snapshot. Decisions made at snapshot time t take effect at t + latency (new, replace or cancel).
At most one in-flight message per side: while a message is pending, new decisions for that
side are skipped. The resting order can still fill until a cancel/replace takes effect.
Post-only: an order that would cross at arrival is rejected.

Fill models (EXPLICIT ASSUMPTIONS; all are approximations of a queue we cannot observe):
  0 OPTIMISTIC : filled in full by any opposite-aggressor trade at or through our price
                 (no queue). Upper bound; strongly overstates fills and profit.
  1 QUEUE_CONS : join the BACK of the displayed queue at our price. Only trades at our price
                 advance the queue; cancellations are assumed to be behind us. The queue is
                 capped at the currently displayed level size (if the level shrinks below our
                 queue estimate, we must be closer to the front). Pessimistic.
  2 QUEUE_PROP : as 1, plus level-size decreases not explained by trades (cancellations) are
                 assumed to be spread uniformly, so a proportional share is removed from ahead
                 of us. Middle case (similar in spirit to probabilistic queue models).
In every model: a trade strictly through our price fills the remainder; a snapshot whose
opposite best price has moved to or through our price fills the remainder (trade-through
inferred from the book). Partial fills are allowed when the excess volume is below our size.
Market impact of our own orders is ignored (we do not remove liquidity from the replayed data).
"""
from __future__ import annotations

import numpy as np
from numba import njit

OPTIMISTIC, QUEUE_CONS, QUEUE_PROP = 0, 1, 2
BIG = 1e300


@njit(cache=True)
def _level_qty(prices, qtys, price, is_bid, half_tick):
    # displayed qty at `price` on one side; 0 if price is inside the visible range but empty
    # or better than the best; BIG if worse than the 10th visible level (unknown, assume large)
    for k in range(prices.shape[0]):
        if abs(prices[k] - price) < half_tick:
            return qtys[k]
    if is_bid:
        if price > prices[0]:
            return 0.0
        if price < prices[prices.shape[0] - 1]:
            return BIG
    else:
        if price < prices[0]:
            return 0.0
        if price > prices[prices.shape[0] - 1]:
            return BIG
    return 0.0


@njit(cache=True)
def simulate(tb, bp, bq, ap, aq, seg, tt, tpx, tqty, tside, want_bid, want_ask,
             latency_ns, model, order_qty, max_inv, tick):
    nb = tb.shape[0]
    nt = tt.shape[0]
    half = tick * 0.5
    cap = nb + nt + 16
    f_t = np.empty(cap, np.int64)
    f_side = np.empty(cap, np.int8)
    f_px = np.empty(cap)
    f_q = np.empty(cap)
    f_bi = np.empty(cap, np.int64)
    f_why = np.empty(cap, np.int8)  # 0 queue fill at our price, 1 trade through, 2 book crossed us, 3 optimistic touch
    nf = 0
    act = np.zeros(2, np.bool_)
    aprice = np.zeros(2)
    arem = np.zeros(2)
    aqueue = np.zeros(2)
    alevel = np.zeros(2)
    trat = np.zeros(2)
    pend = np.zeros(2, np.bool_)
    pend_on = np.zeros(2, np.bool_)
    pend_t = np.zeros(2, np.int64)
    pend_px = np.zeros(2)
    n_msgs = 0
    n_reject = 0
    inv = 0.0
    cur = -1
    i = 0
    j = 0
    INF = np.int64(9223372036854775807)
    while i < nb or j < nt:
        tbk = tb[i] if i < nb else INF
        ttr = tt[j] if j < nt else INF
        tnow = ttr if ttr <= tbk else tbk
        # ---- pending messages reaching the exchange
        if cur >= 0:
            for s in range(2):
                if pend[s] and pend_t[s] <= tnow:
                    pend[s] = False
                    if not pend_on[s]:
                        act[s] = False
                        continue
                    p = pend_px[s]
                    if s == 0:
                        crosses = p >= ap[cur, 0] - half
                        lq = _level_qty(bp[cur], bq[cur], p, True, half)
                    else:
                        crosses = p <= bp[cur, 0] + half
                        lq = _level_qty(ap[cur], aq[cur], p, False, half)
                    if crosses:
                        act[s] = False
                        n_reject += 1
                        continue
                    act[s] = True
                    aprice[s] = p
                    arem[s] = order_qty
                    aqueue[s] = 0.0 if model == OPTIMISTIC else lq
                    alevel[s] = lq
                    trat[s] = 0.0
        if ttr <= tbk:
            # ---- trade
            sd = tside[j]
            s = 0 if sd < 0 else 1          # seller-aggressor hits bids, buyer-aggressor lifts asks
            if act[s] and cur >= 0:
                p = tpx[j]
                q = tqty[j]
                fill = 0.0
                if s == 0:
                    through = p < aprice[s] - half
                else:
                    through = p > aprice[s] + half
                at = abs(p - aprice[s]) < half
                why = 1
                if through or (model == OPTIMISTIC and at):
                    fill = arem[s]
                    if not through:
                        why = 3
                elif at:
                    trat[s] += q
                    excess = q - aqueue[s]
                    if excess > 0:
                        fill = min(arem[s], excess)
                        aqueue[s] = 0.0
                    else:
                        aqueue[s] -= q
                    why = 0
                if fill > 0:
                    f_t[nf] = ttr
                    f_side[nf] = 1 if s == 0 else -1
                    f_px[nf] = aprice[s]
                    f_q[nf] = fill
                    f_bi[nf] = cur
                    f_why[nf] = why
                    nf += 1
                    inv += fill if s == 0 else -fill
                    arem[s] -= fill
                    if arem[s] <= 1e-12:
                        act[s] = False
            j += 1
            continue
        # ---- book snapshot
        if cur >= 0 and seg[i] != seg[cur]:
            act[0] = act[1] = False
            pend[0] = pend[1] = False
        cur = i
        for s in range(2):
            if not act[s]:
                continue
            if s == 0:
                crossed = aprice[s] >= ap[i, 0] - half
                lq = _level_qty(bp[i], bq[i], aprice[s], True, half)
            else:
                crossed = aprice[s] <= bp[i, 0] + half
                lq = _level_qty(ap[i], aq[i], aprice[s], False, half)
            if crossed:
                f_t[nf] = tbk
                f_side[nf] = 1 if s == 0 else -1
                f_px[nf] = aprice[s]
                f_q[nf] = arem[s]
                f_bi[nf] = i
                f_why[nf] = 2
                nf += 1
                inv += arem[s] if s == 0 else -arem[s]
                act[s] = False
                continue
            if model == QUEUE_PROP and alevel[s] < BIG and lq < BIG and alevel[s] > 0:
                canc = alevel[s] - lq - trat[s]
                if canc > 0:
                    aqueue[s] -= canc * aqueue[s] / alevel[s]
            if model != OPTIMISTIC and lq < aqueue[s]:
                aqueue[s] = lq
            if aqueue[s] < 0:
                aqueue[s] = 0.0
            alevel[s] = lq
            trat[s] = 0.0
        # ---- strategy decision at this snapshot
        teff = tbk + latency_ns
        for s in range(2):
            if s == 0:
                want = want_bid[i] and (inv + order_qty <= max_inv + 1e-12)
                target = bp[i, 0]
            else:
                want = want_ask[i] and (inv - order_qty >= -max_inv - 1e-12)
                target = ap[i, 0]
            if pend[s]:
                continue
            if want:
                if act[s] and abs(aprice[s] - target) < half:
                    continue
                pend[s] = True
                pend_on[s] = True
                pend_px[s] = target
                pend_t[s] = teff
                n_msgs += 1
            elif act[s]:
                pend[s] = True
                pend_on[s] = False
                pend_t[s] = teff
                n_msgs += 1
        i += 1
    return f_t[:nf], f_side[:nf], f_px[:nf], f_q[:nf], f_bi[:nf], n_msgs, n_reject, f_why[:nf]


def load_arrays(sym: str):
    """Exchange-clock book and trade arrays for one symbol (from clean + features)."""
    import pandas as pd

    from config import CLEAN, FEAT
    d = pd.read_parquet(CLEAN / "depth" / f"{sym}.parquet")
    f = pd.read_parquet(FEAT / f"book_{sym}.parquet")
    t = pd.read_parquet(CLEAN / "trade" / f"{sym}.parquet")
    lag = int(d["ts_ns"].iloc[0] - f["t_ex"].iloc[0])
    book = dict(
        tb=f["t_ex"].to_numpy(np.int64),
        bp=np.ascontiguousarray(d[[f"b_p{k}" for k in range(10)]].to_numpy()),
        bq=np.ascontiguousarray(d[[f"b_q{k}" for k in range(10)]].to_numpy()),
        ap=np.ascontiguousarray(d[[f"a_p{k}" for k in range(10)]].to_numpy()),
        aq=np.ascontiguousarray(d[[f"a_q{k}" for k in range(10)]].to_numpy()),
        seg=f["seg"].to_numpy(np.int32),
    )
    trades = dict(
        tt=t["ts_ns"].to_numpy(np.int64),
        tpx=t["price"].to_numpy(),
        tqty=t["qty"].to_numpy(),
        tside=np.where(t["buyer_is_maker"].to_numpy(), -1, 1).astype(np.int8),
    )
    return book, trades, f, lag
