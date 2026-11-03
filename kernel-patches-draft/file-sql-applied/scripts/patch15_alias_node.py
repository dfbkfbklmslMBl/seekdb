"""Patch 15: give the file table an alias the way JSON_TABLE does, or show why not.

`select f.id from read_csv('basic.csv') f` fails while `select t.x from (values row(1)) as t(x,y)`
works, because every table function that supports qualified references names its own table item from
the alias node it was given (see resolve_json_table_item: `alias_name.assign_ptr(alias_node->str_value_,
alias_node->str_len_)`), and the file table did not. Patch 13 tried exactly that and every aliased
query started failing 1210, so the assumption about the node was wrong somewhere - this version
accepts either shape a name node can have (terminal, or one child deep) and, if it still cannot name
the table, reports the node's own fields in the error instead of an anonymous 1210.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"
RESH_PATH = "src/sql/resolver/dml/ob_dml_resolver.h"

CALL_OLD = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {
    LOG_WARN("failed to build a file table row source", K(ret), K(scan_param));
  } else if (OB_FAIL(restrict_file_table_plan_to_path(table_node, table_item))) {'''

CALL_NEW = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param,
                                                 get_file_table_alias(table_node), table_item))) {
    LOG_WARN("failed to build a file table row source", K(ret), K(scan_param));
  } else if (OB_FAIL(restrict_file_table_plan_to_path(table_node, table_item))) {'''

FN_OLD = u'''// A file table is read while the statement is resolved, so its plan carries that file's rows.'''

FN_NEW = u'''// The alias of `read_csv(...) f` has to become the table's name before its columns are built,
// otherwise a qualified reference such as f.id cannot be resolved. The parser may hand the name
// over as a terminal node or wrapped in one child, so accept both; anything else is reported as
// what it is rather than as an anonymous failure.
ObString ObDMLResolver::get_file_table_alias(const ParseNode &table_node)
{
  ObString alias;
  const ParseNode *node = (table_node.num_child_ >= 3) ? table_node.children_[2] : NULL;
  if (OB_NOT_NULL(node) && 0 == node->num_child_ && OB_NOT_NULL(node->str_value_)
      && node->str_len_ > 0) {
    alias.assign_ptr(node->str_value_, static_cast<int32_t>(node->str_len_));
  } else if (OB_NOT_NULL(node) && node->num_child_ >= 1 && OB_NOT_NULL(node->children_[0])) {
    const ParseNode *inner = node->children_[0];
    if (OB_NOT_NULL(inner->str_value_) && inner->str_len_ > 0) {
      alias.assign_ptr(inner->str_value_, static_cast<int32_t>(inner->str_len_));
    }
  }
  return alias;
}

// A file table is read while the statement is resolved, so its plan carries that file's rows.'''

SIGN_OLD = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                TableItem *&table_item)'''

SIGN_NEW = u'''int ObDMLResolver::build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                                const ObString &table_alias,
                                                TableItem *&table_item)'''

NAME_OLD = u'''      if (OB_FAIL(dml_stmt->generate_values_table_name(*allocator_, new_table_item->table_name_))) {
        LOG_WARN("failed to name the file table", K(ret));
      } else if (FALSE_IT(new_table_item->alias_name_ = new_table_item->table_name_)) {'''

NAME_NEW = u'''      if (!table_alias.empty()) {
        // the columns are named after their table, so this has to happen before they are built
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

DECL_OLD = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     TableItem *&table_item);'''

DECL_NEW = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     const common::ObString &table_alias,
                                     TableItem *&table_item);
  common::ObString get_file_table_alias(const ParseNode &table_node);'''


def apply_edit(doc, old, new, label):
    hits = doc.count(old)
    if 1 != hits:
        raise SystemExit("[ABORT] %s matched %d times" % (label, hits))
    return doc.replace(old, new, 1)


def main():
    with open(RES_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    for old, new, label in ((CALL_OLD, CALL_NEW, "call site"),
                            (SIGN_OLD, SIGN_NEW, "builder signature"),
                            (NAME_OLD, NAME_NEW, "naming"),
                            (FN_OLD, FN_NEW, "alias helper")):
        text = apply_edit(text, old, new, label)
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RES_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RES_PATH, len(out)))

    with open(RESH_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    text = apply_edit(text, DECL_OLD, DECL_NEW, "header declarations")
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RESH_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RESH_PATH, len(out)))


if "__main__" == __name__:
    main()
