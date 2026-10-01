# 知识库一等公民：Phase 0 作用域契约与代码差距审计

更新时间：2026-09-27
状态：**决策冻结完成，M1/Phase 0 工程实施待开始**（决策点 D1–D5 已确认，见第 7 节）；本文件不是工程完成证明，差距审计基于当日代码实况（含行号证据）
分工约定：实现由工程师 / 其他 AI 按 M1–M6 执行；项目负责人只做分析与审查（审查清单见 §8.1），不直接写代码。
上游输入：

1. `docs/KNOWLEDGE_BASE_CATALOG_IMPLEMENTATION_PLAN.md`（知识库目录实施规划，本文件的阶段划分与其 Phase 0–4 对齐）
2. `study/knowledge-base-composition-blueprint.md` 第 17/18 节（腾讯 ADP P0、WeKnora P0 增量）
3. 2026-09-27 三端代码审计（Go / Python / 前端，证据见第 3 节）

本文回答两个问题：**契约冻结成什么样**（第 2 节）、**现有代码差在哪里、每个文件要改什么**（第 3、4 节）。实现按第 4 节里程碑 M1–M6 顺序推进。

---

## 1. 审计总结论

`kb_id` 目前只是三处孤立挂点：

1. `admin_jobs` 表的 `kb_id` 列（写入但 worker 从不读取，`backend/admin/models.py:165`、`backend/services/job_runtime.py:53`）
2. RBAC 绑定的 `scope_type=kb` + `kb_id` 列（`backend/admin/models.py:134-137`，Go 侧鉴权已消费，`go-backend/internal/adminstore/client.go:665-747`）
3. 请求头 `x-tenant-id / x-project-id / x-kb-id` 的透传管道（Go 解析 `authz_middleware.go:205-223`、转发 Python `orchestrator_handlers.go:356-373`、Python 有解析函数但从不用于检索 `backend/admin/api/deps.py:131-140`）

除此之外**全链路均为全局单库**：没有 KB 实体表；文档没有任何元数据表（列表=文件系统遍历）；`doc_id` 是文件绝对路径的 SHA-1 前 12 位（`documents_native.go:801-804`）；Neo4j 的 Document/Chunk/Entity/Relation 全部不带 `kb_id` 且**实体按全局 name MERGE（跨库必串图）**；Milvus 单 collection 无 kb 字段、检索 filter 恒为空；四条检索路径零文档过滤；`clear_kb` 全局抹除；QA trace 无 scope 列；前端"知识库治理"页实为"全局文档资产"列表。

初始审计结论（历史）：可以做，且挂点充足，但隔离必须落在 M3（存储与索引层）而不是页面下拉框；彼时没有 M1 实现、迁移或双 KB 黑盒验收证据，不能关闭 Phase 0，也不能切换 strict。

当前状态更新（2026-09-29）：M1-M4 已实现并通过真实统一活栈验收，双 KB 黑盒（`check_dual_kb_blackbox.py` passed=15 failed=0）与缺 scope 负向测试均已到位，调用方（前端/脚本/E2E）已全量显式携带 kb_id；strict 已正式启用（见 §2.11 D4）。

### 1.1 当前审计状态

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 竞品和架构研究 | 已完成 | 研究稿位于 `study/`，不等于代码交付 |
| D1-D5 决策冻结 | 已完成 | 仅表示契约口径已确认 |
| 正式契约文档 | 待纳入 Git 审计链 | 被忽略的本地文件不能作为可复核交付物 |
| M1/Phase 0 工程实施 | 已完成 | KB 实体表、迁移、ScopeResolver、scope helper 与隔离测试产物均已落地 |
| M1 验收 | 已完成 | 真实统一活栈：`check_dual_kb_blackbox.py` passed=15 failed=0、`check_kb_scope_isolation.py` passed=54 failed=0、Playwright E2E 5 passed / 3 skipped / PW_EXIT=0 |
| `KB_SCOPE_ENFORCE=strict` | 已正式启用 | 代码中不存在 strict/compat 开关或 default KB 兜底，KB 作用域强制无条件；由 `check_migration_cleanup_guards.py` 静态守卫防削弱（契约 §2.11 D4） |

---

## 2. 冻结契约（P0 不可变规则）

以下规则一经确认即为后续所有阶段的实现约束。改动任何一条都视为契约变更，需回到本文件修订。

### 2.1 作用域链与强制点

```text
Tenant -> Project -> KnowledgeBase -> Document -> Chunk
```

所有由文档导入的数据对象**必须**携带 `tenant_id / project_id / kb_id` 三元组。P0 阶段 tenant/project 仍为自由字符串（与现有 `admin_jobs` 设计一致），不建实体表；KB 是第一个一等实体。

