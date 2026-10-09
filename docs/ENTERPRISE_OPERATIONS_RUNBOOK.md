# GraphInsight 企业级运维手册

更新时间：2026-06-13
状态：执行版
适用范围：`frontend`、`go-backend`、`backend`、`Neo4j`、本地宿主机联调环境

## 1. 目标

本手册用于支撑以下场景：

1. 本地或测试环境启动 GraphInsight。
2. 排查 Go 网关、Python 能力层、Neo4j、模型网关异常。
3. 执行发布前固定检查。
4. 在链路异常时快速定位是入口层、编排层还是 AI 能力层问题。

## 2. 当前运行口径

当前项目按以下职责运行：

1. Go `8081`：默认外部入口、控制面入口、业务编排层；若端口自动回退，以 `logs/dev/runtime.env` 中的 `GO_BASE_URL` 为准。
2. Python `8001`：内部 AI 能力层、文档解析、建图执行、问答执行；若端口自动回退，以 `logs/dev/runtime.env` 中的 `PYTHON_BASE_URL` 为准。
3. Neo4j `7687/7474`：图数据库。
4. Frontend `5173`：默认开发态页面入口；手工 QA 可使用当前验证过的 `1234` 端口。

说明：

1. 前端、回归脚本、发布前 smoke 默认应优先访问 Go。
2. Python 不是当前推荐的默认外部访问入口。
3. 本地开发管理库默认使用 `docker-compose.dev.yml` 中的 PostgreSQL：`127.0.0.1:5434/graphinsight_admin`。

## 3. 启动顺序

推荐直接使用统一启动脚本：

```bash
scripts/dev-backend.sh up
```

脚本会按顺序拉起本地 PostgreSQL、Neo4j、Python 能力层和 Go 网关，并把真实访问地址写入 `logs/dev/runtime.env`。

如需手动启动，建议固定按以下顺序：

1. 启动 Neo4j。
2. 启动 Python 能力层。
3. 启动 Go 网关。
4. 启动前端。

推荐检查命令：

```bash
cat logs/dev/runtime.env
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8081/health
```

在 Windows PowerShell 中，如果 `curl` 被映射到 `Invoke-WebRequest`，优先使用：

```powershell
curl.exe http://127.0.0.1:8001/health
curl.exe http://127.0.0.1:8081/health
```

## 4. 服务健康判断

### 4.1 Python `8001`

最小健康标准：

1. `/health` 返回 `code=200`。
2. 管理库初始化成功，且 `ADMIN_DATABASE_URL` 指向当前本地开发库。
3. 如本次需要图查询或建图，`neo4j.connected` 应为 `true`。

### 4.2 Go `8081`

最小健康标准：

1. `/health` 返回 200。
2. 响应中可见 Python 上游状态。
3. `authz.mode` 应为 `go_db`。
4. 如本次涉及图查询或主链路回归，`neo4j.connected` 应为 `true`。
5. `neo4j.config_source` 在默认 unified 本地开发中应优先体现为 `admin_db` 或可解释的 fallback 来源。

### 4.3 前端 `5173`

最小健康标准：

1. 页面可打开。
2. 管理后台登录页可访问。
3. 页面请求默认打到 Go，而不是直连 Python。

## 5. 发布前固定动作

Linux / WSL 推荐执行完整发布验收：

```bash
backend/tests/run_release_acceptance.sh --fail-fast
```

如只验证后端统一边界与发布级 smoke，可执行：

```bash
backend/tests/run_release_acceptance.sh --skip-frontend-e2e --skip-perf-probe
```

Windows PowerShell 可继续执行：

```powershell
powershell -ExecutionPolicy Bypass -File backend/tests/run_backend_preflight.ps1
```

如本次涉及主业务链路、模型问答或任务中心，至少补跑：

```bash
backend/.venv/bin/python backend/tests/run_backend_smoke_suite.py --include docqa_full_chain
```

参考文档：

