# M4-R1 / strict 启用 验收报告（2026-09-29，供专家复审）

## 1. 范围与口径

对应上级复审三个阻断项：

1. KB-scoped 授权在 `RBACEnforceBusinessAPI=false` / `local_jwt_soft` 下必须独立 fail-closed，补 soft 配置跨 KB 负向测试，确认拒绝不触达 Python / Graph / QA trace store。
2. E2E 中 trace list、job detail、trace detail 三处请求补齐 `kb_id`，并在真实统一活栈跑通 Playwright E2E。
3. 迁移 smoke 的 Windows UTF-8 捕获修复（明确 UTF-8 运行命令与真实输出）。

strict 语义：代码中不存在 `KB_SCOPE_ENFORCE` 开关或 default KB 兜底，fail-closed 为唯一形态；第一阶段通用 RBAC soft 语义（store 不可用/错误/拒绝软放行、local_jwt_soft）按设计保留，不受第二阶段 KB 授权影响。该口径由 `backend/tests/check_migration_cleanup_guards.py::test_kb_scope_strict_mode_has_no_compat_toggle` 静态守卫防削弱。

## 2. 提交清单（origin/main 与本地 main 已一致，远端哈希与核验判据见 §7.6；下表 16 个提交为 M4-R1 交付面）

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
| `5d5a245` | docs(enterprise): 新增本轮 strict 启用验收报告（供复审） |
| `78b1f28` | fix(httpserver): 封堵 KB 作用域授权剩余放行例外（含身份头剥离、拒绝审计、异常场景测试） |
| `6705e25` | fix(tests): 统一边界守卫子进程输出按 UTF-8 捕获 |
| `e4eccd8` | chore(repo): 忽略 Python 能力层 8001 运行态输出文件 |
| `3df2bd5` | docs(enterprise): 记录复审整改轮与复跑证据 |
| `d392fb0` | docs(enterprise): 补记身份头剥离后的统一活栈与 E2E 复跑结果 |
| `a268a1e` | docs(enterprise): 记录 push 出口复测与提交计数 |
| `f8fbe29` | docs(enterprise): 记录 push 完成与远端一致性核验 |

上表 16 个提交即 M4-R1 交付面。本报告的收口修订（把"待 push"改写为已核验状态）作为第 17 个提交随后推送，最终远端哈希以 §7.6 的 `git ls-remote` 记录为准。

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
- push 已完成并通过远端一致性核验（见 §7.6）；GitHub 出口在本机是间歇可用，命令超时需重试而非判定"无出口"。

## 7. 复审整改轮（2026-09-30，专家 5 项）

### 7.1 KB 授权剩余放行例外封堵

- `checkPermissionWithScope`：删除"无主体→放行"与"soft 且 store 为 nil→放行"两处例外，第二阶段 KB 授权无条件 fail-closed。
- `authorizeQAEffectiveKBIDs`：无主体→401 `UNAUTHORIZED`（写拒绝审计）；store 未实现 `qaScopeAuthorizer`→503 `AUTHZ_UNAVAILABLE`；`AuthorizedKBIDs` 出错→503；交集为空→403 `KB_ACCESS_DENIED`。
- 拒绝审计补齐：`adminStore` 为 nil、KB 目录不可用、KB 行加载失败、`KB_NOT_FOUND` 均写 `authz_denied`，`details.error_code` 区分实际拒绝码（此前固定为 `KB_ACCESS_DENIED`）。
- 身份头信任边界（复审发现的额外放行面）：`allowRequestWithScope` 入口先剥离 `x-authz-permission`、`x-authz-reason`、`x-auth-user-id`、`x-auth-user-name`、`x-auth-user-email`，只允许认证中间件重新写入。修复前 soft 无 token 请求可自带 `x-auth-user-name` 伪造主体进入二阶段授权，"无主体 fail-closed"实际可被绕过。
- 新增 `m4r1_kb_exceptions_test.go`：覆盖无主体（go_db soft / go_db enforce / local_jwt_soft）、nil store、store 不支持接口、授权解析出错、DocQA/NL2Cypher handler 级无 token 与跨 KB、伪造主体头回归、`checkPermissionWithScope` 无主体与 nil store、graph schema soft 无 token；Python 侧用"被调用即 fail"的 orchestrator 客户端断言拒绝路径零触达。
- 既有正向测试适配：KB-scoped 与管理员写路由用例改为携带合法 JWT（不再依赖客户端伪造 `x-auth-user-id`），断言未认证来源的 operator/requested_by 不被采纳。

### 7.2 统一边界守卫 UTF-8 捕获

`backend/tests/run_unified_boundary_guards.py::_run_case` 增加 `_utf8_env()`（`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`）与 `encoding="utf-8", errors="replace"`，Windows 默认码下子进程中文输出不再乱码。

### 7.3 临时产物与计数

`.py8001.err`、`.py8001.out` 纳入 `.gitignore`（原始文件保留未删）；§2 提交清单随本轮整改提交同步。

### 7.4 本轮复跑证据（2026-09-30）

| 套件 | 结果 |
|---|---|
| gofmt（容器内去 CR 后校验改动文件） | 零伪报 |
| `go test ./... -count=1`（Docker golang:1.24.13-bookworm） | 8 包全 ok |
| `run_unified_boundary_guards.py`（`python -X utf8`） | total=14 failed=0，中文输出正常 |
| `check_kb_migrations_smoke.py`（`python -X utf8`） | 12✓ EXIT=0 |

注：不带 `-X utf8` 直接跑 `check_kb_migrations_smoke.py` 时，父进程在 GBK 控制台打印 `✓` 会 `UnicodeEncodeError`；这是运行命令前提（见 §5），子进程捕获已修复，不在本轮改动面内。

