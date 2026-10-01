# M5 Chunk 版本与索引状态 — 设计说明 v3.2.1（评审修订稿）

状态：待评审（v3.2.1，2026-10-01）
上游契约：`docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` §2.6 / §2.9 / §2.10 / §4 M5
v2 基线：2026-09-30 稿（§0-§14）；v3 增补见 §15；v3.1 增补见 §16；v3.2 增补/覆盖见 §17；v3.2.1 为最终收口（仅设计文档与迁移契约，两个阻断项见下方 v3.2.1 修订对照），与旧口径冲突处一律以 v3.2.1 为准。
本轮约束：仅修订设计文档与迁移契约（targets_hash DDL 冻结在 §16.3），禁止新增迁移、API、worker 或前端代码。

## v3 修订对照

| # | 复审要求 | v3 落点 |
| --- | --- | --- |
| 1 | targets 多 chunk revision payload | §15.2 |
| 2 | current revision 部分唯一索引 | §15.1 |
| 3 | backfill 对 Neo4j/Milvus 真实补写 | §15.3 |
| 4 | Milvus v3 写入、切换、回滚顺序 | §15.4 |
| 5 | stale/skipped/failed 重建与 retry 规则 | §15.5 |
| 6 | QA graph/vector 双投影版本语义 | §15.6 |
| 7 | 错误码及 status/revision_status 契约同步 | §15.7 |
| 8 | 旧图关系清理与失败恢复 | §15.8 |

v3 附件：完整 reindex job JSON 示例 §15.9、backfill 时序 §15.3、Milvus v3 切换/回滚时序 §15.4、契约差异表 §15.7、更新后验收矩阵 §15.10。

## v3.1 修订对照

| # | 复审要求 | v3.1 落点 |
| --- | --- | --- |
| 1 | 冻结 Milvus 迁移期读写策略：写冻结或双写，禁止 v3 写入而 v2 继续作正常读源 | §16.1 |
| 2 | backfill 对已有 revision / Neo4j-only / Milvus-only / 内容不一致 / 中途失败的处理 | §16.2 |
| 3 | `dedupe_key` 改为 `targets_hash`；不同 job 终态下的复用与 retry 规则 | §6.1、§7、§8、§15.9、§16.3 |
| 4 | 直接修改旧 §6/§7/§8 的 targets/stale 旧口径，全文只保留一套规范 | §6.1、§6.3、§7、§8.2、§8.3 |
| 5 | QA 增加 `content_source`；graph/vector 分别最新与整体最新语义 | §16.4 |
| 6 | 旧图清理覆盖 MENTIONS 边，给出实际 Cypher 范围 | §16.5 |
| 7 | 实际同步 P0 契约、错误码表、Python/Go 类型与 Backend API Spec | §15.7 + 契约文件落库（§16.6） |
| 8 | 修正所有 job JSON，tenant_id/project_id/kb_id 完整，只处理 current revision | §15.9 |

v3.1 附件：Milvus R/W 状态机 §16.1、backfill 失败恢复时序 §16.2、dedupe/retry 状态表 §16.3、QA content_source 与双投影最新语义 §16.4、MENTIONS 清理 Cypher §16.5、实际修改的契约文件清单 §16.6。

## v3.2 修订对照

| # | 阻断项 | v3.2 落点 |
| --- | --- | --- |
| 1 | backfill 只处理不存在 revision 的 chunk，禁止把已有 current revision 降级为 revision 1 | §16.2 表格 + §15.3 步骤（索引补写仅限无 revision 行 chunk） |
| 2 | reindex-document 明确只枚举 revision_status=current | §15.2 填充规则（API Spec §3.3 同步） |
| 3 | 冻结 targets_hash 唯一索引 DDL 和 failed job 的原地 retry 状态转换 | §16.3（DDL + 状态转换图 + 原地重置语义） |
| 4 | Milvus S2 之后回滚到 v2 的数据一致性规则 | §16.1（写冻结→回放→校验→切回五步） |
| 5 | 删除 chunk_revisions 作为 citation 内容 fallback，保持 D1 索引即权威 | §16.4（回退链 neo4j→milvus，缺失标 INDEX_MISSING） |
| 6 | INDEX_UNAVAILABLE 纳入统一错误码 | §15.7 错误码表 + exceptions.py/scope.go 落库（§17.4） |
| 7 | 增加 source_content_hash，修正重新解析幂等判断 | §3 字段表 + §10 幂等规则（基于 source_content_hash，非 content_hash）+ 契约类型落库 |
| 8 | 修正 Python ChunkRevision.edited_at 的 nullable 口径 | scope_contract.py `edited_at: str`（非 Optional），与 DB NOT NULL 一致（§17.4） |
| 9 | targets_hash retry 补齐并发规则：SELECT FOR UPDATE 锁定、cancelled 原地重置不耗配额、max_retries 超限拒绝 | §16.3（锁后分支 + cancelled 状态转换 + 超限 409 `JOB_409` + 审计 `kb_chunk_reindex_failed`，§17.3 验收） |
| 10 | backfill inventory 增加 `needs_reindex_targets`，前置门未收敛不关闭 | §15.3 步骤 1/7 + §16.2 表格 + §17.1（已有 revision 行分路，§17.3 验收） |

v3.2 附件：v3.2 验收矩阵 §17.3、v3.2 实际修改的契约文件清单 §17.4。

## v3.2.1 修订对照

| # | 阻断项 | v3.2.1 落点 |
| --- | --- | --- |
| 1 | targets_hash 改为**包含所有状态**的部分唯一索引（`WHERE targets_hash IS NOT NULL`），保留 SELECT FOR UPDATE，failed/cancelled 均原地 retry，删除"排除 failed/cancelled"矛盾口径 | §16.3（DDL 注释 + 索引谓词 + ON CONFLICT 谓词 + 状态转换，§17.3 验收） |
| 2 | backfill 前置门修正：indexed 必须满足 `*_content_revision == current.content_revision`；skipped 必须满足"能力未配置 且 `*_content_revision IS NULL`"，允许关闭技术迁移前置门但必须输出 `DEGRADED_SKIPPED`，不得宣布完整索引验收通过；failed/stale/pending 继续阻断前置门 | §15.3 步骤 1/7 + §16.2 表格 + §17.3 验收 |

v3.2.1 附件：v3.2.1 修订直接改写 §16.3 / §15.3 / §16.2 / §17.3 对应口径，全文只保留一套规范；验收矩阵增补见 §17.3。

## 0. 本版修订对照

| # | 审计要求 | 本版落点 |
| --- | --- | --- |
| 1 | 完整字段表、nullable/default/index/unique | §3 |
| 2 | 所有 SQL/锁/更新/回滚显式带 kb_id | §3、§4 |
| 3 | 契约字段名 `edited_at/reason` | §3 |
| 4 | 删除空 chunk_ids 隐式全量语义 | §7 |
| 5 | 拆分 revision 生命周期与 graph/vector projection 状态 | §3 |
| 6 | 写入、stale、任务创建事务边界与幂等、wake、旧任务保护 | §6、§8 |
| 7 | 已有 chunk backfill 或受控清库 | §9 |
| 8 | 人工编辑、重新解析、重新建图优先级 | §10 |
| 9 | stale 期间 QA current/indexed revision 与状态 | §11 |
| 10 | Milvus content_revision 字段与 collection 迁移/回滚 | §8.5 |
| 11 | 作用域冲突必须拒绝 | §4 |
| 12 | 并发、跨 KB、作用域、部分失败、重复任务、重试矩阵 | §12 |

## 1. 范围与验收门

M5 实现"Chunk 可编辑版本 + 独立索引投影状态"，四项能力：

1. Chunk 内容可编辑，产生不可变 revision 历史；并发编辑用乐观锁裁决，失败方得 `CHUNK_REVISION_CONFLICT`（409）。
2. 编辑/回滚后受影响 chunk 的 graph/vector 投影分别置 `stale`，只重建 stale 投影（不整库重建）。
3. QA 引用能回答"用的哪个版本"：同时暴露 `indexed_content_revision`、`current_content_revision`、`graph_status`、`vector_status` 与 `is_latest`。
4. `kb:write` 权限覆盖编辑/回滚/重建写路径。

验收门（对齐审计 §4 M5）：并发编辑一胜一 409；回滚生成新 revision；编辑后重建只处理显式范围内 stale chunk；QA trace 可答版本与新旧状态；无 `kb:write` 编辑被拒；跨 KB/跨作用域请求被拒。

## 2. 现状基线（v1 已核实的代码事实，仍有效）

| 事实 | 证据 |
| --- | --- |
| chunk 文本写三处：Neo4j `Chunk.text`、Milvus `text`（PK=`chunk_id`）、`parsed_documents/{kb_id}/{doc_id}/chunks.jsonl` | document_graph_service.py:637 / vector_store.py:132 / document_graph_service.py:1255 |
| QA citation 文本首选读 Neo4j `Chunk.text`，Neo4j 缺失时回退 Milvus metadata | retrieval_orchestrator.py:459,581,612 |
| `content_revision` 字段已贯通 retrieval → citation → QA 响应，但建图时从不写入 | retrieval_orchestrator.py:603,629；doc_qa_service.py:1039；document_graph_service.py 无写入点 |
| `knowledge_base_documents.graph_status/vector_status` 列存在，但生产链路无 indexed/stale/failed 写点 | models.py:233；Go `MarkDocumentStatus` 无非测试调用 |
| 任务类型仅 `build_graph / clear_kb / reindex`；`reindex` 只重建 Neo4j 全文索引 | job_runtime.py:87,252 |
| 无按 chunk_id 的局部重建/重嵌入入口 | document_graph_service.py:238,852,921；vector_store.py:132 |
| `kb:write` 已注册并授予 super_admin/project_admin/operator | rbac_seed.go:29,51,56 |
| Milvus collection：`graphinsight_chunks_v2`，dynamic field 开启，显式字段含 chunk_id/doc_id/kb_id/tenant_id/project_id/text 等；现有 schema 无显式 `content_revision` | vector_store.py:91-137 |
| Milvus 已有"维度/schema 冲突不得静默 drop，必须人工迁移新 collection"模式 | vector_store.py:122-137 |
| `admin_jobs` 有 payload/status/lease/retry 等字段，无幂等键 | models.py:159-182 |
| citation snapshot 已含 `content_revision` 键 | doc_qa_service.py:1030 |

