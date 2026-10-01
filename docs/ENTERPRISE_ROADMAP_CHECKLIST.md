# GraphInsight 企业级改造执行清单（Step-by-Step）

更新时间：2026-06-13
执行方式：按阶段推进，阶段内项目全部打勾后再进入下一阶段。

## 阶段 0：基线冻结与规范统一

- [x] 冻结当前 API 清单并生成接口基线文档。
- [x] 统一接口分层规则：`/api` 业务、`/api/v1/admin` 后台。
- [x] 统一错误码、响应格式、追踪字段（`trace_id`）。
- [x] 建立发布模板（变更说明、回滚说明、影响范围）。

## 阶段 1：身份与权限（企业入口）

- [x] 定义角色模型：`super_admin`、`project_admin`、`operator`、`viewer`。
- [x] 完成资源级权限：租户/项目/知识库三级授权。
- [x] 后台关键接口接入鉴权中间件。
- [x] 增加“权限变更审计日志”。
- [x] 完成权限回归测试（越权访问、最小权限、撤权生效）。

## 阶段 2：知识库管理（P0/P1）

- [x] 单文档删除（文件 + 图谱联动清理）。
- [x] 全量清空知识库（文件 + 图谱联动清理）。
- [x] 前端文档面板增加删除按钮与全量清空按钮。
- [x] 增加“软删除”与恢复窗口（P1）。
- [x] 增加 dry-run 删除预览（P1）。
- [x] 删除后自动回归校验（文档数、图节点数、关系数）。

## 阶段 3：任务中心（核心运营能力）

- [x] 建图任务异步化（创建任务、排队、执行、重试、取消）。
- [x] 建立任务状态机：`pending/running/succeeded/failed/cancelled`。
- [x] 后台任务面板：过滤、重试、失败原因、耗时统计。
- [x] 抽取日志与任务记录绑定（按 `job_id` 聚合）。
- [x] 失败任务自动重试策略（指数退避）。

## 阶段 4：问答中心与模型中心

- [x] 文档问答健康检查接口：`/api/docqa/health`。
- [x] 模型连通性测试（按模型与网关分组展示）。
- [x] 问答质量看板：命中率、引用率、失败率、延迟分布。
- [x] 问答链路追踪：问题 -> 检索片段 -> 模型响应。

## 阶段 5：监控与告警

- [x] 统一指标采集：API 错误率、建图成功率、问答可用率。
- [x] 日志分级与告警策略（error/warn 级别路由）。
- [x] 健康检查分层：系统、数据库、图数据库、模型网关。
- [x] 异常告警通道（邮件/IM Webhook）。
- [x] 建立 SLO 看板（P50/P95/P99）。

## 阶段 6：稳定性与交付质量

- [x] 后端关键路径自动化测试（上传、建图、删除、问答）。
- [x] 前端核心流程 E2E（上传->建图->问答->删除）稳定实跑通过。
- [x] 压测报告（建图并发、问答并发、查询并发）。
- [x] 生成运维手册（排障、回滚、应急流程）。
- [x] 生成上线验收清单并固化到 CI。

## 每阶段通用验收标准（DoD）

- [ ] 功能验收：需求闭环可演示。
- [ ] 回归验收：既有能力不回退。
- [ ] 观测验收：日志可追踪，异常可定位。
- [ ] 安全验收：权限覆盖、敏感数据脱敏。
- [ ] 文档验收：接口文档、变更记录、操作手册齐全。

## 建议迭代节奏（可直接执行）

1. 第 1-2 周：阶段 0 + 阶段 1
2. 第 3-4 周：阶段 2 + 阶段 3
3. 第 5-6 周：阶段 4 + 阶段 5
4. 第 7-8 周：阶段 6 + 全链路验收

## 当前优先级（下一步）

1. 在 CI 手动入口复跑完整 `release-acceptance`，确认新增数据库迁移回滚烟测与远端环境结果一致。
2. 基于发布基线继续扩展容量上限与 soak 测试。（2026-09-30 已跑通 release soak 3 轮并落档，遗留：并发
   4→8→16 递增 capacity 矩阵、带真实 embedding/LLM 配置的同口径复跑。）
3. 执行代码版本级回滚演练并写入发布记录。（2026-09-30 已实跑并落档，见"版本级回滚演练"一节；遗留项是
   把 soft 授权模式腿固化进 CI，并把回滚下限写入运维手册回滚章节。）
4. 继续推进高价值控制面细节 Go 原生化，但不再恢复 Python public business/admin surface。

## 最新联调记录（2026-09-30，Release Gate R1 项3/项4：回滚演练与 soak）

- [x] 演练口径：夹具只在当前版本用真实 HTTP API 建立一次（第二个 KB + 仅绑 `viewer@kb-b` 的低权限用户），
  探针断言在回滚前后完全不变，全程本地隔离栈（独立 Postgres + 一次性网关容器），未触碰生产、未 force push。
- [x] 版本身份链已做实：`git archive <sha>` 取提交态源树 → 容器内 `GOOS=linux CGO_ENABLED=0 go build` →
  宿主 `sha256sum` → `docker run` 一次性容器 → `docker top` 核对实际进程路径，避免"跑的不是那个版本"的伪证。
- [x] enforce（`RBAC_AUTHZ_MODE=go_db`）结果：`4f4f205` 15/15、`78b1f28` 15/15、`45db134` 15/15、
  `a053532` 6/15（9 条失败）；恢复态对常驻网关复探 15/15。
- [x] soft（`local_jwt_soft` + `RBAC_ENFORCE_BUSINESS_API=false`）结果：`4f4f205` 15/15、`78b1f28` 15/15、
  `45db134` 14/15（伪造 `x-auth-user-name` 未认证请求返回 200，即 `78b1f28` 关闭的旁路）、`a053532` 5/15。
- [x] 回滚下限判定：**代码级可回滚下限 = `78b1f28`**；`a053532`（pre-M4-R1 基线）与 `45db134` 均不可作为回滚目标。
  只跑 enforce 会误判 `45db134` 安全，必须 enforce + soft 双模式各一腿。
- [x] 迁移状态：11 表 schema 在 `a053532` 与 `4f4f205` 两个二进制间可直接互用，八条腿零 DDL、网关零 ERROR，
  即本段改造代码回滚与库表回滚解耦，无需 down-migration。
- [x] `a053532` 失败根因（源码级）：该版本 `go-backend` 中 `KB_SCOPE_REQUIRED|KB_CROSS_SCOPE|KB_ACCESS_DENIED`
  命中 0 条（`45db134`/`78b1f28` 各 34 条），且 Go 网关未注册 `/api/knowledge-bases`，
  回滚等于同时砍掉 KB 目录链路与全部作用域守卫。
- [x] 恢复动作核验：一次性容器 `docker ps -a --filter name=gi-drill-gw` 计数 0；常驻网关 `RestartCount=0`、
  Cmd 未变；仓库数据文件与临时 worktree 未被改写。
