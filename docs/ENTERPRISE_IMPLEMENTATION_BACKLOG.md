# GraphInsight 企业级改造 Backlog（执行看板）

更新时间：2026-06-11
状态字段：`todo`、`in_progress`、`done`

## P0（必须）

1. `AUTH-001` 角色模型落地（super_admin/project_admin/operator/viewer）
状态：`done`

2. `AUTH-002` 项目级权限校验中间件
状态：`done`

3. `JOB-001` 建图任务异步化（job_id + 状态机）
状态：`done`

4. `JOB-002` 任务查询与重试 API
状态：`done`

5. `KB-001` 单文档删除（文件+图谱）
状态：`done`

6. `KB-002` 全量清空知识库（文件+图谱）
状态：`done`

7. `ARCH-001` Go 默认外部入口收口（前端默认地址、smoke/preflight/E2E 默认走 Go）
状态：`done`

## P1（应做）

1. `KB-003` 软删除与恢复窗口
状态：`done`

2. `KB-004` 删除 dry-run 预览
状态：`done`

3. `OBS-001` 问答质量看板（引用率、失败率、延迟）
状态：`done`

4. `OBS-002` 模型连通性测试中心
状态：`done`

5. `OBS-003` 问答链路追踪
状态：`done`

6. `OPS-001` 告警通道（Webhook）
状态：`done`

7. `CTRL-001` Go 控制面入口收口（优先 `monitor / jobs / qa-traces`，逐步替代整段 `/api/v1` 代理）
状态：`done`

验收进展：

