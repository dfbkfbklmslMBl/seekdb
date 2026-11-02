"""Run the AI-function oracle's *non-network* assertions against a live seekdb and report actual vs expected.

tools/deploy/mysql_test/test_suite/ai_function/r/mysql/ai_function_enhancement.result points every
endpoint at http://0.0.0.0:8000 with access_key "mock-key", so most of it needs a mock AI service.
But several assertions are pure-function or DDL/registry behaviour, and those already tell us whether
the drafted patches describe work that is still needed — on a real server, without compiling anything.

Also deliberate: the ai_embed(NULL) case is run with NO mock listening. If the engine short-circuits on
NULL input the row yields NULL (the oracle's expectation); if it reaches HTTP first, we get a
connection error — which is exactly the gap patch 02 claims to close. So "error" here is a finding, not
a bug in this script.
"""

import json
import sys

import pymysql

EP = json.load(open("D:/scratch/seekdb-endpoint.json", encoding="utf-8"))
conn = pymysql.connect(host=EP["host"], port=EP["port"], user="root", charset="utf8mb4",
                       autocommit=True)
cur = conn.cursor()
cur.execute("SET ob_query_timeout = 100000000000")

CASES = [
    ("ai_prompt 多占位复用+json 参数",
     "select ai_prompt('{0}/{0}/{1}', json_array(1,true), json_object('k',2))",
     '{"args": [[1, true], {"k": 2}], "template": "{0}/{0}/{1}"}'),
    ("ai_prompt 走 LOB 列(longtext/text)",
     None,  # handled below because it needs two statements
     '{"args": ["lob"], "template": "{0}-ok"}'),
]


def report(label, sql, expected, got):
    if isinstance(got, Exception):
        verdict = "ERROR"
        shown = f"{type(got).__name__}: {str(got)[:150]}"
    else:
        shown = str(got)
        verdict = "MATCH" if shown == expected else "DIFF"
    print(f"[{verdict:5s}] {label}")
    print(f"        期望: {expected}")
    print(f"        实际: {shown}")
    print(f"        SQL : {sql[:120]}")


print("=== 版本", end=" ")
cur.execute("select version()")
print(cur.fetchone()[0])

# 1) ai_prompt pure function
sql = "select ai_prompt('{0}/{0}/{1}', json_array(1,true), json_object('k',2))"
try:
    cur.execute(sql)
    got = cur.fetchone()[0]
except Exception as e:  # noqa: BLE001
    got = e
report("ai_prompt 多占位复用 + json 参数", sql,
       '{"args": [[1, true], {"k": 2}], "template": "{0}/{0}/{1}"}', got)

# 2) ai_prompt over LOB columns
setup = [
    "drop table if exists test.ai_enhance_prompt",
    "create table test.ai_enhance_prompt(t longtext, a text)",
    "insert into test.ai_enhance_prompt values('{0}-ok', 'lob')",
]
sql2 = "select ai_prompt(t, a) from test.ai_enhance_prompt"
try:
    for statement in setup:
        cur.execute(statement)
    cur.execute(sql2)
    got = cur.fetchone()[0]
except Exception as e:  # noqa: BLE001
    got = e
report("ai_prompt 走 LOB 列(longtext/text)", sql2, '{"args": ["lob"], "template": "{0}-ok"}', got)
cur.execute("drop table if exists test.ai_enhance_prompt")

# 3) DDL: is provider=ollama accepted at all, and is it echoed verbatim? (patch 05 claims it must be added)
ddl = [
    "call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('gap_ollama_ep')",
    "call DBMS_AI_SERVICE.DROP_AI_MODEL('gap_ollama')",
    "call DBMS_AI_SERVICE.CREATE_AI_MODEL('gap_ollama', '{\"type\":\"completion\",\"model_name\":\"qwen2.5\"}')",
    "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('gap_ollama_ep', "
    "'{\"ai_model_name\":\"gap_ollama\",\"scope\":\"all\","
    "\"url\":\"http://0.0.0.0:8000/api/generate\",\"access_key\":\"\",\"provider\":\"ollama\"}')",
]
for statement in ddl:
    try:
        cur.execute(statement)
        if "CREATE_AI_MODEL_ENDPOINT" in statement:
            print("[OK   ] CREATE_AI_MODEL_ENDPOINT 接受 provider=ollama（未被拒绝）")
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {statement[:60]} -> {type(e).__name__}: {str(e)[:160]}")