| 层 | 必带字段 | 强制位置 | 现状 |
| --- | --- | --- | --- |
| 文档元数据（PG） | `kb_id, tenant_id, project_id` | `knowledge_base_documents` 表（新） | 无元数据表 |
| 文件存储 | `storage_prefix(kb_id)/relative_path` | 上传与读取路径解析 | 单一根目录 |
| 解析产物 | `parsed_documents/{kb_id}/{doc_id}/` | `document_graph_service.py` 产物写入 | `{doc_id}/` |
| Neo4j Document/Chunk | `kb_id, tenant_id, project_id` | 全部 MERGE/SET/DELETE/MATCH | 无 |
| Neo4j Entity | `entity_key, kb_id` | MERGE 键 | 全局 name MERGE |
| Neo4j Relation | `kb_id, doc_id, chunk_id` | 关系属性与删除条件 | 有 doc_id 无 kb_id |
| Milvus metadata | `kb_id, tenant_id, project_id, doc_id, chunk_id` | upsert 与 search filter（filter 为空即拒绝） | 无 kb 字段，filter 恒空 |
| 任务 payload | `kb_id` 必填 + `doc_ids` 显式列表 | 任务创建校验 + worker 执行 | 列存在但 worker 不读 |
| QA citation / trace | `kb_id, tenant_id, project_id, doc_id, content_revision` | trace 写入 | 无 scope 列 |
| 审计事件 | `tenant_id, project_id, kb_id` | `admin_logs` | 仅 `tenant_id` 列 |

任何一层遗漏 scope 都视为阻断上线缺陷，不是后续优化项。

**作用域字符串格式（已确认的 D5 规格）**：`tenant_id / project_id / kb_id` 统一为 `^[a-z0-9][a-z0-9_-]{1,99}$`（小写字母或数字开头，总长 2–100）。三端入口（Go ScopeResolver、Python ScopeResolver、KB CRUD）必须做 trim + 小写归一 + 格式校验，非法值返回 `SCOPE_INVALID`；不通过校验的 scope 不得进入任何存储、索引或日志查询。已有索引要求：`admin_jobs`、`admin_user_role_bindings`、`admin_logs`、`admin_qa_traces`、`knowledge_bases`、`knowledge_base_documents` 的 scope 列全部带索引（M1 迁移覆盖）。**实体化边界**：出现以下任一情况时启动 tenant/project 实体表建设——(a) 需要租户级配额或计费；(b) 需要租户级 SSO / 成员体系；(c) 接入第二个真实外部租户。实体化时新增 `tenants/projects` 表并把字符串列升级为受校验外键；本项目按全新知识库初始化，不通过 default KB 兼容旧数据，本契约的作用域算法不变。

### 2.2 实体与关系键

```text
entity_key   = sha256(kb_id + normalized_name + entity_type) 的稳定截断
relation_key = sha256(kb_id + subject_entity_key + predicate + object_entity_key + evidence_chunk_id)
```

Neo4j `Entity` 的 MERGE 键从全局 `name` 改为 `{entity_key, kb_id}`；`name` 属性保留用于展示与全文匹配。禁止跨 `kb_id` 合并同名实体。

### 2.3 doc_id 策略

- 新上传文档：Go 生成**服务端稳定 doc_id（UUID）**，与文件路径解耦，记录在 `knowledge_base_documents`。
- 不提供旧文档 doc_id 兼容映射；如果初始化前发现旧文档，按一次性知识数据清理流程处理。
- 新 `doc_id` 不由路径推导；文件移动/改名不改变 doc_id、chunk_id 与图谱证据地址。

### 2.4 SearchTarget 与有效检索范围

```json
{ "kb_ids": ["kb-001"], "folder_ids": [], "tag_ids": [], "document_ids": [] }
```

P0 只实现 `kb_ids`；`folder_ids / tag_ids` 为保留字段（对应腾讯 ADP P0 的标签硬过滤，后续阶段启用）。`document_ids` 允许（现有 doc_ids 语义收编）。

有效范围计算链（冻结）：

```text
effective_kb_ids
  = user_authorized_kb_ids        （服务端从绑定解析，不信客户端）
  ∩ api_key.allowed_kb_ids        （机器调用时）
  ∩ application.allowed_kb_ids    （应用消费时，后续阶段）
  ∩ request.kb_ids                （客户端请求范围）
```

规则：

1. 请求未携带任何 `kb_id/kb_ids` → `KB_SCOPE_REQUIRED`；请求已指定 KB 但与授权范围求交为空 → `KB_ACCESS_DENIED`。不允许通过请求级 default KB 兜底补齐 scope。
2. header / query / body 同时携带 scope 且不一致 → `KB_CROSS_SCOPE`，不允许静默取其一。
3. 无 scope 的检索请求不得默认查询全库——这是第 7 节测试的通过标准。
4. worker 只消费任务创建时批准并写入 payload 的 scope，不从用户 token 重新推断。

### 2.5 KBGrant（授权授予）

逻辑契约：

```json
{ "subject_type": "user|api_key|application", "subject_id": "...", "kb_id": "...",
  "role": "viewer|editor|admin", "capabilities": ["kb:read","kb:write","kb:review","kb:manage","kb:delete","kb:publish"] }
```

实现映射：**复用现有 `admin_user_role_bindings`（scope_type=kb）**，P0 不新建表。Go 鉴权在请求开始时解析出已验证的 grant 集合，handler 只消费解析结果。`kb:review / kb:manage / kb:publish` 三个新权限码注册进权限种子，P0 只启用 `kb:read/write/delete` 的强制校验，其余为预留。

