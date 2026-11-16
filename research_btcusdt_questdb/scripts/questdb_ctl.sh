#!/usr/bin/env bash
# Project-local QuestDB control. Uses the vendored, checksum-verified 10.2.0
# distribution (vendor/) and a project-local data root (data/questdb-root).
# Binds HTTP/ILP to 127.0.0.1 only; no global install, no system service.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QDB_HOME="$HERE/vendor/questdb-10.2.0-rt-linux-x86-64"
QDB_ROOT="$HERE/data/questdb-root"
cmd="${1:-status}"
case "$cmd" in
  start)
    mkdir -p "$QDB_ROOT/conf"
    # Install the loopback-only config only on first start; never overwrite local edits.
    if [ ! -f "$QDB_ROOT/conf/server.conf" ]; then
      cp "$HERE/questdb/server.loopback.conf" "$QDB_ROOT/conf/server.conf"
    fi
    "$QDB_HOME/bin/questdb.sh" start -d "$QDB_ROOT" ;;
  stop)   "$QDB_HOME/bin/questdb.sh" stop -d "$QDB_ROOT" ;;
  status) "$QDB_HOME/bin/questdb.sh" status -d "$QDB_ROOT" ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