- [x] 完整记录见 [docs/ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md](ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md)。
- [x] soft 模式腿已固化成可重复执行器并接入 CI：`backend/tests/run_rollback_matrix.sh`（缺 enforce 或
  soft 任一腿即 `MATRIX_PREREQ_INVALID` 拒跑）+ `backend/tests/run_rollback_leg.sh`（单腿起停，
  非法授权环境变量与模式错配均 exit 2）+ `rollback-matrix` job（`workflow_dispatch`）。
  本地实跑 3 版本 × 2 模式：`legs=6 legs_passed=5 legs_failed=1`，唯一失败仍是
  `45db134/soft` 的 `forged_identity_header_rejected status=200`，同版本 enforce 腿 401（§10）。
  CI 首跑仍待 `workflow_dispatch`（本机 `gh` 未登录），不以本地结果冒充 CI 结果。
- [x] 回滚 state 文件保护：`run_rollback_drill.py` 拒绝仓库内路径、以 `0o600` 写入并回读校验，
  打印 `STATE_FILE_PROTECTED ... posix_enforced=`；`.gitignore` 兜底挡 `*drill_state*.json`。
  矩阵结束后复核容器 0 个、演练端口无监听、`git status` 无 state 残留。
- [x] soak/capacity 阈值与参数在执行前落档声明，执行后未回改：`preset=release`、`rounds=3`、
  `requests=20`、`concurrency=4`、`max_error_rate=0.0`（与发布验收同口径，不放宽）、
  `max_p95_ms=0`（沿用既有"0 即关闭"口径，本轮不新增延迟门禁）。
- [x] release soak 实跑全绿：360 请求（6 用例 × 20 × 3 轮）零失败，`SOAK_SUMMARY rounds=3 failed_rounds=0`，
  `route_owner_check=true`；表内数值逐个与 `summary.json` 复核一致。最慢一环是 `docqa-health`
  （p50 270-350ms，串到 Python 能力层），`docqa` 轮1 p95=310ms 后收敛到 85-90ms（热身效应）。
- [x] 已写明容量结论的边界：隔离栈未配置 embedding/LLM，`docqa` 返回 200 但检索为空（`citations=0`），
  故本轮 p95 不代表真实模型生成时延；`graph-build` 走"提交即取消"，只测任务受理路径。
  带模型的容量口径需在配置真实模型凭据的栈上另跑一轮且先声明阈值。
- [x] soak 运行器可跨平台复跑：`backend/tests/run_perf_soak.py` 不再硬依赖 `backend/.venv/bin/python`
  （缺失时回退当前解释器，可用 `--python`/`SOAK_PYTHON` 指定），子进程输出按 UTF-8 捕获。

## Release Gate R2 整改记录（2026-09-30，上级 6 项）

- [x] CI 敏感信息输出/上传整改：Playwright 在 CI 关闭 trace/video；scrub 覆盖
  `frontend/playwright-report`（HTML 报告会复制 trace，只清 `test-results` 原件无效）；新增
  `backend/tests/check_artifact_secrets.py` 作为上传前门禁，命中即 `clean=false` 且不发布产物；
  `release-acceptance` 不再整文件 `cat logs/dev/runtime.env`，只回显 `*_BASE_URL`，并由
  `check_dev_runtime_defaults.py` 断言该文件不得含其它键；两处失败消息不再回显带口令 DSN。
- [x] Python/Go RBAC 权限目录精确对账：`backend/tests/check_rbac_catalog_parity.py`（16 条检查，
  进统一边界守卫套件 `total=15 failed=0`）与 `go-backend/internal/adminstore/rbac_seed_parity_test.go`
  （4 个测试，反向读 Python 种子，缺源文件即 `t.Fatalf`）双向覆盖 `graph:admin` 只授 super_admin 且
  强制于 `/api/query`、预留 `kb:review/kb:manage/kb:publish` 注册但不授予不强制；两侧均做负向注入自证。
- [x] 无 LLM 口径收敛：回滚探针逐腿打印 `NOTE main_docqa_scope=link_smoke_only llm_configured=0`，
  所有 `docqa` 判据只声明为"检索/引用链路 smoke"，不宣称问答质量；回滚矩阵不产出性能判据。
- [x] 临时性能阈值落档：`docs/ENTERPRISE_PERF_SOAK_2026_09_30.md` §6 台账写明 CI `llm_disabled`
  档 `max_p95_ms=3000`、`llm_configured` 档 `20000` 均为临时值及其依据与复审触发；
  `max_error_rate=0.0` 长期不放宽；阈值均在执行前声明，未在结果出来后回改。
- [x] 新增限制记录：`go-backend` 含 Linux 专属调用（`syscall.Statfs`），`GOOS=windows` 编译失败，
  候选版本二进制必须在 Linux 构建；本地 Windows 以桩二进制验证执行器起停与前置校验 6 个用例。
- [ ] 遗留：CI `rollback-matrix` 与 `release-acceptance` 的 `workflow_dispatch` 首跑取证（受本机 `gh`
  未登录阻塞）；把回滚下限 `78b1f28` 写入 `ENTERPRISE_OPERATIONS_RUNBOOK.md` 回滚章节；
  配置真实 embedding/LLM 后按新声明口径复跑 soak 与 4→8→16 capacity 矩阵。
- [ ] 遗留：并发 4→8→16 的 capacity 递增矩阵，用于定位真实上限（当前只有并发 4 单点）。

## Release Gate R2 跟进（2026-10-01，任务 #31：secret scan 正样本自检与范围排除）

- [x] 开工前先测基线：用 HEAD 规则跑形状矩阵，11 条正样本 6 条 MISS（其中 1 条为探针 bcrypt
  样本自身长度缺陷），真实规则盲区 5 条——§9.10 记录的 3 条全部实测复现，另确认下划线后缀键
  （`SECRET_KEY=`）为同类变体、JSON 引号键（`{"password": "..."}`）为评审未记录的新盲区。
- [x] 规则层修复：键锚点去 `\b` 改"前缀段 + 凭据后缀白名单（`_key/_hash/_token/_value/_secret`）"，
  键后与值前各允许可选引号；值字符集放回 `;{}`；结构性/调用表达式误报改由值首段字面量表与
  「点分标识符紧跟左括号」判别承接。pass/fail 判据未变（仍 `findings=0` 放行），未放宽验收阈值。
- [x] 误报修复从"全局放宽字符集"改为"扫描范围"：新增 `DEFAULT_SHAPE_EXCLUDES`
  （playwright-report/、test-results/、node_modules/、dist/、build/、`*.min.js`、`*.min.css`、`*.map`），
  排除只关闭赋值形状一档，字面值与 jwt/dsn/bcrypt 照常扫；`--exclude` / `--no-default-excludes`
  可控，SUMMARY 增 `shape_scanned_files`/`shape_excluded_files`/`exclude_rules` 并逐次打印排除 NOTE。
- [x] 正样本自检套件 `backend/tests/check_artifact_secrets_selftest.py`：43 项断言全绿（初版 42 项，
  第 43 项是 §9.11.2 那次 CI 变红后补的步骤名回显守卫），逐样本单文件
  单进程；负向对照（把扫描器形状循环去势）→ `assertions=37 failed=14` 变红，sha256 逐字节还原后复绿，
  证明"门禁永远干净"这种失效无法躲过自检。
