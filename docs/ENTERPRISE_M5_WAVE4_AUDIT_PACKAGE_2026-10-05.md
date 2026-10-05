# 审计复核内容包：M5 Wave 4（总回归 / 任务中心验证 / 隔离守卫 / 事故口径）

编制时间：2026-10-05。交付对象：审计人员。分支 `m5/dual-write`。
本包只汇总**可独立复核**的内容：每条主张给代码位置（`file:line`）、复跑命令、本轮实跑输出摘录，
并写清"这条证据覆盖什么 / 不覆盖什么"。给不出复跑命令的主张一律进 §7 未验证清单。

## 0. 本轮边界（先读，否则判据会错位）

用户 2026-10-05 指令口径（原文约束）：本轮**仅**做总回归、前端/Go 任务中心验证、隔离 fail-closed
守卫、审计交付；**不运行任何 `--confirm`**，**不连接真实 Neo4j/Milvus**，**不 push**，**不 amend 历史提交**；
Wave 4 交审后再裁定方向 B 与 push。

由此推出四条判定，本包不越过任何一条：

1. 不宣布"方向 B（dual_write 影子侧）通过"；
2. 不开启 `dual_write`，不进入 S2；
3. 不宣布真实投影闭环（Neo4j / Milvus / 共享 PostgreSQL 执行态）；
4. 本包所有绿色都是**隔离证据**（临时 SQLite + 假 client），不是真实取证（见 §2 分栏）。

## 1. 交付范围与远端权威状态

| 项 | 值 |
|---|---|
| 分支 | `m5/dual-write`（本地） |
| 本地 HEAD | `6cabff9`（本包与守卫两笔交付提交后的树）。其后仅有 docs 修正笔，**终值请用 `git rev-parse --short HEAD` 现取** |
| 权威远端 `main` | `59332f42503443872d382dbce0710c7226175abb`（`git ls-remote origin main` 取值，非本地缓存） |
| 领先提交数 | `git rev-list --count 59332f4..HEAD`；`6cabff9` 树 = **21**（docs 修正笔后为 22，以命令现值为准） |
| 远端分支 | `git ls-remote origin 'refs/heads/m5*'` = 空 → **Wave 0–4 全部提交均未推送** |
| 已推送？ | 否。本轮无任何 push / amend / force；未改 `remote.origin.url`、hosts、证书校验 |
| 提交集规模 | `git diff --shortstat 59332f4..HEAD`；在 `6cabff9` 树实测 = 28 files changed, 5461 insertions(+), 162 deletions(-)。本包随后还有文档修正笔，**以复核命令现值为准**，不要把本行当终值 |
| Wave 4 改动面 | 6 项：守卫脚本、两处 docstring 口径改正、迁移方案 §9.4、本审计包、`.gitignore` 白名单行；分两笔本地提交（守卫笔 + 文档笔），**均未 push** |

Wave 0–4 逐笔（`git log --oneline origin/main..HEAD`，新→旧）：

| SHA | 类型 | 意图 |
|---|---|---|
| `6cabff9` | docs | Wave 4 审计交付包 + §9.4 记录 + Go 侧过期引用修正（`supportedJobTypes` / 400 `INVALID_BODY`，409 NOT-IMPLEMENTED） |
| `5034fcb` | test | Wave 4-3 建引擎脚本隔离守卫（禁裸 `DATABASE_URL`、方言闸门、钉 env/URL/sqlite） |
| `ee70025` | test | Wave 3 连续场景取证 `check_m5_wave3_handoff.py` + 注册统一门禁 |
| `856f7de` | feat | reindex_chunks 提交路径走去重入队 + 作业终态失败回写投影 |
| `e3aca62` | fix | `targets_hash` 不在 ORM 声明单列索引，恢复迁移回滚可执行 |
| `aae175a` | fix | 任务中心类型登记 reindex_chunks 并暴露 targets_hash |
| `39a51fe` | feat | 影子/向量失败逐 chunk 落 failed 并持久转交 reindex job |
| `9a30ee7` | feat | reindex_chunks 入队抽为共享服务并补全 §16.3 冲突分支 |
| `e50e55c` | docs | §9.2 记录开发库遗留行已备份并清除 |
| `1f395a2` | test | 作用域隔离单测钉死临时 SQLite |
| `96f0eef` | feat | Wave 2：build_graph 建立权威 content_revision |
| `792a147` | fix | Wave 1：dual_write 配置非法 fail-closed 扩到 upsert/delete/clear |
| `0203542` | chore | config 尾随空格 |
| `174d7df` | test | 普通 build_graph→影子失败→job retry 全链路调用链测试 |
| `91bb1a4` | fix | build_graph 作业边界对 vector_failures fail-closed（P1） |
| `0fd9d6e` | test | E2 表述修正 + 收紧断言 |
| `14313d0` | test | dual_write 影子失败调用链 |
| `767fbfb` | docs | 迁移方案同步 dual_write 现状 |
| `10d8adf` | test | `check_m5_dual_write.py` + 门禁注册 |
| `250b087` | feat | MilvusVectorStore §16.1 S1 双写扇出 |
| `1100175` | feat | 配置层 dual_write 开关（默认关闭） |

## 2. 隔离证据 vs 真实证据（分栏，不许混用）

| 维度 | 本轮隔离证据（已实跑） | 真实取证（本轮**零**连接） |
|---|---|---|
| PostgreSQL | 临时 SQLite 文件库（每场景一库），env 钉 `GRAPHINSIGHT_BACKEND_ENV_FILE` | 共享开发 PG：无连接、无 DDL、无写入（例外见 §6 事故记录） |
| Milvus | 假 client（记录调用序列） | 未连；v3 建集合、显式 INT64 `content_revision` 真实写入均未做 |
| Neo4j | 不触达（驱动层打桩） | 未连；Chunk/Document MERGE 真实落图未验证 |
| 作业 worker | 驱动子进程直跑 `job_service` 路径 | 未起真实 worker，未验证租约/并发 |
| 覆盖结论 | §16.3 去重、影子失败转交、终态父子回写、§8.5 拒写的**逻辑正确性** | 上述四项在真实库上的**落地性**仍为未验证 |

## 3. Wave 4-1 总回归实跑（本轮新鲜输出）

统一门禁两轮，都是全量重跑：

