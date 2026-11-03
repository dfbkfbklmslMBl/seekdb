"""Patch 09: the four bugs the live probe on the box exposed.

Measured on the binary built from patch 08 (md5 1622456a...), against the graded fixtures:

  select count(*) from read_csv('basic.csv')             -> 3                   (row source works)
  select * from read_csv('basic.csv')                    -> id,...,id__8        (want id__2)
  select count(*) from read_csv('basic.csv', header=true)       -> 1235 option not supported
  select count(*) from read_csv('basic.csv', sample_size=-1)    -> 1235 option not supported
  select * from read_parquet('simple.parquet')           -> one "file" column   (ran the GLOB branch)
  select * from glob('DIR/*')                            -> 1017 File not exist (ran the CSV branch)
  select * from read_json('basic.jsonl')                 -> JSON split on commas (no JSONL reader)

Fixes:
  1. the grammar numbers the five table functions 0..4 (READ_CSV, READ_JSON, READ_NDJSON,
     READ_PARQUET, GLOB); the C++ enum had only four entries, so align them and add JSON.
  2. merge_nodes() wraps an option list but leaves a single option unwrapped, so the fixed chain
     walk collected an option's *value* node instead of the option node; flatten instead.
  3. duplicate headers are suffixed by occurrence count (id__2), not by column position (id__8).
  4. read_json/read_ndjson read newline-delimited JSON with a union schema in first-seen key order.

Run from the repo root: python3 ktools/patch09_file_table_fixes.py
"""

H_PATH = "src/sql/file_scan/ob_file_scan_utils.h"
CPP_PATH = "src/sql/file_scan/ob_file_scan_utils.cpp"
RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

NS_END = u"} // namespace sql\n} // namespace oceanbase"

ENUM_OLD = u'''enum ObFileSqlFormat
{
  OB_FILE_SQL_FMT_CSV = 0,
  OB_FILE_SQL_FMT_JSONL = 1,
  OB_FILE_SQL_FMT_PARQUET = 2,
  OB_FILE_SQL_FMT_GLOB = 3,
};'''

ENUM_NEW = u'''// These numbers are the value_ the grammar stores for each table function, so the two lists have
// to stay in the same order: READ_CSV, READ_JSON, READ_NDJSON, READ_PARQUET, GLOB.
enum ObFileSqlFormat
{
  OB_FILE_SQL_FMT_CSV = 0,
  OB_FILE_SQL_FMT_JSON = 1,
  OB_FILE_SQL_FMT_JSONL = 2,
  OB_FILE_SQL_FMT_PARQUET = 3,
  OB_FILE_SQL_FMT_GLOB = 4,
};'''

LOADER_DECL_OLD = u'''  static int load_glob(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                       ObFileSqlTableData &data);'''

LOADER_DECL_NEW = u'''  static int load_glob(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                       ObFileSqlTableData &data);
  static int load_jsonl(common::ObIAllocator &alloc, const ObFileSqlScanParam &param,
                        ObFileSqlTableData &data);'''

DEDUP_OLD = u'''          // duplicate headers get a DuckDB-style suffix instead of overwriting
          for (int64_t k = 0; k < cols.count(); ++k) {
            if (0 == cols.at(k).name_.case_compare(col.name_)) {'''

DEDUP_NEW = u'''          // duplicate headers get a DuckDB-style suffix instead of overwriting: the second `id`
          // becomes `id__2`, so the suffix counts how many times the header was already seen
          for (int64_t k = 0; k < cols.count(); ++k) {
            const ObString &earlier = cols.at(k).name_;
            bool same_base = false;
            if (earlier.length() == col.name_.length()) {
              same_base = (0 == earlier.case_compare(col.name_));
            } else if (earlier.length() > col.name_.length()
                       && '_' == earlier[col.name_.length()]
                       && '_' == earlier[col.name_.length() + 1]) {
              same_base = (0 == strncasecmp(earlier.ptr(), col.name_.ptr(), col.name_.length()));
            }
            if (same_base) {'''

