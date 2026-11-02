#!/usr/bin/env bash
# Apply the guard fix, rebuild the affected unity blob, boot a clean instance, re-run the contract
# table and the AI regression.
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
PYB=/opt/conda/bin/python3
FIX=$D/fsdata/data_for_mysqltest_file_sql
cd "$S" || exit 1
export PATH=/root/.cargo/bin:$PATH

$PYB "$D/fix_file_table_guard.py" || { echo "FIX FAILED"; exit 1; }
grep -c 'create_table_item(\*allocator_)' src/sql/resolver/dml/ob_dml_resolver.cpp | head -1

bash "$D/fs-check.sh" src/sql/resolver/dml/ob_dml_resolver.cpp 2>&1 | grep -aE 'RESULT|error:' | head -5

# a source inside a CMake unity blob is only recompiled if the blob itself is touched
find build_release/src/sql/CMakeFiles/ob_sql.dir/Unity -name '*resolver_dml*.cxx' -exec touch {} +
time ./build.sh release --make -j256 > "$D/make-m1g.log" 2>&1
echo "make rc=$?"
grep -a 'error:' "$D/make-m1g.log" | head -5

pkill -f '[o]bserver/seekdb'; sleep 15
export INSDIR=$D/ins11
rm -rf "$INSDIR"; mkdir -p "$INSDIR"
bash "$D/ktools/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"
grep -aE 'binary under test|listener pid' "$D/bootstrap.log" | tail -2

$PYB - <<PY
import pymysql
FIX = "$FIX"
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test", autocommit=True).cursor()
def run(label, sql, want):
    try:
        c.execute(sql); got = "OK"
    except pymysql.MySQLError as e:
        got = e.args[0]
    ok = str(got) == str(want)
    print(f"[{'PASS' if ok else 'FAIL'}] {label:30s} want={want} got={got}")
run("off: gate -> 1235", f"select count(*) from read_csv('{FIX}/basic.csv')", 1235)
c.execute("set session enable_file_sql = ON")
run("on: positional xml -> 1064", f"select * from read_csv('{FIX}/basic.csv', 'xml')", 1064)
run("on: sample_size=10 -> 1235", f"select * from read_csv('{FIX}/basic.csv', sample_size = 10)", 1235)
run("on: csv -> scan layer 1235", f"select count(*) from read_csv('{FIX}/basic.csv')", 1235)
run("on: header+sample=-1", f"select * from read_csv('{FIX}/basic.csv', header = true, sample_size = -1)", 1235)
run("on: jsonl -> scan layer", f"select * from read_json('{FIX}/basic.jsonl', format = 'newline_delimited')", 1235)
run("on: glob -> scan layer", f"select * from glob('{FIX}/*')", 1235)
run("on: missing file", f"select count(*) from read_csv('{FIX}/nope.csv')", "ANY")
run("plain sql", "select 1 + 1", "OK")
run("read_csv usable as identifier", "select 1 as read_csv", "OK")
run("glob usable as table alias", "select 1 from (select 1) glob", "OK")
PY

echo "=== AI regression ==="
for n in ai_function_enhancement ai_prompt; do
  $PYB "$D/ktools/mysqltest-oracle.py" --host 127.0.0.1 --port 2881 \
    --test "tools/deploy/mysql_test/test_suite/ai_function/t/$n.test" \
    --result "tools/deploy/mysql_test/test_suite/ai_function/r/mysql/$n.result" \
    --out "$D/m1g-$n.actual" > "$D/m1g-$n.diff" 2>&1
  echo "$(grep -a '\[ORACLE\]' "$D/m1g-$n.diff" | head -1)"
done
echo M1G_DONE