1. [docs/ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md](ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md)
2. 历史引用 `ENTERPRISE_GO_LIVE_ACCEPTANCE_CHECKLIST.md` 本轮未找到，不能据此验收；现存发布取证见 [M4-R1 验收](ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md)。
3. 历史引用 `ENTERPRISE_RELEASE_TEMPLATE.md` 本轮未找到；保留文档债务，不凭空补写模板。

## 6. 常见故障与处理顺序

### 6.1 端口占用

典型现象：

1. Python 或 Go 启动时报 `address already in use`。

Windows 建议检查：

```powershell
netstat -ano | findstr :8001
netstat -ano | findstr :8081
```

处理顺序：

1. 找到占用端口的 PID。
2. 确认是否是旧的 Python、Go、Node 进程。
3. 先优雅结束旧进程，再重启目标服务。

### 6.2 Neo4j 凭据漂移

典型现象：

1. 旧进程还能工作。
2. 新启动的 Python 或 Go 进程显示 `neo4j.connected=false`。
3. 图查询、建图、删除图谱联动失败。

处理顺序：

1. 确认当前 `7474/7687` 对应的是哪一个 Neo4j 实例。
2. 确认 `backend/.env` 中的 Neo4j 地址、用户名、密码与当前实例一致。
3. 重新启动新的 Python 与 Go 进程验证，而不是只依赖历史进程状态。

### 6.3 Python 健康但 Go 异常

典型现象：

1. `8001/health` 正常。
2. `8081/health` 异常或部分接口失败。

优先检查：

1. Go 是否正确加载了 `.env`。
2. Go 到 Python 上游地址是否可达。
3. Go `/health` 中的 `python_backend`、`authz`、`orchestrator` 状态。
4. 是否是 Go 新进程暴露了旧配置失配问题。

### 6.4 Go 健康但主链路失败

典型现象：

1. `/health` 正常。
2. 上传、建图、问答、追踪或删除在中途失败。

优先检查：

1. `backend/tests/check_docqa_full_chain.py`
2. `backend/tests/check_jobs_api.py`
3. `backend/tests/check_qa_traces_api.py`
4. 后台任务中心与问答追踪页中的 `job_id`、`trace_id`

### 6.5 问答结果异常

典型现象：

1. 接口 200，但回答为空或质量异常。
2. 问答追踪状态为失败。
3. 模型网关连接错误。

优先检查：

1. 当前 QA 模型配置是否指向预期模型集合。
2. 当前模型是否需要关闭思考模式，或切到更适合问答链路的档位。
3. `HTTP_CLIENT_TRUST_ENV` 是否导致代理环境变量干扰。
4. `/api/v1/admin/qa-traces` 中 `generation_snapshot.mode` 与错误详情。

## 7. 诊断分层

建议按以下顺序定位问题：

1. 基础层：端口、进程、环境变量、依赖服务是否启动。
2. 健康层：Go `/health`、Python `/health`。
3. 网关层：Go 原生查询接口是否正常。
4. 编排层：文档、建图、问答接口是否能经由 Go 走通。
5. 控制面层：任务中心、问答追踪、监控页是否能定位失败步骤。

## 8. 最小回滚思路

### 8.0 适用边界与当前回滚下限（2026-10-09 对账）

- `78b1f28` 是 2026-09-30 scope/RBAC 版本级演练的代码回滚下限，适用条件是该演练范围；不能直接作为当前 M5 schema/v3 或共享迁移回滚目标。
- M5 迁移的回滚必须遵守 v3 方案五步：写冻结 → v3 增量幂等回放至 v2 → 完整 `(chunk_id, revision, content_hash)` 对账 → 切回 v2 并重启 → 解冻；未执行共享迁移前，本手册不宣称该链路已真机验收。
- 历史无模型 soak 仅证明隔离栈链路/受理路径；真实 embedding/LLM 容量与 4→8→16 矩阵本轮未执行，不能作为容量上限。


当 Go 默认入口出现阻断，而 Python 能力层仍正常时，可按以下思路临时回滚联调路径：

