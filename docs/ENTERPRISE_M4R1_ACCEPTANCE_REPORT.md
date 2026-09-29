# M4-R1 / strict 启用 验收报告（2026-09-29，供专家复审）

## 1. 范围与口径

对应上级复审三个阻断项：

1. KB-scoped 授权在 `RBACEnforceBusinessAPI=false` / `local_jwt_soft` 下必须独立 fail-closed，补 soft 配置跨 KB 负向测试，确认拒绝不触达 Python / Graph / QA trace store。
2. E2E 中 trace list、job detail、trace detail 三处请求补齐 `kb_id`，并在真实统一活栈跑通 Playwright E2E。
3. 迁移 smoke 的 Windows UTF-8 捕获修复（明确 UTF-8 运行命令与真实输出）。

strict 语义：代码中不存在 `KB_SCOPE_ENFORCE` 开关或 default KB 兜底，fail-closed 为唯一形态；第一阶段通用 RBAC soft 语义（store 不可用/错误/拒绝软放行、local_jwt_soft）按设计保留，不受第二阶段 KB 授权影响。该口径由 `backend/tests/check_migration_cleanup_guards.py::test_kb_scope_strict_mode_has_no_compat_toggle` 静态守卫防削弱。

## 2. 提交清单（本地 main，领先 origin/main 8 个提交，待 push）

| 提交 | 说明 |
|---|---|
| `ceddb83` | chore(repo): 行尾规范化（.gitattributes LF）与 ignore 文件 |
| `45db134` | feat(scope): Go 控制面 KB 作用域强制（含全部 Go 新增测试与 gofmt 修复） |
| `9a11561` | feat(scope): Python 作用域基座与 KB 迁移（含静态守卫、seed 脚本、迁移 smoke） |
| `bfa563c` | feat(scope): Python 路由与服务 kb_id 适配 |
| `55400b1` | feat(frontend): KB 选择器与作用域感知 API 传输 |
| `95c20db` | test(enterprise): 跨 KB fail-closed 守卫套件与 E2E 作用域改造 |
| `80f5056` | docs(enterprise): strict 启用记录、复跑前提、M4 关闭 |
| `4fac798` | chore(repo): 忽略 go-backend 构建产物目录 |

## 3. 阻断项逐项证据

### 3.1 KB 授权独立 fail-closed

- 实现：`go-backend/internal/httpserver/authz_middleware.go`（`checkPermissionWithScope`：错误→拒绝；`!Allowed`→无论 enforce/soft 一律拒绝）；`qa_scope.go`（`authorizeQAEffectiveKBIDs` 解析授权集合失败→503 fail-closed；交集为空→403+审计；`authorizeQAKBScopedRequest` 全链路拒绝即返回，不转发 Python）。
- 测试：`m4r1_kb_failclosed_soft_test.go` 6 用例（soft GoDB 与 local_jwt_soft 两形态下的 retrieval diagnostics / QA trace list / QA trace detail / graph schema / job 读作用域跨 KB 拒绝），断言 `traceStore.listQuery`、`detailKey`、`discoverSchemaCalls`、Python 转发计数为零——拒绝不触达下游。
- 复跑结果（Docker `golang:1.24.13-bookworm`，LF 全新检出）：`go test ./...` 8 包全 ok，`httpserver` 包含上述用例全 PASS。

### 3.2 E2E 三处 kb_id 与活栈跑通

- `frontend/tests/e2e/business-docqa-flow.spec.ts`：trace list（约 :280）、job detail（约 :312）、trace detail（约 :453）三处请求参数均携带 `kb_id`（注释标记 M4-R1 审计 P1）。
- 真实统一活栈（Go 网关 :18082 Docker、Python 能力层 :8001、PostgreSQL :5434、Neo4j :7687、Milvus :19531）终跑：`5 passed / 3 skipped / PW_EXIT=0`（9.5m），业务全链路 upload→build→ask→trace→delete 单条 3.3m 通过；3 个 skip 为需真实密码的 UI 登录用例，token 注入用例已覆盖认证路径。

### 3.3 迁移 smoke Windows UTF-8

- `backend/tests/check_kb_migrations_smoke.py`：`_utf8_env()` 强制 `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`，子进程捕获 `encoding='utf-8', errors='replace'`。
- 复跑命令与真实输出：`python -X utf8 backend/tests/check_kb_migrations_smoke.py` → 12 项全 ✓，`all migration smoke checks passed`，EXIT=0（Windows Git Bash + 系统 Python 3.14）。

## 4. 全量复跑矩阵（2026-09-29，全新 LF 检出 + 真实活栈）

| 套件 | 结果 |
|---|---|
| gofmt（LF 检出，容器内） | 零伪报 |
| go build / go vet / go test ./... | 全绿，8 包 ok |
| check_migration_cleanup_guards.py | MIGRATION_CLEANUP_GUARDS_OK |
| check_m4r1_scope_guards_unit.py | passed=15 failed=0 |
| check_kb_migrations_smoke.py（UTF-8） | 12✓ EXIT=0 |
| run_unified_boundary_guards.py | total=14 failed=0 |
| check_dual_kb_blackbox.py | passed=15 failed=0 |
| check_kb_scope_isolation.py | passed=54 failed=0 |
| Playwright E2E | 5 passed / 3 skipped / PW_EXIT=0 |

## 5. 复跑前提（Windows）

1. 管理库须存在至少一个 active KB；缺数据先跑 `backend/scripts/seed_e2e_local_stack.py`（须设 `SEED_ADMIN_PASSWORD`；seed 与登录取 token 须在同一条 shell 内完成）。
2. Go 网关与 Python 能力层（8001）必须同时在线；Go `/health` 的 `python_backend.connected=true` 仅代表代理客户端初始化成功，不探测上游。
3. E2E 启动环境：`E2E_BROWSER_CHANNEL=chrome`、`VITE_API_BASE_URL=http://127.0.0.1:18082`、`E2E_API_BASE_URL=http://127.0.0.1:18082`、`E2E_ADMIN_TOKEN=<登录 token>`；漏设 `VITE_API_BASE_URL` 时 vite 会把 /api 代理到默认 8081（本机被无关服务占用）导致鉴权类用例假失败。
4. Windows 检出为 CRLF（core.autocrlf），容器内 `gofmt -l` 会全量伪报；以 LF 规范化检出为准（入库 blob 均为 LF）。
5. Go 依赖在受限网络下走 `gomodulecache` Docker 卷，避免直连 proxy.golang.org。

## 6. 已知边界与非阻断项

- 3 个 UI 登录 E2E 用例 skip（需人工密码），认证路径由 token 注入用例覆盖。
- 嵌入端点 `api.vectorgate.ai` TLS SSL 错误为外部网络问题；检索走关键词模式，不影响本轮验收面。
- push 尚待执行（本机无 GitHub 网络出口），复审可先基于本地提交或待 push 后以远端为准。
