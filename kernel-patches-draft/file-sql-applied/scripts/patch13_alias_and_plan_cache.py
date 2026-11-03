"""Patch 13: two correctness gaps the robustness sweep proved on the live server.

  $ select f.id from read_csv('basic.csv') f order by f.id
    ERR 1054 Unknown column 'f.id'                       <- the alias never reached the table item
  $ select count(*) as c from read_csv('big.csv') f   -> 5000
  $ select count(*) as c from read_csv('basic.csv') f -> 5000   <- WRONG, and so is every other path
  $ select count(*) as c from read_csv('nope.csv') f  -> 5000   <- a missing file answered 5000

The second one is the serious one: the statement is parameterised on the path literal while the rows
themselves are baked into the plan, so the plan cache happily serves one file's materialised table
to another path. The engine already has the right knob for a plan that must not be reused across
different constant values - the same `need_match_all_params_` flag the insert and update resolvers
set - so a file table sets it too.

The first is smaller: the grammar carries the relation alias as child 2 of the table node, and the
generic alias machinery keys off node types the file table does not share, so the alias is applied
here, before the column items are generated and named after the table.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"
RESH_PATH = "src/sql/resolver/dml/ob_dml_resolver.h"

CALL_OLD = u'''  if (OB_FAIL(ret)) {
  } else if (params_.session_info_->is_inner()) {'''

CALL_NEW = u'''  ObString table_alias;
  if (OB_FAIL(ret)) {
  } else if (params_.session_info_->is_inner()) {'''

BUILD_CALL_OLD = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {'''

BUILD_CALL_NEW = u'''  } else if (table_node.num_child_ >= 3 && OB_NOT_NULL(table_node.children_[2])
             && OB_NOT_NULL(table_node.children_[2]->str_value_)
             && table_node.children_[2]->str_len_ > 0
             && FALSE_IT(table_alias.assign_ptr(table_node.children_[2]->str_value_,
                                                static_cast<int32_t>(table_node.children_[2]->str_len_)))) {
    // the alias node is a terminal identifier; nothing to do when it is absent
  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_alias, table_item))) {'''

SIGN_OLD = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                TableItem *&table_item)'''

SIGN_NEW = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                const ObString &table_alias,
                                                TableItem *&table_item)'''

NAME_OLD = u'''      if (OB_FAIL(dml_stmt->generate_values_table_name(*allocator_, new_table_item->table_name_))) {
        LOG_WARN("failed to name the file table", K(ret));
      } else if (FALSE_IT(new_table_item->alias_name_ = new_table_item->table_name_)) {'''

NAME_NEW = u'''      // a user who wrote `from read_csv(...) f` refers to the columns as f.id, so the alias has to
      // be the table's name before the column items are built; the generic alias machinery keys off
      // node types this table function does not share
      if (!table_alias.empty()) {
        ObString copied;
        if (OB_FAIL(ob_write_string(*allocator_, table_alias, copied, true))) {
          LOG_WARN("failed to copy the file table alias", K(ret), K(table_alias));
        } else {
          new_table_item->table_name_ = copied;
          new_table_item->alias_name_ = copied;
        }
      } else if (OB_FAIL(dml_stmt->generate_values_table_name(*allocator_, new_table_item->table_name_))) {
        LOG_WARN("failed to name the file table", K(ret));
      } else if (FALSE_IT(new_table_item->alias_name_ = new_table_item->table_name_)) {'''

CACHE_OLD = u'''      if (OB_FAIL(ret)) {
      } else if (OB_FAIL(gen_values_table_column_items(table_def->column_cnt_,
                                                       table_def->column_types_,
                                                       *new_table_item))) {'''

CACHE_NEW = u'''      // the path literal is parameterised while the rows are baked into the plan, so this plan may
      // only be reused when the constants match again - otherwise read_csv('b.csv') would be served
      // the materialised table of read_csv('a.csv')
      if (OB_NOT_NULL(dml_stmt->get_query_ctx())) {
        dml_stmt->get_query_ctx()->need_match_all_params_ = true;
      }
      if (OB_FAIL(ret)) {
      } else if (OB_FAIL(gen_values_table_column_items(table_def->column_cnt_,
                                                       table_def->column_types_,
                                                       *new_table_item))) {'''

DECL_OLD = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     TableItem *&table_item);'''

DECL_NEW = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     const common::ObString &table_alias,
                                     TableItem *&table_item);'''


def apply_edit(doc, old, new, label):
    hits = doc.count(old)
    if 1 != hits:
        raise SystemExit("[ABORT] %s matched %d times" % (label, hits))
    return doc.replace(old, new, 1)


def main():
    docs = {}
    for path in (RES_PATH, RESH_PATH):
        with open(path, "rb") as handle:
            raw = handle.read()
        crlf = raw.count(b"\r\n")
        docs[path] = [raw.decode("utf-8").replace("\r\n", "\n"), crlf]
        if "need_match_all_params_" in docs[path][0] and path == RES_PATH:
            raise SystemExit("[ABORT] patch 13 already applied")

    text = docs[RES_PATH][0]
    for old, new, label in ((CALL_OLD, CALL_NEW, "alias declaration"),
                            (BUILD_CALL_OLD, BUILD_CALL_NEW, "alias read + call"),
                            (SIGN_OLD, SIGN_NEW, "builder signature"),
                            (NAME_OLD, NAME_NEW, "alias naming"),
                            (CACHE_OLD, CACHE_NEW, "plan cache match")):
        text = apply_edit(text, old, new, label)
    docs[RES_PATH][0] = text
    docs[RESH_PATH][0] = apply_edit(docs[RESH_PATH][0], DECL_OLD, DECL_NEW, "header declaration")

    for path, (text, crlf) in docs.items():
        out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(out)
        print("[OK] %-52s crlf=%d bytes=%d" % (path, crlf, len(out)))


if "__main__" == __name__:
    main()
