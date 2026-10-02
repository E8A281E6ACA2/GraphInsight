# AGENTS.md

本文件是 GraphInsight 项目的协作与工程规范。所有 AI agent 和开发者在修改本仓库前都应先阅读本文件。

## 项目目标

GraphInsight 是知识图谱可视化与企业级后台管理系统。当前主线是把后台从“可用功能集合”升级为“可运营控制台”，重点包括权限、任务中心、知识库治理、问答质量、监控告警和交付质量。

相关规划文档：

1. `docs/ENTERPRISE_ADMIN_BLUEPRINT.md`
2. `docs/ENTERPRISE_ROADMAP_CHECKLIST.md`
3. `docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md`
4. `docs/ENTERPRISE_BACKEND_API_SPEC.md`

## 本地环境

前端必须使用 nvm 管理 Node.js。Vite 7 要求 Node `20.19+` 或 `22.12+`，本项目当前使用 Node `22.22.2`。

首次进入项目：

```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm install
nvm use
node -v
npm -v
```

期望版本：

```text
node v22.22.2
npm 10.x
```

不要在 `~/.npmrc` 中设置 `prefix` 或 `globalconfig`，这会和 nvm 冲突。可以保留 registry 镜像源。

## 常用命令

前端：

```bash
cd frontend
npm install
npm run build
npm run lint
npm run e2e:install
npm run e2e
```

前端 E2E 推荐使用项目脚本，它会优先检查后端健康，并自动补齐 Playwright 运行库：

```bash
ADMIN_TOKEN=*** ./frontend/tests/run_admin_e2e.sh
```

后端语法检查：

```bash
backend/.venv/bin/python -m py_compile backend/core/observability.py backend/api/routes/doc_qa.py
```

后端接口烟测需要先启动统一后端，并确保 Go 网关可访问。默认入口以 `logs/dev/runtime.env` 中的 `GO_BASE_URL` 为准。

```bash
scripts/dev-backend.sh up
backend/.venv/bin/python backend/tests/run_unified_boundary_guards.py
backend/.venv/bin/python backend/tests/run_backend_smoke_suite.py
backend/.venv/bin/python backend/tests/verify_admin_authz.py
backend/.venv/bin/python backend/tests/check_documents_soft_delete_flow.py
backend/.venv/bin/python backend/tests/check_jobs_api.py
backend/.venv/bin/python backend/tests/check_job_reindex_and_observability.py
backend/.venv/bin/python backend/tests/check_qa_traces_api.py
```

## Git 提交规范

提交信息使用 Conventional Commits 风格：

```text
type(scope): subject
```

允许的 `type`：

1. `feat`：新增功能
2. `fix`：修复问题
3. `docs`：文档变更
4. `refactor`：重构，不改变外部行为
5. `test`：测试相关
6. `chore`：工具、依赖、杂项维护
7. `build`：构建系统或依赖变更
8. `ci`：CI 配置变更
9. `perf`：性能优化

示例：

```text
feat(admin): add qa quality metrics
fix(frontend): use same-origin api base
docs(enterprise): update sprint acceptance record
chore: sync project updates
```

提交要求：

1. 一个提交只表达一个清晰意图。
2. 不提交密钥、token、密码、数据库 dump、日志大文件。
3. 不把格式化、重构、功能改动混在同一个提交里。
4. 数据库结构变更必须带迁移脚本和回滚说明。
5. 企业级改造相关变更要同步更新 `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` 或对应验收文档。

## 代码质量底线

为了避免代码逐渐失控，修改时遵守以下规则：