DISPATCH_OLD = u'''  if (OB_FILE_SQL_FMT_GLOB == param.format_) {
    ret = load_glob(alloc, param, data);
  } else if (OB_FILE_SQL_FMT_PARQUET == param.format_) {'''

DISPATCH_NEW = u'''  if (OB_FILE_SQL_FMT_GLOB == param.format_) {
    ret = load_glob(alloc, param, data);
  } else if (OB_FILE_SQL_FMT_JSON == param.format_ || OB_FILE_SQL_FMT_JSONL == param.format_) {
    ret = load_jsonl(alloc, param, data);
  } else if (OB_FILE_SQL_FMT_PARQUET == param.format_) {'''

INCLUDE_OLD = u'#include "common/timezone/ob_time_convert.h"'

INCLUDE_NEW = (u'#include "common/timezone/ob_time_convert.h"\n'
               u'#include "common/json_type/ob_json_base.h"\n'
               u'#include "common/json_type/ob_json_tree.h"\n'
               u'#include <strings.h>')

JSONL_IMPL = u'''
// Newline-delimited JSON: one object per line, the union of the keys in first-seen order, and a
// key a line does not carry becomes NULL. Values are kept in their JSON text form so the same
// widening rules that read CSV also decide the column type here.
int ObFileSqlLoader::load_jsonl(ObIAllocator &alloc, const ObFileSqlScanParam &param,
                                ObFileSqlTableData &data)
{
  int ret = OB_SUCCESS;
  const int64_t MAX_JSONL_BYTES = 128LL * 1024 * 1024;
  ObSEArray<ObString, 32> keys;
  ObSEArray<ObFileSqlInferCol, 32> inferred;
  ObFileSqlReader opener(alloc);
  int64_t file_size = 0;
  if (OB_FAIL(opener.init(param))) {
    LOG_WARN("failed to open the json lines file", K(ret), K(param));
  } else {
    file_size = opener.get_file_size();
    opener.destroy();
    char *whole = NULL;
    int64_t read_len = 0;
    if (file_size > MAX_JSONL_BYTES) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "a json lines file larger than 128MB as a table");
    } else if (file_size <= 0) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "an empty json lines file as a table");
    } else if (OB_ISNULL(whole = static_cast<char *>(alloc.alloc(file_size + 1)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      int fd = -1;
      // the path needs a NUL-terminated copy for the syscalls; the buffer above holds the content
      char *path_buf = NULL;
      if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(param.path_.length() + 1)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        MEMCPY(path_buf, param.path_.ptr(), param.path_.length());
        path_buf[param.path_.length()] = '\\0';
        fd = ::open(path_buf, O_RDONLY);
        if (fd < 0) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the file cannot be opened");
        }
      }
      while (OB_SUCC(ret) && read_len < file_size) {
        const ssize_t got = ::read(fd, whole + read_len, file_size - read_len);
        if (got < 0) {
          ret = OB_IO_ERROR;
          LOG_WARN("failed to read the json lines file", K(ret), K(read_len));
        } else if (0 == got) {
          break;
        } else {
          read_len += got;
        }
      }
      if (fd >= 0) {
        ::close(fd);
      }
      if (OB_FAIL(ret)) {
      } else {
        whole[read_len] = '\\0';
        const ObString content(static_cast<int32_t>(read_len), whole);
        // pass one builds the key union, pass two fills the cells: a column only exists once some
        // line mentioned it, and the inference has to see every line either way
        ObString rest = content;
        while (OB_SUCC(ret) && !rest.empty()) {
          ObString line;
          const char *nl = static_cast<const char *>(memchr(rest.ptr(), '\\n', rest.length()));
          if (NULL == nl) {
            line = rest;
            rest = ObString();
          } else {
            line.assign_ptr(rest.ptr(), static_cast<int32_t>(nl - rest.ptr()));
            rest.assign_ptr(const_cast<char *>(nl) + 1,
                            static_cast<int32_t>(rest.length() - line.length() - 1));
          }
          while (!line.empty() && ('\\r' == line[line.length() - 1])) {
            line.assign_ptr(line.ptr(), static_cast<int32_t>(line.length() - 1));
          }
          if (line.empty()) {
            // a blank line carries no record
          } else {
            ObIJsonBase *base = NULL;
            if (OB_FAIL(ObJsonBaseFactory::get_json_base(&alloc, line, ObJsonInType::JSON_TREE,
                                                           ObJsonInType::JSON_TREE, base))) {
              LOG_WARN("failed to parse a json line", K(ret), "len", line.length());
            } else if (OB_ISNULL(base) || ObJsonNodeType::J_OBJECT != base->json_type()) {
              ret = OB_INVALID_ARGUMENT;
              LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, a json line is not an object");
            } else {
              ObJsonObject *obj = static_cast<ObJsonObject *>(base);
              const uint64_t member_cnt = obj->member_count();
              for (uint64_t m = 0; OB_SUCC(ret) && m < member_cnt; ++m) {
                ObString key;
                ObJsonNode *value = NULL;
                if (OB_FAIL(obj->get_value_by_idx(m, key, value))) {
                  LOG_WARN("failed to read a json member", K(ret), K(m));
                } else if (OB_ISNULL(value) || key.empty()) {
                  ret = OB_INVALID_ARGUMENT;
                  LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, a json member is unusable");
                } else {
                  bool known = false;
                  for (int64_t k = 0; k < keys.count() && !known; ++k) {
                    known = (0 == keys.at(k).case_compare(key));
                  }
                  if (!known) {
                    ObString copied;
                    ObFileSqlInferCol col;
                    if (OB_FAIL(ob_write_string(alloc, key, copied, true))) {
                      LOG_WARN("failed to copy a json key", K(ret), K(key));
                    } else if (FALSE_IT(col.name_ = copied)) {
                    } else if (OB_FAIL(keys.push_back(copied))) {
                      LOG_WARN("failed to collect a json key", K(ret));
                    } else if (OB_FAIL(inferred.push_back(col))) {
                      LOG_WARN("failed to collect a json column", K(ret));
                    }
                  }
                }
              }
            }
          }
        }
        if (OB_SUCC(ret) && keys.empty()) {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the json lines file has no fields");
        }
        if (OB_SUCC(ret)) {
          data.col_cnt_ = keys.count();
          for (int64_t k = 0; OB_SUCC(ret) && k < keys.count(); ++k) {
            ObFileSqlColumn col;
            col.name_ = keys.at(k);
            col.type_ = ObVarcharType;
            if (OB_FAIL(data.cols_.push_back(col))) {
              LOG_WARN("failed to publish a json column", K(ret), K(k));
            }
          }
        }
        ObString rest2 = content;
        while (OB_SUCC(ret) && !rest2.empty()) {
          ObString line2;
          const char *nl2 = static_cast<const char *>(memchr(rest2.ptr(), '\\n', rest2.length()));
          if (NULL == nl2) {
            line2 = rest2;
            rest2 = ObString();
          } else {
            line2.assign_ptr(rest2.ptr(), static_cast<int32_t>(nl2 - rest2.ptr()));
            rest2.assign_ptr(const_cast<char *>(nl2) + 1,
                             static_cast<int32_t>(rest2.length() - line2.length() - 1));
          }
          while (!line2.empty() && ('\\r' == line2[line2.length() - 1])) {
            line2.assign_ptr(line2.ptr(), static_cast<int32_t>(line2.length() - 1));
          }
          if (line2.empty()) {
            // a blank line carries no record
          } else {
            ObIJsonBase *base2 = NULL;
            if (OB_FAIL(ObJsonBaseFactory::get_json_base(&alloc, line2, ObJsonInType::JSON_TREE,
                                                           ObJsonInType::JSON_TREE, base2))) {
              LOG_WARN("failed to reparse a json line", K(ret));
            } else if (OB_ISNULL(base2) || ObJsonNodeType::J_OBJECT != base2->json_type()) {
              ret = OB_INVALID_ARGUMENT;
            } else if (data.row_cnt_ >= MAX_ROWS) {
              ret = OB_NOT_SUPPORTED;
              LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file with more than 100000 rows as a table");
            } else {
              ObJsonObject *obj2 = static_cast<ObJsonObject *>(base2);
              for (int64_t k = 0; OB_SUCC(ret) && k < data.col_cnt_; ++k) {
                ObString text;
                ObJsonNode *member = obj2->get_value(keys.at(k));
                if (OB_NOT_NULL(member) && ObJsonNodeType::J_NULL != member->json_type()) {
                  ObStringBuffer buf(&alloc);
                  if (OB_FAIL(member->print(buf, 0))) {
                    LOG_WARN("failed to render a json value", K(ret), K(k));
                  } else {
                    text = buf.string();
                    // print() keeps the JSON quoting of a string, the table holds the value itself
                    if (ObJsonNodeType::J_STRING == member->json_type()
                        && text.length() >= 2 && '"' == text[0] && '"' == text[text.length() - 1]) {
                      text.assign_ptr(text.ptr() + 1, static_cast<int32_t>(text.length() - 2));
                    }
                  }
                }
                ObString stored;
                if (OB_FAIL(ret)) {
                } else if (OB_FAIL(ob_write_string(alloc, text, stored, true))) {
                  LOG_WARN("failed to keep a json value", K(ret), K(k));
                } else if (OB_FAIL(data.cells_.push_back(stored))) {
                  LOG_WARN("failed to collect a json cell", K(ret), K(k));
                } else if (OB_FAIL(ObFileSqlReader::merge_value(text, inferred.at(k)))) {
                  LOG_WARN("failed to widen a json column", K(ret), K(k), K(text));
                }
              }
              if (OB_SUCC(ret)) {
                ++data.row_cnt_;
              }
            }
          }
        }
        if (OB_SUCC(ret)) {
          for (int64_t k = 0; k < data.col_cnt_; ++k) {
            data.cols_.at(k).type_ = ob_file_sql_infer_final_type(inferred.at(k));
            data.cols_.at(k).max_len_ = inferred.at(k).max_len_;
            data.cols_.at(k).nullable_ = inferred.at(k).has_null_;
          }
          if (0 == data.row_cnt_) {
            ret = OB_NOT_SUPPORTED;
            LOG_USER_ERROR(OB_NOT_SUPPORTED, "a file table with no rows");
          }
        }
      }
    }
  }
  return ret;
}

'''

