# GraphInsight 知识库目录与多知识库实施规划

更新时间：2026-09-27  
状态：**决策冻结完成，M1/Phase 0 工程实施待开始**；本文是实施规划，不代表功能已经完成
适用对象：负责后端、前端、数据库迁移和验收的 AI agent 或开发者

## 1. 目标和结论

GraphInsight 已经具备文档解析、结构化 Chunk、实体/关系抽取、Neo4j 图谱、Milvus 向量索引、混合检索、DocQA、Deep Research、任务中心和知识库治理页面。

当前缺少的是一个真正的一等知识库资源模型。现状中的“知识库”主要是全局文档目录和治理页面：

```text
DOCUMENT_STORAGE_PATH
  -> DocumentGraphService
  -> Document / Chunk / Entity / Relation
  -> Neo4j + 可选 Milvus
  -> DocQA / Deep Research
```

本规划要把它升级为：

```text
Tenant
  -> Project
    -> KnowledgeBase
      -> Document / Version / Source
        -> Parsed Artifact / Chunk
          -> Entity / Relation / Vector
      -> Build / Reindex / Clear Jobs
      -> Retrieval Profile / Parser Profile
      -> Audit / Quality Metrics
```

本阶段不重写 GraphRAG 核心，不先做大规模外部连接器，而是先完成“多知识库目录 + 全链路隔离 + 可运营治理”。

## 2. 当前事实基线

实现时必须以代码为准，不把旧规划中的接口当成已实现功能：

1. Go 已经原生处理文档上传、列表、软删除、恢复和清空，入口是 `/api/documents*`。
2. 前端已有 `/admin/knowledge-base` 页面，但当前页面标题是“全局文档资产”，说明它管理的是全局文档集合，不是可切换的知识库目录。
3. 当前文档存储以 `DOCUMENT_STORAGE_PATH` 为主，解析产物以 `PARSED_DOCUMENT_STORAGE_PATH/{doc_id}/` 为主。
4. Neo4j 已使用 `Document`、`Chunk`、`Entity`、`RELATION`；Milvus 向量索引是可选能力。
5. 权限和任务对象已经有 `tenant_id`、`project_id`、`kb_id` 字段，但文档、图谱和向量主链路还没有统一绑定 `kb_id`。
6. 现有 API 规范已经预留 `/api/v1/admin/knowledge-bases`，但当前代码没有完整的知识库 CRUD 和目录页面实现。

相关现有代码和文档：

- `go-backend/internal/httpserver/documents_native.go`
- `backend/services/document_graph_service.py`
- `backend/services/retrieval_orchestrator.py`
- `frontend/src/pages/Admin/KnowledgeBasePage.tsx`
- `docs/ENTERPRISE_BACKEND_API_SPEC.md`
- `docs/KNOWLEDGE_DISCOVERY_PIPELINE_DESIGN.md`

## 3. 范围边界

### 3.1 本阶段必须完成

1. KnowledgeBase 目录资源的创建、查询、修改、归档和删除。
2. 文档与 KnowledgeBase 的明确归属。
3. 文件、解析产物、Neo4j、Milvus、任务和问答全部按 `kb_id` 隔离。
4. 知识库级权限校验、审计字段和 `trace_id`。
5. 前端知识库目录、知识库切换、文档列表和治理操作。
6. 按全新知识库初始化，不迁移旧全局文档；如果部署环境存在旧知识数据，先执行受控清理，不作为新 KB 的数据来源。
7. 两个以上知识库之间的隔离验收和回归测试。

### 3.2 本阶段明确不做

1. 不一次性实现 Glean、Dify 的全部连接器和 Agent 市场。
2. 不改变 Neo4j、Milvus、文档解析器和 QA Trace 的核心协议，只增加作用域字段。
3. 不把二进制文件复制到 PostgreSQL；数据库只保存元数据，文件仍由文档存储目录管理。
4. 不允许通过“把所有请求都映射到 default KB”来伪造多知识库隔离；运行时请求不得隐式补齐 scope，缺少 `kb_id` 必须失败。
5. 不在路由中直接堆 SQL；知识库数据访问放到现有 Go admin store/service 边界。