- [x] 接线与取证：`run_unified_boundary_guards.py` 新增 case `secret_scanner_selftest`
  （本地复跑 `SUMMARY total=16 failed=0`）；`ci.yml` `backend-scripts` py_compile 清单加入该文件；
  CI 实际扫描范围（playwright-report/test-results/artifacts/logs/dev）新旧均 `findings=0`。
- [x] 出口恢复后补证（2026-10-02）：github.com 仍只解析到被丢包的 `20.205.243.166`（4 次 ls-remote
  均 21s 超时），改走 GitHub 官方 SSH-over-443 入口 fast-forward push 9 笔——推送前远端
  `main=cae11831`（经 `merge-base --is-ancestor` 验证为首父），推送后远端 `main=c9433ef`
  与本地 HEAD 逐字节一致；未 force、未改 hosts、未关证书校验、未改 `remote.origin.url`。
  push 档 CI run#36937457198 结论 success，并以 `commits/c9433ef/check-suites` 独立复核 Actions
  = completed/success；CI 日志含 `SECRET_SCAN_SELFTEST_SUMMARY assertions=42 failed=0 result=pass`、
  `[OK] secret_scanner_selftest`、`SUMMARY total=16 failed=0`，与本地计数一致。详见报告 §9.11.1。
- [x] dispatch 实跑暴露并闭环一次**我自己引入的回归**（2026-10-02，报告 §9.11.2）：触发 run#36939737132
  （headSha=`0c18cb6`）取新 SUMMARY 取值，结果 step 16 报 `findings=7 result=fail`、产物被 withhold。
  根因是自检套件的**步骤名里写了样本凭据字面量**，而 release-acceptance 会把守卫 stdout 落成
  `artifacts/release-acceptance/acceptance-summary.log`——该路径在扫描范围内，于是门禁判红自己的夹具。
  修复按 TDD：先加第 43 条 `label_echo_guard`（旧标签下实测红 `assertions=43 failed=1`）→ 再把标签去
  凭据化（样本仍写临时文件喂扫描器，断言语义不变）→ 复跑 `43/0`、守卫 `16/0`、守卫 stdout 落盘复扫
  `findings=0`。复现取证：沙箱重跑旧自检并按 CI 参数扫其 stdout，7 条 `match_sha256`
  与该 run 逐条相同（口径为整段 `match.group(0)` 取哈希，见 check_artifact_secrets.py:184）。
- [ ] 遗留：`SECRET_SCAN_SUMMARY` 新增三键（`shape_scanned_files`/`shape_excluded_files`/
  `exclude_rules`）的 **CI 绿态实跑取值仍未取到**。run#36939737132 虽给到
  `shape_scanned_files=5 shape_excluded_files=1 exclude_rules=8`，但该运行整体为红，只能证明"CI 里
  确实按新参数执行并计数"，不能当绿灯证据；6 个 scanner step 全在 `workflow_dispatch` 档 job，
  push 档一条不执行，且 `perf-probe` 腿只扫 `artifacts` 不覆盖 minified 产物；要补这条需在修复推送后
  重新触发一次真实 dispatch 运行。已知代价两条（`Pa(ss)word` 形态不报、
  排除路径不扫赋值形状）详见 `docs/ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md` §9.11。

## 最新联调记录（2026-09-28，M4-R1 步骤 3：前端调用方迁移）

- [x] 后端新增业务面 KB 目录 `GET /api/knowledge-bases`（`knowledge_bases_business.go`）：仅认证 + `AuthorizedKBIDs(subject, "graph:read")` 计算集合，再由 `adminstore.ListAuthorizedKnowledgeBases` 按集合取 active KB 行；无授权→空集合，不放大。
- [x] `handlers.go` 已在 `registerNativeGraphRoutes` 旁挂载 `/api/knowledge-bases`；新增 `knowledge_bases_business_test.go` 覆盖 global/仅 project 集合/未认证/过期/授权服务不可用/空集合/405 七类用例，`go test ./...` 全绿（Docker golang:1.24.13-bookworm）。
- [x] 前端新增 `KnowledgeBaseSelector`（workspace TopBar）+ `kbService.ts` 目录拉取；`graphStore` 新增 `activeKbId`（持久化 + 切库重置视图）；`api` 拦截器为所有 `/api/*` 注入 `X-KB-ID`；`docQa.ts`/`graphService.ts` 在 body 显式携带 `kb_id`；DocQA/Cypher 无 `activeKbId` 时本地拦截不发请求。
- [x] E2E `business-docqa-flow.spec.ts` 已改为先进入 workspace 并选定一个测试 `kb_id`（浏览器注入 + request 上下文 `X-KB-ID`），确保带作用域发起。
- [x] 真实统一活栈验收通过（2026-09-29，M4 关闭）：`npm run e2e`（Playwright）5 passed / 3 skipped / PW_EXIT=0，业务全链路 upload→build→ask→trace→delete（3.8m）通过，3 个 skip 为需真实密码的 UI 登录用例；Go 套件（Docker golang:1.24.13-bookworm）`BUILD/VET/gofmt/TEST` 全 0，8 包全 ok；`run_unified_boundary_guards.py` total=14 failed=0；`check_m4r1_scope_guards_unit.py` passed=15 failed=0；活栈 `check_dual_kb_blackbox.py` passed=15 failed=0、`check_kb_scope_isolation.py` passed=54 failed=0；`check_migration_cleanup_guards.py` 通过（含新增 KB strict 静态回归守卫）。
- [x] 复跑前提（2026-09-29 复核确认全量复跑同计数，并记录三条前置条件）：① 管理库须存在至少一个 active KB，缺数据时先跑 `backend/scripts/seed_e2e_local_stack.py` 播种（KB 目录为空会导致业务 E2E 在 resolveTestKbId 直接失败）；② Go 网关与 Python 能力层（8001）必须同时在线——Go `/health` 中 `python_backend.connected=true` 仅代表代理客户端初始化成功、不探测上游，不能当作 Python 存活证据；Python worker 未起时 `build_graph` 任务会停留 pending 并使 E2E 超时；③ 3 个 skip 用例需要真实 admin 密码，token 注入用例已覆盖认证路径。另：Windows 检出为 CRLF（core.autocrlf），容器内 `gofmt -l` 会对全量 Go 文件伪报，以 LF 规范化检出为准（入库已由 `.gitattributes` 统一 LF）。
- [x] M4 结论口径更新：KB 作用域 strict 已正式启用（契约 §2.11 D4 满足：双 KB 黑盒全通过 + 全部调用方显式携带 kb_id 且缺 scope 负向测试在位）。代码中不存在 `KB_SCOPE_ENFORCE` 开关或 default KB 兜底，strict 为唯一形态，并由 `check_migration_cleanup_guards.py` 静态守卫防削弱。

## 最新联调记录（2026-06-08）

