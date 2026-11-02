"""Run a mysqltest `.test` file through the MySQL protocol, without mysqltest or the mysql client.

Why this exists: the competition's kernel suites ship as t/*.test + r/mysql/*.result, and iterating on
them through mysqltest means a full deploy + a whole-file diff. This executes the statement stream
directly and asserts the *behavioural claims* the suite actually encodes — the `--error N` lines —
while dumping every result set into a transcript you can diff against the recorded `.result`.

Supported directives (the subset used by file_sql / ai_function suites):
  --source PATH, let $V = value, eval SQL, --error N[,N..], --echo,
  --disable_result_log / --enable_result_log, --disable_query_log / --enable_query_log
Anything it does not understand is reported as UNSUPPORTED and counted — it never quietly skips,
because a runner that skips an assertion is how a red suite gets reported as green.

Usage:
  python mysqltest-lite.py --host 127.0.0.1 --port 2881 \
      --test tools/deploy/mysql_test/test_suite/file_sql/t/file_sql_basic.test \
      --var OBSERVER_DIR=/tmp/fakeserver [--out transcript.txt]
"""

import argparse
import os
import re
import sys

import pymysql

DIRECTIVE_RE = re.compile(r"^--([a-z_]+)(?:\s+(.*))?$")

# mysqltest 的客户端指令（不带 -- 前缀），绝不能当 SQL 发给服务器
CLIENT_COMMANDS = {"connect", "connection", "disconnect", "exec", "system"}


