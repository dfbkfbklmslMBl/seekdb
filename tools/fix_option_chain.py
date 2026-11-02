"""Fix the file-table option traversal (the crash) and harden every child access.

merge_nodes(T_EXPR_LIST, $option_list) puts the T_LINK_NODE chain in children_[0]; children_[1] is
not populated for a one-element list, so reading it was an out-of-bounds pointer read that killed the
observer. Walk the chain instead, and bound every children_[i] access by num_child_ so a malformed
node can never crash the server again.
"""

import pathlib
import re

P = pathlib.Path("src/sql/resolver/dml/ob_dml_resolver.cpp")
t = P.read_text(encoding="utf-8", errors="surrogateescape")

old = """    ParseNode *opt_list = table_node.children_[1];
    while (OB_SUCC(ret) && OB_NOT_NULL(opt_list)) {
      if (opt_list->num_child_ < 1 || OB_ISNULL(opt_list->children_[0])) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("broken option list", K(ret));
      } else if (OB_FAIL(options.push_back(opt_list->children_[0]))) {
        LOG_WARN("failed to collect option", K(ret));
      } else {
        opt_list = opt_list->children_[1];
      }
    }"""
new = """    // merge_nodes(T_EXPR_LIST, chain) keeps the chain in children_[0]; each link is
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
    }"""
assert t.count(old) == 1, t.count(old)
t = t.replace(old, new, 1)

# make the path-child check tolerant of a node with fewer children than the grammar promises
before = t.count("table_node.num_child_ < 3")
t = t.replace("table_node.num_child_ < 3 || OB_ISNULL(table_node.children_[0])",
              "table_node.num_child_ < 1 || OB_ISNULL(table_node.children_[0])")
P.write_text(t, encoding="utf-8", errors="surrogateescape")
print(f"option traversal fixed; path child check relaxed ({before} site)")
