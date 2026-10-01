# M5-A 审计整改轮验收报告（2026-10-01）

基线：`ef88722`（本地，未 push）。本报告只记录**已验证事实**与**明确未验证项**，不含推测性结论。

## 1. 结论（TLDR）

1. 审计提出的四项阻断问题（#1 孤儿 revision、#2 严格 UNRECOVERABLE、#3 v2 Milvus 真实查询路径、#4 作用域 fail-closed）与两项补充（schema/index 结构校验、`rows_skipped_existing`）**代码已落地并通过契约测试矩阵**（临时 SQLite，111 项断言全绿，退出码 0）。
2. 活栈取证已完成的部分：**真实 PostgreSQL 结构校验通过**、**真实 Milvus / Neo4j / PG 只读路径通过（非 mock，含零写入自证）**，并按用户授权（"dev 上用专用合成 KB 跑 / 只用合成专用 KB"）在 dev 活栈跑通**真实写入执行态**：PG revision 行 + Neo4j `content_revision=1` + `admin_jobs` 入队 + 幂等重跑，34 项断言全绿（§3.4）。写入面严格限制在合成 kb_id `m5a-live-20261001`，取证后已按 kb_id 整块回收并复核回到基线计数（`chunk_revisions=0`、`admin_jobs=21`），未触碰现有真实 KB。
3. 执行态取证又暴露并修复了一个静默缺陷：**同一轮新写入但未收敛的 chunk 没有排入 reindex job**（§5.4）。修复后活栈幂等重跑才复用 `targets_hash`。
4. **仍未取得证据的腿见 §6**：reindex 收敛闭环（当前代码库根本没有该 job 的消费方，属结构性缺口而非"没跑"）、Milvus 向量侧真实写入、真实上传→解析链路、规模与并发。因此**本轮不宣布 M5-A 验收通过，M5-B Go API 继续冻结**。
5. 活栈探测额外发现并已修复四处问题（§5.1–§5.4，其中 §5.3 是口径纠偏），这些缺陷都是纯 mock/SQLite 测试结构上不可能发现的。

## 2. 四项阻断问题的落点

| # | 审计要求 | 实现落点 | 验证方式 |
|---|---|---|---|
| 1 | inventory 必须纳入已有 current revision，禁止孤儿 revision 静默跳过 | `build_inventory` 的 universe 改为 `Neo4j ∪ Milvus ∪ 解析产物 ∪ PG current 行`；`neo is None and mil is None` 的已有行计入 `Inventory.orphan_revisions` 并强制 `blocked`，绝不进 `converged` | 场景 `orphan_gate`（exit 3、`orphan_revisions=1`、`blocked=1`）+ `orphan_converged`（补证据后 orphan 归零、exit 0） |
| 2 | 严格实现"无解析产物且无 Milvus 向量 = UNRECOVERABLE_MISMATCH" | `_has_recoverable_evidence(parsed, mil)`：仅解析产物有文本或 Milvus 有记录算可恢复；**仅 Neo4j 残留文本不算**；该判定只作用于无 revision 行的 new chunk | 场景 `unrecoverable_neo_only`（exit 2 + 拒绝路径零写入）、`unrecoverable_dry`（空文本解析产物） |
| 3 | 修 v2 Milvus 缺 `content_revision` 字段时的真实查询路径 + 非 mock 测试 | `_milvus_query_output_fields()`：`describe_collection` 探测实际字段后动态构造 `output_fields`，缺失字段不请求；`_milvus_collection_name()` 复用 `services.vector_store` 的 collection 归一化（§5.2） | 非 mock：`backend/tests/check_m5a_live_stack_readonly.py` 对真实 `graphinsight_chunks_v2` 做 `describe_collection` + 真实 `query`；可重复回归：场景 `collection_resolution` |
| 4 | 写入前校验 KB/tenant/project 作用域一致性，冲突零写入 fail-closed | `_load_kb_scope` 取 `knowledge_bases` 为权威，`_check_row_scope` / `_check_plan_scope` 与 revision 行、Neo4j、Milvus 证据交叉比对，另有 `kb_id` 归属防御校验；冲突 → `SCOPE_MISMATCH` 明细（chunk_id/field/expected/actual）→ 在任何写入与 dry-run 分支之前 `return 2` | 场景 `scope_conflict_new`（新 chunk tenant 冲突）、`scope_conflict_row`（已有行 project 冲突），两条都断言"PG 行、job、索引调用三空"；`scope_unresolved`（无登记不误判冲突，降级 `SCOPE_WARNING`） |