- [x] 修复 Go 统一响应信封细节：成功响应在 `data=nil` 时仍显式返回 `data: null`，前端统一解析不再因缺少 `data` 字段报错；已通过 `go test ./internal/httpserver ./internal/config` 回归。
- [x] 修复浏览器同源联调与 Node 侧 E2E 地址语义混用：业务 E2E 现明确区分 `VITE_API_BASE_URL=same-origin` 与 `ADMIN_BASE_URL` / `E2E_API_BASE_URL`。
- [x] 修复 Playwright 自定义 `E2E_BASE_URL` 时的前端启动端口错位，`http://localhost:1234` 浏览器 QA 端口已重新实跑通过。
- [x] 删除无引用的前端遗留管理服务 `frontend/src/services/adminApi.ts`，管理后台服务入口继续收敛到 `frontend/src/services/adminService.ts`。
- [x] Linux / Windows 前端 E2E 包装脚本与发布验收包装脚本已统一成“浏览器 `same-origin`、Node 侧 `ADMIN_BASE_URL` / `E2E_API_BASE_URL`”语义，避免再次混用。
- [x] 当日复验通过：`frontend npx tsc --noEmit -p tsconfig.json`、`frontend npm run build`、`backend/tests/run_unified_boundary_guards.py`、`backend/tests/run_backend_smoke_suite.py --include unified_mode --include go_orchestrated --include documents --include jobs_api --include reindex_obs --include qa_traces --include qa_cost_unit`、`frontend/tests/run_admin_e2e.sh`、`backend/tests/run_release_acceptance.sh --skip-boundary-guards --skip-backend-smoke --skip-perf-probe`、`backend/tests/run_release_acceptance.sh --skip-perf-probe`；结果分别为 `2 passed, 1 skipped` 与两次 `ACCEPTANCE_SUMMARY failures=0`。

## 最新联调记录（2026-06-09）

- [x] 补齐第一条可实跑的数据库迁移回滚链：`backend/admin/migrate_job_worker_lease.py` 现已支持 `--dry-run` 与 `--action rollback`。
- [x] 新增隔离 SQLite 单测 `backend/tests/check_migrate_job_worker_lease_rollback_unit.py`，覆盖 `dry-run -> migrate -> rollback dry-run -> rollback -> rollback 再执行一次`，结果 `MIGRATE_JOB_WORKER_LEASE_ROLLBACK_UNIT_OK`。
- [x] 第二批数据库迁移回滚链已补齐：`backend/admin/migrate_admin_log_audit_fields.py`、`backend/admin/migrate_add_login_count.py`、`backend/admin/migrate_add_is_encrypted.py` 现已支持 `--dry-run` 与 `--action rollback`。
- [x] 第二批隔离 SQLite 单测已通过：`backend/tests/check_migrate_admin_log_audit_fields_rollback_unit.py`、`backend/tests/check_migrate_add_login_count_rollback_unit.py`、`backend/tests/check_migrate_add_is_encrypted_rollback_unit.py`，结果分别为 `MIGRATE_ADMIN_LOG_AUDIT_FIELDS_ROLLBACK_UNIT_OK`、`MIGRATE_ADD_LOGIN_COUNT_ROLLBACK_UNIT_OK`、`MIGRATE_ADD_IS_ENCRYPTED_ROLLBACK_UNIT_OK`。
- [x] 配置级回滚演练再次实跑通过：`backend/tests/run_config_rollback_drill.py` 输出 `ROLLBACK_DRILL_SUMMARY result=pass`，并生成 `artifacts/rollback-drill/config-rollback-2026-06-09-final/summary.json`。
- [x] 完整发布验收再次实跑通过：`backend/tests/run_release_acceptance.sh --fail-fast` 输出 `ACCEPTANCE_SUMMARY failures=0`；期间发现前端业务 E2E 首页入口断言过于脆弱，已在 `frontend/tests/e2e/business-docqa-flow.spec.ts` 修正并重跑通过。

## 最新联调记录（2026-06-10）

- [x] 第三批数据库迁移回滚链已补齐：`backend/admin/migrate_rbac_core.py`、`backend/admin/migrate_jobs_table.py`、`backend/admin/migrate_qa_traces_table.py` 现已支持 `--dry-run` 与 `--action rollback`。
- [x] 第三批隔离 SQLite 单测已补齐：`backend/tests/check_migrate_rbac_core_rollback_unit.py`、`backend/tests/check_migrate_jobs_table_rollback_unit.py`、`backend/tests/check_migrate_qa_traces_table_rollback_unit.py`。
- [x] 数据库迁移回滚统一烟测入口已新增：`backend/tests/run_migration_rollback_smoke.py` 会串行执行当前 7 条迁移回滚单测。
- [x] 发布验收入口已挂接迁移回滚烟测：`backend/tests/run_release_acceptance.sh` 与 `backend/tests/run_release_acceptance.ps1` 默认会执行 `migration-rollback-smoke` 步骤，避免发布前只测在线链路、不测迁移可回退性。

## 最新联调记录（2026-06-13）

- [x] CI 后端边界编译清单已同步当前真实 Python internal capability 文件，移除已退役 `documents` / `graph_build` 路由文件引用。
- [x] Python internal `nl2cypher` 边界守卫已调整为环境无关契约测试，不再要求本机必须配置 AI API Key 才能通过统一边界守卫。
- [x] 新增 [docs/BACKEND_BOUNDARY_FINAL.md](/home/yuanhuan/GraphInsight/docs/BACKEND_BOUNDARY_FINAL.md)，冻结当前 Go 默认外部入口、Python internal capability、退役 Python public surface、本地 Docker PG 与验收守卫。
- [x] 运维手册与发布前 smoke 清单已更新到当前运行口径：端口可回退，真实地址读 `logs/dev/runtime.env`，本地管理库默认 Docker PostgreSQL。
- [x] 本轮复验通过：`backend/tests/run_unified_boundary_guards.py` 输出 `SUMMARY total=12 failed=0`；`go test ./internal/httpserver ./internal/config` 通过；`frontend npm run build` 通过；`frontend/tests/e2e/admin-core.spec.ts` 输出 `4 passed`。

## 最新联调记录（2026-06-06）

