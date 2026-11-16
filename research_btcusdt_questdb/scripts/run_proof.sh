#!/usr/bin/env bash
# One-shot bounded proof: start loopback QuestDB, capture N seconds, verify via SQL, stop QuestDB.
# Everything runs inside this process tree; QuestDB is stopped on exit (trap).
# Usage: scripts/run_proof.sh [seconds<=600]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
SECS="${1:-180}"
if [ "$SECS" -gt 600 ] 2>/dev/null || [ "$SECS" -lt 1 ] 2>/dev/null; then
  echo "seconds must be 1..600" >&2; exit 2
fi
mkdir -p data
PY="$HERE/../.venv/bin/python"
QDB_DISABLE_HUP_HANDLER=true ./scripts/questdb_ctl.sh start > data/questdb_console.log 2>&1 &
QPID=$!
cleanup() {
  ./scripts/questdb_ctl.sh stop > data/questdb_stop.log 2>&1 || true
  wait "$QPID" 2>/dev/null || true
}
trap cleanup EXIT
ready=0
for _ in $(seq 1 150); do
  if curl -sf -m 2 "http://127.0.0.1:9000/exec?query=select%201" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then echo "QUESTDB_NOT_READY"; exit 3; fi
echo "QUESTDB_READY"
if [ -n "${VERIFY_ONLY_SESSION:-}" ]; then
  CRC=0; SID="$VERIFY_ONLY_SESSION"
else
  "$PY" -m btc_l2.capture --seconds "$SECS" --out-dir data/capture > data/capture_run.log 2>&1
  CRC=$?
  # Session id is emitted explicitly by the capture on stderr (SESSION_ID=...).
  SID="$(sed -n 's/^SESSION_ID=//p' data/capture_run.log | tail -1)"
  if [ -z "$SID" ]; then echo "NO_SESSION_ID_EMITTED"; CRC=1; fi
fi
"$PY" scripts/verify_questdb.py --session "$SID" --out "data/verify_${SID}.json" > data/verify_run.log 2>&1
VRC=$?
echo "CAPTURE_RC=$CRC VERIFY_RC=$VRC SESSION=$SID"
