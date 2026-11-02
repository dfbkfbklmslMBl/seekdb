# GAP.md — 赛题二 “文件即数据库” (read_csv / read_json / read_parquet) vs `origin/2026-competition`

Read this first:

* **Nothing in this document was compiled or executed.** What is missing on this machine, measured
  with `command -v`: MSVC `cl`, `bison`, `flex`, `win_flex_bison`, and every Python flavour. Note
  that this is *not* “no build path”: seekdb ships a Windows x64 CMake path (`build.ps1:1-6`) and a
  Windows parser generator (`src/sql/parser/gen_parser_win.ps1`) — see PLAN.md §1, because that
  changes what is verifiable locally. Every claim below is either **PROVEN** (I read the bytes with
  `git cat-file -p origin/2026-competition:<path>`, or decoded a fixture with a Node script I wrote)
  or **INFERRED** (my reasoning from what I read). Tags are explicit at each claim.
* All code references are to the **ref** `origin/2026-competition` (sha `5bce9e06d12`), not to the
  checked-out working tree. The working tree is 3 local commits ahead of that ref on unrelated
  files (`src/sql/rewrite/ob_transform_pre_process.cpp`, `test_suite/vector_index/...`), and I
  verified byte-for-byte (md5) that the four files this analysis and the patch touch are identical
  between worktree and ref.
* Line numbers are from the ref blobs. Those blobs are **LF**, not CRLF — I measured it
  (`CR=0` in `sql_parser_mysql_mode.y`, `non_reserved_keywords_mysql_mode.c`, `ob_item_type.h`,
  `ob_dml_resolver.cpp`). The `.gitattributes` `filter=codeformat` covers only
  `*.c *.cc *.cpp *.h *.hpp *.i *.ic *.js` — `.y`/`.l` are not filtered — and no
  `filter.codeformat.*` is even defined in this clone, so the filter is a no-op here.

---

## 0. Headline verdict

**The feature is 100 % greenfield in `src/`. Zero lines of implementation exist.**
**The test assets, however, are fully shipped — including the expected `.result`, which is a
complete output oracle for 8 of the 8 sub-questions.**

PROVEN — `git grep` over the ref returns hits *only inside* `tools/deploy/mysql_test/test_suite/file_sql/`:

| searched symbol | hits anywhere in `src/` on the ref |
|---|---|
| `enable_file_sql` | **0** (only the .test and .result) |
| `read_csv` | **0** |
| `read_json` / `read_ndjson` | **0** |
| `read_parquet` | **0** |
| `glob` as a SQL function | **0** (see §1.4 — a private POSIX `glob()` helper exists in the LOAD DATA resolver) |
| `T_FILE_TABLE*` / `PHY_FILE_SCAN` / `ObFileScan*` | **0** |
| `COPY ... TO` as a statement | **0** (`COPY` exists only as the `ALTER TABLE ALGORITHM=COPY` keyword) |

So this is not “find the missing piece of a half-done feature”; it is “build a feature from zero,
against a frozen acceptance test that is already in the tree”.

That reframes the plan completely: the *shape* of the deliverable is fixed by
`file_sql_basic.test` + `file_sql_basic.result`, and the only unknowns are the hidden tests the
spec admits are not covered publicly (“当前基础测试未覆盖全部评分用例，JOIN、安全和异常路径等需补充专项测试”).

---

## 1. Existence audit of the test entry points named by the spec

### 1.1 `tools/deploy/mysql_test/test_suite/file_sql/t/file_sql_basic.test` — EXISTS (8 files total in the suite)

```
tools/deploy/mysql_test/test_suite/file_sql/data/basic.csv
tools/deploy/mysql_test/test_suite/file_sql/data/basic.jsonl
tools/deploy/mysql_test/test_suite/file_sql/data/invalid.txt
tools/deploy/mysql_test/test_suite/file_sql/data/simple.parquet.b64
tools/deploy/mysql_test/test_suite/file_sql/include/copy_file_sql_data.inc
tools/deploy/mysql_test/test_suite/file_sql/include/remove_file_sql_data.inc
tools/deploy/mysql_test/test_suite/file_sql/r/mysql/file_sql_basic.result
tools/deploy/mysql_test/test_suite/file_sql/t/file_sql_basic.test
```

Nothing else. There is no `file_sql` entry in any suite list to update; PROVEN that
`tools/deploy/mysql_test/` outside `test_suite/` contains only `include/*.inc` helpers
(I listed it), and mysqltest discovers suites by `t/` + `r/mysql/` directory layout, the same way
`load_data` works (`tools/deploy/mysql_test/include/copy_data_for_load.inc:7` uses the identical
`ssh $HOST_IP ... scp ... $OBSERVER_DIR/...` idiom that `copy_file_sql_data.inc:7-9` copies).
**INFERRED:** no harness registration is required; the suite is picked up automatically.

### 1.2 fixtures + include scripts — EXISTS, semantics decoded

PROVEN, `copy_file_sql_data.inc` (whole file):
* connects via `$OBMYSQL_MS0`, reads `select host_ip()` (that expression **does** exist:
  `src/sql/engine/expr/ob_expr_host_ip.cpp:54`, keyword `N_HOST_IP` at
  `src/oblib/lib/ob_name_def.h:719`);
* `rm -rf` + `mkdir -p` `$OBSERVER_DIR/data_for_mysqltest_file_sql`;
* `scp` the four data files;
* `base64 -d simple.parquet.b64 > simple.parquet` and **deletes the `.b64`** — which is why
  `glob()` must return exactly 4 files (result lines 32-35), not 5.
* `remove_file_sql_data.inc` `rm -rf`s only that directory.
* `$OBSERVER_DIR` is a real mysqltest variable (same use in `copy_data_for_load.inc:7`). **PROVEN.**

PROVEN fixtures, byte level:

* **`basic.csv`** (202 bytes, **LF only — there is not a single `\r` in the file**):
  ```
  id,name,score,active,day,created,note,id        <-- duplicate last header "id"
  1,Alice,98.5,true,2026-08-01,2026-08-01 10:11:12,"hello,\nworld",101
  2,Bob,87,false,,,plain,102                      <-- two empty fields => NULL
  3,Chen,90.25,true,2026-08-03,2026-08-03T12:13:14,"say ""hi""",103
  ```
  So the public CSV fixture exercises: header row, duplicate header, RFC4180 quoted field with an
  embedded **newline** and an **embedded comma**, doubled-quote escaping, and empty→NULL.
  It does **not** exercise CRLF, bad-row diagnostics, or batching (all hidden-test-only).
