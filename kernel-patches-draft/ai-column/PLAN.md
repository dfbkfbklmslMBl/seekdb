# 赛题三 AI COLUMN —— 施工计划（PLAN）

前置阅读：`GAP.md`（本文所有"已存在/不存在"的判断都指向那里的 `file:line` 证据）。
硬约束：**本机无 Linux 工具链，本文没有任何一条是跑出来的结论**；所有顺序与可行性都是读码推断，需要在 Linux 构建机上按 §4 的回路逐条证实。

## 1. 定性：这是"复用基础设施 + 新建子系统"，不是从零造轮子，也不是改几个 bug

- **不需要从零造的**（都有现成骨架，见 GAP §3）：HTTP/模型调用与超时重试（`ObAIFuncClient`）、持久化任务表 + 线程池 + 定时器 + 状态机 + 重试 + 历史清理（`src/observer/vector_index/` 全套）、租户服务挂载点（`ob_server_runtime_controller.cpp:1455` 的 `server_module_init`）、同事务写辅表（`ObDomainDMLIterator`，GAP §3.9）、列依赖追踪（`get_cascaded_column_ids`）、隐藏列、系统表代码生成、会话变量代码生成、错误码空闲位 `-11120+`、向量索引 DML 增量维护与 ANN 扫描。
- **确实一行都没有的**：`src/` 下关于 AI 列的**任何**代码（GAP §1：`git grep` 零命中）；`__all_ai_column` / `__all_ai_column_task` 两张表；`AI COLUMN` 语法；类型提示词字符串；`ai_column_strict_read` 变量；`RETRY AI COLUMN` 通路；`STATUS` 伪列；worker 分组发布与迟到保护。
- 一句话：**脚手架齐全，房子要自己盖。** 工作量的大头在 SQL/Schema/DML 三段"通路"，而不是算法。

## 2. 关键事实决定了策略：官方验收用例在仓库里（只是换了目录）

5 个 mysqltest + 5 个 `.result` + mock 都在 `tools/deploy/mysql_test/test_suite/ai_column/`（GAP §1），并且 CI 用**黑名单**发现用例、exclude-set 里没有它们（GAP §3.11）→ 这 5 个用例就是当前分支上"必须转绿"的目标，而且是**精确到错误码、系统表列名、期望字符串**的 spec。

推论（直接影响省分策略）：

1. **不要**按题面文字去新建 `test_suite/ai_function/t/ai_column_*.test`。重复定义用例名会让 `discover_cases()` 抛 `duplicate mysqltest case name`（`.github/script/seekdb/mysqltest_for_seekdb.py:275-277`）。
2. 实现应"面向 result 文件"开发：`table_id=%lu, column_id=%lu` 的消息文本、`` `id` = 2 `` 的 predicate 文本、`Bearer ` 前缀、`Return exactly one valid JSON value` / `base-10 integer` 子串、`state=3`、`count(distinct request_id)=1` —— 这些是"白送的分"，靠猜题面文字大概率拿不到。
3. 3 个 unittest（parser/resolver/schema）确实缺失，且 `unittest/` 整个 gtest 骨架被裁（只剩 2 个 `.gitignore`）。补它们等于先给自己造一套测试基建，性价比很低 → 建议**不做**，用 mysqltest 覆盖同等断言（§5）。
4. `tools/ai_gateway/` 缺失是外部评测工具，不属于数据库事务边界（题面原话），不需要我们实现；但它意味着**性能/故障注入那部分分数在本地无法验证**。

## 3. 施工顺序（按"每单位风险换多少分"排）

设计基线（GAP §5 D1/D2/D3）：任务队列 = 事务型系统表；模型调用在提交后由 worker 扫表发起；写回 = 带 `request_id` 条件的单事务 UPDATE；失效 = 真写 NULL。这一套把"事务一致性"三个最难的对齐点（回滚不遗留、提交后才调用、删除不复活）变成数据库自身的原子性，**避免去发明 commit callback**。