### 2.6 ChunkRevision（M5 实现，契约现在冻结，v3.1 字段集）

```text
chunk_revisions: { revision_id, kb_id, tenant_id, project_id, doc_id, chunk_id,
                   source_content, source_content_hash, content, content_hash, content_revision,
                   revision_status, graph_status, vector_status,
                   graph_content_revision, vector_content_revision,
                   revision_source, source_version, parser_version,
                   edited_by, edited_at, reason, trace_id }
```

规则：`source_content` 为解析产出，不可变；`source_content_hash` = sha256(source_content)，重新解析幂等判断基于它（不基于可被人工编辑的 `content_hash`，v3.2）；`content` 为当前可编辑内容；同一 `content_revision` 并发编辑 → `CHUNK_REVISION_CONFLICT`（409，响应带 `current_revision`）；revision 历史不可变；`revision_status` 取值 `current|superseded`，每 `(kb_id, chunk_id)` 至多一个 current 行（数据库部分唯一索引强制，见 M5 设计 §15.1）；编辑后受影响 chunk 的 `graph_status / vector_status` 置 `stale`，只重建 stale 索引；`graph_status / vector_status` 取值 `pending|stale|indexed|skipped|failed`，`skipped` 表示能力未配置（LLM/embedding 关闭），**不伪装 indexed**；`graph_content_revision / vector_content_revision` 记录投影实际对应版本，stale 期间保留旧值。完整字段语义、唯一约束与状态机见 `docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` §3/§15/§16/§17。

### 2.7 PipelineRun / StepRun（契约冻结，M5 起落库）

```json
{ "run_id": "...", "task_type": "ingest_document", "tenant_id": "...", "project_id": "...", "kb_id": "...",
  "document_ids": [], "pipeline_version": "ingestion-v1", "status": "running",
  "steps": [ { "name": "parse|chunk|extract|graph_write|vector_write", "status": "...",
               "output_count": 0, "error_summary": "", "retry_count": 0, "duration_ms": 0,
               "provider_version": "", "trace_id": "..." } ] }
```

任务中心仍是唯一调度/审计 owner；PipelineRun/StepRun 是任务执行结构（先落在 `admin_jobs.payload/result` 的规范化结构内，M5 评估是否独立建表）。

### 2.8 API Key（契约冻结，实现排在 M6 后）

```json
{ "key_id": "...", "name": "...", "key_hash": "...", "tenant_id": "...", "project_id": "...",
  "allowed_kb_ids": [], "capabilities": ["chat","ingest",...], "status": "active", "expires_at": null }
```

`allowed_kb_ids` 与 `capabilities` 分开；有效范围永远与用户授权求交。当前系统完全没有 API key 认证（审计确认），本契约只冻结形状。

### 2.9 统一错误码

两个后端使用相同字符串 code（映射进现有统一响应体 `code/message/data/timestamp/trace_id`；落地时对齐 Go 与 Python 各自现有错误类型的构造方式）：

```text
KB_SCOPE_REQUIRED        缺少 kb 作用域（始终拒绝）
KB_NOT_FOUND             kb_id 不存在
KB_ACCESS_DENIED         有效范围为空 / 越权
KB_ARCHIVED              目标知识库已归档，禁止写入
KB_CROSS_SCOPE           header/query/body 作用域不一致
KB_DUPLICATE_NAME        同 project 内知识库重名
KB_STORAGE_PATH_INVALID  存储前缀非法或路径逃逸
SCOPE_INVALID            tenant_id/project_id/kb_id 格式非法（见 §2.1 作用域格式）
CHUNK_REVISION_CONFLICT  Chunk 并发编辑冲突（M5，409，响应带 current_revision）
CHUNK_NOT_FOUND          chunk 不存在 / 不属于该 kb（M5，404）
CHUNK_CONTENT_EMPTY      content 为空或超长（M5，400）
REINDEX_SCOPE_REQUIRED   重建目标缺失或跨知识库（M5，400）
INDEX_UNAVAILABLE        索引迁移写冻结/降级期间写入口拒绝（M5，503）
```

### 2.10 审计字段与事件

所有 KB 作用域写操作写 `admin_logs`，字段 `operator_id, tenant_id, project_id, kb_id, trace_id, action`。首批事件名：`kb_created / kb_updated / kb_archived / kb_deleted / document_uploaded / document_deleted / document_restored / kb_cleared`。拒绝路径（`KB_ACCESS_DENIED / KB_CROSS_SCOPE`）同样写审计。

### 2.11 强制 scope 与发布门槛

