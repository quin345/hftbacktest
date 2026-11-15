"""Central configuration. Every stage reads from here so a rerun is deterministic.

The QuestDB tables are still being appended to by a live collector, so the study
window is FROZEN to fixed UTC bounds. Changing WINDOW_* changes every number.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# Output root (data/ and results/ live under it). Override with MMR_OUT for a clean rerun.
OUT = Path(os.environ.get("MMR_OUT", str(PROJECT))).resolve()
DATA = OUT / "data"
RAW = DATA / "raw"
CLEAN = DATA / "clean"
FEAT = DATA / "features"
RESULTS = OUT / "results"

QDB_URL = os.environ.get("MMR_QDB_URL", "http://127.0.0.1:9000/exec")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
# Frozen study window [start, end) in UTC. Data before 13:18:40 does not exist;
# the first ~80 s are dropped as warm-up, and the end is fixed so live appends do not leak in.
WINDOW_START = "2026-11-15T13:20:00.000000Z"
WINDOW_END = "2026-11-15T20:40:00.000000Z"
CHUNK_MINUTES = 15

# Binance spot tick sizes (verified from data in the audit: min non-zero price increment).
TICK = {"BTCUSDT": 0.01, "ETHUSDT": 0.01, "SOLUSDT": 0.01}

# Walk-forward: hourly test folds; each fold is fitted on all data strictly before it.
WF_FIRST_TEST_HOUR = "2026-11-15T16:00:00Z"   # first 2h40m = initial training block
WF_FOLD_MINUTES = 60