| 步 | 里程碑 | 产出（新建/改动位置） | 目标用例 | 累计可得分 | 风险 |
| --- | --- | --- | --- | --- | --- |
| M0 | 元数据与语法地基 | `ob_inner_table_schema_def.py` 加 2 表（id 1131/1132，GAP §3.6）；`ob_errno.def` 加 -11120..-1112x（跑 `gen_errno.pl`）；`ob_system_variable_init.json` 加 `ai_column_strict_read`（跑 `gen_ob_sys_variables.py`）；parser 加 `AI COLUMN ( expr ) WITH ( opt )` 与 `RETRY AI COLUMN` 节点；`__all_ai_column` 的 schema/proxy（抄 `ObAiServiceProxy`）+ RS 建表/加列写元数据 + 结果列与 3 个隐藏列 | `ai_column_ddl` 前半（语法+元数据） | 8 里的 3–5 | **中**：改 `.def`/`.json` 是 codegen，必须与签入产物同步；系统表新增牵涉 bootstrap/升级兼容 |
| M1 | 校验与类型提示词 | resolver 校验：`sync_mode='async'` 必填、AI 列不可依赖 AI 列（`-1235`）、维度校验复用 `OB_ERR_INVALID_VECTOR_DIM`(-7600)、表达式必须且只能引用同表列、结果列类型白名单；新增类型提示词表（JSON/整数/文本/向量→system 文本）并拼进 `normalized_expr` | `ai_column_ddl` 全绿 | **8** | **低**：纯 resolver 逻辑，用例文本已给定，最容易先落地 |
| M2 | DML 联动（同事务建任务 + 失效） | `ObDMLService::process_insert_row/process_update_row/process_delete_row` 钩子（`ob_dml_service.h:80/107/99`）；用 `get_cascaded_column_ids` 判依赖（无关列不触发）；置 NULL + 状态列 PENDING + 请求列=新 request_id；同事务插任务（首选 DAS `related_ctdef` 通道，即 `ObDomainDMLIterator` 模式，GAP §3.9；退路：`create_spi_connection_with_external_session` 走会话 tx_desc）；主键更新走 `split_upd_to_del_and_ins` 后新 rowkey 建任务；`rowkey_predicate` 生成 `` `col` = val `` 文本 | `ai_column_t1_dml` | **18** | **高**：这一步是整个任务的命门。DML 期写第二张表在 OB 里没有"AI 列"这种用法先例，需要真实跑通才敢定 |
| M3 | worker：调度 + 分组发布 + 迟到保护 | 新目录 `src/observer/ai_column/`（抄 vector_index 结构）：`ObAiColumnTaskScheduler`（`ObTimerTask`+`ObSimpleThreadPool`）、任务表扫描与并发领取/lease、按 `request_id` 分组、`ObAIFuncClient.send_post` 调用、类型化结果解析与 CAST、**单事务**发布结果+状态、`state=3`/`retry_count` 退避重试、终态 FAILED 删任务行、进程重启恢复；租户挂载点用 `server_module_init` 那套 | `ai_column_worker` | **30** | **高**：并发时序 + HTTP 错误分类 + 类型转换；测试用例的轮询窗口（0.1s×150 / 1s×15）对调度周期有硬要求 |
| M4 | 重试伪列 + 严格读取 | `ALTER TABLE ... RETRY AI COLUMN` 的 resolver→RS→执行（按谓词逐行置 PENDING + 建新 request_id 任务）；`STATUS` 伪列在解析期区分裸名与反引号；`ai_column_strict_read` 运行期检查（SELECT/UPDATE/DELETE 三入口 + 子查询/WHERE-only 引用，列级全表判"非 READY 存在"），报 -11120 带 `table_id/column_id` | `ai_column_retry_strict` | **40** | **中**：语义已完全由用例写死；难点只在"哪些语句算引用了该列"的收集面（要覆盖子查询、DML） |
| M5 | 向量列 | AI 向量列就是普通 `vector(N)` 列 + 用户自建 HNSW 索引；只需：embedding 响应→`vector` 二进制、维度与类型校验、失效真写 NULL、写回真 UPDATE。**先做 spike 验证**：对普通 vector 列做 `update ... set emb = NULL` 能否让 ANN 少一行、回滚后能否恢复 | `ai_column_vector_index` | **50** | **未知**：GAP §5 D6 说是"免费"，但未证实；若 NULL 更新不产生删除增量，就要额外做索引侧失效 |
| M6 | 效率/鲁棒（加分，非必需） | 批量请求（`send_post_batch`）、同 endpoint 并发/限流、响应丢失导致的重复推理计数、429/5xx 分类退避 | 无本地用例 | 表内不单独计 | **高/不可测**：题面"性能比较须先满足正确性"，且网关不在仓里 |

