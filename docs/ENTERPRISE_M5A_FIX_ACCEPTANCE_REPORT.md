# M5-A 审计整改轮验收报告（2026-10-01）

基线：`63f9932`（v1/v2 交付基线；v3 追加轮 §9 在其后单独提交）。2026-10-02 的 M5-B0 返工提交为 `df29a72`、`373544e`、`44393e7`，验收记录为 `4640cab`；当前本地 `main` 未 push。本报告只记录**已验证事实**与**明确未验证项**，不含推测性结论。

## 1. 结论（TLDR）

1. 审计提出的四项阻断问题（#1 孤儿 revision、#2 严格 UNRECOVERABLE、#3 v2 Milvus 真实查询路径、#4 作用域 fail-closed）与两项补充（schema/index 结构校验、`rows_skipped_existing`）**代码已落地并通过契约测试矩阵**（临时 SQLite，断言全绿，退出码 0；断言数为 **110**，v1/v2 写的 111 是计数口径虚高，见 §9.5）。
2. 活栈取证已完成的部分：**真实 PostgreSQL 结构校验通过**、**真实 Milvus / Neo4j / PG 只读路径通过（非 mock，含零写入自证；断言 20 项，§3.3）**，并按用户授权（"dev 上用专用合成 KB 跑 / 只用合成专用 KB"）在 dev 活栈跑通**真实写入执行态**：PG revision 行 + Neo4j `content_revision=1` + `admin_jobs` 入队 + 幂等重跑，全绿退出码 0（§3.4；其历史"34 项"含子进程回显与汇总行，统一口径下的准确数需带 `--confirm` 复跑才能取，见 §9.5）。写入面严格限制在合成 kb_id `m5a-live-20261001`，取证后已按 kb_id 整块回收并复核回到基线计数（`chunk_revisions=0`、`admin_jobs=21`），未触碰现有真实 KB。
3. 执行态取证又暴露并修复了一个静默缺陷：**同一轮新写入但未收敛的 chunk 没有排入 reindex job**（§5.4）。修复后活栈幂等重跑才复用 `targets_hash`。
4. **仍未取得共享生产证据的腿见 §6**：B0 的 Python worker 消费闭环和临时 v3 向量写入已经验证，但共享 v3 迁移、C3 数据治理、真实上传→解析链路、规模与并发仍未验证。因此**本轮不宣布 M5-A 共享生产验收通过，M5-B Go API 继续冻结**。
5. 活栈探测额外发现并已修复四处问题（§5.1–§5.4，其中 §5.3 是口径纠偏），这些缺陷都是纯 mock/SQLite 测试结构上不可能发现的。
6. v3 追加轮闭合存量风险任务 **#55**：`check_kb_migrations_smoke.py` 原用"置空 env 覆盖变量 + 注入 sqlite 地址"的**伪隔离**，其 `rollback` 步实际会打到开发 PostgreSQL 的两张真实表。已改为 env 文件真隔离并在任何破坏性动作前加方言守卫，同时新增静态防回归守卫；历史影响面的可证否部分与**不可判定窗口**见 §9.2。

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

### 3.1 契约测试矩阵（临时 SQLite，110 项断言）

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

34 行 ✓（其中含 CLI 回显的 `✓ dry-run completed` / `✓ backfill 完成` 与末尾汇总行；统一口径的断言数见 §9.5）。这一轮跑出的是**审计前置条件的直接证据**：真实 PG 写 revision 行、真实 Neo4j MERGE `content_revision=1` 并读回、真实 `admin_jobs` 入队与 `targets_hash` 复用、真实作用域冲突 fail-closed。

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

