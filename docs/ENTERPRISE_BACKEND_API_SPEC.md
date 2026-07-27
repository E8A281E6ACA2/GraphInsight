# GraphInsight 企业级后台 API 规范草案（v1）

更新时间：2026-06-13
状态：统一后端边界版（Go 默认外部入口，Python internal capability）

## 1. 统一约定

1. 后台前缀：`/api/v1/admin`
2. 业务前缀：`/api`
3. 写操作必须写审计日志。
4. 所有返回体统一包含 `code`、`message`、`data`、`timestamp`、`trace_id`。
5. 所有后台接口默认需要 `Bearer Token`。
6. 当前统一后端主路径默认由 Go 接收外部业务请求，再调用 Python `/api/internal/*` 能力入口；Python 已不再保留公开 `/api/*` 业务能力路由，直接访问这些旧路径会返回 `404`。本地诊断应优先走 Go 正式入口，确需定位 Python 能力层时再显式访问 `/api/internal/*`。
7. 当前后台控制面 `/api/v1/admin/*` 默认由 Go 直接处理；Python 不再保留公开 admin 入口，只保留 `POST /api/internal/jobs/wake` 这类内部能力信号。

## 2. 组织与权限

### 2.0 认证

1. `POST /api/v1/admin/auth/login`
2. `POST /api/v1/admin/auth/register`
3. `POST /api/v1/admin/auth/logout`
4. `GET /api/v1/admin/auth/me`
5. `POST /api/v1/admin/auth/change-password`
6. `GET /api/v1/admin/auth/authorize?permission=...`
7. `GET /api/v1/admin/auth/profile`（兼容别名，新增代码优先使用 `/api/v1/admin/auth/me`）

当前统一后端实现中，登录/注册/登出/当前用户信息/修改密码/权限校验由 Go 控制面直接处理。登录返回兼容 Python 的 HS256 JWT，载荷包含 `sub` 和 `exp`，过期时间为 24 小时；注册仅允许创建系统首个管理员账户，并自动授予全局 `super_admin`；登出记录审计日志，保持无状态 JWT 语义，由前端清理本地 token。`GET /api/v1/admin/auth/profile` 仅作为历史兼容别名保留，语义与 `GET /api/v1/admin/auth/me` 一致。

### 2.1 租户

1. `GET /api/v1/admin/tenants`
2. `POST /api/v1/admin/tenants`
3. `PATCH /api/v1/admin/tenants/{tenant_id}`
4. `DELETE /api/v1/admin/tenants/{tenant_id}`

### 2.2 项目

1. `GET /api/v1/admin/projects?tenant_id=...`
2. `POST /api/v1/admin/projects`
3. `PATCH /api/v1/admin/projects/{project_id}`
4. `DELETE /api/v1/admin/projects/{project_id}`

### 2.3 角色与授权

1. `GET /api/v1/admin/roles`
2. `POST /api/v1/admin/roles`
3. `POST /api/v1/admin/roles/{role_id}/bindings`
4. `DELETE /api/v1/admin/roles/{role_id}/bindings/{binding_id}`

## 3. 知识库管理

### 3.1 知识库

1. `GET /api/v1/admin/knowledge-bases?project_id=...`
2. `POST /api/v1/admin/knowledge-bases`
3. `PATCH /api/v1/admin/knowledge-bases/{kb_id}`
4. `DELETE /api/v1/admin/knowledge-bases/{kb_id}`

### 3.2 文档

1. `GET /api/documents`
2. `POST /api/documents/upload`
3. `DELETE /api/documents/{doc_id}?purge_graph=true`
4. `DELETE /api/documents?purge_graph=true`

当前统一后端实现中，上述公开文档接口已经全部由 Go 统一外部入口直接执行。Go 原生负责文档目录读写、回收站元数据、软删除/恢复、整库清空 dry-run，以及文档图谱删除/清空；Python 旧的 `/api/documents*` 业务路由已经移除，不再作为任何运行态上游目标。

当前补充说明：

1. `GET /api/documents`
2. `GET /api/documents/deleted`
3. `POST /api/documents/upload`
4. `DELETE /api/documents/{doc_id}`
5. `DELETE /api/documents`
6. `POST /api/documents/{doc_id}/restore`