- P0 起所有新旧接口统一采用 strict 规则：缺少 `kb_id/kb_ids` 返回 `KB_SCOPE_REQUIRED`；不保留请求级 default KB fallback，也不设置 legacy endpoint allowlist。
- `KB_SCOPE_ENFORCE` 如保留为配置项，只接受 `strict`；`compat` 是非法或废弃配置，不能通过配置重新开启隐式 scope。
- **strict 发布门槛（已确认的 D4 口径）**：正式启用前必须同时满足以下两条，与时间无关——(1) §6.2 两个 KB 的隔离黑盒场景全部通过；(2) 已知调用方（前端、脚本、内部任务、E2E）全部改为显式携带 `kb_id`，并通过缺 scope 的负向测试。
- **启用状态（2026-09-29）**：D4 两条均已满足——双 KB 黑盒 `check_dual_kb_blackbox.py` passed=15 failed=0，作用域隔离 `check_kb_scope_isolation.py` passed=54 failed=0，真实栈 Playwright E2E PW_EXIT=0；全仓静态审计确认代码中无 `KB_SCOPE_ENFORCE`/`KB_DEFAULT_ID`/default KB 兜底。strict 正式启用，并由 `check_migration_cleanup_guards.py::test_kb_scope_strict_mode_has_no_compat_toggle` 静态守卫防止后续削弱（开关重现 / fail-closed 锚点丢失 / 缺 scope 兜底 / 前端 X-KB-ID 丢失均会触发）。
- 本项目不创建 `default KB`，也不提供旧数据自动迁移或请求级 fallback。
- 如果部署环境发现旧的全局文档、解析产物、Neo4j 图谱或 Milvus 向量，必须先执行一次明确范围的知识数据清理；不得把旧数据静默挂到新 KB。清理只允许作用于知识数据，不得删除管理员、权限、配置等无关数据。
- 任何运行时请求都必须携带并校验真实 `kb_id/kb_ids`；新 KB 由 M2 CRUD 显式创建。

### 2.12 明确禁止

1. 路由层直接堆 SQL / 直接解析 PDF / 直接写 Neo4j、Milvus。
2. worker 全局扫描文档目录；worker 只处理 payload 显式列出的文档。
3. 无 `kb_id` 的 Neo4j / Milvus / 全文检索查询。
4. 客户端传入的 scope 扩大授权范围；scope 头不与服务端授权求交即采信。
5. 用全局清库冒充"清空知识库"；跨库操作仅靠前端禁用按钮。

---

## 3. 差距审计（现状证据 → 目标）

### 3.1 PostgreSQL 数据模型（DDL 归属 Python 侧，Go 无 DDL、无版本机制——保持此约定）

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| 9 张表，无 KB 实体 | `backend/admin/models.py:19-207` | 新增 `knowledge_bases`、`knowledge_base_documents` |
| `AdminUserRoleBinding` 已有 scope 三列 + 唯一约束 | `models.py:127-153` | 可直接承载 KBGrant ✅ |
| `AdminJob` 已有 scope 三列但 worker 不读 | `models.py:156-179`；`job_runtime.py:53` 只收 payload 文本 | worker 读 scope 并强制 |
| `AdminQATrace` 无 scope/doc/版本列，出处只在 JSON 快照里 | `models.py:182-207` | 增列（M4） |
| `AdminLog` 仅 `tenant_id` | `models.py:69-86` | 增 `project_id, kb_id` |
| 迁移=独立 argparse 脚本，无 runner | `backend/admin/migrate_*.py` × 10 | 沿用既有模式新增脚本 |

### 3.2 文档生命周期（Go）

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| 列表=文件系统遍历，无元数据 | `documents_native.go:583-617` | 以 `knowledge_base_documents` 为列表权威 |
| doc_id=绝对路径 SHA1 前 12 位；改名即换 id | `documents_native.go:801-804` | 新文档 UUID（2.3） |
| 上传无 KB 概念，存储单根 `DOCUMENT_STORAGE_PATH` | `documents_native.go:106-155, 821-881` | 解析 KB → 存储前缀 |
| 软删除/恢复/清空全部全局 | `documents_native.go:157-334, 477-581, 336-475` | 按 KB 过滤；清空改 KB 级 |
| 列表返回服务器绝对路径 | `documents_native.go` list item | 改相对路径（顺带安全修复） |

### 3.3 Neo4j（Python 写入 + Go 删除）

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| Document/Chunk 无 kb_id；Entity 全局 name MERGE；`entity_name` 全局唯一约束 | `document_graph_service.py:439-537, 529, 1756-1786` | 全部写入带 scope；entity_key 键（2.2） |
| 关系删除按 `doc_id`、孤儿清理按 `source` | `document_graph_service.py:484-491, 918-941` | 条件加 `kb_id` |
| `clear_document_graph` / `_clear_parsed_document_artifacts` 全局抹除 | `document_graph_service.py:706-714, 771-822, 1083-1094` | 改 KB 级，全局仅限显式运维任务 |
| Go 侧删图按 doc_id 已可用 | `graph/service.go:275-392` | 加 kb 校验（doc_id 属于该 KB 才可删） |
| `/api/query` 为原始 Cypher 透传，零 scope 防护 | `graph/service.go:505-560` | 见决策点 D1 |

### 3.4 Milvus

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| 单 collection `graphinsight_chunks`，schema 无 kb 字段 | `vector_store.py:43, 99-109` | schema 增 `kb_id/tenant_id/project_id`（新 collection 直接带 kb schema，见 §5.4） |
| search filter 恒为空串，全库检索 | `vector_store.py:181-206`；`retrieval_orchestrator.py:304` | filter 强制 `kb_id IN ...` |
| delete 按 doc_id；clear=drop collection | `vector_store.py:160-179` | 均加 kb 条件；禁无作用域 drop |

