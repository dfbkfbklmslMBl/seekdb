#!/usr/bin/env bash
# 内核题一键验证：打补丁 → 编译 → 跑官方 mysqltest 套件 → 跑全文索引基准。
#
# 本脚本在 Windows 上编写，**从未被执行过**（本机没有 Linux 工具链）。
# 它的作用是照抄 CI 的入参契约，避免有机器之后还要重新试错。所有需要现场确认的点都标了 [待核]。
#
# CI 契约来源（origin/2026-competition）：
#   .github/script/seekdb/compile.sh        需要 GITHUB_WORKSPACE、SEEKDB_TASK_DIR；先 `build.sh init` 再 release 构建；PACKAGE_TYPE 默认 release
#   .github/script/seekdb/mysqltest_slice.sh 需要 GITHUB_WORKSPACE、SEEKDB_BINARY；
#       SLICE_IDX(默认0) SLICES(默认4) SEEKDB_RUNTIME_DIR(默认 $WORKSPACE/.seekdb_runtime)
#       MYSQLTEST_PORT(默认 5000+SLICE_IDX*100)；客户端取 $RUNTIME_DIR/obclient
#   .github/script/seekdb/mysqltest_for_seekdb.py:249 以 **blacklist** 模式发现 test_suite/*/t/*.test
#
# 用法：bash 验证流程.sh [apply|build|test|bench|all]

set -uo pipefail

REPO="${REPO:-$PWD}"
PATCH_DIRS="${PATCH_DIRS:-$HOME/patches/ai-fn $HOME/patches/fts-opt $HOME/patches/ai-column}"
TASK_DIR="${SEEKDB_TASK_DIR:-/tmp/seekdb-verify}"
BRANCH="${BRANCH:-work/verify}"
BASE="${BASE:-origin/2026-competition}"

export GITHUB_WORKSPACE="$REPO"
export SEEKDB_TASK_DIR="$TASK_DIR"

log() { printf '\n[verify] %s\n' "$*"; }

do_apply() {
  cd "$REPO" || return 1
  git rev-parse --verify "$BRANCH" >/dev/null 2>&1 || git branch "$BRANCH" "$BASE" || return 1
  git checkout "$BRANCH" || return 1
  mkdir -p "$TASK_DIR"
  for dir in $PATCH_DIRS; do
    [ -d "$dir" ] || { log "跳过不存在的补丁目录：$dir"; continue; }
    for p in $(ls "$dir"/*.patch 2>/dev/null | sort); do
      # -p1 与 CI 里 git 生成的 diff 对齐；-N 让新文件生效；--dry-run 先试一遍再落盘
      if patch -p1 -N --dry-run -d "$REPO" < "$p" >/dev/null 2>&1; then
        patch -p1 -N -d "$REPO" < "$p" >"$TASK_DIR/apply-$(basename "$p").log" 2>&1 \
          && log "补丁 OK：$(basename "$p")" \
          || { log "补丁失败：$(basename "$p") 见 $TASK_DIR/apply-*.log"; return 1; }
      else
        log "补丁 dry-run 失败（多半基线已变）：$(basename "$p")"
        return 1
      fi
    done
  done
  log "打完后 git status（应只看到预期文件）："
  git status --short | head -30
}

do_build() {
  cd "$REPO" || return 1
  log "构建（照 CI：先 init 再 release）。首次会拉依赖，耗时较长。"
  # CI 走 compile.sh；这里保留两条路径，脚本不可用时可手工退回到 build.sh
  if [ -x .github/script/seekdb/compile.sh ] || [ -f .github/script/seekdb/compile.sh ]; then
    bash .github/script/seekdb/compile.sh 2>&1 | tee "$TASK_DIR/compile.log" | tail -20
  else
    bash build.sh init 2>&1 | tee "$TASK_DIR/build_init.log" | tail -5
    bash build.sh release --make -j"$(nproc)" 2>&1 | tee "$TASK_DIR/build_release.log" | tail -20
  fi
  # [待核] 产物确切路径：优先 seekdb，其次 observer
  for cand in build_release/src/observer/seekdb build_release/src/observer/observer build_release/bin/seekdb; do
    if [ -x "$REPO/$cand" ]; then export SEEKDB_BINARY="$REPO/$cand"; log "SEEKDB_BINARY=$SEEKDB_BINARY"; break; fi
  done
  : "${SEEKDB_BINARY:?未找到可执行产物，请 [待核] 后手工 export SEEKDB_BINARY}"
}

do_test() {
  cd "$REPO" || return 1
  export SEEKDB_BINARY="${SEEKDB_BINARY:-$REPO/build_release/src/observer/seekdb}"
  log "mysqltest：需要 $SEEKDB_RUNTIME_DIR/obclient/bin/{obclient,mysqltest}"
  # [待核] CI 的 obclient+mysqltest 是 setup job 打成 tar.zst 从 NFS 取的；本机要自己准备：
  #   从 OceanBase 交付件里取 obclient/mysqltest 两个二进制，放进 $REPO/.seekdb_runtime/obclient/bin/
  if [ ! -x "${SEEKDB_RUNTIME_DIR:-$REPO/.seekdb_runtime}/obclient/bin/mysqltest" ]; then
    log "缺 mysqltest 二进制，跳过（见上面 [待核]）"
    return 1
  fi
  for SUITE in ai_function ai_column file_sql fts_index; do
    log "=== 套件 $SUITE ==="
    # CI 用分片；本机按套件跑更直观。[待核] mysqltest_slice.sh 是否支持只跑单套，
    # 不支持就直接用 mysqltest_for_seekdb.py 的发现逻辑或手工 --test-set。
    SLICE_IDX=0 SLICES=1 MYSQLTEST_PORT=5000 bash .github/script/seekdb/mysqltest_slice.sh \
      2>&1 | tee "$TASK_DIR/mysqltest_$SUITE.log" | tail -15
  done
  log "注意：blacklist 模式会自动发现 test_suite/*/t/*.test，**不要往判题树里加用例**（重复/缺 result 会直接报错）"
}

do_bench() {
  cd "$REPO" || return 1
  log "全文索引基准：先 SKIP_LOAD=1 只看分词与查询（快），再按需跑全量"
  LABEL=after SKIP_LOAD=1 bash tools/benchmark/fts_large_bench.sh 2>&1 | tee "$TASK_DIR/bench_fast.log" | tail -20
  log "全量（含重新灌数与建索引）耗时长，确认快速档没问题再跑：SKIP_LOAD=0"
  log "评分：三类各自平均后等权；综合降 50% 满分；退化以负提升参与平均。基线见 tools/benchmark/fts_large_bench_baseline.json"
}

case "${1:-all}" in
  apply) do_apply ;;
  build) do_build ;;
  test)  do_test ;;
  bench) do_bench ;;
  all)   do_apply && do_build && do_test ;;
  *)     log "用法：bash 验证流程.sh [apply|build|test|bench|all]"; exit 2 ;;
esac