| 命令 | 结果 |
|---|---|
| `python tests/run_unified_boundary_guards.py`（13:59，含 Wave 3 注册后首跑） | `SUMMARY total=20 failed=0`，EXIT=0 |
| `python tests/run_unified_boundary_guards.py`（14:2x，§5 守卫扩展后复跑） | `SUMMARY total=20 failed=0`，EXIT=0 |

M5 家族独立 check（逐项 EXIT=0，末行原文摘录）：

| 脚本 | 自报计数（脚本自己的 SUMMARY） | 末行 |
|---|---|---|
| `check_m5_wave3_handoff.py` | 步骤行 `^  ✓` = **71**，✗ = 0 | `RESULT: PASS — Wave 3 连续场景（影子失败转交 / 复用 / 终态父子回写 / §8.5 拒写）全部证成` |
| `check_m5_dual_write.py` | `passed=23 failed=0` | `M5_DUAL_WRITE_SUMMARY passed=23 failed=0` |
| `check_m5_build_graph_revision.py` | `passed=22 failed=0` | `M5_BUILD_GRAPH_REVISION_SUMMARY passed=22 failed=0` |
| `check_kb_scope_isolation.py` | `passed=55 failed=0` | `✓ all KB scope isolation checks passed` |
| `check_build_graph_shadow_retry.py` | 步骤行 24（脚本只报 pass 不报计数） | `BUILD_GRAPH_SHADOW_RETRY_SUMMARY passed result=pass` |
| `check_b0_reindex_chunks.py` | 步骤行 196（含 driver 子进程回显，脚本不报净计数） | `✓ all M5-B0 reindex_chunks checks passed` |
| `check_m5a_revision_backfill.py` | 步骤行 124（含 [A]–[D] 分段回显） | `✓ all M5-A acceptance checks passed` |
| `secret_scanner_selftest`（统一门禁内） | `assertions=53 failed=0` | `SECRET_SCAN_SELFTEST_SUMMARY assertions=53 failed=0 result=pass` |

> 计数口径：按本仓铁律只认脚本**自报 SUMMARY**；无自报计数时给原始 `^  ✓` 行数并标注含子进程回显，
> 不把它当成"净断言数"。历史报告里 `111 项 / 34 项` 的宽口径已废弃。

## 4. Wave 4-2 Go / 前端任务中心对 `reindex_chunks` 的处置验证

**手法声明**：本节是**源码级 + 单测级**验证，未做浏览器/HTTP 活栈实测。原因：本轮禁连真实
Neo4j/Milvus，而 Go 建任务端点一旦放行就会入队并被 worker 消费到真实投影；未放行路径虽零写入，
仍需管理员凭据与活栈，超出"仅验证"边界。因此凡"UI 已实测"字样在本包一律不出现。

实跑证据：

| 命令 | 结果 |
|---|---|
| `go test ./internal/adminstore/... ./internal/httpserver/...`（Windows 宿主） | `ok graphinsight/go-backend/internal/adminstore 0.553s`；`FAIL internal/httpserver [build failed]`：`admin_monitor_native.go:897-898 undefined: syscall.Statfs_t / syscall.Statfs` |
| `go test ./internal/adminstore/ -run 'Job' -v` | `--- PASS: TestNormalizeJobPagination`、`--- PASS: TestBuildJobWhere`、`--- PASS: TestParseObjectJSONForJobs`，`ok … 0.529s` |
| `GOOS=linux GOARCH=amd64 go vet ./internal/httpserver/` | EXIT=0（含测试文件类型检查） |
| `GOOS=linux GOARCH=amd64 go build ./...` | EXIT=0 |

`httpserver` 的 Windows 构建失败是**既有平台限制、非本轮回归**：该文件无 build tag 却调用 Unix-only
`syscall.Statfs`（`go-backend/internal/httpserver/admin_monitor_native.go:896-899`），其最后一次改动是
`a053532`（2026-07-27），Wave 3/4 提交集不含任何 `.go` 文件（`git show --stat 856f7de ee70025 e3aca62`
中 `.go` 命中数为 0）。跨编译目标 vet/build 均绿，说明 Linux 部署形态不受影响。

### 4.1 发现清单（只报不改，等 §9 拍板）

| ID | 事实（file:line） | 影响 |
|---|---|---|
| W4-F1 | Python `JobService.create_job`（`backend/admin/services/job_service.py:191`）**没有 HTTP 调用方**：admin 侧只挂内部唤醒路由（`backend/admin/api/route_registry.py:15-17`、`backend/admin/api/endpoints/jobs.py:24` `internal_router = APIRouter(prefix="/internal/jobs")`，且 `jobs.py:8` 明确 `PYTHON_PUBLIC_ADMIN_API_RETIRED = True`）。生产调用点为空，唯一调用方是取证驱动 `backend/tests/m5_wave3_handoff_driver.py:446/474/568` | GI-9e 的 §16.3 提交路径**当前只能由内部服务/测试触达**；线上无人能提交 reindex_chunks |
| W4-F2 | **入口防线是路由分派，不是建任务白名单**（Wave 5 更正，原判据口径有误）。① HTTP 入口：`switch r.URL.Path`（`admin_jobs_native.go:228`）只列出 `/api/v1/admin/jobs/build-graph`、`/clear-kb`、`/reindex` 三个 case（`:229`），`reindex_chunks` 不在其中 → 落 default 分支返回 **404 `NOT_FOUND`**（`:278`），`adminJobTypeFromPath`（`:686-697`）对未知路径返回 `""`，`CreateJob` 从未被调用。② store 侧第二道线：白名单 `supportedJobTypes`（`go-backend/internal/adminstore/jobs.go:26-30`）= `build_graph/clear_kb/reindex`，`validateJobCreateRequest`（`jobs.go:592-605`，在 `CreateJob:139` 调用、`BeginTx`（`:147`）与 `INSERT` **之前**）返回 `ErrJobValidation`，由 `admin_jobs_native.go:758-761` 映射为 **400 `INVALID_BODY`**——只有内部/测试调用方直接触 `CreateJob` 时才会看到 | 结论不变：前端/运维无法通过 Go 控制面新建 reindex_chunks，且两层拒绝都在写库之前、fail-closed 成立。**但对外可观测语义是 404 而非 400**，runbook 与审计须按 404 判"入口未放行"；把 400 当成入口防线会高估白名的暴露面 |
| W4-F3 | 读侧不校验类型白名单：`buildJobWhere`（`jobs.go:570`）只在 `jobs.go:574-576` 对 `job_type` 做**等值过滤**（值任意，不在 `supportedJobTypes` 也照样进 WHERE）；`ListJobs`（`jobs.go:325`）、`GetJob`（`jobs.go:386`）、`RetryJob`（`jobs.go:181`）、`CancelJob`（`jobs.go:258`）均无 job_type 闸门 | 已存在的 reindex_chunks 行**能被列出、按类型筛出、查看、重试、取消**——写侧关门、读侧开门，语义不对称；`?job_type=reindex_chunks` 在 Go 层是合法读过滤 |
| W4-F4 | `targets_hash` 对上层不可见：Go `JobItem` 结构体（`jobs.go:32-50`）无该字段，`grep -rn "targets_hash" go-backend --include=*.go` = 0 命中；前端同样 0 命中（`grep -rn targets_hash frontend/src` 空） | §16.3 去重结果（reused/retried/rejected）在任务中心**无法核对**，审计只能读 DB |
| W4-F5 | 前端类型与筛选项缺项：`frontend/src/types/admin.ts:732` `JobType = 'build_graph' | 'clear_kb' | 'reindex'`（无 `reindex_chunks`），`frontend/src/pages/Admin/JobsPage.tsx:48-52` `jobTypeOptions` 同样缺项；表格直出原始值（`JobsPage.tsx:472` `{item.job_type}`），URL 参数按选项校验（`JobsPage.tsx:144-146`） | 行能显示（裸字符串），但类型层不认、筛选下拉选不到、`?job_type=reindex_chunks` 深链被忽略。**Wave 5 已按 §11.1-P2 补只读契约**（现树 `admin.ts:734` 含 `reindex_chunks`、`:765` 有 `targets_hash`），本行保留为 Wave 4 快照 |
| W4-F6 | 白名单判定无测试载体：`grep -rn "validateJobCreateRequest\|supportedJobTypes" --include=*_test.go internal/` = 0 命中 | 未来给 Go 放行 reindex_chunks 时，没有回归网兜住 400/404 语义 |