1. 已知后台模块路由当前已由 Go 显式注册并标记为 `go-native` 或 `go-orchestrator`。
2. `/api/v1/admin` 与未知 `/api/v1/admin/*` 已由 Go 兜底返回 `404`，不会继续落入 legacy Python `/api/v1/**` 代理。
3. 回归验证：`go test ./...` 通过；对应提交 `f324b71 fix(go): own unknown admin routes`。
4. 非 admin 的未知 `/api/v1/**` 兼容代理已移除；`/api/media/**` 与公开媒体代理兼容入口已由 Go 原生承接。
5. `jobs` 控制面写侧已迁移为 Go 原生：创建 `build_graph/clear_kb/reindex` 任务、取消、重试均直接写 `admin_jobs` 和 `admin_logs`，路由 owner 为 `go-native`；任务执行器仍作为 Python 能力层/后续 worker 范畴。
6. `auth` 模块已迁移为 Go 原生：`POST /api/v1/admin/auth/login` 直接校验 `admin_users.password_hash`、更新登录统计、签发兼容 Python 的 HS256 JWT；`POST /api/v1/admin/auth/register` 仅允许创建首个管理员并授予全局 `super_admin`；`POST /api/v1/admin/auth/logout` 记录审计日志并保持无状态 JWT 语义；`GET /api/v1/admin/auth/me`、`POST /api/v1/admin/auth/change-password`、`GET /api/v1/admin/auth/authorize` 均已改为 Go 直连 admin store；`GET /api/v1/admin/auth/profile` 仅保留为历史兼容别名，新增代码应统一使用 `/api/v1/admin/auth/me`。
7. `users` 导出接口已迁移为 Go 原生：`GET /api/v1/admin/users/export-csv` 直接复用 Go 侧 `admin_users` 读模型生成 UTF-8 BOM CSV，并写入 `user_export_csv` 审计日志。
8. `config/test` 已迁移为 Go 原生：`POST /api/v1/admin/config/test/neo4j`、`POST /api/v1/admin/config/test/ai_service`、`POST /api/v1/admin/config/test/model`、`POST /api/v1/admin/config/test/embedding`、`POST /api/v1/admin/config/test/vector_store`、`POST /api/v1/admin/config/test/document_parser` 均由 Go 控制面执行；其中 `model` 保留真实上游探测并继续向 `GET /api/v1/admin/config/test/model/latest` 写入最近一次探测快照，`document_parser` 支持 native 可用性和 MinerU `/health` 探测。历史 `test/openai` 兼容别名已移除。
9. `profile` 历史兼容子路径已彻底收口：`/api/v1/admin/profile/*` 除 `GET /stats`、`PUT /password` 及根路径读写外，未知子路径现在由 Go 直接返回 `404`，不再回落 Python 管理后台代理。
10. 本轮已清空 Go 路由中的 admin 兼容代理挂载：`config`、`logs`、`users` 未知子路径、`monitor/health/simple`、未知 `/api/v1/admin/*` 均改为 Go 原生处理或 Go-owned `404/405`；`/api/media/**` 与公开媒体代理兼容入口也已切到 Go 原生。
11. `jobs` 执行边界已进一步收口：Go 仅负责写入/变更 `admin_jobs` 控制面状态，Python 在启动时拉起常驻 worker 轮询 `admin_jobs` 并原子 claim `pending` 任务执行，避免再依赖 Python 自身 `/api/v1/admin/jobs/*` 请求生命周期触发后台任务。
12. `jobs` worker lease 已落地：`admin_jobs` 新增 `claimed_by`、`claim_expires_at`、`last_heartbeat_at` 字段，并提供 `backend/admin/migrate_job_worker_lease.py` 迁移脚本，用于支撑多 Python 进程下的过期租约重领与心跳续租。
13. `jobs` stale lease recovery 已补齐：Python worker 每轮会优先扫描 `claim_expires_at` 已过期的 `running` 任务，将其回收到 `pending` 并记录 `job_requeued_stale_lease` 审计日志，避免执行进程异常退出后任务永久卡死。
14. `jobs` 显式唤醒链路已补齐：Go 在 `POST /api/v1/admin/jobs/build-graph|clear-kb|reindex` 和 `POST /api/v1/admin/jobs/{job_id}:retry` 成功后，会 best-effort 调用 Python `POST /api/internal/jobs/wake` 提前唤醒 worker，减少纯轮询延迟；若唤醒失败，Python 仍通过轮询兜底执行。
15. Python 侧已同步去掉 admin jobs 路由中的请求级 `BackgroundTasks` 执行路径，改为统一调用 `job_service.wake_worker()`，并把唤醒入口迁入 `/api/internal/jobs/wake`，确保 Go/Python 两个入口共享同一套任务执行边界且不再依赖 Python `/api/v1/admin/jobs/*` 兼容路径。
16. `docqa` 主通道已完成公开面收口：Go 对外保留 `/api/docqa*`，但实际执行统一进入 Python 内部能力入口 `/api/internal/docqa*`；Python 原 `/api/docqa*` 公开业务路由已从源码与运行态删除，统一后端默认路径不再依赖任何 Python public business surface。
17. `nl2cypher` 主通道已完成公开面收口：Go 对外保留 `/api/nl2cypher*`，但上游统一切换为 Python 内部能力入口 `/api/internal/nl2cypher*`；Python 原 `/api/nl2cypher*` 公开业务路由已删除，不再作为统一后端默认上游目标。
18. `documents` 与 `graph/build` 主通道已同步收口：Go 对外仍暴露 `/api/documents*` 与 `/api/graph/build`；其中 `GET /api/documents`、`GET /api/documents/deleted`、`POST /api/documents/upload`、`DELETE /api/documents/{doc_id}`、`DELETE /api/documents`、`POST /api/documents/{doc_id}/restore` 已改为 Go 原生读取/写入文档目录与回收站元数据，并直接执行单文档图谱删除与整库图谱清空；`POST /api/graph/build` 也已并回 Go 原生建图任务提交入口，直接创建 `build_graph` 任务、支持幂等回放并唤醒 Python worker；相关 Python 公开业务路由已删除，不再保留兼容挂载。
19. Python 公开业务能力路由已完成移除：`/api/graph/build`、`/api/documents*`、`/api/docqa*`、`/api/nl2cypher*`、`/api/media/**`、`/api/client-logs`、`/api/proxy-media`、`/api/proxy-image`、`/api/video-thumbnail` 均不再由 Python 公开挂载；统一后端主路径只保留 Go 外部入口与 Python `/api/internal/*` 能力入口。
20. Python 业务路由文件级边界已继续收紧：`documents`、`doc_qa`、`nl2cypher`、`graph_build` 的共享实现与内部能力入口已和公开兼容入口分离，运行态职责与源码布局保持一致。
21. 所有 Python 公开业务兼容入口已完成删除；共享实现与内部能力继续保留在 `api/routes/*`。
22. Python 公开管理兼容入口已完成收口并移除：`admin/api/route_registry.py` 不再注册 public admin routes；`admin/api/endpoints/*` 仅保留共享实现与内部 jobs capability。
23. 新增 `docs/PYTHON_COMPAT_SURFACE_AUDIT.md` 与 Python 路由边界守卫测试，冻结当前 Python surface：哪些路由属于内部 capability、哪些公开业务/管理路径已经删除，避免后续边界再次漂移。
24. Python admin compatibility surface 已继续缩小：默认 compat registry 已完全清空；`config/monitor/logs/profile/qa-traces/rbac/users/auth` 对应公开路由模块在 Python 源码层已收口为退役标记文件，`jobs` 仅保留 `/api/internal/jobs/wake` 所需的最小内部能力实现。
25. Python business compatibility surface 已继续缩小：`graph_build/documents/docqa/nl2cypher/query/expand/node/media/client-logs/proxy-*` 对应公开路由模块均已从 Python 源码删除，统一后端默认运行态只保留 `/api/internal/*` 业务 capability。
26. 已新增 `backend/tests/check_dev_runtime_defaults.py` 并接入 smoke suite，用于固定 `scripts/dev-backend.sh` 默认写出的 unified 运行态：`RBAC_AUTHZ_MODE=go_db`，以及 Go `/health` 中的 Python/orchestrator/authz 边界信息。
27. Python 源码包层级已继续收口：业务 public compat 入口与 `api/compat_routes/*` 已删除；共享实现与内部能力保留在 `api/routes/*`、`api/routes/*_internal.py` 与 `admin/api/endpoints/*`。
28. 仓库内零引用的旧 shim 已继续删除：`api/compat_routes/{client_logs,query,node,expand,media}.py`、`admin/api/compat_routes/{auth,jobs,config,logs,monitor,profile,qa_traces,rbac,users}.py` 以及对应的 `api/routes/{client_logs,query,node,expand,media}.py` 已移除，避免旧公开路由继续伪装成 normal compat surface。
29. 最小 compat 集合对应的 `api/routes/*_public.py` 过渡 shim 与后续引入的 `api/legacy_debug_routes/*`、`admin/api/legacy_debug_routes/*` 临时承接层也已删除；业务 `api/compat_routes/*` 公开兼容源码面也已同步删除。
30. Python 兼容关闭态识别面也已继续收紧：零引用的 `api/compatibility.py` 轻量日志钩子已删除；已删除的公开业务/管理路径直接回落为普通不存在路径，不再保留额外 compatibility helper。
31. Python admin public compat 已彻底移除：不再保留 `auth/jobs` public compat 模块；当前 admin 侧只保留 `/api/internal/jobs/wake`。
32. Go authz 运行态已继续规范化：`RBAC_AUTHZ_MODE` 在配置加载时只落到 `go_db` 或 `local_jwt_soft`；Python authz 上游兼容链已删除，更宽 smoke 已覆盖 `authz` 与 `jobs_api` 黑盒回归。
33. unified 开发启动链路已补齐 admin DB 运行态收口：`scripts/dev-backend.sh` 会把 `ADMIN_DATABASE_URL` 写入 `logs/dev/backend.env` 并显式注入 Python/Go 进程，避免 Python worker 回退到占位默认库。
34. `backend/tests/check_dev_runtime_defaults.py` 已固定 unified 运行态的 admin DB 约束：`ADMIN_DATABASE_URL` 必须存在且不能是占位默认值。
35. 默认 unified 深链路黑盒验证已继续扩大：`run_backend_smoke_suite.py` 当前覆盖 `dev_runtime_defaults / migration_cleanup_guards / go_orchestrated / nl2cypher_contract / nl2cypher_post_contract / nl2cypher_audit_contract / docqa_post_contract / docqa_audit_contract / deep_research_post_contract / deep_research_audit_contract / docqa_health_contract / python_public_removed / authz / unified_mode / documents / jobs_api / reindex_obs / qa_traces / qa_cost_unit / docqa_full_chain` 共 20 个 case，统一用于固定 Go 外部入口、Python capability plane、静态迁移清理守卫与控制面读写审计边界。
36. Go -> Python business capability contract 已继续收紧：Python `/api/internal/{docqa,documents,graph/build,nl2cypher}` 现在要求 `X-Go-Orchestrator=graphinsight-go` 且必须带 `X-Trace-Id`；Go 编排层在未收到上游 trace 时会主动补发 trace，确保跨语言链路可对账。
37. Go internal header 语义已拆分：Python business capability 不再接受仅带 `X-Go-Proxy` 的请求，`X-Go-Proxy` 只保留给 `POST /api/internal/jobs/wake` 这类内部控制信号，避免编排流量与代理/唤醒流量继续混用。
38. `nl2cypher` Go 入口独立回归已补齐：新增 `backend/tests/check_nl2cypher_go_contract.py`，并纳入 `run_backend_smoke_suite.py` 与默认 unified smoke，验证 `/api/nl2cypher/examples`、`/api/nl2cypher/status` 的 Go owner、响应契约与鉴权边界。
39. `POST /api/nl2cypher` 入口边界已进一步收紧：Go 侧现在先校验请求体 JSON 与 `natural_language` 非空，再转发到 Python `/api/internal/nl2cypher` 推理；新增 `backend/tests/check_nl2cypher_post_go_contract.py` 与 Go 单测，固定“坏请求不再穿透到 Python capability plane”的外部入口语义。
40. `POST /api/nl2cypher` Go 入口审计已补齐：Go 在拒绝坏请求或完成上游转发后都会写入 `admin_logs` 业务审计，新增 `backend/tests/check_nl2cypher_audit_go_contract.py` 固定审计落库契约，Python 侧重复业务审计已移除以避免双写。
41. `POST /api/docqa` 入口边界已按同一模式收紧：Go 侧现在先校验请求体 JSON 与 `question` 非空，再转发到 Python `/api/internal/docqa` 问答能力；新增 `backend/tests/check_docqa_post_go_contract.py` 与 Go 单测，固定“坏请求不再穿透到 Python capability plane”的外部入口语义。
42. `POST /api/docqa` Go 入口业务审计已补齐：Go 在拒绝坏请求或完成上游转发后都会写入 `admin_logs` 业务审计，动作标记为 `docqa_ask`，并新增 `backend/tests/check_docqa_audit_go_contract.py` 固定 trace 可对账的审计落库契约；Python 继续仅负责 QA trace 与问答执行。
43. `POST /api/docqa/deep-research` 入口边界已按同一模式收紧：Go 侧现在先校验请求体 JSON 与 `question` 非空，再转发到 Python `/api/internal/docqa/deep-research` 深度研究能力；新增 `backend/tests/check_deep_research_post_go_contract.py` 与 Go 单测，固定“坏请求不再穿透到 Python capability plane”的外部入口语义。
44. `POST /api/docqa/deep-research` Go 入口业务审计已补齐：Go 在拒绝坏请求或完成上游转发后都会写入 `admin_logs` 业务审计，动作标记为 `docqa_deep_research`，并新增 `backend/tests/check_deep_research_audit_go_contract.py` 固定 trace 可对账的审计落库契约；Python 继续负责深度研究执行与 `qa_traces`。
45. `GET /api/docqa/health` 公开健康检查入口也已从通用编排收口为显式 Go handler：Go 侧现在先校验 `probe_llm` 查询参数是否合法，再转发到 Python `/api/internal/docqa/health` 健康诊断能力；新增 `backend/tests/check_docqa_health_go_contract.py` 与 Go 单测，固定“坏查询参数不再穿透到 Python capability plane”的外部入口语义。
46. CI 已固化后端统一边界：`.github/workflows/ci.yml` 新增默认 `backend-unified-guards` job，在 Ubuntu 上创建 `backend/.venv` 后执行关键模块语法检查，并通过 `backend/tests/run_unified_boundary_guards.py` 运行迁移清理静态 guard、Python 公开业务/管理路由移除守卫、内部 capability header 契约单测、worker unit check 与 QA cost unit check，防止 Go/Python 职责边界回退。
47. CI 已补充发布级运行态入口：手动 `backend-release-smoke` job 复用 `backend/tests/run_release_acceptance.sh --skip-frontend-e2e --skip-perf-probe`，可对外部 Go 网关执行统一边界守卫与默认 20-case 后端 smoke；必要时可用 `backend_release_smoke_include` 定向执行单个或多个 case。
48. Linux / WSL 发布验收入口已补齐：新增 `backend/tests/run_release_acceptance.sh`，默认串联 `run_unified_boundary_guards.py`、`run_backend_smoke_suite.py`、`frontend/tests/run_admin_e2e.sh` 与 `run_perf_probe.py`；已实跑 `--skip-frontend-e2e --skip-perf-probe --include migration_cleanup_guards --include go_orchestrated --include unified_mode`，输出 `ACCEPTANCE_SUMMARY failures=0`。
49. 前端业务 E2E 已纳入统一发布验收 CI 入口：`.github/workflows/ci.yml` 新增手动 `release-frontend-e2e` job，复用 `backend/tests/run_release_acceptance.sh --skip-boundary-guards --skip-backend-smoke --skip-perf-probe` 执行 `frontend/tests/run_admin_e2e.sh`，确保发布态前端主链路继续默认走 Go 网关。
50. 完整发布链路已补齐 CI 手动入口：`.github/workflows/ci.yml` 新增 `release-acceptance` job，复用 `backend/tests/run_release_acceptance.sh --fail-fast` 串联统一边界守卫、后端 smoke、前端业务 E2E 与性能探针，并上传 `artifacts/release-acceptance` 与 `frontend/playwright-report`。
51. 本地 Linux 完整发布验收已实跑通过：`backend/tests/run_release_acceptance.sh --fail-fast` 输出 `ACCEPTANCE_SUMMARY failures=0`；当前统一边界守卫为 `SUMMARY total=13 failed=0`，后端 smoke `SUMMARY total=20 failed=0`，前端业务 E2E 通过，发布性能探针全 case `error_rate=0`。
52. 前端业务 E2E 已适配当前异步任务中心行为：建图从旧同步按钮文案验证改为提交任务后轮询任务终态，继续覆盖上传、建图任务成功、问答、QA trace 与软删除。
53. 发布性能探针已收口副作用：`graph-build` 探针提交任务后会取消自身创建的 probe job，避免遗留 `pending` 任务污染 Python worker 队列和后续 `reindex` smoke。