OPTION_WALK_OLD = u'''    // merge_nodes(T_EXPR_LIST, chain) keeps the chain in children_[0]; each link is
    // T_LINK_NODE{option, next}. children_[1] of the list itself is not populated.
    ParseNode *chain = (table_node.num_child_ >= 2 && OB_NOT_NULL(table_node.children_[1])
                        && table_node.children_[1]->num_child_ >= 1)
                       ? table_node.children_[1]->children_[0] : NULL;
    while (OB_SUCC(ret) && OB_NOT_NULL(chain)) {
      if (chain->num_child_ < 1 || OB_ISNULL(chain->children_[0])) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("broken option chain", K(ret), K(chain->num_child_));
      } else if (OB_FAIL(options.push_back(chain->children_[0]))) {
        LOG_WARN("failed to collect option", K(ret));
      } else {
        chain = (chain->num_child_ >= 2) ? chain->children_[1] : NULL;
      }
    }'''

OPTION_WALK_NEW = u'''    // merge_nodes() wraps a list of options but leaves a single option unwrapped, so the option
    // nodes are collected by flattening any link/list container instead of assuming one chain
    // shape. The depth bound keeps a malformed list from recursing without end.
    if (table_node.num_child_ >= 2 && OB_NOT_NULL(table_node.children_[1])
        && OB_FAIL(flatten_file_table_options(table_node.children_[1], 0, options))) {
      LOG_WARN("failed to collect the file table options", K(ret), K(table_node.num_child_));
    }'''