> 与上一轮文档口径的修正：先前 §9.3 把标识符写成 `allowedJobTypes` 并暗示存在 HTTP 409 映射，
> **两处都不实**（实名 `supportedJobTypes`，实际 400 `INVALID_BODY`，全仓无 `JOB_409`）。本轮已在
> `docs/ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md` §9.3、`backend/services/reindex_queue.py:24-29`、
> `backend/admin/services/job_service.py:263-267` 三处按 file:line 改正。

## 5. Wave 4-3 隔离 fail-closed 静态守卫

落点：`backend/tests/check_migration_cleanup_guards.py`（既有 `migration_cleanup` 门禁项内，不新增
门禁 case；注册见 `backend/tests/run_unified_boundary_guards.py:42`）。

新增判据（`_db_isolation_findings`，只对**含 `create_all(`/`create_engine(`** 的 tracked 脚本生效）：

| 规则 | 判据 | 堵的坑 |
|---|---|---|
| R-BARE | 禁止裸 `DATABASE_URL`（正则 `(?<!ADMIN_)(?<![_A-Za-z0-9])DATABASE_URL\b`） | `admin/database.py` 只认 `ADMIN_DATABASE_URL`，写错名 = 静默回落共享开发 PG（本轮事故根因） |
| R-DIALECT | 必须有方言闸门：`dialect.name` + (`!= "sqlite"` 或 `'DIALECT'` 探针回读) | 非 sqlite 时不早停，DDL 直接打到开发库 |
| R-ENV（非 `_driver.py`） | 必须出现 `GRAPHINSIGHT_BACKEND_ENV_FILE` / `ADMIN_DATABASE_URL` / `sqlite:///` | 只靠默认配置建引擎 |
| R-SCOPE | 不建引擎的脚本不进扫描面 | 纯单元脚本被读成假红 |
| R-SURFACE | 枚举只用 `git ls-files`（新 helper `_tracked_backend_tests`），git 退出码非 0、追踪文件缺失、扫描面为空 → **一律 raise** | 未追踪 worktree/venv 污染（本地红、CI 不复现）；以及"扫描面塌成 0 却读成全干净"的假绿灯 |

本轮实跑取证：

| 取证 | 输出 |
|---|---|
| 真实扫描面 | `git ls-files backend/tests/*.py` = **93** 个文件；命中建引擎判据 = **8** 个（不含守卫自身）：`b0_reindex_chunks_driver.py`、`build_graph_shadow_retry_driver.py`、`check_b0_reindex_chunks.py`、`check_kb_migrations_smoke.py`、`check_m5_build_graph_revision.py`、`check_m5_wave3_handoff.py`、`check_m5a_revision_backfill.py`、`m5a_backfill_driver.py` |
| 真树结果 | findings = `[]`；`MIGRATION_CLEANUP_GUARDS_OK`，EXIT=0；守卫项 19 条（`main()` 计数） |
| **真文件红证** | 取扫描面首个真实脚本，在内存里把 `ADMIN_DATABASE_URL` 逐字退化成 `DATABASE_URL`（复现事故形态），守卫判红：`build_graph_shadow_retry_driver.py 写了裸 DATABASE_URL（admin/database.py 只认 ADMIN_DATABASE_URL，未知变量被静默忽略 → 回落 backend/.env 的共享开发 PostgreSQL）` |
| 负向自证（守卫内常驻） | 4 类缺陷样本各命中对应判据；"不建引擎"样本必须 0 findings（防规则过宽）；合规 check / 合规 driver 样本必须 0 findings；空扫描面必须 raise |
| 门禁安静自查 | 守卫源码自带被禁字面量，靠同名排除（`path.name == self_name`）不参与自身扫描 |
| 扩展后全量复跑 | `SUMMARY total=20 failed=0`，EXIT=0（§3 第二轮） |

## 6. 事故记录（用户钦定口径，逐字）

> **事故口径（2026-10-05 裁定，本包照此表述，不加码也不减轻）**：
> 发生共享开发 PG 连接及 DDL 尝试，已检查范围内未观察到持久化变化；
> 因无事前全库快照，保留不可完全判定窗口。