已经改为 Go 原生读取文档目录、维护回收站元数据，并直接执行单文档上传、删除、恢复以及整库清空。Python `/api/internal/documents*` 已从 unified runtime 与诊断入口一并退役。

后续增强接口（P1）：

1. `POST /api/v1/admin/knowledge-bases/{kb_id}/documents:delete-preview`
2. `POST /api/v1/admin/knowledge-bases/{kb_id}/documents:restore`

## 4. 任务中心

### 4.1 任务创建

1. `POST /api/v1/admin/jobs/build-graph`
2. `POST /api/v1/admin/jobs/clear-kb`
3. `POST /api/v1/admin/jobs/reindex`

统一请求体：

```json
{
  "tenant_id": "optional",
  "project_id": "optional",
  "kb_id": "optional",
  "payload": {},
  "max_retries": 3
}
```

当前统一后端实现中，以上创建接口由 Go 控制面直接创建 `pending` 任务记录并写入审计日志；具体建图、清库、重建索引执行仍属于 Python 能力层/后续 worker。Go 在创建成功后会 best-effort 调用 Python 内部 `POST /api/internal/jobs/wake` 以提前唤醒 worker，失败时仍由 Python 轮询兜底。Python 侧唤醒入口已迁入内部能力命名空间，不再依赖 `/api/v1/admin/jobs/*` 兼容路径。

`build_graph` 任务 payload 当前支持：

```json
{
  "source": "documents",
  "force": false,
  "doc_ids": ["optional_doc_id"],
  "complex_extraction": false,
  "reasoning_profile": "fast",
  "parser_provider": "native"
}
```

`parser_provider` 可取 `native` 或 `mineru`。未传入时使用运行期配置 `document_parser.provider`；传入 `mineru` 时由 Python worker 调用 MinerU HTTP sidecar，失败时按 `document_parser.fallback_provider` 回退。

文档解析产物会写入 `PARSED_DOCUMENT_STORAGE_PATH/{doc_id}/`，默认位置为 `backend/parsed_documents/{doc_id}/`。目录内包含 `manifest.json`、`content.md`、`blocks.json`、`chunks.jsonl`、`structured_chunks.jsonl`、`document_profile.json`、`extraction_schema.json`、`extraction_plan.json`，MinerU 场景还会保存原始 `raw.json` 或 `raw.txt`；Neo4j `Document.parsed_artifact_path` 记录该目录，`Document.document_type/domain/profile_confidence/profile_version` 记录文档画像，Chunk 会记录 `extraction_strategy/extraction_use_llm/extraction_priority/extraction_reasons`，其中表格在高质量档位可出现 `structured_table_plus_llm`，表示结构化表格关系保底后继续做 LLM 语义增强，便于从任务、问答质量问题反查解析和抽取策略。

### 4.2 任务查询

1. `GET /api/v1/admin/jobs`
2. `GET /api/v1/admin/jobs/{job_id}`
3. `POST /api/v1/admin/jobs/{job_id}:retry`
4. `POST /api/v1/admin/jobs/{job_id}:cancel`

状态流转约束：

1. 创建后状态为 `pending`。
2. 仅 `failed/cancelled` 可重试，且 `retry_count < max_retries`。
3. 仅 `pending/running` 可取消。
4. 任务写操作必须记录 `job_created`、`job_retry_submitted` 或 `job_cancelled` 审计日志。
5. `create` 与 `retry` 成功后允许触发 Python 内部 worker wake；该 wake 调用失败不改变主 API 返回结果。

## 5. 问答与模型

1. `POST /api/docqa`
2. `POST /api/docqa/deep-research`
3. `GET /api/docqa/health?probe_llm=true|false`
4. `GET /api/v1/admin/config/ai-service/models`
5. `POST /api/v1/admin/config/test/ai_service`
6. `POST /api/v1/admin/config/test/model`
7. `POST /api/v1/admin/config/test/embedding`
8. `POST /api/v1/admin/config/test/vector_store`
9. `POST /api/v1/admin/config/test/document_parser`
10. `GET /api/v1/admin/config/test/model/latest`
11. `GET /api/v1/admin/monitor/qa`
12. `GET /api/v1/admin/qa-traces`
13. `GET /api/v1/admin/qa-traces/{trace_id_or_pk}`
14. `POST /api/v1/admin/qa/retrieval-diagnostics`