FLATTEN_HELPER = u'''
// Collect the option nodes of a file table call, whatever shape the parser left them in.
static int flatten_file_table_options(const ParseNode *node, int64_t depth,
                                      common::ObIArray<ParseNode *> &options)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(node)) {
    // nothing to collect
  } else if (depth > 8) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "file table, the option list is nested too deeply");
  } else if (T_LINK_NODE == node->type_ || T_EXPR_LIST == node->type_) {
    for (int64_t i = 0; OB_SUCC(ret) && i < node->num_child_; ++i) {
      if (OB_NOT_NULL(node->children_[i])
          && OB_FAIL(flatten_file_table_options(node->children_[i], depth + 1, options))) {
        LOG_WARN("failed to flatten a file table option", K(ret), K(i), K(depth));
      }
    }
  } else if (OB_FAIL(options.push_back(const_cast<ParseNode *>(node)))) {
    LOG_WARN("failed to collect a file table option", K(ret), "type", node->type_);
  }
  return ret;
}

'''

FORMAT_OPT_OLD = u'''        const ObString fmt(opt->children_[0]->str_len_, opt->children_[0]->str_value_);
        const bool ok = ((OB_FILE_SQL_FMT_JSONL == param.format_ || OB_FILE_SQL_FMT_CSV == param.format_)'''

