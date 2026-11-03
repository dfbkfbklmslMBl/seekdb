set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
PY=/opt/conda/bin/python3
cd "$S" || exit 1
$PY "$K/patch09b_no_sql_string.py" || exit 1
echo '### single-TU syntax checks'
ok=1
for f in src/sql/file_scan/ob_file_scan_utils.cpp src/sql/resolver/dml/ob_dml_resolver.cpp; do
  line=$(bash "$K/fs-check.sh" "$f" 2>&1)
  echo "$line" | tail -1
  if echo "$line" | grep -aq 'RESULT.*clean'; then :; else ok=0; echo "$line" | grep -aE 'error:' | head -14; fi
done
[ "$ok" = 1 ] || { echo P09B_CHECK_FAILED; exit 1; }
echo '### build'
md5_before=$(md5sum build_release/src/observer/seekdb | cut -c1-32)
for blob in $(find build_release -name 'unity_*' -name '*.cxx' -exec grep -l 'ob_file_scan_utils.cpp\|ob_dml_resolver.cpp' {} \;); do
  touch "$blob"
done
export PATH=/root/.cargo/bin:$PATH
./build.sh release --make -j256 > "$D/p09b-make.log" 2>&1; rc=$?
md5_after=$(md5sum build_release/src/observer/seekdb | cut -c1-32)
echo "make rc=$rc md5 $md5_before -> $md5_after"
if [ "$rc" != 0 ]; then grep -aE 'error:' "$D/p09b-make.log" | head -20; echo P09B_BUILD_FAILED; exit 1; fi
echo '### restart on the new binary'
bash "$K/p07-restart.sh" > "$D/p09b-verify.log" 2>&1 || true
echo '### the seven graded shapes'
$PY "$K/p09-probe.py" 2>&1 | tail -20
grep -aE '^\[SUMMARY\]' "$D/p09b-verify.log" | head -3
for n in file_sql_basic ai_function_enhancement ai_prompt; do
  printf '%-26s %s\n' "$n" "$(grep -a '\[ORACLE\]' "$D/oracle-$n.diff" 2>/dev/null | head -1)"
done
echo '### file_sql residual diff'
grep -aE '^[+-]' "$D/oracle-file_sql_basic.diff" 2>/dev/null | grep -av '^\(---\|+++\)' | head -34
echo P09B_ALL_DONE