## 4. 目标领域模型

### 4.1 KnowledgeBase

建议放在 Go 控制面使用的 PostgreSQL admin 数据库中。

| 字段 | 类型 | 要求 |
| --- | --- | --- |
| `id` | string/UUID | 主键，稳定且不可复用 |
| `tenant_id` | string | 必填 |
| `project_id` | string | 必填 |
| `name` | string | 必填，同一 project 内唯一 |
| `slug` | string | 可选，展示和 URL 使用 |
| `description` | string | 可选 |
| `status` | enum | `active`、`archived`、`deleting` |
| `storage_prefix` | string | 逻辑前缀，不允许逃逸根目录 |
| `parser_profile` | JSON | 解析器和解析模式 |
| `retrieval_profile` | JSON | 检索模式、top_k、rerank 等 |
| `created_by` / `updated_by` | string | 审计主体 |
| `created_at` / `updated_at` | timestamp | 必填 |
| `archived_at` | timestamp | 可空 |
| `metadata` | JSON | 仅放非敏感扩展字段 |

约束：

1. `tenant_id + project_id + name` 唯一。
2. 删除采用归档或异步清理，不允许直接丢失文档和图谱。
3. 不创建 `default` 知识库；所有 KB 必须由 M2 CRUD 显式创建。

### 4.2 KnowledgeBaseDocument

当前文件系统仍然是二进制内容的权威存储，但必须新增文档元数据表或等价的持久化层。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `doc_id` | string | 新上传文档使用服务端 UUID，不承担旧文档兼容 |
| `kb_id` | string | 必填 |
| `tenant_id` / `project_id` | string | 与知识库一致 |
| `name` / `relative_path` | string | 原文件名和安全相对路径 |
| `source_type` | enum | `upload`、`url`、`connector`、`import` |
| `source_uri` | string | 脱敏或可公开的来源标识 |
| `mime_type` / `size` | string/int | 文件信息 |
| `sha256` | string | 去重和幂等依据 |
| `version` | int | 文档版本号 |
| `status` | enum | `uploaded`、`parsing`、`indexed`、`failed`、`archived` |
| `parser_provider` / `parser_version` | string | 解析运行信息 |
| `graph_status` / `vector_status` | enum | 分别记录图谱和向量索引状态 |
| `error_summary` | string | 安全截断的错误摘要 |
| `created_by` / `updated_by` | string | 审计主体 |
| `created_at` / `updated_at` | timestamp | 必填 |

推荐约束：`kb_id + sha256 + version` 唯一；文件路径只能是 `storage_prefix` 下的相对路径。

### 4.3 图谱和向量作用域

所有由文档导入的图谱和索引对象都必须可以按 `kb_id` 过滤：

```text
Neo4j Document: doc_id, kb_id, tenant_id, project_id
Neo4j Chunk: chunk_id, doc_id, kb_id
Neo4j Entity: entity_key, kb_id, name
Neo4j Relation: doc_id, chunk_id, kb_id, evidence
Milvus metadata: kb_id, doc_id, chunk_id
QA citation: kb_id, doc_id, chunk_id
```

特别注意：当前实体使用名称做全局 `MERGE`。新写入必须使用知识库作用域内的稳定 `entity_key`，否则两个知识库中的同名实体会串图。

## 5. API 规划

所有响应继续遵守项目统一结构：`code`、`message`、`data`、`timestamp`、`trace_id`。所有写操作必须鉴权、审计和可追踪。

### 5.1 知识库目录管理

```text
GET    /api/v1/admin/knowledge-bases?tenant_id=&project_id=&status=
POST   /api/v1/admin/knowledge-bases
GET    /api/v1/admin/knowledge-bases/{kb_id}
PATCH  /api/v1/admin/knowledge-bases/{kb_id}
DELETE /api/v1/admin/knowledge-bases/{kb_id}
```

