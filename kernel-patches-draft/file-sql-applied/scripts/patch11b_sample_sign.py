"""Patch 11b: read the sign of sample_size from the statement text.

The diagnostic build answered the question: the node that arrives for `sample_size = -1` is
T_EXTEND with value_ = 1 and a one-character text "1", so the leading '-' never reaches the
resolver through the option node. Rather than fight the parser's constant folding, the statement
text is the authority: the node carries its own offset in the query, and the non-space character
before that offset is the sign the user typed. `sample_size = 10` still has nothing before it but a
space and '=', so it stays rejected, as the suite requires.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

OLD = u'''      const ParseNode *value_node = (opt->num_child_ >= 1) ? opt->children_[0] : NULL;
      const int64_t sample = (OB_NOT_NULL(value_node)) ? value_node->value_ : 0;
      const bool negative = (sample < 0)
                            || (OB_NOT_NULL(value_node) && OB_NOT_NULL(value_node->str_value_)
                                && value_node->str_len_ > 0 && '-' == value_node->str_value_[0]);'''

NEW = u'''      const ParseNode *value_node = (opt->num_child_ >= 1) ? opt->children_[0] : NULL;
      const int64_t sample = (OB_NOT_NULL(value_node)) ? value_node->value_ : 0;
      bool negative = (sample < 0);
      int64_t off = -1;
      char before = ' ';
      if (!negative && OB_NOT_NULL(value_node) && OB_NOT_NULL(session_info_)) {
        // the parser folds a leading '-' into conf_const, but the constant node that reaches here
        // carries only the digits, so the user's own text decides the sign
        const ObString &sql_text = session_info_->get_current_query_string();
        off = value_node->sql_str_off_;
        int64_t i = off - 1;
        while (i >= 0 && (' ' == sql_text[i] || '\\t' == sql_text[i])) {
          --i;
        }
        if (i >= 0 && '-' == sql_text[i]) {
          negative = true;
          before = '-';
        }
      }'''

DIAG_OLD = u'''                 "sample_size other than -1 [opt_children=%ld val_type=%d val_value=%lld "
                 "val_str_len=%lld val_children=%ld gc_type=%d gc_value=%lld]",'''

DIAG_NEW = u'''                 "sample_size other than -1 [val_type=%d val_value=%lld str_off=%lld before=%c]",'''

DIAG_ARGS_OLD = u'''                 static_cast<long>(opt->num_child_),
                 (OB_ISNULL(value_node) ? -1 : static_cast<int>(value_node->type_)),
                 static_cast<long long>(sample),
                 (OB_ISNULL(value_node) ? -1 : static_cast<long long>(value_node->str_len_)),
                 (OB_ISNULL(value_node) ? -1 : static_cast<long>(value_node->num_child_)),
                 (OB_ISNULL(gc) ? -1 : static_cast<int>(gc->type_)),
                 (OB_ISNULL(gc) ? 0 : static_cast<long long>(gc->value_)));'''

DIAG_ARGS_NEW = u'''                 (OB_ISNULL(value_node) ? -1 : static_cast<int>(value_node->type_)),
                 static_cast<long long>(sample), static_cast<long long>(off), before);'''

GC_OLD = u'''        const ParseNode *gc = (OB_NOT_NULL(value_node) && value_node->num_child_ >= 1
                               && OB_NOT_NULL(value_node->children_[0]))
                              ? value_node->children_[0] : NULL;
'''

with open(RES_PATH, "rb") as handle:
    raw = handle.read()
crlf = raw.count(b"\r\n")
text = raw.decode("utf-8").replace("\r\n", "\n")
for old, new, label in ((OLD, NEW, "sign rule"), (GC_OLD, u"", "unused grandchild probe"),
                        (DIAG_OLD, DIAG_NEW, "diagnostic format"),
                        (DIAG_ARGS_OLD, DIAG_ARGS_NEW, "diagnostic arguments")):
    if text.count(old) != 1:
        raise SystemExit("[ABORT] %s matched %d times" % (label, text.count(old)))
    text = text.replace(old, new, 1)
out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
with open(RES_PATH, "wb") as handle:
    handle.write(out)
print("[OK] %s crlf=%d bytes=%d" % (RES_PATH, crlf, len(out)))
