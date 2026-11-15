#!/usr/bin/env bash
# End-to-end pipeline. Usage:  ./run_all.sh            (writes ../data and ../results)
#                              MMR_OUT=/path ./run_all.sh  (writes /path/data and /path/results)
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-../.venv/bin/python}
$PY test_sim.py
$PY extract.py
$PY audit_clean.py
$PY features.py
$PY hypotheses.py
$PY robustness.py
$PY summaries.py