### 3.5 检索与问答（Python）

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| `retrieve(question, top_k)` 无 scope 参数；keyword/vector/hybrid/graph_hybrid 四路全部无文档过滤 | `retrieval_orchestrator.py:73-137, 241-374` | 签名增 scope；四路全部过滤 |
| DocQA 请求模型无 kb 字段 | `api/routes/doc_qa.py:49-66` | 增 `kb_id / kb_ids` |
| deep_research、retrieval-diagnostics、nl2cypher 同样无 scope | `doc_qa_internal.py:29-100`；`nl2cypher.py:13-17` | 同上 |
| Python 已有 `resolve_request_scope` 但检索从未使用 | `admin/api/deps.py:131-140` | 接入 internal 入口 |

### 3.6 任务中心

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| `clear_kb` 全局删除文档目录+图谱+drop Milvus | `job_runtime.py:130-159` | 改为 KB 级；全局清空仅限显式运维任务 |
| `build_graph` 扫描全局目录 | `job_runtime.py:53-127`；`document_graph_service.py:943-953` | 输入=Go 解析后的 `doc_ids` 列表 |
| `admin_jobs.kb_id` 从不参与执行 | `job_service.py:456-460` | payload 必含 scope，worker 校验 |

### 3.7 前端

| 现状 | 证据 | 差距 |
| --- | --- | --- |
| 知识库页=全局文档资产，无 KB 选择 | `KnowledgeBasePage.tsx:277`（"全局文档资产、回收站与高风险清理操作"） | 升级为目录+详情页 |
| `documents.ts / docQa.ts / graphBuild.ts` 无 kb 参数 | 三文件全文 | 全部增 scope |
| 无 knowledgeBase 类型/服务 | `src/types/`、`src/services/` | 新增（M6） |
| JobsPage 以自由文本填 scope 三元组 | `JobsPage.tsx:206-254` | 改为 KB 选择器；危险操作文案改 KB 级 |

---

## 4. 里程碑与逐文件实施清单

> 标注：**新增**=新文件；**修改**=改现有文件。每个里程碑末尾给出验收门。M1–M4 对应用户"第一条后端闭环"，M5=Chunk 版本，M6=UI 最后做。

### M1 契约落地与全新初始化（对应规划 Phase 0）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `backend/admin/models.py` | 修改 | 增 `KnowledgeBase`、`KnowledgeBaseDocument` 模型（字段=规划 §4.1/4.2 + 蓝图 §3.4 的 `graph_status/vector_status`）；`AdminLog` 增 `project_id/kb_id`；`AdminQATrace` 增 `tenant_id/project_id/kb_id`（`doc_version` 留 M5） |
| `backend/admin/migrate_knowledge_base_tables.py` | 新增 | 建两表 + 唯一约束 `(tenant_id, project_id, name)` + 索引；幂等、支持 `--dry-run`、含回滚 action（沿用 `migrate_*.py` 模式） |
| `backend/admin/migrate_audit_scope_columns.py` | 新增 | `admin_logs.project_id/kb_id`、`admin_qa_traces.tenant_id/project_id/kb_id` + 索引 |
| `backend/admin/reset_legacy_knowledge_data.py` | 新增 | 一次性初始化前清理旧的全局文档、解析产物、Neo4j 图谱和 Milvus 向量；必须支持 `--dry-run`、显式确认 token、范围/数量报告；不得删除 admin 用户、权限、配置等无关数据 |
| `backend/core/errors.py`（或现有异常体系所在文件） | 修改 | 注册 §2.9 错误码与 HTTP 映射 |
| `backend/admin/api/deps.py` | 修改 | `resolve_request_scope` 增强为 ScopeResolver：header/query/body 一致性校验 → `KB_CROSS_SCOPE`；输出 `SearchTarget` |
| `backend/services/scope_contract.py` | 新增 | Python 侧冻结契约类型（`SearchTarget`、`KBGrant`、`ChunkRevision`、`PipelineRun/StepRun` dataclass）+ Neo4j/Milvus scope helper（`kb_filter_expr(kb_ids)` 等），供 M3/M4 复用 |
| `go-backend/internal/scope/scope.go` | 新增 | Go 侧同一套契约类型 + 解析器（header/query 解析、body 一致性由各 handler 校验）、`EffectiveKBs()` 交集算法 |
| `go-backend/internal/config/config.go` | 修改 | 采用 strict scope 规则；不得保留 `KB_DEFAULT_ID` 或任何 default KB fallback 配置 |
| `go-backend/internal/middleware`（或 `authz_middleware.go`） | 修改 | 拒绝路径（`KB_ACCESS_DENIED/KB_CROSS_SCOPE`）写审计 |

**验收门**：新库迁移可幂等执行并回滚；初始化前旧知识数据清理必须先 dry-run、确认并输出范围/数量报告；不得创建 default KB；`py_compile`、`go build ./...`、`go test ./...` 通过；契约类型在两侧字段名完全一致；scope 缺失、不一致、越权和无 scope 全库查询均有负向测试。没有这些真实命令输出和测试结果，M1 不能关闭。