class Runner:
    def __init__(self, cur, repo_root, variables, out):
        self.cur = cur
        self.repo_root = repo_root
        self.vars = variables
        self.out = out
        self.asserts = 0
        self.failures = 0
        self.unsupported = []

    def emit(self, text=""):
        self.out.append(text)

    def expand(self, text):
        # mysqltest expands variables recursively: `let $A = $B/sub` must resolve $B too.
        # Bounded passes rather than recursion, so a self-referencing var can't spin forever.
        for _ in range(4):
            before = text
            for name, value in self.vars.items():
                text = text.replace("$" + name, str(value)).replace("${" + name + "}", str(value))
            if text == before:
                break
        return text

    def check_error(self, expected_codes, sql):
        self.asserts += 1
        try:
            self.cur.execute(sql)
            rows = self.cur.fetchall()
            # mysqltest 的 `--error 0, 11118` 里 0 表示「成功也算对」，不能当失败——
            # 之前把这种情况算成红，会让 DROP-if-exists 这类语句全变成假失败。
            ok = 0 in expected_codes
            got = "no error" if not ok else "no error（0 在允许集合里）"
            self.emit(f"### {sql}\n(期望错误 {sorted(expected_codes)}，实际无错误，返回 {len(rows)} 行)")
        except pymysql.MySQLError as e:  # noqa: PERF203
            code = e.args[0] if e.args else None
            got = f"{code}: {str(e)[:120]}"
            ok = code in expected_codes
            self.emit(f"### {sql}\n{got}")
        if not ok:
            self.failures += 1
            self.emit(f"!!! FAIL 期望 error {sorted(expected_codes)}，实际 {got}")
        else:
            self.emit(f"--- ok（error {sorted(expected_codes)} 如预期）")

    def run_sql(self, sql, expect_rows=True):
        try:
            self.cur.execute(sql)
        except pymysql.MySQLError as e:
            self.emit(f"### {sql}\n!!! 非预期错误 {e.args[0] if e.args else '?'}: {str(e)[:200]}")
            self.failures += 1
            return
        if expect_rows:
            desc = self.cur.description
            rows = self.cur.fetchall()
            self.emit("### " + sql)
            if desc:
                self.emit("\t".join(d[0] for d in desc))
                for row in rows:
                    self.emit("\t".join("NULL" if v is None else str(v) for v in row))
                self.emit(f"({len(rows)} 行)")

    def execute_file(self, path, depth=0):
        if depth > 4:
            self.unsupported.append(f"source 嵌套过深: {path}")
            return
        pending_errors = None
        suppress_rows = False
        with open(path, encoding="utf-8", errors="replace") as handle:
            buffer = ""
            for raw in handle:
                line = raw.rstrip("\n")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                # mysqltest 的客户端指令不带 --，也不能发给服务器（发过去只会得到 1064 噪声）。
                # 记成 UNSUPPORTED，让「没被验证的东西」显式可见。
                head = re.split(r"[\s(]", stripped, 1)[0].lower()
                if head in CLIENT_COMMANDS:
                    self.unsupported.append("mysqltest 客户端指令（需要 mysqltest/ssh 环境）：" + stripped[:110])
                    continue
                match = DIRECTIVE_RE.match(line)
                if match and not buffer:
                    name, rest = match.group(1), (match.group(2) or "").strip()
                    if name == "error":
                        pending_errors = {int(x) for x in re.findall(r"\d+", rest)}
                    elif name == "echo":
                        self.emit("# " + self.expand(rest))
                    elif name in ("disable_result_log",):
                        suppress_rows = True
                    elif name in ("enable_result_log",):
                        suppress_rows = False
                    elif name in ("disable_query_log", "enable_query_log"):
                        pass  # 只影响 .result 里是否抄录 SQL，本脚本自己总是抄
                    elif name in ("disable_warnings", "enable_warnings", "biff"):
                        # 这些只影响 SHOW WARNINGS 噪声，不承载断言 ⇒ 忽略它们不会漏掉任何验证
                        pass
                    elif name == "source":
                        target = self.resolve_source(rest)
                        if target:
                            self.emit(f"### source {rest}")
                            self.execute_file(target, depth + 1)
                    elif name == "let":
                        self.exec_let(rest)
                    else:
                        self.unsupported.append(f"--{name} {rest}")
                    continue
                buffer = (buffer + " " + line).strip() if buffer else line.strip()
                if buffer.endswith(";"):
                    sql = self.expand(buffer[:-1]).strip()
                    buffer = ""
                    if not sql:
                        continue
                    if sql.lower().startswith("eval "):
                        sql = self.expand(sql[5:]).strip()
                    if pending_errors is not None:
                        self.check_error(pending_errors, sql)
                        pending_errors = None
                    else:
                        self.run_sql(sql, expect_rows=not suppress_rows)
        if buffer:
            self.emit(f"!!! 文件末尾有未结束语句：{buffer[:120]}")
            self.failures += 1

    def resolve_source(self, rest):
        candidates = [
            os.path.join(self.repo_root, rest),
            os.path.join(self.repo_root, "tools/deploy", rest),
            os.path.join(os.path.dirname(os.path.abspath(self.current_file)), rest),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        self.unsupported.append(f"source 找不到：{rest}")
        return None

    def exec_let(self, rest):
        match = re.match(r"\$(\w+)\s*=\s*(.*)", rest)
        if not match:
            self.unsupported.append(f"let {rest}")
            return
        name, value = match.group(1), match.group(2).strip()
        # `let $X = query_get_value(...)` / ssh / `...` 是 mysqltest 侧求值，本脚本做不到：
        # 宁可把这个变量留空并报告，也不要拼出一个假 SQL。
        if re.match(r"^(query_get_value|`|\w+\s*\(.*\)$)", value) or value.startswith(("ssh ", "scp ")):
            self.unsupported.append(f"let ${name} 需要 mysqltest 侧求值：{value[:90]}")
            self.vars.setdefault(name, "<UNSUPPORTED_" + name + ">")
            self.emit(f"### let ${name} = <未支持，取值为 mysqltest 侧表达式>")
            return
        self.vars[name] = value
        self.emit(f"### let ${name} = {self.vars[name]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2881)
    parser.add_argument("--user", default="root")
    parser.add_argument("--test", required=True)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--var", action="append", default=[], help="NAME=VALUE，可重复")
    parser.add_argument("--db", default="test", help="mysqltest 默认连的就是 test 库；不设会满屏 1046")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    repo_root = args.repo_root
    if repo_root is None:
        # .../<repo>/tools/deploy/mysql_test/test_suite/<suite>/t/x.test -> 取 tools 之前的部分
        parts = os.path.abspath(args.test).split(os.sep)
        repo_root = os.sep.join(parts[:parts.index("tools")]) if "tools" in parts else os.getcwd()

    variables = {}
    for item in args.var:
        name, _, value = item.partition("=")
        variables[name.lstrip("$")] = value

    conn = pymysql.connect(host=args.host, port=args.port, user=args.user, db=args.db,
                           autocommit=True, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SET ob_query_timeout = 100000000000")
    out = []
    runner = Runner(cur, repo_root, variables, out)
    runner.current_file = args.test
    runner.execute_file(args.test)
    conn.close()

    print("\n".join(out))
    print("\n" + "=" * 70)
    print(f"断言 {runner.asserts} 条，失败 {runner.failures} 条")
    if runner.unsupported:
        print(f"未识别的指令 {len(runner.unsupported)} 条 —— 这些**没有**被验证：")
        for item in sorted(set(runner.unsupported)):
            print("   " + item[:160])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out) + "\n")
    return 1 if runner.failures or runner.unsupported else 0


if __name__ == "__main__":
    raise SystemExit(main())