* **`basic.jsonl`** — three flat objects, key union `{id,name,score,active,day,note}`, missing keys
  per line, `score` present as `98.5` (line 1) and `90` (line 3) → an int/double **type conflict**.
* **`invalid.txt`** — `this file has an unsupported extension`. Never queried; only appears in
  `glob()` output (result line 34). **PROVEN** it is in the expected glob list, so `glob()` must
  *not* filter by extension.
* **`simple.parquet.b64`** → 461-byte Parquet file, fully decoded in §5 below.

### 1.3 `unittest/sql/parser/test_file_sql_parser.cpp` — **DOES NOT EXIST**

### 1.4 `unittest/sql/engine/basic/test_file_scan_utils.cpp` — **DOES NOT EXIST**

Stronger than that: **the entire `unittest/` tree on this ref contains exactly two files**, both
ignore-files:

```
unittest/.gitignore              # content: /run_tests.log
unittest/sql/resolver/.gitignore # content: /log  /out
```

PROVEN (`git ls-tree -r --name-only origin/2026-competition -- unittest | wc -l` → `2`), and the
whole repo has only **three** `.cpp` files matching `test_*.cpp` / `*_test.cpp` (PROVEN, I
enumerated rather than assuming):

```
deps/oblib/unittest/lib/alloc/test_jemalloc_hook.cpp
deps/oblib/unittest/lib/allocator/test_malloc_backend.cpp
tools/ob_error/test/test_ob_error.cpp
```

`CMakeLists.txt:327-329` only adds the subdir **if `unittest/CMakeLists.txt` exists**:

```cmake
   AND EXISTS "${CMAKE_SOURCE_DIR}/unittest/CMakeLists.txt")
  enable_testing()
  add_subdirectory(unittest EXCLUDE_FROM_ALL)
```

But `CMakeLists.txt:94-101` proves the harness **expects** a per-module Bazel test target:

```cmake
foreach(SEEKDB_PRETEST_MODULE
    oblib share storage sql rootserver observer logservice pl query data_plane)
  set(SEEKDB_PRETEST_BUILD_FILE
    "${CMAKE_SOURCE_DIR}/unittest/${SEEKDB_PRETEST_MODULE}/BUILD.bazel")
  if(EXISTS "${SEEKDB_PRETEST_BUILD_FILE}") ...
```

and `CMakeLists.txt:325` comments “Other unit-test workflows and their architecture rules stay
**Bazel-owned**”, while `.akfarm:1` lists `pretest` as the first farm job.

**Conclusion (PROVEN structure, INFERRED consequence):** the `unittest/` tree was deliberately
stripped from this branch and the grading harness **restores it**, including
`unittest/sql/BUILD.bazel`. So the two spec-named unit tests will arrive with their own build
targets — writing `unittest/CMakeLists.txt` myself would fight that. What actually matters is that
the implementation exposes the *pure, linkable* helpers their tests will call by name
(`test_file_scan_utils.cpp` implies a `file_scan_utils` unit: CSV option parsing, JSONL field-union,
file-change detection, Parquet encoding + Snappy). **Design those as standalone functions/structs
with no session dependency, or the hidden unit tests cannot link them at all.**

---

## 2. What the acceptance driver demands, line by line

`file_sql_basic.test` (69 lines) → `file_sql_basic.result` (40 lines). The `.result` is checked-in
and unambiguous, so this table is the real spec.

| test line | statement (abridged) | expected | notes on what it constrains |
|---|---|---|---|
| 12 | `select @@session.enable_file_sql AS default_enabled;` | result:2-3 `default_enabled` / `0` | a real **SESSION** system variable named `enable_file_sql`, default OFF, readable through `@@session.` |
| 14-15 | `--error 1235` `select count(*) from read_csv('...basic.csv')` | — | with the gate OFF the query must **parse successfully** and then fail with `ER_NOT_SUPPORTED_YET` = **1235**. PROVEN mapping: `src/share/ob_errno.def:77` `DEFINE_ERROR_EXT_DEP(OB_NOT_SUPPORTED, -4007, ER_NOT_SUPPORTED_YET, ...)`, `src/share/mysql_errno.h:240` `#define ER_NOT_SUPPORTED_YET 1235` |
| 18 | `set session enable_file_sql = ON;` | — | `SET SESSION` on that var must work |
| 22 | `select count(*) from read_csv(...)` (result log off) | — | succeeds once enabled |
| 23-24 | `--error 1064` `select * from read_csv('...basic.csv', 'xml')` | — | **a second positional argument must be a PARSE error.** 1064 = `ER_PARSE_ERROR` (`src/share/mysql_errno.h:69`), which OB reaches via `OB_ERR_PARSE_SQL` (`src/share/ob_errno.def:478`) — i.e. bison `yyerror`, **not** a resolver error. Note `OB_ERR_PARSER_SYNTAX` (`ob_errno.def:481`) maps to `ER_SYNTAX_ERROR` = **1149**, so returning the “normal” resolver syntax error here fails the test. This is the single most under-specified constraint in the whole task and it forces the grammar design in §3. |
| 25-26 | `--error 1235` `select * from read_csv(..., sample_size = 10)` | — | named-option syntax **parses**; a disallowed *value* is `OB_NOT_SUPPORTED`. Confirms “`sample_size` only supports -1” is a resolve/execute-time check, not a grammar check |
| 30-33 | `select id,name,score,active,day,created, replace(note,char(10),'<NL>') AS note, id__2 from read_csv(...) f order by id` | result:6-9 | alias `f`; dynamic columns resolvable by bare name; **duplicate header renamed `id`→`id__2`**; BOOLEAN printed `1`/`0`; DATE/DATETIME NULL for row 2; expression (`replace`/`char`) over a scanned VARCHAR |
| 34-36 | `select count(*), sum(id), round(avg(score),2), sum(active) from read_csv(..., header = true, sample_size = -1) f` | result:10-11 `3 6 91.92 2` | `header = true` and `sample_size = -1` accepted; `COUNT/SUM/AVG/MIN/MAX` all through seekdb’s own aggregate ops. Arithmetic check (PROVEN from the fixture): (98.5+87+90.25)/3 = 91.9166… → 91.92 ✓; `sum(active)` = 2 means BOOLEAN coerces to numeric 1/0 ✓ |
| 39-41 | `select id,name,score,active,day,note from read_json('...basic.jsonl', format = 'newline_delimited') f order by id` | result:13-16 | `format = '<string>'` named option; **key union in first-seen order** = id,name,score,active,day,note exactly as fixture line order; missing→NULL; `score` prints `90` on line 3 — see the ambiguity note in §4.3 |
| 44 | `describe select * from read_csv('...basic.csv')` | result:18-26 | **DuckDB-shaped result-schema output**, headers `column_name column_type null key default extra`, 8 rows, types exactly `BIGINT VARCHAR DOUBLE BOOLEAN DATE DATETIME VARCHAR BIGINT`, nullability `NO/YES/NO/NO/YES/YES/NO/NO` |
| 47-49 | `select count(*), min(c1), max(c1), sum(c1) from read_parquet('...simple.parquet') p` | result:28-29 `7 0 6 21` | exactly reproduced by my independent decode of the fixture (§5) → the `.result` is genuinely derived from this file |
| 52-54 | `select substring_index(file, '/', -1) AS file from glob('...data_for_mysqltest_file_sql/*') order by file` | result:31-35 `basic.csv basic.jsonl invalid.txt simple.parquet` | `glob()` returns **one column literally named `file`**, full paths with `/` separators, no extension filtering, sorted. `substring_index` exists (`src/oblib/lib/ob_name_def.h:427`) |
| 57-61 | `copy (select id,name,score from read_csv(...) order by id) to '...export.csv' (format csv, header, nullstr 'NULL')` | (no output) | new top-level `COPY` statement; **PostgreSQL-flavoured option syntax** (`format csv` — bare word, no `=`; `header` — bare flag, no `= true`; `nullstr 'NULL'` — bare keyword + string), which is a *different* option grammar from the `key = value` options of `read_*` |
| 62-64 | `select id,name,score from read_csv('...export.csv') order by id` | result:37-40 | COPY output must be re-readable by `read_csv`, and header inference on the export must yield 3 columns |
| 66 | `set session enable_file_sql = OFF;` | — | reversible |