### M2 Go 控制面 KB CRUD（规划 Phase 1）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `go-backend/internal/adminstore/knowledge_bases.go` | 新增 | repository：CRUD、分页、状态过滤、`tenant+project+name` 唯一、归档/恢复事务；统计（文档/Chunk 计数走延迟查询） |
| `go-backend/internal/adminstore/client.go` | 修改 | 注册新 repo、连接复用 |
| `go-backend/internal/httpserver/admin_knowledge_bases_native.go` | 新增 | 路由 handler：`GET/POST /api/v1/admin/knowledge-bases`、`GET/PATCH/DELETE .../{kb_id}`、`POST .../{kb_id}/archive`、`POST .../{kb_id}/restore`；权限 `kb:read/kb:write/kb:delete`；写审计事件 |
| `go-backend/internal/httpserver/handlers.go` | 修改 | 注册路由（控制面段） |
| `go-backend/internal/rbac_seed.go`（rbac_seed.go） | 修改 | 注册 `kb:review/kb:manage/kb:publish` 权限码（预留，不强制） |
| `go-backend/internal/adminstore/logs.go` | 修改 | 审计写入带 `project_id/kb_id` |

**验收门**：CRUD + 唯一性 + 分页契约测试；无 `kb:write` 用户创建 KB 被拒并留审计。

### M3 文档、Neo4j、Milvus、任务隔离（规划 Phase 2，"第一条闭环"核心）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `go-backend/internal/httpserver/documents_native.go` | 修改 | 上传：解析 KB（active 校验）→ `storage_prefix` 目录 → 计算 SHA-256 → 写注册表行（UUID doc_id）→ 幂等去重；列表/删除/恢复：按注册表 + `kb_id` 过滤；清空：改 KB 级（全局清空移到显式运维任务）；列表不再返回绝对路径 |
| `go-backend/internal/httpserver/admin_jobs_native.go` | 修改 | 任务创建强制 `kb_id` 且校验 KB 存在/active；`clear-kb` payload 必含 `kb_id`；worker 交付的 payload 固化 scope + `doc_ids` |
| `go-backend/internal/graph/service.go` | 修改 | `DeleteDocumentGraph/ClearDocumentGraph` 增 kb 校验（doc_id 不属于目标 KB 拒绝）；`PreviewDeleteDocumentGraph` 同步 |
| `backend/services/document_graph_service.py` | 修改 | 全部新写入带 `tenant/project/kb`；Entity MERGE 改 `{entity_key, kb_id}`；约束 `entity_name` 改复合唯一；`delete/clear` 按 KB；产物路径 `{parsed_root}/{kb_id}/{doc_id}/`；`build_graph` 输入改为显式 `doc_ids`，不再扫描目录 |
| `backend/services/vector_store.py` | 修改 | schema 增 `kb_id/tenant_id/project_id` 字段；`search` 强制 filter；`delete_doc` 加 kb；`clear` 改为按 kb 过滤删除 |
| `backend/services/retrieval_orchestrator.py` | 修改 | `index_chunks/delete_doc/clear` 带 scope |
| `backend/services/job_runtime.py` | 修改 | worker 读 payload 的 `kb_id`（缺失即失败）；`clear_kb` 只清目标 KB；`build_graph` 只处理 payload `doc_ids` |
| `backend/admin/services/job_service.py` | 修改 | 创建任务校验 KB（读注册表）；`execute_job` 传递 scope |
| `backend/tests/check_kb_scope_isolation.py` | 新增 | 两 KB 黑盒隔离测试（§6 场景 A） |

**验收门**：§6.2 场景 1/2/4/6 通过（A 列表不见 B 文件；A 建图不增 B 节点；删 A 不动 B；同名实体两套节点）。

### M4 问答与检索隔离（规划 Phase 3，闭环收口）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `go-backend/internal/httpserver/docqa_native.go` | 修改 | 解析 `SearchTarget`（body `kb_id/kb_ids` + header 一致性）→ 与授权求交 → 注入 `x-kb-id/x-kb-ids` 转发头；空交集 → `KB_ACCESS_DENIED` |
| `go-backend/internal/httpserver/nl2cypher_native.go` | 修改 | 同上；转发头带授权 kb 集合 |
| `go-backend/internal/httpserver/orchestrator_handlers.go` | 修改 | `buildForwardHeaders` 增 `x-kb-ids` |
| `backend/api/routes/doc_qa.py` | 修改 | `DocQARequest/DeepResearchRequest/RetrievalDiagnosticsRequest` 增 `kb_id/kb_ids` |
| `backend/api/routes/doc_qa_internal.py`、`nl2cypher_internal.py` | 修改 | internal 入口调用 ScopeResolver；缺 scope 返回 `KB_SCOPE_REQUIRED` |
| `backend/services/doc_qa_service.py` | 修改 | `answer/deep_research` 接收并下传 scope |
| `backend/services/retrieval_orchestrator.py` | 修改 | `retrieve()` 签名增 scope；keyword（Cypher 加 `kb_id IN $`）、vector（Milvus filter）、graph 扩展（Cypher 起点+扩展均带 kb）、rerank 候选窗口四路全部过滤 |
| `backend/services/qa_trace_runtime.py` | 修改 | trace 记录 `tenant/project/kb/doc_id`（citation 继承检索结果的 kb） |
| `backend/services/nl2cypher_service.py` | 修改 | 生成的 Cypher 注入 `kb_id IN $authorized_kb_ids`；禁止写操作与 APOC（对齐蓝图 §5.5） |
| `go-backend/internal/graph/service.go`（`ExecuteQuery`） | 修改 | 决策点 D1 的落地（见 §7） |
| `backend/tests/check_kb_retrieval_isolation.py` | 新增 | §6.2 场景 3（A 提问不引用 B Chunk）+ 无 scope 检索被拒 |