1. **共享生产 needs_reindex 收敛仍未取证**：M5-B0 已在 SQLite 和唯一临时 v3 namespace 验证 backfill 入队 → Python worker 消费 → 两侧投影回写 → 幂等复跑；共享生产 KB 的 C3（blocked/orphan/unrecoverable/scope）逐 KB 清单和实际收敛仍未完成，因此共享生产门保持 OPEN。
2. **Milvus 向量侧共享生产写入仍未取证**：临时 v3 collection 已证明显式 `content_revision INT64`、真实 upsert 数量和读回；共享 v2 collection 仍禁止改 schema，共享 v3 collection 尚未迁移。
3. **真实业务链路**：执行态取证用的是合成 `knowledge_bases` 行 + 合成解析产物（`parsed_documents/m5a-live-20261001/`），不是"用户上传→解析→出 chunks.jsonl"的真实链路。backfill 读写契约已验，端到端业务链路未验。
4. **规模与并发**：未做。合成 KB 只有 3 个 chunk，dev 全库也只有 10 个 chunk，不具备容量与并发取证条件；`insert_conflicts_skipped` 作为竞态指标也因此没有真实触发样本。
5. `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` / `ENTERPRISE_IMPLEMENTATION_BACKLOG.md` 已同步 M5-B0 当前状态；共享生产迁移和 C3 治理仍明确标为未完成。

## 7. 复现命令

```bash
# 契约矩阵（临时 SQLite，无需活栈）
python backend/tests/check_m5a_revision_backfill.py

# 迁移幂等/回滚 smoke（v3 起为真隔离：env 文件 + 破坏性动作前的方言守卫）
python backend/tests/check_kb_migrations_smoke.py

# 静态守卫（含 #55 的 sqlite 隔离写法防回归）
python backend/tests/check_migration_cleanup_guards.py

# 语法检查
python -m py_compile backend/admin/backfill_chunk_revisions.py \
    backend/admin/m5a_schema_check.py backend/admin/migrate_chunk_revisions.py \
    backend/admin/migrate_jobs_targets_hash.py backend/tests/m5a_backfill_driver.py \
    backend/tests/check_m5a_revision_backfill.py backend/tests/check_m5a_live_stack_readonly.py \
    backend/tests/check_m5a_live_execution.py backend/tests/check_kb_migrations_smoke.py \
    backend/tests/check_migration_cleanup_guards.py

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
- v2（2026-10-01，历史记录）：补真实写入执行态取证并记录当时的 reindex 消费缺口；该状态已由 2026-10-02 的 M5-B0 实现和临时 v3 取证 supersede，当前结论见本报告 §6。
- v3（2026-10-01）：闭合任务 #55（迁移测试 DB 伪隔离）。新增 §9 记录根因、"历史是否曾在活 PG drop 表"的取证结论与不可判定窗口、整改后的三层守卫输出与静态防回归。**本节追加在修订记录之后，是为了保持审计已引用的 §1–§8 编号不变**。M5-A 结论不变：仍不宣布通过，M5-B 保持冻结。
- v4（2026-10-02）：新增 §10——Windows UTF-8 验收链修复（验收基础设施）。基线复现出四条真实失败腿与**一处假绿灯**（只读脚本的"已登记 KB CLI"步骤不看 `returncode`，子进程已崩仍判通过），整改 5 个文件（三个被点名脚本 + 统一守卫入口 + 迁移 smoke 的父进程侧），取消"必须带 `-X utf8`"这个历史前提；第二项五条命令用普通 `python` 复跑全部 `EXIT=0`。§10.4 如实登记全仓同类缺口 22 个文件（本轮未越界修改），§10.5 勘误 M4R1 报告里"运行前提"的旧表述。**M5-A 仍不宣布通过，M5-B 保持冻结，未 push。**
- v5（2026-10-02，历史记录）：新增 §11 依赖方案。当时文档为 v1 提案；随后 `df29a72`/`373544e`/`44393e7` 完成 M5-B0，依赖方案已更新为 v2。M5-A 共享生产结论不变：仍不宣布通过，M5-B Go API 保持冻结，未 push。

---

## 9. 追加轮：迁移测试 DB 伪隔离整改（任务 #55）

### 9.1 现象与根因

`backend/tests/check_kb_migrations_smoke.py` 的文档口径是"临时 SQLite，不触碰开发/生产数据库"，
但它对子进程用的是 `ADMIN_DATABASE_URL=sqlite:///...` + `GRAPHINSIGHT_BACKEND_ENV_FILE=""`。
`backend/admin/database.py:14-22` 只在该变量**指向存在的文件**时才走隔离分支；空串落到 `else`
执行 `load_dotenv(find_dotenv(), override=True)`，沿子进程脚本 `__file__` 向上命中 `backend/.env`，
用其中的 PostgreSQL 地址**覆盖**注入的 sqlite 地址。旧脚本同文件三处（子进程 env、bootstrap、
父进程 inspector）都用了这个伪隔离配方。

