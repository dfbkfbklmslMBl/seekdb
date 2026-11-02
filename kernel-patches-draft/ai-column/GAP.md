# 赛题三 AI COLUMN —— GAP 分析

分析对象：ref `origin/2026-competition`，commit `5bce9e06d1281347ad61344e4b01fab9158c58a2`（"upload 2026-competition testcases"，wangzelin.wzl，2026-10-26）。

## 0. 方法与可信度声明

- 所有 `src/` 结论均来自 `git grep -n origin/2026-competition -- <path>` 与 `git cat-file -p origin/2026-competition:<path>`，行号是该 ref 的行号。
- 本地工作树 = `origin/2026-competition` + 3 个本地提交（`git merge-base --is-ancestor origin/2026-competition HEAD` 为真），这 3 个提交只改 `src/sql/rewrite/ob_transform_pre_process.cpp` 与两个 NOT IN 测试文件（`git diff --stat HEAD origin/2026-competition` 输出 3 files/185 deletions），因此与 AI 列无关的文件行号可通用。
- 本 clone **只配置了 lfs filter，没有 codeformat filter**（`git config --get-regexp "^filter"` 只有 `filter.lfs.*`），并且抽样校验工作树与 `git cat-file` 内容 md5 相同（`src/share/schema/ob_ai_model_mgr.h`：两者均为 `d007fc09…`）。所以工作树内容是可信的。
- **本机没有任何 Linux 工具链，无法编译、无法运行 mysqltest、无法运行 python mock**（mock 依赖 `/tmp/seekdb_ai_column_mock_<port>`，见 `tools/deploy/mysql_test/test_suite/ai_column/include/ai_column_mock.py:17`）。本文所有"可行/不可行"都是读码推断，不是验证结论。
- 全程未修改 `D:\比赛\20-seekdb-bugfix\seekdb` 任何文件；ref 测试文件只读副本导出到 `D:\scratch\ai-column\ref-tests\`。

## 1. 最重要的更正：验收用例并没有缺失，只是换了路径

任务说明里"已确认缺失"的清单需要修正——**5 个 mysqltest + mock 全部存在于该 ref**，只是不在题面写的 `test_suite/ai_function/t/` 路径下，而是独立成了 `test_suite/ai_column/`：

| 题面声明路径 | 实际 ref 路径 | 状态 |
| --- | --- | --- |
| `tools/deploy/mysql_test/test_suite/ai_function/t/ai_column_ddl.test` | `tools/deploy/mysql_test/test_suite/ai_column/t/ai_column_ddl.test` | **存在** |
| …`ai_function/t/ai_column_t1_dml.test` | `…/ai_column/t/ai_column_t1_dml.test` | **存在** |
| …`ai_function/t/ai_column_worker.test` | `…/ai_column/t/ai_column_worker.test` | **存在** |
| …`ai_function/t/ai_column_retry_strict.test` | `…/ai_column/t/ai_column_retry_strict.test` | **存在** |
| …`ai_function/t/ai_column_vector_index.test` | `…/ai_column/t/ai_column_vector_index.test` | **存在** |
| …`ai_function/include/ai_column_mock.py` | `…/ai_column/include/ai_column_mock.py` | **存在** |
| 对应 `.result` 5 个 | `…/ai_column/r/mysql/*.result` | **存在** |
| `unittest/sql/parser/test_ai_column_parser.cpp` | — | 缺失（真缺失） |
| `unittest/sql/resolver/test_ai_column_resolver.cpp` | — | 缺失（真缺失） |
| `unittest/share/schema/test_ai_column_schema.cpp` | — | 缺失（真缺失） |
| `tools/ai_gateway/` | — | 缺失（真缺失，评测网关是外部工具） |

补充证据：

- `git ls-tree -r --name-only origin/2026-competition | grep -i ai_column` 只返回上面 11 个 `tools/deploy/...` 文件，`src/` 下 **零** 命中；`git grep -i -E "ai_column_strict_read|AiColumn|AI_COLUMN" origin/2026-competition -- src/` 无输出。即：**只有测试，没有任何实现**。
- 该目录唯一提交就是 ref tip：`git log --oneline origin/2026-competition -- tools/deploy/mysql_test/test_suite/ai_column/` → `5bce9e06d`。
- 三个 unittest 不只是文件缺失，**整个 gtest 骨架都不在这个开源仓里**：`git ls-tree -r --name-only origin/2026-competition -- unittest/` 只有 `unittest/.gitignore` 和 `unittest/sql/resolver/.gitignore` 两个文件；`mittest/` 不存在。

**这一条改变整个计划**：mysqltest 侧有精确到错误码、系统表名、列名、期望字符串的"可执行 spec"，不需要猜；unittest 侧则完全没有脚手架（见 PLAN.md 第 6 节）。

## 2. 从已发布用例反推的硬契约

这些是"读码得到"的事实（用例与 mock 是 ref 内容），不是推断。

### 2.1 SQL 语法

- 建表内联：`<col> <type> AI COLUMN ( <expr> ) WITH (sync_mode='async', max_retries=N)`
  （`ai_column_ddl.test:31-36`，`ai_column_t1_dml.test:23-25`）
- 加列：`ALTER TABLE t ADD COLUMN tags json AI COLUMN (AI_COMPLETE(...)) WITH (sync_mode='async', max_retries=1)`（`ai_column_ddl.test:82-84`）
- 手动重试：`ALTER TABLE t RETRY AI COLUMN <col> [WHERE <predicate>]`
  支持无 WHERE（全表）、`where id in (1,3)`、`where status = 'FAILED' and category = 'news'`、反引号 `` `status` ``（业务列）
  （`ai_column_retry_strict.test:52-53,114-116,120,140,150-151,170`）
- RETRY 限制（`ai_column_retry_strict.test:175-179`，result 的报错文本）：
  - 目标不是 AI 列 → `ERROR 0A000: retrying a column that is not an AI column not supported`（`--error 1235`）
  - 与其他 ALTER action 混合 → `ERROR 0A000: combining AI COLUMN RETRY with another ALTER action not supported`（`--error 1235`）
- 会话变量：`SET SESSION ai_column_strict_read = ON|OFF`；`select @@ai_column_strict_read` 默认 **0**（`ai_column_retry_strict.test:48,54,61,87,107,111,132,135`）
- 严格读取错误（`ai_column_retry_strict.result:53`）：`ERROR HY000: AI column has unfinished tasks for table_id=<table_id>, column_id=<column_id>`，用例 `--error 11120` 并用 `--replace_regex /table_id=[0-9]+, column_id=[0-9]+/...` 归一化 → **错误码必须是 -11120，消息格式必须含 `table_id=%lu, column_id=%lu`**。

### 2.2 新增系统表（名字与列名都是硬约束）

`oceanbase.__all_ai_column`，用例引用的列：`table_id, column_id, column_name, state_column_id, request_column_id, result_spec_column_id, max_retries, normalized_expr`
（`ai_column_ddl.test:39-78,85-89`）

- 每个 AI 列在用户表上额外挂 **3 个隐藏列**，且 `is_hidden = 1`，`column_id` 分别等于 `state_column_id / request_column_id / result_spec_column_id`（`ai_column_ddl.test:52-61`）；结果列本身 `is_hidden = 0` 用户可见（`:63-70`）。
- `normalized_expr` 里必须**持久化类型提示词**：JSON 列要求 `normalized_expr like '%Return exactly one valid JSON value%'`（`:72-78`）。
- 表上元数据行数 = AI 列个数（2 列 → 2 行）。

`oceanbase.__all_ai_column_task`，用例引用的列：`table_id, rowkey_predicate, request_id, state, retry_count, eligible_ts`
（`ai_column_t1_dml.test:32-37,64-68,75-79,101-106,151-156`；`ai_column_worker.test:42-47,95-101,150-155`；`ai_column_vector_index.test:58-63,109-114`）

- `rowkey_predicate` 是**字符串**且带反引号：期望值 `` `id` = 2 ``、`` `id` = 10 ``、`` `id` = 11 ``（`ai_column_t1_dml.test:106,156`）。这是写回时定位行的依据，也是主键变更后任务用新主键的依据。
- `eligible_ts` 可与 `now(6)` 比较（datetime(6) 语义），插入后立即 `<= now(6)`（`ai_column_t1_dml.test:32-37`）→ 首次执行零退避。
- 同一行同次变更的 2 个 AI 列 → `count(*) = 2` 且 `count(distinct request_id) = 1`（`ai_column_worker.test:42-47`）→ **一行一次变更一个 request_id，每列一条任务**。
- 重试可观测态：`task.state = 3 and task.retry_count = 1` 必须在轮询窗口（0.1s ×100）里至少可见一次（`ai_column_worker.test:95-101`）→ 存在一个编号为 3 的"已失败待重试"状态（该值的语义是推断，编号是硬约束）。
- 终态（成功发布或最终失败）后任务行**被删除**：`grouped_tasks_removed`、`retry_task_removed`、`terminal_failure_removes_task`、`failed_task_rows_cleaned` 均要求 `count(*) = 0`（`ai_column_worker.test:75-79,130-134,168`；`ai_column_retry_strict.test:106`）。

### 2.3 Mock HTTP 契约（`ai_column_mock.py`，端口 19120–19124）

- OpenAI 兼容：`POST /v1/chat/completions`，body `{"model","messages":[{"role":"system"...},{"role":"user"...}]}`；`POST /v1/embeddings`，body `{"input": ...}`（`ai_column_mock.py:104-117,134-155,167-169`）。
- **类型提示词必须放在 system 消息**：断言 `"valid JSON value" in system_text`、`"base-10 integer" in system_text`（`:198-210`）；**用户文本必须放在 user 消息**：断言 `"retry-once" in event["user"]`、`"slow-old" in event["user"]`、`"rollback-value" not in user`（`:196,211-221`）。
- 鉴权：`Authorization` 必须以 `Bearer ` 开头，且 typed-prompts 断言要求**所有**请求都带（`:207-209`）。
- 故障注入：路径含 `always-fail` 或 `retry-once` 的首次请求 → HTTP **400** + `{"error": {...}}`（`:128-133`）。
- 时序：`slow-old` 请求 sleep **1.2s**，其它 0.05s（`:123-126`）→ 迟到结果保护靠这个造出来。
- 响应内容：JSON 列返回 `{"category":"mock","input":<user_text>}`；bigint 列返回 `"7"`；其它返回 `"mock:" + user_text`（`:156-169`）；embedding 按 `vec-zero/vec-ten/vec-twenty` → `[0,0,0,0]/[10,0,0,0]/[20,0,0,0]`（`:139-147`）。
- 调用次数约束：`retry-once` 断言**恰好 2 次**请求且状态序列 `[400,200]`（`:211-216`）→ 不得重复/多余调用。
- 用例对 mock 的操作：`start/stop/reset/assert <name>`，通过 `--exec python3 mysql_test/test_suite/ai_column/include/ai_column_mock.py ...` 调用（`ai_column_t1_dml.test:4,59,69,144,178` 等），**stop 后到达的行必须终态化为 FAILED 且任务被清理**（`ai_column_retry_strict.test:51,106`）。

### 2.4 语义（从用例正文抽出的判定）

| 语义 | 证据 |
| --- | --- |
| 未完成 / 最终失败 → 结果列读到 NULL | `ai_column_t1_dml.test:31`；`ai_column_worker.result:96` |
| 依赖列更新 → **同事务内**立刻失效（结果列变 NULL）并创建任务 | `ai_column_t1_dml.test:71-79` |
| 无关列更新 → 不失效、不建任务、不发请求 | `ai_column_t1_dml.test:60-69` |
| 主键更新 → 旧主键行消失、新主键行失效、任务用**新** rowkey | `ai_column_t1_dml.test:95-107` |
| 回滚 → 行、结果、任务全部回到旧值，且不产生模型请求 | `ai_column_t1_dml.test:126-144`；`ai_column_vector_index.test:105-125` |
| 批量插入 → 每行一条任务；同事务 DELETE → 清理待执行任务 | `ai_column_t1_dml.test:146-164` |
| 分组发布：同一行同次变更的多个 AI 列**不得出现部分可见** | `ai_column_worker.test:50-74`（轮询中若看到 1 个可见即判失败） |
| 迟到结果不得覆盖新数据；旧结果请求确实发出过 | `ai_column_worker.test:170-195` + mock `stale-guard` |
| 删除行不得被后台写回复活 | `ai_column_t1_dml.test:166-172`（任务被清空）+ 写回必须是 UPDATE 语义（推断） |
| 严格读取是**列级完整性**，与谓词命中行无关：只查 id=2 也报错 | `ai_column_retry_strict.test:65-72` |
| 严格读取覆盖 SELECT / UPDATE / DELETE，且列只出现在 WHERE、子查询里也算引用 | `ai_column_retry_strict.test:74-86`；`select id, category` 不报错 `:62` |
| 严格读取判据是"状态非 READY"，任务行清空后（FAILED）仍报错 | `ai_column_retry_strict.test:107-110` |
| READY 行也可按业务字段/状态重试；零行命中是 no-op 且不建任务 | `ai_column_retry_strict.test:139-153` |
| 重试后结果被重新生成并可见 | `ai_column_retry_strict.test:118-135` |
| 向量失效 → 旧向量立刻从 ANN 结果消失 | `ai_column_vector_index.test:64-68` |
| 重算后新向量进入索引，`explain` 仍走 `VECTOR INDEX SCAN` | `ai_column_vector_index.test:94-101` + result `:73-84`（含 `__vid_0/__type_0_0` 归一化） |
| 回滚后旧向量对 ANN 仍可见 | `ai_column_vector_index.test:126-130` |
| 类型约束：`vector(4)` + `AI_EMBED(m, c, 3)` → `--error 7600`，消息 `inconsistent dimension: expected 4 got 3` | `ai_column_ddl.test:91-95` + result `:93` |
| AI 列依赖 AI 列（链式）→ `--error 1235`（OB_NOT_SUPPORTED） | `ai_column_ddl.test:96-102` + result `:99` |

## 3. 现成基础设施（proven，可直接构建其上）

### 3.1 AI 函数表达式

- 表达式类型：`T_FUN_SYS_AI_COMPLETE = 2082`、`T_FUN_SYS_AI_EMBED = 2083`、`T_FUN_SYS_AI_RERANK = 2084`、`T_FUN_SYS_AI_PROMPT = 1929` —— `src/objit/include/objit/common/ob_item_type.h:869,914-916`。
- 实现目录：`src/sql/engine/expr/ob_expr_ai/`（`ob_ai_func.{h,cpp}`、`ob_ai_func_client.{h,cpp}`、`ob_ai_func_utils.{h,cpp}`、`ob_expr_ai_{complete,embed,prompt,rerank}.{h,cpp}`）。
- `AI_COMPLETE(model, prompt [, config])` 求值入口：`src/sql/engine/expr/ob_expr_ai/ob_expr_ai_complete.cpp:91-158`（`eval_param_value(arg_model_id, arg_prompt, arg_config)`，JSON prompt 对象走 `ObAIFuncPromptObjectUtils`）。
- `AI_EMBED` 已校验"维度参数必须是正整数"：`ob_expr_ai_embed.cpp:66-67,113-118`，并把 `dimensions` 写入请求 config（`:118`）。
- 请求体构造：`ob_ai_func_utils.cpp:74-113`（`construct_messages_array`，`system`/`user` 两个 role 在 `:101-102`），鉴权头 `Authorization: Bearer <api_key>` 在 `ob_ai_func_utils.cpp:46`。**类型提示词字符串（`Return exactly one valid JSON value` / `base-10 integer`）在整个 src/ 下不存在** → 需要新增（`git grep` 无命中，见 §5）。

### 3.2 AI 模型 / Endpoint 元数据与管理

- 系统表：`__all_ai_model`（table_id **1128**）、`__all_ai_model_endpoint`（**1130**）、`__all_ai_model` 的历史表占 **1129** —— `src/share/inner_table/ob_inner_table_schema_def.py:2399-2447`。
- Schema 侧：`src/share/schema/ob_ai_model_mgr.{h,cpp}`、`ob_ai_model_schema_getter_guard.ipp`、`ob_ai_model_sql_service.{h,cpp}`；RS 侧 DDL：`src/rootserver/ob_ai_model_ddl_operator.{h,cpp}`、`ob_ai_model_ddl_service.{h,cpp}`。
- 运行时读取：`src/observer/omt/ob_ai_service.h:33 ObAiServiceGuard`（`get_ai_endpoint_by_ai_model_name` at `:39`），实现在 `src/observer/omt/ob_ai_service.cpp:83-139`（含 `check_access_privilege`）。
- Inner SQL 读写：`src/observer/ai_service/ob_ai_service_proxy.{h,cpp}`（`select_ai_endpoint_by_ai_model_name` at `.h:47` / `.cpp:133`），DDL 执行：`src/observer/ai_service/ob_ai_service_executor.{h,cpp}`。
- PL 包：`src/pl/sys_package/ob_dbms_ai_service.{h,cpp}` + `src/share/inner_table/sys_package/dbms_ai_service{,_body}_mysql.sql`（用例里的 `DBMS_AI_SERVICE.CREATE_AI_MODEL[_ENDPOINT]/DROP_*` 全部已实现）。
- 结构体：`src/share/ai_service/ob_ai_service_struct.{h,cpp}`、`ob_ai_model_info.h`；鉴权：`src/sql/privilege_check/ob_ai_model_priv_util.{h,cpp}`。
- 注意：`ObAiService`（`omt/ob_ai_service.h:46`）**只是 endpoint 查询守卫，没有任何后台线程/队列**（`start()/stop()` 是空实现）。

### 3.3 错误码空间

- AI 块现有 `-11112 … -11119`（`src/share/ob_errno.def:1884-1891`），紧接 `// 余留位置`，**`-11120` 及之后未被占用**（`git grep -E '\-111[2-9][0-9]'` 于 `.def` 无命中）→ 与用例要求的 `--error 11120` 完全吻合，说明参考实现就是往这个块里追加。
- `OB_NOT_SUPPORTED = -4007`，MySQL errno **1235**，SQLSTATE `0A000`，user msg 模板 `"%s not supported"` —— `src/share/ob_errno.def:77` → 直接可产出两条 1235 报错文本。
- `OB_ERR_INVALID_VECTOR_DIM = -7600`，SQLSTATE `22000`，user msg `"inconsistent dimension: expected %u got %u"` —— `src/share/ob_errno.def:1677` → 维度校验错误**无需新增，直接复用**。
- 生成列相关既有错误可复用：`-5290 OB_NON_DEFAULT_VALUE_FOR_GENERATED_COLUMN`、`-5292 OB_ERR_UNSUPPORTED_ACTION_ON_GENERATED_COLUMN`、`-5293 OB_ERR_DEPENDENT_BY_GENERATED_COLUMN`（`ob_errno.def:728-731`）。

### 3.4 生成列 / 虚拟列机制（存在，可借用"骨架"而非语义）

- 列 flag：`is_virtual_generated_column()`、`is_stored_generated_column()`、`is_generated_column()`、`is_generated_column_using_udf()` —— `src/share/schema/ob_column_schema.h:238-243`（`column_flags_` 位标志）。
- **依赖列追踪已存在**：`ObColumnSchemaV2::get_cascaded_column_ids(ObIArray<uint64_t> &)` —— `src/share/schema/ob_column_schema.h:153`；被表 schema 使用于 `src/share/schema/ob_table_schema.cpp:3830`、`:5773`。
- DML 侧的"级联列被改 → 生成列重算"逻辑已存在：`src/sql/resolver/dml/ob_del_upd_resolver.cpp:667`（注释 "when cascaded column is updated, the generated column will be updated with new column"）、`:715-727`（未被赋值但级联列被赋值时自动补赋值）、`:130-138 check_basic_column_generated`、`:561`、`:1429-1466`、`:2256-2345`（VALUES 里生成列排序与 default 替换）。
- 语法已有挂载点：`column_definition_ref data_type opt_collation opt_generated_keyname AS '(' expr ')' opt_storage_type opt_generated_column_attribute_list opt_column_reference opt_position_column` —— `src/sql/parser/sql_parser_mysql_mode.y:4843`；`opt_generated_keyname` at `:4913-4931`。
- 结论：**可复用"列 flag + cascaded column ids + DML 依赖检测"这套结构**；但"同步求值"语义必须替换为"置 NULL + 记状态 + 建任务"，因此是"借道不借车"。

### 3.5 隐藏列 / 列元数据持久化

- `ObColumnSchemaV2::set_is_hidden()/is_hidden()` —— `src/share/schema/ob_column_schema.h:126,196`；`__all_column` 已有 `is_hidden` 列 —— `ob_inner_table_schema_def.py:329`（table `__all_column` id 4，`:303-312`）。
- 现成的"给用户表塞隐藏列"先例：`src/rootserver/ob_ddl_service.cpp:1944`（堆表隐藏 pk）、`:10336`；`src/sql/resolver/ddl/ob_create_table_resolver.cpp:123`；索引构造期批量加隐藏列：`src/sql/resolver/ddl/ob_vec_index_builder_util.cpp:2854,2923,3010,3093,3173,3256`、`ob_fts_index_builder_util.cpp:1007,1156,1261,1361,2652`、`ob_index_builder_util.cpp:178,734,979,1033`。
- DDL 主干：`src/rootserver/ob_ddl_service.cpp`、`src/rootserver/ob_ddl_operator.cpp`、`src/share/schema/ob_table_sql_service.cpp`（均含生成列处理，`git grep -l` 命中）。
- 展示路径：`src/observer/virtual_table/ob_show_create_table.cpp` 存在；打印器 `src/sql/printer/ob_schema_printer.cpp`、`ob_raw_expr_printer.cpp`。

### 3.6 系统表新增与代码生成

- 定义文件：`src/share/inner_table/ob_inner_table_schema_def.py`；生成器 `src/share/inner_table/generate_inner_table_schema.py`，由 `CMakeLists.txt:38` 在 configure 期调用（`git grep -n generate_inner_table_schema CMakeLists.txt`）。
- 该 ref 里**没有**签入的 `ob_inner_table_schema.cpp`（`git ls-tree -r --name-only origin/2026-competition -- src/share/inner_table` 只有 py/def/dump/load）→ 纯生成产物，加表只改 python。
- 空闲 table_id：**1131、1132、1135–1145 均未被引用**（`grep "'113[12]'"`、`grep -E "'113[5-9]'|'114[0-5]'"` 于 def 文件无命中；已用的是 1128/1129/1130/1133/1134）。
- 最佳模板：`__all_vector_index_task`（id 1124）+ `__all_vector_index_task_history`（1125）—— `ob_inner_table_schema_def.py:2349-2394`，rowkey `(table_id, tablet_id, task_id)`，普通列 `trigger_type/task_type/status/target_scn/ret_code/trace_id`，且各自有 `oceanbase.` 视图暴露（`:13706`、`:13739`）。

### 3.7 会话变量

- 定义：`src/share/system_variable/ob_system_variable_init.json`，当前最大 id **10753**（`ob_sparse_drop_ratio_search`，文件尾部；`"flags": "SESSION"` 纯会话 bool/uint 变量大量先例，如该文件 `:228,460,563,1602,2364`）。
- 生成器：`src/share/system_variable/gen_ob_sys_variables.py`，产物 `ob_system_variable_init.{h,cpp}`、`ob_sys_var_class_type.h`、`ob_system_variable_alias.h` 均签入。
- `SET SESSION x = ON` → `data_type: "bool"` 的 SESSION 变量（先例：`autocommit`，json `:31-40`）。

### 3.8 后台 / 异步任务框架（**关键：这套东西 seekdb 里有整套先例**）

向量索引异步任务体系（`src/observer/vector_index/`，32 个文件）：

- 持久化任务表 + 状态机 + 重试 + 取消：`ob_vector_index_async_task_util.h:65 ObVecIndexAsyncTaskStatus`、`:74 ObVecIndexAsyncTaskType`、`:160 ObVecIndexAsyncTaskCtx`（含 `retry_time`、`task_status`、`sys_task_id`、`in_thread_pool`）、`:186 VecIndexAsyncTaskMap`（tablet→ctx 哈希表）、`:199 ObVecIndexAsyncTaskOption`（并发/容量控制）、`:235 ObVecIndexAsyncTaskHandler : public common::ObSimpleThreadPool`、`:48` 的 `check_task_is_cancel` 宏。
- 线程池本体：`src/oblib/lib/thread/ob_simple_thread_pool.h`（`git grep -l "class ObSimpleThreadPool"`）。
- 定时器与历史清理：`ob_vector_index_async_task_scheduler.h:31 class ObVectorIndexHistoryTask : public common::ObTimerTask`、`:61 ObTenantVecAsyncTaskScheduler`、`:64 VEC_INDEX_OPT_TASK_PERIOD = 10s`。
- LS 级调度器（含 DAG、回放、回调、内存检查、schema 版本检查）：`ob_plugin_vector_index_scheduler.h:188 class ObPluginVectorIndexLoadScheduler : public data_plane::ObIVectorIndexScheduler, public common::ObTimerTask`，方法含 `runTimerTask`、`check_and_execute_tasks`、`can_schedule_task`/`try_schedule_task`/`try_schedule_remaining_tasks`、`generate_vec_idx_memdata_dag`、`check_schema_version`、`handle_replay_result`、`flush`（同文件 `:223-276`）。
- 租户模块挂载点：`src/observer/omt/ob_server_runtime_controller.cpp:1455 ObPluginVectorIndexService::server_module_init(...)` → 这是"给 AI 列起一个服务"的现成注册位置。
- Change stream（提交版本驱动的变更消费框架）：`src/observer/change_stream/ob_change_stream_{dispatcher,fetcher,mgr,plugin,worker}.{h,cpp}`，插件接口 `ob_change_stream_plugin.h:43 class ObCSPlugin { init/destroy/process(ObIArray<ObCSRow>&, ObCSExecCtx&)/commit }`、`:67 ObCSPluginRegistry::register_factory`；现成业务插件 `ob_cs_plugin_async_index.{h,cpp}`。
- DDL 任务框架（持久化任务表 + 状态机 + 恢复）：`src/rootserver/ddl_task/`（48 个文件）。
- `ObTimerTask` 其他先例：`src/rootserver/dbms_job/ob_dbms_job_master.h`、`src/share/cache/ob_kv_storecache.h`、`src/oblib/lib/task/ob_timer_monitor.h`。

**结论：异步调度、领取、失败重试、中断恢复、历史清理，全部有可直接抄的骨架。**

### 3.9 同事务写辅表（模块 2 的命门）

三种已被证明存在的机制：

1. **DAS domain DML 迭代器**（与主表同一事务写索引/辅表）：`src/sql/das/ob_das_domain_utils.h:222 class ObDomainDMLIterator`、`:228 static int create_domain_dml_iterator(...)`、`:260 generate_domain_rows()` 纯虚；调用点在更新算子里、持有 `trans_desc_`：`src/sql/das/ob_das_update_op.cpp:304,347`，删除路径 `ob_das_delete_op.cpp:127`（`build_ft_doc_word_infos(trans_desc_, snapshot_, related_ctdefs_, related_tablet_ids_, ...)`）。向量索引增量维护即走此路：`src/sql/das/ob_das_dml_vec_iter.h:27 ObVecIndexDMLIterator : public ObDomainDMLIterator`、`:61 generate_vec_delta_buff_row(...)`；同族还有 `ObSparseVecIndexDMLIterator`、`ObHybridVecLogDMLIterator`、`ObEmbeddedVecDMLIterator`，以及 `ObSpatialDMLIterator`/`ObFTDMLIterator`/`ObMultivalueDMLIterator`（`ob_das_domain_utils.cpp:765,993,1250`）。
2. **绑定到外部会话/用户事务的 inner SQL**：`src/observer/ob_inner_sql_connection.h:124 create_connection_with_external_session`、`:127 create_spi_connection_with_external_session`（实现 `.cpp:175`）；连接可直接复用会话的 `tx_desc`，见 `.cpp:959 register_multi_data_source()` 中 `get_session().get_tx_desc()`（`:979`）与 `start_transaction/commit/rollback`（`.h:152-155`）。PL/SPI 正是靠它在用户事务里发 DML；DML 引擎里也有触发器挂载点：`src/sql/engine/dml/ob_dml_service.h:193 init_trigger_for_insert`、`:213 init_trigger_for_update`。
3. **MDS（事务多数据源）**：同上 `register_multi_data_source` + `ObRegisterMdsFlag`，事务原子回滚；但 MDS 数据无法被用例 `select ... from oceanbase.__all_ai_column_task` 查到，故**不能**单独满足用例，只能做补充。

`ObDMLService` 的插入/更新/删除钩子齐备：`src/sql/engine/dml/ob_dml_service.h:80 process_insert_row`、`:99 process_delete_row`、`:107 process_update_row`、`:111-135 insert_row/delete_row/update_row`（含 `ObDAS*CtDef` 重载）、`:141 split_upd_to_del_and_ins`（主键更新会被拆成删+插 → 正好解释用例里"主键变更任务用新 rowkey"）、`:172 prepare_dml_execution`、`:235-236 create_anonymous_savepoint/rollback_local_savepoint`。

### 3.10 向量列与索引维护（模块 6 基本是"免费"的）

- 建索引语法用例已在用：`create vector index ... with(distance=l2, type=hnsw, lib=vsag, m=10, ef_construction=12, ef_search=40)`（`ai_column_vector_index.test:47-49`）。
- ANN 计划：`result:73-84` 显示 `VECTOR INDEX SCAN docs(idx_p0_ai_emb)`，rowkey 是 `(docs.__vid_0, docs.__type_0_0)` → HNSW 的 vid/type 隐藏列由既有索引构造器生成（列 flag 判定见 `src/share/schema/ob_column_schema.h:268-270 is_vec_hnsw_vid_column()/is_vec_hnsw_type_column()/is_vec_hnsw_vector_column()`）。
- DML → 索引增量的通道：`ObVecIndexDMLIterator`（§3.9）+ 运行期适配器维护 `ob_plugin_vector_index_scheduler.h:235-245 check_and_execute_adapter_maintenance_task / acquire_adapter_in_maintenance`。
- 也就是说：**只要"失效 = 把向量列真写成 NULL、写回 = 真 UPDATE 新向量"，旧项移除/新项加入/回滚可见性都由现有域索引通道自动保证**（这是本文对模块 6 最重要的判断，仍属推断，需要跑用例证实）。

### 3.11 验证回路（Linux 侧，proven）

- mysqltest 官方文档与命令：`docs/developer-guide/zh/mysqltest.md`（`./obd.sh mysqltest -n test --all`；单例：`--test-dir ./mysql_test/test_suite/<suite>/t --result-dir .../r --test-set <name>`；整 suite：`--suite <name>`）。
- CI 编排：`.github/workflows/seekdb.yml` 有 4 个并行 `mysqltest-slice-*` job（`:242-251`），入口 `.github/script/seekdb/mysqltest_slice.sh`（env：`SEEKDB_BINARY`、`SLICE_IDX/SLICES`、`MYSQLTEST_PORT` 默认 5000+i×100）→ `mysqltest_for_seekdb.py run`。
- **用例发现是黑名单模式**：`mysqltest_for_seekdb.py:249 discover_cases()` 扫 `mysql_test/t/*.test` 和 `mysql_test/test_suite/*/t/*.test`（`:258-278`），减去 `tools/deploy/mysqltest_config.yaml` 的 `runtime_configs.psmall.exclude-set`；该 exclude-set 全文 18 行、**不含任何 ai_column 项** → `ai_column.ai_column_ddl` 等 5 例已被纳入 CI 集合（并且现在必然失败）。脚本还会在 `:297` 检查 result 文件缺失并报错。
- 编译入口：`build.sh`（Linux）、`.github/script/seekdb/compile.sh`；仓库里另有 `build.ps1`（"Configure or build the seekdb CMake compatibility release on Windows x64"，450 行）**但本次任务不假设它可用，且 mysqltest/mock 依赖 Linux（`/tmp`、`obd.sh`、`python3`）**。

## 4. 六个子题的缺口清单

题面"6 个子题 / 50 分"，评分表实际是 5 行（8+10+12+10+10=50），其中"条件重试与严格读取"这一行的评分重点是向量/索引 → 与 mysqltest 文件一一对应如下：

| 计分行 | 分值 | mysqltest | 已存在 | 缺口 |
| --- | --- | --- | --- | --- |
| DDL 与类型约束 | 8 | `ai_column_ddl` | 语法挂载点、隐藏列先例、`__all_column.is_hidden`、-7600/-1235 错误、AI expr | ①`AI COLUMN`/`WITH(...)` 语法节点；②`__all_ai_column` 新表 + proxy/schema/序列化/列 ID 重映射；③结果列 + 3 个隐藏列的建表/加列落地；④依赖列集合与"AI 列不可依赖 AI 列"校验；⑤**类型提示词字符串本身不存在**，需新增并持久化进 `normalized_expr`；⑥`ALTER TABLE ... RETRY AI COLUMN` 语法/arg/RS 通路；⑦`SHOW AI COLUMNS`/SHOW CREATE TABLE 展示 |
| DML 联动与事务一致性 | 10 | `ai_column_t1_dml` | 生成列级联检测、`split_upd_to_del_and_ins`、DAS 同事务辅表写、external-session/SPI inner SQL | ①`__all_ai_column_task` 新表；②DML 钩子：依赖判定 → 置 NULL + 状态列 PENDING + 生成 `rowkey_predicate`（带反引号的等值串）+ `eligible_ts`；③删除行清任务、批量插入多任务；④"模型调用只在提交后"由队列表事务性天然保证（无需 commit callback，见 §5 推断 D1） |
| 异步调度与结果写回 | 12 | `ai_column_worker` | 向量异步任务体系全套（线程池/定时器/状态机/重试/历史清理/租户挂载）、`ObAIFuncClient` curl（批量、超时、可重试状态码） | ①AI 列任务扫描/并发领取/lease 与所有权校验；②按 `request_id` 分组执行 + **一个事务发布多列结果与状态**；③自动重试（`state=3`、`retry_count`）与终态 FAILED 后**删任务行**；④写回必须用带 `request_id` 条件的 UPDATE 实现迟到结果防覆盖与"不复活已删行"；⑤类型化结果转换（JSON / bigint / varchar / vector）与转换失败处理；⑥进程重启/中断恢复 |
| 失败重试与严格读取 | 10 | `ai_column_retry_strict` | sysvar 代码生成、错误码块空闲 -11120、resolver 层列引用信息 | ①`ai_column_strict_read` 会话变量；②**运行期**列级完整性检查（引用到即查全列，SELECT/UPDATE/DELETE 三入口，子查询/WHERE-only 也算引用），且不得递归触发；③`STATUS` 重试伪列 vs 反引号业务列的区分（解析期判定）；④RETRY 语义：READY 可重做、零命中 no-op、终态 FAILED 可重做 |
| 向量列与索引维护 | 10 | `ai_column_vector_index` | HNSW/VSAG 索引、vid/type 隐藏列、`ObVecIndexDMLIterator` 同事务增量、ANN 扫描与 explain | 理论上只需：AI 向量列作为普通 vector 列参与索引 + 失效真写 NULL + 写回真 UPDATE + 维度校验/转换（`vector(4)` vs 响应长度）+ embedding 结果反序列化。**真正的风险**是写回发生在独立事务时索引可见性/时序，以及 `emb = [0,0,0,0]` 的字面量比较与 `is null` 判定的组合 |
| 性能 / 故障（非功能） | 表内不单列，"性能比较须先满足正确性" | 无（`tools/ai_gateway/` 缺失） | curl 批量、超时、`is_retryable_status_code` | 评测网关不在仓库；调用次数受 mock `retry-once` 恰 2 次约束；批处理/限流需自建，风险最高、收益不确定 |

## 5. 明确标注为"推断"的判断

- **D1（重要）**：不需要"事务提交回调"。把任务队列做成**普通事务型系统表**，DML 在同一事务里插入任务行，天然满足"提交后才发请求 / 回滚不遗留任务 / 不触发请求 / 删除清理任务"，因为 worker 只能扫到已提交行。用例的 4 个断言（事务内可见任务、回滚后任务消失、回滚行不发请求、删除清理任务）与这一设计完全自洽。反证需求不存在：全仓 `src/` 里没有任何 AI 列 commit-callback 痕迹。
- **D2**：写回采用 `UPDATE <表> SET <AI 列>..., <状态列>..., <请求列>=... WHERE <rowkey_predicate> AND <请求列> = <本次 request_id>`。这解释了三件事：为什么 `rowkey_predicate` 要存成字符串、为什么迟到结果只可能匹配 0 行、为什么已删行不会被复活（UPDATE 语义而非 INSERT/REPLACE）。属推断，但有 §2.2 的列形态强支持。
- **D3**：类型提示词在**解析期**拼进 `normalized_expr` 并持久化，运行期把提示词送 system、把表达式求值结果送 user。依据：用例同时检查 `normalized_expr` 内容与 mock 的 `system`/`user` 字段。属推断，方向明确。
- **D4**：`state = 3` 是"已失败、等待重试"的枚举值。用例只约束"retry_count=1 时 state=3 至少可见一次"。
- **D5**：严格读取检查在**执行期**做一次"非 READY 行存在性"扫描（而非解析期），因为同一语句在任务清空后（FAILED 态）仍须报错、且必须看到全表而不是谓词命中行。实现位置（`ObTableScanOp` 打开阶段 / `ObTableModifyOp` 前置）读码无法确定，需自选并验证。
- **D6**：模块 6 的索引一致性可由既有域索引通道免费提供 —— 前提是失效/写回都是真 UPDATE。这是本计划里最大的"好听的假设"，必须最先跑用例证实。
- **D7**：本 ref 的 mysqltest 已纳入 CI（§3.11）→ 强烈暗示评分就是这 5 个用例；但"隐藏评分另有其人"的可能性无法从代码排除。

## 6. 与其他 agent 的边界

`D:\scratch\ai-fn\` 已有 5 个针对 **AI 函数表达式**的补丁（`01-ai-prompt-types-and-lob.patch` 改 `ob_expr_ai_prompt.cpp`，`02/04` 改 `ob_expr_ai_embed.cpp`/`ob_expr_ai_complete.cpp`，`05-ollama-provider-and-prompt-template.patch` 改 `src/share/ai_service/ob_ai_service_struct.cpp` + `src/sql/engine/expr/ob_ai_func_utils.cpp`）。
本 AI COLUMN 工作在**类型提示词**上与之最可能撞车的正是 `ob_ai_func_utils.cpp`；因此本任务的补丁刻意**不改** `ob_expr_ai_*` 与 `ob_ai_func_utils.cpp` 的行为，把类型提示词放进新增的 AI 列模块里（新目录 `src/share/ai_column/` 或 `src/observer/ai_column/`），仅调用既有 API。
`fts-opt/`、`src/storage/blocksstable/`、`ob_das_*fts*`、排序相关文件本任务一律未触碰。