1. 先读现有实现，再改代码。优先复用项目已有的服务、schema、响应结构和权限依赖。
2. 改动要小而完整。不要顺手做无关重构。
3. 不新增“临时万能函数”“大而全 service”“随手复制的分支逻辑”。重复出现三次以上再抽象。
4. 后台 API 使用 `/api/v1/admin/*`，业务 API 使用 `/api/*`。
5. 后台写操作必须有鉴权、审计字段和可追踪 `trace_id`。
6. 重操作优先进入任务中心，不让前端依赖长请求等待。
7. 响应体保持统一：`code`、`message`、`data`、`timestamp`、`trace_id`。
8. 错误要返回明确类型和状态码，不吞异常，不只写 `except Exception: pass`。
9. 新增配置项要考虑脱敏、默认值、环境变量来源和后台展示。
10. 新增前端页面要接入现有 `AdminLayout`、`adminService`、`types/admin.ts`，不要绕过统一 API 客户端。

## 后端规范

1. 路由层只做参数转换、权限声明和响应包装。
2. 业务逻辑放到 `backend/admin/services` 或 `backend/services`。
3. 数据访问放到已有 CRUD 层或服务层，不在路由里堆 SQL。
4. Pydantic schema 要放到对应 `schemas` 模块，避免在多个文件重复定义接口形状。
5. 权限使用 `require_admin_permission` 或 `require_permission`，不要手写 token 解析。
6. 日志使用 `get_logger()`，敏感信息必须脱敏。
7. 后端变更至少做语法检查；涉及接口时补充 smoke 脚本或更新已有脚本。

## 前端规范

1. TypeScript 类型先放到 `frontend/src/types`，API 调用统一放到 `frontend/src/services`。
2. 管理后台页面保持操作型界面风格：信息密度适中、状态明确、按钮行为可预期。
3. 不在组件里硬编码接口 base URL，统一使用 `API_BASE_URL` 和已有 axios client。
4. 不在 UI 中展示完整 token、api key、password。
5. 新增后台模块要考虑 loading、empty、error、success 四种状态。
6. 提交前运行 `npm run build`。如果只改样式或文档，也至少确认 TypeScript 没有新增错误。

## 文档与验收

企业级改造每完成一个阶段或 backlog 项，必须更新对应文档：

1. 路线状态：`docs/ENTERPRISE_ROADMAP_CHECKLIST.md`
2. Backlog 状态：`docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md`
3. API 变化：`docs/ENTERPRISE_BACKEND_API_SPEC.md`
4. 验收记录：`docs/ENTERPRISE_SPRINT*_ACCEPTANCE.md`
5. 发布说明：参考 `docs/ENTERPRISE_RELEASE_TEMPLATE.md`

文档要写事实状态，不写“计划已完成”这种无法验证的表述。

## 安全规则

1. 不读取、输出、提交 `.env` 中的敏感值。
2. 不把用户 token、管理员密码、API key 写入文档、日志或测试输出。
3. 涉及删除、清空、重建的功能必须支持确认、dry-run 或可恢复策略中的至少一种。
4. 权限绕过只能用于本地诊断脚本，不能进入生产路由。

## 工作流

建议每次开发按以下顺序推进：

1. 读规划和相关代码。
2. 明确最小交付面。
3. 小步修改。
4. 本地验证。
5. 更新文档状态。
6. 按提交规范提交。

## 审计与交付汇报口径

涉及后台任务、知识库索引或企业级 backlog 时，交付汇报必须把以下三类证据分开写：

1. 隔离测试：说明测试使用的数据库、外部服务替身和通过数量；隔离测试只能证明代码合同与状态机，不得表述为生产闭环。
2. 真机取证：分别记录真实 Neo4j、Milvus、Go/Python 运行态和写入范围；未实际连接或未写入时必须明确写“未取证”。
3. 生产闭环：只有在真机取证、任务状态收敛、重跑幂等和文档验收记录均具备后，才可使用 CLOSED；否则使用 CLOSED_DEGRADED、待取证或未验收。

审计人员话术至少包含：提交号、测试入口与 exit code、已验证边界、未验证边界、已知风险、是否 push，以及是否触碰共享 dev。不得把 mock、静态检查、构建或服务健康检查单独表述为已完成生产验收。
