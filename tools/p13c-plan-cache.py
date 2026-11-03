"""Prove the plan-cache fix: the same statement shape over four different files must give four
different answers, and a missing file must be an error rather than somebody else's row count."""
import pymysql

D = "/root/rivermind-data/fsdata/data_for_mysqltest_file_sql"
EXPECT = {"basic.csv": 3, "big.csv": 5000, "widen.csv": 4, "nope.csv": "error"}
c = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test",
                    autocommit=True).cursor()
c.execute("set session enable_file_sql = ON")
failed = []
for name, want in EXPECT.items():
    sql = "select count(*) as cc from read_csv('%s/%s') f" % (D, name)
    try:
        c.execute(sql)
        got = c.fetchall()[0][0]
    except pymysql.MySQLError as exc:
        got = "error" if want == "error" else str(exc.args[:2])
    ok = (got == want)
    if not ok:
        failed.append(name)
    print("[%s] %-12s want=%-6s got=%s" % ("PASS" if ok else "FAIL", name, want, got))
print("[SUMMARY] plan-cache isolation across paths failed=%s" % failed)