8. `OPS-002` 运维手册与上线验收清单固化
状态：`done`

## P2（增强）

1. `COST-001` 模型调用成本统计
状态：`done`

2. `RAG-001` Milvus 双路/图谱混合检索底座
状态：`in_progress`

验收进展：

1. Python DocQA 已新增轻量 Retrieval Orchestrator，支持 `keyword/vector/hybrid/graph_hybrid` 四种检索模式，默认保持 `keyword`，避免未配置 Milvus 或 embedding key 时影响现有问答。
2. Milvus 已作为独立向量库接入本地 Docker 编排；Neo4j 继续只负责 Document/Chunk/Entity/Relation 图谱结构和图谱扩展召回。
3. Embedding 接入 OpenAI-compatible provider，默认复用 `ai_service` 配置；`embedding.base_url` 默认保持空值并在运行时回退到 AI 服务网关，只有嵌入模型走独立供应商时才通过 `embedding` 分类覆盖 provider、base_url、api_key、模型、维度和批大小。
4. Python worker 启动时先加载 `backend/.env`，再叠加 `GRAPHINSIGHT_BACKEND_ENV_FILE` 指向的本地运行态配置；配置中心里的空 `ai_service.api_key` / `embedding.api_key` 不再遮蔽环境变量密钥，保证 Go 控制面模型目录和 Python 问答/向量运行态使用同一套 AI 服务凭据。
5. 文档建图完成后会 best-effort 写入 Milvus chunk 向量索引；删除单文档或清空知识库时同步清理向量索引。
6. QA trace 的 retrieval snapshot 已补充 orchestrator trace，可定位召回来源、融合策略、候选数量和向量检索跳过原因。
7. 后台配置页已拆分问答模型与嵌入/向量检索配置，并提供嵌入模型与 Milvus 连通性测试入口；嵌入模型候选复用 Go 控制面的模型目录接口，不需要维护第二套网关地址。
8. 模型目录已补充 token 上限元信息：优先读取服务商 `/models` 返回的 `context_window/max_output_tokens` 等字段，缺失时只给保守 `suggested_max_tokens`；配置中心选中问答模型时仅在 `Max Tokens` 为空的情况下自动填建议值。
9. `max_tokens`、`max_output_tokens`、`context_window` 已从敏感配置识别中排除，不再被误判为 API token。
10. 已新增管理员检索诊断入口 `POST /api/v1/admin/qa/retrieval-diagnostics`，由 Go 控制面按 `qa:ask` 权限转发到 Python internal capability，可对同一问题并行对比 `keyword/vector/hybrid/graph_hybrid` 的轻量命中、orchestrator trace、skip reason、summary 与检索健康信息。
11. 检索诊断已补充 `backend/tests/check_admin_retrieval_diagnostics_api.py` 作为 Go 外部管理入口 smoke；脚本读取 `ADMIN_TOKEN`，不写入任何凭证。
12. 已新增 [docs/DOCUMENT_PARSER_MINERU_INTEGRATION_PLAN.md](/Volumes/HP%20P900/projects/GraphInsight/docs/DOCUMENT_PARSER_MINERU_INTEGRATION_PLAN.md)，将 PDF/Office 解析质量提升、MinerU sidecar 接入、结构化 chunk 与混合检索质量改进列为 RAG 上游增强方向。
13. 文档解析器已抽象为 `NativeDocumentParser / MinerUDocumentParser / DocumentParserManager`，默认保持 native；`document_parser` 运行期配置支持 `provider/fallback_provider/base_url/endpoint_path/parse_mode/output_format/timeout_seconds/parser_version`，MinerU 作为 HTTP sidecar 调用并支持失败回退 native。
14. 建图任务已支持任务级 `parser_provider` 灰度参数；`POST /api/graph/build` 可直接传入，`POST /api/v1/admin/jobs/build-graph` 可放入 `payload`。Python worker 会把该参数传入建图执行层。
15. 建图阶段已新增解析产物持久化：默认写入 `backend/parsed_documents/{doc_id}/manifest.json|content.md|blocks.json|chunks.jsonl|raw.json`，Neo4j `Document.parsed_artifact_path` 记录目录；删除单文档、清空知识库或全量 force rebuild 时同步清理对应产物。
16. Neo4j `Document/Chunk` 与 Milvus chunk dynamic metadata 已写入解析元数据：`parser_provider/parser_version/parse_mode/block_type/heading_path/page_start/page_end/source_location`；建图已优先使用结构化 chunk，只有结构化切分为空时才回退旧字符窗口。
17. 已新增 `backend/tests/check_document_parser_unit.py`，并扩展 job worker、retrieval orchestrator、runtime config boundary 单测，固定解析器回退、任务透传、解析产物落盘和向量元数据传递。
18. 后台配置中心已新增 `document_parser` 配置分类和“文档解析 / MinerU”表单分区，可维护 `provider/fallback_provider/base_url/endpoint_path/file_field/parse_mode/output_format/timeout_seconds`，并可在界面触发 MinerU 连通性测试。
19. 已新增 [docs/KNOWLEDGE_DISCOVERY_PIPELINE_DESIGN.md](/Volumes/HP%20P900/projects/GraphInsight/docs/KNOWLEDGE_DISCOVERY_PIPELINE_DESIGN.md)，将下一阶段收敛为通用结构化 chunker、文档画像识别、按文档类型 prompt、schema-aware 抽取、实体/关系归一化与 evidence 校验。
20. 结构化 chunker 已完成基础接入：建图阶段会从 Markdown/ParsedDocument 识别标题层级、段落和 HTML table，表格独立成 `block_type=table` chunk，并保留 `caption/neighbor_before/neighbor_after/table_columns`；解析产物目录新增 `structured_chunks.jsonl`。
21. Embedding 写入 Milvus 的批大小已在运行时限制到 10，兼容当前 OpenAI-compatible 网关对 `/embeddings` input batch 的上限；向量 collection 创建时会使用实际 embedding 返回维度，并在发现已有 collection 维度不匹配时自动重建，避免单次建图向量索引整批失败。
22. 知识发现基础治理已接入：实体归一化会清理 LLM JSON/dict 污染并规范常见药剂名称空格差异；表格 chunk 会按行生成“首列主体 -> 指标值”和“主体 -> 表格主题”关系，并写入 `evidence/relation_type/confidence`。schema-aware LLM 关系抽取已要求输出 `source/target/label/evidence/confidence`，并由 `EvidenceValidator` 过滤无证据或证据无法覆盖 source/target 的关系；旧规则抽取已禁止把任意短语当关系类型，低置信度“同段提及”不进入默认图谱。当前 PDF 在表格 LLM 语义增强后最近复建结果为 `Chunk=14`、`Entity=141`、`RELATION=96`、脏实体 0、无 evidence 关系 0。
23. 图谱实体/关系抽取器已改为读取配置中心 `ai_service` 运行时配置，避免继续使用启动环境变量里的旧模型名导致“模型不可用，自动切换”；同时新增自适应 `ExtractionPlanner`，基于文档类型、标题路径、block type、重要章节、信息密度和任务档位选择 LLM schema-aware 抽取 chunk，不再固定抽取前 N 个非表格 chunk。`LLM_GRAPH_EXTRACT_MAX_LLM_CHUNKS` 作为 fast 档基础预算保护，balanced/deep 会按结构覆盖动态扩展；表格 chunk 采用结构化表格关系保底，并在 balanced/deep 或 `complex_extraction=true` 时进入 LLM 语义增强。图谱实体/关系抽取已采用 bounded reasoning，`fast/balanced` 均使用 low effort，只有显式 `deep` 才提升到 medium effort。LLM 实体/关系抽取的熔断范围已收窄为 provider 级错误和累计重复 timeout，单个 chunk 超时不再暂停整轮抽取，重复 timeout 会短暂 cooldown 以保护构图时延。
24. 配置中心模型目录主路径已从历史 `openai` 命名收敛为 `GET /api/v1/admin/config/ai-service/models`；前端和 Go 测试已切换到新路径，Go 旧 `openai` 路由别名和 Python 旧 `get_openai_config/test_openai_connection/get_available_openai_models` 兼容方法已删除，并由 runtime boundary guard 防止回退。
25. 已新增 [docs/CONFIG_CENTER_RUNTIME_AUDIT.md](/Volumes/HP%20P900/projects/GraphInsight/docs/CONFIG_CENTER_RUNTIME_AUDIT.md)，记录配置中心运行态主模型、已清理的旧 `openai` 命名、空字符串遮蔽默认值问题和剩余兼容别名。
26. 本地开发启动脚本默认 Neo4j Bolt 地址已从 `bolt://127.0.0.1:7687` 调整为 `bolt://localhost:7687`，避免 macOS 本机进程抢占 IPv4 端口时 Python/Go 误连非 Neo4j 服务；当前配置库中的 `neo4j.uri` 也已按该策略对齐。
27. 后端根目录旧调试/演示脚本已清理：移除 `check_node.py`、`debug_node.py`、`create_video_node.py`、`init_admin_quick.py`、`check_logs_table.py` 和旧同名 schema 文件 `admin/schemas.py`，避免硬编码演示数据、默认账号和旧 schema 入口误导运行态开发；迁移清理 guard 已加入防回流检查。
28. 本地开发启动脚本已改为优先用 `screen` 托管 Python/Go 服务，并在 `stop/restart` 时按服务端口清理残留监听进程，避免 screen session 退出但子进程继续占用 8001/8081 导致新代码未加载。
29. DocQA 检索编排已接入真正的二阶段 Reranker：`keyword/vector/graph` 候选先按 RRF 融合，再在 `retrieval.rerank_enabled=true` 且 `retrieval.rerank_model` 已配置时调用 OpenAI-compatible `/rerank` 端点重排；`rerank_base_url` 留空复用 `ai_service.base_url`，API key 复用 `ai_service.api_key`，调用失败会降级为原融合排序并写入 orchestrator trace。
30. 已修复多轮问答第二轮可能 500 的问题：上下文检索 query 中的 `g/L`、`/` 等 Lucene 特殊字符会先做 fulltext-safe 清洗，且 Neo4j fulltext 懒加载迭代异常已纳入 fallback，不再直接中断问答。

