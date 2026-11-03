"""Patch 14: pin a file-table plan to the path it was built for.

`need_match_all_params_` (patch 13) turned out to compare parameter *types*, not values, so a plan
materialised for basic.csv still answered a query naming big.csv. The engine's real mechanism for
"this plan is only valid for these constant values" is `ObPCConstParamInfo` in
`all_plan_const_param_constraints_`, which is what the raw-expression resolver uses for exactly
this situation, and the fast parser leaves the path as a T_QUESTIONMARK node whose value_ is the
index of the literal in the parameter list.

Pinning the path is enough: the file's contents are what the plan baked in, and two statements that
differ in any other literal also differ in their normalised text.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

# the alias experiment from patch 13 is gone; anchor on the current shape of the call site
CALL_OLD = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {
    LOG_WARN("failed to build a file table row source", K(ret), K(scan_param));
  }
  return ret;
}
'''

CALL_NEW = u'''  } else if (OB_FAIL(build_file_table_values_item(scan_param, table_item))) {
    LOG_WARN("failed to build a file table row source", K(ret), K(scan_param));
  } else if (OB_FAIL(restrict_file_table_plan_to_path(table_node, table_item))) {
    LOG_WARN("failed to pin the file table plan to its path", K(ret), K(scan_param));
  }
  return ret;
}

// A file table is read while the statement is resolved, so its plan carries that file's rows. The
// path itself is a parameter to the plan cache, which would happily serve the plan to a query naming
// another file, so the plan records the path as a constant it may only match again.
int ObDMLResolver::restrict_file_table_plan_to_path(const ParseNode &table_node,
                                                    const TableItem *table_item)
{
  int ret = OB_SUCCESS;
  ObDMLStmt *dml_stmt = get_stmt();
  const ParseNode *path_node = table_item == NULL ? NULL : table_node.children_[0];
  if (OB_ISNULL(dml_stmt) || OB_ISNULL(dml_stmt->get_query_ctx()) || OB_ISNULL(path_node)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("missing context to constrain a file table plan", K(ret));
  } else if (T_QUESTIONMARK != path_node->type_ || OB_ISNULL(params_.param_list_)
             || 0 == params_.param_list_->count()) {
    // the path is spelled literally in the statement text, so it is already part of the plan key
  } else {
    const int64_t param_idx = path_node->value_;
    ObPCConstParamInfo const_param_info;
    if (param_idx < 0 || param_idx >= params_.param_list_->count()) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("file table path has no parameter slot", K(ret), K(param_idx),
               "params", params_.param_list_->count());
    } else if (OB_FAIL(const_param_info.const_idx_.push_back(param_idx))) {
      LOG_WARN("failed to record the file table path constraint", K(ret), K(param_idx));
    } else if (OB_FAIL(const_param_info.const_params_.push_back(params_.param_list_->at(param_idx)))) {
      LOG_WARN("failed to record the file table path value", K(ret), K(param_idx));
    } else if (OB_FAIL(dml_stmt->get_query_ctx()->all_plan_const_param_constraints_
                       .push_back(const_param_info))) {
      LOG_WARN("failed to add the file table path constraint", K(ret));
    }
  }
  return ret;
}
'''

DECL_OLD = u'''  int build_file_table_values_item(const ObFileSqlScanParam &scan_param,
                                     TableItem *&table_item);'''

# patch 13's flag only forces a parameter *type* comparison, which is not what makes a file-table
# plan safe; the constraint below is. Take the dead flag out rather than leave a misleading line.
FLAG_OLD = u'''      // the path literal is parameterised while the rows are baked into the plan, so this plan may
      // only be reused when the constants match again - otherwise read_csv('b.csv') would be served
      // the materialised table of read_csv('a.csv')
      if (OB_NOT_NULL(dml_stmt->get_query_ctx())) {
        dml_stmt->get_query_ctx()->need_match_all_params_ = true;
      }
'''

FLAG_NEW = u''

DECL_NEW = DECL_OLD + u'''
  int restrict_file_table_plan_to_path(const ParseNode &table_node, const TableItem *table_item);'''


def main():
    with open(RES_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if text.count(CALL_OLD) != 1:
        raise SystemExit("[ABORT] call site matched %d times" % text.count(CALL_OLD))
    text = text.replace(CALL_OLD, CALL_NEW, 1)
    if text.count(FLAG_OLD) != 1:
        raise SystemExit("[ABORT] the dead flag block matched %d times" % text.count(FLAG_OLD))
    text = text.replace(FLAG_OLD, FLAG_NEW, 1)
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RES_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RES_PATH, len(out)))

    with open(RES_PATH.replace(".cpp", ".h"), "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if text.count(DECL_OLD) != 1:
        raise SystemExit("[ABORT] header declaration matched %d times" % text.count(DECL_OLD))
    text = text.replace(DECL_OLD, DECL_NEW, 1)
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(RES_PATH.replace(".cpp", ".h"), "wb") as handle:
        handle.write(out)
    print("[OK] %-52s bytes=%d" % (RES_PATH.replace(".cpp", ".h"), len(out)))


if "__main__" == __name__:
    main()