顺序要点：M0+M1 是"地基 + 8 分"，几乎无并发风险，先落地换取语法与元数据的稳定；M2 单点风险最高，要最早做可跑通的 spike；M5 的 spike 应**与 M2 并行**（几小时级，结论决定 M5 是 10 分还是 0 分）；M4 收益/风险比最好，可在 M3 未完工时插入做"部分分"。

## 4. 最小验证回路（Linux 构建机；一次性投入，之后每个里程碑都跑）

前置：`./build.sh release --init` 或复用 `.github/script/seekdb/compile.sh` 的流程；`tools/deploy/obd.sh` 部署一个单节点 seekdb（GAP §3.11，命令来自 `docs/developer-guide/zh/mysqltest.md`）。

按用例顺序逐个跑（**不要**一次 `--suite ai_column`，因为 5 个用例各自启停不同端口的 mock，失败时难以定位）：

```
cd tools/deploy
./obd.sh mysqltest -n test --test-dir ./mysql_test/test_suite/ai_column/t \
  --result-dir ./mysql_test/test_suite/ai_column/r \
  --test-set ai_column_ddl        # M0/M1 的门禁
```

依次 `ai_column_t1_dml`(19121) → `ai_column_worker`(19122) → `ai_column_vector_index`(19123) → `ai_column_retry_strict`(19124)。

注意：

- 用例里 `--exec python3 mysql_test/test_suite/ai_column/include/ai_column_mock.py start <port>` 假定 cwd = `tools/deploy/mysql_test`（路径是相对的）。若 `obd.sh` 的 cwd 不同，会直接 `python3: can't open file` → 第一步先在机器上手工验证这条 `--exec` 能命中。
- mock 需要 `python3`（stdlib only，见 `ai_column_mock.py` 只用 `http.server`），状态写 `/tmp/seekdb_ai_column_mock_<port>`；跑前清 `/tmp` 与占用端口 19120–19124。
- 全量回归：`python3 .github/script/seekdb/mysqltest_slice.sh`（设 `SEEKDB_TASK_DIR`、`SEEKDB_BINARY`、`SLICE_IDX/SLICES`），可发现新功能是否打坏了既有 `ai_function` / `vector_index` / DDL 用例。这是"不得破坏现有 AI 函数和普通 SQL"的机器可验证形式。
- 只看 diff 不收尾：失败时优先读 `mysqltest` 产生的 `.reject`/`<case>.log`，用 `--replace_regex` 已归一化的字段（`table_id=<..>`、`__vid_0`）反推实现输出是否对得上。

## 5. 官方用例覆盖不到的东西：自建用例的正确姿势

`discover_cases()` 扫 `mysql_test/t/*.test` 和 `mysql_test/test_suite/*/t/*.test`，并在 result 缺失时报错、用例名重复时抛异常（`mysqltest_for_seekdb.py:262-300`）。**因此任何放进 graded 仓的新用例都会自动进入 CI**，而我们这次的规则是"不得在 graded 仓里创建文件"。

方案（按优先级）：