3. `SEC-001` 敏感配置 KMS 加密
状态：`todo`

4. `DATA-001` 文档版本差异对比
状态：`todo`

5. `UI-001` 后台导航与信息架构重做
状态：`in_progress`

验收进展：

1. 后台侧边导航已从平铺菜单调整为按运营场景分组：`总览`、`知识库运营`、`问答与模型`、`观测与审计`、`组织安全`。
2. 后台首页已从单纯“系统仪表板”调整为“运营总览”，首屏提供知识库治理、任务中心、问答追踪和系统监控入口。
3. 问答追踪详情已补充可读诊断摘要，直接展示检索模式、召回/引用、生成模式、模型、档位、Token、trace_id 与检索 skip reason。
4. 问答追踪详情已接入管理员检索诊断入口，可基于当前问题一键对比 `keyword/vector/hybrid/graph_hybrid` 的命中、耗时、来源计数与诊断建议。
5. 问答追踪详情已补充到配置中心、建图任务、知识库治理的上下文跳转，形成“问答异常 -> 检索诊断 -> 配置/建图/知识库治理”的第一条排障链路。
6. 配置中心 `AI 服务配置` 标签已调整为 `AI / 模型 / 检索`，页顶显式展示真实配置分区：`AI 基础`、`模型策略`、`检索策略`、`Embedding / Milvus`。
7. 配置中心分区入口复用现有真实字段与接口，不新增假配置项：`ai_service`、`retrieval`、`embedding`、`vector_store` 分类仍通过既有 Go 控制面读写。
8. 本轮未改动后端 API、路由路径或权限语义；前端仍使用既有 `/admin/*` 页面、Go 默认外部入口与 `POST /api/v1/admin/qa/retrieval-diagnostics`。
9. 后续剩余工作：知识库/任务/问答追踪之间的更细上下文参数透传、运营总览的质量指标聚合、配置中心各分区进一步拆成独立子页或锚点状态。