FORMAT_OPT_NEW = u'''        const ObString fmt(opt->children_[0]->str_len_, opt->children_[0]->str_value_);
        // read_json's format option is what picks the layout, and the suite asks for
        // format = 'newline_delimited': one JSON object per line
        if (OB_FILE_SQL_FMT_JSON == param.format_
            && (0 == fmt.case_compare("newline_delimited") || 0 == fmt.case_compare("ndjson")
                || 0 == fmt.case_compare("jsonl") || 0 == fmt.case_compare("lines"))) {
          param.format_ = OB_FILE_SQL_FMT_JSONL;
        }
        const bool ok = ((OB_FILE_SQL_FMT_JSONL == param.format_ || OB_FILE_SQL_FMT_CSV == param.format_
                          || OB_FILE_SQL_FMT_JSON == param.format_)'''

RES_ANCHOR = u'''int ObDMLResolver::resolve_file_table_item(const ParseNode &table_node, TableItem *&table_item)'''


def apply_edit(doc, old, new, label, expect=1):
    hits = doc.count(old)
    if expect != hits:
        raise SystemExit("[ABORT] %s matched %d times, expected %d" % (label, hits, expect))
    return doc.replace(old, new, expect)


def main():
    docs = {}
    for path in (H_PATH, CPP_PATH, RES_PATH):
        with open(path, "rb") as handle:
            raw = handle.read()
        crlf = raw.count(b"\r\n")
        docs[path] = [raw.decode("utf-8").replace("\r\n", "\n"), crlf]
        if "load_jsonl" in docs[path][0]:
            raise SystemExit("[ABORT] patch 09 already applied in %s" % path)

    text = docs[H_PATH][0]
    text = apply_edit(text, ENUM_OLD, ENUM_NEW, "format enum")
    text = apply_edit(text, LOADER_DECL_OLD, LOADER_DECL_NEW, "loader declaration")
    docs[H_PATH][0] = text

    text = docs[CPP_PATH][0]
    text = apply_edit(text, INCLUDE_OLD, INCLUDE_NEW, "includes")
    text = apply_edit(text, DEDUP_OLD, DEDUP_NEW, "duplicate header suffix")
    text = apply_edit(text, DISPATCH_OLD, DISPATCH_NEW, "loader dispatch")
    text = apply_edit(text, NS_END, JSONL_IMPL + NS_END, "jsonl implementation")
    docs[CPP_PATH][0] = text

    text = docs[RES_PATH][0]
    text = apply_edit(text, OPTION_WALK_OLD, OPTION_WALK_NEW, "option walk")
    text = apply_edit(text, RES_ANCHOR, FLATTEN_HELPER.strip("\n") + "\n\n" + RES_ANCHOR,
                      "flatten helper")
    text = apply_edit(text, FORMAT_OPT_OLD, FORMAT_OPT_NEW, "format option")
    docs[RES_PATH][0] = text

    for path, (text, crlf) in docs.items():
        out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(out)
        print("[OK] %-52s crlf=%d bytes=%d" % (path, crlf, len(out)))


if "__main__" == __name__:
    main()
