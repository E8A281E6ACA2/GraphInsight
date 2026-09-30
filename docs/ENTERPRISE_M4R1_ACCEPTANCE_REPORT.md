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

## 8. Release Gate R1 进度（2026-09-30）

### 8.1 项3：版本级回滚演练（已完成，含独立复核）

完整记录见 `docs/ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md`。要点：

- 八条腿（4 个版本 × enforce/soft 双模式）全部用 `git archive` 提交态源树 + 容器内交叉编译 +
  `docker top` 进程核对建立二进制身份链；探针集合与判定阈值全程未改。
- 结果：`4f4f205`、`78b1f28` 双模式 15/15；`45db134` enforce 15/15 但 soft 14/15（伪造 `x-auth-user-name`
  未认证请求返回 200）；`a053532` enforce 6/15、soft 5/15。恢复态对常驻网关复探 15/15。
- 结论口径：**代码级可回滚下限 = `78b1f28`**；`a053532` 与 `45db134` 不可作为回滚目标。
  仅跑 enforce 会把下限误判为 `45db134`，因此门禁要求 enforce + soft 双模式各一腿。
- 数据库侧：11 表 schema 两个二进制互用，八条腿零 DDL、网关零 ERROR → 本段改造代码回滚与库表回滚解耦，
  无需 down-migration。
- 恢复动作已复核：`docker ps -a --filter name=gi-drill-gw` 计数 0；常驻网关 `RestartCount=0` 且 Cmd 未变。

本轮曾纠正一处自身失误：首次只跑 enforce 腿时差点把回滚下限写成 `45db134`，补 soft 腿后被黑盒探针证伪。
另一次失误是腿执行器取错口令文件（`gi_fresh_pw` 而非 `gi_fresh_pw2`），表现为当前版本腿 `401`，
经直接对常驻网关双口令比对定位为操作侧问题，与代码无关。

### 8.2 项1/项2：CI `workflow_dispatch` 自包含发布验收（阻塞，需用户凭据）

- `.github/workflows/ci.yml` 的 `workflow_dispatch` 入参与开工令要求一致
  （`run_release_acceptance`、`frontend_e2e_spec=business-docqa-flow.spec.ts`、`perf_probe_preset=release`、
  `perf_probe_requests=20`、`perf_probe_concurrency=4`），CI 侧自包含栈 job 已在 `66e1d18` 落地。
- 实跑阻塞点：本机 `gh` 2.101.0 已安装但 `gh auth status` = "not logged into any GitHub hosts"，
  环境变量 `GITHUB_TOKEN`/`GH_TOKEN` 均未设置，因此**无法在不获取用户凭据的前提下触发远端流水线**。
  本项不做任何绕过（不改阈值、不改验收口径、不把本地结果冒充 CI 结果）。
- 需要用户执行：`gh auth login`（或提供可用的 `GITHUB_TOKEN`），随后触发并回填 run 链接与产物。

### 8.3 项4：soak / capacity（2026-09-30 已跑单点，阈值先声明）

- 阈值与参数在执行前写入 `docs/ENTERPRISE_PERF_SOAK_2026_09_30.md` §1 并声明"事后不回改"：
  `preset=release`、`rounds=3`、`requests=20`、`concurrency=4`、`max_error_rate=0.0`（与发布验收同口径）、
  `max_p95_ms=0`（沿用"0 即关闭"，本轮不新增延迟门禁）。
- 实跑（隔离统一栈 `127.0.0.1:18090`，Go 代码与 HEAD 等价）：360 请求零失败，
  `SOAK_SUMMARY rounds=3 failed_rounds=0`、`route_owner_check=true`；表内数值逐个与 `summary.json` 复核一致。
- 如实记录的限制：隔离栈无 embedding/LLM 配置，`docqa` 检索为空（`citations=0`），故 p95 不代表真实模型
  生成时延；`graph-build` 为"提交即取消"，只测受理路径。真实模型容量与 4→8→16 递增 capacity 仍待补。
- 为让 soak 能在本机复跑，修了 `backend/tests/run_perf_soak.py` 两处平台假设：不再硬要求
  `backend/.venv/bin/python`（缺失时回退当前解释器，支持 `--python` / `SOAK_PYTHON`），
  子进程输出显式按 UTF-8 捕获（沿用整改2 的口径，避免中文输出触发 cp936 解码失败）。

## 9. Release Gate R2 整改轮（2026-09-30，上级 6 项）

R1 轮不验收，按 6 项继续整改。本轮口径先说清楚：**所有 `docqa` 相关判据只声明为"检索/引用链路
smoke"（无 embedding / LLM 配置），不构成问答质量结论**；CI 实跑类证据仍受 §8.2 的凭据阻塞，
本轮不以本地结果冒充 CI 结果。