补充项落点：

- 结构校验：新增共享模块 `backend/admin/m5a_schema_check.py`（列存在/类型/可空、表级 UNIQUE 约束、索引唯一性与列顺序、部分索引谓词）。两个迁移脚本在 `--action migrate` 成功后自动回读校验，不符即退出码 1 并打印"禁止进入下一步"。
- `rows_skipped_existing`：语义固定为**决策时（写入前）已存在 current 行的 chunk 数**；写入后重算 inventory 时继承该值，避免与 `rows_new` 重复计数。已在报告行输出并被场景断言（新库首轮=0、幂等重跑=1、no_downgrade=1、orphan=1）。
- 两个计数不再同名两义（活栈取证轮改名）：写库行现在打印 `rows_new=N insert_conflicts_skipped=M`，其中 `M` 是"本次待插清单里撞 `UNIQUE(kb_id, chunk_id, content_revision)` 被跳过"的数量（干净重跑恒为 0，因为已有行在决策阶段就归入 `rows_skipped_existing`，`M` 实际是竞态/残留指标）；`rows_skipped_existing` 只表达决策时存量跳过。

## 3. 已验证证据（真实输出摘录）

### 3.1 契约测试矩阵（临时 SQLite，111 项断言）

```text
$ python backend/tests/check_m5a_revision_backfill.py
...
  ✓ 修复#1 孤儿 revision 纳入 inventory：exit 3 且 orphan_revisions=1
  ✓ 修复#1 孤儿 revision 显式列名且计入 blocked（非静默跳过）
  ✓ 修复#1 孤儿行仍计入 rows_skipped_existing=1
  ✓ 修复#1 补回索引证据后 orphan 归零、门可关闭（exit 0）
  ✓ 修复#2 仅 Neo4j 有文本判 UNRECOVERABLE（exit 2）
  ✓ 修复#2 拒绝路径零写入
  ✓ 修复#4 新 chunk tenant 与 KB 登记冲突 → fail-closed（exit 2）
  ✓ 修复#4 冲突明细含 expected/actual
  ✓ 修复#4 冲突时零写入（PG 行、job、索引调用全空）
  ✓ 修复#4 已有行 project 与 KB 登记冲突 → fail-closed（exit 2）
  ✓ 无 KB 登记时不误判冲突（SCOPE_WARNING 降级但正常执行）
  ✓ §8.5 REVISION_FIELD_ABSENT：vector 保持 pending 不伪标
  ✓ §8.5 pending 转 needs_reindex 阻断门（exit 3）
  ✓ 同一轮必须为未收敛的新 chunk 排队（禁止报告 needs_reindex 却零 job）      # §5.4 回归
  ✓ pending 目标重跑复用 targets_hash 不新建 job                                # §5.4 回归
  ✓ 幂等重跑：rows_skipped_existing=1（决策时已有行）且不重复插入（insert_conflicts_skipped=0）
  ✓ 活栈纠偏：三件套不全判 SCOPE_UNRESOLVED 且 fail-closed（exit 2）
  ✓ 活栈纠偏：内容可恢复的 chunk 不再误标 UNRECOVERABLE_MISMATCH
  ✓ 活栈纠偏：SCOPE_UNRESOLVED 同样零写入
  ✓ backfill 归一化历史 collection 名 graphinsight_chunks → _v2
  ✓ 显式非历史 collection 名保持原样（不擅自改写）
  ✓ --kb 必填（argparse 拒绝）
------------------------------------------------------------
✓ all M5-A acceptance checks passed          # 退出码 0
```

结构校验的负向自证（同套件 §A/§B）：故意 `DROP INDEX idx_chunk_rev_kb_doc_graph` 后跑 `m5a_schema_check.py chunk_revisions`，必须退出码 1 且打印 `FAILED`；重新 migrate 后该断言恢复 ✓。即校验器不是"永远返回绿"的空壳。

### 3.2 真实 PostgreSQL 结构校验（活库，只读）