当前统一后端实现中，Go 对外保留 `/api/docqa*` 与 `/api/nl2cypher*`，但上游目标分别切换为 Python 内部能力入口 `/api/internal/docqa*` 与 `/api/internal/nl2cypher*`。其中 `GET /api/nl2cypher/examples` 与 `GET /api/nl2cypher/status` 已改为 Go 原生提供；`GET /api/docqa/health` 由 Go 校验 `probe_llm` 查询参数后编排到 Python 内部健康诊断能力；`POST /api/nl2cypher`、`POST /api/docqa` 与 `POST /api/docqa/deep-research` 仍由 Go 编排到 Python capability plane 执行推理/问答，但请求体 JSON 及必填字段非空校验已前移到 Go 入口，并由 Go 写入业务审计日志。这样外部权限、基础契约与外部审计收口在 Go，Python 只保留能力执行、QA trace 和必要的运行时逻辑。

当前已落地 / 后续扩展约定：

1. 模型集合返回项当前包含：`provider`、`label`、`supports_reasoning`、`supported_profiles`、`default_profile`、`context_window`、`max_output_tokens`、`suggested_max_tokens`、`token_limit_source`
2. `POST /api/docqa` 与 `POST /api/docqa/deep-research` 当前已支持可选请求字段 `reasoning_profile`
3. `reasoning_profile` 统一取值：`fast`、`balanced`、`deep`
4. 当前默认策略：`docqa=balanced`，`deep_research=deep`，`graph_extract=fast`，`graph_extract_complex=balanced`
5. `GET /api/v1/admin/config/ai-service/models` 当前返回 `models[]`、`catalog[]` 与 `scenario_profiles`，用于承载模型目录元信息、场景默认档位和 token 上限提示；配置中心通过 `purpose=chat|embedding` 区分问答与嵌入模型候选，嵌入目录只保留可识别的 embedding/BGE/E5/GTE/Jina 等向量模型及当前手工配置模型。嵌入模型默认传入 AI 服务网关地址，只有独立供应商时才需要单独配置 `embedding.base_url`。历史 `/config/openai/models` 已移除。
6. `POST /api/docqa` 与 `POST /api/docqa/deep-research` 在请求未显式传入 `reasoning_profile` 时，当前会由 Go 外部入口按统一场景策略自动补齐默认档位后再编排到 Python internal capability
7. `POST /api/v1/admin/config/test/model` 当前会在测试结果快照中记录 `model_probe` 场景采用的默认档位，但不会把该统一档位直接透传成供应商私有探测参数；OpenAI-compatible 探测只发送标准必需的 `model/messages`，不强制附加 `temperature`、`max_tokens` 等可能被新模型拒绝的可选参数。上游返回非 2xx 时会提取并安全截断服务商错误信息
8. `POST /api/v1/admin/config/test/embedding` 当前使用 OpenAI-compatible `/embeddings` 探测嵌入模型连通性，`embedding.api_key/base_url` 为空时复用 `ai_service` 配置；上游返回非 2xx 时会提取并截断服务商错误信息，HTTP 400 会明确提示核对模型是否支持 Embeddings API
9. `POST /api/v1/admin/config/test/vector_store` 当前对 Milvus 做轻量 TCP 连通性探测，用于验证本地或远程向量库地址可达；即使向量检索功能开关关闭也会继续探测，避免把“功能未启用”误报为“Milvus 未启动”。配置读取使用运行进程的 `MILVUS_URI` 作为未落库时的默认地址
10. `POST /api/v1/admin/config/test/document_parser` 当前读取 `document_parser` 配置分类；`provider=native` 返回内置解析器可用，`provider=mineru` 会探测 `{base_url}/health` 并返回 MinerU version/status、`file_field`、`parse_mode`、`endpoint_path` 等运行信息
11. `POST /api/graph/build` 当前已支持可选请求字段 `reasoning_profile`、`complex_extraction` 与 `parser_provider`；未显式传入 `reasoning_profile` 时，Go 会按 `graph_extract / graph_extract_complex` 场景自动补齐默认档位，再交由 Python worker 执行；`parser_provider=mineru` 用于单次建图灰度调用 MinerU sidecar；建图会生成 `DocumentProfiler` 画像、动态抽取 schema 和自适应 extraction plan，并把 `document_type/domain/profile_version/extraction_strategy` 写入 Chunk 与向量 metadata；表格 chunk 在 balanced/deep 或复杂抽取任务下会使用 `structured_table_plus_llm`
12. `GET /api/v1/admin/qa-traces` 列表项当前已返回轻量 `reasoning_profile` 字段，便于后台直接筛查运行档位
13. `POST /api/v1/admin/qa/retrieval-diagnostics` 当前由 Go 控制面校验 `qa:ask` 权限后转发到 Python internal capability，可对同一问题分别运行 `keyword/vector/hybrid/graph_hybrid` 检索模式，返回各模式轻量命中、orchestrator trace、skip reason、summary 与检索健康信息，用于判断全文、向量和图谱扩展召回是否实际生效；`summary` 包含每种模式命中数、耗时、来源计数、跳过来源、最佳命中模式、最慢模式和建议动作
14. `POST /api/docqa` 当前支持可选 `conversation_history`，每轮包含 `role=user|assistant` 与 `content`，最多保留 8 轮；该历史只用于代词、指代和省略问题的检索改写与回答上下文，不作为事实证据来源
15. `POST /api/docqa` 返回的 `citations[].snippet` 当前会根据本轮 question/answer 聚焦到更相关的证据窗口，避免前端展示固定 chunk 开头造成引用证据滞后
16. `retrieval.rerank_enabled=true` 且配置了 `retrieval.rerank_model` 时，Python Retrieval Orchestrator 会在 keyword/vector/graph RRF 融合候选之后调用 OpenAI-compatible `/rerank` 二阶段重排；`rerank_base_url` 留空时复用 `ai_service.base_url`，API key 复用 `ai_service.api_key`，reranker 失败只回退原融合排序，不阻断问答
17. 不对外返回原始思维链，只返回最终答案、引用和运行元信息
18. `POST /api/v1/admin/config/test/ai_service` 只检查启用状态、API Key 是否配置及 OpenAI-compatible API 地址等必要配置；API Key 视为服务商定义的不透明凭据，不以 `sk-`、`sk-ant-` 或其他前缀推断有效性。实际鉴权与模型连通性由 `POST /api/v1/admin/config/test/model` 发起真实请求验证

