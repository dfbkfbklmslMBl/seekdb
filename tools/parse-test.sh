#!/usr/bin/env bash
# Boot a genuinely fresh instance dir and report what the new grammar accepts.
set -uo pipefail
D=/root/rivermind-data
export INSDIR="$D/ins3"
rm -rf "$INSDIR"; mkdir -p "$INSDIR"
bash "$D/ktools/newbox.sh" boot > /tmp/boot3.log 2>&1; echo "boot rc=$?"
grep -aE 'launched|binary under test|listener pid|SQL=|FATAL|WARNING' "$D/bootstrap.log" | tail -5
/opt/conda/bin/python3 - <<'PY'
import pymysql
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test",
                    autocommit=True, connect_timeout=15).cursor()
tests = (
    ("default off", "select @@session.enable_file_sql"),
    ("count(read_csv) while OFF", "select count(*) from read_csv('/tmp/x.csv')"),
    ("set ON", "set session enable_file_sql = ON"),
    ("count(read_csv) while ON", "select count(*) from read_csv('/tmp/x.csv')"),
    ("positional format arg", "select * from read_csv('/tmp/x.csv', 'xml')"),
    ("sample_size = 10", "select * from read_csv('/tmp/x.csv', sample_size = 10)"),
    ("glob", "select * from glob('/tmp/*')"),
    ("describe select", "describe select * from read_csv('/tmp/x.csv')"),
    ("alias form", "select * from read_csv('/tmp/x.csv') f"),
    ("plain sql still fine", "select 1 + 1"),
)
for label, sql in tests:
    try:
        c.execute(sql)
        rows = c.fetchall()
        print(f"{label:26s} -> OK {str(rows)[:70]}")
    except pymysql.MySQLError as e:
        print(f"{label:26s} -> ERR {e.args[0]} {str(e.args[1])[:60]}")
PY
echo PARSETEST_DONE