```text
$ cd backend && PYTHONPATH=. python admin/m5a_schema_check.py both
[schema-check] chunk_revisions
  ✓ 表 chunk_revisions 存在 / 23 列类型与可空性逐项 ✓
  ✓ UNIQUE 约束 uq_chunk_revisions_rev
  ✓ 索引 uq_chunk_revisions_current 唯一性 / 列顺序 / 部分谓词
[schema-check] admin_jobs.targets_hash
  ✓ 列 targets_hash 类型 varchar / 可空
  ✓ 索引 uq_admin_jobs_targets_hash 唯一性 / 列顺序 / 部分谓词
✓ schema structure validation passed
```

dev 库现状：`chunk_revisions rows=0`、`admin_jobs rows=21`、`knowledge_bases rows=1`。

### 3.3 活栈只读非 mock 取证（真实 Milvus / Neo4j / PG）

```text
$ cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py
  ✓ 引擎是真实 PostgreSQL（非隔离 sqlite）
[baseline] chunk_revisions rows=0 admin_jobs rows=21
  live collections: ['graphinsight_chunks_v2']
  actual schema fields: ['chunk_id','content_hash','doc_id','embedding_model','entities_json',
                        'kb_id','location','project_id','tenant_id','text','title','vector']
  ✓ 解析到的 collection 真实存在（旧 bug：配置名 graphinsight_chunks 指向不存在的库）
  ✓ 动态 output_fields 是实际字段的子集（不会请求不存在字段）
  ✓ v2 缺 content_revision：动态 output_fields 已剔除
  ✓ §8.5 判定：milvus_revision_field=False（不伪标）
  ✓ 真实 query（动态 output_fields，限定 kb）未报错
  ✓ knowledge_bases 有可探测的 KB
[CLI] 真实入口 backfill --dry-run（写库前返回，零写入）
    [capabilities] kb_id=34905f75-… graph=enabled vector=enabled
                   milvus_collection=graphinsight_chunks_v2 milvus_revision_field=no
      neo4j_chunks=0 milvus_chunks=0 parsed_chunks=2 overlap=0 revision_rows_current=0
      new_chunks=2 needs_reindex_targets=0 converged=0 blocked=0 orphan_revisions=0
      rows_skipped_existing=0 content_mismatch=0 unrecoverable=0 scope_unresolved=0
    [gate] CLOSED mode=dry-run needs_reindex=0 blocked=0
    ✓ dry-run completed，未写库
[after] chunk_revisions rows=0 admin_jobs rows=21
  ✓ 零写入自证：chunk_revisions 行数不变
  ✓ 零写入自证：admin_jobs 行数不变
✓ live-stack read-only evidence collected (non-mock)      # 退出码 0
```

未登记 KB（只有解析产物、无 `knowledge_bases` 行）在活栈上的真实判定：

```text
kb=594b2516-… parsed=3 unrecoverable=0 scope_unresolved=3 kb_scope_missing=True  → CLI exit 2
kb=5ac90b8f-… parsed=5 unrecoverable=0 scope_unresolved=5 kb_scope_missing=True  → CLI exit 2
```

该腿已固化进只读脚本（`unregistered_parsed_kbs()` 自动探测"有解析产物但无 KB 登记"的目录），不再依赖手工命令。

### 3.4 活栈真实写入执行态取证（非 mock，2026-10-01，退出码 0）

用户授权范围：只允许在 dev 活栈写一个专用合成 kb_id，不触碰真实 KB。脚本 `backend/tests/check_m5a_live_execution.py` 据此设计四道闸门：`kb_id` 必须以 `m5a-live-` 开头、必须显式 `--confirm`（无 confirm 只打印计划并 exit 2）、方言非 postgresql 直接失败、合成 kb 有残留即拒跑；`finally` 无条件按 kb_id 整块清理并复核回基线。

