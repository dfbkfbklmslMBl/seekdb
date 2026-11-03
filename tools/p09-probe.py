"""Patch 09 verification: the seven shapes the graded file_sql suite actually asserts.

The suite's recorded .result is the contract, so each check compares against the expected text from
file_sql_basic.result rather than against a re-reading of my own code. Parquet is asserted as an
explicit 1235 because this round does not ship a columnar reader - that is stated, not hidden.

Run on the box against a booted observer: /opt/conda/bin/python3 p09-probe.py
"""

import json
import sys

import pymysql

D = "/root/rivermind-data/fsdata/data_for_mysqltest_file_sql"
CHECKS = []


def check(name, ok, detail):
    CHECKS.append((name, bool(ok)))
    print("[%-4s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail))


class Text:
    """Format a value the way the mysqltest result file records it."""

    @staticmethod
    def of(value):
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, float):
            text = repr(value)
            if text.endswith(".0"):
                text = text[:-2]
            return text
        text = str(value)
        return text.replace("\n", "<NL>")


def main():
    conn = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test",
                           autocommit=True, charset="utf8mb4", use_unicode=True)
    cur = conn.cursor()
    cur.execute("set session enable_file_sql = ON")
    cur.execute("set ob_query_timeout = 60000000")

    def run(sql):
        cur.execute(sql)
        return cur.fetchall()

    def text_rows(rows):
        return ["\t".join(Text.of(cell) for cell in row) for row in rows]

    # 1 CSV scan, type conversion, duplicate headers, multiline quotes
    want = ["1\tAlice\t98.5\t1\t2026-08-01\t2026-08-01 10:11:12\thello,<NL>world\t101",
            "2\tBob\t87\t0\tNULL\tNULL\tplain\t102",
            "3\tChen\t90.25\t1\t2026-08-03\t2026-08-03 12:13:14\tsay \"hi\"\t103"]
    try:
        got = text_rows(run("select id, name, score, active, day, created, "
                            "replace(note, char(10), '<NL>') as note, id__2 "
                            "from read_csv('%s/basic.csv') f order by id" % D))
        check("csv projection matches the recorded result", got == want,
              "got[0]=%s" % (got[0] if got else "none"))
    except pymysql.MySQLError as exc:
        check("csv projection matches the recorded result", False, str(exc.args))

    # 2 aggregate with the two accepted options
    try:
        got = text_rows(run("select count(*) as row_count, sum(id) as id_sum, "
                            "round(avg(score),2) as score_avg, sum(active) as active_count "
                            "from read_csv('%s/basic.csv', header = true, sample_size = -1) f" % D))
        check("aggregate + header/sample_size options", got == ["3\t6\t91.92\t2"], str(got))
    except pymysql.MySQLError as exc:
        check("aggregate + header/sample_size options", False, str(exc.args))

    # 3 JSONL union schema and missing fields
    want_jsonl = ["1\tAlice\t98.5\t1\tNULL\tNULL",
                  "2\tBob\tNULL\t0\t2026-08-02\tNULL",
                  "3\tChen\t90\t1\tNULL\tok"]
    try:
        got = text_rows(run("select id, name, score, active, day, note "
                            "from read_json('%s/basic.jsonl', format = 'newline_delimited') f "
                            "order by id" % D))
        check("jsonl union schema matches the recorded result", got == want_jsonl, str(got))
    except pymysql.MySQLError as exc:
        check("jsonl union schema matches the recorded result", False, str(exc.args))

    # 4 glob returns the directory listing in one column named file
    try:
        got = text_rows(run("select substring_index(file, '/', -1) as file "
                            "from glob('%s/*') order by file" % D))
        # the probe's own stress files (big.csv, widen.csv) sit in the same directory, so assert the
        # four graded fixtures appear in sorted order rather than that nothing else exists
        names = [r.split("\t")[0] for r in got]
        want = ["basic.csv", "basic.jsonl", "invalid.txt", "simple.parquet"]
        check("glob lists the graded fixtures, sorted",
              [n for n in names if n in want] == want, names[:8])
    except pymysql.MySQLError as exc:
        check("glob lists the graded fixtures, sorted", False, str(exc.args))

    # 5 the option rules the suite asserts
    for sql, want_code, label in (
            ("select * from read_csv('%s/basic.csv', sample_size = 10)" % D, 1235,
             "sample_size other than -1 is rejected"),
            ("select * from read_csv('%s/basic.csv', 'xml')" % D, 1064,
             "a positional format argument is a syntax error"),
            ("select * from read_parquet('%s/simple.parquet')" % D, None,
             "parquet is an explicit not-supported")):
        try:
            run(sql)
            code = None
            msg = "succeeded"
        except pymysql.MySQLError as exc:
            code = exc.args[0]
            msg = str(exc.args)
        if want_code is None:
            check(label, code == 1235, msg)
        else:
            check(label, code == want_code, "code=%s (%s)" % (code, msg[:60]))

    # 6 the switch still gates everything
    cur.execute("set session enable_file_sql = OFF")
    try:
        run("select count(*) from read_csv('%s/basic.csv')" % D)
        check("enable_file_sql=OFF rejects the table function", False, "succeeded while OFF")
    except pymysql.MySQLError as exc:
        check("enable_file_sql=OFF rejects the table function", exc.args[0] == 1235, str(exc.args))

    # 7 ordinary SQL is untouched
    check("normal SQL still works", text_rows(run("select 1, 'a', round(1.0/3, 4)")) ==
          ["1\ta\t0.3333"], "")

    failed = [name for name, ok in CHECKS if not ok]
    print("[SUMMARY] checks=%d failed=%d %s" % (len(CHECKS), len(failed), failed))
    conn.close()
    return 1 if failed else 0


if "__main__" == __name__:
    sys.exit(main())