## 6. 监控与审计

1. `GET /api/v1/admin/monitor/stats`
2. `GET /api/v1/admin/monitor/health`
3. `GET /api/v1/admin/logs`
4. `GET /api/v1/admin/logs/{id}`

`GET /api/v1/admin/monitor/health` 当前返回 PostgreSQL 控制面、Neo4j、Milvus、Python 能力层、AI 服务配置和系统资源检查。Milvus 使用当前管理配置或运行进程 `MILVUS_URI` 做 TCP 探测；向量检索未启用时仍返回实际连通状态，但 Milvus 不可达不会单独导致整体降级。Python 能力层通过 `{PYTHON_BACKEND_BASE_URL}/health` 探测，失败会使整体状态降级。AI 服务状态读取后台已保存配置，只表示凭据与模型已配置，不执行可能计费的模型请求。

## 7. 错误码建议

1. `AUTH_401`：认证失败
2. `AUTH_403`：无权限
3. `RESOURCE_404`：资源不存在
4. `VALIDATION_422`：参数校验失败
5. `JOB_409`：任务冲突
6. `DB_503`：数据库不可用
7. `LLM_503`：模型服务不可用

## 8. 发布前检查

1. API 文档已更新（OpenAPI 与 md 同步）。
2. 审计字段完整（operator、tenant、trace_id）。
3. 回归脚本通过（建议执行 `backend/tests/run_backend_preflight.ps1` 或 `backend/tests/run_backend_smoke_suite.py`）。
4. 降级策略清晰（DB/LLM 异常时的返回行为）。