创建请求示例：

```json
{
  "tenant_id": "tenant-a",
  "project_id": "project-a",
  "name": "农业试验知识库",
  "description": "农业试验论文和技术报告",
  "parser_profile": {"provider": "native", "parse_mode": "auto"},
  "retrieval_profile": {"mode": "graph_hybrid", "top_k": 8}
}
```

权限建议：

- 列表/详情：`kb:read`
- 创建/修改：`kb:write`
- 归档/删除：`kb:delete`

### 5.2 文档和建图作用域

保留已有业务入口，但新增显式知识库作用域：

```text
GET  /api/documents?kb_id={kb_id}
POST /api/documents/upload?kb_id={kb_id}
POST /api/graph/build?kb_id={kb_id}
POST /api/docqa             body.kb_id
POST /api/docqa/deep-research body.kb_id
```

`x-tenant-id`、`x-project-id`、`x-kb-id` 可以作为统一作用域头，但 body/query 与 header 同时存在时必须校验一致，不能静默选择其中一个。

初始化策略（选择 A，不保留请求级兼容 fallback）：

1. 所有新旧请求都必须显式携带 `kb_id/kb_ids`；缺少 scope 统一返回 `400 KB_SCOPE_REQUIRED`。
2. 所有前端、脚本、内部任务、worker 和 E2E/smoke 调用方必须在同一迁移批次内完成改造。
3. 新库不创建 `default KB`，也不为旧文档生成注册表映射；部署前发现旧知识数据时，执行显式确认的清理流程。
4. 不允许用一个全局目录同时承载多个知识库；文件路径必须带知识库存储前缀。

### 5.3 任务中心

继续复用已有任务接口，但 `kb_id` 必须进入任务唯一性和执行上下文：

```text
POST /api/v1/admin/jobs/build-graph
POST /api/v1/admin/jobs/clear-kb
POST /api/v1/admin/jobs/reindex
GET  /api/v1/admin/jobs?kb_id={kb_id}
```

任务 payload 必须包含：`kb_id`、`doc_ids`（可选）、`parser_provider`、`reasoning_profile`、`force`。worker 不得从全局目录扫描其他知识库。

## 6. 后端实施顺序

### Phase 0：契约冻结和全新初始化准备（决策完成，工程待实施）

1. 新增数据库迁移：`knowledge_bases`、`knowledge_base_documents`、必要索引和回滚脚本。
2. 按空知识库初始化；不得创建 `default KB` 或跨 project 的全局单例。
3. 如果发现旧全局知识数据，先执行 `reset_legacy_knowledge_data.py --dry-run`，确认后清理并报告数量；不得把旧文档挂到新 KB。
4. 增加 `KB_SCOPE_REQUIRED`、`KB_NOT_FOUND`、`KB_ACCESS_DENIED`、`KB_ARCHIVED`、`KB_CROSS_SCOPE` 错误类型。
5. 增加作用域解析器：统一读取 header/query/body，并拒绝不一致。

### Phase 1：Go 控制面知识库 CRUD

1. 在 `go-backend/internal/adminstore` 增加 repository、类型和事务方法。
2. 在 Go control plane 增加知识库 CRUD 路由。
3. 接入 `kb:read/write/delete` 和租户/项目层级校验。
4. 所有写操作写审计事件：`kb_created`、`kb_updated`、`kb_archived`、`kb_deleted`。
5. 增加分页、状态过滤和项目过滤，禁止把所有知识库一次性无分页返回。

### Phase 2：文档存储和索引隔离

1. 上传时先解析并校验 `kb_id`，再计算知识库安全存储目录。
2. 文档列表、删除、恢复、清空全部添加 `kb_id` 过滤。
3. 建图时把 `kb_id` 传入 Python worker 和 `DocumentGraphService`。
4. Neo4j 写入和删除语句全部增加 `kb_id` 条件。
5. 新写入实体使用知识库内 `entity_key`；不提供旧图谱实体 backfill。
6. Milvus upsert、search、delete、clear 全部增加 `kb_id` filter；禁止无作用域的全库 clear 作为普通操作。
7. 解析产物路径改为 `parsed_documents/{kb_id}/{doc_id}/` 或等价安全目录。