## 3. 数据模型：chunk_revisions 完整字段表

字段名与契约 §2.6 对齐：单一时间口径 `edited_at`，单一原因口径 `reason`；不引入 `created_at`/`edited_reason` 第二套命名。状态拆成三个正交维度：revision 生命周期、graph 投影、vector 投影。

```text
chunk_revisions:
  revision_id             BIGSERIAL PK
  kb_id                   VARCHAR(100) NOT NULL
  tenant_id               VARCHAR(100) NOT NULL
  project_id              VARCHAR(100) NOT NULL
  doc_id                  VARCHAR(255) NOT NULL
  chunk_id                VARCHAR(255) NOT NULL
  source_content          TEXT NOT NULL
  source_content_hash     VARCHAR(80) NOT NULL                  # sha256(source_content)，重新解析幂等判断（v3.2）
  content                 TEXT NOT NULL
  content_hash            VARCHAR(80) NOT NULL         # sha256(content)，供重建幂等判断
  content_revision        INTEGER NOT NULL
  revision_status         VARCHAR(20) NOT NULL DEFAULT 'current'   # current | superseded
  graph_status            VARCHAR(20) NOT NULL DEFAULT 'pending'    # pending | stale | indexed | skipped | failed
  vector_status           VARCHAR(20) NOT NULL DEFAULT 'pending'    # pending | stale | indexed | skipped | failed
  graph_content_revision  INTEGER NULL                             # 最近一次成功/尝试写入 Neo4j 的 revision
  vector_content_revision INTEGER NULL                             # 最近一次成功/尝试写入 Milvus 的 revision
  revision_source         VARCHAR(20) NOT NULL DEFAULT 'system_initial'  # system_initial | system_reparse | human_edit | rollback
  source_version          VARCHAR(100) NULL
  parser_version          VARCHAR(100) NULL
  edited_by               INTEGER NULL REFERENCES admin_users(id)  # system 生成行可为 NULL
  edited_at               TIMESTAMPTZ NOT NULL DEFAULT now()
  reason                  VARCHAR(500) NULL
  trace_id                VARCHAR(100) NULL

  唯一约束: UNIQUE (kb_id, chunk_id, content_revision)
  索引:
    (kb_id, chunk_id, revision_status)
    (kb_id, revision_status, graph_status)
    (kb_id, revision_status, vector_status)
    (kb_id, doc_id, revision_status, graph_status)
    (kb_id, doc_id, revision_status, vector_status)
```

状态语义：

- `revision_status`：只有 `current` 行的 `content` 是当前权威内容；`superseded` 行不可变、只用于历史与回滚。
- `graph_status` / `vector_status`：chunk 级投影状态，互相独立。`skipped` 表示能力未配置（LLM 关闭或 embedding 未启用），**不代表 indexed**；文档级聚合见 §6。
- `graph_content_revision` / `vector_content_revision`：记录某个投影实际对应的 revision，先标记 stale 时保留旧值，直到重建成功才更新；这样 stale 期间也能精准回答"索引里是哪个版本"。
- `revision_source` / `source_version` / `parser_version`：支撑 §10 的人工/解析/重建优先级与幂等。

## 4. 作用域纪律

**所有 chunk_revisions 查询、锁、更新、回滚、唯一性判断、重建范围和 backfill 都必须显式带 `kb_id`。** chunk_id 即使实际全局唯一，也不得作为隔离依据。

示例（设计冻结的 SQL 形态）：

```sql
-- 读最新
SELECT * FROM chunk_revisions
 WHERE kb_id = $1 AND chunk_id = $2 AND revision_status = 'current'
 ORDER BY content_revision DESC LIMIT 1;

-- 乐观锁读（同一事务内再插入）
SELECT content_revision FROM chunk_revisions
 WHERE kb_id = $1 AND chunk_id = $2 AND revision_status = 'current'
 FOR UPDATE;

-- 旧行失效
UPDATE chunk_revisions SET revision_status = 'superseded'
 WHERE kb_id = $1 AND chunk_id = $2 AND content_revision = $3;

-- 回滚目标
SELECT * FROM chunk_revisions
 WHERE kb_id = $1 AND revision_id = $2;
```

作用域冲突规则：请求同时携带 `X-KB-ID` header、query `kb_id`、body `kb_id` 时，**所有出现的作用域值必须一致**；任一与路径 `kb_id` 不一致，一律 400 `KB_CROSS_SCOPE`，不得静默忽略。路径 `kb_id` 只是权威起点，不是"只认它"的特权。

重建范围校验：请求中的每个 `chunk_id` 都必须属于路径 `kb_id`；任一不属于 → 404 `CHUNK_NOT_FOUND`，不部分执行。

## 5. 版本语义与并发控制

- `content_revision` 为每 chunk（按 kb_id+chunk_id 作用域）单调递增整数，初始 1。
- 建图即创建 revision 1（`revision_source=system_initial`，`source_content=content=解析原文`，`graph/vector_status` 按投影结果落 `indexed|skipped|failed`）。
- PATCH 编辑：`expected_revision` 必须等于该 chunk 当前 `current` 行的 `content_revision`；事务内 `FOR UPDATE` 读当前行，不匹配 → 409 `CHUNK_REVISION_CONFLICT`，响应携带 `current_revision` 供客户端重试。
- 编辑成功：插入 `content_revision = 旧值+1` 的新行（`revision_source=human_edit`，`revision_status=current`，`graph_status=stale`，`vector_status=stale`，两个 `*_content_revision` 保留旧值），旧行置 `superseded`。
- 回滚：`POST .../revisions/{revision_id}/rollback` 复制目标 revision 的 `content` 为新行（`content_revision=当前+1`，`revision_source=rollback`，`reason` 记录来源 revision），历史行全部不变。历史不可变由此成立。

## 6. 事务边界、幂等与旧任务保护

### 6.1 编辑/回滚的单一事务

下列动作必须在**同一个 PostgreSQL 事务**内完成，任一失败整体回滚：

```text
BEGIN
  1) 按 (kb_id, chunk_id) FOR UPDATE 锁定当前 current 行并校验 expected_revision
  2) INSERT 新 revision（content_revision=old+1）
  3) UPDATE 旧行 revision_status='superseded'
  4) 新行 graph_status='stale', vector_status='stale'（保留旧 *_content_revision）
  5) 按 kb_id+doc_id 聚合重算 knowledge_base_documents.graph_status/vector_status
  6) INSERT admin_jobs（job_type='reindex_chunks'，payload 含 kb_id/tenant_id/project_id/doc_id/targets，
     targets_hash=sha256(canonical_json({targets}))，作用域三件套与 payload 一致）
  7) INSERT admin_logs 审计（kb_chunk_updated / kb_chunk_rolled_back）
COMMIT
-- 提交后才执行 best-effort wake；wake 失败不影响事务，worker 轮询兜底
```

约束与恢复规则：

- **幂等键**：`admin_jobs` 新增 `targets_hash VARCHAR(64) NULL` + 唯一索引（迁移脚本 ALTER TABLE，保留历史 NULL 行）。同一 `(job_type, kb_id, targets_hash)` 重复提交 → `ON CONFLICT` 复用既有 job，不重复入队；不同终态下的复用与 retry 规则见 §16.3。
- **wake 失败**：job 已落库为 `pending`，由既有 worker 轮询与 lease/heartbeat 机制接管；wake 仅优化延迟。
- **编辑后再次编辑**：每次编辑产生新的 revision（新 target_revision），targets_hash 随之变化；旧 revision 的待执行 job 保持存在，但 worker 执行前校验 `target_revision == 当前 current revision`，不等则标记 `OUTDATED_SKIPPED`，不写索引。
- **旧任务晚于新任务执行**：worker 在写 Neo4j 前、写 Milvus 前各做一次当前 revision 复核；只有 target 仍等于 current 才写，写后再次复核才允许把投影置 `indexed`。若期间有新编辑提交，投影保持 `stale`，由新 job 收敛。
- **worker 重试**：任务幂等；重试先复核 target/current 与投影状态，已完成则 no-op。Milvus 与 Neo4j 非事务一致，采用"复核 + 收敛"而非伪原子：旧 worker 即使短暂写入旧 revision，新 job 会覆盖，且 stale 可见性保证客户端不会误认为最新。
- **任务重复创建**：唯一索引拦截；不同 source（编辑/手动/文档级）生成相同 targets 时 targets_hash 相同，只保留一个 job。

### 6.2 文档级状态聚合

`knowledge_base_documents.graph_status/vector_status` 是**聚合结果，不由单个任务直接覆盖**：

```text
failed   > stale/skipped > pending > indexed
graph 聚合:  任一 chunk failed -> failed；否则任一 stale|skipped -> stale；
             否则任一 pending -> pending；全部 indexed -> indexed
vector 聚合: 同上规则
```

`skipped` 计入文档级 `stale`（诚实表达"未完整建索引"），避免把 LLM/embedding 未配置伪装成 indexed。

### 6.3 时序（编辑 → 重建）

```text
Client        Go API                PostgreSQL              Python worker        Neo4j/Milvus
  |  PATCH       |                      |                        |                   |
  |------------->| BEGIN                |                        |                   |
  |              | lock current row     |                        |                   |
  |              | insert rev N+1       |                        |                   |
  |              | supersede rev N      |                        |                   |
  |              | mark stale + doc agg |                        |                   |
  |              | insert job(targets_hash) |                        |                   |
  |              | insert audit         |                        |                   |
  |              | COMMIT ------------->|                        |                   |
  |<-- 200 rev N+1|                      |                        |                   |
  |              | best-effort wake ---------------------------> |                   |
  |              |                      |<-- claim job --------- |                   |
  |              |                      |                        | target==current?  |
  |              |                      |                        | re-extract/re-embed
  |              |                      |                        |------------------>|
  |              |                      |<-- projection result --|                   |
  |              |                      |                        | status indexed if |
  |              |                      |                        | still target==current
```

## 7. API 端点设计（Go native，`/api/v1/admin/knowledge-bases/{kb_id}/` 下）