6. `MODEL-001` 模型集合与推理档位抽象（fast / balanced / deep，不做版本管理）
状态：`in_progress`

验收进展：

1. `qa-traces/cost-summary` 已在 Go 默认外部入口补齐真实成本估算，不再只统计 token；当前支持从 `AI_COST_MODEL_PRICING_JSON` 读取模型价格表，并返回 `estimated_cost`、`currency`、`pricing_source`。
2. 问答与深度调研内部能力入口已开始接入统一 `reasoning_profile`：当前 `POST /api/docqa` 与 `POST /api/docqa/deep-research` 已支持可选字段 `reasoning_profile=fast|balanced|deep`，并会把实际档位写入 `qa_traces.generation_snapshot`。
3. 当前最小运行策略已落地：`docqa` 默认 `balanced`，`deep_research` 默认 `deep`。
4. 前端业务问答面板已支持显式选择 `docqa` 与 `deep_research` 档位；后台 `qa-traces` 列表与详情都可直接查看实际 `reasoning_profile`。
5. 后台 AI 配置页已开始展示模型目录元信息，并支持配置场景默认档位：`docqa_reasoning_profile`、`deep_research_reasoning_profile`、`graph_extract_reasoning_profile`、`graph_extract_complex_reasoning_profile`。
6. Go 外部入口已在未显式传入 `reasoning_profile` 时，按统一场景策略自动补齐默认档位，并转发到 Python internal capability；当前已接入 `docqa`、`deep_research`，以及建图任务 `graph_extract / graph_extract_complex`。
7. `model_probe` 已纳入统一场景策略配置，当前会在模型连通性测试结果快照中记录实际策略档位。
8. `POST /api/graph/build` 当前支持任务级 `reasoning_profile` 与 `complex_extraction`，Go 会按场景默认策略补齐，Python worker 再把实际档位传递到实体/关系抽取执行层。
9. `POST /api/docqa` 已支持轻量多轮上下文 `conversation_history`；后端将历史用于检索改写和指代消解，前端问答面板会随每轮问题传递最近对话，并在新问题开始时清空旧引用选择，避免引用证据面板残留上一轮内容。
10. `POST /api/docqa` 的引用摘要已改为基于本轮 question/answer 聚焦证据窗口，而不是固定展示 chunk 开头；验证覆盖 `check_docqa_reasoning_profile_unit.py`。
11. 尚未完成的部分进一步收敛为“更完整的模型目录来源”。

