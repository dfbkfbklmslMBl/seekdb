"""Patch 09b: the file-scan TU must not depend on ObSqlString.

The single-TU checker found `variable has incomplete type 'ObSqlString'` in load_glob: the header
that declares it is not in this translation unit's include set, and pulling another umbrella header
in for one path join is the wrong trade. The path is built with snprintf into arena memory instead,
with the length bounds checked rather than trusted.
"""

CPP_PATH = "src/sql/file_scan/ob_file_scan_utils.cpp"

OLD = u'''            ObSqlString path_str;
            char *path_buf = NULL;
            if (OB_FAIL(path_str.append_fmt("%s/%s", dir_buf, entry->d_name))) {
              LOG_WARN("failed to build a path", K(ret));
            } else if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(path_str.length())))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              MEMCPY(path_buf, path_str.ptr(), path_str.length());
              ObString full;
              full.assign_ptr(path_buf, static_cast<int32_t>(path_str.length()));
              if (OB_FAIL(found.push_back(full))) {'''

NEW = u'''            const int64_t dname_len = static_cast<int64_t>(strlen(entry->d_name));
            const int64_t dir_len = static_cast<int64_t>(strlen(dir_buf));
            const int64_t need = dir_len + dname_len + 2;
            char *path_buf = NULL;
            char *full_buf = NULL;
            int written = 0;
            if (OB_ISNULL(path_buf = static_cast<char *>(alloc.alloc(need)))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else if (FALSE_IT(written = snprintf(path_buf, need, "%s/%s", dir_buf, entry->d_name))) {
            } else if (written <= 0 || written >= need) {
              ret = OB_SIZE_OVERFLOW;
              LOG_WARN("failed to build a glob path", K(ret), K(dir_len), K(dname_len));
            } else if (OB_ISNULL(full_buf = static_cast<char *>(alloc.alloc(written)))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            }
            if (OB_SUCC(ret)) {
              MEMCPY(full_buf, path_buf, written);
              ObString full;
              full.assign_ptr(full_buf, static_cast<int32_t>(written));
              if (OB_FAIL(found.push_back(full))) {'''


def main():
    with open(CPP_PATH, "rb") as handle:
        raw = handle.read()
    crlf = raw.count(b"\r\n")
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if OLD not in text:
        raise SystemExit("[ABORT] ObSqlString block not found (patch already applied?)")
    if text.count(OLD) != 1:
        raise SystemExit("[ABORT] anchor is not unique")
    text = text.replace(OLD, NEW, 1)
    out = text.replace("\n", "\r\n").encode("utf-8") if crlf else text.encode("utf-8")
    with open(CPP_PATH, "wb") as handle:
        handle.write(out)
    print("[OK] %-52s crlf=%d bytes=%d" % (CPP_PATH, crlf, len(out)))
    print("  ObSqlString left in the file -> %s" % ("ObSqlString" in text))


if "__main__" == __name__:
    main()
