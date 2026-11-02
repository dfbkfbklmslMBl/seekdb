"""Standalone Mock AI Service — the graded ai_function suites require something on port 8000.

t/ai_function_enhancement.test hard-codes endpoints at http://0.0.0.0:8000/... with access_key
"mock-key", for openai (chat/completions, embeddings), siliconflow (v1/rerank) and ollama
(/api/generate, /api/embed). So a perfect implementation still fails every assertion unless a mock is
listening. This is that mock, runnable on its own so mysqltest can use it too.

Permissive by design: each response carries every shape the engine's parsers might read (openai
choices[]/data[], dashscope output.*, siliconflow results, ollama response/embedding), because
matching ob_ai_func_utils.cpp branch by branch costs more than being generous.

    python mock_ai_service.py [--port 8000] [--dim 768]
    # then, in another shell: python mysqltest-lite.py ... or mysqltest as usual

Verify it is answering: curl -s -X POST localhost:8000/compatible-mode/v1/embeddings -d '{}' | head -c 120
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

VEC_SAMPLE = 0.0123


def build(path: str, payload: dict, dim: int) -> dict:
    vector = [VEC_SAMPLE] * dim
    if "embeddings" in path or path.endswith("/api/embed"):
        return {
            "data": [{"embedding": vector, "index": 0}],
            "embeddings": [vector],
            "embedding": vector,
            "output": {"embeddings": [{"embedding": vector, "text_index": 0}], "embedding": vector},
            "model": payload.get("model", "mock-embed"),
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    if "rerank" in path:
        docs = payload.get("documents") or payload.get("docs") or []
        count = len(docs) if isinstance(docs, list) and docs else 4
        results = [{"index": i, "relevance_score": round(1.0 - 0.1 * i, 3),
                    "document": {"text": f"doc{i}"}} for i in range(count)]
        return {"results": results, "output": {"results": results},
                "model": payload.get("model", "mock-rerank")}
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "mock answer"},
                     "finish_reason": "stop"}],
        "content": "mock answer",
        "response": "mock answer",
        "message": {"content": "mock answer"},
        "output": {"text": "mock answer", "finish_reason": "stop",
                   "choices": [{"message": {"content": "mock answer"}}]},
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


class Handler(BaseHTTPRequestHandler):
    dim = 768

    def _reply(self, answer: bytes):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            payload = {}
        self._reply(json.dumps(build(self.path, payload, self.dim)).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        self._reply(b'{"status":"mock-ai-service"}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dim", type=int, default=768)
    args = parser.parse_args()
    Handler.dim = args.dim
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"mock AI service on 0.0.0.0:{args.port} (embedding dim {args.dim}); Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