作用域规则按 §4：路径 `kb_id` 与 header/query/body 出现的作用域值必须一致，否则 400 `KB_CROSS_SCOPE`。

| 方法 | 路径 | 权限 | 请求 | 响应要点 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/admin/knowledge-bases/{kb_id}/chunks/{chunk_id}` | `kb:read` | — | 当前 `content` + `content_revision` + `revision_status` + graph/vector 投影状态与 `*_content_revision` + revisions 列表（`limit/offset` 倒序，默认 50）；chunk 不存在 404 `CHUNK_NOT_FOUND` |
| PATCH | `/api/v1/admin/knowledge-bases/{kb_id}/chunks/{chunk_id}` | `kb:write` | `{expected_revision:int, content:str, reason?:str}` | 成功：新 `content_revision` + 投影状态；冲突：409 `CHUNK_REVISION_CONFLICT` 且响应含 `current_revision`；空 content 400 `CHUNK_CONTENT_EMPTY`；审计 `kb_chunk_updated` |
| POST | `/api/v1/admin/knowledge-bases/{kb_id}/revisions/{revision_id}/rollback` | `kb:write` | `{reason?:str}` | 新 revision（内容=目标 revision）+ 投影置 stale + 审计 `kb_chunk_rolled_back`；目标 revision 必须属于该 kb，否则 404 |
| POST | `/api/v1/admin/jobs/reindex-chunks` | `kb:write` | payload `{kb_id, chunk_ids:[...]}` | `chunk_ids` 非空且全部属于 `kb_id`（任一不属于 → 404，不部分执行）；空 → 400 `REINDEX_SCOPE_REQUIRED`；返回 job_id |
| POST | `/api/v1/admin/jobs/reindex-document` | `kb:write` | payload `{kb_id, doc_id}` | 显式文档范围：重建该文档全部 stale chunk（服务端按 kb_id+doc_id 枚举，非隐式扩层）；返回 job_id |

说明：

- 新建 `go-backend/internal/adminstore/chunk_revisions.go`（请求 struct + `(Item, error)` + 哨兵错误 `ErrChunkNotFound/ErrRevisionConflict/ErrScopeMismatch` + `BeginTx`/`rollbackUnlessCommitted`）与 `go-backend/internal/httpserver/admin_kb_chunks_native.go`（`guard.wrap("kb:read"/"kb:write", ...)`）；路由注册进 `handlers.go` 与现有 KB 段同处。
- 新建 `reindex-chunks` / `reindex-document` 走任务中心（Go `adminJobTypeFromPath` 白名单与 `job_runtime.py` 分发两侧同步，job_type 命名与 targets_hash 前缀规则一致，见 §15.9 注）。
- 409 响应体：`{code: "CHUNK_REVISION_CONFLICT", current_revision: N, expected_revision: M}`，客户端用 `current_revision` 重读后重试。
- 审计事件写入沿用 §2.10：`operator_id, tenant_id, project_id, kb_id, trace_id, action`；拒绝路径（`KB_CROSS_SCOPE / KB_ACCESS_DENIED`）同样写审计。

## 8. 重建范围与任务语义

### 8.1 范围显式原则

**不存在任何"空数组/空值 → 隐式扩大范围"的语义。** 重建范围只有两种合法表达：

- `reindex-chunks`：显式非空 `chunk_ids` 列表（同一 kb）。
- `reindex-document`：显式 `kb_id + doc_id`，服务端只在该文档内选择 stale chunk。

### 8.2 reindex 执行语义（Python worker）

payload `{kb_id, tenant_id, project_id, doc_id, source, targets:[{chunk_id, target_revision}]}`（统一形态见 §15.2，v3.1 不再存在 `chunk_ids`/裸 revision 旧口径）。**worker 只处理 `target_revision == 当前 current.content_revision` 的 target**，其余标记 `OUTDATED_SKIPPED`。对每个目标 chunk：

1. 读当前 `current` 行；无行或 `target_revision != current.content_revision` → 标记 `OUTDATED_SKIPPED`，不写索引。
2. 按能力可用性重建投影：
   - graph 投影：写 Neo4j `Chunk.text` + `content_revision`，LLM 开启时重新抽取实体/关系，`llm_disabled` 时置 `graph_status=skipped`（关系未建，**不伪装 indexed**）；
   - vector 投影：重新 embedding 并 upsert Milvus（`content_revision` 显式 INT64），embedding 未配置时置 `vector_status=skipped`。
3. 每次投影写入前、写入后各复核 `current` 是否仍等于 target；不等则放弃该投影写入并保持 stale（由新任务收敛）。
4. 成功后更新该行 `graph_status/vector_status=indexed`、对应 `*_content_revision=target`。
5. 任务结束按 §6.2 聚合重算文档级状态。

### 8.3 旧任务保护

写 Neo4j 前/后、写 Milvus 前/后四道复核（§6.1 规则）；`targets_hash` 唯一索引防止重复入队（复用规则 §16.3）；`OUTDATED_SKIPPED` 保证 revision 2 的旧任务不会覆盖 revision 3。Milvus 无条件 upsert 的残余窗口由"新任务覆盖 + stale 可见性"收敛，不引入伪原子。

### 8.4 无 LLM 口径

`llm_disabled` 下 `reindex-chunks/reindex-document` 照常重建文本与向量；graph 投影为 `skipped`，文档级按 §6.2 聚合为 `stale`。验收口径继续按"检索/引用链路 smoke"声明，不宣称完整问答质量。

### 8.5 Milvus `content_revision` 冻结与 collection 迁移

现状：`graphinsight_chunks_v2` 开启 dynamic field，`content_revision` 若写入只会进 dynamic metadata，类型不可靠；现有 schema 无显式字段（vector_store.py:91-137）。

设计冻结：

- 新建 collection **`graphinsight_chunks_v3`**：显式字段与 v2 相同，另加 `content_revision INT64 NOT NULL`；`chunk_id` 仍为 PK。**禁止在 v2 上改 schema、禁止自动 drop v2。**
- `vector_store` 的 `ensure_collection` 增加 v3 分支（带 `content_revision` 字段 + 已有 kb 字段校验）；`upsert_chunks` 的 `content_revision` 一律写显式 INT64，且不被 `chunk.metadata` 覆盖（沿用现有"作用域以显式字段为准"模式）。
- 迁移步骤（人工控制，脚本只做辅助）：
  1. 用新代码创建 `graphinsight_chunks_v3`；
  2. 按 kb 执行 `reindex-chunks`/`reindex-document` 把向量写入 v3（带正确 `content_revision`）；
  3. 校验：v3 计数与 v2 一致、抽样 search 返回 metadata 中 `content_revision` 为 int 且值正确；
  4. 切换 `vector_store.collection=v3` 并重启，复验搜索；
  5. 回滚：切回 `graphinsight_chunks_v2` 并复验；v2 保留，不 drop。
- 验证断言纳入验收矩阵：metadata 类型 `isinstance(..., int)`、数值等于 chunk 当前 revision。

## 9. 存量 chunk 处理：backfill（主） / 受控清库（备）

**主选：按 kb_id 限定、可重复、可 dry-run 的 revision 1 backfill。**

新增 `backend/admin/backfill_chunk_revisions.py`：

1. `--kb <kb_id>`（必填）+ `--dry-run`：输出存量清单 `NEO4J_CHUNKS / MILVUS_CHUNKS / OVERLAP`，不写库。
2. 执行：对每个 Neo4j chunk 插入 revision 1 行（`source_content=content=Chunk.text`，`revision_status=current`，`revision_source=system_initial`），`ON CONFLICT (kb_id, chunk_id, content_revision) DO NOTHING` → 可重复、幂等。
3. 投影初始状态按证据落：Neo4j 有 text → `graph_status=indexed, graph_content_revision=1`；Milvus 有同 chunk → `vector_status=indexed, vector_content_revision=1`；否则 `pending`。
4. 输出 `rows_backfilled / rows_existing`，逐 kb 复核。

**备选：受控清库**。仅当 inventory 显示不可恢复错位（如 Neo4j chunk 无任何解析产物且 Milvus 无记录）时采用：复用 `reset_legacy_knowledge_data.py` 的 dry-run + 显式确认 token + 范围/数量报告路径，清库后提供"存量 chunk 数 = 0"的真实证据，再建图重建。

**验收证据**：CI/本地跑 backfill 前先跑 inventory；存量为空或 backfill 完成的行数作为 M5 验收前置证据。不做 backfill 也不清库直接上线 = 阻断。新增 chunk 走建图即建 revision 1（§5），不含在存量处理范围。

## 10. 人工编辑 / 重新解析 / 重新建图优先级

**权威顺序：人工内容 > 系统解析内容。** 系统操作绝不直接改写人工内容。

| 场景 | 规则 |
| --- | --- |
| 首次建图 | 创建 revision 1（`system_initial`）；投影按结果落 indexed/skipped/failed |
| 人工编辑（PATCH） | 新 `human_edit` revision，`content=编辑内容`；投影置 stale |
| 回滚 | 新 `rollback` revision，内容=目标 revision；投影置 stale |
| 重新解析/重新建图（当前行为 system） | 解析产物变化（`source_content_hash` 或 `parser_version` 不同）→ 创建新 `system_reparse` revision（新 source_content/content），投影置 stale；`source_content_hash` 与 `parser_version` 均未变且内容不变 → no-op（幂等）。**注意：幂等判断基于解析产出 `source_content_hash`，不基于 `content_hash`**（`content` 可被人工编辑，编辑不影响重新解析的幂等判定，v3.2 修正） |
| 重新解析/重新建图（当前行为 human/rollback） | **不创建新 revision、不改 content**；解析产物仅在 job result 记录 `SOURCE_CHANGED`；如需采用新解析文本，走显式人工确认动作（M6 提供） |
| 重复建图（幂等） | `source_content_hash`、`source_version`、`parser_version` 与投影状态均未变 → 不建 revision、不入队 |
| 重新抽取/重嵌入（reindex） | 只重建投影，不产生新 revision；以 current 行 content 为唯一内容源 |

`source_version`/`parser_version` 记在 revision 行上；`source_content_hash`（sha256(source_content)）支撑重新解析幂等判断，`content_hash`（sha256(content)）支撑重建/编辑幂等判断。`llm_disabled` 下 graph 投影 `skipped`，不伪装完整 indexed（§8.4）。

## 11. stale 期间 QA 语义（D1 补强）

D1"索引即权威"保留：stale 期间 QA 继续读旧索引文本，但**必须显式暴露版本差**，不让客户端误认为最新。

- citation snapshot 扩展为：

```text
{
  "id": chunk_id, "kb_id", "doc_id",
  "content_revision": indexed_content_revision,   // 索引内实际版本（旧值）
  "current_content_revision": current_revision,   // chunk_revisions 最新版本
  "is_latest": bool,                              // current == indexed
  "graph_status", "vector_status"
}
```

- 实现：retrieval_orchestrator 命中 chunk 后，按 `(kb_id, chunk_id)` 查一次 chunk_revisions 当前行，只取元信息（不 overlay 内容，保持 D1）；QA 响应与 trace snapshot 都带上述字段。
- 无 revision 行（理论上 backfill 后不存在）→ `current_content_revision=null`、`is_latest=null` + `MISSING_REVISION` 标记，如实呈现。

## 12. 测试与验收矩阵

| 维度 | 用例 | 期望 |
| --- | --- | --- |
| 并发 | 两 PATCH 同 expected_revision 并发 | 一胜一 409 且响应含 `current_revision` |
| 并发 | 并发读 revision 列表 | 一致、无脏读 |
| 跨 KB | PATCH/GET/rollback 用 A kb 的 chunk 打 B kb 的 kb_id | 404 `CHUNK_NOT_FOUND` |
| 作用域 | header/query/body 任一 kb_id 与路径不一致 | 400 `KB_CROSS_SCOPE`，拒绝且写审计 |
| 权限 | 无 `kb:write` 执行 PATCH/rollback/重建 | 403 |
| 内容 | 空 content / 超长 content | 400 |
| 版本 | 回滚后生成新 revision，历史行不变 | 版本号递增，`superseded` 行内容保持 |
| 投影 | 编辑后仅目标 chunk stale，同文档其他 chunk 不动 | 状态隔离成立 |
| 投影 | graph=indexed 而 vector=failed（embedding 失败） | 两状态独立，文档级聚合 failed |
| 投影 | llm_disabled / embedding 未配置 | `skipped`，文档级 stale，非 indexed |
| 任务幂等 | 重复提交相同 (kb,chunk,target) | 复用 job，不重复入队 |
| 旧任务保护 | revision 2 任务晚于 revision 3 执行 | `OUTDATED_SKIPPED`，不写索引 |
| worker 重试 | 重建中途失败 | 重试先复核 target/current；已完成则 no-op |
| stale QA | 编辑未 reindex 时提问 | citation 含 indexed=旧值、current=新值、`is_latest=false`、状态 stale |
| stale QA | reindex 完成后提问 | `is_latest=true`，版本一致 |
| backfill | 存量 kb 重复跑两遍 | 第二遍 `rows_backfilled=0`，`rows_existing=N` |
| Milvus | v3 抽样 search | `content_revision` 为 int 且等于当前值 |
| Milvus | 回滚切回 v2 | 计数与搜索正常，v2 未被 drop |
| 迁移 | chunk_revisions 迁移/回滚 | 幂等、`--dry-run`、rollback drop 后恢复 |

## 13. 决策表（评审结论落档）

| 决策 | 结论 | 本版落实 |
| --- | --- | --- |
| D1 索引即权威 | 有条件批准：补 stale 可见性 | §11 current/indexed + is_latest |
| D2 建图建 revision 1 | 批准：配套 backfill/清库与幂等重建 | §5、§9、§8.2 |
| D3 自动入队并 wake | 批准方向：事务化、幂等键、提交后 wake | §6 |
| D5 只重建 stale | 原写法不批准：取消空 chunk_ids 隐式语义 | §7、§8.1（显式范围） |
| D7 不新增 QA trace 列 | 批准：snapshot 补 current/indexed 语义 | §11 |

## 14. 迁移/回滚清单与实施顺序

迁移（均沿用既有 migrate 脚本模式：幂等、`--dry-run`、脱敏 DSN、`--action rollback`）：

1. `backend/admin/migrate_chunk_revisions.py` — 建 chunk_revisions 表（字段/索引/唯一约束见 §3）。
2. `backend/admin/migrate_jobs_targets_hash.py` — `admin_jobs` 增 `targets_hash VARCHAR(64) NULL` + 唯一索引（历史 NULL 行保留）。
3. `backend/admin/backfill_chunk_revisions.py` — 存量 revision 1（§9，先 inventory 后 backfill）。

代码回滚：表独立、检索链路不依赖 chunk_revisions；drop 表即回库，编辑端点随版本回滚失效；Milvus 回滚切回 `graphinsight_chunks_v2` 配置（§8.5），不 drop。

实施顺序（设计复审通过后）：

1. 跑 inventory，按证据定 backfill 或受控清库（§9）。
2. 实现迁移 + 索引 + backfill。
3. 实现 Go revision store、权限与乐观锁 API（§7）。
4. 实现事务化任务创建与幂等键（§6.1）。
5. 实现 Python 局部重建与 Neo4j/Milvus 独立投影状态（§8）。
6. 接通 QA citation 的 current/indexed revision 语义（§11）。
7. 执行双 KB、并发编辑、旧任务覆盖保护、部分失败恢复与 stale 查询验收（§12）。
8. 最后进入 M6 前端。

---

## 15. v3 增补修订（2026-10-01）

以下章节为 v3 新增/覆盖项。除特别说明外，v2 §1-§14 仍然有效；冲突处以本节为准。

### 15.1 current revision 部分唯一索引

在 §3 唯一约束 `UNIQUE (kb_id, chunk_id, content_revision)`（历史版本不可重复）之外，**新增数据库级部分唯一索引**，强制每个 `(kb_id, chunk_id)` 最多一个 `current` 行：

```sql
CREATE UNIQUE INDEX uq_chunk_revisions_current
  ON chunk_revisions (kb_id, chunk_id)
  WHERE revision_status = 'current';