Note the ordering trap that is **PROVEN** from the fixture + include scripts: `glob()` runs at
test line 53, i.e. **before** `COPY` creates `export.csv` at line 60, which is why the expected glob
list has 4 entries. If a re-run leaves `export.csv` behind (the cleanup `.inc` only deletes the
whole directory, which is correct), glob output would gain a 5th row and the test fails. So COPY
must write *inside* the fixture directory and cleanup must keep working.

---

## 3. Item 1 — “SQL 接口与文件发现” (7 points)

### 3.1 What already exists (PROVEN, reusable scaffolding)

The `FROM` clause already has **three** precedent productions for “a function-shaped thing that
produces a relation”, all wired end-to-end. A fourth is the intended insertion point.

* Grammar — `table_factor` alternatives, `sql_parser_mysql_mode.y:10790-10851`:
  `json_table_expr` (:10834), `unnest_expr` (:10843), `hybrid_search_expr` (:10847), terminating `;` (:10851).
  `%type` for them at :500 and :513.
  Models to copy: `json_table_expr` :16627-16655, `unnest_expr` :17431-17452,
  `hybrid_search_expr` :17454-17469 (the closest fit — `NAME '(' literal ',' param ')' [relation_name]`).
* Keyword plumbing — `%token <non_reserved_keyword>` block :260-373; `unreserved_keyword_normal:`
  list :17493-18184; keyword→token table `non_reserved_keywords_mysql_mode.c:28-961`
  (`{"json_table", JSON_TABLE}` :393, `{"unnest", UNNEST}` :955, `{"hybrid_search", HYBRID_SEARCH}` :960 —
  appended, not alphabetical; the header comment at :27 explicitly says lexicographic order is not
  required). `sql_parser_mysql_mode.l` needs **no** change (0 hits for `HYBRID_SEARCH`/`JSON_TABLE` there).
* Item types — `src/query/api/query/parser/ob_item_type.h` is the one the parser generator reads
  (`gen_parser.sh:13 ITEM_TYPE_HEADER="$CURDIR/../../query/api/query/parser/ob_item_type.h"`).
  Highest id **PROVEN**: `T_FLUSH_PRIVILEGES = 4920` (line 2277), `T_MAX` at 2278.
  A second, drifted copy exists at `src/objit/include/objit/common/ob_item_type.h` whose max is
  `T_MERGE_TABLE = 4919` — i.e. it already lags by one; they are not both compiled into one TU
  (PROVEN: `src/sql/parser/ob_item_type.h` is just `#include "query/parser/ob_item_type.h"`).
* Resolver dispatch — `ObDMLResolver::resolve_table` switch, `ob_dml_resolver.cpp:2648-2732`,
  cases `T_RELATION_FACTOR` :2649, `T_SELECT` :2654, `T_JOINED_TABLE` :2688,
  `T_TABLE_COLLECTION_EXPRESSION` :2697, `T_JSON_TABLE_EXPRESSION` :2704, `T_UNNEST_EXPRESSION` :2708,
  `T_HYBRID_SEARCH_EXPRESSION` :2715, `T_VALUES_TABLE_EXPRESSION` :2722, `default: OB_ERR_PARSER_SYNTAX` :2727-2730.
  Reference implementations to imitate: `resolve_json_table_item` :3769, `resolve_unnest_item` :3508,
  `resolve_hybrid_search_item` :3692, `resolve_function_table_item` :3926.
* `TableItem` — `ob_dml_stmt.h:180-193` `enum TableType { BASE_TABLE, ALIAS_TABLE, GENERATED_TABLE,
  JOINED_TABLE, CTE_TABLE, FUNCTION_TABLE, TRANSPOSE_TABLE, TEMP_TABLE, JSON_TABLE, VALUES_TABLE,
  LATERAL_TABLE }`; predicates :218-228. **Adding a `FILE_TABLE` member is an enum-in-insert**, which
  `TO_STRING`/serialization in this file may assume append-only — **INFERRED risk**, verify against
  `ObTableItemSerialize`/`encode` in `ob_dml_stmt.cpp` before choosing between insert vs append.
* Column metadata for a function table already has two working shapes:
  `resolve_function_table_column_item` (`ob_dml_resolver.cpp:6727`, `:6768`, sys-func variant :7621-7651,
  which refuses non-nested items with `OB_NOT_SUPPORTED` + `LOG_USER_ERROR("access rows from a
  non-nested table item")` at :7635-7637) and `is_expr_can_be_used_in_table_function`
  (`ob_resolver_utils.cpp:2649`, decl `ob_resolver_utils.h:740`).
  `ObDMLStmt::get_table_function_exprs` `ob_dml_stmt.cpp:1216-1228`, decl `ob_dml_stmt.h:657`.