7. `QA-001` 前端业务主流程 E2E（上传 -> 建图 -> 问答 -> 删除）
状态：`done`

验收进展：

1. 已新增并稳定化 `frontend/tests/e2e/business-docqa-flow.spec.ts`。
2. 已新增 `npm run e2e:business`。
3. 已支持 `E2E_SPEC=business-docqa-flow` 通过 `frontend/tests/run_admin_e2e.ps1` / `frontend/tests/run_admin_e2e.sh` 单独执行。
4. 已增加 Go route owner 断言与测试文档自清理。
5. 已纳入统一发布验收 CI 手动入口：`release-frontend-e2e` 通过 `backend/tests/run_release_acceptance.sh` 调用 `frontend/tests/run_admin_e2e.sh`，继续使用 Go 外部网关作为 `VITE_API_BASE_URL`。

8. `PERF-001` 压测报告（建图并发、问答并发、查询并发）
状态：`done`

验收进展：

1. 已新增 [docs/ENTERPRISE_PERFORMANCE_BASELINE.md](/home/yuanhuan/GraphInsight/docs/ENTERPRISE_PERFORMANCE_BASELINE.md)，沉淀 2026-06-07 当前统一运行态的性能基线。
2. 只读探针已实跑：`backend/tests/run_perf_probe.py --preset readonly --requests 20 --concurrency 4`，`error_rate=0`。
3. 发布路径探针已实跑：`backend/tests/run_perf_probe.py --preset release --requests 5 --concurrency 2`，覆盖 `query`、`docqa-health`、`nl2cypher-status`、`docqa`、`graph-build`，`error_rate=0`。
4. 当前文档记录的是发布级性能基线，不等同于容量上限；更高并发和 soak 压测仍可后续扩展。
5. 已新增 `backend/tests/run_perf_soak.py` 作为最小 soak/capacity 执行入口，复用 `run_perf_probe.py` 做多轮批次验证并输出 `summary.json`。