实测探针（复刻旧配方，仅 `--dry-run`，零 DDL）：

```text
数据库: postgresql://graphinsight:****@127.0.0.1:5434/graphinsight_admin
方言: postgresql
计划动作: rollback
- drop table knowledge_base_documents
- drop table knowledge_bases
```

即：旧 smoke 的 `rollback` 步一旦在装有 `backend/.env` 的机器上手工执行，就删的是开发库的两张真实表。

### 9.2 历史是否真的在活 PG 上 drop 过表（结论 + 不可判定窗口）

能证否的部分（当前活库只读快照，全程未下发任何 DDL）：

- `knowledge_bases` 现存 1 行，`created_at = 2026-09-30 00:46:43.324408+00`（E2E 合成 KB）。该行仍在**当前表版本**里，故 2026-09-30 00:46 之后未发生过 DROP。
- OID 单调：`knowledge_bases=32906`、`knowledge_base_documents=32932`、`chunk_revisions=33001`（本轮 M5-A 建）。
- `admin_logs` 的 `project_id/kb_id` 位于 attnum 31/32，恰为该表 attnum 上界（32）；`admin_qa_traces` 的 `tenant_id/project_id/kb_id` 位于 42/43/44，恰为上界（44）。`migrate_audit_scope_columns.py` 的 rollback 会 `DROP COLUMN`、migrate 再 `ADD COLUMN`（attnum 只增不复用），若这套 smoke 真跑过活库，这些列会落在更高的 attnum 上并留下空洞——现未见该痕迹。
- 行数面：`knowledge_base_documents` 0 行、`chunk_revisions` 0 行；`pg_stat` 累计 `knowledge_bases` ins=5/del=4、`knowledge_base_documents` ins=10/del=10，量级与"建表后仅 E2E/测试轮次使用"一致。

不可判定的窗口（如实记录，不粉饰）：

- 时间窗：`2026-09-29 14:46`（`9a11561` 引入伪隔离）→ `2026-09-30 00:46`（E2E 行落入当前表版本）。
- 该窗口的语句日志取不到：活库 `logging_collector=off`、`log_statement=none`，`data_directory=/var/lib/postgresql/data`（容器内），无历史 DDL 日志可查；OID 布局与该窗口内"是否曾 rollback 再 recreate"并不互斥。
- **结论：这一段窗口无法从数据库侧证实或证伪。** 影响面评估：按 M1 契约 KB 目录不建 default KB、不迁旧数据，dev 库该窗期内无业务存量；且该 smoke 从未进 CI，也未挂进 `run_unified_boundary_guards.py` / `run_backend_smoke_suite.py`（Explore 取证：仅 `9a11561` 一次提交，聚合器无引用），只有手工运行才可能触发。因此不存在"生产数据被删"的路径，dev 库当前数据面自洽。

### 9.3 整改内容