```

规则：

- "每 chunk 只有一个 current 行"由数据库强制，不依赖应用层顺序；并发 PATCH 经 `FOR UPDATE` 锁行串行化后，第二个事务的 INSERT 会撞部分唯一索引 → 回滚并返回 409。
- 事务内语句顺序固定：先 `UPDATE` 旧行置 `superseded`，再 `INSERT` 新 `current` 行；顺序颠倒会在同事务内触发唯一冲突。
- 回滚、reparse 生成新版本同样遵守该顺序。
- 迁移/回滚：索引随表创建、随表 drop，无独立回滚面。

### 15.2 targets 多 chunk revision payload（覆盖 §7/§8 payload 形态）

重建任务的统一 payload 不再用"裸 `chunk_ids` 数组 + 隐式 revision"，改为显式 `targets` 数组，**每个元素携带 chunk_id 与 target_revision**：

```json
{
  "kb_id": "kb-01",
  "tenant_id": "t-01",
  "project_id": "p-01",
  "doc_id": "doc-01",
  "source": "chunk_edit",
  "targets": [
    { "chunk_id": "doc-01-000", "target_revision": 2 },
    { "chunk_id": "doc-01-001", "target_revision": 3 }
  ]
}
```

来源与填充规则：

- **编辑/回滚自动入队**：Go 在事务内写入新 revision 时生成 targets（快照当时 current 行的 `content_revision`），payload 落 `admin_jobs.payload`。
- **手动 `reindex-chunks`**：请求 `{kb_id, chunk_ids:[...]}`（非空、同 kb），服务端读取每个 chunk 当前 current 行生成 targets 快照返回给客户端，并以其落库；任一个无 current 行 → 404 `CHUNK_NOT_FOUND`，不部分执行。
- **`reindex-document`**：请求 `{kb_id, doc_id}`，服务端枚举该文档全部 `revision_status='current'` 且 `graph_status/vector_status != indexed` 的 chunk 生成 targets（v3.2：显式限定只枚举 current 行，superseded 历史行不参与枚举）。
- **约束**：一个 job 只允许一个 `kb_id`；`targets` 非空（空 → 400 `REINDEX_SCOPE_REQUIRED`）；target_revision 由服务端生成或校验，客户端不得随意指定（手动接口只给 chunk_ids）。

worker 消费语义不变（§8.2/§8.3）：每个 target 执行前/后复核 `target_revision == 当前 current.content_revision`，不等 → `OUTDATED_SKIPPED`。

### 15.3 backfill 对 Neo4j/Milvus 的真实补写（扩展 §9）

backfill 不止建 chunk_revisions 行，还必须把版本号真实补写到索引侧，否则"索引里是哪个版本"仍是空话。

步骤（脚本 `backend/admin/backfill_chunk_revisions.py`，全流程支持 `--dry-run`；**v3.2 口径：索引补写只作用于"无任何 revision 行的 chunk"**，已有 revision 行的 chunk 整体跳过，禁止把已有 current revision 降级为 1）：

1. **inventory（dry-run 报告）**：按 `--kb` 输出三类 chunk 集合（v3.2 收口）：
   - `new_chunks`：无任何 revision 行的存量 chunk → backfill 插 revision 1 + 补索引；
   - `needs_reindex_targets`：**已有 revision 行，且该投影能力已配置（embedding/LLM 未关闭），但外部投影缺失（Neo4j/Milvus 无该 chunk）或版本不一致（`graph_content_revision/vector_content_revision != current.content_revision`）** → backfill 不触碰索引侧版本，改为生成 current revision 的 reindex targets（reindex-chunks/reindex-document 语义，不降级为 1）；**能力未配置的投影（skipped、`*_content_revision IS NULL`）不归入本集合**，按步骤 7 的 skipped 放行并输出 `DEGRADED_SKIPPED`；
   - `converged`：已有 revision 行且外部投影版本一致 → 跳过。
   
   同时输出 Milvus 记录数、两者 overlap、三集合计数。任一 chunk 无解析产物且无向量 → 报告 `UNRECOVERABLE_MISMATCH`，要求走受控清库分支（§9 备选），backfill 终止。
2. **写 chunk_revisions revision 1 行**：`ON CONFLICT (kb_id, chunk_id, content_revision) DO NOTHING`，幂等；已有行的 chunk 不产生新行。
3. **补写 Neo4j**：仅对"本次新生成 revision 1 行的 chunk" `MERGE` 后 `SET c.content_revision = 1`（幂等）；统计 `neo4j_updated`。已有 revision 行的 chunk 不补写索引侧版本。
4. **补写 Milvus**：仅对本次新 backfill 的 chunk：读取 embedding 后 upsert 到目标 collection（v3，显式 `content_revision INT64 = 1`）；embedding 未配置 → 跳过，报告 `VECTOR_BACKFILL_SKIPPED`，该行 `vector_status=skipped`。
5. **投影状态落库**：仅对本次新 backfill 的 chunk：Neo4j 已补写 → `graph_status=indexed, graph_content_revision=1`；Milvus 已 upsert → `vector_status=indexed, vector_content_revision=1`；其余按能力跳过/失败。
6. **验证**：Neo4j 抽样 `content_revision=1`；Milvus 抽样 metadata `content_revision` 为 int 且 =1；输出 `rows_new/rows_existing/needs_reindex_targets/converged/neo4j_updated/milvus_upserted/skipped`。
7. **needs_reindex_targets 前置门（v3.2.1 收口）**：`needs_reindex_targets` 非空时，把该集合生成 current revision 的 reindex targets 入队（reindex-document/reindex-chunks），执行到全部收敛（按投影各自独立判定）**之后**，才允许关闭 backfill 前置门；未收敛不关闭。收敛判定：**indexed** 必须满足 `*_content_revision == current.content_revision`；**skipped** 必须满足"该投影能力未配置（embedding/LLM 关闭）且 `*_content_revision IS NULL`"——skipped 允许关闭技术迁移前置门，但必须输出 `DEGRADED_SKIPPED`，**不得宣布完整索引验收通过**；**failed/stale/pending 继续阻断前置门**。`converged` 集合不产生任务。
8. **重复执行**：全幂等，第二次 `rows_new=0`、`needs_reindex_targets=0`（已有 revision 行的 chunk 不再被触碰）。

backfill 时序：

```text
脚本(inventory)                PostgreSQL                Neo4j                Milvus v3
  |-- dry-run 统计 -------------->|                      |                    |
  |-- 读取 chunk 清单 <-----------|                      |                    |
  |-- INSERT revision 1 (conflict 跳过) ---------------->|                    |
  |-- MERGE SET content_revision=1 ---------------------------->|            |
  |-- upsert(text, content_revision=1) --------------------------------------->|
  |-- 落投影状态 --------------------------------------->|                    |
  |-- 抽样验证(Neo4j rev=1 / Milvus rev int=1) --------->|                    |
