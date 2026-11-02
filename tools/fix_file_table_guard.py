"""Remove the two debug probes and fix the guard that produced the bogus OB_ERR_UNEXPECTED.

Written as a file (not inline shell) because shell quoting had already mangled this edit twice.
Every replacement asserts its anchor count, so a silent no-op is impossible.
"""

import pathlib
import re

P = pathlib.Path("src/sql/resolver/dml/ob_dml_resolver.cpp")
text = P.read_text(encoding="utf-8", errors="surrogateescape")

# 1. drop the snprintf DIAG block
pattern = re.compile(
    r"  \{\n    char diag\[96\][^\n]*\n(?:[^\n]*\n)*?    return ret;\n  \}\n")
n_diag = len(pattern.findall(text))
text = pattern.sub("", text)

# 2. drop the entry probe
n_enter = text.count('LOG_WARN("QODER_ENTER resolve_file_table_item"')
text = text.replace(
    '  LOG_WARN("QODER_ENTER resolve_file_table_item", K(table_node.num_child_), K(table_node.value_));\n',
    "")

# 3. the real fix: table_item is NULL on entry, so allocate it like the sibling cases do
old_guard = """  ObFileSqlScanParam scan_param;
  common::ObSEArray<ParseNode *, 8> options;
  if (OB_ISNULL(params_.session_info_) || OB_ISNULL(table_item)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("unexpected null", K(ret), K(params_.session_info_), K(table_item));
  } else if (OB_FAIL(params_.session_info_->get_sys_variable("""
new_guard = """  ObFileSqlScanParam scan_param;
  common::ObSEArray<ParseNode *, 8> options;
  ObDMLStmt *dml_stmt = get_stmt();
  TableItem *new_table_item = NULL;
  // table_item arrives NULL: resolve_table() hands the case an unallocated slot, exactly like the
  // VALUES/JSON_TABLE/UNNEST cases below, each of which calls create_table_item() itself.
  if (OB_ISNULL(params_.session_info_) || OB_ISNULL(dml_stmt) || OB_ISNULL(allocator_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("unexpected null", K(ret), K(params_.session_info_), K(dml_stmt), K(allocator_));
  } else if (OB_FAIL(params_.session_info_->get_sys_variable("""
assert text.count(old_guard) == 1, text.count(old_guard)
text = text.replace(old_guard, new_guard, 1)

# 4. allocate the item once the gate/path/options checks have passed
old_glob = """  } else if (OB_FILE_SQL_FMT_GLOB == scan_param.format_) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table row source");"""
new_glob = """  } else if (OB_ISNULL(new_table_item = dml_stmt->create_table_item(*allocator_))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("failed to create table item", K(ret));
  } else if (FALSE_IT(table_item = new_table_item)) {
  } else if (OB_FILE_SQL_FMT_GLOB == scan_param.format_) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "file table row source");"""
assert text.count(old_glob) == 1, text.count(old_glob)
text = text.replace(old_glob, new_glob, 1)

assert "QODER_ENTER" not in text and "char diag[96]" not in text
P.write_text(text)
print(f"probes removed: diag={n_diag} enter={n_enter}; guard fixed; table item allocated")