1. 隔离手法统一为：临时目录内生成真实存在的 `kb_mig_smoke.env`（内容 `ADMIN_DATABASE_URL=sqlite:///...`），**所有子进程与父进程共用这一份**，并在父进程 import 前写入 `os.environ`。
2. 破坏性动作前置守卫：`guard_isolation()` 先验证子进程与父进程引擎方言均为 sqlite；每个迁移脚本再跑一次 `--dry-run` 并解析其打印的 `方言:`，非 sqlite 即中止，**一条 migrate/rollback 都不下发**；每个真实动作额外断言 "exit 0 且仍为 sqlite"。
3. 守卫有效性自证：把 env 文件内容改指 PostgreSQL，引擎方言必须随之变化（`create_engine` 惰性，只读方言、不建连接），证明守卫不是摆设。
4. 静态防回归：`backend/tests/check_migration_cleanup_guards.py` 新增 `test_sqlite_isolated_tests_use_env_file_not_blank_override()`——凡源码含 `sqlite:///` 的 `check_*.py`，禁止出现置空 `GRAPHINSIGHT_BACKEND_ENV_FILE` 的写法，且必须把该变量指向真实 env 文件。该守卫已随 `migration_cleanup` case 进入 `run_unified_boundary_guards.py`。

### 9.4 整改后真实输出

`python backend/tests/check_kb_migrations_smoke.py`（19 项，EXIT=0）：

```text
隔离方式: GRAPHINSIGHT_BACKEND_ENV_FILE -> kb_mig_smoke.env
  ✓ 子进程引擎守卫（必须 sqlite）
  ✓ 父进程引擎守卫（必须 sqlite）
  ✓ 基础表引导
[migrate_knowledge_base_tables.py]
  ✓ dry-run 解析到 sqlite（真实动作前的最后一道闸）
  ✓ 首次 migrate（exit 0 且仍为 sqlite）
  ✓ 重复 migrate（幂等）（exit 0 且仍为 sqlite）
  ✓ rollback（exit 0 且仍为 sqlite）
  ✓ rollback 后再 migrate（exit 0 且仍为 sqlite）
[migrate_audit_scope_columns.py]  （同上 5 项全 ✓）
  ✓ 结构校验连接的仍是 sqlite（非误连开发库）
  ✓ knowledge_bases 存在
  ✓ knowledge_base_documents 存在
  ✓ admin_logs 含 project_id/kb_id
  ✓ admin_qa_traces 含 tenant/project/kb
  ✓ 守卫有效性自证（env 文件改指 PG 时方言必须变化，否则守卫是摆设）
✓ all migration smoke checks passed
```

静态守卫与负向自证：

```text
$ python backend/tests/check_migration_cleanup_guards.py
MIGRATION_CLEANUP_GUARDS_OK            # GUARD_EXIT=0
$ # 负向自证 A：把 HEAD 里带伪隔离的旧 smoke 原样复制成 tests/check_zz_head_legacy_probe.py
AssertionError: ... check_zz_head_legacy_probe.py 置空 env 覆盖变量（伪隔离）: GRAPHINSIGHT_BACKEND_ENV_FILE"] = "";
              ... check_zz_head_legacy_probe.py 未把 env 覆盖变量指向真实 env 文件   # PROBE_EXIT=1
$ # 负向自证 B：最小合成样本（sqlite 注入 + 置空变量），同样被点名
AssertionError: ... check_zz_guard_probe_tmp.py 置空 env 覆盖变量（伪隔离）: ...   # PROBE_EXIT=1
$ # 两个探针文件均已删除（tests/check_zz* 计数为 0），守卫复跑回到 exit 0
```

即：守卫不是只对合成样本生效，对**真实历史 bug 文件**同样会红。

活库回归对照（整改后复跑，与 §9.2 快照逐字一致，证明本轮零触碰）：

```text
dialect = postgresql | db = graphinsight_admin
  knowledge_bases          rows=1  oid=32906 relfilenode=32906
  knowledge_base_documents rows=0  oid=32932 relfilenode=32932
  chunk_revisions          rows=0  oid=33001 relfilenode=33001
  admin_logs      last_attnum=32 live_cols=16
  admin_qa_traces last_attnum=44 live_cols=20
```

