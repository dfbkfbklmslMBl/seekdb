#!/usr/bin/env bash
# Milestone M1a for 赛题二: declare the enable_file_sql session sysvar and prove a live session reads it.
# Uses the repo's own generator (verified: a no-op regeneration only reorders field assignments and adds
# LOG_WARN lines, so it is semantically neutral), because hand-editing the generated arrays is what the
# generator exists to avoid.
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
PYB=/opt/conda/bin/python3
cd "$S" || exit 1
export PATH=/root/.cargo/bin:$PATH

echo "=== 1. port the generator to py3 (3 iteritems calls) ==="
$PYB - <<'PY'
import pathlib
p = pathlib.Path("src/share/system_variable/gen_ob_sys_variables.py")
b = p.read_bytes()
print("iteritems left:", b.count(b".iteritems()"))
p.write_bytes(b.replace(b".iteritems()", b".items()"))
PY

echo "=== 2. add enable_file_sql to the JSON source of truth ==="
$PYB - <<'PY'
import json
import pathlib
p = pathlib.Path("src/share/system_variable/ob_system_variable_init.json")
text = p.read_text(encoding="utf-8")
entry = {
    "id": 10754,
    "name": "enable_file_sql",
    "default_value": "0",
    "base_value": "0",
    "data_type": "bool",
    "info": "enable querying local CSV/JSONL/Parquet files as tables with read_csv/read_json/read_parquet/glob",
    "flags": "SESSION | GLOBAL | INFLUENCE_PLAN | NEED_SERIALIZE",
    "publish_version": "1.0.0.0",
    "info_cn": "",
    "background_cn": "",
    "ref_url": "",
}
if '"enable_file_sql"' in text:
    print("entry already present")
else:
    idx = text.rstrip().rfind("\n}")
    assert idx > 0, idx
    body = json.dumps(entry, indent=2, ensure_ascii=False)
    body = "\n".join(("  " + l) if i else ("  " + l) for i, l in enumerate(body.split("\n")))
    text = text[:idx] + ",\n  \"enable_file_sql\": " + body + text[idx:]
    p.write_text(text, encoding="utf-8")
    print("entry added at id 10754")
    json.loads(p.read_text(encoding="utf-8"))  # must still parse
    print("json still valid, entries:", len(json.loads(p.read_text(encoding='utf-8'))))
PY

echo "=== 3. regenerate ==="
cd src/share/system_variable || exit 1
$PYB gen_ob_sys_variables.py > "$D/gen-m1a.log" 2>&1; echo "generator rc=$?"
tail -3 "$D/gen-m1a.log"
cd "$S" || exit 1
echo "=== 4. what changed ==="
git diff --numstat -- src/share/system_variable src/sql/session
echo "new enum:      $(grep -rn 'SYS_VAR_ENABLE_FILE_SQL' src/share/system_variable/ob_sys_var_class_type.h | head -1)"
echo "new alias:     $(grep -rn 'ENABLE_FILE_SQL' src/share/system_variable/ob_system_variable_alias.h | head -1)"
echo "class emitted: $(grep -rn 'class SessionEnableFileSql' src/sql/session/ob_system_variable_factory.h | head -1)"

echo "=== 5. build ==="
./build.sh release --make -j256 > "$D/make-m1a.log" 2>&1; echo "make rc=$?"
grep -aE 'error:' "$D/make-m1a.log" | head -12

echo "=== 6. boot and ask a live session ==="
bash "$D/ktools/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"
$PYB - <<'PY'
import pymysql
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test", autocommit=True).cursor()
for sql in ("select @@session.enable_file_sql",
            "set session enable_file_sql = ON",
            "select @@session.enable_file_sql",
            "set session enable_file_sql = OFF",
            "select @@session.enable_file_sql",
            "show variables like 'enable_file_sql'"):
    try:
        c.execute(sql)
        print(f"{sql:45s} -> {c.fetchall()}")
    except pymysql.MySQLError as e:
        print(f"{sql:45s} -> ERR {e.args}")
PY
echo M1A_DONE