- 触发路径：Wave 4 期间的一次性探针脚本把注入变量写成 `DATABASE_URL`（正确名是
  `ADMIN_DATABASE_URL`），变量被 `admin/database.py` 静默忽略，引擎回落到 `backend/.env`
  的共享开发 PostgreSQL，随后在该连接上发起了 DDL 尝试（`create_all` 形态）。
- 已检查范围（当轮只读复核，逐条可复查）：`ix_admin_jobs_targets_hash` 不存在；
  `admin_jobs` 既有索引 15 个；行数 21；`targets_hash` 全列 NULL。SQLAlchemy 侧解释：
  `SchemaGenerator.visit_table`（`sqlalchemy/sql/ddl.py:1482`）在 `checkfirst=True` 且表已存在时
  早返回，既不产 CREATE TABLE 也不产 CREATE INDEX。
- 不可完全判定窗口的来源：连接前没有全库快照，因此"未观察到变化"不等于"零变化"。本包不把它写成
  "证明无影响"。
- 本轮不做二次连库复核：再次连接只会让这个窗口更长。§5 的静态守卫是**防再犯**措施，不是事后取证。
- 另有已闭环的旧泄漏（非本窗口）：Wave 2 期间 `check_kb_scope_isolation.py` 曾把 1 行
  `chunk_revisions` 写进开发库，删前整表备份在 `artifacts/dev_db_backups/
  chunk_revisions_stray_row_2026-10-05.sql`（gitignored），2026-10-05 经授权删除，`DELETE 1`，
  删后 `SELECT count(*) FROM chunk_revisions` = 0。

## 7. NOT-IMPLEMENTED / 未验证清单（禁止当作已具备能力）

| 项 | 状态 |
|---|---|
| Go 控制面 reindex_chunks 建任务入口 | **NOT-IMPLEMENTED**（入口在路由分派处未放行 → **404 `NOT_FOUND`**；store 白名单是第二道线 → 400 `INVALID_BODY`。Wave 5 单测已钉住两层语义，见 §11.2） |
| HTTP 409 / `JOB_409` 作业状态映射 | **NOT-IMPLEMENTED**（Go、Python 两侧均不存在；本轮实改文档旧误述；Wave 5 加了"出现 409 即判口径漂移"的断言，见 §11.2） |
| `targets_hash` 在 Go DTO / 前端类型可读 | **Wave 5 已实现**（Go `JobItem.TargetsHash`、前端 `JobItem.targets_hash` + `reindex_chunks` 筛选项） |
| `targets_hash` 在任务中心 UI 可见（表格列/详情） | **NOT-IMPLEMENTED**（数据已到前端类型层，页面未渲染；是否加列待拍板，见 §12 P5） |
| Python 侧 reindex_chunks 的 HTTP 提交入口 | **NOT-IMPLEMENTED**（`create_job` 无生产调用方） |
| §5.1 / §5.2 canary runner | **NOT-IMPLEMENTED** |
| 真实 v3 建集合、显式 INT64 `content_revision` 真实写入、切读源 | 未做，未获授权 |
| `check_m5a_live_execution.py --confirm` | 未运行（会写真实库，本轮禁 `--confirm`） |
| 浏览器/HTTP 活栈实测任务中心 | 未做（见 §4 手法声明） |

## 8. 未闭环

1. W4-F1～F6 全部只是**发现**，未动 Go/前端一行代码（本轮边界）。
2. `httpserver` 包在 Windows 宿主无法本地测试；只有跨编译 vet/build 证据。
3. 方向 B（dual_write 影子侧）判定悬置，交审后才裁。
4. 本轮改动以两笔本地提交落库（守卫笔 + 文档笔），**未 push**；远端仍只有 `main`，无 `m5/*` 分支。
5. `stash@{0}` / `stash@{1}` 按用户指令保留未动。
6. （Wave 5 追加口径）本节的"未动 Go/前端一行代码"只描述 **Wave 4 轮次**的边界；Wave 5 已按 §9 的
   P1/P2/P3 裁定动码并就地更正 §4.1 W4-F2 的机制误述，完整记录见 §11。

## 9. 待拍板（审计/用户决定，我给推荐）

| # | 议题 | 选项与推荐 |
|---|---|---|
| P1 | 是否让 Go 放行 `reindex_chunks`（写侧开门） | 推荐 **暂不放行**：先补 W4-F6 的 Go 单测（白名单 + 400 语义）再开，避免开门无网；零迁移、零成本 |
| P2 | `targets_hash` 是否进 Go DTO + 前端类型/筛选 | 推荐 **下一波最小闭环**：Go `JobItem` 加字段、前端 `JobType` 加字面量 + `jobTypeOptions` 加项；不改 DB、不新增迁移 |
| P3 | §16.3 的 409 语义要不要真做 | 推荐 **不做**，改文档口径为"超限拒绝走 Python 3xxx `OPERATION_NOT_ALLOWED` + 结构化 details；Go 提交路径未落地"（本轮已按此改正三处旧误述） |
| P4 | 方向 B 与 push | 本包交审后由用户裁定；当前所有提交只在本地（`git ls-remote origin 'refs/heads/m5*'` 为空） |

## 10. 复核命令（可直接复制）

```bash
cd backend
PYTHONUTF8=1 python tests/run_unified_boundary_guards.py          # 期望 SUMMARY total=20 failed=0
PYTHONUTF8=1 python tests/check_migration_cleanup_guards.py       # 期望 MIGRATION_CLEANUP_GUARDS_OK
PYTHONUTF8=1 python tests/check_m5_wave3_handoff.py               # 期望 RESULT: PASS
cd ../go-backend
go test ./internal/adminstore/ -v                                  # 期望 ok
GOOS=linux go vet ./internal/httpserver/ && GOOS=linux go build ./... # 期望 EXIT=0
cd .. && git ls-remote origin main                                 # 权威远端，勿信本地缓存
```

守卫面自查（判据是否被绕过）：把任一真实脚本源码里的 `ADMIN_DATABASE_URL` 改成 `DATABASE_URL`
（仅内存替换，别落盘），`_db_isolation_findings` 必须报"写了裸 DATABASE_URL"。

## 11. Wave 5 记录（按 §9 的 P1/P2/P3 裁定执行，2026-10-05）

**边界**：不 push、不开 `dual_write`、不做真实库取证/S2/canary；Wave 5 只动 Go 读投影 + 前端只读契约
+ 新增测试 + 文档口径。