### Phase 3：问答和检索隔离

1. DocQA、Deep Research、retrieval diagnostics 请求必须带 `kb_id`。
2. keyword、vector、graph、rerank 每个召回源都必须按 `kb_id` 过滤。
3. 引用、QA trace、检索 snapshot 保存 `kb_id`。
4. QA 失败时返回可定位的 scope 信息，但不泄露其他知识库名称或文档。
5. 默认检索模式继续尊重现有配置，不在本阶段强制把所有实例切到 graph_hybrid。

### Phase 4：前端知识库目录和切换

新增 `frontend/src/services/knowledgeBases.ts`、`frontend/src/types/knowledgeBase.ts`，不要在组件里硬编码接口。

页面建议：

1. `/admin/knowledge-bases`：知识库目录、创建、编辑、归档、统计卡片。
2. `/admin/knowledge-bases/:kbId`：知识库详情、文档、索引状态、任务、检索配置。
3. 现有 `/admin/knowledge-base`：改为重定向到知识库目录 `/admin/knowledge-bases`，不再展示“全局清库”语义。
4. 工作台上传和问答前必须有当前知识库选择状态。
5. loading、empty、error、success、archived、indexing 六种状态都要有明确 UI。

危险操作：

- 归档前显示文档数、图谱数、向量数和正在运行的任务。
- 清空必须先 dry-run，再二次确认；默认软删除，可恢复。
- 跨知识库操作必须直接阻断，不能仅靠前端禁用按钮。

## 7. 测试和验收

### 7.1 后端单元和契约测试

1. KnowledgeBase CRUD 的唯一性、状态和分页。
2. 租户/项目/知识库三级权限越权测试。
3. header/query/body 作用域不一致测试。
4. 文档上传路径穿越和跨知识库路径访问测试。
5. 实体同名但不同 `kb_id` 不得合并。
6. Neo4j、Milvus、QA citation 的所有读取都必须带作用域。
7. 删除 KB 的 dry-run、软删除、恢复和异步任务失败回滚。
8. 所有无 `kb_id` 请求都必须失败并返回 `KB_SCOPE_REQUIRED`；不得通过 legacy endpoint、兼容开关或 default KB 绕过 scope 校验。

### 7.2 黑盒验收场景

准备 `KB-A` 和 `KB-B`，各上传一份包含同名实体但内容不同的文档：

1. A 的文档列表不出现 B 的文件。
2. A 建图不会增加 B 的节点、关系或向量。
3. 在 A 提问不能引用 B 的 Chunk。
4. 删除/清空 A 不改变 B 的文件、图谱、向量和 QA trace。
5. 只有 A 权限的用户不能读取、修改或清理 B。
6. A 的同名实体和 B 的同名实体保持两套作用域内节点。
7. 前端切换 A/B 后，文档、任务、问答和统计全部同步切换。
8. 服务重启后目录、索引和任务状态仍可恢复，重复建图不产生重复关系。

### 7.3 交付门槛

```text
backend/.venv/bin/python -m py_compile <changed_python_files>
go test ./...
cd frontend && npm run build
backend/.venv/bin/python backend/tests/run_unified_boundary_guards.py
backend/.venv/bin/python backend/tests/run_backend_smoke_suite.py --include authz --include documents --include jobs_api --include qa_traces
frontend/tests/run_admin_e2e.sh
```

只有“两个知识库隔离黑盒场景”通过，才能把本项标为完成；单纯 CRUD 通过不等于知识库完成。

## 8. 风险和回滚

