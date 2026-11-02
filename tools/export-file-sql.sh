#!/usr/bin/env bash
# Export the file-sql work so far as reviewable patches.
set -uo pipefail
S=/root/rivermind-data/seekdb
O=/root/rivermind-data/out
mkdir -p "$O"
cd "$S" || exit 1

# 02: the sysvar (json source + regenerated arrays + the py3 port of the generator)
git diff -- src/share/system_variable src/sql/session > "$O/02-enable-file-sql-sysvar.patch"
echo "02: $(grep -ac '^+' "$O/02-enable-file-sql-sysvar.patch") added lines, $(wc -l < "$O/02-enable-file-sql-sysvar.patch") patch lines"

# 03: the new reader (untracked files -> a create-style patch)
{
  for f in src/sql/file_scan/ob_file_scan_utils.h src/sql/file_scan/ob_file_scan_utils.cpp; do
    diff -u --label "a/$f" --label "b/$f" /dev/null "$f"
  done
} > "$O/03-file-scan-reader.patch"
echo "03: $(wc -l < "$O/03-file-scan-reader.patch") patch lines"

# 01 is the already-drafted grammar patch; verify it still applies to the current tree
git diff -- src/sql/parser src/query/api/query deps/oblib/src/lib/oblog \
           src/sql/resolver/ob_stmt_type.h src/objit > "$O/01-file-sql-parser-table-fn.applied.patch"
echo "01: $(wc -l < "$O/01-file-sql-parser-table-fn.applied.patch") lines of currently-applied grammar diff"
echo "=== all modified/untracked in sql+share ==="
git status --short | grep -aE 'system_variable|session|parser|file_scan|query/api' | head -12
echo EXPORT_DONE
