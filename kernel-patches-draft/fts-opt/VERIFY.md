# Minimum verification loop

Everything below runs on Linux from the repo root. Nothing here was executed — the
commands are what the checked-in tooling on `origin/2026-competition` supports.

Rule of thumb: **one `SKIP_LOAD=1` run per candidate patch, one full run per accepted
patch set, and never trust a single sample.** The per-sample stdevs in
`tools/benchmark/fts_large_bench_baseline.json` are ~1.6-2 % for `tokenize_*` but only
0.1-0.3 % for `query_*`, so a change that does not clear roughly 3× that on *several*
repeats is noise.

## 0. Prerequisites (once per build tree)

```bash
./build.sh release --init            # configures build_release/
./build.sh release --make -j32       # or: cmake --build build_release -j32
# CI expects the binary at:
ls -l build_release/src/observer/seekdb
```
`.github/script/seekdb/compile.sh` is the authoritative build recipe (`PACKAGE_TYPE=release`,
`MAKE_ARGS=-j32`). Use the same build type for before/after — the scoring rule forbids
comparing across differing compile settings.

## 1. Start a throwaway instance

`tools/benchmark/fts_large_bench.sh` drops and recreates the `fts_large_bench` database, so
point it at a dedicated instance.

```bash
python3 .github/script/seekdb/sdb.py start \
    --binary build_release/src/observer/seekdb \
    --base-dir /tmp/ftsbench/instance --port 2881
python3 .github/script/seekdb/sdb.py ready --base-dir /tmp/ftsbench/instance --port 2881
```
(`sdb.py` also provides `stop` / `destroy`; `.github/script/seekdb/mysqltest_for_seekdb.py
run ...` does the same thing for the mysqltest loop.)

## 2. Cheap inner loop — `SKIP_LOAD=1` (tokenize + query only, ~2 min)

Use this for every candidate. It reuses the already-loaded table and the already-built
indexes, and re-runs only the `tokenize_*` and `query_*` metrics.

```bash
# one time, on the baseline binary, WITHOUT SKIP_LOAD:
OUTPUT=/tmp/ftsbench/baseline.txt LABEL=baseline \
  ./tools/benchmark/fts_large_bench.sh

# then, per patch rebuild:
OUTPUT=/tmp/ftsbench/p03.txt LABEL=p03 SKIP_LOAD=1 \
  ./tools/benchmark/fts_large_bench.sh
```
Caveats, both important:
* `SKIP_LOAD=1` reports `build_*_sec` as `N/A`, so `fts_large_bench_score.py` **refuses to
  score it** (it raises on missing metrics). Read `tokenize_*` / `query_*` off the report
  directly, e.g. `grep -E 'tokenize|query_' /tmp/ftsbench/p03.txt`, or diff two reports.
* The indexes it measures were built by the *previous* binary. That is fine for tokenize
  and query, and it is exactly what makes the loop fast — but it means a build-path patch
  (F1/F3) is invisible until you do step 3.
* Do not change `ROWS/BATCH/ROUNDS/QUERY_ROUNDS/SAMPLES/WARMUP` if you want the score
  script to accept the report; it hard-checks `benchmark_config` and exits on mismatch
  (use `--allow-config-mismatch` only for a deliberate quick-and-dirty probe).
* Reports append. Use a fresh `OUTPUT` file per run.
* Rebuild, restart the instance, and re-run 2-3 times; the very first `SKIP_LOAD=1` run
  after a restart pays dictionary/KV-cache warm-up that later runs do not.

## 3. Full run + score (build + tokenize + query)

Only after a patch survives step 2, on a freshly loaded table:

```bash
OUTPUT=/tmp/ftsbench/after.txt LABEL=after ./tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py /tmp/ftsbench/after.txt
python3 tools/benchmark/fts_large_bench_score.py /tmp/ftsbench/after.txt --json
```
The score is `clamp(mean(build_impr, tokenize_impr, query_impr), 0, 0.5) / 0.5 * 100`, with
regressions entering as negative per-metric improvements — check the per-metric lines, not
just the total, so a build gain is not hiding a query regression.
Compare `*_hits` between the baseline and after reports: `query_cn_hits=8001`,
`query_beng_hits=11000`, `query_mixed_hits=7332`, `query_limit_hits=20`. **The hit counts
are a correctness gate** — a faster query with different counts is a failed patch, not a
win. Also watch `select1_avg_ms` (harness/SQL-layer noise floor) and `raw_load_sec`: if
those moved, the machine moved and the run is void.

