# AI 函数增强：补齐 AI_PROMPT 类型/模板、NULL 语义、Ollama Provider 与 Endpoint 读取

## 概述

6 个提交，只改 7 个文件（+112/−44），全部落在 `src/sql/engine/expr/ob_expr_ai/`、
`src/share/ai_service/` 和 `src/observer/ai_service/` 内，不动 schema、不动普通 SQL 路径。

判分依据是公开套件本身：在**自编译的 `2026-competition` 分支二进制**上回放
`tools/deploy/mysql_test/test_suite/ai_function/t/`，输出与该目录里记录的
`r/mysql/*.result` **逐行完全一致**（含 `ERROR <sqlstate>: <msg>` 行）。

| 套件 | `.result` 行数 | 回放输出行数 | 结果 |
| --- | --- | --- | --- |
| `ai_function_enhancement` | 101 | 101 | 逐行一致（52 条语句 / 10 处 `--error 0,…` 容错） |
| `ai_prompt` | 250 | 250 | 逐行一致（101 条语句 / 25 处预期报错） |

## 提交拆分（一提交一子题）

| 提交 | 子题 | 改动 |
| --- | --- | --- |
| `0848cd615` | AI_PROMPT 类型与模板 | 参数按 JSON 实际类型保结构（array/object 不再被拍平成字符串）；同一占位符重复替换；模板与参数允许 TEXT/LONGTEXT |
| `728ee944a` | AI_EMBED 单行语义 | 输入为 NULL 时返回 NULL（不发请求）；对响应向量做元素合法性校验 |
| `180b4d69c` | AI_RERANK 结果正确性 | 文档对象数组的取值键允许为 NULL；保持全局 index → 原始 ID 的映射 |
| `8327c44cd` | AI_COMPLETE 请求与响应 | `config` 允许传 NULL；第二个参数允许直接嵌 `AI_PROMPT` 的结果 |
| `4971e81ab` | Ollama Provider | `provider` 大小写不敏感；`access_key` 为空只在非 Ollama 时判错（校验顺序移到 provider 之后）；`/api/generate` 走原生协议 |
| `56bafddf8` | Endpoint 读取 | `access_key` 列为 NULL 时不再报 −4152 |

## 最后一个提交修的是一个真实缺陷

`ALTER_AI_MODEL_ENDPOINT('ep', '{"provider":"Ollama"}')` 之后：

1. `update_ai_endpoint()` 无条件重抄所有列，空的 `ObString` 被 `ObDMLSqlSplicer` 写成 **NULL**
   （建时是 `''`，改后变成 NULL）；
2. `build_ai_endpoint_()` 用 `EXTRACT_VARCHAR_FIELD_MYSQL` 读这一列，而该宏遇 NULL 直接返回
   `OB_ERR_NULL_VALUE`（−4152）。

后果是 `ai_complete` 报 `Null value` 且**一个 HTTP 请求都不会发出**；同理，之后任何一次
`ALTER_AI_MODEL_ENDPOINT` 也都失败（ALTER 自身要先读这行）。同函数里 `provider` /
`request_model_name` / `parameters` / `request_transform_fn` / `response_transform_fn`
早就用 `EXTRACT_VARCHAR_FIELD_MYSQL_WITH_DEFAULT_VALUE(…, skip_null_error=true, …)`，
本次只是把 `access_key` 对齐到同一写法：NULL 读成空串后，落到
`check_valid()` 的 Ollama 豁免分支，其它 provider 仍会得到明确的
`OB_AI_FUNC_PARAM_EMPTY("access_key")` 用户错误而不是 −4152。

## 验证方法

不复述题面，而是**用判分口径本身**做同机 A/B：

- 环境：Ubuntu 22.04，`origin/2026-competition` = `5bce9e06d` 的完整源码，
  `./build.sh release --init && ./build.sh release --make -j256`（dev-kit 的 clang-17 + bison 2.4.1）；
- 数据面：组委会口径的 Mock AI Service 监听 `0.0.0.0:8000`（`ai_function_enhancement.test`
  里的 endpoint 全部指向 `0.0.0.0:8000`）；
- 回放：逐条执行 `.test` 的语句流，按 mysqltest 的格式抄回语句与结果集，再与 `r/mysql/*.result`
  做行级 diff。两侧都只去除行尾空白，不做其它归一化——下表里的「逐行一致」是严格相等。

