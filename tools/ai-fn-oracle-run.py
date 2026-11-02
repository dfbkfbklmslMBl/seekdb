"""Run the official ai_function_enhancement oracle against a live seekdb, with a permissive mock AI service.

Why: r/mysql/ai_function_enhancement.result points every endpoint at http://0.0.0.0:8000 with
access_key "mock-key", so the graded assertions never talk to a real provider — they need a mock.
This script starts that mock in-process and then executes the oracle's statements one by one, printing
expected vs actual per assertion. On an UNPATCHED build the result is the empirical gap list (which of
the 5 drafted patches describes work that is actually still needed); on a PATCHED build the same
script is the pass/fail harness.

The mock answers in every response shape the engine's parsers might read (openai
choices[].message.content, openai data[].embedding, dashscope output.*, siliconflow results, ollama
response/embedding), because reading each branch of ob_ai_func_utils.cpp costs more than being
permissive. It is a test double, not an implementation of any provider.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pymysql

DIM = 768
VEC = [0.0123] * DIM
EP = json.load(open("D:/scratch/seekdb-endpoint.json", encoding="utf-8"))


def body_for(path: str, payload: dict) -> dict:
    if "embeddings" in path or path.endswith("/api/embed"):
        # openai: data[].embedding ; dashscope: output.embeddings ; ollama: embedding
        return {"data": [{"embedding": VEC, "index": 0}],
                "embeddings": [VEC],
                "output": {"embeddings": [{"embedding": VEC, "text_index": 0}],
                           "embedding": VEC},
                "embedding": VEC,
                "model": payload.get("model", "mock"),
                "usage": {"prompt_tokens": 1, "total_tokens": 1}}
    if "rerank" in path:
        docs = payload.get("documents") or payload.get("docs") or []
        count = len(docs) if isinstance(docs, list) else 4
        count = count or 4
        results = [{"index": i, "relevance_score": round(1.0 - 0.1 * i, 3),
                    "document": {"text": f"doc{i}"}} for i in range(count)]
        return {"results": results, "output": {"results": results},
                "model": payload.get("model", "mock-rerank")}
    # chat/completions, ollama /api/generate, completions
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "mock answer"},
                         "finish_reason": "stop"}],
            "content": "mock answer",
            "response": "mock answer",
            "message": {"content": "mock answer"},
            "output": {"text": "mock answer", "finish_reason": "stop",
                       "choices": [{"message": {"content": "mock answer"}}]},
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            payload = {}
        answer = json.dumps(body_for(self.path, payload)).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def do_GET(self):  # noqa: N802  health probes from the engine, if any
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):  # keep stdout for the assertion table
        pass


def start_mock(port: int = 8000) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


CUR = None


def run(label, sql, expected, *, fetch="scalar"):
    global CUR
    try:
        CUR.execute(sql)
        if fetch == "scalar":
            row = CUR.fetchone()
            got = row[0] if row else None
        elif fetch == "rows":
            got = CUR.fetchall()
        else:
            CUR.fetchall()
            got = "OK"
    except Exception as e:  # noqa: BLE001
        got = f"ERROR {type(e).__name__}: {str(e)[:130]}"
    status = "PASS" if str(got) == str(expected) else "DIFF"
    if str(got).startswith("ERROR"):
        status = "ERR "
    print(f"[{status}] {label}")
    if status != "PASS":
        print(f"        期望 {expected!r}")
        print(f"        实际 {got!r}")


def main():
    server = start_mock()
    conn = pymysql.connect(host=EP["host"], port=EP["port"], user="root", charset="utf8mb4",
                           autocommit=True)
    global CUR
    CUR = conn.cursor()
    CUR.execute("SET ob_query_timeout = 100000000000")
    CUR.execute("select version()")
    print(f"engine {CUR.fetchone()[0]} · mock on 0.0.0.0:8000 · oracle 逐条对照\n")

    def call(name, sql):
        try:
            CUR.execute(sql)
        except Exception as e:  # noqa: BLE001
            print(f"[setup-skip] {name}: {str(e)[:110]}")

    for model, spec in (("enhance_complete", '{"type":"completion","model_name":"qwen3-32b"}'),
                        ("enhance_embed", '{"type":"dense_embedding","model_name":"text-embedding-v1"}'),
                        ("enhance_rerank", '{"type":"rerank","model_name":"BAAI/bge-reranker-v2-m3"}')):
        call("DROP_MODEL_OLD", f"call DBMS_AI_SERVICE.DROP_AI_MODEL('{model}')")
        call("DROP_EP_OLD", f"call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('{model}_ep')")
        call("CREATE_MODEL", f"call DBMS_AI_SERVICE.CREATE_AI_MODEL('{model}', '{spec}')")

    call("EP_COMPLETE", "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('enhance_complete_ep', "
         "'{\"ai_model_name\":\"enhance_complete\",\"scope\":\"all\",\"url\":"
         "\"http://0.0.0.0:8000/compatible-mode/v1/chat/completions\",\"access_key\":\"mock-key\","
         "\"provider\":\"openai\"}')")
    call("EP_EMBED", "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('enhance_embed_ep', "
         "'{\"ai_model_name\":\"enhance_embed\",\"scope\":\"all\",\"url\":"
         "\"http://0.0.0.0:8000/compatible-mode/v1/embeddings\",\"access_key\":\"mock-key\","
         "\"provider\":\"openai\"}')")
    call("EP_RERANK", "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('enhance_rerank_ep', "
         "'{\"ai_model_name\":\"enhance_rerank\",\"scope\":\"all\",\"url\":"
         "\"http://0.0.0.0:8000/v1/rerank\",\"access_key\":\"mock-key\",\"provider\":\"siliconflow\"}')")

    print("\n-- 1. ai_prompt 的 json 参数与 LOB 支持（草稿补丁 01） --")
    run("ai_prompt 多占位复用 + json_array/json_object",
        "select ai_prompt('{0}/{0}/{1}', json_array(1,true), json_object('k',2))",
        '{"args": [[1, true], {"k": 2}], "template": "{0}/{0}/{1}"}')
    CUR.execute("drop table if exists test.ai_enhance_prompt")
    CUR.execute("create table test.ai_enhance_prompt(t longtext, a text)")
    CUR.execute("insert into test.ai_enhance_prompt values('{0}-ok', 'lob')")
    run("ai_prompt 接受 longtext 模板 + text 参数（LOB）",
        "select ai_prompt(t, a) from test.ai_enhance_prompt",
        '{"args": ["lob"], "template": "{0}-ok"}')

    print("\n-- 2. ai_complete / ai_embed / ai_rerank 正常路径（应已可用） --")
    run("ai_complete 返回非空",
        "select length(ai_complete('enhance_complete', 'hello', null)) > 0", "1")
    run("ai_complete 接受 ai_prompt 结果作第二个参数 + config",
        "select length(ai_complete('enhance_complete', "
        "ai_prompt('describe {0}', json_object('text','你好')), "
        "'{\"temperature\":0.2,\"max_tokens\":16}')) > 0", "1")
    run("ai_embed 默认维度 768",
        "select json_length(ai_embed('enhance_embed', 'single-row'))", "768")
    run("ai_embed 指定维度参数被接受",
        "select json_length(ai_embed('enhance_embed', 'dimension-row', 768))", "768")

    print("\n-- 3. NULL 传播（草稿补丁 02） --")
    CUR.execute("drop table if exists test.ai_enhance_batch")
    CUR.execute("create table test.ai_enhance_batch(id int primary key, content text)")
    CUR.execute("insert into test.ai_enhance_batch values(1,'first'),(2,null),(3,'third')")
    run("ai_embed 遇 NULL 行返回 NULL 而不是报错",
        "select id, json_length(ai_embed('enhance_embed', content)) as dimension "
        "from test.ai_enhance_batch order by id",
        ((1, 768), (2, None), (3, 768)), fetch="rows")

    print("\n-- 4. ai_rerank 的文档键（草稿补丁 03） --")
    run("rerank 返回条数 = 文档数",
        "select json_length(ai_rerank('enhance_rerank','apple',"
        "'[\"apple\",\"banana\",\"fruit\",\"vegetable\"]', null))", "4")
    run("对象数组 + doc_key='text'：首条 id 与条数",
        "select json_unquote(json_extract(r, '$[0].id')) as first_id, json_length(r) as rerank_count "
        "from (select ai_rerank('enhance_rerank','apple',"
        "'[{\"id\":\"a\",\"text\":\"apple\"},{\"id\":\"b\",\"text\":\"banana\"}]','text') as r) x",
        ("a", "2"))

    print("\n-- 5. ollama provider（草稿补丁 05，空 access_key 是前置） --")
    call("CREATE_OLLAMA_MODEL", "call DBMS_AI_SERVICE.CREATE_AI_MODEL('enhance_ollama_complete', "
         "'{\"type\":\"completion\",\"model_name\":\"qwen2.5\"}')")
    call("CREATE_OLLAMA_EP", "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT("
         "'enhance_ollama_complete_ep', '{\"ai_model_name\":\"enhance_ollama_complete\",\"scope\":\"all\","
         "\"url\":\"http://0.0.0.0:8000/api/generate\",\"access_key\":\"\",\"provider\":\"ollama\"}')")
    run("provider 原样回显 ollama（小写）",
        "select provider from oceanbase.DBA_OB_AI_MODEL_ENDPOINTS "
        "where endpoint_name = 'enhance_ollama_complete_ep'", "ollama")
    call("ALTER_OLLAMA_EP", "call DBMS_AI_SERVICE.ALTER_AI_MODEL_ENDPOINT("
         "'enhance_ollama_complete_ep', '{\"provider\":\"Ollama\"}')")
    run("ALTER 后大小写仍原样保留 Ollama",
        "select provider from oceanbase.DBA_OB_AI_MODEL_ENDPOINTS "
        "where endpoint_name = 'enhance_ollama_complete_ep'", "Ollama")
    run("ollama /api/generate 走通（ai_complete 非空）",
        "select length(ai_complete('enhance_ollama_complete', 'hello')) > 0", "1")
    call("CREATE_OLLAMA_EMBED", "call DBMS_AI_SERVICE.CREATE_AI_MODEL('enhance_ollama_embed', "
         "'{\"type\":\"dense_embedding\",\"model_name\":\"nomic-embed-text\"}')")
    call("CREATE_OLLAMA_EMBED_EP", "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT("
         "'enhance_ollama_embed_ep', '{\"ai_model_name\":\"enhance_ollama_embed\",\"scope\":\"all\","
         "\"url\":\"http://0.0.0.0:8000/api/embed\",\"access_key\":\"\",\"provider\":\"OLLAMA\"}')")
    run("ollama /api/embed 走通（返回合法 JSON）",
        "select json_valid(ai_embed('enhance_ollama_embed', 'hello'))", "1")

    for statement in ("drop table if exists test.ai_enhance_prompt",
                      "drop table if exists test.ai_enhance_batch"):
        call("cleanup", statement)
    conn.close()
    server.shutdown()


if __name__ == "__main__":
    main()