## 4. Correctness gate (must pass before any perf number is believed)

Fulltext suite, `tools/deploy/mysql_test/test_suite/fts_index/t/`:

| test | guards |
|---|---|
| `tokenize_function.test` | TOKENIZE output for space/beng/ik/ngram/ngram2, min/max token size, `output:all` doc lengths — the primary gate for patches 01, 02, 03, 04 |
| `cn_word.test` | IK max_word / smart token streams, Chinese output — gate for 02 and 03 |
| `simple_query.test`, `partitioned_simple_query.test` | MATCH semantics, relevance ordering — gate for F2 |
| `basic_dml.test`, `basic_dml_sequel.test`, `load_data.test` | word rows in the fts_index and fts_doc_word aux tables — gate for F3 |
| `create_table_with_fts_index.test`, `create_fts_index_afterward.test`, `online_ddl_on_table_with_fts_index.test`, `offline_ddl_with_fts_index.test`, `drop_index.test` | index build / rebuild / drop paths |
| `parser_properties.test`, `index_view.test` | parser property JSON and dictionary configuration surface |
| `tools/deploy/mysql_test/test_suite/fork_table/t/fork_table_fulltext.test` | fulltext index across fork table |

Cheapest reliable way to run one of them against a running instance:

```bash
mysqltest --host=127.0.0.1 --port=2881 --user=root \
  --test-file=tools/deploy/mysql_test/test_suite/fts_index/t/tokenize_function.test \
  --result-file=tools/deploy/mysql_test/test_suite/fts_index/r/mysql/tokenize_function.result \
  --record   # first, on the baseline binary, to confirm you can reproduce the checked-in result
```
CI's own runner (which discovers every `test_suite/*/t/*.test` and slices them) is
`.github/script/seekdb/mysqltest_slice.sh` / `mysqltest_for_seekdb.py run`; run the whole
`fts_index` suite at minimum, since the patches touch shared tokenizer code.

Stronger than mysqltest for the tokenizer patches: dump
`SELECT tokenize(<each CONTENT_CN and CONTENT_EN template from fts_large_bench_gen.py>, 'ik'|'beng')`
and `... '[{"output":"all"}]'` from the baseline binary into a file, then diff byte-for-byte
against the patched binary. Patches 01-03 must produce **zero** diff.

## 5. Before you touch F1 / F3 / F6 (parser caching, build-path sharing, dict lookup)

Fix the KV-cache key hazard first: `ObDictCacheKey::operator==`
(`src/storage/fts/dict/ob_ft_cache.h:47-52`) ignores `range_id_` although `hash()`
(`:54-60`) includes it, so a bucket collision can return the wrong dictionary range. Every
proposed parser-reuse change increases how often that range lookup matters.

## 6. Suggested measurement order

1. Patch the build first, then profile: `perf record -g` on a
   `SKIP_LOAD=1` `LABEL=probe` run gives per-patch attribution for F1/F4/F5/F7 in one shot
   (`ObIKFTParser::init*`, `ObStopWordChecker::check_stopword`,
   `ObIIKProcessor::process` should all be visible and shrinkable).
2. Accept 04 → 01 → 02 → 03 individually (each on its own, each with step 2 + step 4).
3. Then attack F1 (per-document parser construction) — highest expected payoff on both
   `build_ik_*` and `tokenize_ik`, and the only finding that needs real design work.
4. Then F3 (double tokenization during DDL) for `build_*`, and F2 (TAAT vs DAAT) for
   `query_*`. Both change what gets written or in what order, so neither may be accepted on
   timing alone.

Do not report a number until steps 2-4 of the same patch have been run at least 3 times
with overlapping results.
