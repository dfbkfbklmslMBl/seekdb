#!/usr/bin/env bash
# Final purity pass: tree must equal the pushed branch exactly, then re-measure.
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
PY=/opt/conda/bin/python3
cd "$S" || exit 1
export PATH=/root/.cargo/bin:$PATH

git checkout -- src/storage/fts src/observer/composition src/sql/engine/expr/ob_expr_tokenize.cpp 2>/dev/null
echo "[PURE] dirty tracked files now: $(git status --short | grep -c '^ M')"
git diff --name-only

# Compare each modified file's hash with the pushed branch's blob (captured locally from the worktree,
# so "verified" cannot silently mean "verified against something slightly different from what was pushed").
$PY - <<'PYEOF'
import subprocess
EXPECTED = {
    "src/observer/ai_service/ob_ai_service_proxy.cpp": "19e52104def726f42d5bb21a86b14fe26872603f",
    "src/share/ai_service/ob_ai_service_struct.cpp": "d1aab5746d6fa8bd78312429483a1c3744aad2c4",
    "src/sql/engine/expr/ob_expr_ai/ob_ai_func_utils.cpp": "f16423fde63824f711115c27d4a4eb7f90ed0a2a",
    "src/sql/engine/expr/ob_expr_ai/ob_expr_ai_complete.cpp": "6b49c6668e1258c7ecda0972d964f785961d8527",
    "src/sql/engine/expr/ob_expr_ai/ob_expr_ai_embed.cpp": "ba9e0b264ad175704f6469725d30d4a7bf4a74ee",
    "src/sql/engine/expr/ob_expr_ai/ob_expr_ai_prompt.cpp": "0fc2e0340e8bc7334e359a4ea5df0ed160c3a8de",
    "src/sql/engine/expr/ob_expr_ai/ob_expr_ai_rerank.cpp": "bd3ee4f23090054acc468d6f798563e6548ccee3",
}
files = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True).stdout.split()
bad = []
for f in files:
    local = subprocess.run(["git", "hash-object", f], capture_output=True, text=True).stdout.strip()
    want = EXPECTED.get(f)
    if want is None:
        tag, _ = "UNEXPECTED-FILE", bad.append(f)
    elif local == want:
        tag = "MATCH"
    else:
        tag, _ = "DIFFER", bad.append(f)
    print(f"[PURE] {tag} {f}")
print(f"[PURE] modified={len(files)} mismatched={len(bad)} expected_set={len(EXPECTED)}")
PYEOF

./build.sh release --make -j256 > "$D/ab-make-4.log" 2>&1; echo "[PURE] make rc=$?"
bash "$K/newbox.sh" boot > /dev/null 2>&1; echo "[PURE] boot rc=$?"
echo "[PURE] binary_md5=$(md5sum build_release/src/observer/seekdb | cut -c1-32)"
for n in ai_function_enhancement ai_prompt; do
  $PY "$K/mysqltest-oracle.py" --host 127.0.0.1 --port 2881 \
    --test "tools/deploy/mysql_test/test_suite/ai_function/t/$n.test" \
    --result "tools/deploy/mysql_test/test_suite/ai_function/r/mysql/$n.result" \
    --out "$D/ab-pure-$n.actual" > "$D/ab-pure-$n.diff" 2>&1
  echo "[PURE] $(grep -a '\[ORACLE\]' "$D/ab-pure-$n.diff" | head -1)"
done
echo PURE_DONE