* Option vocabulary — **big find**: `external_file_format` (`sql_parser_mysql_mode.y:7294-7370`)
  is already a `KEY COMP_EQ value` production list covering `TYPE`, `FIELD_DELIMITER`,
  `LINE_DELIMITER`, `ESCAPE`, `FIELD_ENCLOSED_BY`, `FIELD_OPTIONALLY_ENCLOSED_BY`, `ENCODING`,
  `SKIP_HEADER`, `SKIP_BLANK_LINES`, `TRIM_SPACE`, `NULL_IF_EXETERNAL`, `EMPTY_FIELD_AS_NULL`,
  `COMPRESSION`, `BINARY_FORMAT`, `FILE_EXTENSION`, `PARSE_HEADER`, `IGNORE_LAST_EMPTY_COLUMN`,
  plus `opt_equal_mark` (:1119-ish) and the `INTO OUTFILE ... FORMAT '(' external_file_format_list ')'`
  call site at :8911. Reuse it wholesale instead of inventing a parallel option language.
* Backing C++ struct for those options already exists: `ObCSVGeneralFormat`
  (`src/query/api/query/engine/cmd/ob_load_data_parser.h:38-120`, `OPTION_NAMES[]` :60-77,
  `ObCSVOptionsEnum` :80-100 with a `static_assert` coupling the two, `ObCSVCompression` :106-114 =
  NONE/AUTO/GZIP/DEFLATE/ZSTD) and `ObExternalFileFormat` (:944-979, `enum FormatType {
  INVALID_FORMAT=-1, CSV_FORMAT=0, MAX_FORMAT }`).

### 3.2 Gaps for item 1

| # | gap | evidence |
|---|---|---|
| 1.1 | `read_csv/read_json/read_ndjson/read_parquet/glob` are not tokens, not item types, not productions. `HEADER`, `SAMPLE_SIZE`, `NULLSTR`, `CSV`, `NDJSON`, `NEWLINE`, `DELIMITED` are also absent (PROVEN: `grep -c -w` = 0 in both `sql_parser_mysql_mode.y` and `non_reserved_keywords_mysql_mode.c`). | §3.1 |
| 1.2 | The 1064-vs-1235 split (§2, rows 23-24 / 25-26) has no precedent in the tree; it *dictates* that the positional-arg case be rejected by bison. Nothing in the current grammar does that. | test:23-26 + `ob_errno.def:478` vs `:481` |
| 1.3 | `glob()` as a relation: only a **private static POSIX helper** `static int glob(const char*, int, void*, glob_t*)` in `ob_load_data_resolver.cpp:30` (with `#include <glob.h>` at :63, used at :612-617). It is not exposed to SQL and is Windows-conditional. A SQL `glob()` returning a one-column `file` relation is new. | ref read |
| 1.4 | `DESCRIBE SELECT * FROM read_*()` is **semantically in conflict with existing SQL**. PROVEN: `explain_or_desc: EXPLAIN / DESCRIBE / DESC` (`sql_parser_mysql_mode.y:12307-12311`) all collapse to `$$ = NULL`, i.e. **the keyword identity is discarded**, and `DESCRIBE <select_stmt>` already means `EXPLAIN` → `T_EXPLAIN` (`:12290-12303`, macro `GEN_EXPLAN_STMT` `:53`) → `ObExplainResolver::resolve` (`src/sql/resolver/ddl/ob_explain_resolver.cpp:29-75`) → `ObExplainStmt`/`ObExplainLogPlan` → plan rows. The expected `.result` (lines 18-26) is **not** a plan; it is DuckDB’s result-schema table. Also PROVEN: seekdb has **no** `T_DESC_TABLE` (0 hits repo-wide) and no `DESCRIBE <table>` support, so `DESCRIBE` in this kernel is EXPLAIN-only. → Implementing item 1.4 necessarily changes the meaning of `DESCRIBE <select>`, which the spec forbids breaking. The resolution **must** be to recover the keyword identity in `explain_or_desc` (currently thrown away) and only reinterpret `DESCRIBE`/`DESC`, leaving `EXPLAIN` alone. **INFERRED**, and the highest-consequence design decision in the task. |
| 1.5 | DESCRIBE’s `column_type` strings must be exactly `BIGINT/VARCHAR/DOUBLE/BOOLEAN/DATE/DATETIME`. `ob_obj_type_str()` (`src/oblib/common/object/ob_obj_type.cpp:24` → `ob_sql_type_str()` :43) exists but I **could not verify the literal strings it returns** without running it. `src/oblib/common/object/ob_obj_type.h:40` comments `ObIntType = 5, // int64, aka bigint`. **INFERRED**: you will need a small explicit `ObObjType → DuckDB name` table rather than trusting the helper. |
| 1.6 | Alias support (`f`, `p`, bare and `AS`) is free if the new production follows `hybrid_search_expr`/`json_table_expr`, which already model all three spellings. |

Draft patch for 1.1/1.2 (grammar half): `01-file-sql-parser-table-fn.patch` — see PLAN.md §6 for
what it does and does not do, and for the validation I actually performed.

---

## 4. Item 2 — CSV (10 pts), Item 3 — JSONL (7 pts)

### 4.1 Reusable machinery (PROVEN) — this is the good news

Two pieces are close to exactly what the task needs and already live in the kernel:

1. **`ObFileReader`** — `src/sql/engine/cmd/ob_load_data_file_reader.h:55-113`.
   Pure-virtual `read(char*, int64_t count, int64_t &read_size)` (:68), `get_file_size` (:75),
   `seek(int64_t offset)` (:82), `seekable()` (:83), `get_offset()` (:84), `eof()` (:85),
   `readn()` (:94), static `open(ObFileReadParam&, ObIAllocator&, ObFileReader*&)` (:99),
   `destroy()` (:100).
   Implementations: `ObStreamFileReader` (:115), `ObRandomFileReader` (:126, wraps
   `common::ObFileReader file_reader_` :142), `ObPacketStreamFileReader` (:156), and
   **`ObDecompressFileReader`** (:226) layered over `ObDecompressor` (:201, virtual `decompress()`
   :209, `compression_format()` :212) with concrete `ObZlibDecompressor` (:260) and
   `ObZstdDecompressor` (:285).
   **This is a chunked, buffered, seekable, rescannable reader — precisely the primitive the
   “streaming scans, batch fetch + rescan, no whole-file materialisation” constraint needs, and it
   is also what a Parquet reader needs for footer/row-group random access.** Do not write a new one.
