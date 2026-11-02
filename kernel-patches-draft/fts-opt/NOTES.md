# Patch notes

All paths are repo-relative on `origin/2026-competition`. Each patch is independent,
applies with `patch -p1` / `git apply -p1` at zero fuzz (verified against pristine blobs),
and ships **no measured speedup claim** — the numbers below are directional only.

Order to try them: 03, 01, 02, 04 (03 has the widest expected reach on the two IK build
metrics; 04 is free).

---

## 01-fts-stopword-check-no-per-token-arena.patch
Files: `src/storage/fts/ob_fts_stop_word.h`, `src/storage/fts/ob_fts_stop_word.cpp`

**Changes.** `ObStopWordChecker::check_stopword` built a
`common::ObArenaAllocator("ChkStopWord")` on line 79 for *every* token. Removed it; the
caller-supplied `common::ObIAllocator &` (already available as `ObAddWord::allocator_`,
the document-scoped arena) is now used only by the branch that genuinely converts. Added
a short-circuit: when source and target collations share a charset, the token bytes are
used directly.

**Why it was pure overhead.** `ObCharset::charset_convert(alloc, in, src, dst, out, 0)`
performs `out = in` whenever `charset_type_by_coll(src) == charset_type_by_coll(dst)`
(`src/oblib/lib/charset/ob_charset.cpp:2270-2277`), i.e. it never allocated. Beng and
space set `ObAddWordFlag::set_stop_word()` (`src/storage/fts/ob_beng_ft_parser.cpp:152-159`,
`src/storage/fts/ob_whitespace_ft_parser.cpp:180-188`); IK/ngram/ngram2 do not, so this
touches `tokenize_beng` and `build_beng_en` only. The "stop-word global singleton" hint in
`fts_large_bench.sh` is already implemented (`ObFTParseData::init_and_set_stopword_list`,
`src/storage/fts/ob_fts_parser_helper.cpp:199-213`).

**Invariant that must not break.** For identical input, `is_stopword` must be bit-for-bit
identical, and the set's case-insensitive matching must still come from `stopword_type_`
(`CS_TYPE_UTF8MB4_GENERAL_CI`, set in `init()` at `:47-51`) — that meta is applied to the
probe word in both branches, unchanged.

**Memory note.** In the cross-charset branch the conversion buffer now lives in the
document arena instead of a per-token arena, so peak arena use grows by at most the
stopword-probed token bytes per document. That branch is unreachable for built-in parsers
(hard-coded utf8mb4), but it is the behavioural difference to look for.

**Focused test.** `tools/deploy/mysql_test/test_suite/fts_index/t/tokenize_function.test`
(lines 32-33, 91-93 exercise `space`/`beng` TOKENIZE output) and
`tools/deploy/mysql_test/test_suite/fts_index/t/basic_dml.test` (beng + space index DML).
Also hand-check that stopwords really are dropped:
`SELECT tokenize('the quick brown fox jumps over the lazy dog', 'beng');` and
`... , 'space');` must match the pre-patch output exactly.

---

## 02-ik-processor-pass-current-char.patch
Files: `src/storage/fts/ik/ob_ik_processor.h`, `src/storage/fts/ik/ob_ik_processor.cpp`,
`src/storage/fts/ob_ik_ft_parser.cpp`

**Changes.** `ObIIKProcessor::process(TokenizeContext&)` now takes the already-decoded
`(ch, char_len, type)` and forwards straight to `do_process`. `ObIKFTParser::process_one_char`
passes the values it was already handed.

**Why.** `process_one_char` (`src/storage/fts/ob_ik_ft_parser.cpp:131-145`) received
`ch/char_len/type` and discarded them; each of the 4 processors re-read both the character
and its type through `TokenizeContext` (`ob_ik_processor.cpp:34-47`) — 8 redundant accessor
calls plus a thrown-away `ObFTCharUtil::classify_first_char` result per character, for every
character of every IK tokenization (`build_ik_all`, `build_ik_content`, `tokenize_ik`, and
query-side IK tokenization of the search string).

