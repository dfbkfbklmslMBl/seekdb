"""Measure cross-row AI_EMBED batching on the server side, where the .result file cannot see it.

The 6th sub-item of task 1 asks for "one request carrying several inputs" plus correct handling of an
out-of-order response, NULL rows, filtering and multiple batches. The official mysqltest only reads
back json_length() of each row's vector, so a per-row implementation scores identically there. This
probe therefore instruments the mock: every request is logged with how many inputs it carried, and the
response is returned in REVERSE order with each vector tagged by its own input index. A positional
(response-order) mapping then shows up as wrong tags in the SQL result.

Run on the box: /opt/conda/bin/python3 batch-probe-server.py
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pymysql

PORT = 8010
PATH = "/compatible-mode/v1/embeddings"
DIM = 768  # the graded mock answers with 768 floats; keep the same length so a dimension check can't fire
REQUESTS = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            payload = {"_unparsed": True}
        inputs = payload.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        inputs = list(inputs or [])
        with LOCK:
            REQUESTS.append({"path": self.path, "n": len(inputs), "inputs": inputs})
        data = []
        for idx, text in enumerate(inputs):
            vec = [float(idx), float(len(text))] + [0.0] * (DIM - 2)
            data.append({"index": idx, "embedding": vec})
        data.reverse()  # the spec's out-of-order-response case
        body = json.dumps({"object": "list", "model": payload.get("model", "?"), "data": data,
                           "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)}})
        raw_body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw_body)))
        self.end_headers()
        self.wfile.write(raw_body)

    def log_message(self, *args):  # silence
        pass


def serve():
    httpd = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    httpd = serve()
    conn = pymysql.connect(host="127.0.0.1", port=2881, user="root", database="test",
                           autocommit=True, charset="utf8mb4")
    cur = conn.cursor()
    url = f"http://127.0.0.1:{PORT}{PATH}"
    for stmt in (
        "call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('batch_probe_ep')",
        "call DBMS_AI_SERVICE.DROP_AI_MODEL('batch_probe')",
    ):
        try:
            cur.execute(stmt)
        except pymysql.MySQLError:
            pass
    cur.execute("call DBMS_AI_SERVICE.CREATE_AI_MODEL("
                "'batch_probe', '{\"type\":\"dense_embedding\",\"model_name\":\"probe-embed\"}')")
    cur.execute("call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT("
                f"'batch_probe_ep', '{{\"ai_model_name\":\"batch_probe\",\"scope\":\"all\","
                f"\"url\":\"{url}\",\"access_key\":\"k\",\"provider\":\"openai\"}}')")
    cur.execute("drop table if exists batch_probe_t")
    cur.execute("create table batch_probe_t(id int primary key, content text)")
    rows = [(1, "alpha"), (2, None), (3, "gamma"), (4, "delta"), (5, "epsilon")]
    cur.executemany("insert into batch_probe_t values(%s,%s)", rows)
    with LOCK:
        REQUESTS.clear()

    print("--- A) plain full scan, 4 non-null rows -------------------------------")
    show(cur, "select id, ai_embed('batch_probe', content) from batch_probe_t order by id")
    with LOCK:
        dump("   ", REQUESTS)

    print("--- B) LIMIT 2 (must not embed the filtered rows) ---------------------")
    with LOCK:
        REQUESTS.clear()
    show(cur, "select id, ai_embed('batch_probe', content) from batch_probe_t where id <= 2 order by id")
    with LOCK:
        dump("   ", REQUESTS)

    print("--- C) single literal input -------------------------------------------")
    with LOCK:
        REQUESTS.clear()
    show(cur, "select ai_embed('batch_probe', 'solo')")
    with LOCK:
        dump("   ", REQUESTS)

    print("--- D) filter + limit on the batch path -------------------------------")
    with LOCK:
        REQUESTS.clear()
    show(cur, "select id, ai_embed('batch_probe', content) from batch_probe_t "
              "where content is not null and id > 1 order by id desc limit 3")
    with LOCK:
        dump("   ", REQUESTS)

    print("--- verdict -----------------------------------------------------------")
    with LOCK:
        total_inputs = sum(r["n"] for r in REQUESTS)
        multi = [r for r in REQUESTS if r["n"] > 1]
    print(f"[BATCH] requests={len(REQUESTS)} inputs_total={total_inputs} "
          f"requests_with_multiple_inputs={len(multi)}")
    cur.execute("call DBMS_AI_SERVICE.DROP_AI_MODEL_ENDPOINT('batch_probe_ep')")
    cur.execute("call DBMS_AI_SERVICE.DROP_AI_MODEL('batch_probe')")
    cur.execute("drop table batch_probe_t")
    httpd.shutdown()
    conn.close()


def dump(prefix, requests):
    for r in requests:
        print(f"{prefix}req n={r['n']} inputs={r['inputs']}")
    if not requests:
        print(prefix + "(no request reached the mock)")


def show(cur, sql):
    """Print (id, tag, text_len, dim) per row; tag is the input position the mock assigned."""
    try:
        cur.execute(sql)
    except pymysql.MySQLError as exc:
        print("   ERR", exc.args)
        return
    for row in cur.fetchall():
        cells = []
        for value in row:
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", "replace")
            if isinstance(value, str) and value.startswith("["):
                try:
                    vec = json.loads(value)
                    cells.append(f"tag={vec[0]:g} len={vec[1]:g} dim={len(vec)}")
                except (json.JSONDecodeError, IndexError):
                    cells.append(repr(value)[:60])
            else:
                cells.append("NULL" if value is None else str(value))
        print("   row", " | ".join(cells))


if __name__ == "__main__":
    main()