### 9.1 项1：CI 敏感信息输出 / 上传整改

泄露面审计结论：真正带凭据的产物是 **Playwright HTML 报告内嵌的 trace / video**（`admin-core.spec.ts`、
`business-docqa-flow.spec.ts` 里有 `localStorage.setItem('admin_token', token)`，trace 会记录
`addInitScript` 与登录请求体），而原 scrub 只删 `frontend/test-results` 原件，HTML 报告副本不受影响；
`frontend-e2e`、`release-frontend-e2e` 两个 job 甚至完全没有 scrub。GitHub 只自动 mask 日志里的
Secrets，**不 mask 二进制产物**。

整改四层：

1. `frontend/playwright.config.ts`：CI 下 `trace`/`video` 改为 `off`（本地仍 `retain-on-failure`）。
2. `ci.yml` 所有报告类 job 的 scrub 扩到 `frontend/playwright-report`，并打印 `residual=` 复核计数。
3. 新增 `backend/tests/check_artifact_secrets.py` 上传前门禁：扫 `jwt` / 带口令 DSN / 凭据赋值 /
   bcrypt 哈希四类形状，并对本次 run 注入的口令做字面值比对；命中即 `clean=false`，对应
   upload step 用 `if: always() && steps.secret_scan.outputs.clean == 'true'` 不发布产物
   （失败仍可调试，含密产物不流出）。fixture 放行必须显式 `--allow-fixture` 声明并打进日志，
   不静默放宽；扫描器只输出 `match_sha256` 与脱敏片段，不复读原文。
4. `release-acceptance` 不再 `cat logs/dev/runtime.env`，只 `grep` `*_BASE_URL`；
   "该文件只允许承载 `*_BASE_URL`"由 `check_dev_runtime_defaults.py` 断言（新增
   `non_url_keys` 为空的前置检查）。`check_dual_kb_blackbox.py` 与 `check_dev_runtime_defaults.py`
   的失败消息不再回显带口令的 DSN，改为 `_dsn_target()` / `_db_target_redacted()` 只留 `host:port/db`。

实测：脏目录 4 条 finding（含 run 口令字面值与 JWT 形状）→ exit 1；干净目录 `findings=0 result=pass`
→ exit 0；`ci.yml` 经 `yaml.safe_load` 解析为 11 个 job。

### 9.2 项2：Python / Go RBAC 权限目录精确对账

两侧各一条腿，任一侧漂移都能被判：

| 文件 | 视角 | 覆盖 |
|---|---|---|
| `backend/tests/check_rbac_catalog_parity.py` | Python 读 Go 种子 | 16 条检查：权限码集合、字段、角色名、描述、逐角色授权、强制码必须是已注册码、`graph:admin` 只授 super_admin 且强制于 `/api/query`、预留 `kb:review/kb:manage/kb:publish` 在目录中但不授予也不强制 |
| `go-backend/internal/adminstore/rbac_seed_parity_test.go` | Go 读 Python 种子 | 4 个测试，同一套判据反向自证；找不到 Python 源文件时 `t.Fatalf`，对账腿不允许静默跳过 |

接入点：前者进 `run_unified_boundary_guards.py`（`SUMMARY total=15 failed=0`），后者进
`go test ./...`。负向自证：改 role description、给 viewer 授 `kb:review`、把 `job:manage`
换成 `screenshot:read`，Python 腿得到 4 条 FAIL / exit 1，Go 腿 3 个测试 FAIL，还原后复绿。

### 9.3 项3：enforce + soft 双模式纳入可重复回滚验收

新增 `backend/tests/run_rollback_matrix.sh`（编排）+ `backend/tests/run_rollback_leg.sh`（单腿起停），
并接入 CI `rollback-matrix` job（`workflow_dispatch` + `run_rollback_matrix`，默认版本串 `HEAD,78b1f28`）。
执行器前置硬门禁：缺任一授权模式直接 `MATRIX_PREREQ_INVALID`（exit 2）。

本轮实跑 3 版本 × 2 模式 = 6 腿（隔离栈，明细见 `docs/ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md` §10）：

```text
ROLLBACK_MATRIX_SUMMARY versions=3 modes=2 legs=6 legs_passed=5 legs_failed=1
detail=HEAD/enforce=pass HEAD/soft=pass 78b1f28/enforce=pass 78b1f28/soft=pass
45db134/enforce=pass 45db134/soft=fail
```

`45db134/soft` 唯一失败断言是 `forged_identity_header_rejected status=200`，同版本 enforce 腿为 401 ——
即门禁确实能抓到"只在 soft 配置下泄漏"的一类缺陷，回滚下限 `78b1f28` 由此可重复成立。

### 9.4 项4：rollback state 文件保护与隔离清理

