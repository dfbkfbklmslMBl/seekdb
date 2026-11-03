"""Patch 11d: the only authority for the sign is the text the user typed.

After 11c the probe still refused nothing: `sample_size = 10` was accepted. That is the `sample < 0`
clause firing on a T_EXTEND node, whose value_ field is not the parsed magnitude at all - the
diagnostic build had already shown it reporting 1 for -1. So drop that clause and keep the scan that
looks at the character preceding the literal in the statement text: it is what actually distinguishes
`sample_size = -1` (read every row) from `sample_size = 10` (sampling, not implemented).
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

OLD = u'''      const int64_t sample = (OB_NOT_NULL(value_node)) ? value_node->value_ : 0;
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

NEW = u'''      const int64_t sample = (OB_NOT_NULL(value_node)) ? value_node->value_ : 0;
      int64_t off = -1;
      char before = ' ';
      bool negative = false;
      if (OB_NOT_NULL(value_node) && OB_NOT_NULL(session_info_)) {
        // the parser folds a leading '-' into conf_const, but the node that reaches the resolver is
        // a T_EXTEND whose value_ is not the magnitude, so the user's own text is the authority on
        // the sign: the character in front of the literal is what separates -1 from 10
        const ObString &sql_text = session_info_->get_current_query_string();
        off = value_node->sql_str_off_;
        int64_t i = off - 1;
        while (i >= 0 && (' ' == sql_text[i] || '\\t' == sql_text[i])) {
          --i;
        }
        if (i >= 0) {
          before = sql_text[i];
          negative = ('-' == before);
        }
      }'''

with open(RES_PATH, "rb") as handle:
    raw = handle.read()
crlf = raw.count(b"\r\n")
text = raw.decode("utf-8").replace("\r\n", "\n")
if text.count(OLD) != 1:
    raise SystemExit("[ABORT] sign block matched %d times" % text.count(OLD))
text = text.replace(OLD, NEW, 1)
out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
with open(RES_PATH, "wb") as handle:
    handle.write(out)
print("[OK] %s crlf=%d bytes=%d" % (RES_PATH, crlf, len(out)))