```text
$ cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py --confirm
  · dialect=postgresql parsed_root=E:\projects\GraphInsight\backend\parsed_documents
[baseline] chunk_revisions total=0 admin_jobs total=21 neo4j_chunks=0 milvus_rows=0
[P2] 真实作用域冲突：backfill 必须 SCOPE_MISMATCH 拒绝且零新增写入
      SCOPE_MISMATCH count=1（fail-closed，零写入）：
        chunk_id=m5alive-c000 field=revision.project_id expected=default actual=m5a-wrong-project
      ORPHAN_REVISION chunk_ids（…）: m5alive-c000
    ✗ 拒绝执行：SCOPE_MISMATCH 1 处，零写入
  ✓ SCOPE_MISMATCH 拒绝（exit 2） / 冲突明细给出 expected/actual
  ✓ 拒绝路径零写入（仍是 1 行、0 job）
  ✓ 拒绝路径未触碰 Neo4j
[P3] 修正 scope 后跑真实写入分支（非 dry-run）
      rows_new=2 insert_conflicts_skipped=0 neo4j_updated=2 neo4j_failed=0 milvus_upserted=0 …
      MILVUS_REVISION_FIELD_ABSENT count=2（…vector 投影保持 pending…）
    [reindex] jobs_enqueued=1 jobs_reused=0 targets_total=3
      new_chunks=0 needs_reindex_targets=3 … rows_skipped_existing=1 … scope_unresolved=0
    [gate] OPEN mode=execute needs_reindex=3 blocked=0
  ✓ 3 个 chunk 全部落 revision 行 / content_revision=1 且 revision_status=current
  ✓ graph 投影真实落库：indexed + graph_content_revision=1
[P4] Neo4j 真实读回（非 mock）
  ✓ Neo4j 实际写入 2 个 Chunk 节点 / content_revision=1 / 作用域与 KB 登记一致
  ✓ Neo4j 节点文本与解析产物逐块一致（UTF-8）
[P5] Milvus 真实侧
  ✓ 报告给出 MILVUS_REVISION_FIELD_ABSENT
  ✓ 新行 vector_status=pending 且 vector_content_revision 为 NULL
  ✓ Milvus collection 内该合成 kb 真实 0 行（未被写入）
[P6] 前置门与 admin_jobs 真实入队
  ✓ 前置门 OPEN（exit 3） / reindex_chunks job 已入队 / targets_hash 为 64 位十六进制
  ✓ payload 含真实 reindex targets（chunk_id + target_revision）
[P7] 幂等重跑    [reindex] jobs_enqueued=0 jobs_reused=1 targets_total=3
  ✓ rows_new=0 且 insert_conflicts_skipped=0 / 行数不变 / 复用 targets_hash 不新建 job / Neo4j 节点数不变
[P8] 副作用面    ✓ chunk_revisions 全表行数 = 基线 + 3   ✓ admin_jobs 全表增量 = 本次入队数
[P9] 清理        · deleted: {'chunk_revisions': 3, 'admin_jobs': 1, 'knowledge_bases': 1,
                             'neo4j_chunks': 2, 'parsed_dir': 'removed'}
  ✓ 该 kb 无 revision 行 / 无 job / KB 登记行已删除 / Neo4j 无节点 / 解析产物目录不存在
  ✓ 全表回到基线行数
✓ live execution-state evidence collected (real PG + Neo4j + Milvus)
```

34 项断言全绿。这一轮跑出的是**审计前置条件的直接证据**：真实 PG 写 revision 行、真实 Neo4j MERGE `content_revision=1` 并读回、真实 `admin_jobs` 入队与 `targets_hash` 复用、真实作用域冲突 fail-closed。

## 4. dev 活栈的客观条件（影响"执行态"能否取证）

| 数据源 | 实测状态 | 对执行态验证的影响 |
|---|---|---|
| PostgreSQL | `chunk_revisions` 表存在且结构符合契约，行数 0 | 可写；写入即产生真实 revision 行 |
| Neo4j | `MATCH (n) RETURN count(n)` = **0**（labels 只有 Document/Chunk/Entity 定义，无数据） | 无存量图数据；backfill 的 Neo4j 分支会 MERGE 新建节点 |
| Milvus | 真实 collection 只有 `graphinsight_chunks_v2`，schema **无 `content_revision`**，`row_count=0` | 按 §8.5 backfill 只读判定并拒绝写版本字段（vector 投影保持 pending），不会写向量 |
| 解析产物 | 3 个 KB 目录共 10 个 chunk（其中 1 个 KB 已登记，2 个未登记） | 已登记 KB 走 `new_chunks` 正常路径；未登记 KB 已被 fail-closed 拦住 |

结论：现网 dev 缺的是"存量数据"，不是缺工具链。本轮按用户授权用**合成专用 KB 补上这份存量**跑真实写入执行态（§3.4），既拿到执行态证据，又不把真实 KB 当试验场。

## 5. 活栈发现并已修复的真实缺陷（mock 测试结构上不可能发现）

### 5.1 结构校验器在 PostgreSQL 上假失败（首轮即命中）

首轮 `m5a_schema_check.py both` 在真实 PG 上报 3 项 `✗`，SQLite 上却全绿。根因：