### 11.1 裁定 → 落地映射

| 来源 | Wave 5 处置 | 代码落点（file:line，均为现树实测） |
|---|---|---|
| P1 / W4-F6 | 白名单判定与 400/404 语义补单测；**写侧仍不放行** | `internal/adminstore/jobs_test.go`（新增）、`internal/httpserver/admin_jobs_native_test.go`（新增） |
| P1 / W4-F2 | 机制更正就地写入 §4.1（入口=路由 404，白名=第二道线 400），两层各由测试钉住 | `admin_jobs_native.go:228-229`/`:278`/`:686-697`；`jobs.go:26-30`/`:592-605`（`CreateJob:139` 调用、`BeginTx:147` 之前） |
| P2 / W4-F4 | `targets_hash` 进 Go 读投影（**只加读侧，不加写路径**） | `jobs.go:71`（`TargetsHash *string \`json:"targets_hash,omitempty"\``）、`jobColumns:34-51`、`scanJobItem:430`→`:474` |
| P2 / W4-F5 | 前端 `JobType` 加 `reindex_chunks`、`JobItem.targets_hash?`、筛选下拉加"分片重建"（全部只读，页面无新建入口） | `frontend/src/types/admin.ts`、`frontend/src/pages/Admin/JobsPage.tsx` |
| P3 | **不实现 409**；维持 §9 P3 口径，并加"出现 409 即判口径漂移"的断言 | `admin_jobs_native_test.go:77`（表格内 `rec.Code == http.StatusConflict` → `t.Fatalf`；另有 `:149` 同断言） |
| W4-F1 | 未动：Python `create_job` 仍无生产调用方，属真实取证/canary 范畴，待授权 | §7 保持 NOT-IMPLEMENTED |

改动量（`git diff --numstat`，本轮实测）：`jobs.go` 31/102（六处内联列清单收敛为 `jobColumns` 单一真相源）、
`types/admin.ts` 5/1、`JobsPage.tsx` 2/0、`reindex_queue.py` 11/5、`job_service.py` 8/4（两份 docstring
机制改正）；两份 Go 测试为新文件。

### 11.2 新增测试（9 个顶层，全部 PASS）与反假绿设计

| # | 测试 | 钉住的语义 |
|---|---|---|
| 1 | `TestValidateJobCreateRequestWhitelist` | 表驱动 **12 例**：`build_graph/clear_kb/reindex` 放行；`reindex_chunks`、空串、未知类型拒；`MaxRetries` −1/21 拒、20 放行；tenant/project/kb 101 字符拒 |
| 2 | `TestCreateJobRejectsUnsupportedTypeWithoutTouchingStore` | 自建 `recordingDriver` 计次：**白名单拒绝时数据库触达次数必须为 0**（任何真实调用返回 `errNotReady`） |
| 3 | `TestCreateJobSupportedTypeReachesStore` | 配对正向：受支持类型必须**真的触达一次** —— 证明 #2 的"零触达"不是驱动整体失配的假绿 |
| 4 | `TestWriteAdminJobMutationResultMapsValidationToBadRequest` | 表驱动 **4 例**：`ErrJobValidation`→400 `INVALID_BODY`；**同文案但非哨兵**的错误→503 `ADMIN_STORE_UNAVAILABLE`（证明映射按 `errors.Is` 精确匹配、不按字符串）；`ErrJobNotFound`→404；nil→201；并断言任何分支都不得出现 409 |
| 5 | `TestAdminJobCreateRouteReturnsBadRequestOnStoreValidation` | 真路由端到端：store 返回 `ErrJobValidation` → 400，且**不得唤醒 python worker** |
| 6 | `TestAdminJobCreateRouteDispatchesByPathType` | 未知 `/api/v1/admin/jobs/reindex-chunks` → **404 `NOT_FOUND`** 且 `CreateJob` 未被调用（`createReq.JobType` 仍为 `""`）；配对 `/build-graph` → 201 且 `JobType == "build_graph"` |
| 7 | `TestJobColumnsAndScanTargetsStayAligned` | `jobColumns` 列数 == `scanJobItem` 的 Scan 目标数、末列是 `targets_hash`、末目标是 `*sql.NullString` |
| 8 | `TestScanJobItemReadsTargetsHash` | 值真的落到 DTO（`populatedJobScanner` 注入 `sha256:abc`） |
| 9 | `TestScanJobItemKeepsNullTargetsHash` | NULL → `nil` 且 JSON `omitempty` 不出现该字段（历史行语义） |

**列漂移红证（本轮实跑，非回忆）**：在 `jobColumns` 单边插一列（`sed '50a'` 加 `wave5_drift_probe,`）后 ——

```
jobs_test.go:137: jobColumns 19 列但 scanJobItem 只有 18 个目标 —— 位置扫描已错位
--- FAIL: TestJobColumnsAndScanTargetsStayAligned (0.00s)
```

随后从 `/tmp` 备份 `cp` 精确回滚，`cmp` 判 IDENTICAL、`grep -c wave5_drift_probe` = 0、
`git diff --numstat` 仍 31/102、`gofmt -l` 空。这条红证说明 #7 不是同义反复：位置扫描错位会被抓住。

### 11.3 `targets_hash` 的部署耦合（一次性容器实测，未碰共享库）

| 步骤 | 动作 | 实测输出 |
|---|---|---|
| A | 建 17 列表（迁移前形态），跑 Go 的 18 列投影 | `ERROR: column "targets_hash" does not exist`（SQLSTATE 42703） |
| B | `ALTER TABLE admin_jobs ADD COLUMN targets_hash VARCHAR(64) NULL`（逐字抄自 `backend/admin/migrate_jobs_targets_hash.py:122`），再跑 18 列投影 | 成功，`(0 rows)` |
| C | 迁移后再跑**旧 17 列**投影 | 成功 —— 加列对旧读兼容 |
| D | `DROP COLUMN targets_hash`（回滚形态），再跑 18 列投影 | 再次 `column "targets_hash" does not exist` |

结合读路径映射（`ListJobs` 错误 → `admin_jobs_native.go:144-145`、`GetJob` → `:173`，均 503
`ADMIN_STORE_UNAVAILABLE`）得到**部署顺序判据**：**先跑迁移，再上带 `jobColumns` 的 Go 构建**；
反向（回滚了列却没回滚 Go）会让任务列表/详情全量 503。