```

### 15.4 Milvus v3 写入、切换、回滚顺序（扩展 §8.5）

阶段化，任何时刻**只有一个 collection 被写入**；v2 永不 drop，切换前 v3 只是影子。

| 阶段 | 动作 | 验证 | 回滚点 |
| --- | --- | --- | --- |
| A 写入 | 新代码 `ensure_collection` 建 `graphinsight_chunks_v3`（显式 `content_revision INT64 NOT NULL` + 既有 kb 字段）；所有 upsert 写 v3 | v3 存在、字段类型正确 | 切回 v2 配置即可，v3 影子删除可选 |
| B backfill/重索引 | 按 kb 对 v3 执行 §15.3 backfill 或 reindex，写入正确 revision | 逐 kb `v3_count == v2_count`；抽样 metadata `content_revision` 为 int 且等于当前值 | 同 A |
| C 切换 | 配置 `vector_store.collection=v3`，重启，复验搜索 | search 返回字段/类型/数值正确；双 KB 检索隔离复验 | 切回 `v2` 配置重启，复验计数与搜索 |
| D 稳态 | v3 为唯一写入/读取目标；v2 保留只读备份 | 无 | 同 C |

不变量：

- 禁止自动 drop v2；禁止在 v2 上改 schema（延续 vector_store.py:122-137 既有模式）。
- 回滚顺序 = C → B → A 逆序执行：先切配置回 v2，再复验存量与隔离，v3 数据保留不删（供排查），确认后按需清理（清理也是人工动作，非自动）。
- 版本一致性：upsert 时 `content_revision` 写显式 INT64（int 类型），`chunk.metadata` 不得覆盖它（沿用"作用域以显式字段为准"模式）。

### 15.5 stale/skipped/failed 的重建与 retry 规则（扩展 §6.2/§8.2）

| 投影状态 | 含义 | 重建入口 | retry 规则 |
| --- | --- | --- | --- |
| stale | 内容已更新，索引落后 | reindex-chunks / reindex-document（自动或手动） | 每次重建可拾取；失败走 job retry |
| skipped | 能力未配置（llm/embedding） | 能力恢复后的 reindex | 不告警、不无限重试；配置变化后重新入队可转 indexed |
| failed | 上次执行失败 | 仅当 `target_revision == current` 且状态为 failed 的重建任务 | 沿用 admin_jobs `max_retries`（默认 3）+ 指数退避；重试前复核 target/current；连续超过 max_retries → job failed，投影保持 failed，文档级 failed，写审计；不自动无限重试 |

拾取规则（worker 对一个 job 的 targets）：

```text
仅处理 target_revision == 当前 current.content_revision 的 target；
且该 chunk 至少一个投影 != indexed（graph 或 vector）。
部分失败：graph 成功、vector 失败 → 各自独立状态，任务整体 failed；
重试只补未完成投影，已 indexed 的投影不再重做（幂等）。
```

补充：`skipped` 在文档级聚合计为 `stale`（§6.2）；失败优先级 `failed > stale/skipped > pending > indexed` 不变。

### 15.6 QA graph/vector 双投影版本语义（扩展 §11）

citation 从单版本扩展为双投影版本。文本内容仍读 Neo4j（D1 索引即权威），但版本与状态分别暴露：

```json
{
  "chunk_id": "doc-01-000",
  "kb_id": "kb-01",
  "doc_id": "doc-01",
  "content_revision": 2,
  "graph_status": "indexed",
  "graph_content_revision": 2,
  "vector_status": "stale",
  "vector_content_revision": 1,
  "current_content_revision": 3,
  "is_latest": false
}
```

语义定义：

- `content_revision` = Neo4j 中该 chunk 的版本（即 citation 文本来源版本）。
- `graph_content_revision` = Neo4j 投影实际版本；`vector_content_revision` = Milvus 投影实际版本；两者可不同（部分重建、部分失败）。
- `current_content_revision` = chunk_revisions 当前行版本。
- `is_latest = (graph_content_revision == current) && (vector_content_revision == current)`。任一投影落后 → `false`。
- 无 revision 行（backfill 后不应存在）→ 各字段 `null` + `MISSING_REVISION` 标记，如实呈现。
- QA 响应与 trace snapshot 同步携带上述字段（trace 复用 retrieval_snapshot，不加列，符合 D7）。

### 15.7 错误码、status/revision_status 契约同步（含差异表）

新增错误码（进入审计 §2.9 既有错误码表）：

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `CHUNK_NOT_FOUND` | 404 | chunk 不存在 / 不属于该 kb |
| `CHUNK_REVISION_CONFLICT` | 409 | expected_revision 与当前不符，响应带 `current_revision` |
| `CHUNK_CONTENT_EMPTY` | 400 | content 为空或超长 |
| `REINDEX_SCOPE_REQUIRED` | 400 | targets/chunk_ids 为空或跨 kb |
| `INDEX_UNAVAILABLE` | 503 | 索引迁移写冻结/降级期间写入口拒绝（v3.2 纳入统一错误码，§16.1） |

复用既有：`KB_SCOPE_REQUIRED`(400)、`KB_CROSS_SCOPE`(400)、`KB_ACCESS_DENIED`(403)、`KB_NOT_FOUND`(404)。

契约同步落点（本轮只冻结位置，不写代码）：

- `backend/core/exceptions.py`（或现有异常注册处）：注册上述错误码与 HTTP 映射。
- `backend/services/scope_contract.py` 的 `ChunkRevision` dataclass：字段扩展为 §3 全量（`revision_status/graph_status/vector_status/graph_content_revision/vector_content_revision/revision_source/source_version/parser_version`），与 Go 侧字段名逐字一致。
- `go-backend/internal/scope/scope.go`：同字段 + 错误码常量。
- `docs/ENTERPRISE_BACKEND_API_SPEC.md`：`/api/v1/admin/knowledge-bases/{kb_id}/chunks/{chunk_id}`、`/api/v1/admin/jobs/reindex-chunks`、`/api/v1/admin/jobs/reindex-document` 接口契约。

契约差异表（v2 冻结口径 → v3 修订口径）：

| 项 | v2 | v3 | 理由 |
| --- | --- | --- | --- |
| current 唯一性 | 应用层保证 | 数据库部分唯一索引 `(kb_id, chunk_id) WHERE revision_status='current'` | 并发安全由 DB 强制 |
| 重建 payload | `chunk_ids` + 隐式当前 revision | `targets:[{chunk_id, target_revision}]` | 显式目标版本，支持多 chunk 混合版本、防旧任务覆盖 |
| 手动接口入参 | `chunk_ids` | `chunk_ids`（服务端生成 targets 快照） | 客户端不猜 revision |
| backfill | 只建 revision 1 行 | 补写 Neo4j `content_revision` + Milvus v3 显式 INT64 | 索引侧版本真实可答 |
| Milvus | v3 迁移框架 | 阶段 A/B/C/D + 影子/回滚不变量 | 时序与回滚可执行 |
| 投影状态 | stale/skipped/failed 语义模糊 | 各自 retry/拾取/聚合规则明确 | 防止无限重试与伪装 indexed |
| QA 版本 | indexed/current 两个值 | + graph/vector 双投影版本与 `is_latest` | 部分重建时如实暴露 |
| 旧关系清理 | 未定义 | 按 chunk 删关系 + 孤立实体引用计数 | 防止编辑后旧图残留 |

### 15.8 旧图关系清理与失败恢复策略（扩展 §8.2）

编辑/重解析后实体与关系可能变化，重建时执行"先清旧、再写新"：

- **清理范围**：只删除该 chunk 专属关系（`relation.chunk_id == 目标`），实体节点保留（归一化实体可能被其他 chunk 引用）；删除后做孤立实体回收（引用计数为 0 且不属于任何 chunk 才删）。
- **写入顺序**：先删旧关系 → 抽取新实体/关系 → Neo4j 事务内写 Chunk.text + content_revision + 新关系；Neo4j 单事务失败整体回滚，不写半成品。
- **失败恢复**：
  - 抽取失败：不清理旧图（保留可服务旧内容），revision 侧保持 stale，下次重建全量重做。
  - Neo4j 写入中途失败：事务回滚，旧 text/旧关系保留。
  - Milvus 写入失败：不影响 Neo4j 已提交部分（投影独立），下次 retry 只补 vector 投影（§15.5）。
  - 任务连续失败超限：投影 failed，文档级 failed，写审计 `kb_chunk_reindex_failed`（含 chunk_id、target、错误摘要），不自动无限重试。
- **幂等**：清理与写入均按 (kb_id, chunk_id, content_revision) 判定，重试不会重复累积关系。

### 15.9 完整 reindex job JSON 示例（v3.1 修订）

自动入队（编辑触发，Go 事务内生成）：

```json
{
  "job_type": "reindex_chunks",
  "status": "pending",
  "tenant_id": "t-01",
  "project_id": "p-01",
  "kb_id": "kb-01",
  "doc_id": "doc-01",
  "targets_hash": "a1b2c3d4e5f6...（sha256(canonical_json(targets))）",
  "payload": {
    "kb_id": "kb-01",
    "tenant_id": "t-01",
    "project_id": "p-01",
    "doc_id": "doc-01",
    "source": "chunk_edit",
    "targets": [
      { "chunk_id": "doc-01-000", "target_revision": 2 }
    ]
  },
  "requested_by": 7,
  "trace_id": "tr-abc"
}
```

手动 reindex-chunks（多 chunk 混合版本）：

```json
{
  "job_type": "reindex_chunks",
  "status": "pending",
  "tenant_id": "t-01",
  "project_id": "p-01",
  "kb_id": "kb-01",
  "doc_id": "doc-01",
  "targets_hash": "b2c3d4e5f6a7...（sha256(canonical_json(targets))）",
  "payload": {
    "kb_id": "kb-01",
    "tenant_id": "t-01",
    "project_id": "p-01",
    "doc_id": "doc-01",
    "source": "manual",
    "targets": [
      { "chunk_id": "doc-01-000", "target_revision": 2 },
      { "chunk_id": "doc-01-001", "target_revision": 3 }
    ]
  }
}
```

reindex-document：

```json
{
  "job_type": "reindex_document",
  "status": "pending",
  "tenant_id": "t-01",
  "project_id": "p-01",
  "kb_id": "kb-01",
  "doc_id": "doc-01",
  "targets_hash": "c3d4e5f6a7b8...（sha256(canonical_json(targets))）",
  "payload": {
    "kb_id": "kb-01",
    "tenant_id": "t-01",
    "project_id": "p-01",
    "doc_id": "doc-01",
    "source": "manual",
    "targets": [
      { "chunk_id": "doc-01-000", "target_revision": 2 },
      { "chunk_id": "doc-01-003", "target_revision": 1 }
    ]
  }
}
```

注（v3.1 口径，取代旧 dedupe_key 说明）：

- `targets_hash = sha256(canonical_json(targets))`：canonical 指 targets 数组按 `(chunk_id, target_revision)` 字典序排序、key 排序、无多余空白；相同 targets 无论来源（编辑/手动/文档级）都得到同一 hash，唯一索引 `(job_type, kb_id, targets_hash)` 去重。
- job 顶层 `tenant_id / project_id / kb_id` 必须完整且与 payload 内完全一致（不一致 → 400 `KB_CROSS_SCOPE`）；`doc_id` 可空：仅当全部 targets 属于同一文档时填充（reindex-document 与编辑自动入队必填；手动 reindex-chunks 跨文档时置 null，payload 同样不填）。
- **worker 只处理 `target_revision == 当前 current.content_revision` 的 target**，其余 `OUTDATED_SKIPPED`（§8.2），不写索引。
- 不同 job 终态（pending/running/succeeded/failed）下的复用与 retry 规则见 §16.3。

### 15.10 更新后的验收矩阵（v3 增补行）

| 维度 | 用例 | 期望 |
| --- | --- | --- |
| 部分唯一索引 | 两并发 PATCH 同 expected | DB 层一胜一 409；任一时刻仅一个 current 行 |
| targets 混合 | 单 job 含 rev2/rev3 两个 chunk | 各自按 target 处理；rev2 若已过期 → OUTDATED_SKIPPED |
| backfill 补写 | 存量 kb 跑 backfill | Neo4j 抽样 `content_revision=1`；Milvus metadata `content_revision` int=1；重复跑 `rows_new=0` |
| Milvus 切换 | 阶段 A→C 全流程 | 切换前 v3 为影子、v2 正常；切换后搜索 revision 正确 |
| Milvus 回滚 | C 切回 v2 | 计数/隔离复验通过；v2 未被 drop |
| failed 重试 | graph 成功、vector 失败 | 投影独立状态；重试只补 vector |
| 旧关系清理 | 编辑后重抽取 | 旧关系消失、新关系出现；孤立实体按引用计数回收 |
| 双投影版本 | vector 落后于 graph | citation 暴露 `graph_content_revision != vector_content_revision`，`is_latest=false` |
| 契约同步 | 错误码/状态枚举 | Python/Go 两侧字段与错误码逐字一致；API spec 同步 |
| 幂等键 | 手动重复提交 | 复用既有 job，不重复入队 |

---

## 16. v3.1 增补修订（2026-10-01）

以下章节为 v3.1 新增/覆盖项。与 v2/v3 旧口径冲突处**一律以本节为准**；旧 §6/§7/§8 的 `dedupe_key`/裸 `chunk_ids` 口径已在正文直接改写为 `targets_hash`/`targets`，全文只保留一套规范。

### 16.1 Milvus 迁移期 R/W 状态机（冻结读写策略，覆盖 §15.4）

**冻结规则：任何时刻，读写必须指向同一 collection 族；禁止"v3 写入 + v2 继续作正常读源"的组合。** 迁移期采用"双写"为主路径、"写冻结"为降级手段。

| 状态 | 写目标 | 读目标 | 说明 | 进入条件 | 回滚到 |
| --- | --- | --- | --- | --- | --- |
| S0 现状 | v2 | v2 | v3 不存在 | 基线 | — |
| S1 双写 | v2 + v3 | v2 | v3 为影子收集增量；存量由 §15.3 backfill 对齐；v2 继续正常读写（读仍走 v2） | 部署含 v3 分支的新代码，`milvus.dual_write=true` | S0：关 dual_write，v3 影子保留不删 |
| S2 切换 | v3 | v3 | 配置 `vector_store.collection=v3` + 重启；v2 冻结为只读备份，不再接收任何写入 | S1 校验通过（逐 kb `v3_count==v2_count`、抽样 `content_revision` 为 int 且数值正确）后人工切换 | S1：切回 v2 写+读（dual_write 重开）或直接 S0 |
| S3 稳态 | v3 | v3 | 唯一读写目标；v2 保留备份不 drop | S2 复验通过后持续 | S2 |

时序：

```text
S0 ──部署 dual_write──▶ S1 ──backfill 存量 + 增量双写── 校验 ──▶ S2（切配置+重启）──复验──▶ S3
回滚（逆序）：S3 → S2 → S1 → S0
  S2→S1：切回 v2 写+读，复验 v2 计数与隔离；v3 数据保留（供排查）
  S1→S0：关 dual_write；v3 影子保留不删，确认后人工清理（清理也是人工动作，非自动）