1. 用文本切片解析 `pg_indexes.indexdef`，部分索引的列括号后面还跟着 `WHERE (...)` 的括号，导致列序读成 `('kb_id', 'chunk_id)')`；
2. PG `information_schema` 把 varchar 回读成 `character varying`，jobs 侧断言只认字面 `varchar`。

修复：PG 侧改为读 `pg_index`/`pg_attribute`/`pg_get_expr` 与 `pg_constraint` 的权威目录，不再解析 DDL 文本；类型断言按别名集合（`varchar` / `character varying`）判定。修复后 §3.2 全绿。

### 5.2 backfill 的 Milvus collection 名与线上读写路径不一致（审计 #3 的真实根因）

`services/vector_store.py` 按契约 §11.1/决策 D3 把历史配置名 `graphinsight_chunks` 归一化为 `graphinsight_chunks_v2`，但 backfill 自己直读 runtime 配置拿到 `graphinsight_chunks`，而该库在真实 Milvus 上**不存在**。后果：backfill 会对"向量其实存在"的 KB 报 `collection 不存在 / 字段缺失`，把作用域与投影口径整体错报。

修复：新增 `_milvus_collection_name()`，复用 `vector_store.config()` 的归一化结果（导入失败才退回配置直读），`_load_milvus_chunks()` / `_backfill_milvus()` / `build_inventory` 全部改用同一解析，并把 collection 名打进 `[capabilities]` 报告行。§3.3 是该缺陷修复后的非 mock 自证。

### 5.3 口径纠偏：`SCOPE_UNRESOLVED` 与 `UNRECOVERABLE_MISMATCH` 分离

活栈实测发现"解析产物有文本但 KB 未登记"的 chunk 被计入 `unrecoverable`，语义错位（内容可恢复，缺的是作用域权威）。现分列：`scope_unresolved` 单独计数、同样 fail-closed（exit 2、零写入），处置建议改为"补 `knowledge_bases` 登记或走 reindex 重建"，不再引导走 §9 清库分支。SQLite 场景 `scope_unresolved` 与 §3.3 活栈输出双向印证。

### 5.4 同一轮未收敛的 chunk 没有排入 reindex job（执行态取证命中）

现象（修复前的 §3.4 第一轮）：写入分支打印 `[reindex] … targets_total=1`，而同一轮最终报告打印 `needs_reindex_targets=3`；重跑时才多出第 2 个 job（jobs 1→2），幂等复用断言失败。

根因：`run()` 用**决策时** inventory 的 `needs_reindex_targets` 入队，而该集合按定义只包含"本轮之前就有 current 行"的 chunk（§16 口径）。本轮新写入但投影未收敛的 chunk——例如 §8.5 下 vector 保持 `pending`、或 graph 写失败——在决策时是 `new_chunks`，不在入队清单里，而门评算是基于**写入后**的新鲜 inventory 做的。于是一场 backfill 会"报告 needs_reindex=N 却只给其中一部分排队"，运维不重跑就永远补不上缺失的 job。这与审计禁止的"静默跳过"属同一类缺陷。

修复：入队改到写入之后，清单取 `build_inventory` 重新读取的新鲜 inventory（`rows_skipped_existing` 仍从决策时继承，避免与 `rows_new` 重复计数）；模块口径第 4 条同步写死"禁止按决策时清单入队"。

验证：SQLite 新场景 `rfa_run` + `rfa_rerun`（新 chunk 写后 vector pending 必须在同一轮被排队，且重跑复用同一 `targets_hash`）；活栈 §3.4 P3/P6/P7 现在给出 `targets_total=3`、重跑 `jobs_reused=1`、job 数保持 1。

### 5.5 契约文字歧义：§15.3 步骤 8 的"第二次 needs_reindex_targets=0"

设计文档 §15.3 步骤 8 原文"重复执行：全幂等，第二次 `rows_new=0`、`needs_reindex_targets=0`"按字面读会被当成验收判据，而活栈第二轮实际是 `needs_reindex_targets=3`（job 还没被消费，投影仍是 pending）。这不是实现违约，而是那句话本意是"不重复触碰已有行"。已在文档内补口径澄清：`needs_reindex_targets=0` 只在集合被 reindex 收敛后成立；未收敛时第二轮列出**同一批** targets，幂等体现在不新增行、不重复补写索引、入队复用 `targets_hash`。

