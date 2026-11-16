"""Central configuration. Every stage reads from here so a rerun is deterministic.

The QuestDB tables are still being appended to by a live collector, so every run uses a
FIXED window [WINDOW_START, WINDOW_END). Defaults reproduce the frozen 2026-11-15 study
(13:20 to 20:40 UTC). The daily entrypoint (run_daily.sh) sets MMR_WINDOW_END to the
invocation time (floored to the minute), so its window is cumulative from 13:20.

Environment overrides (all optional):
  MMR_OUT          root for data/ and results/ (default: project folder)
  MMR_DATA         data folder (default: $MMR_OUT/data)
  MMR_RESULTS      results folder (default: $MMR_OUT/results)
  MMR_WINDOW_END   window end, e.g. 2026-11-16T00:00:00.000000Z
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
# Output root (data/ and results/ live under it). Override with MMR_OUT for a clean rerun.
OUT = Path(os.environ.get("MMR_OUT", str(PROJECT))).resolve()
DATA = Path(os.environ.get("MMR_DATA", str(OUT / "data"))).resolve()
RAW = DATA / "raw"
CLEAN = DATA / "clean"
FEAT = DATA / "features"
RESULTS = Path(os.environ.get("MMR_RESULTS", str(OUT / "results"))).resolve()

QDB_URL = os.environ.get("MMR_QDB_URL", "http://127.0.0.1:9000/exec")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
# Study window [start, end) in UTC. Data before 13:18:40 does not exist; the first ~80 s are
# dropped as warm-up. The start is fixed for every run (cumulative window).
WINDOW_START = "2026-11-15T13:20:00.000000Z"
WINDOW_END = os.environ.get("MMR_WINDOW_END", "2026-11-15T20:40:00.000000Z")
CHUNK_MINUTES = 15

# Binance spot tick sizes (verified from data in the audit: min non-zero price increment).
TICK = {"BTCUSDT": 0.01, "ETHUSDT": 0.01, "SOLUSDT": 0.01}

# Walk-forward: hourly test folds; each fold is fitted on all data strictly before it.
WF_FIRST_TEST_HOUR = "2026-11-15T16:00:00Z"   # first 2h40m = initial training block
WF_FOLD_MINUTES = 60