#### 7.4.1 统一活栈复跑（身份头剥离改动后）

前提：`go-backend/bin/api-linux` 用含 7.1 改动的源码重新交叉编译后 `docker restart graphinsight-go-gateway`；`/health` 显示 `enforce_business_api:true`、`rbac_authz_mode:go_db`；Python 能力层 :8001 在线；PostgreSQL :5434 / Neo4j :7687 / Milvus :19531 可达。

| 验证 | 结果 |
|---|---|
| `check_dual_kb_blackbox.py` | passed=15 failed=0 skipped=0 |
| `check_kb_scope_isolation.py` | passed=54 failed=0 |
| `GET /api/graph/schema?kb_id=<active KB>` + 合法 JWT | 200 |
| 同上，KB 不存在 | 404 `KB_NOT_FOUND` |
| 同上，缺 `kb_id` | 400 `KB_SCOPE_REQUIRED` |
| header `X-KB-ID` 与 query `kb_id` 不一致 | 400 `KB_CROSS_SCOPE` |
| 无 token 但伪造 `x-auth-user-name` / `x-auth-user-id` | 401 `UNAUTHORIZED`（7.1 剥离生效，绕过面已实测封堵） |
| `POST /api/docqa` + JWT + `kb_id` | HTTP 200，`code 200`、answer 非空 |
| Playwright E2E（`E2E_BROWSER_CHANNEL=chrome`，前端 4173 → 网关 18082） | 5 passed / 3 skipped / EXIT=0（3.8m），与基线一致；业务全链路 upload→build→ask→trace→delete 单条 3.6m 通过 |

E2E 的 3 个 skip 为需真实密码的 UI 登录用例（登录/登出/偏好回跳），认证路径由 token 注入用例覆盖；与上一轮基线完全同集合，非新增回归。

### 7.5 勘误：seed 脚本"密码哈希未落库"系本人误判（已排除）

7.5 原先记为独立待办的"播种未落库"经复查**不成立**，`backend/scripts/seed_e2e_local_stack.py` 行为正确，此处按事实更正：

- 复现步骤：同一 shell 内连续两次以不同口令播种，逐次读取 `password_hash` 并输出其 sha256 前 12 位 → `b5a4a3fee95b` → `6980c1f1d430`，摘要变化即证明 UPDATE 已提交；随后 `bcrypt.checkpw(本轮口令)=True`、`checkpw(上一轮口令)=False`，与"最后一次播种生效"完全一致。
- 端到端复核：播种后用该口令请求网关 `POST /api/v1/admin/auth/login` → HTTP 200、`code 200`、返回 JWT（非空）。
- 误判根因（两条叠加，均为操作侧）：
  1. 当时用 `substring(password_hash,1,7)` 比较前后两轮，得到恒定 `$2b$12$`——这是 bcrypt cost-12 的固定前缀，与内容无关，"前缀未变"不构成"未写入"的证据。
  2. 随机口令在其中一次调用生成、登录在另一次调用使用；托管 Bash 跨调用不保留 shell 变量，登录用的是从未播种过的口令，于是 `checkpw` 为 False 并被误读为写库失败。当时的手工 `UPDATE` 只是把同一个新口令再写一遍并让我记住了它，掩盖了真实原因。
- 结论与约束：脚本无需修改；`updated_at` 在播种后保持不变是预期行为（UPDATE 语句未更新该列）。活栈复跑取口令必须遵守"生成→播种→登录在同一条 shell 调用内完成"，且校验哈希变化要用整串或摘要比较，不得用 bcrypt 前缀。

### 7.6 整改 5：push 与远端一致性核验（2026-09-30，已完成）

- 前置说明：7.4 记录过本机 `git ls-remote` / `git push --dry-run` 连不上 `github.com:443`（约 21s 超时）。实测该出口是**间歇可用**而非彻底不通——同一命令重试即可建立连接，因此"本机无出口"的旧判断已被证伪，报告以最终成功的核验为准。
- 执行前确认：`git merge-base HEAD <远端 main>` 等于远端引用，push 为纯 fast-forward、不涉及强推与历史改写。
- 执行（两次 fast-forward，均 `PUSH_EXIT=0`）：
  1. `a053532..a268a1e`（整改 1–4 的全部代码、测试与验收记录，共 15 个提交）。
  2. `a268a1e..f8fbe29`（本报告补记推送与核验结果）。
- 一致性核验（整改要求的两条命令，逐字节 `cmp`）：
  - 第一次推送后：`git rev-parse HEAD` 与 `git ls-remote origin refs/heads/main` 均为 `a268a1ebdf1bcd9d357adbc93afe99fded5608cd` → `MATCH=yes`。
  - 第二次推送后：两者均为 `f8fbe294744862017404402c478d8842a6435c8e` → `MATCH=yes`；`git rev-list --count origin/main..HEAD` = 0；工作树干净。
  - 本报告收口后若再产生文档提交，以同组命令重跑为准（判据不变：远端引用与 `git rev-parse HEAD` 逐字节相等且 ahead=0）。
- 敏感面复查：推送区间 `a053532..a268a1e` 共 150 个文件，按 `\.env|token|secret|password|credential|dump|\.log$|\.py8001` 过滤仅命中 `backend/.env.example`，其新增键为 `EMBEDDING_PROVIDER/BASE_URL/API_KEY=` 空占位（模板文件，无真实凭据）。本地 `.e2e_token.tmp`、`.e2e_recheck.log`、`.py8001.*`、`go-backend/bin/` 均为未跟踪/忽略态，未进入任何提交。
