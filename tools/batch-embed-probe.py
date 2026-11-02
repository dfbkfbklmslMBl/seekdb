"""Does the engine batch multi-row AI_EMBED into one request, and does it survive an out-of-order response?

The task spec's 6th sub-item (10 pts, no drafted patch yet) demands: one request carrying several
inputs, correct handling of unordered responses, NULL propagation, filtering/LIMIT, multiple batches,
and failure. None of that is observable from the .test file alone — it is observable from the *server
side*. So this probe puts an instrumented mock on :8000 that

  1. logs every request: path, how many inputs it carried, and the order it received them in;
  2. tags each returned vector so the value identifies its own input text;
  3. deliberately returns the array in REVERSE order while setting each item's `index` correctly.

Then it runs a plain multi-row SELECT of ai_embed over a table containing a NULL, and reads back
row -> vector tag. From the log we learn whether the engine batches at all; from the tags we learn
whether the reversed response scrambles the mapping (the exact failure the spec warns about).

Scratch only: local pylibseekdb instance, writes under D:/scratch.
"""

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymysql

EP = json.loads(Path("D:/scratch/seekdb-endpoint.json").read_text(encoding="utf-8"))
LOG = Path("D:/scratch/mock-requests.jsonl")
DIM = 8


def tag_of(text: str) -> int:
    """Stable small integer per input text, so a row can prove which vector it got back."""
    return sum(ord(c) for c in text) % 997


def vector_for(text: str) -> list:
    vec = [0.0] * DIM
    vec[0] = float(tag_of(text))
    return vec


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            payload = {}
        inputs = payload.get("input")
        texts = inputs if isinstance(inputs, list) else ([inputs] if isinstance(inputs, str) else [])
        record = {
            "ts": time.time(),
            "path": self.path,
            "n_inputs": len(texts),
            "input_order": [tag_of(t) for t in texts],
            "reversed_response": len(texts) > 1,
        }
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

        if "embeddings" in self.path:
            items = [{"embedding": vector_for(t), "index": i} for i, t in enumerate(texts)]
            items = list(reversed(items))  # 乱序返回，但 index 字段仍是原始下标
            answer = {"data": items, "model": payload.get("model", "mock"),
                      "usage": {"prompt_tokens": len(texts), "total_tokens": 0}}
        elif "rerank" in self.path:
            docs = payload.get("documents") or []
            answer = {"results": [{"index": i, "relevance_score": round(1.0 - 0.05 * i, 3)}
                                  for i in range(len(docs))][::-1]}
        else:
            answer = {"choices": [{"index": 0, "message": {"role": "assistant",
                                                            "content": "mock"}}]}
        body = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def call(cur, sql):
    try:
        cur.execute(sql)
        return cur.fetchall()
    except pymysql.MySQLError as e:
        return f"ERROR {e.args[0] if e.args else '?'}: {str(e)[:120]}"


def main():
    LOG.unlink(missing_ok=True)
    server = HTTPServer(("127.0.0.1", 8001), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    conn = pymysql.connect(host=EP["host"], port=EP["port"], user="root", db="test",
                           autocommit=True, charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SET ob_query_timeout = 100000000000")

    for statement in ("call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('batch_ep')",
                      "call DBMS_AI_SERVICE.DROP_AI_MODEL('batch_model')"):
        call(cur, statement)
    print("create model:", call(cur, "call DBMS_AI_SERVICE.CREATE_AI_MODEL('batch_model', "
                                     "'{\"type\":\"dense_embedding\",\"model_name\":\"mock-embed\"}')"))
    print("create endpoint:", call(cur, "call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('batch_ep', "
          "'{\"ai_model_name\":\"batch_model\",\"scope\":\"all\","
          "\"url\":\"http://127.0.0.1:8001/compatible-mode/v1/embeddings\","
          "\"access_key\":\"mock-key\",\"provider\":\"openai\"}')"))

    call(cur, "drop table if exists be_probe")
    call(cur, "create table be_probe(id int primary key, content varchar(200))")
    rows = [(1, "alpha"), (2, "beta"), (3, None), (4, "gamma"), (5, "delta"),
            (6, "epsilon"), (7, "zeta"), (8, "alpha")]
    for row in rows:
        cur.execute("insert into be_probe values(%s,%s)", row)

    print("\n=== 每个 tag 的期望值（行 -> 它自己的向量首元素）===")
    expected = {rid: (tag_of(text) if text else None) for rid, text in rows}
    print(json.dumps(expected, ensure_ascii=False))

    for label, sql in (
        ("整表 8 行", "select id, json_extract(ai_embed('batch_model', content), '$[0]') "
                      "from be_probe order by id"),
        ("带 LIMIT 的 3 行", "select id, json_extract(ai_embed('batch_model', content), '$[0]') "
                             "from be_probe where content is not null order by id limit 3"),
        ("单行", "select ai_embed('batch_model', 'alpha')"),
    ):
        before = len(LOG.read_text(encoding="utf-8").splitlines()) if LOG.exists() else 0
        result = call(cur, sql)
        after_lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
        new_reqs = [json.loads(x) for x in after_lines[before:]]
        print(f"\n--- {label}")
        print("   返回:", str(result)[:400])
        print(f"   本次发出请求数: {len(new_reqs)}")
        for req in new_reqs:
            print(f"     path={req['path']} n_inputs={req['n_inputs']} "
                  f"收到的 tag 顺序={req['input_order']} (响应被反序返回)")

    # 顺序/映射正确性：拿整表结果与期望逐行比对
    whole = call(cur, "select id, json_extract(ai_embed('batch_model', content), '$[0]') "
                      "from be_probe order by id")
    print("\n=== 映射正确性判定（mock 故意反序返回）===")
    if isinstance(whole, str):
        print("   查询失败:", whole)
    else:
        bad = []
        for rid, first in whole:
            got = None if first is None else float(first)
            want = expected[rid]
            if (want is None and got is not None) or (want is not None and got != float(want)):
                bad.append((rid, want, got))
        print("   错位行数:", len(bad))
        for item in bad[:8]:
            print("     id=%s 期望 tag=%s 实际=%s" % item)

    call(cur, "drop table if exists be_probe")
    for statement in ("call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('batch_ep')",
                      "call DBMS_AI_SERVICE.DROP_AI_MODEL('batch_model')"):
        call(cur, statement)
    conn.close()
    server.shutdown()


if __name__ == "__main__":
    main()