```

不变量与禁止项：

- **禁止**"只写 v3 + 只读 v2"（双写期间 v2 同时写；切换后 v2 不写）。读源切换与写源切换必须同一点完成（S2 是原子切换点，配 `vector_store.collection` + 重启，不做运行期漂移）。
- 禁止自动 drop v2；禁止在 v2 上改 schema（延续 vector_store.py:122-137 既有模式）。
- 写冻结（降级）：迁移期若无法维持双写（如 v3 upsert 持续失败），回退 S0 并**冻结知识库写入口**（编辑/重建暂停，返回 503 `INDEX_UNAVAILABLE`），修复后重新走 S1，禁止在"v2 只读 + 新写入丢弃"的状态下继续运行。
- S1 双写的写入路径：编辑自动入队的重建任务与手动 reindex 的 upsert 同时写 v2/v3；backfill 只补存量，不做双写（存量在 S1 阶段一次性对齐）。
- 校验断言（纳入验收矩阵）：`isinstance(content_revision, int)`、数值等于 chunk 当前 revision；v3 与 v2 计数逐 kb 一致。

**S2（含）之后回滚到 v2 的数据一致性规则（v3.2 增补）：**

S2 切换后 v3 是唯一写源，v2 冻结在切换时点；S2 之后产生的新写入 v2 没有。**禁止不做任何处理直接切回 v2（会静默丢弃 S2 之后写入）**。回滚必须按序执行：

```text
1) 写冻结：暂停编辑/重建入口（返回 503 INDEX_UNAVAILABLE，见 §7 错误码），确保 v3 无新增写入
2) 回放：按 kb 对 v3 全量 upsert 到 v2（带当前 content_revision，幂等），把 S2 期间增量补回 v2
3) 校验：逐 kb v2_count == v3_count，抽样 search 的 content_revision 一致
4) 切回：配置 vector_store.collection=v2 + 重启；v3 数据保留不删（供排查），确认后人工清理
5) 解除写冻结，复验编辑/重建/双 kb 隔离
```

- 回放动作是幂等的（upsert 同 key 覆盖），中断可重跑；回放期间保持写冻结。
- S2 之前（S0/S1）的回滚不涉及此规则：S0→S0 无切换，S1→S0 关闭 dual_write 后 v2 数据完整（v3 影子不参与读）。

### 16.2 backfill 失败恢复（覆盖 §15.3）

backfill 按"全流程可重跑、任意阶段中断不产生坏状态"设计；每个阶段幂等，重跑补齐剩余、跳过已完成。

| 场景 | 处理 |
| --- | --- |
| 已有 revision 行 | **不插行、不补索引侧版本**（v3.2 修正——补写 `content_revision=1` 会把已有 current revision 降级）。分路：外部投影一致 → `converged`，跳过；**能力未配置的投影（skipped、`*_content_revision IS NULL`）→ 按 skipped 放行并输出 `DEGRADED_SKIPPED`（§15.3 步骤 7）**；**能力已配置但外部投影缺失（Neo4j/Milvus 无该 chunk）或版本不一致（`*_content_revision != current.content_revision`）→ 归入 `needs_reindex_targets`**，生成 current revision 的 reindex targets 收敛（§15.3 步骤 7），未收敛不得关闭 backfill 前置门 |
| Neo4j-only（存量只有 Neo4j chunk，Milvus 无记录） | 仅在无 revision 行时生效：`graph_status=indexed, graph_content_revision=1`；Milvus 侧补写：embedding 已配置 → upsert v3（`content_revision INT64=1`）并把 `vector_status=indexed`；embedding 未配置 → `vector_status=skipped`，报告 `VECTOR_BACKFILL_SKIPPED`（§15.3） |
| Milvus-only（存量只有向量，Neo4j 无 chunk 节点） | 仅在无 revision 行时生效：先补 Neo4j `MERGE (c:Chunk {kb_id, chunk_id})` + `SET c.text=解析产物, c.content_revision=1`（文本是 D1 索引权威源，graph_status=indexed）；Milvus 记录幂等保留，不重复 embedding 也无妨（upsert 同 key 覆盖，revision 置 1） |
| 内容不一致（Neo4j `Chunk.text` ≠ Milvus `text` ≠ 解析产物） | 无 revision 行：以解析产物 `chunks.jsonl` 为权威（M5 前存量无 revision 概念），revision 1 的 `source_content=content=解析产物文本`，Neo4j/Milvus 补写统一为解析产物文本 + `content_revision=1`，报告 `CONTENT_MISMATCH` 计数供人工复核，不静默选边。**已有 revision 行：不补写、不覆盖，索引侧版本由 reindex 收敛** |
| 中途失败（PG 插了一部分 / Neo4j 补了一部分 / Milvus 补了一部分） | 全流程可重跑：PG `DO NOTHING`、Neo4j `MERGE SET`、Milvus `upsert` 均幂等；失败点从脚本日志/计数恢复，重跑补齐剩余，不引入部分成功状态机 |
| inventory 显示不可恢复错位 | `UNRECOVERABLE_MISMATCH`（任一 chunk 无解析产物且无向量）→ 终止 backfill，走 §9 备选受控清库，需显式确认 token |

时序：

```text
0 inventory(dry-run): 分类 Neo4j-only / Milvus-only / OVERLAP / MISMATCH / 已有 revision
1 PG: INSERT ... ON CONFLICT DO NOTHING（幂等）
2 Neo4j: MERGE (c:Chunk {kb_id, chunk_id}) SET c.text=<解析产物>, c.content_revision=1（幂等）
3 Milvus v3: upsert(text=<解析产物>, content_revision INT64=1)（embedding 未配置 → skipped）
4 PG: 落投影状态（indexed → `*_content_revision=1`；skipped → `*_content_revision IS NULL`）
5 抽样验证 + 输出 rows_new/rows_existing/neo4j_updated/milvus_upserted/skipped/DEGRADED_SKIPPED
中断重跑：任意阶段失败 → 从步骤 1 重跑，已完成步骤幂等跳过
```

### 16.3 targets_hash 幂等：不同 job 终态下的复用与 retry（覆盖 §6.1/§8.3）

唯一索引 `(job_type, kb_id, targets_hash)` 只拦截"重复入队"；各终态语义：

| job 终态 | 同 `(job_type, kb_id, targets_hash)` 再次提交 | 说明 |
| --- | --- | --- |
| pending | **复用**：返回既有 job_id，不重复入队 | 尚未开始，直接复用 |
| running | **复用**：返回既有 job_id；worker 端幂等（重试先复核 target/current，已完成投影 no-op） | 并发提交同一任务不产生双执行 |
| succeeded | **复用**：返回既有 job_id 与 result，**不重入** | target 仍 current → 索引已最新，无需重建 |
| failed | **原地 retry**（v3.2 冻结）：同 `(job_type, kb_id, targets_hash)` 重提交时，若存在既有 failed 行 → 原地重置该行 `status='pending', retry_count=retry_count+1`，不新建 job；worker 复核 target/current 后只补未完成投影 | failed 代表上次未完成；同 targets_hash 重试是幂等补写，不产生重复索引写入 |
| cancelled | **原地重置**（v3.2 收口）：同 targets_hash 重提交时，若存在既有 cancelled 行 → 原地重置 `status='pending', retry_count=0`（人工取消不消耗重试配额），不新建 job | pending/running 可取消（§4.2）；取消不是失败，重试不累计 max_retries |
| 已过期（target 不再 current） | 新提交自然产生新 targets_hash（新 revision）；旧 job 保持原状态，worker 复核后 `OUTDATED_SKIPPED` 收尾，不写索引 | 旧 job 不重入、不重置 |

**targets_hash 唯一索引 DDL（v3.2.1 冻结，迁移脚本 `migrate_jobs_targets_hash.py` 执行）：**

```sql
ALTER TABLE admin_jobs ADD COLUMN targets_hash VARCHAR(64) NULL;
-- 部分唯一索引包含所有状态（v3.2.1 修正）：failed/cancelled 行同样占位唯一性，
-- 同 targets_hash 重提交一律原地 retry（不新建行），故无需排除 failed/cancelled；
-- 历史 NULL 行不参与唯一性
CREATE UNIQUE INDEX uq_admin_jobs_targets_hash
  ON admin_jobs (job_type, kb_id, targets_hash)
  WHERE targets_hash IS NOT NULL;
