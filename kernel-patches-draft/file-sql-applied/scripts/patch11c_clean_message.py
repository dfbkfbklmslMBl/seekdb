"""Patch 11c: take the parse-node fields out of the user error, keep them in the server log.

The fields earned their keep - they are what identified the node as T_EXTEND carrying only the
digits - but what a user reads should say what is wrong. The node type, the offset and the sign
character belong in LOG_WARN, which is also where the next person debugging this will look.
"""

RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

MSG_OLD = u'''        char why[224] = {'\\0'};
        snprintf(why, sizeof(why),
                 "sample_size other than -1 [val_type=%d val_value=%lld str_off=%lld before=%c]",
                 (OB_ISNULL(value_node) ? -1 : static_cast<int>(value_node->type_)),
                 static_cast<long long>(sample), static_cast<long long>(off), before);
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, why);
        LOG_WARN("sample_size rejected", K(ret), K(opt->type_), K(sample));'''

MSG_NEW = u'''        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "sample_size other than -1");
        LOG_WARN("sample_size must be -1 to mean every row", K(ret), K(sample), K(off),
                 K(before), "value_type",
                 OB_ISNULL(value_node) ? -1 : static_cast<int>(value_node->type_));'''

with open(RES_PATH, "rb") as handle:
    raw = handle.read()
crlf = raw.count(b"\r\n")
text = raw.decode("utf-8").replace("\r\n", "\n")
if text.count(MSG_OLD) != 1:
    raise SystemExit("[ABORT] message block matched %d times" % text.count(MSG_OLD))
text = text.replace(MSG_OLD, MSG_NEW, 1)
out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
with open(RES_PATH, "wb") as handle:
    handle.write(out)
print("[OK] %s crlf=%d bytes=%d" % (RES_PATH, crlf, len(out)))
print("  internals left in the user error ->", "val_type" in text)