手法声明：一次性 `postgres:16-alpine` 容器（`--rm`、未发布端口、`POSTGRES_HOST_AUTH_METHOD=trust`），
表是"17 列最小复刻"，只验投影列清单耦合、不复刻约束；跑完 `docker rm -f gi-wave5-pg` 并复核容器列表
已无该名字，其它项目的 postgres 容器全程未触碰。**这不是真实 admin 库取证**（真实取证仍待授权）。

### 11.4 Wave 5 树全量门禁复跑（与 §3 同口径、同判据）

| 门禁 | 结果 |
|---|---|
| `run_unified_boundary_guards.py` | `SUMMARY total=20 failed=0`，EXIT=0；含 `SECRET_SCAN_SELFTEST_SUMMARY assertions=53 failed=0` |
| `check_m5_wave3_handoff.py` | `RESULT: PASS`，✓=71 ✗=0 |
| `check_m5_dual_write.py` | `passed=23 failed=0` |
| `check_m5_build_graph_revision.py` | `passed=22 failed=0` |
| `check_kb_scope_isolation.py` | `passed=55 failed=0` |
| `check_build_graph_shadow_retry.py` | ✓=24，EXIT=0 |
| `check_b0_reindex_chunks.py` | ✓=196，EXIT=0 |
| `check_m5a_revision_backfill.py` | ✓=124，EXIT=0 |
| `check_migration_cleanup_guards.py` | `MIGRATION_CLEANUP_GUARDS_OK` + pytest `19 passed` |
| Go（linux 容器 `golang:1.27`，挂载宿主 module cache、`GOPROXY=off`） | `go build ./...` EXIT=0；`go vet ./...` EXIT=0；`go test ./... -count=1` → 8 包 `ok`、0 FAIL（`httpserver` 12.0s） |
| 前端 | `npx tsc -b --pretty false` EXIT=0；`npx eslint src/pages/Admin/JobsPage.tsx src/types/admin.ts` EXIT=0 |

Python 侧数字与 Wave 4 §3 逐项一致 → Wave 5 改动未引入回归。
**本节数字是写入时的实测快照，复核以 §12 命令的现值为准**——不再复现 §3 那类"把 HEAD/提交数写死进文档"
的自造漂移。（gofmt 修正后 `adminstore`/`httpserver` 两包已单独复跑：`ok … 0.099s` / `ok … 12.041s`。）

**文档落笔后的最终树复跑**（即上表所有绿光都跑在将被提交的这棵树上）：统一门禁 `total=20 failed=0`
EXIT=0；`check_m5_wave3_handoff` `RESULT: PASS` ✓=71；`check_m5_dual_write` `passed=23`；
`check_m5_build_graph_revision` `passed=22`；`check_kb_scope_isolation` `passed=55`；
`check_build_graph_shadow_retry` ✓=24；`check_m5a_revision_backfill` ✓=124；`check_b0_reindex_chunks`
✓=196（两处 ✓ 计数已扣除汇总行 `✓ all … checks passed`，与 Wave 4 同口径）；
`check_migration_cleanup_guards` `GUARDS_OK`；Go 容器 `gofmt -l`（本轮 3 个 Go 文件）无输出、
build/vet EXIT=0、`go test ./... -count=1` 8 包 `ok` 0 FAIL；前端 `tsc -b` 与 `eslint` 均 EXIT=0。
密钥扫描：本轮 8 个改动/新增文件单独跑 `check_artifact_secrets.py` → `frontend/src/types/admin.ts`
报 11 条 `credential_assignment`，**取 HEAD 版本同扫也是同样 11 条**（TS 类型声明的形状匹配，非本轮引入）。
**口径按裁定收紧**：这只说明"该问题不在现有门禁的拦截面内、不会让 CI 变红"，
**不等于全仓库密钥扫描通过**——本轮从未做全仓扫描，CI 的扫描面只有 `artifacts`、
`frontend/playwright-report`、`frontend/test-results`、`logs/dev/*.log`（`ci.yml:418-421`、`616-621`、
`1010-1011`、`1087-1088`），`frontend/src`、`go-backend`、`docs` 全在扫描范围外。
因此这 11 条是**扫描范围外的待复核项**，列为独立议题（§11.5-5），其"非本轮引入"的判断依据仅限
"HEAD 同文件同扫计数相同"这一条证据。

Windows 宿主限制不变：`httpserver` 仍不能在本机编译（`admin_monitor_native.go:896-899` 调 Unix-only
`syscall.Statfs`），Go 侧证据一律来自 linux 容器。

### 11.5 本轮明确未做 / 未验证

1. **未放行 `reindex_chunks` 写侧**：路由 case 列表与白名单都没动，Go 控制面仍不能新建该类任务。
2. **未在任务中心渲染 `targets_hash`**：数据已到前端类型层，页面不展示（是否加列见 §12 P5）。
3. 未做浏览器/活栈 HTTP 实测；未跑真实库取证、S2、canary；`dual_write` 仍默认关闭。
4. 既有漂移未动：前端 `JobItem.progress?` 在 Go 侧无对应字段（本轮发现级记录，不改码、不"顺手优化"）。
5. 既有扫描器形状匹配未动：`frontend/src/types/admin.ts` 的 11 条 `credential_assignment`（TS 类型声明被
   形状规则命中）在 HEAD 版本同样存在。若要消，得改扫描器的结构值判定或加显式放行清单，属独立议题，
   不与 Wave 5 混做。

## 12. Wave 5 待拍板 + 复核命令

| # | 议题 | 裁定（2026-10-05 审计回执，见 §13） |
|---|---|---|
| P5 | `targets_hash` 是否在任务中心可见 | **裁定：暂不在列表加列**，但必须保住可审计性 —— 四条留存前提见 §12.1。等真实 `reused/retried` 样本到手后再决定是否加列 |
| P6 | Go 写侧何时放行 `reindex_chunks` | **裁定：暂不放行**。下一步**不碰共享生产库**：先补 disposable Postgres + Go HTTP 集成验证（§12.2 六项判据），全绿后再单独申请受控真实库取证 |
| P4（沿用） | 方向 B（dual_write）与 push | 维持：dual_write 继续关闭、不进 S2、不做真实库 `--confirm`、不 push。**远端口径收紧**：本轮 `git ls-remote origin` 因 github.com:443 连接超时**未能核实**，故只能写"本轮未执行 push、分支 `m5/dual-write` 无 upstream"，**不得写成"远端已验证一致"** |