sql3 = ("select provider from oceanbase.DBA_OB_AI_MODEL_ENDPOINTS "
        "where endpoint_name = 'gap_ollama_ep'")
try:
    cur.execute(sql3)
    row = cur.fetchone()
    got = row[0] if row else "（查不到该行）"
except Exception as e:  # noqa: BLE001
    got = e
report("DBA 视图回显 provider（期望原样 ollama）", sql3, "ollama", got)

# 4) ALTER to a different case, expect verbatim echo
try:
    cur.execute("call DBMS_AI_SERVICE.ALTER_AI_MODEL_ENDPOINT('gap_ollama_ep', '{\"provider\":\"Ollama\"}')")
    cur.execute(sql3)
    row = cur.fetchone()
    got = row[0] if row else "（查不到）"
except Exception as e:  # noqa: BLE001
    got = e
report("ALTER 后 provider 大小写原样保留", sql3, "Ollama", got)

# 5) NULL 传播：没有 mock 在监听，所以能区分「短路返回 NULL」与「先打 HTTP」
register = [
    "call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('gap_embed_ep')",
    "call DBMS_AI_SERVICE.DROP_AI_MODEL('gap_embed')",
    "call DBMS_AI_SERVICE.CREATE_AI_MODEL('gap_embed', '{\"type\":\"dense_embedding\",\"model_name\":\"text-embedding-v1\"}')",
    "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('gap_embed_ep', "
    "'{\"ai_model_name\":\"gap_embed\",\"scope\":\"all\","
    "\"url\":\"http://0.0.0.0:8000/compatible-mode/v1/embeddings\","
    "\"access_key\":\"mock-key\",\"provider\":\"openai\"}')",
]
for statement in register:
    try:
        cur.execute(statement)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] register: {type(e).__name__}: {str(e)[:120]}")

cur.execute("drop table if exists test.ai_enhance_batch")
cur.execute("create table test.ai_enhance_batch(id int primary key, content text)")
cur.execute("insert into test.ai_enhance_batch values(1,'first'),(2,null),(3,'third')")
sql5 = ("select id, json_length(ai_embed('gap_embed', content)) as dimension "
        "from test.ai_enhance_batch order by id")
try:
    cur.execute(sql5)
    rows = cur.fetchall()
    null_row = [r for r in rows if r[0] == 2]
    got = str(rows)
    verdict = "MATCH-if-2-is-NULL" if null_row and null_row[0][1] is None else "DIFF"
    print(f"[{verdict}] ai_embed 对 NULL 行的传播（oracle 期望该行 dimension 为 NULL）")
    print(f"        实际整表: {got}")
    print(f"        SQL : {sql5}")
except Exception as e:  # noqa: BLE001
    print("[ERROR] ai_embed(NULL) 没有短路 —— 它去打了 HTTP（8000 上没人监听）")
    print(f"        {type(e).__name__}: {str(e)[:200]}")

cur.execute("drop table if exists test.ai_enhance_batch")
for statement in ["call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('gap_embed_ep')",
                  "call DBMS_AI_SERVICE.DROP_AI_MODEL('gap_embed')",
                  "call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('gap_ollama_ep')",
                  "call DBMS_AI_SERVICE.DROP_AI_MODEL('gap_ollama')"]:
    try:
        cur.execute(statement)
    except Exception:  # noqa: BLE001
        pass
conn.close()