**验收门**：四条检索路径在 KB-A 均召回不到 KB-B 内容；无 scope 请求在 strict 下 `KB_SCOPE_REQUIRED`；QA trace 含 kb。

### M5 Chunk 版本与索引状态（用户步骤 4）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `backend/admin/models.py` + 新迁移脚本 | 修改/新增 | `chunk_revisions` 表（§2.6）；`knowledge_base_documents` 增 `graph_status/vector_status` 落地使用 |
| `go-backend/internal/adminstore/chunk_revisions.go` | 新增 | revision 读写 + 乐观锁（`content_revision` 比对 → `CHUNK_REVISION_CONFLICT`） |
| `go-backend/internal/httpserver/admin_kb_chunks_native.go` | 新增 | `GET .../chunks/{chunk_id}`、`PATCH .../chunks/{chunk_id}`（带 expected_revision）、`POST .../revisions/{id}/rollback` |
| `backend/services/document_graph_service.py` | 修改 | 局部重索引：单 chunk 重抽取/重嵌入；编辑后 `graph_status/vector_status=stale` |
| `backend/services/job_runtime.py` | 修改 | 新任务类型 `reindex_chunks`（限定 `kb_id + chunk_ids`） |
| `backend/services/qa_trace_runtime.py` | 修改 | trace 记录 `content_revision` |

**验收门**：§6.2 补充——并发编辑一胜一冲突；回滚生成新 revision；编辑后仅 stale 索引重建；QA trace 可答"用的哪个版本"。

### M6 前端目录与工作台切换（规划 Phase 4，最后做）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `frontend/src/types/knowledgeBase.ts` | 新增 | KB/文档/revision/SearchTarget 类型 |
| `frontend/src/services/knowledgeBases.ts` | 新增 | KB CRUD + KB 内文档/任务/检索 API 客户端 |
| `frontend/src/pages/Admin/KnowledgeBasesPage.tsx` | 新增 | 知识库目录（列表/创建/归档/统计卡） |
| `frontend/src/pages/Admin/KnowledgeBaseDetailPage.tsx` | 新增 | 详情：文档列表、任务、索引状态、检索配置 |
| `frontend/src/pages/Admin/KnowledgeBasePage.tsx` | 修改 | 重定向到知识库目录 `/admin/knowledge-bases`；移除"全局清库"语义文案 |
| `frontend/src/components/knowledge-base/KnowledgeScopeSelector.tsx` | 新增 | 工作台/后台共用的 KB 选择器（只显示授权范围） |
| `frontend/src/components/ChatPanel/DocChatPanel.tsx` | 修改 | 上传携带当前 kb；问答 body 带 kb |
| `frontend/src/components/Workspace/DocumentPanel.tsx` | 修改 | 列表按 KB 过滤；切换 KB 清空旧列表与引用 |
| `frontend/src/services/documents.ts`、`docQa.ts`、`graphBuild.ts` | 修改 | 全部请求带 `kb_id` |
| `frontend/src/services/adminService.ts` | 修改 | jobs API 强制 scope；新增 KB 端点 |
| `frontend/src/pages/Admin/JobsPage.tsx` | 修改 | 自由文本 scope 改选择器；"高风险全局操作"文案改 KB 级 |
| `frontend/src/App.tsx` | 修改 | 路由：`/admin/knowledge-bases`、`/admin/knowledge-bases/:kbId`；旧页兼容跳转 |
| `frontend/tests/`（e2e 脚本） | 修改 | 两 KB 切换隔离用例 |

**验收门**：§6.2 场景 7（前端切换 A/B，文档、任务、问答、统计同步切换）+ loading/empty/error/success/archived/indexing 状态齐全。

---

## 5. 全新初始化与回滚

1. **PG**：`migrate_knowledge_base_tables.py` / `migrate_audit_scope_columns.py`，均幂等、`--dry-run`、带回滚 action；新库不创建 default KB。
2. **旧知识数据清理**：`reset_legacy_knowledge_data.py` 仅作为全新初始化前的一次性运维动作，先 dry-run，再使用显式确认 token 执行；清理文档、解析产物、图谱和向量并报告数量，不迁移、不生成注册表映射，不删除 admin 用户、权限或配置。
3. **Neo4j**：新写入直接携带 `kb_id` 和 `entity_key`；不做旧图谱 backfill。初始化前若存在旧图谱，只能由一次性清理流程按范围删除。
4. **Milvus**：新 collection 直接使用带 `kb_id` 的 schema；不把旧 collection 重建或混入新 KB。初始化前若存在旧向量，只能由一次性清理流程按范围删除。
5. **清理类操作**：一次性初始化清理必须支持 dry-run、显式确认、范围/数量报告和失败停止；运行时文件、图谱、向量、产物各自记录状态，路由内禁止不可恢复的全量长操作。

