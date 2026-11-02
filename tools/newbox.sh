#!/usr/bin/env bash
# Bootstrap the seekdb competition tree on a fresh CPU-only box. Stages are idempotent and re-runnable.
#   bash newbox.sh apt | src | patches | rust | init | make | boot | verify | all
# Everything it does is logged to $D/bootstrap.log; each stage prints its own timing so a slow step is
# visible instead of looking like a hang.
set -uo pipefail
D=/root/rivermind-data
S=$D/seekdb
K=$D/ktools
LOG=$D/bootstrap.log
STAGE="${1:-all}"
exec >> "$LOG" 2>&1
say() { echo "[$(date +%H:%M:%S)] $*"; }
do_stage() { [ "$STAGE" = all ] || [ "$STAGE" = "$1" ]; }
jobn() { nproc; }

say "===== stage=$STAGE host=$(hostname) cores=$(nproc) mem=$(free -g | awk 'NR==2{print $2}')G"

if do_stage apt; then
  say "-- apt: rpm2cpio+cpio are what make dep_create.sh's el9 RPMs land in deps/3rd"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || say "apt update rc=$?"
  apt-get install -y -qq rpm2cpio cpio curl wget git openssh-client patch || say "apt install rc=$?"
  say "rpm2cpio=$(command -v rpm2cpio || echo MISSING) cpio=$(command -v cpio || echo MISSING)"
fi

if do_stage src; then
  say "-- src: unpack $(ls -la $D/seekdb-src.tar.gz 2>/dev/null | awk '{print $5}') bytes"
  mkdir -p "$S"
  if [ -f "$S/.bootstrapped_src" ]; then
    say "already unpacked"
  else
    tar xzf "$D/seekdb-src.tar.gz" -C "$S" || { say "FATAL: unpack failed"; exit 1; }
    cd "$S" || exit 1
    git init -q
    git config user.email build@local
    git config user.name build
    git add -A >/dev/null 2>&1
    git commit -qm "import uploaded tree" >/dev/null 2>&1
    say "git HEAD=$(git rev-parse --short HEAD) files=$(git ls-files | wc -l)"
    touch "$S/.bootstrapped_src"
  fi
fi