2. **CSV line state machine** — `ob_load_data_parser.h:860-903`: `line_no`, `output_line_no`,
   `line_begin`, `blank_line_cnt`, `skip_blank_lines_`, `fields_per_line_`,
   `format_.file_column_nums_`, `HandleBatchLinesParam` and a `handle_one_line(param)` callback,
   ending with `nrows = line_no`. Plus `ObCSVGeneralFormat`/`ObOriginFileFormat` (:906-926) which
   already store `origin_line_term_str_`, `origin_field_term_str_`, `origin_field_escaped_str_`,
   `origin_field_enclosed_str_`, `origin_null_if_str_` — i.e. the exact knobs the CSV sub-question lists.
   `compression_algorithm_from_suffix()` (:939-940) guesses compression from a filename suffix.

### 4.2 CSV gaps

| # | gap | evidence |
|---|---|---|
| 2.1 | The LOAD DATA pipeline is **ingest-to-storage**; nothing turns `ObFileReader` + `handle_one_line` into an `ObOperator` that pushes `ObDatum` rows upward. `ObFunctionTableOp` (`src/sql/engine/basic/ob_function_table_op.h`) is **not** reusable: its `inner_get_next_row_udf/sys_func` + `pl::ObPLCollection *value_table_` + `common::ObObj value_` design is Oracle-mode PL collection/AUX_TABLE-style, single `value_expr_`, one `COLUMN_VALUE` column, and it materialises via `ObChunkDatumStore` includes. It has no notion of a multi-column dynamic schema. **PROVEN from the header; INFERRED conclusion** (a new `FileScanOp` is required — `AGENTS.md` “C++ Type Naming” says new classes do **not** take the `Ob` prefix, and new headers use `#pragma once`). |
| 2.2 | Duplicate-header renaming to `id__2` (result:6, :26) exists nowhere. `resolve_json_table_check_dup_name` (`ob_dml_resolver.cpp:6911`) is a *reject*-on-duplicate precedent, not a rename. The suffix rule `name -> name__2` is only guessable from this one sample; **INFERRED** `_<k>` for the k-th repeat. |
| 2.3 | Header detection: fixture has a header and the option is `header = true` *and* the default (test:30 vs test:36 both infer `id`,`name`,…) → default = header present. **PROVEN** by result:6 using header names as columns. Whether `header=false` must yield `column0..columnN` is not covered publicly → hidden-test surface. |
| 2.4 | Quoted field spanning a newline, quoted comma, `""` escape, empty→NULL: all present in the fixture but nothing in `src/` reads a CSV into typed columns. The `enclosed by / escaped by` state machine for *reading* exists only inside LOAD DATA's ingest path. **INFERRED**: it is salvageable but coupled to `ObLoadDataArg`/column-count, so expect to reimplement a small dedicated tokenizer rather than lift it. |
| 2.5 | Bad-row diagnostics (“坏行诊断”) and CRLF are **not** in the public fixture (LF-only, no bad lines) → hidden tests. The spec names both, so budget for an error-position struct. |
| 2.6 | Batch fetch (“批量扫描”) → `inner_get_next_batch` must be implemented; note `ObFunctionTableOp` has it *commented out* (`ob_function_table_op.h`: `//virtual int inner_get_next_batch(int64_t max_row_cnt) override;`) — **PROVEN**, and it hints that the batch path is fiddly here. |

### 4.3 JSONL gaps

| # | gap | evidence |
|---|---|---|
| 3.1 | No `read_json`/`read_ndjson` anywhere (§0). |
| 3.2 | **Union schema requires two passes over the file** (line 1 has no `day`; line 2 has no `score`; line 3 adds `note`). With `ObFileReader::seek(0)` (`:82`) that is a bounded 2-pass or a schema-deferred rescan — **INFERRED** design, but it must not violate “no full materialisation”: schema keys are small, rows are not retained. |
| 3.3 | Column order is first-seen order: `id,name,score,active,day,note` (result:13) matches fixture line order exactly — PROVEN, and it rules out alphabetical output. |
| 3.4 | Type conflict `score`: 98.5 then missing then 90 → prints `90`. **AMBIGUOUS, PROVEN-ambiguous**: both a DOUBLE column (printing 90.0 as `90`) and a VARCHAR column produce the identical byte string in mysqltest. There is no `describe select * from read_json(...)` in the public test, so JSONL’s inferred types are **unobservable** publicly. **This is a real trap: pick wrong and hidden tests lose points with no public signal.** §5/§6 of the spec says CSV/JSONL use “全量 Schema 推断”, and the widening rule for int+double in a single column is DOUBLE — INFERRED, and I would implement DOUBLE. |
| 3.5 | Reject nested objects and arrays (spec) — no existing helper does that check for a table function; JSON *parsing* itself exists (`src/oblib/common/json_type/ob_json_base.*`, referenced by `src/oblib/common/object/ob_obj_type.cpp` includes and used across exprs) but **I did not read its API**, so treat “reuse the OB JSON parser” as INFERRED. |
| 3.6 | `format = 'newline_delimited'` — the string value must be recognised; `'json'`/other spellings are unknown. Also **`read_json` and `read_ndjson` must be aliases** per the spec title but only `read_json` appears in the public test → `read_ndjson` is entirely hidden-test surface. |

---

## 5. Item 4 — Parquet (6 pts): the riskiest dependency, fully characterised

I decoded the shipped fixture rather than guessing. **PROVEN** (via Node scripts I wrote: a
Thrift-compact struct reader + a Snappy raw-format decoder, both run against
`simple.parquet` = the base64 of `simple.parquet.b64`):