9. `CI-001` 上线验收清单固化到 CI
状态：`done`

验收进展：

1. `.github/workflows/ci.yml` 已新增默认 `backend-unified-guards`，通过 `backend/tests/run_unified_boundary_guards.py` 持续防止 Python public surface、内部 capability header 与 Go 入口显式注册发生回退。
2. `.github/workflows/ci.yml` 已新增手动 `backend-release-smoke`，通过 Linux 发布验收入口在准备发布时对外部 Go 网关执行默认 20-case 后端 smoke。
3. 上述后端 Python 检查均先创建并使用 `backend/.venv`，不依赖系统 Python 作为项目运行环境。
4. 本地 Linux 完整发布验收已通过，结果记录在 [docs/ENTERPRISE_RELEASE_DRY_RUN_2026_06_07.md](/home/yuanhuan/GraphInsight/docs/ENTERPRISE_RELEASE_DRY_RUN_2026_06_07.md)；CI 手动入口仍应在真实发布前复跑。
5. 2026-06-13 已修正 CI 后端边界编译清单，移除已退役 Python `documents` / `graph_build` 路由文件引用，补齐当前 runtime helper 与 admin retired endpoint 模块编译检查。
6. 2026-06-13 已新增 [docs/BACKEND_BOUNDARY_FINAL.md](/home/yuanhuan/GraphInsight/docs/BACKEND_BOUNDARY_FINAL.md) 作为后续开发与验收的后端职责冻结清单。
7. 2026-06-13 本地复验通过：`run_unified_boundary_guards.py`、`go test ./internal/httpserver ./internal/config`、`frontend npm run build`、`admin-core` E2E。

