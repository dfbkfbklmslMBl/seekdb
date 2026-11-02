#!/usr/bin/env bash
# 每份内核补丁单独做「只编译受影响翻译单元」的检查。
#
# 为什么要它：整库 build.sh 是几十分钟量级，而一个语法错要等到整库编译到那个文件才暴露；
# 这个脚本用 build.sh 已经生成的 compile_commands.json（build.sh:203 就设了
# -DCMAKE_EXPORT_COMPILE_COMMANDS=ON）只对被改到的那些 TU 跑 -fsyntax-only，
# 一份补丁几十秒，失败就打印第一条 error。
#
# 用法（在 seekdb 仓库根目录）：
#     bash ../kbundle-tools/补丁语法二分.sh                  # 默认扫 kbundle 里四个补丁目录
#     PATCH_DIR=~/kbundle/kernel-patches-draft bash .../补丁语法二分.sh
#     BUILD_DIR=build/debug bash .../补丁语法二分.sh
#
# 只对 .cpp/.h 做 -fsyntax-only。改生成物输入的补丁（ai-column 那四份改的是
# ob_inner_table_schema_def.py / ob_errno.def / ob_system_variable_init.json / .y）不由本脚本判定，
# 它们要跑的是生成步骤本身：bison/flex 见租机清单第 6 步，schema/sysvar 生成看 cmake 的输出目录。
set -uo pipefail

REPO="${REPO:-$(pwd)}"
PATCH_DIR="${PATCH_DIR:-$HOME/kbundle/kernel-patches-draft}"
BUILD_DIR="${BUILD_DIR:-build/release}"
CC_JSON="$REPO/$BUILD_DIR/compile_commands.json"
cd "$REPO" || exit 2

if [ ! -f "$CC_JSON" ]; then
  echo "缺少 $CC_JSON —— 先执行 ./build.sh release --init（它会 configure 出编译数据库），"
  echo "或者手工 cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B $BUILD_DIR"
  exit 3
fi

syntax_check() {  # $1 = 源文件相对路径 ; 返回 0 表示该文件相关的 TU 全部语法通过
  SRC="$1"
  DIRTY=0
  mapfile -t CMDS < <(python3 - "$CC_JSON" "$SRC" <<'PY'
import json, sys, os
cc, src = json.load(open(sys.argv[1])), sys.argv[2]
src = src.lstrip('./')
base = os.path.basename(src)
stem = os.path.splitext(base)[0]
hit = []
for entry in cc:
    f = entry['file']
    # 直接命中，或者头文件命中它的同名 .cpp（OB 里 .h/.cpp 成对）
    if f.endswith(src) or (base.endswith('.h') and os.path.basename(f) == stem + '.cpp'):
        hit.append(entry)
for entry in hit[:6]:
    command = entry.get('command')
    if not command:
        command = ' '.join(entry.get('arguments') or [])
    parts = command.split()
    out = []
    skip = False
    for part in parts:
        if skip:                      # 丢掉 -o <obj>
            skip = False
            continue
        if part == '-o':
            skip = True
            continue
        if part.startswith('-c'):
            continue
        out.append(part)
    print(entry.get('directory', '.') + '\t' + ' '.join(out) + ' -fsyntax-only ' + entry['file'])
PY
  )
  if [ "${#CMDS[@]}" -eq 0 ]; then
    echo "      （编译数据库里没有 $SRC 对应的 TU —— 生成物类补丁或新文件未进 CMakeLists，需人工确认）"
    return 0
  fi
  for line in "${CMDS[@]}"; do
    dir="${line%%$'\t'*}"; cmd="${line#*$'\t'}"
    ( cd "$dir" && eval "$cmd" ) >/tmp/syn.$$ 2>&1
    if [ $? -ne 0 ]; then
      echo "      FAIL $SRC :: $(grep -m1 -E 'error:' /tmp/syn.$$ || head -2 /tmp/syn.$$)"
      DIRTY=1
    fi
  done
  rm -f /tmp/syn.$$
  return $DIRTY
}

touched() {  # 列出一份补丁动过的文件（两种 diff 头都认：+++ b/x 与 +++ B/x）
  grep -hE '^\+\+\+ [bB]/' "$1" | sed -E 's|^\+\+\+ [bB]/||; s|\t.*||'
}

TOTAL=0; OK=0; BAD=0
for patch in "$PATCH_DIR"/*/*.patch "$PATCH_DIR"/*/*.diff; do
  [ -e "$patch" ] || continue
  TOTAL=$((TOTAL + 1))
  echo "===== $patch"
  if ! patch -p1 --dry-run -N -s < "$patch" >/dev/null 2>&1; then
    echo "  跳过：这份补丁在当前树上贴不上（上下文已变），需要人工重做"
    BAD=$((BAD + 1)); continue
  fi
  patch -p1 -N -s < "$patch" >/dev/null || { echo "  应用失败"; BAD=$((BAD + 1)); continue; }
  PATCH_OK=1
  while IFS= read -r file; do
    case "$file" in
      *.cpp|*.h) echo "  检查 $file"; syntax_check "$file" || PATCH_OK=0 ;;
      *.y|*.l)   echo "  语法文件 $file：交给 bison/flex（见租机清单第 6 步）" ;;
      *)         echo "  生成物输入 $file：跑生成步骤，不在本脚本判定范围" ;;
    esac
  done < <(touched "$patch")
  patch -R -p1 -N -s < "$patch" >/dev/null || echo "  !! 回滚失败，工作树可能仍是脏的：git status 检查一下"
  if [ $PATCH_OK -eq 1 ]; then echo "  => OK"; OK=$((OK + 1)); else echo "  => 有失败项"; BAD=$((BAD + 1)); fi
done

echo
echo "汇总：$TOTAL 份补丁，$OK 份通过，$BAD 份有问题"
git status --porcelain | head -5
[ $BAD -eq 0 ]