- [x] `scripts/dev-backend.sh` 默认 unified 运行态复核通过：`logs/dev/backend.env` 写入 `RBAC_AUTHZ_MODE=go_db`，且不再写入任何 Python public compat 开关。
- [x] 修复 unified 启动链路中的管理库配置漂移：`scripts/dev-backend.sh` 现在会为 `logs/dev/backend.env` 及 Python/Go 进程显式注入 `ADMIN_DATABASE_URL`，避免 Python worker 回退到占位默认库并导致任务长期停留在 `pending`。
- [x] 新增并验证 `backend/tests/check_dev_runtime_defaults.py`，同时确认 `logs/dev/runtime.env`、Python `/health` 与 Go `/health` 中的 `authz/python_backend/orchestrator` 边界信息一致。
- [x] `backend/tests/check_dev_runtime_defaults.py` 已扩大覆盖：默认 unified 运行态必须写出非占位 `ADMIN_DATABASE_URL`，防止任务 worker 与 admin DB 配置再次漂移。
- [x] 运行态 smoke 通过：`backend/tests/run_backend_smoke_suite.py --include dev_runtime_defaults --include go_orchestrated --include python_public_removed --include unified_mode`，`SUMMARY total=4 failed=0`。
- [x] Python public runtime 已进一步收口：业务侧公开 `/api/*` 路由已全部移除；管理侧公开 compat 已移除，仅保留 `internal wake`；Go 已原生接手的公开入口均已从 Python 运行面与源码面删除，对应临时 legacy 承接层也已删除。
- [x] Go authz 边界已进一步固定：Python `/api/v1/admin/auth/authorize` 兼容路由已从源码与运行态删除；Go `RBAC_AUTHZ_MODE` 仅保留本地 `go_db` / `local_jwt_soft` 语义，并在配置加载时将非法 mode 回退为 `go_db`。
- [x] 更宽运行态 smoke 通过：`backend/tests/run_backend_smoke_suite.py --include dev_runtime_defaults --include go_orchestrated --include python_public_removed --include authz --include unified_mode --include jobs_api`，`SUMMARY total=6 failed=0`。
- [x] 默认 unified 深链路 smoke 基线已扩大到入口契约、静态 guard 与审计校验：`backend/tests/run_backend_smoke_suite.py` 当前包含 `dev_runtime_defaults`、`migration_cleanup_guards`、`go_orchestrated`、`nl2cypher_contract`、`nl2cypher_post_contract`、`nl2cypher_audit_contract`、`docqa_post_contract`、`docqa_audit_contract`、`deep_research_post_contract`、`deep_research_audit_contract`、`docqa_health_contract`、`python_public_removed`、`authz`、`unified_mode`、`documents`、`jobs_api`、`reindex_obs`、`qa_traces`、`qa_cost_unit`、`docqa_full_chain` 共 20 个 case，用于固定 Go 外部入口、Python capability plane、静态迁移清理守卫与审计边界。
- [x] 定向链路复核通过：`check_job_reindex_and_observability.py`、`check_qa_traces_api.py`、`check_docqa_full_chain.py` 均在默认 `Go(8081) -> Python(8001)` unified 运行态下返回成功，证明 Go 控制面、Python capability plane 与 Python worker 执行边界可运行。
- [x] Go/Python internal contract 已进一步固定：业务 capability 入口现在要求 `X-Go-Orchestrator + X-Trace-Id`；`X-Go-Proxy` 仅用于 `jobs/wake` 这类内部控制信号。验证通过：`check_unified_backend_mode.py`、`check_go_orchestrated_routes.py`、`check_nl2cypher_go_contract.py`、`check_docqa_internal_route_unit.py`、`check_nl2cypher_internal_route_unit.py`、`go test ./internal/httpserver`。
- [x] `POST /api/nl2cypher` 外部入口已继续收紧：Go 现在先校验 JSON 请求体与 `natural_language` 非空，再决定是否转发到 Python `/api/internal/nl2cypher`；验证通过：`check_nl2cypher_post_go_contract.py`、`go test ./internal/httpserver -run 'TestNL2CypherRouteRejectsInvalidJSONBeforeUpstream|TestNL2CypherRouteRejectsBlankNaturalLanguageBeforeUpstream'`。
- [x] `POST /api/nl2cypher`、`POST /api/docqa` 与 `POST /api/docqa/deep-research` 入口审计已收口到 Go：坏请求会在 Go 入口直接拒绝并写入 `admin_logs`，成功转发后的业务审计也由 Go 负责，Python 保留 capability 执行与 `qa_traces`；验证通过：`check_nl2cypher_audit_go_contract.py`、`check_docqa_post_go_contract.py`、`check_docqa_audit_go_contract.py`、`check_deep_research_post_go_contract.py`、`check_deep_research_audit_go_contract.py` 及 `go test ./internal/httpserver` 相关用例。
- [x] `GET /api/docqa/health` 也已从通用编排收口为显式 Go 入口：Go 现在先校验 `probe_llm` 查询参数，再转发到 Python `/api/internal/docqa/health`；验证通过：`check_docqa_health_go_contract.py`、`go test ./internal/httpserver -run TestDocQAHealthRejectsInvalidProbeLLMBeforeUpstream`。
- [x] Python 公开业务路由与 Go 管理控制面命名已对齐当前运行边界：Python 仅保留 `/api/internal/*` capability 与 `/api/internal/jobs/wake`；已退役的 `/api/internal/documents*` 与 `/api/internal/graph/build` 不再保留为默认 runtime 或诊断入口。Go 管理路由装配与测试已按 control plane / python wake 语义收口，并通过 `check_unified_route_mounts_unit.py`、`check_docqa_internal_route_unit.py`、`check_nl2cypher_internal_route_unit.py`、`go test ./internal/httpserver` 复验。
- [x] CI 已固化两层后端门禁：默认 `backend-unified-guards` job 使用 `backend/.venv` 执行 `backend/tests/run_unified_boundary_guards.py`，防止公开路由回退；手动 `backend-release-smoke` job 已复用 `backend/tests/run_release_acceptance.sh --skip-frontend-e2e --skip-perf-probe`，通过统一发布验收入口对外部 Go 网关执行后端发布级 smoke。
- [x] Linux / WSL 发布验收入口已补齐：`backend/tests/run_release_acceptance.sh` 默认使用 `backend/.venv`、Go `8081` 与统一边界守卫，并可串联后端 smoke、前端业务 E2E 和性能探针；PowerShell 版也已补充 `.venv/bin/python` 识别。
- [x] 前端业务主流程 E2E 已纳入统一发布验收 CI 手动入口：`release-frontend-e2e` 复用 `backend/tests/run_release_acceptance.sh --skip-boundary-guards --skip-backend-smoke --skip-perf-probe`，确保发布态前端主链路默认仍经 Go 网关。
- [x] 完整发布链路已纳入 CI 手动入口：`release-acceptance` 复用 `backend/tests/run_release_acceptance.sh --fail-fast`，串联统一边界守卫、后端 smoke、前端业务 E2E 与性能探针。
- [x] 发布级性能基线已补齐：`docs/ENTERPRISE_PERFORMANCE_BASELINE.md` 记录只读探针 `20 requests / concurrency 4` 与发布路径探针 `5 requests / concurrency 2`，覆盖 `query`、`docqa`、`graph-build`，两轮 `error_rate=0`。
- [x] 发布记录模板与回滚检查项已固化：`docs/ENTERPRISE_RELEASE_TEMPLATE.md` 和 `docs/ENTERPRISE_OPERATIONS_RUNBOOK.md` 现已覆盖统一发布验收入口、Go/Python 边界验收、性能基线与回滚后最小验证动作。
- [x] Linux 本地完整发布验收已实跑通过：`backend/tests/run_release_acceptance.sh --fail-fast` 输出 `ACCEPTANCE_SUMMARY failures=0`，覆盖统一边界守卫、20-case 后端 smoke、前端业务 E2E 与发布性能探针。
- [x] Python 实现层边界已继续收口一轮：`nl2cypher_service`、`neo4j_service`、`job_service`、`qa_trace_service`、`monitor_service` 的运行态配置读取开始统一改用 `services/runtime_config.py`，不再直接依赖 `admin.services.config_service`；新增守护 `check_runtime_config_boundary_unit.py` 并通过 `run_unified_boundary_guards.py` 与定向 smoke 复验。
- [x] `docqa` capability route 已继续收口：QA trace 写入改走 `services/qa_trace_runtime.py`，`api/routes/doc_qa.py` 不再直接 import admin QA trace schema/service；验证通过 `check_docqa_reasoning_profile_unit.py`、`run_unified_boundary_guards.py` 与 DocQA/QA trace 定向 smoke。
- [x] Python route 层 admin 依赖继续收口：`api/routes/doc_qa_internal.py` 的 DB dependency 改走 `services/runtime_db.py`；退役 documents route 源码实现已删除，文档治理 public owner 固定在 Go；`check_runtime_config_boundary_unit.py` 已覆盖该边界，复验 `run_unified_boundary_guards.py` 输出 `SUMMARY total=12 failed=0`。
- [x] Python job worker 职责继续拆分：新增 `services/job_runtime.py` 承接 `build_graph`、`clear_kb`、`reindex` 执行实现，`admin/services/job_service.py` 保留任务状态机、lease、heartbeat、retry 与审计；`check_runtime_config_boundary_unit.py` 已守护该边界。
- [x] Python 退役建图路由源码继续清理：`api/routes/graph_build.py` 已删除，`check_migration_cleanup_guards.py` 已加入回归守护，防止 Python graph build route 实现重新出现。
- [x] Python admin route registry 边界已继续固定：`admin/api/route_registry.py` 只允许挂载 `jobs_endpoints.internal_router`，不得重新导入或挂载 `auth/config/logs/monitor/profile/qa_traces/rbac/users` 等 public admin endpoint router；`check_migration_cleanup_guards.py` 已加入静态守护。
- [x] Python admin endpoint 源码语义已标记：`admin/api/endpoints/{auth,config,jobs,logs,monitor,profile,qa_traces,rbac,users}.py` 均声明 `PYTHON_PUBLIC_ADMIN_API_RETIRED = True`，明确 public admin API 已由 Go 接管；除 `jobs.py` 的 internal wake 外不得新增 Python internal admin router。
- [x] job/worker 抽离后复验通过：`run_unified_boundary_guards.py` 输出 `SUMMARY total=12 failed=0`，定向 `jobs_api`、`reindex_obs`、`qa_cost_unit`、`unified_mode` smoke 输出 `SUMMARY total=4 failed=0`。
- [x] 本轮后端定向 smoke 通过：`backend/tests/run_backend_smoke_suite.py --include dev_runtime_defaults --include go_orchestrated --include nl2cypher_contract --include nl2cypher_post_contract --include nl2cypher_audit_contract --include docqa_health_contract --include unified_mode --include jobs_api --include reindex_obs --include qa_traces --include qa_cost_unit` 输出 `SUMMARY total=11 failed=0`。
- [x] 前端业务 E2E 已适配异步建图任务中心：提交建图任务后轮询任务终态，不再依赖旧同步按钮文案。
- [x] 发布性能探针已修正 `graph-build` 副作用：probe job 提交后自动取消，避免污染后续 Python worker 队列。
- [x] 已补最小 soak/capacity 执行入口：`backend/tests/run_perf_soak.py` 复用 `run_perf_probe.py` 做多轮批次验证，产出 `summary.json` 与每轮报告，作为容量趋势与长稳观察起点。
- [x] 已补 soak/capacity 与回滚演练记录模板：`docs/ENTERPRISE_PERF_SOAK_TEMPLATE.md`、`docs/ENTERPRISE_ROLLBACK_DRILL_TEMPLATE.md`，用于固定参数矩阵、结果落档与回滚演练记录。
- [x] 已补正式 `release` soak 落档与最小回滚验证记录：`docs/ENTERPRISE_PERF_SOAK_2026_06_07.md`、`docs/ENTERPRISE_ROLLBACK_MIN_VERIFY_2026_06_07.md`，说明 soak 已从脚本入口推进到事实记录，回滚最低验证动作已实跑可用。
- [x] 已补更高并发 `capacity` 观察记录并修复运行态误报：`docs/ENTERPRISE_CAPACITY_OBSERVATION_2026_06_07.md` 记录了 `docqa-health` 的 Python internal 限流误伤、修复动作与复验结果，说明当前容量观察瓶颈已从“错误 429”回到“真实延迟趋势”。
- [x] 已完成一轮真实配置回滚演练并落档：`docs/ENTERPRISE_CONFIG_ROLLBACK_DRILL_2026_06_07.md` 记录了“错误 Python 上游配置 -> `docqa health` 返回 `502` -> 恢复正确配置 -> 最小验收通过”的全过程，说明发布级回滚不再只停留在模板和最小验证。

