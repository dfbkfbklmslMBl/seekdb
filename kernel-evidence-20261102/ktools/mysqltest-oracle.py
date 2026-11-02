"""Replay a mysqltest `.test` file and diff the output against the recorded `.result`.

Why: `mysqltest-lite.py` only asserts the `--error N` lines, so a suite can be "green" there while the
result rows are wrong. The competition grades by diffing against the checked-in `r/mysql/*.result`, so
this runner reproduces mysqltest's own echo format (statement text with each line's leading whitespace
stripped, then a tab-separated header/rows block, `ERROR <sqlstate>: <msg>` on failure) and reports the
diff per suite -- which maps directly onto the scored sub-items.

Deliberately strict: anything it cannot express is emitted as a `[UNSURE]` marker line instead of being
silently dropped, so an unverified statement can never look like a matching line.

Usage:
  python3 mysqltest-oracle.py --test X.test --result X.result [--out actual.result] [--mtr-root DIR]
"""

import argparse
import difflib
import os
import re
import sys

import pymysql

DIRECTIVE_RE = re.compile(r"^--([a-z_]+)(?:[ \t]+(.*))?$")
CLIENT_COMMANDS = {"connect", "connection", "disconnect", "exec", "system"}


class Oracle:
    def __init__(self, cur, variables, mtr_root):
        self.cur = cur
        self.vars = variables
        self.mtr_root = mtr_root
        self.out = []
        self.unsupported = []
        self.stmts = 0
        self.errors = 0
        self.current_file = ""
        self.query_log = True
        self.result_log = True

    # ---------------------------------------------------------------- output
    def emit(self, text):
        self.out.append(text)

    def unsure(self, why):
        self.unsupported.append(why)
        self.emit("[UNSURE] " + why)

    def expand(self, text):
        for _ in range(4):
            before = text
            for name, value in self.vars.items():
                text = text.replace("$" + name, str(value)).replace("${" + name + "}", str(value))
            if text == before:
                break
        return text

    # ---------------------------------------------------------------- sql
    @staticmethod
    def fmt(value):
        if value is None:
            return "NULL"
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def run_statement(self, lines, expected_errors):
        sql = " ".join(l.strip() for l in lines).strip()
        if not sql:
            return
        sql = self.expand(sql)
        self.stmts += 1
        if self.query_log:
            # mysqltest echoes the statement text with each source line's leading whitespace removed
            self.emit("\n".join(l.strip() for l in lines if l.strip()))
        try:
            self.cur.execute(sql)
        except pymysql.MySQLError as exc:
            self.errors += 1
            code = exc.args[0] if exc.args else "?"
            msg = exc.args[1] if len(exc.args) > 1 else str(exc)
            state = getattr(exc, "sqlstate", None) or "HY000"
            if len(msg) > 300:
                msg = msg[:300] + "...<TRUNCATED>"
            if expected_errors is not None and code in expected_errors:
                # The shipped results disagree on purpose: `--error 0,N` suites (ai_function_enhancement)
                # recorded no line at all for the tolerated error, while `--error N` suites (ai_prompt)
                # recorded "ERROR <sqlstate>: <msg>". Reproduce each, otherwise 25 assertions look red.
                if 0 in expected_errors:
                    return
                self.emit(f"ERROR {state}: {msg}")
                return
            self.emit(f"ERROR {state}: {msg}")
            self.emit("[UNSURE] %s error %s not in %s" % (
                "expected" if expected_errors is not None else "UNEXPECTED",
                code, sorted(expected_errors) if expected_errors is not None else []))
            return
        if expected_errors is not None and 0 not in expected_errors:
            self.emit("[UNSURE] expected error %s but statement succeeded" % sorted(expected_errors))
        desc = self.cur.description
        if desc and self.result_log:
            self.emit("\t".join(d[0] for d in desc))
            for row in self.cur.fetchall():
                self.emit("\t".join(self.fmt(v) for v in row))

    # ---------------------------------------------------------------- files
    def execute_file(self, path, depth=0):
        if depth > 4:
            self.unsure(f"source nesting too deep: {path}")
            return
        prev_file = self.current_file
        self.current_file = path
        pending_errors = None
        buffer = []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n").rstrip("\r")
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                head = re.split(r"[\s(]", stripped, 1)[0].lower()
                if head in CLIENT_COMMANDS and not buffer:
                    if head in ("connection", "disconnect") and re.match(
                            r"^(connection|disconnect)\s+default\s*;?$", stripped, re.I):
                        continue
                    if head == "connect":
                        continue
                    self.unsure("mysqltest client command not replayed: " + stripped[:110])
                    continue
                match = DIRECTIVE_RE.match(line)
                if match and not buffer:
                    name, rest = match.group(1), (match.group(2) or "").strip()
                    if name == "error":
                        pending_errors = {int(x) for x in re.findall(r"\d+", rest)}
                    elif name == "echo":
                        self.emit(self.expand(rest))
                    elif name == "disable_query_log":
                        self.query_log = False
                    elif name == "enable_query_log":
                        self.query_log = True
                    elif name == "disable_result_log":
                        self.result_log = False
                    elif name == "enable_result_log":
                        self.result_log = True
                    elif name in ("disable_warnings", "enable_warnings", "biff",
                                 "enable_sorted_result", "sorted_result",
                                 "disable_abort_on_error", "enable_abort_on_error"):
                        pass
                    elif name == "source":
                        target = self.resolve_source(rest)
                        if target:
                            self.execute_file(target, depth + 1)
                    elif name == "let":
                        self.exec_let(rest)
                    else:
                        self.unsure(f"--{name} {rest}")
                    continue
                buffer.append(line)
                if stripped.endswith(";"):
                    self.run_statement(buffer, pending_errors)
                    pending_errors = None
                    buffer = []
        if buffer:
            self.unsure("unterminated statement at EOF: " + " ".join(buffer)[:120])
        self.current_file = prev_file

    def resolve_source(self, rest):
        rest = self.expand(rest)
        here = os.path.dirname(os.path.abspath(self.current_file))
        for candidate in (
            os.path.join(self.mtr_root or "", rest),
            os.path.join(here, rest),
            os.path.join(here, "..", "..", "..", rest),
            rest,
        ):
            if candidate and os.path.isfile(candidate):
                return candidate
        self.unsure("source not found: " + rest)
        return None

    def exec_let(self, rest):
        match = re.match(r"\$(\w+)\s*=\s*(.*)", rest)
        if not match:
            self.unsure("let " + rest)
            return
        name, value = match.group(1), match.group(2).strip()
        if re.match(r"^(query_get_value|`|\w+\s*\(.*\)$)", value) or value.startswith(("ssh ", "scp ")):
            self.vars.setdefault(name, "")
            self.unsure(f"let ${name} needs mysqltest-side evaluation: {value[:90]}")
            return
        self.vars[name] = self.expand(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2881)
    parser.add_argument("--user", default="root")
    parser.add_argument("--db", default="test")
    parser.add_argument("--test", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--var", action="append", default=[])
    parser.add_argument("--mtr-root", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    variables = {}
    for item in args.var:
        name, _, value = item.partition("=")
        variables[name.lstrip("$")] = value

    mtr_root = args.mtr_root
    if mtr_root is None:
        parts = os.path.abspath(args.test).split(os.sep)
        mtr_root = os.sep.join(parts[: parts.index("mysql_test") + 1]) if "mysql_test" in parts else None

    conn = pymysql.connect(host=args.host, port=args.port, user=args.user, db=args.db,
                           autocommit=True, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SET ob_query_timeout = 100000000000")
    oracle = Oracle(cur, variables, mtr_root)
    oracle.current_file = args.test
    oracle.execute_file(args.test)
    conn.close()

    actual_text = "\n".join(oracle.out) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(actual_text)

    with open(args.result, "rb") as handle:
        blob = handle.read()
    try:
        expected_text = blob.decode("utf-8")
    except UnicodeDecodeError:
        # some recorded results carry raw bytes from a LOB/binary assertion -- keep them comparable
        expected_text = blob.decode("utf-8", "surrogateescape")
    actual = [l.rstrip() for l in actual_text.split("\n")]
    expected = [l.rstrip() for l in expected_text.split("\n")]
    # trailing empty line from the final newline is not a difference
    while actual and actual[-1] == "":
        actual.pop()
    while expected and expected[-1] == "":
        expected.pop()
    # The recorded .test/.result pairs are inconsistent about echoing a statement's continuation lines
    # (ai_function_enhancement drops the indent, ai_model_ddl keeps the indent that is inside a string
    # literal), so indentation-only differences are reported separately from substantive ones.
    strict = actual == expected
    trim_a = [l.lstrip() for l in actual]
    trim_e = [l.lstrip() for l in expected]
    matcher = difflib.SequenceMatcher(a=trim_e, b=trim_a, autojunk=False)
    same = sum(block.size for block in matcher.get_matching_blocks())
    unmatched = [l for l in actual if l.startswith("[UNSURE]")]
    print(f"[ORACLE] test={os.path.basename(args.test)} expected_lines={len(expected)} "
          f"actual_lines={len(actual)} matched={same} strict={int(strict)} "
          "indent_only=" + str(int(not strict and trim_a == trim_e)) + " "
          f"stmts={oracle.stmts} sql_errors={oracle.errors} unsure={len(unmatched)}")
    if trim_a != trim_e:
        diff = list(difflib.unified_diff(trim_e, trim_a, "expected", "actual", lineterm="", n=1))
        print("\n".join(diff[:120]))
        if len(diff) > 120:
            print(f"... ({len(diff) - 120} more diff lines in {args.out})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