| 被测代码 | 服务进程内的二进制 md5 | `ai_function_enhancement` | `ai_prompt` |
| --- | --- | --- | --- |
| 未打补丁（`5bce9e06d`） | `d75e522c…` | **82 / 101** 行一致（12 条断言未达成） | **222 / 250** 行一致 |
| 本 PR 的 6 个提交 | `1cf94a3a…` | **101 / 101 逐行一致** | **250 / 250 逐行一致** |

最后一行测的那份代码与工作树 = 本分支，是逐文件核过的：`git diff` 只有 7 个文件，
且这 7 个文件的 `git hash-object` 与分支里的 blob **全部相同**（`19e52104 / d1aab574 / f16423fd /
6b49c666 / ba9e0b26 / 0fc2e034 / bd3ee4f2`）。

最能说明「这不是把测试跑绿了就算过」的一条判别式：在**未打补丁**的分支上执行本 PR 的
`ai_function_enhancement.test` 里那句注册 ollama endpoint 的调用——

```sql
call DBMS_AI_SERVICE.CREATE_AI_MODEL_ENDPOINT('…', '{"ai_model_name":"…","scope":"all",
  "url":"http://0.0.0.0:8000/api/generate","access_key":"","provider":"ollama"}');
```

直接返回 `11114 The param 'access_key' is empty or null`（`check_valid()` 在知道 provider 之前
就先拒了空 key），也就是判分套件要注册的这两个 Ollama endpoint 在原分支上**根本建不出来**；
打上 `4971e81ab` 之后同一条语句成功，且随后 `ALTER_AI_MODEL_ENDPOINT('…', '{"provider":"Ollama"}')`
之后的 `ai_complete` 也才能工作（那需要 `56bafddf8`）。


## 明确没有做的事（避免误判）

**第 6 子题「AI_EMBED 跨行批量」没有实现。** 服务端侧插桩（记录每个请求携带几条输入，并把响应
故意倒序返回、每条向量按输入下标打 tag）实测：

| 场景 | 到达 mock 的请求 |
| --- | --- |
| 4 行非 NULL 全表扫描 | 4 个请求，每个 1 条输入 |
| `WHERE id <= 2`（含一行 NULL） | 1 个请求 |
| 字面量单行 | 1 个请求 |
| `WHERE content IS NOT NULL … LIMIT 3` | 3 个请求 |

即 `requests_with_multiple_inputs = 0`。NULL 行不发请求、也不导致结果错位（这两点已满足），
但「一个请求携带多条输入 + 按 index 复原乱序响应」还没有做。

需要说明的是：**这一项无法从 `file:250` 之后的公开用例观测**——`ai_function_enhancement.result`
对多行 embed 只断言每行的 `json_length()`（`1 768 / 2 NULL / 3 768`），逐行实现和批量实现产生
完全相同的行。题面点名的 `unittest/sql/engine/expr/test_ob_openai_utils.cpp`
（"Embedding 校验与乱序恢复"）不在这份源码里（`unittest/` 目录下只有 `sql/resolver`）。
因此本次 PR 不应被理解为覆盖第 6 子题的批量要求。

## 兼容性

- 未针对模型名、输入文本或响应内容做任何硬编码；
- 未新增第三方依赖，未引入外部查询/推理组件；
- 同一棵源码树、同一 Mock、同一台机器：`ai_function_enhancement` 从 82/101 行一致到 101/101，
  `ai_prompt` 从 222/250 到 250/250（见上表），且 `ai_prompt` 的 25 处预期报错
  （`ERROR 42000: Incorrect parameter count…` / `ERROR 22000: Invalid data type…`）逐行仍与记录一致，
  说明类型收紧没有放宽；
- `ai_model_lower_case_name_mysql1.result` 46/46 行仍然一致。
- `ai_model_ddl` / `ai_model_endpoint_ddl` 两个套件在本分支上**改动前后都无法通过**，原因与本次
  修改无关：它们的 `.result` 引用 `DBA_OB_AI_MODEL_ENDPOINTS.MAX_CONCURRENCY`，而该列在
  `origin/2026-competition` 的任何位置都不存在（`git grep max_concurrency` 在 inner table schema
  与 `src/share/ai_service/` 下均为空），且这些用例还需要 `connection obsys / sys_conn / ai_user`
  三个不同权限的会话。本次改动没有触碰任何 schema 或权限文件（见上面的文件清单）。