全仓同类写法排查：`GRAPHINSIGHT_BACKEND_ENV_FILE` 置空仅此一例（已修）；含 `sqlite:///` 的 10 个 `check_*.py`
现全部使用 env 文件手法。`check_dual_kb_blackbox.py` 的置空属于活栈测试"就要连开发库"的有意行为，不含 sqlite 注入，不在禁列。

### 9.5 本轮自查纠偏：断言计数口径虚高（我自己犯的，主动认账）

现象：v1/v2 里的"111 项 / 22 项 / 34 项"是用 `grep -c "✓"` 统计整份输出得到的，把两类**非断言行**算了进去：

1. 套件末尾的汇总行，例如 `✓ all M5-A acceptance checks passed`；
2. 被回显进输出的子进程日志行，例如 backfill CLI 的 `✓ dry-run completed，未写库`、`✓ backfill 完成，前置门 CLOSED`。

统一口径（此后一律照此计数）：**断言数 = 输出中匹配 `^  ✓`（两空格前缀，`step()` 的固定格式）的行数**；失败对应 `^  ✗`。

2026-10-01 按统一口径复跑核对：

| 套件 | v1/v2 记录 | 统一口径实测 | 差异来源 |
| --- | --- | --- | --- |
| `check_m5a_revision_backfill.py` | 111 | **110**（✗ 0，exit 0） | 多算 1 行汇总 |
| `check_m5a_live_stack_readonly.py` | 22 | **20**（✗ 0，exit 0） | 多算 1 行汇总 + 1 行 CLI 回显 |
| `check_kb_migrations_smoke.py`（v3 整改后） | — | **19**（✗ 0，exit 0） | 本轮新计 |
| `check_migration_cleanup_guards.py` | — | 守卫输出 `MIGRATION_CLEANUP_GUARDS_OK`，负向自证 exit 1 | 非 step 型套件，按退出码判定 |
| `check_m5a_live_execution.py` | 34 | **需带 `--confirm` 复跑才能给准数**（其回显含 CLI 的 ✓ 行） | 本轮未复跑，避免对 dev 活库做无必要的真实写入 |

影响面：**不改变任何结论**。四个套件当时与现在的退出码都是 0、失败断言都是 0，虚高只出现在"数量表述"，不涉及阈值、不涉及通过/失败判定，也未掩盖任何缺陷。§3.4 的准确断言数按上表标注为待复跑项，不在本文里猜数。

---

## 10. 追加轮：Windows UTF-8 验收链修复（2026-10-02，验收基础设施）

裁定口径：**M5-A 本轮仍不验收**；这一轮只修验收基础设施——此前所有 Windows 取证都把
`python -X utf8` 当成运行前提（`docs/ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md:110` 明文写着"这是运行命令前提"），
本轮取消这个前提：**脚本自身必须能用普通 `python` 直接通过**。

### 10.1 基线复现（普通 `python`，显式清掉 `PYTHONUTF8`/`PYTHONIOENCODING`）

前置证据：`python -c "import sys; print(sys.version.split()[0], sys.stdout.encoding)"` → `3.14.7 gbk`，
即下列失败都发生在 Windows 默认码下，不是我把环境配坏了。

