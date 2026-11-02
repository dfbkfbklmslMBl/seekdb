"""Where does the FTS build time actually go? Decides whether the drafted patches aim at the right phase.

fts_large_bench_score.py gives continuous credit: per-metric improvement, mean inside each category,
then the three categories (build / tokenize / query) averaged with EQUAL weight, clamped to [0, 0.5]
and mapped to 0..100. So a patch set that only helps one category caps out around 33 of 100 — and a
patch that slows another category costs real points.

The question this answers locally (the graded CI runs 20k rows; proportions are what matter):
  is `ALTER TABLE ... ADD FULLTEXT INDEX ... WITH PARSER ik` dominated by *tokenization*
  (which the 4 drafted patches attack: per-token arena, parser member, unused dict, dead objects)
  or by index storage / DAS merge (which they do not touch)?

Method: load rows built from the benchmark's own vocabulary lists, time each of the three index
builds exactly as the bench does, then time tokenize() over the same corpus through the same SQL
function, and compare the sums.

Scratch only; talks to the local seekdb on 127.0.0.1:2881 and drops its own database at the end.
"""

import os
import random
import sys
import time

sys.path.insert(0, "D:/比赛/20-seekdb-bugfix/seekdb/tools/benchmark")
import pymysql  # noqa: E402
from fts_large_bench_gen import CONTENT_CN, CONTENT_EN, TITLES_CN, TITLES_EN  # noqa: E402

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
BATCH = 500
DB = "fts_probe"
HOST = os.environ.get("SEEKDB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SEEKDB_PORT", "2881"))


def connect():
    return pymysql.connect(host=HOST, port=PORT, user="root", charset="utf8mb4",
                           autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SET ob_query_timeout = 100000000000")
    cur.execute("SET ob_trx_timeout = 100000000000")
    cur.execute(f"DROP DATABASE IF EXISTS {DB}")
    cur.execute(f"CREATE DATABASE {DB}")
    cur.execute(
        f"CREATE TABLE {DB}.docs ("
        " id BIGINT AUTO_INCREMENT PRIMARY KEY, category INT NOT NULL,"
        " title VARCHAR(256) NOT NULL, content TEXT NOT NULL,"
        " title_en VARCHAR(256) NOT NULL, content_en TEXT NOT NULL)"
        " DEFAULT CHARSET = utf8mb4"
    )

    random.seed(7)
    rows = [
        (
            random.randint(0, 9),
            random.choice(TITLES_CN) + str(i),
            " ".join(random.sample(CONTENT_CN, 3)),
            random.choice(TITLES_EN) + str(i),
            " ".join(random.sample(CONTENT_EN, 3)),
        )
        for i in range(ROWS)
    ]

    def timed(label, sql, args=None):
        start = time.perf_counter()
        cur.execute(sql, args)
        cost = time.perf_counter() - start
        print(f"  {label:26s} {cost:8.3f}s")
        return cost

    t0 = time.perf_counter()
    for offset in range(0, len(rows), BATCH):
        chunk = rows[offset:offset + BATCH]
        cur.executemany(
            f"INSERT INTO {DB}.docs(category,title,content,title_en,content_en) VALUES (%s,%s,%s,%s,%s)",
            chunk,
        )
    load = time.perf_counter() - t0
    print(f"  raw_load {ROWS} rows          {load:8.3f}s   ({ROWS / load:,.0f} rows/s)")

    builds = {}
    builds["build_ik_all_sec"] = timed(
        "build ik(title,content)",
        f"ALTER TABLE {DB}.docs ADD FULLTEXT INDEX fti_all_ik(title, content) WITH PARSER ik",
    )
    builds["build_ik_content_sec"] = timed(
        "build ik(content)",
        f"ALTER TABLE {DB}.docs ADD FULLTEXT INDEX fti_content_ik(content) WITH PARSER ik",
    )
    builds["build_beng_en_sec"] = timed(
        "build beng(title_en,content_en)",
        f"ALTER TABLE {DB}.docs ADD FULLTEXT INDEX fti_en_beng(title_en, content_en) WITH PARSER beng",
    )

    # Tokenize the same corpus through the same SQL entry point, one call per row — that is the shape
    # an index build is in. (A single call with the whole corpus concatenated makes the *result column
    # name* megabytes long, which is a client-visible artifact of the expression, not a workload.)
    cur.execute(f"SELECT content, content_en FROM {DB}.docs")
    corpus = cur.fetchall()
    token_cost = {}
    for column, parser in (("content", "ik"), ("content_en", "beng")):
        start = time.perf_counter()
        for row in corpus:
            cur.execute("SELECT tokenize(%s, %s) AS tok", (row[column], parser))
            cur.fetchone()
        cost = time.perf_counter() - start
        token_cost[parser] = cost
        chars = sum(len(row[column]) for row in corpus)
        print(f"  tokenize {parser} {len(corpus)} rows {cost:8.3f}s  ({chars:,} chars)")

    for name, sql in (
        ("query_cn", f"SELECT COUNT(*) FROM {DB}.docs WHERE MATCH(title, content) AGAINST('全文索引 分词器')"),
        ("query_beng", f"SELECT COUNT(*) FROM {DB}.docs WHERE MATCH(title_en, content_en) AGAINST('database performance audit')"),
        ("query_mixed", f"SELECT COUNT(*) FROM {DB}.docs WHERE MATCH(title, content) AGAINST('OceanBase 稳定 database')"),
        ("query_limit", f"SELECT COUNT(*) FROM (SELECT id FROM {DB}.docs WHERE MATCH(content) AGAINST('倒排索引 tokenizer') LIMIT 20) t"),
    ):
        cur.execute(sql)
        hits = cur.fetchone()
        start = time.perf_counter()
        for _ in range(20):
            cur.execute(sql)
            cur.fetchone()
        print(f"  {name+' avg ms':26s} {(time.perf_counter() - start) / 20 * 1000:8.2f}ms  {hits}")

    ik_build = builds["build_ik_all_sec"] + builds["build_ik_content_sec"]
    # (title, content) 的 ik 分词量 = 两列之和；content-only 索引只算 content
    print("\n判读：分词耗时 / 建索引耗时")
    print(f"  ik   tokenize(全表) = {token_cost['ik']:7.3f}s   vs ik 两次 build = {ik_build:7.3f}s"
          f"   -> 比值 {token_cost['ik'] / ik_build:.2f}")
    print(f"  beng tokenize(全表) = {token_cost['beng']:7.3f}s   vs beng build      = "
          f"{builds['build_beng_en_sec']:7.3f}s   -> 比值 {token_cost['beng'] / builds['build_beng_en_sec']:.2f}")
    print("  比值接近或 >1 ⇒ build 由分词 bound，草稿补丁（每 token arena / parser 成员 / 未用词典 /"
          " 死对象）打在正确位置；比值远小于 1 ⇒ build 由索引存储/DAS 主导，得改那条路径。")
    print("  注意：tokenize 走的是 SQL 往返 + 表达式路径，含协议开销，作为上界看。")
    cur.execute(f"DROP DATABASE IF EXISTS {DB}")
    conn.close()


if __name__ == "__main__":
    main()