```
461 bytes total. "PAR1" magic at [0,4) and [457,461); footer length 334 at [453,457) (LE 0x0000014E);
FileMetaData thrift at [119,453).

FileMetaData: version=2, num_rows=7, created_by="Arrow2 - Native Rust implementation of Arrow",
              key_value_metadata=[ "ARROW:schema" -> base64 Arrow IPC schema ]
schema:   root (num_children=1) -> c1 : type=1 (INT32), repetition_type=1 (OPTIONAL, i.e. nullable)
row_group x1: num_rows=7, total_byte_size=70
ColumnChunk: file_offset=74
ColumnMetaData: type=INT32, encodings=[3,0] = [RLE, PLAIN], path=["c1"],
                codec=1  ==>  SNAPPY            <-- the load-bearing fact
                num_values=7, total_uncompressed_size=68, total_compressed_size=70,
                data_page_offset=4,
                statistics{null_count=0, max=06 00 00 00, min=00 00 00 00}
PageHeader @ [4,42)  (thrift compact, 38 B): type=3 => DATA_PAGE_V2,
                uncompressed_page_size=30, compressed_page_size=32,
                DataPageHeaderV2{num_values=7,num_nulls=0,num_rows=7,
                                 encoding=PLAIN, def_levels_byte_length=2,
                                 rep_levels_byte_length=0, is_compressed=true, stats{...}}
                (arithmetic: 4 + 38 + 32 = 74 == ColumnChunk.file_offset, and 38+30 == 68
                 total_uncompressed_size, 38+32 == 70 total_compressed_size — self-consistent)
Page body [42,74): definition levels = 03 7f        (RLE/bit-packed, 7 present bits, stored UNCOMPRESSED)
                 values [44,74)     = Snappy block, preamble 0x1c => 28 B uncompressed,
                                     single literal run 0x6c => decoded = int32 LE 0,1,2,3,4,5,6
                 decoded c1 = 0,1,2,3,4,5,6   -> COUNT=7 SUM=21 MIN=0 MAX=6
                 == file_sql_basic.result:29 exactly.
[74,119): a further ~45-byte thrift region (min/max 6/0 repeated, "c1", "root") that I identify as
          an Arrow2-written page index; NOT counted in ColumnMetaData sizes. **INFERRED**; do not rely
          on skipping it without checking against a real reader.
```

Consequences:

| # | fact | status |
|---|---|---|
| 4.1 | **The public Parquet fixture requires Snappy.** My own Snappy decoder reproduces 0..6 and hence the expected `7 / 0 / 6 / 21`. | PROVEN |
| 4.2 | **There is no Snappy anywhere in this repo.** `git grep -i snappy origin/2026-competition -- src deps` → **0 hits.** | PROVEN |
| 4.3 | There is no Thrift, no Arrow, no Parquet, no Flatbuffers, no Brotli, no LZ4 in `deps` (`git ls-tree -r deps | grep -iE 'parquet|arrow|thrift|snappy|zstd|brotli|flatbuf|avro|boost'` → 0 hits; `deps/external/CMakeLists.txt` is the only external CMake). | PROVEN |
| 4.4 | What *is* vendored: **zstd** sources at `src/oblib/lib/compress/zstd_1_3_8/zstd_src/*` plus `ob_zstd_wrapper.{h,cpp}`, and zlib via `ObZlibDecompressor`. So a ZSTD/GZIP Parquet codec is reachable; **Snappy is not**. | PROVEN |
| 4.5 | The spec’s own unittest description lists “Parquet 编码与 Snappy” as expected coverage — i.e. Snappy is a *required* capability, not something you can decline. | from the题面 |
| 4.6 | You must hand-write: Thrift **compact** reader (field-delta headers, zigzag varints, list/struct/binary/i64), the Parquet footer/page-header subset, PLAIN, RLE/bit-packed hybrid for definition levels, Snappy raw decompression, DataPageV1 **and** V2, dictionary pages, multi-row-group, and column pruning — inside seekdb, with no external library. | INFERRED from 4.1-4.5 |
| 4.7 | A Snappy implementation is ~120 lines of well-specified, self-contained code (the format is a public, tiny spec) and is *the* unblocking item for 6 points. It does **not** violate “no external query engine” (Snappy is a codec, not a SQL engine) — INFERRED but I consider it safe, and worth calling out in the PR description to avoid a 0-point misreading. |
| 4.8 | The page framing I observed (no 4-byte page-header length prefix before the thrift header at `data_page_offset=4`) is **not** what the standard Parquet layout says readers should expect. Two readings are possible and my size arithmetic favours the no-prefix reading, but I could not run pyarrow/parquet-tools to settle it. **Flagged as the top thing to re-verify on Linux** (`pip`/venv isn’t available here either). If the prefix *is* standard, the reader must skip 4 bytes and my arithmetic reading is an Arrow2 quirk in this one fixture. |

**Verdict:** Parquet is 6/50 points ≈ 12 %, and its dependency list is the longest in the task.
It is also the only item where the *shipped fixture itself* demands a compression codec the repo
does not contain.

---

## 6. Item 5 — deterministic type inference (6 pts)

Expected target (PROVEN, result:19-26): `id BIGINT NO`, `name VARCHAR NO`, `score DOUBLE NO`,
`active BOOLEAN NO`, `day DATE YES`, `created DATETIME YES`, `note VARCHAR NO`, `id__2 BIGINT NO`.

Cross-checked against the fixture (PROVEN):
* `day`/`created` are `YES` **only** because row 2 has empty fields → nullability is driven by
  observed empties, not by column semantics.
* `score` mixes `98.5 / 87 / 90.25` → DOUBLE (int→double widening).
* `active` is `true/false` → BOOLEAN, yet prints `1/0` in the row projection and sums to 2 → it must
  be stored as a BOOLEAN-typed column that casts to numeric in SUM.
* `note` contains `hello,\nworld` / `plain` / `say "hi"` → VARCHAR.
* `id` `1/2/3` → BIGINT. `id__2` `101/102/103` → BIGINT.
* `created` row 3 is `2026-08-03T12:13:14` (**ISO `T` separator in the CSV!**) yet the output prints
  `2026-08-03 12:13:14` (result:9) → DATETIME parsing must accept the `T` form. PROVEN from bytes,
  and it is a subtle requirement a home-grown regex would miss.

Available helpers (PROVEN existence, INFERRED adequacy):
* `ObTimeUtility2::is_valid_date(int y, int m, int d)` — `src/share/ob_time_utility2.h:79`, impl `.cpp:750`
  (also used at `.cpp:161`, `:558`).
* `ObTimeConverter::is_valid_datetime(...)` — used at `src/share/object/ob_obj_cast.cpp:11279`,
  `:11360`, `src/sql/engine/expr/ob_datum_cast.cpp:10607`, `:10648`.
* `ob_obj_type_str` / `ob_sql_type_str` — `src/oblib/common/object/ob_obj_type.cpp:24`, `:43`.
* Type-class machinery: `src/oblib/common/object/ob_obj_type.h:264` X-macro `ObNumberType→ObNumberTC`,
  `:1197` `VEC_TC_NUMBER`, `:1429` type back-mapping.