## 最新联调记录（2026-05-26）

- [x] 使用 Neo4j `127.0.0.1:7474/7687`、Python `8001`、Go 隔离端口 `18081` 完成运行态验收；Python 与 Go `/health` 均返回 `neo4j.connected=true`。
- [x] 完整后端 smoke 通过：`backend/tests/run_backend_smoke_suite.py --base-url http://127.0.0.1:18081`，`SUMMARY total=7 failed=0`，覆盖 authz、documents、jobs_api、reindex_obs、qa_traces、qa_cost_unit、docqa_full_chain。
- [x] 前端业务 E2E 通过：`frontend/tests/run_admin_e2e.ps1 -AdminBaseUrl http://127.0.0.1:18081 -E2ESpec business-docqa-flow.spec.ts`，`1 passed`。
- [x] 修复运行态暴露的两个迁移残留：`/api/nl2cypher/status` 改用当前 `admin.services.config_service`；文档文件级删除/恢复在 `purge_graph=false` 时不再强依赖 Neo4j。
- [x] `backend/tests/run_backend_preflight.ps1` 在默认 `8081` 被非 GraphInsight 进程占用时明确失败并提示端口冲突；隔离端口验收需显式传入 `-BaseUrl http://127.0.0.1:18081`，避免掩盖默认入口不可用。
- [x] 验证通过：`go test ./...`。
- [x] 验证通过：`backend/.venv/bin/python -m py_compile` 覆盖本轮触达的 Python 文件。
- [x] 后续已在 2026-06-07 补齐发布级性能基线、统一发布验收 CI 手动入口与本地完整发布验收；阶段 6 已完成当前交付基线，容量上限与更大范围回滚演练转为后续增强项。

## 最新联调记录（2026-05-11）