1. **发现旧知识数据**：新系统不做归属推断或迁移；先 dry-run 统计文件、解析产物、Neo4j 节点/关系和 Milvus 向量，使用显式确认 token 后定向清理。管理员、权限、配置和其他非知识数据不得删除。
2. **Neo4j 新建约束失败**：初始化阶段先验证新约束和 scope 字段，失败则停止；不对旧图谱做 backfill。
3. **Milvus schema 初始化失败**：新 collection 初始化失败则停止；不混入旧 collection，也不把旧向量迁移到新 KB。
4. **旧客户端不传 `kb_id`**：本项目不提供兼容 fallback；调用方必须在发布前改造，缺失 scope 的请求明确失败并返回 `KB_SCOPE_REQUIRED`。
5. **清理操作中断**：文件、图谱、向量分别记录状态，任务可重试；禁止在路由中做不可恢复的长时间全量操作。

## 9. 交给其他 AI 的执行说明

将以下内容作为实现任务的固定约束。实现方必须先阅读完整输入集，不能只读取三份研究/规划文件：

1. `AGENTS.md`
2. `docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md`
3. `docs/KNOWLEDGE_BASE_CATALOG_IMPLEMENTATION_PLAN.md`
4. `docs/ENTERPRISE_BACKEND_API_SPEC.md`
5. `study/knowledge-base-framework-deep-analysis.md`
6. `study/knowledge-base-composition-blueprint.md`（若本地研究目录可用）
7. 任务涉及的现有 Go、Python、前端入口和测试文件

> 请在 GraphInsight 仓库实现“知识库目录与多知识库隔离”。先阅读 `AGENTS.md`、本规划、`docs/ENTERPRISE_BACKEND_API_SPEC.md`、`go-backend/internal/httpserver/documents_native.go`、`backend/services/document_graph_service.py` 和现有前端知识库页面。先完成 Phase 0 和 Phase 1，再等待验收，不要一次性重构 GraphRAG。所有后台接口使用 `/api/v1/admin/*`，业务接口使用 `/api/*`；写操作必须有权限、审计字段和 `trace_id`。必须复用现有 Go control plane、admin store、任务中心、文档解析器、Neo4j、Milvus、`adminService` 和 `types` 约定。不得把 token、密码或 API key 写入代码、日志、文档或测试输出。每个阶段都要运行对应的单测/编译/smoke，并报告实际文件、迁移、接口和未完成项。选择 A 下不保留请求级兼容 fallback，不创建 default KB，所有新旧调用方必须显式携带 `kb_id/kb_ids`；若发现旧知识数据，先执行受控清理并报告范围。

每次交付必须返回：

1. 修改文件清单和每个文件的职责。
2. 数据库迁移和回滚方法。
3. 新增/修改 API 的请求、响应和权限。
4. 文档、图谱、向量和问答作用域隔离证据。
5. 测试命令和真实结果。
6. 尚未实现的连接器、治理或 UI 能力，不能把计划写成已完成。

在 M1 关闭前，必须额外返回真实命令输出或可复核摘要：迁移幂等、迁移回滚、`go test ./...`、Python 语法检查、边界守卫、后端 smoke、所有已知调用方显式 scope 的检查、缺 scope 负向测试，以及两个 KB 隔离黑盒场景。没有实现文件和测试证据时，只能报告“决策冻结完成，M1 待实施”。

## 10. 与现有规划的关系

本规划是现有企业后台文档的知识库目录实施补充：

1. API 总体约定沿用 `docs/ENTERPRISE_BACKEND_API_SPEC.md`。
2. Go/Python 边界沿用 `docs/BACKEND_BOUNDARY_FINAL.md`。
3. 文档解析和知识发现沿用 `docs/DOCUMENT_PARSER_MINERU_INTEGRATION_PLAN.md` 与 `docs/KNOWLEDGE_DISCOVERY_PIPELINE_DESIGN.md`。
4. 任务、权限、审计和前端验收沿用 `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` 与 `docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md`。

本文件只描述目标和实施顺序；在实现完成并通过验收前，不应把路线图条目标记为完成。