```

**job 状态转换（v3.2 收口，替换"实施时冻结"模糊表述）：**

```text
pending ──claim──▶ running ──完成──▶ succeeded
                 running ──失败──▶ failed
                 pending/running ──取消──▶ cancelled
                 failed ──同 targets_hash 重提交──▶ pending（原地重置，retry_count+1）
                 cancelled ──同 targets_hash 重提交──▶ pending（原地重置，retry_count=0）
                 failed ──retry_count >= max_retries──▶ 拒绝重置（409 JOB_409，人工干预）
```

**原地 retry/cancel 的并发规则（v3.2 收口）：**

- 重提交/重试事务内必须 `SELECT ... FOR UPDATE` 锁定既有 `(job_type, kb_id, targets_hash)` 行（按唯一索引定位），再按锁后状态分支处理：`failed/cancelled → 原地重置`；`pending → 复用`；`running → 返回 job_id（worker 幂等，不重复执行）`；`succeeded → 返回 result`。
- 两个并发同 targets_hash 重提交：第二个事务等锁后看到第一个已把行重置为 `pending`（或已 `succeeded`）→ 按新终态处理，不会双重置、不产生双 job。
- 新增行路径同样走唯一索引拦截；`ON CONFLICT (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL DO NOTHING` 兜底（v3.2.1：Postgres 部分唯一索引的 ON CONFLICT 需完整 WHERE 子句，谓词与索引谓词逐字一致，实施时冻结此写法）。同 targets_hash 的新增请求命中既有 failed/cancelled 行时同样被索引拦截 → 转入原地 retry 分支，不会新建行。

**max_retries 超限后的处理（v3.2 收口）：**

- `retry_count >= max_retries` 时，同 targets_hash 重提交**拒绝原地重置**：返回 409 `JOB_409`（复用 §7 既有"任务冲突"码，不新增错误码），message 说明"重试上限已用尽"，同时保持 failed 终态并写审计 `kb_chunk_reindex_failed`（含 chunk_id、target、错误摘要）。
- 超限后的恢复路径只有两条（人工决策）：手动 `reindex-chunks/reindex-document` 新建 job（新 targets_hash 不冲突），或按 §9 受控清库重建。**禁止自动无限重试**。
- succeeded 后内容再次变化 → 新 revision → 新 targets_hash → 新 job；旧 job 已被 `OUTDATED_SKIPPED` 收尾。
- 手动重复提交（相同 chunk_ids）与编辑自动入队（相同 targets）得到同一 targets_hash → 去重；不同 targets 即使 chunk 集合相同也视为不同 job（版本语义不同）。

### 16.4 QA `content_source` 与"分别最新 / 整体最新"语义（覆盖 §15.6/§11）

citation snapshot 增加 `content_source`，并显式拆出 `graph_latest` / `vector_latest` / `is_latest`：

```json
{
  "chunk_id": "doc-01-000",
  "kb_id": "kb-01",
  "doc_id": "doc-01",
  "content_source": "neo4j",
  "content_revision": 2,
  "graph_status": "indexed",
  "graph_content_revision": 2,
  "graph_latest": true,
  "vector_status": "stale",
  "vector_content_revision": 1,
  "vector_latest": false,
  "current_content_revision": 3,
  "is_latest": false
}
```

语义定义：

- `content_source`：当前 citation 文本实际来源，取值 `neo4j | milvus`（v3.2 删除 `chunk_revisions` 兜底）。D1 索引即权威 → 回退链固定为 **neo4j → milvus**；**两个索引都缺该 chunk 时，不返回内容、不降级到 chunk_revisions 取文本**（chunk_revisions 是内容版本记录，不是索引投影，禁止作为 citation 内容源），该 chunk 标记 `INDEX_MISSING` 并在 QA trace 中如实呈现。
- **分别最新**：`graph_latest = (graph_content_revision == current_content_revision)`；`vector_latest = (vector_content_revision == current_content_revision)`。两个投影各自独立判断。
- **整体最新**：`is_latest = graph_latest && vector_latest`。任一投影落后（stale/failed）或缺失（skipped）→ 整体非最新。
- **skipped 的诚实口径**：`skipped` 表示能力未配置（LLM/embedding），索引从未写入该投影 → `*_content_revision` 保持 NULL，`graph_latest/vector_latest` 判 `false`（NULL ≠ current）。这不代表"内容过期"，而是"该投影不可用"；文档级聚合仍按 §6.2 计为 stale。禁止把 skipped 伪装成 indexed 或 latest。
- 无 revision 行（backfill 后理论上不存在）→ 各版本字段 `null` + `MISSING_REVISION` 标记，如实呈现（§15.6 沿用）。
- QA 响应与 trace snapshot 同步携带上述字段（trace 复用 retrieval_snapshot，不加列，符合 D7）。

### 16.5 旧图清理必须覆盖 MENTIONS 边（覆盖 §15.8，给出实际 Cypher）

现状证据：`(:Chunk)-[:MENTIONS]->(:Entity)`（建边 document_graph_service.py:678，chunk 带 `kb_id/chunk_id/doc_id`）；Entity-Entity 关系（动态 apoc 与固定 RELATION 两条路径）均带 `r.kb_id` + `r.chunk_id`（document_graph_service.py:724-728、777）。

重写前清理（先清旧、再写新），实际 Cypher 范围：

```cypher
// 1) 删该 chunk 专属 MENTIONS 边（实体节点保留，归一化实体可能被其他 chunk 引用）
MATCH (c:Chunk {kb_id: $kb_id, chunk_id: $chunk_id})-[r:MENTIONS]->(:Entity)
DELETE r

// 2) 删该 chunk 专属 Entity-Entity 关系（动态 apoc 与固定 RELATION 都带 chunk_id 属性）
MATCH (:Entity)-[r]->(:Entity)
WHERE r.kb_id = $kb_id AND r.chunk_id = $chunk_id
DELETE r

// 3) 孤立实体回收：无任何 Chunk MENTIONS 指向、且无任何 Entity 关系的 document_ingest 实体
MATCH (e:Entity)
WHERE e.kb_id = $kb_id AND e.source = 'document_ingest'
  AND NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }
  AND NOT EXISTS { MATCH (e)-[r]-(:Entity) }