### 12.1 P5 事实矩阵（三条路径逐格核实；上一版把三条合写成"已具备"，属过度声明，本轮按裁定拆分并登记缺口）

表名勘误先行：本文先前写的 `admin_job_logs` **不存在**。作业审计行落在 `admin_logs`
（`backend/admin/models.py:72` `__tablename__ = "admin_logs"`），Go 读侧按
`resource = 'job' AND resource_id = $1` 过滤并 SELECT 了 `details`
（`go-backend/internal/adminstore/jobs.go:386-406`）；全仓 `admin_job_logs` 命中数 = 0。

| 路径 | `created` | `reused` | `child_job_id` | `targets_hash` | 证据（file:line + 本轮实跑输出） |
|---|---|---|---|---|---|
| **Python 内部提交**（`JobService.create_job:191` → `_create_reindex_chunks_job:248`） | **有**，键名 `action="job_created"` + `details.enqueued` | **有**，键名 `action="job_reused"` + `details.reused` | **无此键**；等价物 = 该任务自身 id，落在 `admin_logs.resource_id`（`job_service.py:612`），details 里另有 `outcome`（`:340`） | **有**：`details.targets_hash`（`:341`）+ `admin_jobs.targets_hash` 列（`reindex_queue.py:269`/`:282`） | `check_m5_wave3_handoff.py:239` 本轮输出 `✓ 复用被审计（job_reused 落 admin_logs，证明审计面可写）`；`:235-236` 输出 `✓ 任务中心 list/get 回读到同一 targets_hash` |
| **backfill 入队**（`_enqueue_reindex_jobs:803` → `enqueue_reindex_jobs(source="backfill_m5a")`） | **仅 stdout 聚合计数**（`backfill_chunk_revisions.py:1079`），无持久留痕 | **仅 stdout 聚合计数**（同上 `jobs_reused=`） | **无**：`report["jobs"][i].job_id` 只在内存（`reindex_queue.py:302`/`:315`），调用方丢弃；只有 `rejected_detail` 会打印 `job_id`（`:1087`），仍不落库 | **部分**：库内 `admin_jobs.targets_hash` 有；backfill 自己的输出只在 rejected 行点名（`:1088`） | `_enqueue_reindex_jobs` 现已是薄委托（`backfill_chunk_revisions.py:810-818`，不再自带 INSERT），但调用点 `:1077-1090` 只 print 计数与 rejected；`check_b0_reindex_chunks.py:532` 的断言面也只有 `enqueue.get("enqueued") == 1`，`:543` 用 `__JOBS__` 验 admin_jobs 行数 —— **没有任何断言覆盖 backfill 侧逐 target 留痕** |
| **父任务结果**（build_graph 影子失败转交，`source="build_graph_m5_wave3"`） | **有**：`result.reindex_handoff.enqueued` 计数 + `jobs[i].outcome` | **有**：`jobs[i].outcome` 取值域含 `reused`（`reindex_queue.py:54-58`） | **有**，键名 `jobs[i].job_id`（不是 `child_job_id`；全仓 `child_job_id` 命中 0） | **有**：`jobs[i].targets_hash` + 父任务 `result` JSON | `document_graph_service.py:898-902` 生成 report、`:922` 把整份 `reindex_handoff` 放进父任务结果；`job_service.py:753` `latest.result = _to_json_text(result)` 持久化到 `admin_jobs.result`。本轮 `check_m5_wave3_handoff.py` 输出 `✓ 转交报表 enqueued=1 / outcome=enqueued / 来源 build_graph_m5_wave3`、`✓ targets_hash 是 64 位十六进制且等于 payload 的 canonical 复算值`，`RESULT: PASS`（`EXIT=0`） |

**P5 缺口（明确登记，不得写成完整结构化留痕）**：

1. **backfill 路径无逐 target 持久留痕。** 去重判定确实发生（同一 `targets_hash` 不产生第二行，
   `check_b0_reindex_chunks.py:543` 已证），但"新建 / 复用 / 重试"只以聚合计数出现在 stdout，
   `report["jobs"]`（含 `job_id` + `targets_hash`）在 `backfill_chunk_revisions.py:1077` 被丢弃，
   既不写 `admin_logs`，也不落任何表。**只有 `ON CONFLICT DO NOTHING` + 计数不能证明留痕**，
   这正是本次要点名的风险，先于任何"已具备"结论。
2. **键名不统一**：实际是 `enqueued/reused/retried/reset/rejected` + `job_id`，
   不是裁定文本里的 `created/child_job_id`。本轮不改名（会牵动既有断言），列为独立议题。
3. Go 日志读侧 `admin_logs.details` 虽在 SELECT 列内（`jobs.go:401`），但**本轮未做活栈 HTTP 实测**
   （§11.5-3），只证到代码与 SQL 层。

因此 P5 的"列表不展示 ≠ 去重不可见"这条判断**只对第 1、3 行成立**；第 2 行（backfill）
当前是"去重生效、留痕不可见"，要真正收口需补 backfill 侧的结构化落痕（写 `admin_logs`
或持久化 `report["jobs"]`），属新一轮改动，需单独拍板。

### 12.2 P6 的前置集成验证（**设计已获批 2026-10-05，实现进行中**）

落点：一次性容器 `postgres:16-alpine`（不发布端口、跑完即删），schema 由 Python 侧迁移建到临时库，
Go 用真实 `database/sql` 连它跑 HTTP 集成用例。六项判据：

**已批形态（2026-10-05 裁定 D1/D2/D3）**：

- **D1 只验 Python 内部路径**：Go 路由继续 404、`supportedJobTypes` 白名单继续不放行
  （`adminstore/jobs.go:26-30` 不动）。用例里"能创建 reindex_chunks"的一方是 Python
  `JobService.create_job:191` → `_create_reindex_chunks_job:248`，**不得写成"Go store 接受"**。
- **D2 临时库先进"旧 17 列"形态**，再按真实迁移脚本 `admin/migrate_jobs_targets_hash.py` 加列；
  **禁止** `create_all` 一步到位——否则"缺列 503"与"旧列可读"两条判据造不出形态，必假绿。
  （既有可抄的样板：`check_b0_reindex_chunks.py:174-195` 的三件套 bootstrap + 真跑迁移脚本。）