- [x] 稳定化前端业务主流程 E2E 用例：`frontend/tests/e2e/business-docqa-flow.spec.ts` 覆盖上传 -> 建图 -> 问答 -> QA trace -> 删除，并增加 Go route owner 断言与测试文档自清理。
- [x] 新增 E2E 验收入口：`npm run e2e:business`，并为 `frontend/tests/run_admin_e2e.ps1` / `frontend/tests/run_admin_e2e.sh` 增加 `E2E_SPEC=business-docqa-flow` 单独执行能力。
- [x] 补齐阶段 5 统一指标与日志分级：`/api/v1/admin/monitor/metrics/unified` 纳入 logs 指标，新增 `/api/v1/admin/monitor/log-severity`，并将 error/warn 日志阈值纳入 `/api/v1/admin/monitor/alerts/check`。
- [x] Go 默认入口补齐新监控路由：`/api/v1/admin/monitor/log-severity` 已纳入 Go 原生 owner，并接入 `monitor:read` 权限回归。
- [x] Go 默认入口收口：`/api/v1/admin` 与未知 `/api/v1/admin/*` 由 Go 统一返回 `go-native` owner 的 404，不再落入 `/api/v1/**` legacy Python 代理；提交 `f324b71 fix(go): own unknown admin routes`。
- [x] 验证通过：`backend/.venv/bin/python -m py_compile backend/admin/services/monitor_service.py backend/admin/api/endpoints/monitor.py`。
- [x] 验证通过：`go test ./...`。
- [x] 验证通过：`frontend npm run build`。
- [x] 当前本地后端/Go 服务未启动，本轮未实跑 `npm run e2e:business`；前端业务主流程 E2E 已在 2026-05-26 的 Neo4j 可用联调环境中补充跑通。
- [x] 后续已在 2026-06-07 补齐发布级性能基线、统一发布验收 CI 手动入口与本地完整发布验收；阶段 6 已完成当前交付基线，容量上限与更大范围回滚演练转为后续增强项。

## 最新联调记录（2026-05-06）

- [x] 新增 `docs/GO_PYTHON_DELIVERY_CLOSURE_CHECKLIST.md`，明确 Go / Python 拆分已进入工程交付收尾阶段。
- [x] 新增 `docs/ENTERPRISE_OPERATIONS_RUNBOOK.md`，固化本地启动顺序、分层排障顺序、Neo4j 凭据漂移诊断与最小回滚思路。
- [x] 新增 `docs/ENTERPRISE_GO_LIVE_ACCEPTANCE_CHECKLIST.md`，固化发布阻断项、主链路验收、控制面验收、观测验收与文档验收清单。
- [x] 修复 Go 配置回退边界：从 `backend/.env` 继承共享配置时跳过 Python 监听端口 `API_HOST/API_PORT`，避免 Go 误占 `8001`。
- [x] 执行 `go test ./internal/config ./internal/httpserver/...`，确认配置加载与 Go 路由层回归通过。
- [x] 补充 Go route owner 回归：已知 admin 模块现由 Go 原生持有 owner；未知 `/api/v1/admin/*` 已在 2026-05-11 收口到 Go 兜底；非 admin 的未知 `/api/v1/**` 兼容代理已移除，`/api/media/**` 与公开媒体代理兼容入口也已收口到 Go。
- [x] 前端业务主流程 E2E 验收入口已补齐，并已在 2026-05-26 的 Neo4j 可用联调环境实跑通过；后续已在 2026-06-07 补齐发布级性能基线、统一发布验收 CI 手动入口与本地完整发布验收，阶段 6 已完成当前交付基线。

## 最新联调记录（2026-04-21）

- [x] 执行 `backend/tests/check_job_reindex_and_observability.py`
- [x] 验证任务链路：创建 reindex 任务 -> 终态 -> job 日志聚合
- [x] 验证监控链路：`/monitor/performance`、`/monitor/slo`、`/monitor/alerts/check`

## 最新联调记录（2026-04-29）

- [x] 执行 `frontend` 生产构建，确认问答追踪后台页面可打包通过。
- [x] 执行 `backend/admin/migrate_qa_traces_table.py`，确认 `admin_qa_traces` 表已创建。
- [x] 通过管理员 token 触发 `/api/docqa` 与 `/api/docqa/deep-research`。
- [x] 验证失败场景也会写入 `admin_qa_traces`，并可通过 `/api/v1/admin/qa-traces` 列表与详情接口查询。
- [x] 新增 `backend/tests/run_backend_smoke_suite.py`，统一串行执行权限、文档治理、任务中心、监控与问答追踪烟测。
- [x] 实跑 `backend/tests/run_backend_smoke_suite.py`，5 条关键链路全部通过。
- [x] 新增 `backend/tests/check_docqa_full_chain.py`，串行验证上传 -> 建图 -> 问答 -> 追踪 -> 删除主链路。
- [x] 实跑 `backend/tests/check_docqa_full_chain.py`，`qwen-flash` 作为默认模型通过主链路验收。
- [x] 修复 OpenAI/httpx 客户端继承空代理环境变量导致的 `Connection error`：新增 `HTTP_CLIENT_TRUST_ENV=false` 默认策略，并统一收敛到 LLM/OpenAI 客户端工厂。
- [x] 修复后再次实跑 `backend/tests/check_docqa_full_chain.py` 与 `run_backend_smoke_suite.py --include docqa_full_chain`，问答追踪 `generation_snapshot.mode=llm_success`。
- [x] 新增 `backend/tests/run_backend_preflight.ps1`，提供 Windows 本地发布前一键校验入口。
- [x] 实跑 `backend/tests/run_backend_preflight.ps1`，自动完成启动、健康检查、smoke suite 与收尾清理。
- [x] 新增 Playwright 前端 E2E 骨架：`frontend/playwright.config.ts`、`frontend/tests/e2e/admin-core.spec.ts`、`frontend/tests/run_admin_e2e.sh`。
- [x] 增强 `frontend/tests/run_admin_e2e.sh`：WSL 下自动下载 Playwright 运行库（`libnspr4` / `libnss3` / `libasound2t64|libasound2`）并自动解析 Windows 后端宿主机地址。
- [x] 实跑 `ADMIN_TOKEN=*** ./frontend/tests/run_admin_e2e.sh`，`authenticated admin can browse core admin pages` 通过，配置中心/任务中心/问答追踪导航链路验证完成。
- [x] 前端业务主流程 E2E（上传 -> 建图 -> 问答 -> 删除）已补齐；2026-05-26 在 Go `18081` 联调环境实跑 `business-docqa-flow.spec.ts` 通过，`1 passed`。

## 最新联调记录（2026-04-30）

- [x] 按 `nvm use` 切换到 Node `22.22.2` 后，实跑 `frontend` 生产构建通过，确认 `任务中心 / 问答追踪 / 系统监控` 页面可打包交付。
- [x] 抽样验证 `/api/v1/admin/jobs`、`/api/v1/admin/qa-traces`、`/api/v1/admin/monitor/*` 返回结构与前端类型定义一致。
- [x] 补充任务中心高风险交互：`清库任务` 新增显式确认弹窗，并在页面中明确提示当前执行语义为“全局清库”，不受列表筛选条件限制。
- [x] 新增 `docs/ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md`，固化发布前固定 smoke 清单，并挂接到发布模板。
- [x] 新增 `docs/GO_PYTHON_HYBRID_BOUNDARY.md`，明确当前执行口径为“Go 业务编排层 + Python AI 能力层”，并列出后续改造缺口。
- [x] 新增 `docs/GO_DEFAULT_ENTRY_EXECUTION_PLAN.md`，把“Go 成为默认外部入口”拆成阶段、文件改动点、验证矩阵与回滚方案。

