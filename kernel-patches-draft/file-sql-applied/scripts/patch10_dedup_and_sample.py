"""Patch 10: finish the two option/name details patch 09 left open, with a diagnostic.

Measured after patch 09b (all on the box, real server, graded fixtures):

  read_json(..., format='newline_delimited')  -> the recorded JSONL rows exactly      PASS
  glob('DIR/*')                               -> the four fixtures, sorted           PASS
  read_csv(path, header = true)               -> 3                                   PASS
  read_csv(path, 'xml')                       -> 1064 syntax error                    PASS
  read_parquet(path)                          -> 1235 stated as not implemented       PASS
  enable_file_sql = OFF                       -> 1235                                 PASS
  read_csv(path)                              -> columns id..note and id__8           FAIL (want id__2)
  read_csv(path, sample_size = -1)            -> 1235 rejected                        FAIL (must pass)

1. the duplicate-header suffix still used the column position (i + 1 = 8); count the occurrences
   instead, and let a header that was already renamed to id__N still count as an `id`;
2. sample_size = -1 is refused, so the negative literal is not arriving as -1. Accept whatever the
   node denotes as negative (numeric sign or a leading '-' in its text) and log the node's fields,
   so if this is still wrong the next step is evidence rather than another guess.
"""

CPP_PATH = "src/sql/file_scan/ob_file_scan_utils.cpp"
RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

DEDUP_OLD = u'''          for (int64_t k = 0; k < cols.count(); ++k) {
            const ObString &earlier = cols.at(k).name_;
            bool same_base = false;
            if (earlier.length() == col.name_.length()) {
              same_base = (0 == earlier.case_compare(col.name_));
            } else if (earlier.length() > col.name_.length()
                       && '_' == earlier[col.name_.length()]
                       && '_' == earlier[col.name_.length() + 1]) {
              same_base = (0 == strncasecmp(earlier.ptr(), col.name_.ptr(), col.name_.length()));
            }
            if (same_base) {
              char *buf = nullptr;
              int64_t len = 0;
              if (OB_ISNULL(buf = static_cast<char *>(io_alloc_.alloc(col.name_.length() + 16)))) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
              } else {
                len = snprintf(buf, col.name_.length() + 16, "%.*s__%ld",
                               col.name_.length(), col.name_.ptr(), static_cast<long>(i + 1));
                col.name_.assign_ptr(buf, static_cast<int32_t>(len));
              }
              break;
            }
          }'''

DEDUP_NEW = u'''          {
            int64_t occurrences = 0;
            for (int64_t k = 0; k < cols.count(); ++k) {
              const ObString &earlier = cols.at(k).name_;
              bool same_base = false;
              if (earlier.length() == col.name_.length()) {
                same_base = (0 == earlier.case_compare(col.name_));
              } else if (earlier.length() > col.name_.length()
                         && '_' == earlier[col.name_.length()]
                         && '_' == earlier[col.name_.length() + 1]) {
                // an already renamed column (id__2) is still an occurrence of its base name
                same_base = (0 == strncasecmp(earlier.ptr(), col.name_.ptr(), col.name_.length()));
              }
              if (same_base) {
                ++occurrences;
              }
            }
            if (occurrences > 0) {
              char *buf = nullptr;
              int64_t len = 0;
              if (OB_ISNULL(buf = static_cast<char *>(io_alloc_.alloc(col.name_.length() + 16)))) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
              } else {
                len = snprintf(buf, col.name_.length() + 16, "%.*s__%ld",
                               col.name_.length(), col.name_.ptr(),
                               static_cast<long>(occurrences + 1));
                col.name_.assign_ptr(buf, static_cast<int32_t>(len));
              }
            }
          }'''

SAMPLE_OLD = u'''    } else if (T_FILE_TABLE_SAMPLE_SIZE == opt->type_) {
      // CSV/JSONL use full-file inference, so sample_size accepts -1 and nothing else
      bool is_minus_one = (opt->num_child_ >= 1 && OB_NOT_NULL(opt->children_[0])
                           && -1 == opt->children_[0]->value_);
      if (!is_minus_one) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "sample_size other than -1");
        LOG_WARN("sample_size rejected", K(ret), K(opt->type_));
      }'''

SAMPLE_NEW = u'''    } else if (T_FILE_TABLE_SAMPLE_SIZE == opt->type_) {
      // sample_size = -1 means "look at every row", which is exactly what full-file inference does,
      // so any negative value is accepted and a positive one (sampling) is not implemented
      const ParseNode *value_node = (opt->num_child_ >= 1) ? opt->children_[0] : NULL;
      const int64_t sample = (OB_NOT_NULL(value_node)) ? value_node->value_ : 0;
      const bool negative = (sample < 0)
                            || (OB_NOT_NULL(value_node) && OB_NOT_NULL(value_node->str_value_)
                                && value_node->str_len_ > 0 && '-' == value_node->str_value_[0]);
      LOG_TRACE("file table sample_size argument", K(sample), K(negative),
                "value_type", OB_ISNULL(value_node) ? -1 : static_cast<int64_t>(value_node->type_),
                "value_str_len", OB_ISNULL(value_node) ? -1 : value_node->str_len_,
                "value_children", OB_ISNULL(value_node) ? -1 : value_node->num_child_);
      if (!negative) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "sample_size other than -1");
        LOG_WARN("sample_size rejected", K(ret), K(opt->type_), K(sample));
      }'''


def apply_edit(doc, old, new, label):
    hits = doc.count(old)
    if 1 != hits:
        raise SystemExit("[ABORT] %s matched %d times, expected 1" % (label, hits))
    return doc.replace(old, new, 1)


def main():
    docs = {}
    for path, old, new, label in ((CPP_PATH, DEDUP_OLD, DEDUP_NEW, "dedup block"),
                                  (RES_PATH, SAMPLE_OLD, SAMPLE_NEW, "sample_size branch")):
        with open(path, "rb") as handle:
            raw = handle.read()
        crlf = raw.count(b"\r\n")
        text = raw.decode("utf-8").replace("\r\n", "\n")
        docs[path] = [apply_edit(text, old, new, label), crlf]
    for path, (text, crlf) in docs.items():
        out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(out)
        print("[OK] %-52s crlf=%d bytes=%d" % (path, crlf, len(out)))


if "__main__" == __name__:
    main()