- **D3 一次性 Docker network**：Python 迁移容器与 Go 测试容器都不发布宿主端口，
  跑完删容器 + 删 network，并 re-list 复核零残留。

七项通过标准（全绿才进入"是否放行 Go 写侧"的评估）：

1. 路由未放行时 `POST /api/v1/admin/jobs/reindex_chunks` → **404**（单测已钉，集成层在真实
   Postgres 上复现一次）。
2. Python 内部提交路径能真的建出 `reindex_chunks` 行（`admin_jobs` 有行、`status='pending'`）。
3. 缺 `targets_hash` 列时 Go 读侧 → **结构化 503 `ADMIN_STORE_UNAVAILABLE`**，
   不得退化成"空列表 200"（§11.3 只证到 SQL 层 42703，HTTP 层 503 在集成层补证）。
4. 跑完迁移脚本加列之后，Go 读侧 → **200 且 `targets_hash` 字段可读**。
5. 旧 17 列投影（历史行全 NULL）仍能被 Go 真实查询路径读出。
6. 相同 `targets_hash` 二次提交 → **不产生第二个任务**（`SELECT count(*)` 不增），
   **且能回读既有 child ID**（`report["jobs"][i].job_id` = 既有行 id，非新建 id）。
7. `failed` / `cancelled` 再提交 → 仍复用同一行，分别走 `retried` / `reset` 分支
   （`reindex_queue.py:15-19` 分支表）。

第 6 项特意加了"回读 child ID"：backfill 路径当前把这份留痕丢在内存里（§12.1 P5 缺口 1），
集成层必须把它证出来，否则"去重生效"与"去重可审计"会被混为一谈。

七项全绿之前：不改 Go 写侧白名单、不跑真实库 `--confirm`、不碰共享 PG/Neo4j/Milvus、
不进 S2、不 push、不动 stash。

```bash
# Go —— Windows 宿主不能编译 httpserver，必须走 linux 容器
cd go-backend
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "E:/projects/GraphInsight:/src" -v "C:/Users/yh/go:/go" \
  -w /src/go-backend -e GOPROXY=off -e GOFLAGS=-mod=mod golang:1.27 \
  sh -c "gofmt -l internal/adminstore/jobs.go internal/adminstore/jobs_test.go \
         internal/httpserver/admin_jobs_native_test.go; go vet ./...; go test ./... -count=1"
#   期望：gofmt 三行全空；vet EXIT=0；8 包 ok、0 FAIL
#   单独跑 Wave 5 新增：go test ./internal/adminstore/ ./internal/httpserver/ -run \
#     'Whitelist|WithoutTouchingStore|ReachesStore|JobColumns|TargetsHash|BadRequest|DispatchesByPathType' -v → 9 个 --- PASS

cd ../backend
PYTHONUTF8=1 python tests/run_unified_boundary_guards.py    # 期望 SUMMARY total=20 failed=0
PYTHONUTF8=1 python tests/check_migration_cleanup_guards.py # 期望 MIGRATION_CLEANUP_GUARDS_OK
PYTHONUTF8=1 python tests/check_m5_wave3_handoff.py         # 期望 RESULT: PASS

cd ../frontend
npx tsc -b --pretty false && npx eslint src/pages/Admin/JobsPage.tsx src/types/admin.ts

cd .. && git ls-remote origin main       # 权威远端；refs/heads/m5* 应为空
```

部署顺序判据（源自 §11.3 实测）：先 `python backend/admin/migrate_jobs_targets_hash.py`，
再上带 `jobColumns` 的 Go 构建；回滚该列必须先回滚 Go。

> 上面最后一行 `git ls-remote origin main` 本轮**实际超时未取到值**
> （`Failed to connect to github.com:443 after 21061 ms`），所以 §1 与 §12-P4 的远端口径
> 只写到"未执行 push、分支无 upstream"为止；复核者若在可联网环境跑通这一行，才可升级为
> "远端已核实"。

## 13. Wave 5 审计回执（2026-10-05 裁定，登记进本文以便文档自足）

裁定人（用户）本轮在 `E:\projects\aistudio` 会话内完成审计，**未独立重跑 GraphInsight 最终树**，
结论基于本文 §11 的证据包。因此本文所有"绿"都受 §11.4 的限定约束：**隔离证据，非真实取证**。

| 议题 | 裁定 | 本文落点 |
|---|---|---|
| Wave 5 接收为 | **本地回归与读侧契约完成** | 不得据此宣布方向 B 通过，也不得据此放行 Go 写侧 / 真实库取证 / push |
| P5 `targets_hash` 列表列 | **暂不加列** | 四条留存前提（DTO/前端类型可读、不展示≠不可见、结构化结果或日志必须留痕、等真实 `reused/retried` 样本再议）逐条核实见 §12.1 |
| P6 Go 写侧 `reindex_chunks` | **暂不放行** | 门槛 = §12.2 六项 disposable 集成验证全绿；在那之前不碰共享生产库，全绿后再单独申请受控真实库取证 |
| W4-F2 拒绝机制口径 | 修正**接收** | 入口防线 = 路由 404，白名单 400 是第二道线（§4.1、§7、两份 Python docstring 已改） |
| 409 语义 | 修正**接收** | 全仓无 409 实现，文档只写 NOT-IMPLEMENTED（§11.2 用例 4 的反 409 断言兜回归） |
| 远端口径 | **收紧，强制** | `git ls-remote` 超时未核实 → 只能写"本轮未执行 push、分支 `m5/dual-write` 无 upstream"，禁止写"远端已验证一致"（§12-P4、§12 末复核提醒） |
| 绿色结果口径 | **收紧，强制** | 所有绿色必须标注为隔离证据（§2 分栏、§11.4 方法声明） |
| 密钥扫描口径 | **收紧，强制** | CI 扫描面不含 `frontend/src`、`go-backend`、`docs`；admin.ts 的 11 条命中是**扫描范围外的待复核项**，不得表述为"全仓库密钥扫描通过"（§11.4） |

回执后的状态机（本轮唯一权威口径）：

```
dual_write 继续默认关闭 · 不做真实库 --confirm · 不进 S2 · 不 push
下一轮候选动作 = §12.2 的 disposable Postgres + Go HTTP 集成套件，
其设计先过一轮人工确认再实现。
```