| # | 腿 | 基线结果 | 根因 |
| --- | --- | --- | --- |
| 1 | `check_kb_migrations_smoke.py` | **EXIT=1**，`UnicodeEncodeError: 'gbk' codec can't encode character '\u2713' in position 2`（崩在 `step()` 的 print） | 父进程没有 `reconfigure`，只有子进程侧有 `_utf8_env()` |
| 2 | `check_m5a_live_stack_readonly.py` | **EXIT=1**，两条断言失败：`✗ kb=… 真实 CLI 因 SCOPE_UNRESOLVED 拒绝（exit 2） (exit=1)` | 子进程没有 `env=`，`backfill_chunk_revisions.py` 自己在 `print` 处崩，真实退出码 2 被 1 顶掉 |
| 3 | CLI 侧独立取证 | `python backend/admin/backfill_chunk_revisions.py --kb 5ac90b8f… --dry-run` → **EXIT=1**，输出字节流 `utf8-decodable: NO`（invalid start byte at 47），末尾 `UnicodeEncodeError: 'gbk' codec can't encode character '\u2717'`（`backfill_chunk_revisions.py:1112`） | CLI 自身未强制 UTF-8 |
| 4 | `run_unified_boundary_guards.py` | **EXIT=1**，`UnicodeEncodeError`（`run_unified_boundary_guards.py:115` 的 `print(output[:12000])`） | 统一入口把子进程输出原样回显，父进程没强制 UTF-8 |
| 5 | **假绿灯（最严重）** | 基线日志第 63 行：`✓ CLI dry-run 有结构化输出（exit=1）` | 该步骤只断言输出含 `[capabilities]`/`[inventory]`，**没有断言 `returncode == 0`**，子进程已经崩了仍判通过 |

第 5 条单独认账：这不是"编码显示问题"，而是**编码缺陷把一条断言变成永久绿灯**——
只要 CLI 在 Windows 默认码下必崩，这条检查就既显示通过、又与真实退出码无关。

### 10.2 整改内容（五个文件，逐条对应裁定要求）

