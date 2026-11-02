# seekdb FTS benchmark — where the time actually goes

Ref analysed: `origin/2026-competition` @ `5bce9e06d`. All `file:line` refer to blob
contents (`git cat-file -p origin/2026-competition:<path>`), not the checked-out tree.
Nothing here was compiled or profiled; **no speedup is claimed**. Patches 01–04 are
mechanical and provably semantics-preserving; Findings 1–3 are the big levers and are
explicitly marked *measure first*.

Timed paths (`tools/benchmark/fts_large_bench.sh`): build = 3 `ALTER TABLE ADD FULLTEXT
INDEX` over 20k rows; tokenize = `SELECT tokenize(<text>,'ik'|'beng')` 3000×3; query =
4 `SELECT COUNT(*) ... WHERE MATCH(...) AGAINST(...)` 200×3.

## F1. Every IK tokenization rebuilds the parser and re-pins the dictionaries
`ObFTParseHelper::segment()` creates a fresh iterator per call and frees it immediately:
`src/storage/fts/ob_fts_parser_helper.cpp:285` (`parser_desc->segment(&param, iter)`),
`:305` (`free_token_iter`). For IK, `ObIKFTParserDesc::segment`
(`src/storage/fts/ob_ik_ft_parser.cpp:193-213`) allocates an `ObIKFTParser` and runs
`init_dict` (`:231`), `init_ctx` (`:288`), `init_segmenter` (`:310`). Per **document
column** that is: 3 × `ObFTDictHub::load_cache`
(`src/storage/fts/dict/ob_ft_dict_hub.cpp:86` — bucket read-lock + hash lookup) →
`ObFTRangeDict::try_load_cache` (`ob_ft_range_dict.cpp:460`) → per range a container
node plus an `ObDictCache` KV-cache get that pins an `ObKVCacheHandle`; then
`build_dict_from_cache` (`:405`) allocates one `ObFTCacheDict` per range from a
fresh `ObArenaAllocator` member (`ob_ft_range_dict.h:106`. ≈275,900 main-dict entries
(`src/storage/fts/dict/ob_ik_dic.cpp:275944`) ÷ `DEFAULT_KEY_PER_RANGE = 50000` ⇒ ≈6
ranges ⇒ ~8 KV pins + ~10 arena allocs + 5 parser/ctx/processor allocs + 3 bucket locks,
created and destroyed every time.
Paid 80k× per `build_ik_all` (20k rows × 2 cols × 2 aux tables), 20k× per
`build_ik_content`, 9000× for `tokenize_ik`, and once per MATCH query. BENG
(`src/storage/fts/ob_beng_ft_parser.cpp:118-138`) has no dict stage — consistent with
IK build ≈ 2.4× BENG and IK tokenize ≈ 1.8× BENG. `ob_ik_ft_parser.h:101` comments
"no change of dict in one query, so we can pin dict this level", but the pin is
per-document, not per-query.
**Measure first.** Cache one parser per `ObFTParseHelper` and add a text-switch entry
point. Trap: `param.allocator_` is `reuse()`d per row
(`src/sql/das/ob_das_domain_utils.cpp:701`, `:740`), so the cached parser must own its
own arena, not the statement arena.

## F2. MATCH with no projected score is forced onto TAAT materialization
`src/sql/das/iter/ob_das_iter_utils.cpp:1499-1506`: `(!is_func_lookup &&
!need_proj_relevance_score()) ⇒ taat_mode_ = 1`. All four benchmark queries are
`COUNT(*)` + MATCH filter ⇒ TAAT. TAAT then serialises *every posting* through
partitioned row stores and back: `partition_cnt_ = MIN((total_doc_cnt-1)/1000+1, 20)`
⇒ 20 partitions for 20k docs (`src/storage/retrieval/ob_text_taat_iter.cpp:73`,
formula in `ObTextTaaTIter::pre_process`);
`fill_chunk_stores` (`:267-341`) murmurhashes each doc-id and calls
`spill_row_store_add_batch` 20× per batch (20 full scans of the batch skip bitmap);
`load_next_hash_map` (`:364-429`) reads every row back one at a time into 20 hash maps.
That explains `query_beng` (24.3 ms) ≫ `query_cn` (16.6 ms) tracking *posting volume*,
not hit count. A DAAT/loser-tree path already exists and is chosen when relevance *is*
projected (`src/observer/composition/retrieval/ob_das_legacy_tr_merge_iter.cpp:423-431`).
**Measure first**: switching modes reorders BM25 summation, so `ORDER BY relevance` ties
can move — validate with `fts_index/simple_query`, not only timings.

## F3. Documents are tokenized twice per fulltext index during DDL
Normal DML avoids this by having the index aux read the doc_word table
(`DOMAIN_DML_MODE_FT_SCAN`). During index build the mode is forced back to DEFAULT:
`src/sql/das/ob_das_dml_ctx_define.cpp:120`, `:141`, `:263-271`, driven by
`is_main_table_in_fts_ddl_` set at `src/sql/code_generator/ob_dml_cg_service.cpp:3370`.
Both aux iterators therefore call `ObFTDMLIterator::generate_ft_word_rows`
(`src/sql/das/ob_das_domain_utils.cpp:1020`) → `generate_fulltext_word_rows` (`:277`) →
`helper->segment` (`:355`) on the same text. The two aux layouts differ only in column
order (`:322-323`). **Measure first** — biggest single lever on `build_*`, but sharing
one tokenization must keep both aux writers byte-identical. Guards:
`fts_index/basic_dml`, `fts_index/create_fts_index_afterward`.

## F4. (patched) per-token arena inside the shared stop-word checker
`src/storage/fts/ob_fts_stop_word.cpp:79` built a `ObArenaAllocator("ChkStopWord")` for
**every token**. The checker is already a process singleton
(`ob_fts_parser_helper.cpp:202` via `ObFTParseData`), so the benchmark's "stop-word
global singleton" hint is stale. `ObCharset::charset_convert` provably only aliases its
input for same-charset collations (`src/oblib/lib/charset/ob_charset.cpp:2270-2277`,
`convert_flag = 0`), so the arena never allocated. Hits beng
(`src/storage/fts/ob_beng_ft_parser.cpp:152-159`) and space
(`src/storage/fts/ob_whitespace_ft_parser.cpp:180-188`); IK does not set the flag. → **01**

## F5. (patched) IK re-derives the current character 4× per character
`ObIKFTParser::process_one_char` (`ob_ik_ft_parser.cpp:131-145`) receives
`ch/char_len/type`, ignores them, and `ObIIKProcessor::process`
(`ik/ob_ik_processor.cpp:34-47`) re-reads them from the context for each of the 4
processors: 8 redundant accessor calls plus a discarded `classify_first_char` result per
character. No processor moves the cursor (`step_next` is called only from
`process_next_batch`), so passing them through is exact. → **02**

## F6. (patched) IK pins a stop-word dictionary that nothing reads
`init_dict` loaded and pinned `DICT_IK_STOP` per document. Its only reader is the
commented-out probe at `ob_ik_ft_parser.cpp:94-97`, and `get_add_word_flag` (`:223-229`)
never sets the stop-word flag. Removing it drops one of the three per-document dict
setups. → **03**

## F7. (patched) dead per-call objects on both measured entry points
`ObArenaAllocator tmp_parse_alloc` (`src/sql/engine/expr/ob_expr_tokenize.cpp:89`) and
`ObSEArray<ObFTWord,16> tokens` (`ob_das_legacy_tr_merge_iter.cpp:1113`) are unreferenced.
→ **04**. Same site as F1's per-query share: the query text is re-tokenized and its
parser-properties JSON re-parsed (`src/storage/fts/ob_fts_parser_property.cpp:1016-1067`
→ `ObJsonParser::get_tree`) on every DAS open. Also `ob_expr_tokenize.cpp:57` + `:206`
create two `ObEvalCtx::TempAllocGuard`s per call.

## Correctness hazard found while reading (not perf)
`ObDictCacheKey::operator==` (`src/storage/fts/dict/ob_ft_cache.h:47-52`) ignores
`range_id_` while `hash()` (`:54-60`) includes it, so a KV-cache bucket collision can make
`get_dict` hand back the **wrong dictionary range**. Fix this before touching the range
lookup or parser caching (F1/F6), and re-run `fts_index/cn_word`.

## Checked and rejected
`ObFTRangeDict::find_first_char_range` is a linear scan (`ob_ft_range_dict.cpp:381`), but
there are only ≈6 ranges and their bounds are single characters
(`ob_ft_dict_def.h:36-50`, `ob_ft_trie.h:111-131`), so a binary search is noise. The
"pooled/memory-efficient `ObFastSegmentArray`" hint names a type that does not exist
anywhere in the ref (only in the generator's filler text). Likewise `ObFTWordMap` is
created per row (`ob_das_domain_utils.cpp:288-297`), but its bucket count scales with
document length and `ObHashMap` iteration is O(buckets), so hoisting one shared map would
regress short documents. Not shipped.
