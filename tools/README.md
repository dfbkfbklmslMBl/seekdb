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

## tools/ai_enhancement_baseline.txt —— 官方 ai_function_enhancement.test 在**未打补丁 1.4.0** 上的实测
跑法（配套 mock 必须先起）：

-  是官方 .test 的**副本**，唯一改动是把 8 处
  `http://0.0.0.0:8000` 换成 `http://127.0.0.1:8000`（这台 Windows 的引擎连不上 0.0.0.0）。
  **仓库里的官方文件没有被改动过**，判分时请用原文件 + 能听 0.0.0.0 的环境。
- 实测：**非预期错误 12 条**，错误码集合 `11112 11114 1210 1235 3141 5083 `。
  其中 `11112` 那几条是**级联**（ollama endpoint 因 11114 空 access_key 建不出来，后面的
  DROP/使用全跟着红），不是独立缺口。
- 修掉的一个假红来源：mysqltest 的 `--error 0, 11118` 里 **0 表示允许成功**，
  runner 原先把成功算成失败，会让 DROP-if-exists 类语句全变红。已修。

## tools/ai_enhancement_baseline.txt —— 官方 ai_function_enhancement.test 在未打补丁 1.4.0 上的实测
跑法（mock 必须先起来）：

```bash
python mock_ai_service.py --port 8000 &
python mysqltest-lite.py --host <h> --port <p> --test ai_function_enhancement.127.test \
    --out ai_enhancement_baseline.txt
```

- `ai_function_enhancement.127.test` 是官方 `.test` 的**副本**，唯一改动是把 8 处
  `http://0.0.0.0:8000` 换成 `http://127.0.0.1:8000`（这台 Windows 的引擎连不上 `0.0.0.0`）。
  **仓库里的官方文件没有被改动过**；判分环境能听 `0.0.0.0` 就用原文件。
- 实测结果：**10 条 `--error` 断言里 12 处非预期错误**，错误码集合
  `11112 11114 1210 1235 3141 5083`。其中 `11112` 是**级联**：ollama endpoint 因 `11114`
  （空 access_key 被拒）建不出来，后面的 DROP 与使用全跟着红 —— 不是独立缺口。
- 与 5 份草稿补丁的对应：`1235` json 参数 + `5083` LOB 模板 ⇒ 补丁 01；
  `1210` embed 遇 NULL ⇒ 补丁 02；`1210` rerank 字符串数组 ⇒ 补丁 03；
  `3141` config 传 NULL ⇒ 补丁 04；`11114` 空 access_key + ollama provider ⇒ 补丁 05。
  正好覆盖全部实测缺口，**没有多余补丁，也没有没补丁的缺口**。
- 修掉的假红来源：mysqltest 里 `--error 0, 11118` 的 **0 表示允许成功**，
  runner 原先把成功当失败，会让 DROP-if-exists 类语句全红（这一版把 20 处降到 12 处）。
