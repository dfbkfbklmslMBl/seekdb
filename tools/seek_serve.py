"""Hold a pylibseekdb instance alive and publish its MySQL-protocol endpoint.

pylibseekdb.open() spawns seekdb.exe as a child and only takes db_dir, so the port is chosen by the
engine; connection_options() is how a driver learns it. The child dies with this Python process, which
is why a probe that opens an instance inline leaves nothing reachable afterwards — this runner exists
so the server outlives the script that measures against it.

Writes D:/scratch/seekdb-endpoint.json = {"host":..., "port":..., "db_dir":...} and blocks.
"""

import json
import sys
import time
from pathlib import Path

import pylibseekdb

DB_DIR = sys.argv[1] if len(sys.argv) > 1 else "D:/scratch/seekdb-fts"
OUT = Path("D:/scratch/seekdb-endpoint.json")


def main() -> int:
    OUT.unlink(missing_ok=True)
    Path(DB_DIR).mkdir(parents=True, exist_ok=True)
    instance = pylibseekdb.open(DB_DIR)
    options = instance.connection_options()
    print("connection_options:", options, flush=True)
    host = options.get("host") or options.get("unix_socket") or "127.0.0.1"
    OUT.write_text(json.dumps({"host": host, "port": options.get("port"), "db_dir": DB_DIR},
                              ensure_ascii=False), encoding="utf-8")
    while not instance.closed:
        time.sleep(5)
    print("instance closed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
