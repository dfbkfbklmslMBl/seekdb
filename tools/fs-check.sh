#!/usr/bin/env bash
# Syntax-check one .cpp with the toolchain flags from compile_commands.json.
# Every run also executes a NEGATIVE CONTROL (a file that must fail to compile), because a
# checker that cannot fail proves nothing.
#   bash fs-check.sh src/sql/file_scan/ob_file_scan_utils.cpp
set -uo pipefail
S=/root/rivermind-data/seekdb
cd "$S" || exit 1
/opt/conda/bin/python3 - "$@" <<'PY'
import json
import os
import shlex
import subprocess
import sys

root = os.getcwd()
cc = json.load(open("build_release/compile_commands.json"))
target_rel = sys.argv[1]
target = os.path.join(root, target_rel)
assert os.path.isfile(target), f"target not found: {target}"

donor = None
for e in cc:
    if e["file"].endswith("unity_ob_sql_ob_sql_resolver_ddl_1_c___cxx.cxx"):
        donor = e
        break
if donor is None:
    donor = next(e for e in cc if "/src/sql/" in e["file"])


def build_cmd(src_abs):
    args = shlex.split(donor["command"])
    out = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "-o":
            skip = True
            continue
        if a == donor["file"] or os.path.basename(a) == os.path.basename(donor["file"]):
            continue
        out.append(a)
    out += ["-fsyntax-only", "-c", src_abs]
    return out


def run(label, src_abs, expect_fail):
    cmd = build_cmd(src_abs)
    r = subprocess.run(cmd, cwd=donor["directory"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    out = r.stdout.decode("utf-8", "replace")
    errs = [l for l in out.splitlines() if "error:" in l]
    warns = [l for l in out.splitlines() if "warning:" in l]
    verdict = "OK" if (bool(errs) == expect_fail) else "CHECKER-BROKEN"
    print(f"[{verdict}] {label}: rc={r.returncode} errors={len(errs)} warnings={len(warns)}")
    for l in errs[:18]:
        print("   " + l[:230])
    if verdict == "CHECKER-BROKEN":
        print(out[:1500])
    return len(errs), verdict


# negative control first: it must produce compiler errors
bad = os.path.join(root, "src/sql/file_scan/.negative_control.cpp")
with open(bad, "w") as h:
    h.write("int probe_negative_control() { return; }\n")
n_err, v = run("negative control (must fail)", bad, True)
os.remove(bad)
if n_err == 0 or v != "OK":
    print("ABORT: checker cannot detect a broken file, its verdicts are meaningless")
    sys.exit(2)

n_err, _ = run(target_rel, target, False)
print(f"RESULT {target_rel}: {'clean' if n_err == 0 else str(n_err) + ' errors'}")
PY
