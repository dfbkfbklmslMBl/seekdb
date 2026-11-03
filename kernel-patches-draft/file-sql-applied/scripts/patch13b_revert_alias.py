"""Patch 13b: keep the plan-cache fix, take the alias change back out.

With an alias in the FROM clause every file query started to fail 1210, so the alias handling in
patch 13 is wrong somewhere in the machinery that hands a table item to the statement - and the
recorded suite does not need it (it refers to file columns unqualified). The plan-cache constraint
is the one that prevents silently wrong answers, so that stays and the naming goes back to the
generated values-table name. Qualified references (f.id) become a stated limitation instead of a
half-working feature.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"
RESH_PATH = "src/sql/resolver/dml/ob_dml_resolver.h"

CALL_NEW = u'''  ObString table_alias;
  if (OB_FAIL(ret)) {
  } else if (params_.session_info_->is_inner()) {'''

CALL_OLD = u'''  if (OB_FAIL(ret)) {
  } else if (params_.session_info_->is_inner()) {'''

BUILD_CALL_NEW = u'''  } else if (table_node.num_child_ >= 3 && OB_NOT_NULL(table_node.children_[2])
             && OB_NOT_NULL(table_node.children_[2]->str_value_)
             && table_node.children_[2]->str_len_ > 0
             && FALSE_IT(table_alias.assign_ptr(table_node.children_[2]->str_value_,
                                                static_cast<int32_t>(table_node.children_[2]->str_len_)))) {
    // the alias node is a terminal identifier; nothing to do when it is absent
  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_alias, table_item))) {'''

BUILD_CALL_OLD = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {'''

SIGN_NEW = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                const ObString &table_alias,
                                                TableItem *&table_item)'''

SIGN_OLD = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                TableItem *&table_item)'''

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

NAME_OLD = u'''      if (OB_FAIL(dml_stmt->generate_values_table_name(*allocator_, new_table_item->table_name_))) {
        LOG_WARN("failed to name the file table", K(ret));
      } else if (FALSE_IT(new_table_item->alias_name_ = new_table_item->table_name_)) {'''

DECL_NEW = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     const common::ObString &table_alias,
                                     TableItem *&table_item);'''

DECL_OLD = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     TableItem *&table_item);'''

PAIRS_RES = ((CALL_NEW, CALL_OLD, "alias declaration"),
             (BUILD_CALL_NEW, BUILD_CALL_OLD, "alias read + call"),
             (SIGN_NEW, SIGN_OLD, "builder signature"),
             (NAME_NEW, NAME_OLD, "alias naming"))


def main():
    with open(RES_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for new, old, label in PAIRS_RES:
        if text.count(new) != 1:
            raise SystemExit("[ABORT] %s: current text matched %d times" % (label, text.count(new)))
        text = text.replace(new, old, 1)
    if "need_match_all_params_" not in text:
        raise SystemExit("[ABORT] the plan-cache fix is not present - refusing to drop it")
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RES_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RES_PATH, len(out)))

    with open(RESH_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if text.count(DECL_NEW) != 1:
        raise SystemExit("[ABORT] header declaration matched %d times" % text.count(DECL_NEW))
    text = text.replace(DECL_NEW, DECL_OLD, 1)
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RESH_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RESH_PATH, len(out)))
    print("  plan-cache fix kept ->", "need_match_all_params_" in
          open(RES_PATH, encoding="utf-8").read())


if "__main__" == __name__:
    main()