| 要求 | 落点 |
| --- | --- |
| 父进程 stdout/stderr 强制 UTF-8 | `check_kb_migrations_smoke.py:36-38`（本轮新增）、`run_unified_boundary_guards.py:19-24`（本轮新增）；`check_m5a_live_stack_readonly.py`、`check_m5a_live_execution.py` 原有 |
| 所有 Python 子进程传 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` | readonly 新增 `_utf8_env()` + `_run_backfill_cli()`（两处 CLI 调用收敛为一个入口）；live_execution `run_cli(..., env=_utf8_env())`；migrations smoke 原有 `_base_env()`（守卫自证用的 `probe_env` 同样继承） |
| 已登记 KB 的 CLI 检查断言 `returncode == 0` | readonly 新增步骤 `已登记 KB 的 CLI dry-run exit 0 且有结构化输出`，断言式 `proc.returncode == 0 and "[capabilities]" in out and "[inventory]" in out` |
| 失败时输出完整 exit code 和 stderr | readonly `_run_backfill_cli(expect_code=…)`、live_execution `run_cli(..., expect_code=…)` 在退出码≠期望值时打印 `!! … exit=N，期望 exit=M` + 完整 `[stderr]`；migrations smoke `_dump_failure()` 打印完整 stdout/stderr 不截断；守卫入口 `_run_case` 改为返回真实退出码，失败腿不再截断到 12000 字符并打印 `[FAIL] <case> exit=N` |
| 不得用 `-X utf8` 掩盖 | 本轮全部复跑命令一律 `env -u PYTHONUTF8 -u PYTHONIOENCODING python …`（§10.3）；§7 复现命令表本就是普通 `python`；那条"运行前提"表述按 §10.5 勘误 |

退出码契约同步写实：live_execution 三处 `run_cli` 现在显式声明期望码（作用域冲突 dry-run=2、真实写入=3、幂等重跑=3），
并把原先**完全没有断言**的幂等重跑退出码补成步骤 `幂等重跑按契约退出（exit 3，前置门仍 OPEN）`。
这里的期望值不是 0，是设计语义：`exit 3` 表示前置门 OPEN，属刻意不收敛（§6.1），不是失败。

### 10.3 整改后真实输出（普通 `python`，Windows 默认码）

```
EXIT=0  checks_pass=110  backend/tests/check_m5a_revision_backfill.py
EXIT=0  checks_pass=69   backend/admin/m5a_schema_check.py both
EXIT=0  checks_pass=20   backend/tests/check_m5a_live_stack_readonly.py
EXIT=0  checks_pass=19   backend/tests/check_kb_migrations_smoke.py
EXIT=0                   backend/tests/check_migration_cleanup_guards.py   （MIGRATION_CLEANUP_GUARDS_OK）
```

补充取证：

- readonly 关键三行：`✓ 已登记 KB 的 CLI dry-run exit 0 且有结构化输出`、
  `✓ kb=5ac90b8f-… 真实 CLI 因 SCOPE_UNRESOLVED 拒绝（exit 2）`、`✓ 零写入自证：chunk_revisions 行数不变`；
  末行 `✓ live-stack read-only evidence collected (non-mock)`。零写入自证仍成立（`chunk_revisions rows=0`、`admin_jobs rows=21` 前后一致）。
- 乱码核查：整改后只读取证日志按 UTF-8 解码，`U+FFFD` 计数 = **0**（整改前同一份日志里 `SCOPE_WARNING` 等中文行全是替换符）。
- 已登记 KB 的 dry-run 真实退出码单独复核过（不是为断言编期望值）：
  `kb=34905f75-38ca-4cfa-bf22-c89c96107fa8` → `EXIT 0`，输出 `[capabilities] … milvus_revision_field=no`、
  `[inventory] … new_chunks=2 … scope_unresolved=0`、`[gate] CLOSED mode=dry-run needs_reindex=0 blocked=0`。
- 统一守卫入口：`python backend/tests/run_unified_boundary_guards.py` → **EXIT=0**、`SUMMARY total=16 failed=0`
  （含 `secret_scanner_selftest` 3.0s）。整改前同一条命令 EXIT=1。
- `check_m5a_live_execution.py` 不带 `--confirm`：`EXIT=2` 且只打印计划（fail-closed 未变）。
  带 `--confirm` 的真实写入腿**本轮未执行**——按裁定需单独授权才能写 dev 活栈。

### 10.4 同类缺口的全量扫描结果（如实登记，本轮未越界修改）

用"打印 `✓` 但父进程无 `reconfigure`"与"起子进程但不传 `PYTHONUTF8`"两条规则扫全仓，除本轮五个文件外仍有命中，
**都不在 M5-A 五条验收命令链路上**，因此不影响 §10.3 的 exit 0 结论，但属同一类缺陷：

- CLI 侧（直接手工调用仍会在 GBK 控制台崩；经本轮改造后的取证套件调用时由 `env=` 兜住）：
  `admin/backfill_chunk_revisions.py`、`admin/migrate_*.py`（13 个迁移脚本）、`admin/reset_legacy_knowledge_data.py`、
  `scripts/seed_e2e_local_stack.py`。
- 套件侧（经 `run_unified_boundary_guards.py` 调用时被父进程 `_utf8_env()` 覆盖，单独直跑仍可能崩）：
  `check_artifact_secrets_selftest.py`、`check_dual_kb_blackbox.py`、`check_kb_scope_isolation.py`、`check_scope_contract.py`，
  以及 `run_backend_smoke_suite.py`、`run_perf_soak.py`、`run_rollback_drill.py`、`run_migration_rollback_smoke.py` 等 18 个"起子进程不传 UTF-8"的入口。

推荐处置：M5-A 链路已闭合；第二批应在 M5-B 开工前一并收（尤其 CI 里会直跑的入口），避免再次出现"编码缺陷把断言变成绿灯"。
是否现在就扩到这两批文件需要拍板——**本轮没有擅自改动这 22 个文件**。

### 10.5 对历史文档的勘误（不改写当轮事实，只标注失效）

`docs/ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md:110` 当轮记录："不带 `-X utf8` 直接跑 `check_kb_migrations_smoke.py` 时，
父进程在 GBK 控制台打印 `✓` 会 `UnicodeEncodeError`；这是运行命令前提"。
**该"前提"自 2026-10-02 起作废**：脚本自身已强制 UTF-8（§10.2），普通 `python` 实测 `EXIT=0`（§10.3）。
原文按版本留痕原则保留，仅在 M4R1 报告原地处标注失效并回指本节。

### 10.6 本轮边界

1. 未宣布 M5-A 验收通过；M5-B Go API 仍冻结（第 3 项依赖方案另见新增设计文档）。
2. `check_m5a_live_execution.py --confirm` 未执行（写活栈需单独授权），其 P1–P8 断言数仍按 §9.5 口径待复跑。
3. §10.2 的断言变更**只加不减**：新增 `returncode == 0` 断言与幂等重跑退出码断言，未放宽任何既有阈值；
   readonly 步骤总数仍为 20（旧步骤 `CLI dry-run 有结构化输出` 改为带 `exit 0` 的更强表述，不是新增计数）。
4. 未 push（本轮改动留在本地待复核）。

---

## 11. 追加轮：M5-A / M5-B 依赖方案交付（2026-10-02，第三项）

同样追加在修订记录之后，保持 §1–§10 编号不变。

### 11.1 交付物

`docs/ENTERPRISE_M5AB_REINDEX_DEPENDENCY_PLAN.md`（已更新为 v2：B0 已实现，生产迁移和 C3 仍未完成）。该方案回答四项：`reindex_chunks` 由谁消费、job service / worker / Go API 的最小接入边界、Milvus v3 如何提供 `content_revision`、`needs_reindex_targets` 何时真正收敛 CLOSED；当前实现证据见 `ENTERPRISE_SPRINT_M5B0_ACCEPTANCE_2026-10-02.md`。

### 11.2 对 §6 措辞的精确化（主动认账）

§6 的历史文字曾写"reindex job 无消费方 ⇒ '门收敛'这条腿**在 M5-B 之前不可能取证**"。该表述已被 B0 实现取代，不能作为当前代码状态：

1. 入队方（backfill）和执行方（Python 进程内 worker）都在 Python 侧，Go 只提供人工触发与列表界面；
2. 因此存在一个不触碰 Go 的最小切片（依赖方案 §3.1 的 **M5-B0**）即可让门变得可取证；
3. 正确的阻断表述是：**在 M5-B0（Python worker 消费 `reindex_chunks`）+ Milvus v3（显式 `content_revision`）之前不可能取证**，两者缺一不可（只做 v3 收敛不了任何既有 `needs_reindex_targets`，只做 worker 会被 §8.5 的 v2 缺字段拒写拦回 `pending`）。

这不改变本轮任何验收阈值与结论，只把"等什么"说准。

### 11.3 依赖方案顺带暴露的两处后续风险

1. `backfill_chunk_revisions.py` 的 Milvus `content_revision` 在 backfill 直接路径仍固定为 revision 1；新 chunk 路径成立，但回放/回滚已有 revision 时可能版本假降，留给后续 #73。
2. Python/Go 仍有多份 job type 白名单，B1 开工前需要跨语言对账静态守卫；当前 Go API 仍冻结，不能把该风险误写成 B0 未实现。

### 11.4 门禁自证

`check_artifact_secrets.py` 对新增文档单独扫描：`files=1 findings=0 result=pass EXIT=0`（普通 `python`，未加 `-X utf8`）。既有四份文档同批扫描 `findings=10`（M5A 报告 1 条 DSN + M4R1 报告 9 条历史标注），本轮新增命中 0。

### 11.5 待用户拍板（依赖方案 §7）

B0 是否授权开工（会改 `RUNNABLE_JOB_TYPES` 并新增真实写索引的执行体）、v3 迁移窗口（共享 dev Milvus 环境变更 + 回滚预演）、不可收敛清单的处置口径（删孤儿行 vs 永久标注 blocked）。
