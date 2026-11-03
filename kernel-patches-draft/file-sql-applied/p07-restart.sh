#!/usr/bin/env bash
# Restart on the freshly built binary, PROVE the listener is it, then run the patch-07 probe and
# the graded AI suites. Every stage prints the binary md5 so a stale process can never masquerade
# as a measurement (that is exactly how the first A/B of this session was voided).
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
PY=/opt/conda/bin/python3
INSDIR=$D/ins14
BIN=$S/build_release/src/observer/seekdb
say() { echo "[$(date +%H:%M:%S)] $*"; }

cd "$S" || exit 1
say "target binary md5=$(md5sum build_release/src/observer/seekdb | cut -c1-32)"
OLD_PID=$(pgrep -f '[o]bserver/seekdb' | head -1 || true)
if [ -n "${OLD_PID:-}" ]; then
  say "stopping pid=$OLD_PID (SIGTERM, up to 120s)"
  kill -15 "$OLD_PID" 2>/dev/null
  for i in $(seq 1 60); do
    kill -0 "$OLD_PID" 2>/dev/null || break
    sleep 2
  done
  if kill -0 "$OLD_PID" 2>/dev/null; then
    say "still alive, SIGKILL"; kill -9 "$OLD_PID" 2>/dev/null; sleep 5
  fi
  kill -0 "$OLD_PID" 2>/dev/null && { say "FATAL: $OLD_PID refuses to die"; exit 1; }
  say "old pid confirmed dead"
fi
setsid nohup "$BIN" --nodaemon --port 2881 --base-dir "$INSDIR" --data-dir "$INSDIR/store" \
  > "$INSDIR/seekdb.log" 2>&1 < /dev/null &
NEW_PID=$!
say "launched pid=$NEW_PID md5=$(md5sum build_release/src/observer/seekdb | cut -c1-32)"
for i in $(seq 1 120); do
  $PY -c "
import socket,sys
s=socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(('127.0.0.1',2881))==0 else 1)" >/dev/null 2>&1 && break
  kill -0 "$NEW_PID" 2>/dev/null || { say "FATAL: new pid died (see seekdb.log)"; tail -20 "$INSDIR/seekdb.log"; exit 1; }
  sleep 5
done
LISTEN_PID=$(ss -lntp 2>/dev/null | grep ':2881' | grep -aoE 'pid=[0-9]+' | head -1 | sed 's/pid=//')
say "listener pid on 2881 = ${LISTEN_PID:-none} (expected $NEW_PID)"
[ "${LISTEN_PID:-}" = "$NEW_PID" ] || { say "FATAL: port held by another pid"; exit 1; }
$PY -c "
import pymysql
c=pymysql.connect(host='127.0.0.1',port=2881,user='root',database='test',autocommit=True).cursor()
c.execute('select version()'); print('SQL=',c.fetchone()[0])"
say "mock :8000 listeners=$(ss -lntp 2>/dev/null | grep -c ':8000')"
if [ "${MOCK_ALIVE:-1}" = "1" ] && [ "$(ss -lntp 2>/dev/null | grep -c ':8000')" = "0" ]; then
  setsid nohup $PY "$K/mock_ai_service.py" > "$D/mock.log" 2>&1 < /dev/null &
  say "restarted mock pid=$!"; sleep 3
fi

say "-- p07 probe (adversarial mock: reversed answers, short answers, dead endpoint)"
$PY "$K/p07-probe.py" 2>&1 | tail -40
say "-- graded suites against their recorded .result (suite filter: '${1:-all}')"
bash "$K/oracle_all.sh" "${1:-}" 2>&1 | tail -70
say "RESTART_VERIFY_DONE"