if do_stage patches; then
  say "-- patches: 5 ai-fn patches + patch06.py (edit script, same anchor strings every time)"
  cd "$S" || exit 1
  for p in "$K"/ai-fn/*.patch; do
    if patch -p1 --dry-run -N -s < "$p" > /dev/null 2>&1; then
      patch -p1 -N -s < "$p" > /dev/null && say "applied $(basename "$p")"
    else
      say "SKIP (already applied or does not fit): $(basename "$p")"
    fi
  done
  if grep -q 'EXTRACT_VARCHAR_FIELD_MYSQL_WITH_DEFAULT_VALUE(result, "access_key"' \
       src/observer/ai_service/ob_ai_service_proxy.cpp; then
    say "SKIP patch06 (already applied)"
  else
    python3 "$K/patch06.py" && say "applied patch06"
  fi
  say "modified files: $(git status --short | grep -c '^ M')"
fi

if do_stage rust; then
  say "-- rust: upstream pins a toolchain; static.rust-lang.org is DNS-poisoned here, so rsproxy"
  export RUSTUP_DIST_SERVER=https://rsproxy.cn
  export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
  export PATH=/root/.cargo/bin:$PATH
  if command -v cargo > /dev/null; then
    say "cargo=$(cargo --version)"
  else
    # the downloaded binary dispatches on argv[0], so it must keep the name rustup-init
    curl -sSf https://rsproxy.cn/rustup-init.sh -o /tmp/rustup-init.sh || say "curl rc=$?"
    sh /tmp/rustup-init.sh -y --default-toolchain none >/dev/null 2>&1 || say "rustup-init rc=$?"
    say "cargo=$(cargo --version 2>&1 | head -1)"
  fi
  mkdir -p /root/.cargo
  cat > /root/.cargo/config.toml <<'CFG'
[source.crates-io]
replace-with = 'rsproxy-sparse'
[source.rsproxy]
registry = "https://rsproxy.cn/crates.io-index"
[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
[registries.rsproxy]
index = "https://rsproxy.cn/crates.io-index"
[net]
git-fetch-with-cli = true
CFG
  say "cargo config written"
  # The pin lives in rust/rust-toolchain.toml, which is NOT an ancestor of the repo root, so rustup
  # will not auto-select it -- `cargo --version` from the top of the tree just says "no default".
  # Read the pin (and its target list) out of the file and install it explicitly.
  if [ -f "$S/rust/rust-toolchain.toml" ]; then
    eval "$(/opt/conda/bin/python3 -c "
import tomllib
cfg = tomllib.load(open('$S/rust/rust-toolchain.toml','rb'))['toolchain']
print('PIN_VER=' + repr(cfg['channel']))
print('PIN_TARGETS=' + repr(' '.join(cfg.get('targets', []))))
print('PIN_COMPONENTS=' + repr(' '.join(cfg.get('components', []))))
")"
    say "pinned toolchain=$PIN_VER targets=[$PIN_TARGETS] components=[$PIN_COMPONENTS]"
    EXTRA=""
    for t in $PIN_TARGETS; do EXTRA="$EXTRA --target $t"; done
    for c in $PIN_COMPONENTS; do EXTRA="$EXTRA --component $c"; done
    rustup toolchain install "$PIN_VER" --profile minimal $EXTRA 2>&1 | tail -5
    rustup default "$PIN_VER" 2>&1 | tail -2
    say "cargo=$(cargo --version 2>&1 | head -1) rustc=$(rustc --version 2>&1 | head -1)"
  fi
fi

if do_stage init; then
  say "-- init: dep_create.sh (~7 GB of el9 RPMs) + cmake configure"
  cd "$S" || exit 1
  export PATH=/root/.cargo/bin:$PATH
  time ./build.sh release --init
  say "init rc=$?"
fi

if do_stage make; then
  say "-- make: -j$(jobn)"
  cd "$S" || exit 1
  export PATH=/root/.cargo/bin:$PATH
  time ./build.sh release --make -j"$(jobn)"
  say "make rc=$? binary=$(ls -la build_release/src/observer/seekdb 2>/dev/null | awk '{print $5, $6, $7, $8}')"
fi

if do_stage boot; then
  say "-- boot: single-node seekdb, store on the ext4 data disk (never on the JuiceFS mount)"
  # A graceful observer shutdown takes far longer than any fixed sleep. The first version of this
  # script slept 8s, relaunched (which silently failed to bind 2881) and then "confirmed readiness"
  # against the OLD process -- so an entire before/after A/B measured the same binary twice.
  OLD_PID=$(pgrep -f '[o]bserver/seekdb' | head -1 || true)
  if [ -n "${OLD_PID:-}" ]; then
    say "stopping pid=$OLD_PID (SIGTERM, waiting up to 120s)"
    kill -15 "$OLD_PID" 2>/dev/null
    for i in $(seq 1 60); do
      kill -0 "$OLD_PID" 2>/dev/null || break
      sleep 2
    done
    if kill -0 "$OLD_PID" 2>/dev/null; then
      say "still alive after 120s, SIGKILL"
      kill -9 "$OLD_PID" 2>/dev/null
      sleep 5
    fi
    kill -0 "$OLD_PID" 2>/dev/null && { say "FATAL: pid $OLD_PID refuses to die"; exit 1; }
  fi
  cd "$S" || exit 1
  mkdir -p "$D/ins1" "$D/fsdata"
  setsid nohup ./build_release/src/observer/seekdb --nodaemon --port 2881 \
    --base-dir "$D/ins1" --data-dir "$D/ins1/store" > "$D/ins1/seekdb.log" 2>&1 < /dev/null &
  NEW_PID=$!
  say "launched pid=$NEW_PID"
  say "binary under test: md5=$(md5sum build_release/src/observer/seekdb | cut -c1-32) mtime=$(date -r build_release/src/observer/seekdb +%H:%M:%S)"
  for i in $(seq 1 120); do
    /opt/conda/bin/python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(('127.0.0.1',2881))==0 else 1)" > /dev/null 2>&1 && break
    kill -0 "$NEW_PID" 2>/dev/null || { say "FATAL: new pid died before binding (see seekdb.log)"; exit 1; }
    sleep 5
  done
  # Prove the listener IS the new binary, not a survivor holding the port.
  LISTEN_PID=$(ss -lntp 2>/dev/null | grep ':2881' | grep -aoE 'pid=[0-9]+' | head -1 | tr -d 'pid=')
  say "listener pid on 2881 = ${LISTEN_PID:-none} (expected $NEW_PID)"
  [ "${LISTEN_PID:-}" = "$NEW_PID" ] || say "WARNING: port 2881 is held by a different pid"
  /opt/conda/bin/python3 -c "
import pymysql
c=pymysql.connect(host='127.0.0.1',port=2881,user='root',database='test',autocommit=True).cursor()
c.execute('select version()'); print('SQL=', c.fetchone()[0])" || say "sql probe rc=$?"
  if ! pgrep -f '[m]ock_ai_service' > /dev/null; then
    setsid nohup /opt/conda/bin/python3 "$K/mock_ai_service.py" > "$D/mock.log" 2>&1 < /dev/null &
    say "mock pid=$!"
  fi
  say "mock listener: $(ss -lntp 2>/dev/null | grep -c ':8000')"
fi

if do_stage verify; then
  say "-- verify: replay the two graded suites against their recorded .result"
  bash "$K/oracle_all.sh" ai_function
fi

say "===== stage=$STAGE done"