## 最新联调记录（2026-05-01）

- [x] 将 `backend/tests/run_backend_smoke_suite.py` 及其默认调用脚本的 `ADMIN_BASE_URL` 从 Python `8001` 切换到 Go `8081`。
- [x] 重写 `backend/tests/run_backend_preflight.ps1`：先确保 Python 能力层可用，再确保 Go 外部网关可用，并先执行 `go-backend/scripts/smoke_orchestrated_routes.py` 后再执行完整 smoke suite。
- [x] 为 `go-backend/scripts/smoke_orchestrated_routes.py` 增加管理员账号密码登录回退，避免本地 preflight 依赖手工准备 token。
- [x] 将 `frontend/tests/run_admin_e2e.sh` 默认健康检查目标切换到 Go `8081`，并保留通过显式环境变量覆盖的能力。

## 最新联调记录（2026-05-02）

- [x] 修复 `go-backend/internal/config` 本地环境加载：Go 启动时优先读取 `go-backend/.env`，缺失时回退到 `backend/.env`，避免本地 Go 与 Python Neo4j 配置源分裂。
- [x] 补齐 Go 编排路由缺口：新增 `GET /api/documents/deleted`、`POST /api/documents/{doc_id}/restore`，保证文档治理 smoke 可完整经过 Go。
- [x] `documents` 读路径继续下沉到 Go 原生：`GET /api/documents`、`GET /api/documents/deleted` 已直接读取 `DOCUMENT_STORAGE_PATH` 与回收站元数据，不再转发 Python 内部能力接口。
- [x] 修复统一运行态文档目录漂移：Go 从 `backend/.env` 回退读取相对路径时已按 env 文件目录归一，`scripts/dev-backend.sh` 也显式对齐 Go/Python 的 `DOCUMENT_STORAGE_PATH`、`MEDIA_STORAGE_PATH`，避免上传与删除落到不同目录。
- [x] `documents` 单文档写路径继续下沉到 Go 原生：`POST /api/documents/upload`、`DELETE /api/documents/{doc_id}`、`POST /api/documents/{doc_id}/restore` 已改由 Go 直接处理文件目录、回收站元数据与单文档图谱删除，文档治理 smoke 与 `DOCQA_FULL_CHAIN_OK` 已在统一运行态复验通过。
- [x] 使用隔离端口完成 Go 默认入口预检：`run_backend_preflight.ps1 -BaseUrl http://127.0.0.1:18082 -Include docqa_full_chain -KeepServer`，验证 Go `/health` 中 `neo4j.connected=true`。
- [x] 在 Go 默认入口下实跑完整主链路：上传文档 -> 发起建图 -> 等待任务成功 -> 发起问答 -> 校验 QA trace -> 删除文档，输出 `DOCQA_FULL_CHAIN_OK`。
- [x] 定位并修复本机 Neo4j Desktop 凭据漂移问题：将数据库实际密码重新对齐到 `backend/.env`，确保“新 Python 进程”和“新 Go 进程”都能直接通过 `bolt://127.0.0.1:7687` 验证连接。
- [x] 使用全新进程再次预检 `http://127.0.0.1:18085`，确认 Go `/health` 返回 `neo4j.connected=true`，且 Go 原生 `POST /api/query` 返回 `200`。
- [x] 额外实跑 `documents` 软删除 smoke，确认新 Python `8001` 与新 Go `18085` 组合下 `DOCUMENTS_SOFT_DELETE_FLOW_OK`。

## 最新联调记录（2026-06-07）

- [x] 继续收紧 Go/Python 内部头语义：`X-Go-Orchestrator` 专用于业务能力编排，`X-Go-Proxy` 仅保留给 `POST /api/internal/jobs/wake` 这类控制面信号，避免继续混用。
- [x] `DELETE /api/documents` 已下沉到 Go 原生执行：Go 直接完成 dry-run、软删除、回收站元数据、整库图谱清空与删除后校验，不再默认调用 Python `/api/internal/documents`。
- [x] `POST /api/graph/build` 已并回 Go 原生任务入口：公开路径继续保持不变，但现在由 Go 直接创建 `build_graph` 任务、支持幂等回放并唤醒 Python worker，不再默认调用 Python `/api/internal/graph/build`。
- [x] `MODEL-001` 已继续打通到建图执行链：`graph_extract` 与 `graph_extract_complex` 默认档位现已可在后台配置，并由 Go 注入任务 payload、Python worker 传递到实体/关系抽取执行层。
- [x] 定向回归通过：`go test ./internal/graph ./internal/httpserver/...`、`backend/.venv/bin/python backend/tests/check_unified_route_mounts_unit.py`、`backend/.venv/bin/python backend/tests/check_docqa_internal_route_unit.py`、`backend/.venv/bin/python backend/tests/check_nl2cypher_internal_route_unit.py`。

## 最新联调记录（2026-06-21）

- [x] `RAG-001` 上游解析质量治理已完成阶段 A/B 基础落地：新增文档解析器抽象、原生解析器兼容迁移、MinerU HTTP sidecar adapter、任务级 `parser_provider` 灰度参数、失败回退 native，以及 `Document/Chunk/Milvus` parser metadata 写入。
- [x] 后台配置中心已新增 `document_parser` 配置分类与“文档解析 / MinerU”表单分区，Go 控制面新增 `POST /api/v1/admin/config/test/document_parser`，支持 native 可用性检查和 MinerU `/health` 探测。
- [ ] 结构化 chunk、知识库治理解析状态展示、任务解析阶段展示与 QA trace parser metadata 展示尚未完成，不能标记为最终交付闭环。

## 阶段状态快照（2026-04-27）

- [x] 阶段 0 已完成：接口分层、统一响应、`trace_id`、发布模板。
- [x] 阶段 1 已完成：RBAC、资源级授权、权限审计、权限回归。
- [x] 阶段 2 已完成：硬删除、软删除、恢复、dry-run、删除后校验。
- [x] 阶段 3 已完成：任务中心、状态机、重试、取消、job 日志聚合。
- [x] 阶段 4 已完成：`docqa/health`、模型连通性测试、问答质量看板、问答链路追踪已落地。
- [x] 阶段 5 已完成：健康检查、告警、SLO、统一指标快照、日志分级统计与 error/warn 告警路由已落地。
- [x] 阶段 6 已完成当前发布基线：后端自动化测试、运维手册、上线验收清单、前端业务 E2E、发布级 smoke、完整发布链路与发布级性能基线均已落档；容量上限与长稳 soak 测试作为后续增强。
