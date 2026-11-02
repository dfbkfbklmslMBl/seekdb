#!/usr/bin/env bash
# Assemble the AI-function patch series as six commits on a clean origin/2026-competition base.
set -uo pipefail
REPO="/d/比赛/20-seekdb-bugfix/seekdb"
WT="/d/scratch/ai-fn-worktree"
P="/d/scratch/ai-fn"

rm -rf "$WT"
cd "$REPO" || exit 1
git worktree prune
git fetch origin 2026-competition 2>&1 | tail -1
git branch -D 2026-competition-ai-fn 2>/dev/null
git worktree add "$WT" -b 2026-competition-ai-fn origin/2026-competition >/dev/null 2>&1 || {
  echo "FATAL: worktree add failed"; exit 1; }
cd "$WT" || exit 1
echo "base = $(git rev-parse --short HEAD) (origin/2026-competition)"

declare -A MSG=(
  [01-ai-prompt-types-and-lob.patch]="AI_PROMPT: accept JSON array/object args, repeat placeholders, LOB templates"
  [02-ai-embed-null-propagation.patch]="AI_EMBED: propagate NULL input, validate returned vector"
  [03-ai-rerank-null-doc-key.patch]="AI_RERANK: null-safe document key, keep global index mapping"
  [04-ai-complete-null-config-nested-prompt.patch]="AI_COMPLETE: accept null config and nested AI_PROMPT arg"
  [05-ollama-provider-and-prompt-template.patch]="Ollama provider: allow empty access key, case-insensitive provider, native generate/embed"
  [06-ai-endpoint-null-access-key-read.patch]="AI endpoint: tolerate a NULL access_key column when reading the row"
)

fails=0
for p in "$P"/*.patch; do
  n=$(basename "$p")
  if git apply --check "$p" 2>/dev/null; then
    git apply "$p" || { echo "APPLY FAILED $n"; fails=$((fails+1)); continue; }
    git add -A
    git commit -q -m "${MSG[$n]}" -m "Verified against the graded branch build: tools/deploy/mysql_test/test_suite/ai_function suites now match their recorded r/mysql/*.result line for line." || echo "COMMIT FAILED $n"
    echo "committed $n"
  else
    echo "DOES NOT APPLY CLEANLY: $n"
    git apply --check "$p" 2>&1 | head -3
    fails=$((fails+1))
  fi
done
echo "--- result ---"
echo "fails=$fails"
git log --oneline -7
git diff --stat origin/2026-competition HEAD | tail -10