DETACH DELETE e
```

规则：

- 三步在同一 Neo4j 事务内执行，与抽取/写入新图同事务；任一失败整体回滚，旧图保留可服务（§15.8 失败恢复沿用）。
- `kb_id` 是隔离下限；不得出现不带 `kb_id` 的全局清理（跨 KB 隔离纪律 §4 延伸至图侧）。
- 步骤 3 只回收 `source='document_ingest'` 的实体；若未来有 `manual` 源实体（保留字段），不在回收范围。
- 幂等：清理与写入均按 `(kb_id, chunk_id, content_revision)` 判定，重试不会重复累积关系（§15.8）。
- 验收断言：编辑后重抽取，旧 MENTIONS 边消失、新边出现；孤立实体按引用计数回收；跨 KB 实体不被误删。

### 16.6 v3.1 实际修改的契约文件清单

本轮在文档修订之外，实际同步了下列契约文件（仅冻结契约，未写业务代码）：

| 文件 | 修改 | 验证 |
| --- | --- | --- |
| `backend/core/exceptions.py` | 新增 `CHUNK_REVISION_CONFLICT` / `CHUNK_NOT_FOUND` / `CHUNK_CONTENT_EMPTY` / `REINDEX_SCOPE_REQUIRED` 常量 + HTTP 映射（404/409/400/400）+ 消息 | `py_compile` 通过 |
| `backend/services/scope_contract.py` | `ChunkRevision` dataclass 扩展为 §3 全量字段（`revision_status/graph_status/vector_status/graph_content_revision/vector_content_revision/revision_source/source_version/parser_version/edited_by/edited_at/reason/trace_id`） | `py_compile` 通过 |
| `go-backend/internal/scope/scope.go` | 同字段 `ChunkRevision` struct + 3 错误码常量 + `httpStatusFor` 补 `CHUNK_NOT_FOUND→404` 分支 | `gofmt` + `go build ./internal/scope/` 通过 |
| `docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` | §2.6 ChunkRevision 字段更新 + §2.9 错误码新增 | 见该文件 §2.6/§2.9 |
| `docs/ENTERPRISE_BACKEND_API_SPEC.md` | 新增 M5 chunk/revision/rollback/reindex 端点契约 | 见该文件对应章节 |

说明：`migrate_jobs_targets_hash.py`、`backfill_chunk_revisions.py` 等迁移/脚本为**设计冻结名**（§14），不在本轮创建文件；实施顺序见 §14。

---

## 17. v3.2 增补修订（2026-10-01）

以下为 v3.2 新增/覆盖项。与 v2/v3/v3.1 旧口径冲突处**一律以本节为准**；旧章节（§3/§10/§15.2/§15.3/§15.7/§16.1/§16.2/§16.3/§16.4）的对应口径已直接改写，全文只保留一套规范。

### 17.1 backfill 禁止降级已有 current revision（覆盖 §16.2/§15.3）

**规则：backfill 的索引侧补写（Neo4j/Milvus 写 `content_revision=1`）只作用于"无任何 revision 行的 chunk"；已有 revision 行的 chunk 整体跳过，不插行、不补索引，禁止把已有 current revision 降级为 1。**

- 判定集合在 inventory 阶段确定：`new_chunks = 存量 chunk - 已有 revision 行 chunk`；后者的索引侧版本一致性由 reindex 任务按 current 收敛，backfill 不碰。
- 已按旧口径跑过的 backfill（把索引侧 revision 写成 1 的 chunk）：后续执行 reindex 按 current 收敛，backfill 本身不提供"反向修复"；若索引侧版本已确认错乱，走受控清库（§9 备选）重建。
- 验收：存量 kb 已有 revision 行时重复跑 backfill，`rows_new=0` 且索引侧 revision 不被改写（§17.3）。

### 17.2 reindex-document 只枚举 current（§15.2 同步）

**规则：`reindex-document` 服务端枚举条件 = `revision_status='current'` 且 `graph_status/vector_status != indexed`。** superseded 历史行不参与枚举；手动 `reindex-chunks` 的服务端 targets 快照同样只取 current 行（原有口径，此处明确）。

### 17.3 v3.2 验收矩阵（新增行）

| 维度 | 用例 | 期望 |
| --- | --- | --- |
| backfill 不降级 | 存量 chunk 已有 current revision（如 revision 3），重复跑 backfill | `rows_skipped_existing=N`、`rows_new=0`；索引侧 `content_revision` 仍为 3，不被改写为 1 |
| backfill 新 chunk | 无 revision 行的存量 chunk 跑 backfill | 插 revision 1 + Neo4j/Milvus 补写 `content_revision=1`；重复跑 `rows_new=0` |
| needs_reindex_targets 前置门 | 存量 chunk 已有 current revision 但能力已配置的投影缺失（Neo4j/Milvus 无该 chunk）或版本不一致（`*_content_revision != current.content_revision`） | inventory 归入 `needs_reindex_targets`（不归 new_chunks，不补写 revision 1）；生成 current revision 的 reindex targets 入队，收敛到 indexed 满足 `*_content_revision == current.content_revision` 后才允许关闭 backfill 前置门；未收敛不关闭，重复跑 `needs_reindex_targets=0` |
| reindex-document 枚举 | 文档含 superseded 历史行 + current 行 | 只枚举 current 行；superseded 行不生成 target |
| DEGRADED_SKIPPED | 存量 chunk 某投影能力未配置（embedding/LLM 关闭），`*_content_revision IS NULL` | 归 skipped 放行（不归 needs_reindex_targets）；允许关闭技术迁移前置门，但必须输出 `DEGRADED_SKIPPED`，不得宣布完整索引验收通过；failed/stale/pending 仍阻断前置门 |
| targets_hash DDL | 并发提交同 targets_hash；failed/cancelled 行已存在 | 唯一索引谓词 `WHERE targets_hash IS NOT NULL`（含 failed/cancelled 占位）；failed/cancelled 同 hash 重提交均原地重置（`failed→pending` retry_count+1 / `cancelled→pending` retry_count=0），不新建行 |
| targets_hash retry 超限 | 同 hash 重试超过 max_retries | job 保持 failed 终态 + 审计 `kb_chunk_reindex_failed` |
| S2 后回滚 | S2 期间产生新编辑，回滚到 v2 | 先写冻结→v3 回放 v2→校验 v2/v3 计数一致→切回；直接切回被禁止 |
| 写冻结降级 | 迁移期间写入口 | 返回 503 `INDEX_UNAVAILABLE`（统一错误码） |
| citation fallback | Neo4j 与 Milvus 都缺该 chunk | citation 不含该 chunk 文本，标记 `INDEX_MISSING`；不从 chunk_revisions 取文本 |
| source_content_hash | 解析产物变化（source 变、content 未人工改） | 新建 `system_reparse` revision；幂等判断基于 `source_content_hash` |
| source_content_hash | content 被人工编辑但 source 未变，重新解析 | 不建新 revision（解析产物未变），no-op |
| edited_at 口径 | ChunkRevision 序列化 | `edited_at` 恒非空（Python `str`、Go `string`、DB NOT NULL），无 null |

### 17.4 v3.2 实际修改的契约文件清单

| 文件 | 修改 | 验证 |
| --- | --- | --- |
| `backend/core/exceptions.py` | 新增 `INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"` 常量 + HTTP 映射 503 + 消息"索引当前不可用" | `py_compile` 通过 |
| `backend/services/scope_contract.py` | `ChunkRevision` 新增必填 `source_content_hash`（sha256(source_content)）；`edited_at` 由 `Optional[str]=None` 修正为 `str`（非空，DB NOT NULL 口径） | `py_compile` 通过 |
| `go-backend/internal/scope/scope.go` | `ChunkRevision` struct 新增 `SourceContentHash string`；`CodeIndexUnavailable` 常量；`httpStatusFor` 补 `INDEX_UNAVAILABLE→503` | `gofmt` + `go vet` + `go build ./internal/scope/` 通过 |
| `docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` | §2.6 ChunkRevision 加 `source_content_hash`；§2.9 加 `INDEX_UNAVAILABLE` | 见该文件 §2.6/§2.9 |
| `docs/ENTERPRISE_BACKEND_API_SPEC.md` | §7 错误码加 `INDEX_UNAVAILABLE`（503）；§3.3 reindex-document 枚举限定 current | 见该文件对应章节 |

说明：`migrate_jobs_targets_hash.py` 的 DDL 形态已在 §16.3 冻结（v3.2.1：部分唯一索引包含所有状态，谓词 `WHERE targets_hash IS NOT NULL`，不再排除 failed/cancelled），脚本仍为设计冻结名，不在本轮创建文件。