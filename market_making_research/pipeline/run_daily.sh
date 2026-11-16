#!/usr/bin/env bash
# Daily entrypoint: full pipeline on the CUMULATIVE window
#   [2026-11-15 13:20:00 UTC, invocation time floored to the minute)
# written to results/runs/<YYYY-MM-DD>/ (UTC date of the window end). Never overwrites a run.
#
# Safe unattended: SELECT-only against QuestDB on localhost, no other network, no git, writes
# only inside the project folder. Exits non-zero on any failure; a failed run leaves only
# results/runs/.incomplete-<run_id>-<pid>/ (never a half-written dated folder or index row).
#
# Optional environment (for manual or verification runs; leave unset for the daily job):
#   MMR_WINDOW_END   fixed window end, e.g. 2026-11-16T00:00:00.000000Z (default: now, floored to minute)
#   MMR_RUN_ID       run folder name (default: UTC date of the window end, YYYY-MM-DD)
#   MMR_RUNS_DIR     runs root inside the project (default: results/runs)
#   MMR_KEEP_DATA=1  keep the intermediate parquet under data/runs/ (default: deleted after success)
#   MMR_SETTLE_S     seconds to wait after the window end before extracting (default 60)
set -euo pipefail
umask 022

PROJECT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT/pipeline"
PY="$PROJECT/.venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: missing $PY (see README: Setup)" >&2; exit 2; }

# localhost-only QuestDB
QDB="${MMR_QDB_URL:-http://127.0.0.1:9000/exec}"
case "$QDB" in http://127.0.0.1:*|http://localhost:*) ;; *) echo "ERROR: MMR_QDB_URL must be localhost" >&2; exit 2;; esac
export MMR_QDB_URL="$QDB"

INVOKED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
END="${MMR_WINDOW_END:-$(date -u -d "${INVOKED}" +%Y-%m-%dT%H:%M:00.000000Z)}"
[[ "$END" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$ ]] || { echo "ERROR: bad window end $END" >&2; exit 2; }
RUN_ID="${MMR_RUN_ID:-${END:0:10}}"
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "ERROR: bad run id $RUN_ID" >&2; exit 2; }

RUNS_DIR="$(realpath -m "${MMR_RUNS_DIR:-$PROJECT/results/runs}")"
DATA_ROOT="$(realpath -m "$PROJECT/data/runs")"
case "$RUNS_DIR/" in "$PROJECT"/*) ;; *) echo "ERROR: runs dir must be inside $PROJECT" >&2; exit 2;; esac
mkdir -p "$RUNS_DIR" "$DATA_ROOT"

# one run at a time
exec 9>"$RUNS_DIR/.lock"
flock -n 9 || { echo "ERROR: another run_daily.sh is running" >&2; exit 4; }

FINAL="$RUNS_DIR/$RUN_ID"
if [ -e "$FINAL" ]; then echo "ERROR: $FINAL exists; runs are never overwritten" >&2; exit 3; fi
STAGE="$RUNS_DIR/.incomplete-$RUN_ID-$$"
DATA_DIR="$DATA_ROOT/$RUN_ID-$$"
mkdir "$STAGE" "$DATA_DIR"
LOG="$STAGE/run.log"

# wait until the window end has settled (late rows from the live collector)
SETTLE="${MMR_SETTLE_S:-60}"
END_S=$(date -u -d "${END:0:19}Z" +%s)
NOW_S=$(date -u +%s)
if [ $((END_S + SETTLE)) -gt "$NOW_S" ]; then sleep $((END_S + SETTLE - NOW_S)); fi

export MMR_WINDOW_END="$END" MMR_RESULTS="$STAGE" MMR_DATA="$DATA_DIR" MMR_OUT="$STAGE"
export NUMBA_CACHE_DIR="$PROJECT/pipeline/__pycache__"
{
  echo "run_id=$RUN_ID invoked_at=$INVOKED window=[2026-11-15T13:20:00.000000Z, $END) stage=$STAGE data=$DATA_DIR"
  for s in test_sim.py test_factors.py extract.py audit_clean.py features.py hypotheses.py ladder.py robustness.py; do
    echo "== $s $(date -u +%H:%M:%S)"
    timeout 4h "$PY" "$s"
  done
  cp "$DATA_DIR/raw/manifest.csv" "$STAGE/audit/extract_manifest.csv"
  echo "== summaries.py $(date -u +%H:%M:%S)"
  timeout 1h "$PY" summaries.py
  "$PY" run_index.py meta "$STAGE" "$INVOKED" "$RUN_ID"
} >"$LOG" 2>&1 || { echo "ERROR: pipeline failed; see $LOG" >&2; exit 1; }

mv -T "$STAGE" "$FINAL"
"$PY" run_index.py index "$FINAL" "$RUNS_DIR/index.csv" >>"$FINAL/run.log" 2>&1 || { echo "ERROR: index update failed; see $FINAL/run.log" >&2; exit 1; }

if [ "${MMR_KEEP_DATA:-0}" != "1" ]; then
  case "$DATA_DIR" in "$DATA_ROOT"/?*) rm -rf -- "$DATA_DIR" ;; *) echo "ERROR: refusing to delete $DATA_DIR" >&2; exit 1;; esac
fi
echo "OK run_id=$RUN_ID window_end=$END results=$FINAL"