---

## 6. 测试与验收

### 6.1 交付门槛（每个里程碑都必须跑）

```bash
backend/.venv/bin/python -m py_compile <changed_python_files>
go test ./...
cd frontend && npm run build
backend/.venv/bin/python backend/tests/run_unified_boundary_guards.py
backend/.venv/bin/python backend/tests/run_backend_smoke_suite.py --include authz --include documents --include jobs_api --include qa_traces
ADMIN_TOKEN=*** ./frontend/tests/run_admin_e2e.sh   # M6 起
```

### 6.2 两 KB 隔离黑盒场景（唯一完成标准）

准备 KB-A、KB-B，各上传一份含**同名实体**但内容不同的文档，分别建图+向量化：

1. A 的文档列表不出现 B 的文件。
2. A 建图不增加 B 的节点、关系、向量。
3. 在 A 提问不引用 B 的 Chunk。
4. 删除/清空 A 不改变 B 的文件、图谱、向量、QA trace。
5. 只有 A 权限的用户不能读/改/清 B（含 API 直连绕过前端的尝试）。
6. A、B 同名实体保持两套作用域内节点。
7. 前端切换 A/B 后文档、任务、问答、统计同步切换（M6）。
8. 服务重启后目录、索引、任务状态可恢复；重复建图不产生重复关系。

只有以上全部通过才能把路线图条目标记完成；单纯 CRUD 通过不算知识库完成。

---

## 7. 决策记录（2026-09-27 已全部确认）

| # | 决策 | 确认结论 |
| --- | --- | --- |
| D1 | `/api/query` 原始 Cypher 透传（`graph/service.go:505-560`）无法可靠注入 kb 过滤 | **已确认**：原始 Cypher 查询只保留给管理员，必须审计；普通工作台的图谱读取（含 `documentGraphSync`）迁移到带 scope 的专用查询接口。M4 落地 |
| D2 | 新文档 doc_id 策略 | **已确认**：新上传使用 UUID；不提供旧文档 path-hash 兼容映射（§2.3） |
| D3 | Milvus collection 策略 | **已确认**：单 collection + `kb_id` filter；新 collection 直接使用带 kb 的 schema，不混入旧向量（§5.4） |
| D4 | strict 发布门槛 | **已确认（选择 A）**：所有新旧调用方显式携带 `kb_id` 后，且隔离测试与缺 scope 负向测试通过，才允许正式启用 strict；全新初始化不保留 default KB 或请求级兼容 fallback（§2.11） |
| D5 | tenant/project 实体化 | **已确认（附加要求）**：暂用字符串可接受，但必须有统一格式、索引、校验和实体化边界（规格见 §2.1 末段，错误码 `SCOPE_INVALID` 见 §2.9） |

---

## 8. 交接约束

实现方（AI 或工程师）必须遵守 `docs/KNOWLEDGE_BASE_CATALOG_IMPLEMENTATION_PLAN.md` §9 的固定约束与 `study/knowledge-base-composition-blueprint.md` §11 的任务模板，并追加本文两条硬规则：

1. 契约以本文第 2 节为准；与规划文档冲突时，以本文（更细、更新）为准并回写修订记录。
2. 每个里程碑交付时返回：修改文件清单、迁移与回滚方法、API 契约、隔离证据（对应 §6.2 场景编号）、真实测试输出、未完成项——不得把计划写成已完成。

完成 M1–M4 后同步更新 `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` 与 `docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md` 的知识库条目状态。

### 8.1 交付审查清单（项目负责人审查口径，2026-09-27 确认）

每个里程碑的提交（代码、diff、迁移脚本、测试结果、实施报告）按以下清单审查，任何一条不通过即打回：

1. **隔离真实性**：是否真正实现了 KB 隔离，而不是页面下拉框或"全部映射 default"的假隔离。
2. **泄露面**：是否存在跨租户或跨 KB 数据泄露（含错误信息、日志、统计计数、路径回显等间接泄露）。
3. **遗漏层**：Neo4j 写入/查询、Milvus upsert/search/delete、任务 payload 与 worker、QA Trace 与 citation 是否每一层都带 scope；对照 §2.1 强制点表逐层核对。
4. **初始化与回滚**：迁移脚本是否幂等、可 dry-run、有真实回滚验证；旧知识数据清理范围和非目标数据保护是否明确。
5. **权限与审计**：写操作是否全部有权限校验、审计字段（operator/tenant/project/kb/trace_id）、拒绝路径审计；scope 头是否与服务端授权求交而非直接采信。
6. **测试覆盖**：是否覆盖 §6.2 两个 KB 的黑盒隔离场景；无 scope 请求在 strict 模式下是否被拒。
7. **诚实报告**：是否把计划误报成已完成；未完成项、未运行的检查是否如实列出。

**最终完成标准（不变）**：两个知识库的文档、图谱、向量、任务、检索结果和引用全部隔离，并通过黑盒验收。