**同步说明**：§5.4 的修复与 §5.5 的澄清都只改"入队取哪一份状态"和"文字歧义"，**未修改任何验收阈值**（indexed/skipped/failed 的收敛判据、退出码语义、fail-closed 条件全部保持 v3.2.1 原样）。

## 6. 明确未验证项（不得当作已通过）

1. **needs_reindex 的收敛闭环（结构性缺口，不是"没跑"）**：`admin_jobs` 侧只验证到"真实入队 + `targets_hash` 复用 + 门保持 OPEN"（§3.4 P6/P7）。全仓搜索 `reindex_chunks` 只命中 backfill 与 M5-A 测试三处，**没有任何 worker/执行器消费该 job**——消费方属 M5-B Go API。因此"投影从 pending 收敛到 indexed、前置门 CLOSED"这条腿在当前代码库上不可能取证；只要一个 KB 存在未收敛投影，backfill 就永远 exit 3。这是设计上的刻意不收敛（§15.3 步骤 7），但必须承认验收链在此断掉。
2. **Milvus 向量侧真实写入**：活栈 collection 无 `content_revision` 字段，按 §8.5 禁止改 schema，因此"向量投影真实 backfill 成功"这条腿在当前 v2 collection 上不可能取证，必须等 v3 collection 迁移。
3. **真实业务链路**：执行态取证用的是合成 `knowledge_bases` 行 + 合成解析产物（`parsed_documents/m5a-live-20261001/`），不是"用户上传→解析→出 chunks.jsonl"的真实链路。backfill 读写契约已验，端到端业务链路未验。
4. **规模与并发**：未做。合成 KB 只有 3 个 chunk，dev 全库也只有 10 个 chunk，不具备容量与并发取证条件；`insert_conflicts_skipped` 作为竞态指标也因此没有真实触发样本。
5. `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` / `ENTERPRISE_IMPLEMENTATION_BACKLOG.md` 尚无 M5-A 条目（本轮未擅自标注状态）。

## 7. 复现命令

```bash
# 契约矩阵（临时 SQLite，无需活栈）
python backend/tests/check_m5a_revision_backfill.py

# 语法检查
python -m py_compile backend/admin/backfill_chunk_revisions.py \
    backend/admin/m5a_schema_check.py backend/admin/migrate_chunk_revisions.py \
    backend/admin/migrate_jobs_targets_hash.py backend/tests/m5a_backfill_driver.py \
    backend/tests/check_m5a_revision_backfill.py backend/tests/check_m5a_live_stack_readonly.py \
    backend/tests/check_m5a_live_execution.py

# 活栈只读结构校验（真实 PG；非破坏）
cd backend && PYTHONPATH=. python admin/m5a_schema_check.py both

# 活栈只读非 mock 取证（真实 PG/Neo4j/Milvus，全程零写入并自证）
cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py
cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py --kb <kb_id>

# 活栈真实写入执行态取证（写 PG/Neo4j，仅限 m5a-live- 前缀合成 KB，结束自动清理并复核回基线）
cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py            # 不带 --confirm 只打印计划并 exit 2
cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py --confirm  # 需要显式授权窗口
cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py --confirm --keep  # 保留数据人工复核
```

## 8. 修订记录

- v1（2026-10-01）：审计整改轮首版交付报告。四项阻断修复 + 两项补充落地；活栈完成真实 PG 结构校验与只读非 mock 取证；写入执行态未取证，M5-A 不宣布通过，M5-B 保持冻结。
- v2（2026-10-01）：补真实写入执行态取证（§3.4，合成 KB 授权窗口，34 项断言全绿、清理后复核回基线）。执行态又命中并修复一个静默缺陷（§5.4：未收敛的新 chunk 未在同一轮排入 reindex job），SQLite 矩阵从 108 增至 111 项并新增 `rfa_rerun` 回归；计数改名 `insert_conflicts_skipped`（§2）；未登记 KB 的 `SCOPE_UNRESOLVED` 拒绝腿固化进只读脚本（§3.3）；§6 改写为当前结构性缺口清单，其中 reindex job 无消费方一条决定了"门收敛"这条腿在 M5-B 之前不可能取证。设计文档 §15.3 步骤 7/8 同步补两处口径说明（§5.4 入队时机、§5.5 幂等文字歧义），**未改动任何验收阈值**。M5-A 仍不宣布通过，M5-B 保持冻结。
