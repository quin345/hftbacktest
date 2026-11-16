"""Evidence script for QA findings A (concatenation) and B (DEPTH_CLEAR grouping).

Run from project root: .venv/bin/python research_baseline/review_evidence/check_concat_and_clears.py
Read-only with respect to upstream files; writes nothing except stdout.
"""
import sys
from pathlib import Path
import numpy as np
from hftbacktest.data.utils.binancefutures import convert
from hftbacktest.data.validation import correct_event_order, validate_event_order
from hftbacktest.types import DEPTH_CLEAR_EVENT, BUY_EVENT, SELL_EVENT, EXCH_EVENT, LOCAL_EVENT

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research_baseline'))
import grid_baseline as gb  # noqa: E402

MASK = gb.EVENT_TYPE_MASK
files = [ROOT / 'examples/usdm/btcusdt_20240808.gz', ROOT / 'examples/usdm/btcusdt_20240809.gz']
cfg = gb.load_config()

def ts(ns):
    import datetime
    return datetime.datetime.fromtimestamp(ns / 1e9, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

raw_events, trimmed = [], []
print('== per-file raw conversion (untrimmed) and clears ==')
for f in files:
    ev = convert(str(f), output_filename=None, opt='', base_latency=0, combined_stream=True, buffer_size=20_000_000)
    raw_events.append(ev)
    tb = ev['ev'] & MASK
    clears = np.flatnonzero(tb == DEPTH_CLEAR_EVENT)
    print(f'{f.name}: converted={len(ev)} clear_rows={len(clears)}')
    for i in clears:
        side = 'BUY' if ev['ev'][i] & BUY_EVENT else ('SELL' if ev['ev'][i] & SELL_EVENT else '?')
        print(f'  idx={i} side={side} exch_ts={ts(ev["exch_ts"][i])} local_ts={ts(ev["local_ts"][i])} px={ev["px"][i]}')
    # group snapshot blocks: clear_row count and snapshot row counts following each clear
    snap_ct = []
    for i in clears:
        j = i + 1
        n = 0
        while j < len(ev) and (ev['ev'][j] & MASK) == 4:
            n += 1; j += 1
        snap_ct.append(n)
    print(f'  snapshot rows following each clear: {snap_ct}')
    t = gb.np.flatnonzero((ev['ev'] & MASK) == DEPTH_CLEAR_EVENT)
    first = int(t[0]) if len(t) else 0
    trimmed.append(ev[first:])

print()
print('== trimmed files (what prepare saves) ==')
for name, a in zip(['8/08', '8/09'], trimmed):
    print(f'{name}: rows={len(a)} first={ts(a["local_ts"][0])} last={ts(a["local_ts"][-1])} span_s={(a["local_ts"][-1]-a["local_ts"][0])/1e9:.2f}')
gap = (trimmed[1]['local_ts'][0] - trimmed[0]['local_ts'][-1]) / 1e9
print(f'gap between trimmed 8/08 last local_ts and trimmed 8/09 first local_ts: {gap:.2f} s')

print()
print('== A: validation of concatenation ==')
cat = np.concatenate(trimmed)
for label, arr in [('plain concatenate(trimmed)', cat),
                   ('concatenate(trimmed) + correct_event_order', correct_event_order(
                       cat,
                       np.argsort(cat['exch_ts'], kind='mergesort'),
                       np.argsort(cat['local_ts'], kind='mergesort')))]:
    try:
        validate_event_order(arr); print(f'{label}: validate_event_order OK')
    except Exception as e:
        print(f'{label}: validate_event_order FAILED: {type(e).__name__}: {e}')
print('plain concatenate(untrimmed raw convert outputs):', end=' ')
try:
    validate_event_order(np.concatenate(raw_events)); print('OK')
except Exception as e:
    print(f'FAILED: {type(e).__name__}: {e}')
print('local_ts overlap in untrimmed converts: 8/08 last', ts(raw_events[0]['local_ts'][-1]), '| 8/09 first', ts(raw_events[1]['local_ts'][0]))