Gaps: no inference engine exists; the “widening lattice” (BIGINT→DOUBLE→VARCHAR?), numeric boundary
cases (BIGINT overflow → what?), all-NULL columns, and DATE legality rejection are all unspecified
by the public test. **The public test only pins 8 columns of one CSV.** INFERRED: the lattice the
题面 implies is BIGINT / DOUBLE / BOOLEAN / VARCHAR / DATE / DATETIME / NULL, and everything that
isn’t cleanly one of those collapses to VARCHAR.

---

## 7. Item 6 — single-file SQL (6 pts) and Item 7 — two-file JOIN (4 pts)

**These are the cheapest points in the task, and the argument for building a real relation rather
than a bolt-on.** PROVEN that the engine side already exists and needs nothing:

* `WHERE` / `ORDER BY` / `LIMIT` / `GROUP BY` / `HAVING` / `COUNT,SUM,AVG,MIN,MAX` / expressions
  (`replace`, `char`, `round`, `substring_index`) / aliases are all exercised by the public test
  against a `read_csv(...)` relation (test:30-36, 39-41, 47-49, 52-54, 62-64) and are pre-existing
  seekdb features. Nothing in the test implies the file table needs its own aggregate handling — the
  header comment on the Parquet case even says “aggregate push through projected columns”, but the
  `.result` is satisfied with or without a pushdown.
* Logical/physical operator lists are append-only X-macros, so a new scan node slots in cleanly:
  `src/sql/optimizer/ob_log_operator_factory.h:18-56` `LOG_OP_DEF(...)` (has `LOG_FUNCTION_TABLE` :43,
  `LOG_JSON_TABLE` :44, `LOG_SELECT_INTO` :38, ends `LOG_OP_END` :55);
  `src/query/api/query/engine/ob_phy_operator_type.h:19-123` `PHY_OP_DEF(...)` (`PHY_FUNCTION_TABLE` :95,
  `PHY_JSON_TABLE` :113, `PHY_SELECT_INTO` :56, `PHY_TABLE_SCAN` :22; `PHY_END` :122; header comment
  `ob_phy_operator_type.h:135 /* @note: append only */`), forwarded into `src/sql/engine/` by the
  17-line shim `src/sql/engine/ob_phy_operator_type.h:17-19`.
  Registration macro: `REGISTER_OPERATOR(log_op, type, spec, op, input, extra)` in
  `src/query/api/query/engine/ob_operator_reg.h:68-99` (with `NOINPUT` :53, `MY_SPEC` :111,
  `MY_INPUT` :129).
  `src/sql/code_generator/ob_static_engine_cg.cpp` is the spec-generation side to extend (PROVEN to
  reference `explain`/ops; I did not read its op switch in detail — **INFERRED** it needs a new case).
* For JOINs: `ObLogPlan::init_onetime_subquery_info` (`ob_log_plan.cpp:8186-8240`) already special-cases
  `get_table_function_exprs` / `get_json_table_exprs` so a function-table expression is *not* treated
  as a onetime subquery (:8214-8217). A file table that behaves like `JSON_TABLE` inherits correct
  treatment; a file table registered as a plain relation needs nothing at all. **INFERRED:** once the
  resolver produces a normal `TableItem` with a fixed column set, `INNER JOIN` / `LEFT JOIN` /
  cross-format JOIN come for free from `PHY_HASH_JOIN` / `PHY_NESTED_LOOP_JOIN` / `PHY_MERGE_JOIN`
  (`ob_phy_operator_type.h:24-26`). **Neither JOIN form appears anywhere in the public test** —
  4 points are pure hidden-test exposure, but they need ~0 new code after item 6 works.

Gaps: `enable_file_sql` gate must not disable normal SQL; the scan must expose `inner_rescan`
(NL-index-join and repeated probes need it) and `inner_get_next_batch`; row-count/estimation for a
file table is undefined (optimizer will assume default stats — **INFERRED**, acceptable since the
test forces `order by`/aggregate shapes that plan fine).

---

## 8. Item 8 — safety, stability, engineering quality (4 pts)

PROVEN reusable, and it is better than expected:

