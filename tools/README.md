# 探测脚本（都在 Windows 上写、本机跑过）

- `seek_serve.py`：用 pylibseekdb 起一个 MySQL 协议的 seekdb 实例并把 host/port 写到
  `D:/scratch/seekdb-endpoint.json`。**它只在父进程存活期间有效** —— 之前那次「2881 掉线」就是父进程退出带走了子进程。
- `fts-phase-probe.py`：判断 **FTS build 是不是分词 bound**。用官方 `fts_large_bench_gen.py` 的词表造数据，
  跑官方那三条 `ALTER TABLE ... ADD FULLTEXT INDEX`，再逐行 `SELECT tokenize(...) AS tok`，
  输出 `tokenize 全表 / build` 比值。**Windows 上实测比值 0.18 ⇒ build 不是分词 bound**（台账 §24）。
  云上第一件事就是重跑它确认这个结论（端口用 `SEEKDB_HOST` / `SEEKDB_PORT` 传）。
- `crowding_probe.py`：赛题四的负结果实验（每文档配额 / bigram 短语权重都让 Recall@20 变差）。
  只在 Windows 上跑过，依赖 `D:/scratch/fb` 的缓存，云上不必复现。

## tools/ai-fn-oracle-run.py —— 官方 ai_function_enhancement 判分的**本地复现器 + Mock AI 服务**
`r/mysql/ai_function_enhancement.result` 把每个 endpoint 都指向 `http://0.0.0.0:8000`、`access_key` 是
`mock-key` ⇒ 判题**不打真实模型**，它需要一个 mock。这份脚本自带该 mock（同进程 8000 端口，
按 openai / dashscope / siliconflow / ollama 四种可能的响应形状同时作答），并逐条执行 oracle 断言，
打印 期望/实际。**未打补丁的机器上跑 = 缺口清单；打上补丁后跑 = 判分对照。**

⚠️ 两个必须知道的坑（都在 Windows 上实测到）：
1. **引擎连不上 `0.0.0.0`**：按 oracle 原样写 `url:"http://0.0.0.0:8000/..."` 时，`ai_embed` 全部
   返回 `4216 curl error code 7`；改成 `http://127.0.0.1:8000` 后立刻 **PASS（json_length = 768）**。
   Linux 上 `0.0.0.0` 通常等于 localhost，所以这条差异**很可能只是 Windows 侧的**，但云上第一次跑
   建议两种都试，别把连不上误判成实现缺失。
2. 对象数组 + doc_key 那条断言里我的 `run()` 默认只取第一列，所以它会把「实际其实对」显示成 DIFF
   （`first_id` 拿到 'a' 就够了；第二列没取）。云上要看整行，别据此判定 rerank 有缺陷。
