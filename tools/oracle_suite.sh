#!/usr/bin/env bash
# Oracle-diff every .test in one suite: bash oracle_suite.sh fts_index
set -uo pipefail
D=/root/rivermind-data
PY=/opt/conda/bin/python3
S=$D/seekdb/tools/deploy/mysql_test/test_suite
SUITE="${1:?suite name required}"
for t in "$S/$SUITE"/t/*.test; do
  n=$(basename "$t" .test)
  r="$S/$SUITE/r/mysql/$n.result"
  if [ ! -f "$r" ]; then
    echo "[SKIP] $n has no recorded result"
    continue
  fi
  timeout 180 "$PY" "$D/ktools/mysqltest-oracle.py" --host 127.0.0.1 --port 2881 \
    --test "$t" --result "$r" --out "$D/oracle-$SUITE-$n.actual" > "$D/oracle-$SUITE-$n.diff" 2>&1
  rc=$?
  [ "$rc" = 124 ] && echo "rc=124 TIMEOUT $n" && continue
  line=$(grep -a '\[ORACLE\]' "$D/oracle-$SUITE-$n.diff" | head -1)
  echo "rc=$rc $line"
done
echo SUITE_DONE
