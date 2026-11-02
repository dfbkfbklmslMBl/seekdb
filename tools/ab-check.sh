#!/usr/bin/env bash
# A/B on ONE machine with a restart that is PROVEN to swap the binary (md5 + listener pid recorded
# every stage, plus a SQL discriminator that only the patched code can pass).
#
#   stage 1: AI files reverted  -> "before"
#   stage 2: AI files restored  -> "after" (FTS patches still applied)
#   stage 3: FTS files reverted -> "submitted" (tree == pushed branch 56bafddf8)
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
PY=/opt/conda/bin/python3
AI_FILES="src/observer/ai_service/ob_ai_service_proxy.cpp
src/share/ai_service/ob_ai_service_struct.cpp
src/sql/engine/expr/ob_expr_ai/ob_ai_func_utils.cpp
src/sql/engine/expr/ob_expr_ai/ob_expr_ai_complete.cpp
src/sql/engine/expr/ob_expr_ai/ob_expr_ai_embed.cpp
src/sql/engine/expr/ob_expr_ai/ob_expr_ai_prompt.cpp
src/sql/engine/expr/ob_expr_ai/ob_expr_ai_rerank.cpp"

cd "$S" || exit 1
export PATH=/root/.cargo/bin:$PATH

measure() {
  local tag="$1" md5 dirty
  md5=$(md5sum build_release/src/observer/seekdb | cut -c1-32)
  dirty=$(git status --short | grep -c '^ M')
  echo "[AB $tag] binary_md5=$md5 dirty_files=$dirty listener=$(ss -lntp 2>/dev/null | grep ':2881' | grep -aoE 'pid=[0-9]+' | head -1)"
  $PY - "$tag" <<'PYEOF'
import json
import sys
import pymysql
tag = sys.argv[1]
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test", autocommit=True).cursor()
for s in ("call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('disc_ep')",
          "call DBMS_AI_SERVICE.DROP_AI_MODEL('disc_model')"):
    try:
        c.execute(s)
    except pymysql.MySQLError:
        pass
c.execute("call DBMS_AI_SERVICE.CREATE_AI_MODEL('disc_model', '"
          + json.dumps({"type": "completion", "model_name": "m"}) + "')")
ep = json.dumps({"ai_model_name": "disc_model", "scope": "all",
                 "url": "http://0.0.0.0:8000/api/generate",
                 "access_key": "", "provider": "ollama"})
try:
    c.execute("call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('disc_ep', '" + ep + "')")
    verdict = "ACCEPTED"
except pymysql.MySQLError as exc:
    verdict = "REJECTED " + str(exc.args)
print(f"[AB {tag}] DISCRIMINATOR empty_access_key_for_ollama = {verdict}")
for s in ("call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('disc_ep')",
          "call DBMS_AI_SERVICE.DROP_AI_MODEL('disc_model')"):
    try:
        c.execute(s)
    except pymysql.MySQLError:
        pass
PYEOF
  for n in ai_function_enhancement ai_prompt; do
    $PY "$K/mysqltest-oracle.py" --host 127.0.0.1 --port 2881 \
      --test "tools/deploy/mysql_test/test_suite/ai_function/t/$n.test" \
      --result "tools/deploy/mysql_test/test_suite/ai_function/r/mysql/$n.result" \
      --out "$D/ab-$tag-$n.actual" > "$D/ab-$tag-$n.diff" 2>&1
    echo "[AB $tag] $(grep -a '\[ORACLE\]' "$D/ab-$tag-$n.diff" | head -1)"
  done
}

echo "===== stage 1: revert the 7 AI files"
for f in $AI_FILES; do git checkout -- "$f"; done
./build.sh release --make -j256 > "$D/ab-make-1.log" 2>&1; echo "make rc=$?"
bash "$K/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"
measure before

echo "===== stage 2: re-apply the 6 AI patches"
for p in "$K"/ai-fn/*.patch; do patch -p1 -N -s < "$p" > /dev/null 2>&1; done
./build.sh release --make -j256 > "$D/ab-make-2.log" 2>&1; echo "make rc=$?"
bash "$K/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"
measure after

echo "===== stage 3: revert the FTS files so the tree equals the pushed branch"
git checkout -- src/storage/fts src/sql/engine/expr/ob_expr_tokenize.cpp
./build.sh release --make -j256 > "$D/ab-make-3.log" 2>&1; echo "make rc=$?"
bash "$K/newbox.sh" boot > /dev/null 2>&1; echo "boot rc=$?"
measure submitted
echo "final dirty files: $(git status --short | grep '^ M' | wc -l)"
git diff --name-only
echo AB_DONE
