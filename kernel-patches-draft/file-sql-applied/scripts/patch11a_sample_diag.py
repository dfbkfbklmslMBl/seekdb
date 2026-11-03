"""Patch 11a: make the sample_size node reveal itself.

sample_size = -1 is still refused after two attempts, which means the reasoning about the parse node
is wrong, not the comparison. Rather than guess a third time, put the node's own fields into the
user error so one query answers what shape the parser actually hands over.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

OLD = u'''      if (!negative) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "sample_size other than -1");
        LOG_WARN("sample_size rejected", K(ret), K(opt->type_), K(sample));
      }'''

NEW = u'''      if (!negative) {
        char why[224] = {'\\0'};
        const ParseNode *gc = (OB_NOT_NULL(value_node) && value_node->num_child_ >= 1
                               && OB_NOT_NULL(value_node->children_[0]))
                              ? value_node->children_[0] : NULL;
        snprintf(why, sizeof(why),
                 "sample_size other than -1 [opt_children=%ld val_type=%d val_value=%lld "
                 "val_str_len=%lld val_children=%ld gc_type=%d gc_value=%lld]",
                 static_cast<long>(opt->num_child_),
                 (OB_ISNULL(value_node) ? -1 : static_cast<int>(value_node->type_)),
                 static_cast<long long>(sample),
                 (OB_ISNULL(value_node) ? -1 : static_cast<long long>(value_node->str_len_)),
                 (OB_ISNULL(value_node) ? -1 : static_cast<long>(value_node->num_child_)),
                 (OB_ISNULL(gc) ? -1 : static_cast<int>(gc->type_)),
                 (OB_ISNULL(gc) ? 0 : static_cast<long long>(gc->value_)));
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, why);
        LOG_WARN("sample_size rejected", K(ret), K(opt->type_), K(sample));
      }'''

with open(RES_PATH, "rb") as handle:
    raw = handle.read()
crlf = raw.count(b"\r\n")
text = raw.decode("utf-8").replace("\r\n", "\n")
if text.count(OLD) != 1:
    raise SystemExit("[ABORT] anchor matched %d times" % text.count(OLD))
text = text.replace(OLD, NEW, 1)
out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
with open(RES_PATH, "wb") as handle:
    handle.write(out)
print("[OK] %s crlf=%d bytes=%d" % (RES_PATH, crlf, len(out)))