**Invariant that must not break.** The values must be the ones the processors would have
read. Proven: `process_one_char` runs immediately after `ctx_->current_char()` /
`ctx_->current_char_type()` in `process_next_batch` (`:162-164`), no processor calls
`step_next()` (grep over `src/storage/fts/ik/*.cpp` shows `step_next` only from
`process_next_batch:172`), and the accessors cannot return `OB_ITER_END` once the driver
already succeeded — so error paths are unchanged. The two accessors stay in place for the
driver; no other caller of `process()` exists (`ob_ik_ft_parser.cpp:141` is the only one).

**Focused test.** `tools/deploy/mysql_test/test_suite/fts_index/t/cn_word.test` (IK max_word
vs smart token streams) and `.../tokenize_function.test`; the strongest check is a
before/after diff of `SELECT tokenize(<bench text>, 'ik')` and
`SELECT tokenize(<text>, 'ik', '[{"output":"all"}]')` for every `CONTENT_CN` template in
`tools/benchmark/fts_large_bench_gen.py`.

---

## 03-ik-skip-unused-stopword-dict.patch
Files: `src/storage/fts/ob_ik_ft_parser.cpp`

**Changes.** `ObIKFTParser::init_dict` no longer calls `init_single_dict(stopword_dict_desc,
cache_stop_)` nor `build_dict_from_cache(stopword_dict_desc, cache_stop_, dict_stop_)`, so
`DICT_IK_STOP` is not loaded or pinned per document. Members and `reset()` cleanup are kept
(`dict_stop_` stays `nullptr`), plus an explanatory comment.

**Why.** Nothing reads `dict_stop_`: its only consumer is the commented-out probe in
`ObIKFTParser::get_next_token` (`:94-97`), and `ObIKFTParserDesc::get_add_word_flag`
(`:223-229`) deliberately sets only `casedown` + `groupby_word`, so `ObAddWord` never
consults a stop-word dictionary for IK either. `git grep` over the whole ref finds no other
reader. This is one of three identical per-document dictionary setups — per setup: an
`ObFTDictHub::load_cache` bucket read-lock + hash lookup, a KV-cache `get_dict` pin per
range, one container node per range, one `ObFTRangeDict` with its own arena and one
`ObFTCacheDict` per range. It also removes the IK stop-word DAT build from the first
tokenization and the per-document pin of a cache entry that could otherwise be evicted.

**Invariant that must not break.** The IK token stream and `doc_length` must be unchanged —
IK tokenization never consulted `dict_stop_`, so it must stay unchanged; if any output
differs, the assumption is wrong and this patch must be dropped. Second invariant: no code
path may dereference `dict_stop_` — re-verify with a grep before building.

**Focused test.** `tools/deploy/mysql_test/test_suite/fts_index/t/cn_word.test`,
`.../tokenize_function.test`, `.../simple_query.test`, plus `.../index_view.test` and
`.../parser_properties.test` (the latter covers the dict/stopword *table* configuration
surface, which is schema-level and untouched here).

---

## 04-drop-dead-per-call-objects-in-fts-entry-points.patch
Files: `src/sql/engine/expr/ob_expr_tokenize.cpp`,
`src/observer/composition/retrieval/ob_das_legacy_tr_merge_iter.cpp`

**Changes.** Delete `ObArenaAllocator tmp_parse_alloc(ObMemAttr("Tmp buffer"))`
(`ob_expr_tokenize.cpp:89`) and `common::ObSEArray<ObFTWord, 16> tokens;`
(`ob_das_legacy_tr_merge_iter.cpp:1113`). Both are unreferenced in their function
(`grep` shows exactly one occurrence each).

**Why.** The first is constructed and destroyed on every `TOKENIZE()` evaluation — the
benchmark runs that 3000 × 3 times per metric. The second is a ~700-byte stack object plus
16 default-constructed keys on every MATCH query's DAS-open, which also does the query
tokenization (`:1108-1131`).

**Invariant.** None — dead code only; the compiler must still build both translation units.

**Focused test.** `tools/deploy/mysql_test/test_suite/fts_index/t/tokenize_function.test`
and `.../simple_query.test`, plus a plain build.