1. 保持 Python `8001` 继续运行。
2. 将局部诊断脚本临时改为直连 Python internal capability。
3. 记录当前失败的 Go 入口症状、时间点、`trace_id`、`job_id`。
4. 问题修复后，恢复所有默认验证路径回到 Go。

说明：

1. 这是诊断性回滚，不代表架构方向回退。
2. 发布口径仍应以 Go 作为默认外部入口。
3. 不允许通过恢复 Python public business/admin routes 作为正式回滚方案。
4. 不允许通过绕过 Go 鉴权作为正式回滚方案。

回滚后至少执行：

```bash
backend/tests/run_release_acceptance.sh \
  --skip-frontend-e2e \
  --skip-perf-probe \
  --include migration_cleanup_guards \
  --include go_orchestrated \
  --include unified_mode
```

回滚成功的最低标准：

1. Go `/health` 返回 200。
2. Python `/health` 返回 200。
3. Python public business/admin routes 仍未恢复。
4. Go route owner 与当前职责边界一致。
5. 涉及主链路时，`docqa_full_chain` 仍通过。

补充事实（2026-06-07）：

1. Linux 本地已实跑 `backend/tests/run_release_acceptance.sh --fail-fast`，结果为 `ACCEPTANCE_SUMMARY failures=0`。
2. 当前后续动作应优先是 CI 远端复跑、容量/soak 验证与真实回滚演练，而不是恢复 Python public business/admin routes。
3. 历史记录称模板已补齐；原 `ENTERPRISE_PERF_SOAK_TEMPLATE.md`、`ENTERPRISE_ROLLBACK_DRILL_TEMPLATE.md` 本轮未找到。后续独立取证见 [无模型 soak](ENTERPRISE_PERF_SOAK_2026_09_30.md) 与 [版本回滚演练](ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md)，不作为原模板存在性的证明。

补充事实（2026-06-13）：

1. CI 后端边界编译清单已同步到当前真实 Python internal capability 文件，移除已退役 `documents` / `graph_build` 路由文件引用。
2. 本地已复验 `backend/tests/run_unified_boundary_guards.py`，结果为 `SUMMARY total=12 failed=0`。
3. 本地已复验 `go test ./internal/httpserver ./internal/config`，结果通过。
4. 本地已复验 `frontend npm run build`，结果通过。
5. 本地已复验管理后台核心 E2E：`frontend/tests/e2e/admin-core.spec.ts`，结果 `4 passed`。

## 9. 发布后观察窗口

建议至少观察以下内容：

1. Go `/health` 持续正常。
2. Python `/health` 持续正常。
3. 任务成功率无明显下降。
4. 问答成功率、失败率、延迟分布无明显恶化。
5. 日志中的 `error`、`warn` 无异常放量。

## 10. 操作记录要求

每次发布或故障处理，至少记录：

1. 操作时间。
2. 操作人。
3. 影响范围。
4. 执行命令。
5. 结果摘要。
6. 关键 `trace_id`、`job_id`。
7. 是否需要回滚。

## 11. 关联文档

1. [GO_PYTHON_MIGRATION_STATUS.md](GO_PYTHON_MIGRATION_STATUS.md)
2. [BACKEND_BOUNDARY_FINAL.md](BACKEND_BOUNDARY_FINAL.md)（当前职责冻结）
3. [ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md](ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md)
4. [当前能力对账](ENTERPRISE_IMPLEMENTATION_BACKLOG.md)
5. [代码回滚演练](ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md)
6. [M5 v3 迁移与回滚条件](ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md)

历史引用 `GO_PYTHON_HYBRID_BOUNDARY.md`、`GO_DEFAULT_ENTRY_EXECUTION_PLAN.md`、`GO_PYTHON_DELIVERY_CLOSURE_CHECKLIST.md`、`ENTERPRISE_RELEASE_TEMPLATE.md` 本轮未找到；不继续保留失效链接，也不补写不存在的文档。