## 模块映射建议

1. 权限与组织：`backend/admin/api/endpoints/auth.py`、`backend/admin/api/deps.py`
2. 任务中心：新增 `backend/admin/api/endpoints/jobs.py`、`backend/admin/services/job_service.py`
3. 知识库治理：`go-backend/internal/httpserver/documents_native.go`、`backend/services/document_graph_service.py`
4. 问答与模型：`backend/api/routes/doc_qa.py`、`backend/services/doc_qa_service.py`
5. 前端后台页：`frontend/src/pages/Admin/*`

## 每周节奏建议

1. 周一：需求拆解与风险确认。
2. 周二到周四：开发与联调。
3. 周五：回归、文档更新、发布记录。

## 变更管理规则

1. 每个 backlog 项必须有验收标准。
2. 每次合并必须更新本看板状态。
3. 每个 `done` 项必须附回归结果。

## M5 Chunk Revision / Reindex（2026-10-02）

1. `M5-B0` Python `reindex_chunks` worker
状态：`in_progress`

验收进展：

1. Python worker、Milvus v3 schema/type 守卫、真实 upsert 数量校验、CAS 回写和文档聚合已实现。
2. SQLite 隔离验收与唯一临时 v3 collection 真实读回均通过；临时 PG/Neo4j/Milvus 清理后无残留。
3. 文档聚合异常已改为 fail-closed：仅“表尚未迁移”兼容，其它数据库异常向上抛出；worker/backfill 不得伪报成功或 `CLOSED`。
4. 共享生产 v3 迁移、逐 KB C3 数据治理和共享生产 `CLOSED` 尚未完成。
5. M5-A/B0 dry-run contract v1 已统一三个入口：固定 `DRY_RUN_RESULT` JSON 标记、`writes=0` 和退出码语义；仅为隔离验收契约，不代表共享生产迁移完成。
6. 2026-10-03 共享只读 C3 inventory 已运行：1 个 KB，`blocked/orphan/unrecoverable/scope_unresolved/scope_mismatches` 全为 0；当前读取到 `graphinsight_chunks_v2` 且无显式 `content_revision`，所以共享生产 gate 仍为 OPEN。

2. `M5-B0-C3` 共享 KB 不可自动收敛清单
状态：`todo`

验收标准：只读输出每个 KB 的 `blocked`、`orphan_revisions`、`unrecoverable`、`scope_unresolved`、`scope_mismatches`，并为人工处置项保留 `chunk_id` 和原因。

3. `M5-V3` 共享生产 Milvus v3 迁移
状态：`todo`

验收标准：独立迁移方案、canary、真实读回、幂等复跑、回滚路径和 v2 不变性证据齐全；未授权前不得写共享 dev/v2。

4. `M5-B1` Go 管理 API
状态：`todo`

验收标准：M5-B0 共享前置条件通过并取得审计授权后，才实现 job type 对账、chunk 读/PATCH/rollback、reindex-chunks、reindex-document 和 Python worker 唤醒。当前保持冻结。
