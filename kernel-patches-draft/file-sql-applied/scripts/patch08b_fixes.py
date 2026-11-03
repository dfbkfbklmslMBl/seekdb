"""Patch 08b: two compile fixes for patch 08, found by the single-TU checker.

  1. OB_MAX_IDENTIFIER_LENGTH is not visible in the file-scan TU; a column name is bounded by the
     same 64 bytes the parser uses, so spell the bound out instead of adding another include.
  2. There is no T_LIT statement-expr type in this tree. Const cells are built through
     ObRawExprUtils::build_const_obj_expr, which is what the rest of the resolver uses.
"""

CPP_PATH = "src/sql/file_scan/ob_file_scan_utils.cpp"
RES_PATH = "src/sql/resolver/dml/ob_dml_resolver.cpp"

CPP_OLD = u'''      char buf[OB_MAX_IDENTIFIER_LENGTH] = {'\\0'};
      ObString base = cols.at(i).name_;
      if (base.length() > OB_MAX_IDENTIFIER_LENGTH - 8) {
        base.assign_ptr(base.ptr(), OB_MAX_IDENTIFIER_LENGTH - 8);
      }'''

CPP_NEW = u'''      // the parser caps an identifier at 64 bytes; the __N suffix has to fit inside that
      char buf[64] = {'\\0'};
      ObString base = cols.at(i).name_;
      if (base.length() > 56) {
        base.assign_ptr(base.ptr(), 56);
      }'''

RES_OLD = u'''          } else if (OB_FAIL(params_.expr_factory_->create_raw_expr(T_LIT, value_expr))) {
            LOG_WARN("failed to create a literal for a file field", K(ret), K(r), K(c));
          } else if (OB_ISNULL(value_expr)) {
            ret = OB_ERR_UNEXPECTED;
          } else if (FALSE_IT(value_expr->set_value(obj))) {'''

RES_NEW = u'''          } else if (OB_FAIL(ObRawExprUtils::build_const_obj_expr(*params_.expr_factory_,
                                                                  obj, value_expr))) {
            LOG_WARN("failed to build a literal for a file field", K(ret), K(r), K(c));
          } else if (OB_ISNULL(value_expr)) {
            ret = OB_ERR_UNEXPECTED;'''


def main():
    for path, old, new in ((CPP_PATH, CPP_OLD, CPP_NEW), (RES_PATH, RES_OLD, RES_NEW)):
        with open(path, "rb") as handle:
            raw = handle.read()
        crlf = raw.count(b"\r\n")
        text = raw.decode("utf-8").replace("\r\n", "\n")
        hits = text.count(old)
        if 1 != hits:
            raise SystemExit("[ABORT] %s anchor matched %d times" % (path, hits))
        text = text.replace(old, new, 1)
        out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(out)
        print("[OK] %-52s crlf=%d bytes=%d" % (path, crlf, len(out)))
    print("T_LIT left in resolver -> %s" % ("T_LIT" in open(RES_PATH).read()))


if "__main__" == __name__:
    main()
