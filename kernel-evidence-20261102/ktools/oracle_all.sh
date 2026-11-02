#!/usr/bin/env bash
# Replay every shipped mysqltest suite against its recorded .result and print the line-level diff.
#   bash oracle_all.sh [suite-glob]     e.g. `bash oracle_all.sh ai_function`
set -uo pipefail
D=/root/rivermind-data
PY=/opt/conda/bin/python3
S=$D/seekdb/tools/deploy/mysql_test/test_suite
FILTER="${1:-}"

for suite in ai_function file_sql ai_column; do
  [ -d "$S/$suite/t" ] || continue
  case "$suite" in *"$FILTER"*) ;; *) continue ;; esac
  for t in "$S/$suite"/t/*.test; do
    n=$(basename "$t" .test)
    r="$S/$suite/r/mysql/$n.result"
    if [ ! -f "$r" ]; then
      echo "[ORACLE] test=$n NO_RESULT_FILE"
      continue
    fi
    "$PY" "$D/ktools/mysqltest-oracle.py" --host 127.0.0.1 --port 2881 \
      --test "$t" --result "$r" --out "$D/oracle-$n.actual" \
      --var OBSERVER_DIR="$D/fsdata" > "$D/oracle-$n.diff" 2>&1
    rc=$?
    echo "===== $n rc=$rc"
    grep -a '\[ORACLE\]' "$D/oracle-$n.diff" | head -1
    grep -aE '^[+-]' "$D/oracle-$n.diff" | grep -av '^\(---\|+++\)' | head -20
  done
done
echo ORACLE_DONE