- 探针强制 state 路径在仓库外（`git rev-parse --show-toplevel` 判定），写入 `0o600` 并回读校验；
  POSIX 下组/其他可读即失败，Windows 打印 `posix_enforced=0` 如实说明权限位不生效。
  证据：`STATE_FILE_PROTECTED path=C:\...\gi_drill_state2.json mode=0o666 posix_enforced=0`；
  Docker（POSIX）内实测 `POSIX_GATE_REJECTS_WORLD_READABLE exit=2`、仓库内路径被 `DRILL_PREREQ_INVALID` 拒。
- `.gitignore` 补 `drill_state.json` / `*.drill_state.json` / `.gi_drill_state*` 兜底。
- 腿级清理由执行器 `trap EXIT` 负责，矩阵结束后复核 `docker ps -a --filter name=gi-drill-mx` = 0、
  18091–18096 无监听、`git status --porcelain` 无 state 残留；CI 侧 "Verify leg cleanup" step 以
  `pgrep -f 'drill-build/.*/bin/api'` 必须为 0 作为硬失败条件。

### 9.5 项5：无 LLM 口径收敛

回滚探针逐腿打印 `NOTE main_docqa_scope=link_smoke_only llm_configured=0`；soak 文档 §6 与回滚文档
§10.4 都把该口径写成"检索/引用链路 smoke，不宣称问答质量"，并声明回滚矩阵不产出性能判据。

### 9.6 项6：临时性能阈值落档

`docs/ENTERPRISE_PERF_SOAK_2026_09_30.md` 新增 §6 台账：CI `llm_disabled` 档 `max_p95_ms=3000`、
`llm_configured` 档 `20000` 均标为**临时**，写明依据（本地实测 p95 最大值 × 9 冷 runner 余量 / 生成链路先验）
与复审触发（CI 累计 3 轮成功后重定；配置真实模型后立即失效）。`max_error_rate=0.0` 保持长期不放宽。

### 9.7 本轮新增限制

`go-backend` 含 `syscall.Statfs` 等 Linux 专属调用，`GOOS=windows` 编译失败
（`internal/httpserver/admin_monitor_native.go: undefined: syscall.Statfs`）。因此候选版本二进制必须在
Linux 构建，本地 Windows 只能验证执行器起停编排（已用桩二进制跑通 START/STOP/前置校验 6 个用例），
真实网关行为由容器腿证据承担。

### 9.8 R2 状态

- [x] 项1 CI 泄露面整改（配置 + scrub + 上传门禁 + runtime.env 最小回显）
- [x] 项2 双语言 RBAC 目录对账（含负向自证）
- [x] 项3 enforce+soft 双模式可重复矩阵（执行器 + CI job + 本地 6 腿实跑）
- [x] 项4 state 保护与隔离清理（含 POSIX 实证与残留复核）
- [x] 项5 无 LLM 口径统一为检索/引用链路 smoke
- [x] 项6 临时性能阈值与复审触发落档
- [x] 项7 提交与 fast-forward push（见 §9.9）
- [ ] 项8 CI 侧证据（workflow run URL、`ACCEPTANCE_SUMMARY`、Playwright exit、artifacts 链接、
  敏感信息扫描结果）仍受 §8.2 凭据阻塞，未取得不申报

### 9.9 R2 收口提交（2026-09-30，已 fast-forward push 并权威源核验）

本地提交四个，均基于上一轮 `3dac3e1`，push 为 fast-forward
（`3dac3e1..def7bf9 main -> main`），核验结果与权威源一致：

- `080fdfe` fix(ci): seed E2E account before rollback fixtures and fix residual scans
- `380a169` test(rbac): thread explicit KB scope through backend smoke scripts
- `d02591b` fix(admin): project only bound columns in first-user migration
- `def7bf9` fix(e2e): fall back to real gateway URL for same-origin/auto base

本地基线确认：`git rev-parse HEAD == git ls-remote origin refs/heads/main == def7bf9273ceaad6b18da17600532e0a24e5d485`。

push 后 run#24（event=push，sha=def7bf9）自动 CI 全绿：
Backend smoke script syntax、Go backend tests、Backend unified boundary guards、
Frontend build 均 `conclusion=success`；发布验收类 job 依赖
`run_release_acceptance=true` dispatch 输入，push 路径按设计 skip。

run#23 失败根因已定位并修复：rollback matrix `Establish drill fixtures` 腿
`SETUP_BLOCKED login status=401 error_code=INVALID_CREDENTIALS`，因为夹具阶段先于
E2E 账号 seed 执行；`080fdfe` 在夹具前插入 seed 步骤。新 dispatch 未跑前，
run#23/#22 失败不作为通过证据，项8 不申报完成。
