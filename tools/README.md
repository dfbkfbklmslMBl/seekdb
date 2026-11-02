# 探测脚本（都在 Windows 上写、本机跑过）

- `seek_serve.py`：用 pylibseekdb 起一个 MySQL 协议的 seekdb 实例并把 host/port 写到
  `D:/scratch/seekdb-endpoint.json`。**它只在父进程存活期间有效** —— 之前那次「2881 掉线」就是父进程退出带走了子进程。
- `fts-phase-probe.py`：判断 **FTS build 是不是分词 bound**。用官方 `fts_large_bench_gen.py` 的词表造数据，
  跑官方那三条 `ALTER TABLE ... ADD FULLTEXT INDEX`，再逐行 `SELECT tokenize(...) AS tok`，
  输出 `tokenize 全表 / build` 比值。**Windows 上实测比值 0.18 ⇒ build 不是分词 bound**（台账 §24）。
  云上第一件事就是重跑它确认这个结论（端口用 `SEEKDB_HOST` / `SEEKDB_PORT` 传）。
- `crowding_probe.py`：赛题四的负结果实验（每文档配额 / bigram 短语权重都让 Recall@20 变差）。
  只在 Windows 上跑过，依赖 `D:/scratch/fb` 的缓存，云上不必复现。