1. **自建用例放 `D:\scratch\ai-column\cases\`**，用绝对路径喂给 obd：`--test-dir D:/scratch/ai-column/cases/t --result-dir D:/scratch/ai-column/cases/r --test-set <name>`。若 obd 不接受树外路径，则在构建机上把 cases 目录 symlink 进 `mysql_test/test_suite/ai_column_dev/`，**symlink 不入库**（`.gitignore` 或只在工作副本做）。
2. 自建用例要专门补官方没覆盖的失分点：
   - **重启/中断恢复**：worker 处理到一半 kill observer，重启后任务被重新领取、结果不重复、状态不残留（GAP §2.2 的"并发领取、执行中断后的恢复"）。官方 5 例完全不测恢复。
   - **`SHOW AI COLUMNS` / `SHOW CREATE TABLE` / `DESC`**：题面"查看定义"、spec 的 parser 用例标题，但 `.result` 里没有 → 说明隐藏测试很可能在这里加扣分。
   - **隐藏列对普通 SQL 的影响**：`select *`、`insert`、`load data`、备份恢复、`information_schema.columns`、`__all_column` 拷贝与列 ID 重映射（对应缺失的 `test_ai_column_schema.cpp`）。
   - **写 AI 列被拒**：`update t set tags = ...` / `insert ... (tags) values(...)` 必须报错（题面 2.1"业务 SQL 不得直接赋值修改 AI 结果列"），官方 5 例没测！可用既有错误 `-5290/-5292`（GAP §3.3）。
   - **同步模式与链式依赖**：`sync_mode='sync'` 明确报"不支持"；链式已在 ddl 用例覆盖（`-1235`）。
   - **故障注入**：429/5xx/连接中断/响应丢失（题面性能段），用 mock 加路径（不改官方 mock 文件本体，放自建 cases 的同名副本）验证"不崩溃、不错位、不泄漏、任务不丢"。
   - **DDL 演进**：`ALTER TABLE ... MODIFY`、drop AI 列、drop 依赖列、rename 列/表、加列到**已有数据**的表（回填任务）、truncate、`drop database` 后任务消失。
3. **不要**尝试补 `unittest/sql/parser|resolver|share/schema` 三个 gtest：骨架缺失，先造 harness 至少 1–2 天且不会被 CI 计分；把等价断言写成 mysqltest（尤其 DDL 元数据断言已能直接查 `__all_ai_column`，比 gtest 更接近用户可见行为）。

## 6. 可行性与分值预期（诚实版）

| 模块 | 分值 | 可行性 | 依据 |
| --- | --- | --- | --- |
| DDL 与类型约束 | 8 | 高（可全取） | 纯语法/元数据/校验，无并发；错误码与期望文本全部已知；有 FTS/向量索引的隐藏列与 codegen 先例 |
| DML 联动与事务一致性 | 10 | 中高（6–10） | 事务机制本身是免费的（队列即事务表），风险集中在"DML 执行期写第二张表"的通路：`ObDomainDMLIterator`/SPI 都可用但都不是为这种用法设计的；主键更新拆删插、批量、删除清理都要精细挂点 |
| 异步调度与结果写回 | 12 | 中（6–12） | 框架可抄，但"分组发布 + 迟到保护 + 重试态可观测 + 任务清理"四件事要在时序上同时满足用例轮询窗口；无法在本机验证，只能靠 Linux 侧反复跑 |
| 失败重试与严格读取 | 10 | 中（5–10） | 严格读取的"引用列收集"要覆盖 SELECT/UPDATE/DELETE/子查询/视图，边界多；`STATUS` 伪列与反引号区分是解析期小活儿，稳 |
| 向量列与索引维护 | 10 | 未知（0–10，赌 spike） | 若真写 NULL 即可让旧向量退出 ANN，则这 10 分几乎跟着 M2/M3 白送；若不成立，需要进 `ObVecIndexDMLIterator`/adapter 维护，成本翻倍 |
| 合计 | 50 | **乐观 40/50；中性 25–30/50** | |

中性预期的由来：M0–M4 是一条串行链（DDL 不成就没有元数据，DML 不成就没有任务，没有任务就没有 worker 可测），任何一环卡住都会同时压掉后面所有模块的分。因此**第一个能跑通 `ai_column_ddl` 的部分绿**比"写完再跑"重要得多。乐观预期的 40 分需要：spike 证实向量"免费"、M2/M3 在第一周内端到端跑通、并且愿意用大量时间对齐 `.result` 文本细节。

给"要不要做"的结论：**这题值得做，但不是 6 个模块 6 个战场的题，而是 1 条主链 + 2 个 spike（向量增量、DML 同事务辅表写）的题。** 先把两个 spike 在构建机上打出结论，再决定投 M2 还是先扫 M1/M4 的稳分。

## 7. 风险与未解问题（需要在 Linux 侧回答）

1. **R1（阻塞级）**：DML 执行期内同事务写 `__all_ai_column_task` 到底走哪条通道？DAS related ctdef（需要 cg/rtdef 与 tablet 解析）vs SPI 嵌套 SQL（需要证明递归 SQL 与 plan cache/超时/共享资源安全）。两条都有先例但都不是现成答案。
2. **R2**：新增系统表对 **bootstrap / 升级兼容** 的影响（`meta_record_in_sys`、`is_cluster_private`、历史表、`__all_virtual_*` 视图与 `oceanbase.__all_ai_column` 可见性/权限）——用例直接从 `oceanbase.` 查，说明需要租户可见且可读。
3. **R3**：`.def`/`.json` codegen 与**签入产物**的同步方式（`gen_errno.pl` 需要 perl，`gen_ob_sys_variables.py` 需要 python2/3 约定；`generate_inner_table_schema.py` 由 `CMakeLists.txt:38` 在 configure 期跑）。跑错顺序会产生巨大无关 diff。
4. **R4**：worker 的调度周期必须让 `retry_count=1`/`state=3` 在 0.1s 轮询里可见、让初始任务在 1s×15 内完成；`VEC_INDEX_OPT_TASK_PERIOD = 10s`（GAP §3.8）对 AI 列太慢，需要更短周期或事件驱动（change stream 可作事件源，`ob_change_stream_plugin.h:43`）。
5. **R5**：任务表在**多机/多副本**下的领取互斥（用例只跑单节点，但"并发领取、执行中断恢复"是评分项）；租约/超时/接管策略需要设计。
6. **R6**：严格读取的实现若用"额外一次全表扫描"，与 `ai_column_strict_read` 下的大表语义/成本如何取舍；且该检查自身的读不能再次触发检查（递归）。
7. **R7**：`normalized_expr` 里持久化提示词后，"优化不得改变模型输入"与"定义版本校验"如何落（`__all_ai_column` 需要一个 definition/version 字段，用例未直接引用，属推断，见 GAP §2.2 末尾"任务归属/定义版本"）。
8. **R8**：性能/故障分不可本地验证（网关缺失），任何"批处理/限流"优化都可能打坏 `retry-once` 的"恰好 2 次调用"断言。

## 8. 补丁草稿清单（`D:\scratch\ai-column\patches\`）

只写了"读码足以确定内容"的机械层补丁；上层（DAS/DML/worker）涉及 §7 R1 的选型，未给伪代码补丁以免误导。

| 文件 | 内容 | 状态 |
| --- | --- | --- |
| `01-inner-tables-ai_column.patch` | `__all_ai_column` + `__all_ai_column_task` python def（id 1131/1132），列名严格对齐用例引用 | 草稿，未编译/未跑 codegen |
| `02-errno-ai_column.patch` | `-11120` 严格读取错误（文本逐字对齐 `.result`）+ 重试相关错误位 | 草稿，需 `gen_errno.pl` 同步产物 |
| `03-sysvar-ai_column_strict_read.patch` | json 新增 SESSION bool 变量（id 10754，默认 0） | 草稿，需 `gen_ob_sys_variables.py` 同步产物 |
| `04-parser-ai_column-skeleton.patch` | 语法骨架：`AI COLUMN ( expr ) WITH ( ... )` 与 `RETRY AI COLUMN` 动作 | **仅设计示意**，未验证，节点类型与 action 合并规则需实现者定 |

另有只读副本：`ref-tests/`（11 个官方用例与 mock 的原文，便于对照实现）。
