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

## tools/mysqltest-lite.py —— 不用 mysqltest / 不用 mysql 客户端跑官方 `.test`
只依赖 pymysql。支持 `--source`、`let $V=…`（含嵌套变量）、`eval`、`--error N`、`--echo`、
`--disable/enable_result_log`、`--disable/enable_query_log`。**逐条断言 `--error` 期望**，
并把所有结果集打成可与人肉 `.result` 对照的转录（`--out`）。

设计底线：mysqltest 的**客户端指令**（`connect/connection/disconnect/exec`、`let $X=query_get_value/ssh`）
**一律记为 UNSUPPORTED，不会当 SQL 发给服务器**（发过去只会得到一堆 1064 噪声），
退出码把「有未支持指令」也算失败 ⇒ 它的 green 永远不等于「套件通过」，只等于「已验证的那些断言没红」。

`file_sql_basic.actual-unpatched.txt` 是**未打补丁**的 1.4.0 上跑出来的现状转录（3 条断言、21 处不符、
10 条客户端指令不支持），判分口径见台账 §26。

## tools/mock_ai_service.py —— 独立版 Mock AI 服务（判分环境的硬前置）
`t/ai_function_enhancement.test` 把 endpoint 全写死成 `http://0.0.0.0:8000/...`（openai 的
chat/completions 与 embeddings、siliconflow 的 v1/rerank、ollama 的 /api/generate 与 /api/embed），
`access_key` 是 `mock-key` 或空串。**⇒ 8000 上没有服务时，实现写得再对也全红。**
云上跑分之前先：`python mock_ai_service.py --port 8000 --dim 768 &`
自检：`curl -s -X POST localhost:8000/compatible-mode/v1/embeddings -d '{}' | head -c 120`

## tools/ai_prompt_baseline.txt —— ai_prompt.test 在**未打补丁 1.4.0** 上的基线
25 条 `--error` 断言、**6 条已经红**。补丁打上之后必须**至少不比这里差**（回归下限）。
`connection default;` 是本脚本有意不执行的 mysqltest 客户端指令。
