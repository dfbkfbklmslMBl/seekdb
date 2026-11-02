#!/usr/bin/env bash
# Milestone M1b: make read_csv/read_json/read_parquet/glob PARSE, then confirm what the resolver
# sees. The old dev store is moved aside (not deleted) because it predates the new sysvar id.
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
PYB=/opt/conda/bin/python3
cd "$S" || exit 1
export PATH=/root/.cargo/bin:$PATH

echo "=== 1. apply the drafted file-sql grammar patch ==="
p="$K/file-sql/01-file-sql-parser-table-fn.patch"
if git apply --check "$p" 2>/dev/null; then
  git apply "$p" && echo "applied cleanly (git apply)"
else
  echo "git apply --check failed; reasons:"
  git apply --check "$p" 2>&1 | head -5
  echo "-- trying patch(1) with fuzz --"
  patch -p1 -N < "$p" 2>&1 | tail -8
fi
git diff --name-only

echo "=== 2. bison check with the graded toolchain (2.4.1, -Werror -d) ==="
BISON=$(find deps/3rd -maxdepth 6 -type f -name bison 2>/dev/null | head -1)
echo "bison=$BISON $([ -n "$BISON" ] && $BISON -V | head -1)"
cd src/sql/parser || exit 1
if [ -n "$BISON" ]; then
  $BISON -v -Werror -d sql_parser_mysql_mode.y -o /tmp/m1b_tab.c 2> /tmp/m1b_bison.err
  echo "bison rc=$?  stderr lines=$(wc -l < /tmp/m1b_bison.err)  conflicts=$(grep -c 'conflict' /tmp/m1b_bison.err)"
  head -5 /tmp/m1b_bison.err
  echo "READ_CSV in generated tab.c: $(grep -ac 'READ_CSV' /tmp/m1b_tab.c)"
fi
cd "$S" || exit 1

echo "=== 3. build ==="
time ./build.sh release --make -j256 > "$D/make-m1b.log" 2>&1
echo "make rc=$?"
grep -aE 'error:' "$D/make-m1b.log" | head -10

echo "=== 4. move the pre-sysvar store aside and boot a fresh instance ==="
pkill -f '[o]bserver/seekdb' >/dev/null 2>&1
sleep 20
[ -d "$D/ins1/store" ] && mv "$D/ins1/store" "$D/ins1/store.pre-sysvar-$(date +%H%M)" && echo "store moved aside"
bash "$K/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"

echo "=== 5. what does the parser do now? ==="
$PYB - <<'PY'
import pymysql
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test",
                   autocommit=True).cursor()
tests = (
    "select @@session.enable_file_sql",
    "select count(*) from read_csv('/tmp/x.csv')",
    "set session enable_file_sql = ON",
    "select count(*) from read_csv('/tmp/x.csv')",
    "select * from read_csv('/tmp/x.csv', 'xml')",
    "select * from read_csv('/tmp/x.csv', sample_size = 10)",
    "select * from glob('/tmp/*')",
    "describe select * from read_csv('/tmp/x.csv')",
)
for sql in tests:
    try:
        c.execute(sql)
        print(f"{sql:52s} -> {str(c.fetchall())[:60]}")
    except pymysql.MySQLError as e:
        print(f"{sql:52s} -> ERR {e.args[0]} {str(e.args[1])[:60]}")
PY
echo M1B_DONE