* `ObResolverUtils::check_secure_path(const ObString &secure_file_priv, const ObString &full_path)`
  — decl `ob_resolver_utils.h`, impl `ob_resolver_utils.cpp:6050-6114` (+ allowlist fallback loop
  :6115-6140, allowlist array :6046-6048 = `{"log/alert"}`). It **canonicalises with `realpath`
  (`_fullpath` on `_WIN32`)** at :6074/:6077, requires the privilege dir to be a real directory
  (:6065-6068), and enforces **prefix match + separator boundary** (:6082-6111), with an explicit
  Windows branch accepting `\` or `/` and a case-insensitive prefix (`prefix_match_ci`, :6093).
  Empty / `NULL` `secure_file_priv` → `OB_ERR_NO_PRIVILEGE` (:6057-6058).
* Two working call sites to copy: `ObSelectIntoOp::check_secure_file_path`
  (`src/sql/engine/basic/ob_select_into_op.cpp:1201-1228` — splits the path at the last `/` with
  `file_name.split_on(file_name.reverse_find('/'))`, resolves it, reads the var via
  `ObSchemaUtils::get_runtime_varchar_variable(..., SYS_VAR_SECURE_FILE_PRIV, ...)`, converts
  `OB_ERR_NO_PRIVILEGE` → `OB_ERR_NO_PRIV_DIRECT_PATH_ACCESS` :1222-1224) and
  `ob_load_data_resolver.cpp:652-660` (which additionally gates on `!session_info_->is_inner()`).
* `secure_file_priv` default is **`/`** (PROVEN: `ob_system_variable_init.json:2449-2461`, id 10102,
  `data_type: varchar`, `flags: "GLOBAL | INFLUENCE_PLAN | NULL"`), and alias `OB_SV_SECURE_FILE_PRIV`
  (`ob_system_variable_alias.h:156`), getter `ob_basic_session_info.cpp:4083`.
  → **the acceptance test never sets `secure_file_priv`; with the default `/` every absolute path
  passes.** So the public test cannot detect a missing check; the 4 safety points are entirely
  hidden-test territory, and “绕过 secure_file_priv ⇒ 安全项 0 分 + 严重缺陷” makes omitting it the
  worst possible shortcut.
* Error codes: `OB_ERR_NO_PRIV_DIRECT_PATH_ACCESS = -9731` (`ob_errno.def:1810`).

Gaps specific to item 8:

| # | gap |
|---|---|
| 8.1 | **`enable_file_sql` does not exist.** See PLAN.md §4: adding one system variable touches **10 files**, two of them large machine-generated arrays whose indices must stay mutually consistent, and the generator script is **Python 2 only** (`gen_ob_sys_variables.py:67` uses `json_Dict.iteritems()`; `:69-70` too) so it cannot be run even on the Linux grading box. |
| 8.2 | Read-only enforcement: no “file changed while reading” detection exists; you need size+mtime (or inode) captured at schema-inference time and re-verified at rescan. Nothing in `ObFileReader` offers it (`get_file_size` :75 is the only hook). |
| 8.3 | Resource caps: max columns, max field length, max open files, a path-length cap (`PATH_MAX` is already used at `ob_select_into_op.cpp:1205`), and refusing directories. None of this is shared code today. |
| 8.4 | “异常数据…不得导致崩溃” — the `invalid.txt` fixture is only *listed*, never read (PROVEN: no test line opens it), so extension rejection is untested publicly. |
| 8.5 | **`unittest/` is stripped** (§1.3/1.4): the two spec-named test files do not exist, but `CMakeLists.txt:94-101` shows the harness expects `unittest/<module>/BUILD.bazel` for a module list that includes `sql`, and `.akfarm:1` runs `pretest`. So the graders restore that tree — expose pure `file_scan_utils`-style helpers instead of inventing test scaffolding. |
| 8.6 | Path-escape: `check_secure_path` validates the *directory* it was handed; the caller must pass an already-absolute, already-normalised `full_path`. For `read_*` the string is user-supplied, so `..`/symlink escape must be handled by calling `realpath` on the **file** path (the INTO OUTFILE site only resolves the parent dir because the file must not exist yet). **INFERRED**, and worth care: a `realpath` on a non-existent file fails. |

---

## 9. COPY export (unscored, but required by test:57-64 and blocking the item-1 result diff)

PROVEN state:
* **No `COPY` statement exists.** `COPY` appears only as `algorithm_opt: ... | COPY { $$ = NULL; }`
  (`sql_parser_mysql_mode.y:14749`), as an unreserved keyword (`:17607`, and
  `non_reserved_keywords_mysql_mode.c:166 {"copy", COPY}`), and in the keyword comment block `:274`.
  There is no `copy_stmt`, no `T_COPY` stmt, and `src/sql/resolver/ob_resolver.cpp`’s dispatch
  (`:179-333+`, ~150 `case T_*:` entries) has no such case.
* **Everything COPY needs on the write side already works.** `INTO OUTFILE` grammar at
  `sql_parser_mysql_mode.y:8907` (`INTO OUTFILE STRING_VALUE opt_charset field_opt line_opt file_opt`
  → `T_INTO_OUTFILE`) and `:8911` (`INTO OUTFILE STRING_VALUE FORMAT opt_equal_mark '('
  external_file_format_list ')' file_opt`); executor `ObSelectIntoOp`
  (`src/sql/engine/basic/ob_select_into_op.{h,cpp}`) with `ObExternalFileFormat external_properties_`
  + `FormatType format_type_` (`ob_select_into_op.h:291-292`, default `CSV_FORMAT` at `:127`),
  `ObCsvFileWriter : public ObExternalFileWriter` (`ob_external_file_writer.h:60`) with
  `alloc_buf` (:85), `flush_buf`/`flush_shared_buf`/`flush_data`/`flush_to_compress_stream`/
  `flush_to_storage` (:99-103), `open_file`/`close_file` (:44-46, `write_file()` pure virtual :47),
  `get_file_size` (:48); and row serialisation in `src/sql/engine/expr/ob_expr_to_outfile_row.cpp`.
  `ObSelectIntoOp::create_the_only_data_writer` (`:1230-1250`) shows the CSV writer being set up with
  `MY_SPEC.buffer_size_` and `T_INTO_OUTFILE == MY_SPEC.into_type_ && MY_SPEC.is_single_`.
* `ObExternalFileWriter::open_file()` (`ob_external_file_writer.cpp:29-34`) sets
  `is_file_opened_` via `file_appender_`; **I did not find an `O_EXCL`/exists guard**, so the spec’s
  “目标已存在时失败” is a real gap (INFERRED — based on grepping the file for `open|O_RDWR|O_CREAT|
  O_TRUNC|O_EXCL|mode` and seeing only :29/:34/:42/:48/:145/:155/:169, none of which is an excl
  check). Temp-file cleanup on failure is likewise unverified → assume missing.

**INFERRED cheapest design:** parse `COPY <rel> TO '<path>' (<opts>)` and *desugar* into the existing
select-into machinery (`ObSelectIntoStmt` + `T_INTO_OUTFILE` + `external_file_format_list`), mapping
`format csv`→`TYPE='csv'`-equivalent, `header`→`T_PARSE_HEADER`, `nullstr 'NULL'`→`T_NULL_IF_EXETERNAL`.
That turns a new statement type, stmt class, resolver, CG node and operator into a resolver-only
change. The option grammar for COPY is *bare/positional* (`format csv`, `header`, `nullstr 'NULL'`)
and is a separate production from `read_*`’s `key = value` list — do not conflate them.

---

## 10. Open questions / ambiguities I could not resolve from code

1. **JSONL column types are publicly unobservable** (§4.3 / 3.4). Only `describe` on **CSV** is in
   the test. Hidden tests decide DOUBLE-vs-VARCHAR for `score`.
2. **`describe select` vs `EXPLAIN` semantics** (§3.1/1.4) — the collision is real and unresolved by
   the题面; whichever way it is settled must be written into the PR description.
3. **Duplicate-header suffix rule** — one sample (`id`→`id__2`). `__k` vs `_k` vs `k` is a guess.
4. **Whether `--error 1064` for `read_csv(x,'xml')` is meant to be a grammar restriction** or the
   authors intended `format='xml'` and mistyped. I designed to satisfy it literally, because the
   `.result`/`.test` pair is the graded artefact.
5. **Page-index region `[74,119)` in the Parquet fixture** (§5/4.8) and the page-header framing
   question: both need one run of a real reader on Linux.
6. **Where the 50 points come from vs “评分总分沿用原设计的 30 分”** — the题面 header says 50 and the
   table sums to 50, but the same cell says the total stays at 30. Unresolvable from here.
7. **Bazel vs CMake** for any new file: the repo has both (`src/sql/BUILD.bazel`,
   `src/sql/sql_module_sources.bzl`, `sql_header_inventory.bzl`, `bazel_pilot/`). I did not read
   them; every new `.cpp/.h` will need registering in **both** build systems. **INFERRED but high
   confidence** from the presence of `src/sql/*.bzl` inventory files, which exist precisely to
   enumerate sources.
