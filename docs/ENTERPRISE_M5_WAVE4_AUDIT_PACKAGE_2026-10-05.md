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
| A | 建**手写 17 列最小复刻表**（当作"迁移前"替身；真实模型回滚后是 20 列，见 §14.2/§15.1），跑 Go 的 18 列投影 | `ERROR: column "targets_hash" does not exist`（SQLSTATE 42703） |
| B | `ALTER TABLE admin_jobs ADD COLUMN targets_hash VARCHAR(64) NULL`（逐字抄自 `backend/admin/migrate_jobs_targets_hash.py:122`），再跑 18 列投影 | 成功，`(0 rows)` |
| C | 迁移后再跑**那张 17 列复刻表的投影** | 成功 —— 加列对旧读兼容 |
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
   → **Wave 6 已闭环这一条**：一次性 Postgres 上 Go 真实查询路径读回了 Python 写入的
   `admin_logs.details`（含 `outcome`），见 §14.3 表末行。缺口 1（backfill 按 target 留痕被丢弃）
   与缺口 2（键名口径）**本轮未动**，仍是待裁定议题。

因此 P5 的"列表不展示 ≠ 去重不可见"这条判断**只对第 1、3 行成立**；第 2 行（backfill）
当前是"去重生效、留痕不可见"，要真正收口需补 backfill 侧的结构化落痕（写 `admin_logs`
或持久化 `report["jobs"]`），属新一轮改动，需单独拍板。

> **Wave 7 更新（缺口 1 / 2 已收口）**：本节是 P5/Wave 6 时的状态快照，保留不改。Wave 7 按裁定
> 补齐了 backfill 的逐组 §16.3 留痕（缺口 1）并把口径统一为 `created/reused/... + child_job_id +
> targets_hash`（缺口 2），两者的正向与反向（缺 `admin_logs` 表 → 退出码 4）证据见 §15.2 / §15.3。


### 12.2 P6 的前置集成验证（**设计已获批 2026-10-05；七项判据已于 Wave 6 一次性容器全绿，结果与口径修正见 §14**）

落点：一次性容器 `postgres:16-alpine`（不发布端口、跑完即删），schema 由 Python 侧迁移建到临时库，
Go 用真实 `database/sql` 连它跑 HTTP 集成用例。六项判据：

**已批形态（2026-10-05 裁定 D1/D2/D3）**：

- **D1 只验 Python 内部路径**：Go 路由继续 404、`supportedJobTypes` 白名单继续不放行
  （`adminstore/jobs.go:26-30` 不动）。用例里"能创建 reindex_chunks"的一方是 Python
  `JobService.create_job:191` → `_create_reindex_chunks_job:248`，**不得写成"Go store 接受"**。
- **D2 临时库先进"迁移前形态"**（口径修正见 §15.1：Wave 5 曾手写 17 列最小复刻表，Wave 6/7 用真实模型 +
  真实回滚脚本量得的生产迁移前形态是 **20 列、无 `targets_hash`**），再按真实迁移脚本
  `admin/migrate_jobs_targets_hash.py` 加列；
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
5. 迁移前形态（复刻表，`targets_hash` 为 NULL）的旧列投影仍能被 Go 真实查询路径读出。
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

## 14. Wave 6 记录（P6 disposable Postgres 集成套件，2026-10-05）

**结论先行**：§12.2 的七项通过标准在一次性容器拓扑上**全部实跑通过**（`criteria=7 failed_criteria=0
failed_steps=0` / `RESULT: PASS`）。因此 P6 的门槛条件已满足；但**"是否放行 Go 写侧"仍是用户的单独
裁定**（§13 回执原文："P6 disposable 套件全绿后，再单独裁定是否放行 Go 写侧"），本文不代答。
所有绿色**只有 §11.4 的隔离证据口径**：一次性网络、不发布端口、跑完即删、未接触共享 PG/Neo4j/Milvus，
也不等于真实生产库取证。

### 14.1 交付物与角色

| 文件 | 角色 | 关键锚点 |
|---|---|---|
| `backend/tests/check_m5_p6_disposable_pg.py` | 宿主编排器：起网络/容器 → 真实迁移脚本 rollback/migrate → 两个 Go 阶段 → 收尾复查残留 → 七项判据台账 | `preflight:122`、`bring_up:156`、未发布端口断言 `:184-186`、`pg_isready:208`、`py_stage:213`、`migrate:225`、`go_phase:235`、`main:259`、判据登记 `:299/301/328/331/335/351/353/356`、`teardown:380`、零残留复查 `:387-391`、共享栈基线比对 `:393-397`、`finish:400`、汇总行 `:410` |
| `backend/tests/p6_disposable_pg_driver.py` | 容器内 schema 状态机 + Python 内部提交路径（判据 2/6/7） | 钉连接 `pin_env:62`、`assert_disposable:72`（方言打印 `:76`、非 postgresql 即 fatal `:77-78`、DSN 主机核对 `:80-83`、`current_database()` 与"集群内不得出现共享开发库名" `:85-99`）、`job_shape:102`、`stage_bootstrap:142`、旧行 INSERT `stage_assert_old_shape:156-179`、`stage_assert_migrated:182`、`_latest_log:199`、`stage_submit:252`、判据 2 断言 `:294-301`、判据 6 `:303-309`、判据 7 `:311-328`、`__SUBMIT__` 标记 `:330-340`、`stage_dump:343` |
| `go-backend/internal/httpserver/p6_disposable_pg_integration_test.go` | Go 真实 HTTP 读侧 + 路由派发（判据 1/3/4/5/6 读侧/7 读侧，并闭环 §12.1 缺口 3） | env 门控 `t.Skip:59`（CI 永不连库）、真 `adminstore.New` + 仅计数 `CreateJob` 的 `p6ProbeStore:99-145`、`do():147`（先 `rec.Body.String()` 再解码，注释说明原因）、判据 1 `:212-231`、判据 3 `:235-270`、判据 4+6 `:274-310`、判据 5 `:315-340`、Python 留痕回读 `:362+` |
| `backend/Dockerfile.p6test` | 一次性 Python 运行镜像（`python:3.11-slim` + 全量 `requirements.txt`；`job_service` 会经 `services.job_runtime` 牵进 pymilvus/neo4j/openai，缺包即 import 失败） | 构建产物 `gi-p6-py:tmp`，`docker build` 退出码 0 |

编排器**有意不进 20 项统一门禁**（`check_m5_p6_disposable_pg.py:19`）：门禁必须能在无 Docker 的 CI 里跑。
容器内驱动按 Wave 4-3 静态守卫的既有约定被豁免（`check_migration_cleanup_guards.py:585` `DRIVER_SUFFIX`、
`:600` `_db_isolation_findings`）；本轮直接对四个新文件调用该函数复核：`FINDINGS []`（驱动在扫描面内、
因 `_driver.py` 后缀豁免；编排器不在扫描面内，因其不含 `create_all(`/`create_engine(` 字样）。

### 14.2 D1 / D2 / D3 落点对照（含一处**必须裁定的口径修正**）

| 裁定 | 落点 | 实况 |
|---|---|---|
| **D1** 只验 Python 内部路径，Go 路由继续 404、白名单继续不放行 | Python 侧：`JobService.create_job:191` → `_create_reindex_chunks_job:248`（驱动 `stage_submit:252` 起真实 service）；Go 侧：`p6ProbeStore.CreateJob:99` 只计数、断言 `createCalls == 0`（测试 `:228-230`）；`adminstore/jobs.go:26-30` 白名单**一字未动** | ✅ 未把任何结果写成"Go store 接受" |
| **D2** 临时库先进旧形态再按真实迁移脚本加列，禁止 `create_all` 一步到位 | 实际序列：`bootstrap`（模型新形态 21 列）→ **真实** `admin/migrate_jobs_targets_hash.py --action rollback`（→ 20 列、无 `targets_hash`、无部分唯一索引）→ 种一条旧行 → Go `pre_migrate` → **真实** `--action migrate`（→ 21 列 + 索引定义逐字符对上）→ Go `post_migrate` | ⚠️ **比 D2 更严**（D2 只要求"先进旧形态"，本轮用真回滚脚本造形态而非手写复刻），且旧形态列数与 D2 措辞不符，见下 |
| **D3** 一次性 network、两端都不发布宿主端口、跑完删并复核零残留 | `gi-p6-net` + `gi-p6-pg`（`postgres:16-alpine`，`POSTGRES_HOST_AUTH_METHOD=trust` ⇒ DSN 无凭据）+ `gi-p6-py`；全程无任何 `-p`；`docker port` 两次为空；`docker rm -f` ×2 + `docker network rm`；`--filter name=gi-p6` 复查容器与网络均空 | ✅ 见 §14.4 的一处如实残余 |

**口径修正（待裁定，本轮不擅改 §11.3 的历史实测数字）**：D2 文本与 §11.3、§12.2 判据 5 都写"旧 **17** 列形态"。
17 列来自 Wave 5 当时**手写的 17 列最小复刻表**（`gi-wave5-pg`，只验投影列清单耦合，§11.3 第 284 行已自我限定），
**不是生产模型回滚后的真实形态**。本轮用真实模型 + 真实回滚脚本量出来的是：**迁移前 20 列 → 迁移后 21 列**
（`__BOOTSTRAP__ column_count: 21` → `__LEGACY__ column_count: 20` → `__MIGRATED__ column_count: 21`，
原始输出见 `/tmp/p6_run5.txt` 第 26 / 52 / 96 行）。所以：

- §11.3 的 A/C 两行（17 列复刻表）作为**当时那张表的实测记录**保留，数字不改；
- 但"旧 17 列"作为**生产 admin_jobs 的形态描述是错的**，往后的行文应读作"迁移前形态（20 列，无 `targets_hash`）"。
  本轮新代码的注释与阶段标题已按实测措辞（编排器阶段 3 标签、判据 5 标题）。
  **Wave 7 已按此裁定修正**（Wave 7 指令："先修正 17→20 列文档口径"）：§12.2 D2（现第 386 行）与
  判据 5（现第 402 行）的"旧 17 列"字样统一改读作"迁移前形态（20 列，无 `targets_hash`）"。
  §11.3 的 A/C 两行是当时那张手写复刻表的实测记录，数字与措辞保持不动。详见 §15.1。

### 14.3 七项通过标准（本轮真实数字，全绿）

| # | 判据 | 判据来源 | 本轮证据（`/tmp/p6_run5.txt`） |
|---|---|---|---|
| 1 | 路由未放行 → **404** | Go `:212-231` | `--- PASS` ×2 阶段；两种拼写（`reindex_chunks` / `reindex-chunks`）都是 404 + `NOT_FOUND`，非 409；`createCalls=0` |
| 2 | Python 内部路径建出 `reindex_chunks` | 驱动 `:294-301` | 落库 `job_type=reindex_chunks`、`status=pending`、`targets_hash` = canonical 复算值、作用域 `t1/p1/kb-p6`、留痕 `outcome=enqueued` |
| 3 | 缺列时 Go 读侧 → **结构化 503** | Go `:235-270` | 列表/详情/日志三条路径全 503 + `ADMIN_STORE_UNAVAILABLE`；显式断言**不得**是 200；日志必须同时含 `targets_hash` 与 `does not exist`（归因到 42703 缺列） |
| 4 | 加列后读侧 200 且带 hash | Go `:274-310` | 列表 `total=1`、Go 读回的 `targets_hash` **等于 Python 写入值**；详情同值 |
| 5 | 迁移前旧行仍可读、不参与唯一性 | Go `:315-340` | 旧行 id=1 仍在列表，`job_type=build_graph`、`TargetsHash == nil`，并按对象作用域断言该行 JSON **不含** `targets_hash` 键（`omitempty` 语义） |
| 6 | 同 hash 不新增且回读既有 child ID | 驱动 `:303-309` | `__SUBMIT__`：`reindex_chunks_count=1`、二次提交 `job_id` 就是既有行 id、留痕 `action=job_reused` + `outcome=reused`、`for_update_sql_count=1`（**Postgres 行锁分支首次被真实跑到**——SQLite 套件到不了） |
| 7 | failed/cancelled 原地 retry/reset | 驱动 `:311-328` | `outcomes=['enqueued','reused','retried','reset']`；failed→pending 且 `retry_count` 1→2；cancelled→pending 且归零；两次都仍 `count=1`（不新建行） |
| （附） | §12.1 **P5 缺口 3** 闭环 | Go `:362+` | Go 真实查询路径读回 Python 写的 `admin_logs.details`（含 outcome 序列）；编排器把它单列一条 ✓（`check_m5_p6_disposable_pg.py:359`，本轮日志第 136 行 `✓ Go 读侧回读到 Python 留痕（P5 缺口 3 闭环）`） |

本轮 `targets_hash` = `42d6adf0785f8c86e027badadc7997b02f9063bccf0a5c9842e53f029ca61023`，
`job_id=2`（旧行 `id=1` 是 `build_graph`、hash 为 NULL）。迁移后的部分唯一索引定义与 §11.3 逐字符一致：
`CREATE UNIQUE INDEX uq_admin_jobs_targets_hash ON public.admin_jobs USING btree (job_type, kb_id, targets_hash) WHERE (targets_hash IS NOT NULL)`。

### 14.4 隔离取证（D3 的"可观察证据"，不是口头声明）

- **不发布端口**：`docker port gi-p6-pg` / `docker port gi-p6-py` 输出为空（`check_...:184-186`）。
- **钉连接 + 断言方言**：每个阶段先 `DIALECT postgresql`（驱动 `:76`），非 postgresql 直接 fatal；
  `__PIN__ {"current_database": "p6_admin", "server_addr": "192.168.16.2/32", "shared_dev_databases_in_cluster": 0}`
  —— 集群内**不存在**共享开发库名，这一条是"没连错库"的正面证据。
- **DSN 无凭据**：trust 认证 ⇒ DSN 里没有密码/token，符合"密钥不落文档/代码"红线。
- **零残留**：`gi-p6-py` / `gi-p6-pg` / `gi-p6-net` 已删；`docker ps -a --filter name=gi-p6` 与
  `docker network ls --filter name=gi-p6` 均为空。
- **共享栈未受影响**：`graphinsight_default`、`gi-phase3-pg` 的存在性与 preflight 记录的基线一致（`:393-397`）。
- **未破的边界**：Go 写侧白名单未改、真实库 `--confirm` 未跑、共享 PG/Neo4j/Milvus 未碰、未进 S2、
  未 push、未动 stash。
- **如实残余（一项）**：一次性镜像 `gi-p6-py:tmp` 仍在本地（可再生，不影响隔离）。要清就跑
  `docker rmi gi-p6-py:tmp`。本轮没删，是为了留一个能直接复跑 Go 阶段的现成镜像。

### 14.5 反假绿记录：套件先红后绿的四次自纠

（真实缺陷驱动的 red→green 链；如果一次就绿，判据本身才是可疑对象。）

1. `docker version --format {{.ServerVersion}}` 在该 CLI 上报 `can't evaluate field` ⇒ 预检**假红**。
   改成 plain `docker version`，并把子进程输出细节 surfaced（`check_...:122`）。
2. Go 阶段命令漏写 `go`，实际跑了 shell 内建 `test`（`test: extra argument "-run"`，rc=2）⇒
   判据 1/3/4/5 **未评分**。改为显式 `go test`。
3. `json.NewDecoder(rec.Body)` 先把 `httptest.ResponseRecorder` 抽干，随后的 `rec.Body.String()` 为空 ⇒
   判据 5 报"解码失败 raw=（空）"。改为**先取 raw 再解码**（Go 测试 `:147` 附注释）。
4. 判据 5 最初用 `strings.Contains(raw, "\"targets_hash\"")` 扫整个列表体 ⇒ **假红**：同时存在的
   `reindex_chunks` 行本就该带 hash。改为按对象作用域（`env.legacyID` 那一行）做键存在性判断。
   这一条正是"门禁对自己的输出安静"家族的复发苗头，记此以防再犯。

### 14.6 未做 / 未验证（不得据此轮宣称的部分）

- **真实共享库仍零取证**：没跑 `migrate_jobs_targets_hash.py --confirm`，没碰 `gi-phase3-pg` 里的生产形态数据。
- **Go 写侧未放行**：`supportedJobTypes` 不含 `reindex_chunks`；是否放行是 §13 约定的**用户单独裁定**，
  七项全绿只是把门槛凑齐，不构成自动放行。
- **P5 缺口 1 / 2 未动**（Wave 6 口径）：`backfill_chunk_revisions.py:1077` 曾丢弃 `report["jobs"]`（按 target
  的结构化留痕不落表）；键名 `enqueued/reused/...` vs 裁定文本 `created/child_job_id` 曾未统一。
  **→ Wave 7 已收口，正向 + 退出码 4 反向证据见 §15.2 / §15.3。**
- **前端/真实浏览器**：本轮无前端改动，故无 UI 实测口径可声明。
- **密钥扫描**：本轮只对 4 个新文件跑扫描器（`paths=4 files=4 bytes=50074 … findings=0 result=pass`），
  加上本文档；`frontend/src`、`go-backend`、`docs` 整体仍在 CI 扫描面外，
  admin.ts 那 11 条仍是**扫描范围外的待复核项**，绝不表述为"全仓库扫描通过"。
- **远端**：本轮未执行 push，分支 `m5/dual-write` 无 upstream。`git ls-remote` 上一轮超时未取到值
  （§12 末注），所以远端口径到此为止，不写"远端已验证"。

### 14.7 复核命令

```bash
cd backend
PYTHONUTF8=1 python tests/check_m5_p6_disposable_pg.py     # 期望 P6_DISPOSABLE_SUMMARY criteria=7 failed_criteria=0 / RESULT: PASS
PYTHONUTF8=1 python tests/run_unified_boundary_guards.py   # 期望 SUMMARY total=20 failed=0（本轮实跑 GATE_EXIT=0）
docker ps -a --filter name=gi-p6 --format '{{.Names}}'     # 期望空（零残留）
docker network ls --filter name=gi-p6 --format '{{.Name}}' # 期望空
```

Windows 宿主不能编译 `httpserver`（`syscall.Statfs`），所以 Go 判据**只能**在 linux 容器里取证；
Go 用例在无 `GI_P6_PG_DSN` 时整体 `t.Skip`，`go test ./...`（CI）永远不会连库。

---

## 15. Wave 7（2026-10-05）：口径修正 + P5 缺口 1/2 收口 + Go 证据补强（skipped=0）

指令原文："先修正 17→20 列文档口径；补齐 backfill 的 jobs 留痕，并统一 created/reused/child_job_id/targets_hash
结构化字段；补强 Go 集成证据，明确 skipped=0。重跑 Wave 6/P6 门禁后，再单独申请 Go 写侧白名单。继续不跑共享库
`--confirm`，不 push，不进 S2。" 本节所有绿都是**隔离证据**（一次性容器 / SQLite 载体 / 手写复刻表），非真实共享库取证。

### 15.1 口径修正：17 列 → 迁移前 20 列 / 迁移后 21 列

- 事实真相（Wave 6 §14.2 已量得）：真实模型 + 真实回滚脚本下，`admin_jobs` 迁移前 **20 列**（无
  `targets_hash`）、迁移后 **21 列**。"旧 17 列"来自 Wave 5 手写的 17 列最小复刻表，只是投影列清单耦合的替身，
  **不是生产形态**。
- 本轮按裁定改的两处行文（§14.2:506 的"待裁定"据此结清）：
  - §12.2 D2（现第 386 行）："旧 17 列形态" → "迁移前形态（20 列、无 `targets_hash`）"，并指向本节；
  - §12.2 判据 5（现第 402 行）："旧 17 列投影" → "迁移前形态（复刻表，`targets_hash` 为 NULL）的旧列投影"。
- **不动**：§11.3 A/C 两行是当时那张手写复刻表的实测记录，数字与措辞保留；§14.2 的实测列数账不改。

### 15.2 P5 缺口 1：backfill 逐组 §16.3 留痕落 `admin_logs`（正向 + 退出码 4 反向）

改点（单一真相源复用，不新造形状）：`backend/admin/backfill_chunk_revisions.py`

- `_write_reindex_audit(job_report, trace_id=...)`（`:822`）：把 `report["jobs"]` 逐条写进 `admin_logs`，
  details 由 `services.reindex_queue.audit_details(...)`（`reindex_queue.py:80`）生成，形状与 Python 内部提交、
  父任务转交三条路径**同源**。整批一次事务：任一条抛错 → 全批回滚并记 `REINDEX_AUDIT_WRITE_FAILED`
  （`:1176`），不留半写。`resource_id = child_job_id`（`:879`）。
- stdout 聚合行键名同步改口径：`jobs_created=`（`:1154`）、`REINDEX_REJECTED child_job_id=`（`:1162`）。
- 新增退出码 **4（留痕未落盘）**：`admin_logs` 表缺失 → 显式失败（`:846`），优先级高于闭环门（先给 4 再谈 3），
  审计缺失绝不被计数掩盖。

证据（均为隔离证据，本轮 `/tmp/w7_m5a.txt` 实跑，`M5A_EXIT=0`，`✓` 149 步、`✗` 0）：

| 判据 | 输出行 |
|---|---|
| 缺口 1 正向：run() 把逐组 §16.3 结果写进 `admin_logs`（1 行 `job_created`，非仅 stdout 计数） | `/tmp/w7_m5a.txt:58` |
| 复用轮追加 `job_reused` 行并回读同一 `child_job_id`（留痕到实例，不只聚合数） | `/tmp/w7_m5a.txt:61` |
| 缺口 2：details 用 `created/child_job_id/targets_hash`，旧名 `enqueued/job_id` 不得出现 | `/tmp/w7_m5a.txt:59` |
| 退出码 4 反向守卫：`admin_logs` 表缺失 → exit 4 + `REINDEX_AUDIT_WRITE_FAILED`（不静默、不返 3） | `/tmp/w7_m5a.txt:68` |

### 15.3 P5 缺口 2：结构化词表统一（created / child_job_id / targets_hash）

冻结后的唯一口径（三条写入路径 + Go 读侧共用 `reindex_queue` 的常量与映射，不再有 `enqueued`/边界 `job_id`）：

| 语义 | outcome 值（`details.outcome`） | action 值（`admin_logs.action`） | 边界键（details 里指向既有子任务） |
|---|---|---|---|
| 新建 | `created` | `job_created` | `child_job_id` = 新行 id |
| 复用（同 hash 命中 pending） | `reused` | `job_reused` | `child_job_id` = 既有行 id |
| 失败原地重试 | `retried` | `job_reused` | `child_job_id` = 既有行 id |
| 取消原地复位 | `reset` | `job_reused` | `child_job_id` = 既有行 id |
| §16.3 拒写 | `rejected` | `kb_chunk_reindex_failed` | `child_job_id`（`rejected_detail[0]` 带） |

- 聚合键：`AGGREGATE_KEYS = ("created","reused","retried","reset","rejected")`（`reindex_queue.py:68`）。
- 说明保留：Python **局部变量/kwargs/SQL bind 名仍叫 `job_id`**（`:853` 等），改的是**结构化留痕的对外键名** =
  `child_job_id`；二者不是同一层，无残留歧义。全仓 `child_job_id` 命中已从 0 变正，旧边界键 `job_id`
  在 details 断言中作为**负向判据**出现（`p6_disposable_pg_integration_test.go:398` 要求 `job_id` 键不得存在）。

证据（`/tmp/w7_p6.txt`，一次性 PG + Go 容器，`RESULT: PASS`）：

| 判据 | 输出行 / marker |
|---|---|
| 判据2 留痕 `outcome=created` 且带同一 `targets_hash` | `/tmp/w7_p6.txt:101` |
| 判据2 留痕 `child_job_id` = 新建行 id（键名 `child_job_id`，不用 `job_id`） | `/tmp/w7_p6.txt:102` |
| 判据6 复用留痕 `outcome=reused` + `action=job_reused` | `/tmp/w7_p6.txt:105` |
| 判据7 `retried`/`reset` 各 1 条留痕 | `/tmp/w7_p6.txt:109`/`:112` |
| marker 四路径 outcomes 序列 = `[created, reused, retried, reset]`，`detail_child_job_ids=[2,2,2,2]`（四次指向同一既有行），`targets_hash=42d6adf0…61023` | `/tmp/w7_p6.txt:149` |

### 15.4 Go 集成证据补强：skipped=0 显式化

改点：`backend/tests/check_m5_p6_disposable_pg.py` 把 Go 阶段从"前缀 `-run TestP6`"改为**每阶段精确用例名
alternation**（`GO_EXPECTED_TESTS`，`:60`），并用集合相等核对"预期用例 == 实际跑到用例"，所以：

- 阶段内不再产生 off-phase 的 `t.Skip`（不匹配的 phase 用例根本不进 run 列表）；
- "matched 0 tests" 单独判红；`skipped` 由 `verdicts` 里 SKIP 计数得出（`:274`），断言 `failed==0 and skipped==0`
  （`:295`），并打印 `GO_EVIDENCE phase=… expected=… ran=… passed=… failed=… skipped=…`（`:297`）；
- 收尾账只有两阶段都跑完才给 `go_phases=2 skipped=<n>`，否则写 `skipped=NA`，**杜绝把"没跑"读成"零跳过"**（`:459-463`）。

证据（`/tmp/w7_p6.txt`，本轮实跑）：

```
GO_EVIDENCE phase=pre_migrate  expected=2 ran=2 passed=2 failed=0 skipped=0   （:59）
GO_EVIDENCE phase=post_migrate expected=4 ran=4 passed=4 failed=0 skipped=0   （:119）
P6_DISPOSABLE_SUMMARY criteria=7 failed_criteria=0 failed_steps=0 go_phases=2 skipped=0   （:155）
RESULT: PASS   （:156，全文 ✗ 计数 = 0）
```

判据 6 同步升级：断言 `detail_child_job_ids` 四条全等且 = 既有行 id，把"复用/重试/复位都回读同一子任务"钉进留痕对账。

### 15.5 Wave 7 总回归（本轮真实数字，全绿；均隔离证据）

| 门禁 / 套件 | 命令 | 结果 |
|---|---|---|
| M5-A backfill 全量（含缺口 1/2 + 退出码 4 守卫） | `check_m5a_revision_backfill.py` | `M5A_EXIT=0`，✓149 步、✗0，末行 `all M5-A acceptance checks passed` |
| B0 reindex_chunks 闭环 | `check_b0_reindex_chunks.py` | `B0_EXIT=0`，`all M5-B0 reindex_chunks checks passed` |
| Wave 3 转交连续场景 | `check_m5_wave3_handoff.py` | `WAVE3_EXIT=0`，`RESULT: PASS`，`转交报表 created=1 / outcome=created`（口径已是 created） |
| P6 一次性 PG + Go 七判据 | `check_m5_p6_disposable_pg.py` | `criteria=7 failed_criteria=0 failed_steps=0 go_phases=2 skipped=0`，`RESULT: PASS` |
| 统一边界门禁 | `run_unified_boundary_guards.py` | `SUMMARY total=20 failed=0`，密钥自测 `assertions=53 failed=0`，`GUARDS_EXIT=0` |
| gofmt（改动的 Go 测试文件） | `gofmt -l` | 输出空，`gofmt-exit=0` |
| 密钥扫描（本轮 13 个改动文件，**扫描范围外的目录仍为待复核项**） | `check_artifact_secrets.py --path ×13` | `paths=13 files=13 findings=0 result=pass`（非"全仓库扫描通过"） |

- 一次性容器/网络零残留：`docker ps -a --filter name=gi-p6`、`docker network ls --filter name=gi-p6` 均空
  （`/tmp/w7_p6.txt:141-142`）；D3 边界维持。

### 15.6 仍守的边界 + Wave 7 收口后的单独申请

维持（本轮复核）：不改 Go 写侧白名单、不跑真实库 `--confirm`、不碰共享 PG/Neo4j/Milvus、不进 S2、
不 push、不动 stash。`git branch --show-current = m5/dual-write`，`git rev-parse --short HEAD = 15662b1`，
13 个改动文件未 staged，本轮只做**本地提交**。远端口径止于"本轮未执行 push、`m5/dual-write` 无 upstream"。

- **Go 写侧白名单：Wave 6/P6 门禁已重跑全绿、缺口 1/2 已收口、skipped=0 已显式化 —— 门槛凑齐。
  是否把 `reindex_chunks` 加入 `supportedJobTypes`（`adminstore/jobs.go:26-30`）放行，按 §13 约定
  仍是用户单独裁定；本轮不自行放行，改为在交付后正式提出申请。**

### 15.7 复核命令（Wave 7 口径）

```bash
cd backend
PYTHONPATH= python tests/check_m5a_revision_backfill.py    # 期望末行 all M5-A acceptance checks passed，含退出码 4 守卫
PYTHONPATH= python tests/check_b0_reindex_chunks.py        # 期望 all M5-B0 reindex_chunks checks passed
PYTHONPATH= python tests/check_m5_wave3_handoff.py         # 期望 RESULT: PASS（报表口径 created=1）
PYTHONPATH= python tests/check_m5_p6_disposable_pg.py      # 期望 criteria=7 failed=0 … go_phases=2 skipped=0 / RESULT: PASS
PYTHONPATH= python tests/run_unified_boundary_guards.py    # 期望 SUMMARY total=20 failed=0
```

## 16. Wave 8：Go 写侧完整提交链 + disposable PG 真实写入验收

**本轮指令口径**：继续实施 Go 写侧完整提交链，并在 disposable PG 中完成真实 Go HTTP 写入验收；
**不得仅增加白名单后宣告放行**；必须证明 §16.3 去重、并发唯一性、原地重试、child ID 回读、
作用域校验与审计留痕；完成后提交最终树与可回读证据再交审。本轮不执行共享库 `--confirm`、不 push、不进 S2。

### 16.1 写侧链路落点（route → store → DB → audit）

| 环节 | 位置 | 作用 |
|---|---|---|
| 路由分派与入口防线 | `httpserver/admin_jobs_native.go`（`submitReindexChunksJob`） | 未列类型走 **路由 404**；`supportedJobTypes`（`adminstore/jobs.go:26-30`）的存储层 400 只是第二道线，本轮未把 `reindex_chunks` 加进去 |
| targets 解析 | `admin_jobs_native.go:368-404` | 逐条要求对象形状 + `chunk_id` 非空 + `target_revision` 是整数语义（`:414` `targetRevisionFromJSON`，小数/字符串/布尔在入口拒，避免两端算出不同 hash） |
| 作用域冻结 | `requireJobKnowledgeBase` → `freezeJobPayloadScope` | tenant/project/kb 取服务端 KB 行，不采信客户端字符串 |
| 去重入队 | `adminstore/reindex_queue.go:101` `EnqueueReindexChunks` | 单事务内分组 → `insertReindexJob`（`:204` `ON CONFLICT (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL DO NOTHING`）→ 零行则 `resolveReindexConflict`（`:258`，`:266` `FOR UPDATE`） |
| §16.3 冲突分支 | `reindex_queue.go:276-295` | pending/running/succeeded→reused；failed 且额度未用尽→原地 retry；failed 且 `retry_count>=max_retries`→**rejected（不复活）**；cancelled→复位归零 |
| 异常哨兵 | `reindex_queue.go:36` `ErrReindexEnqueueAnomaly` | 命中唯一索引却 `FOR UPDATE` 读不到行了 |
| 审计留痕 | `reindex_queue.go:352` `writeReindexAudit` → `adminstore/jobs.go:641-671` `insertJobAuditLogEntry` | `resource='job'`、`resource_id=<jobID>`、`user_id`/`operator_id` 同值；拒绝留痕 `status='failed'` + `error_message` |
| 错误映射 | `admin_jobs_native.go:426-448` | 400 REINDEX_SCOPE_REQUIRED / 400 INVALID_BODY / 503（两条分句，见 §16.4 缺陷 2）；**Go 侧无 409**，§16.3 的 `409 JOB_409` 仍是 NOT-IMPLEMENTED |

### 16.2 判据 8–14 逐条与取证

编排器把判据台账从 7 项改为 `BASE_CRITERIA_COUNT(7) + len(W8_GO_WRITE_CRITERIA)(7) = 14`
（`check_m5_p6_disposable_pg.py:76/80/463-464/505`），写侧每项钉一个 Go 用例名，缺一即红。

| # | 判据 | 用例 | 关键断言（全在真实 Go HTTP + 真实 PG 上） |
|---|---|---|---|
| 8 | child ID 回读 + 跨语言 hash 对等 | `TestP6GoWriteReusesPythonSubmittedJob` | Go 用 Python 那批 targets **原文**（`p6TargetsFromArrayJSON`，`p6_…_test.go:238`，只剥外层方括号、绝不重序列化）重提交 → 200 且 `id == Python 行 id(2)`、`targets_hash` 与 Python 写入值逐字相同（`42d6adf0…61023`）、该 hash 仍 1 行、`createCalls==0`（不走裸 INSERT 的 CreateJob） |
| 9 | §16.3 去重（Go 路径） | `TestP6GoWriteCreatesRowAndDedupesResubmits` | 首批 201 新建；原样重提交与**换序重提交**都 200 复用同一 id；同 hash 恒 1 行；作用域三元组冻结自服务端 KB 行；`requested_by == 夹具操作员`（`:816`） |
| 10 | 原地 retry/reset | `TestP6GoWriteRetriesAndResetsInPlace` | failed(1)→retry_count=2 且行仍 pending/同一 id；cancelled(2)→retry_count 归零；全程该 hash 1 行 |
| 11 | 额度耗尽拒绝 + 失败留痕 | `TestP6GoWriteRejectsExhaustedRetriesAndAudits` | 400 `JOB_MAX_RETRIES_REACHED` + `child_job_id`/`targets_hash`/`retry_count`/`reason=retry_exhausted`；行**不动**（failed/3/原 error_message）；库侧 1 条 `job_rejected` 且 `status='failed'`、`user_id`/`operator_id` 均为操作员（`:971`） |
| 12 | 并发唯一性 | `TestP6GoWriteConcurrentSameHashKeepsSingleRow` | 两 goroutine 同 hash 并发：都成功、回读同一 id、hash 相同、最多一个 201、库里 1 行（ON CONFLICT 第一道 + `FOR UPDATE` 第二道） |
| 13 | 审计形状跨语言一致 | `TestP6GoWriteAuditMatchesPythonFrozenKeys` | `details` 键集合 = Python 冻结 13 键（不多不少、无旧键 `job_id`）；聚合计数 created=1 其余 0；**同一库侧检索式**（`resource='job' AND resource_id='<id>'`）命中 1 条且带操作员身份（`:1063-1071`） |
| 14 | 作用域校验在入队前 | `TestP6GoWriteBlocksCrossScopeAndUnknownKBBeforeEnqueue` | 跨租户 400 `KB_CROSS_SCOPE`（按契约 §2.9 不映射 403）、未知 kb_id 404 `KB_NOT_FOUND`；`enqueueCalls==0 && createCalls==0`；该 KB 行数零增长 |

编排器另加写侧夹具前置闸门（`:460-462`）：`doc_id` / `payload_targets` 任一为空即判红，
不允许"夹具没铺上→用例空转→绿灯"的链路。

### 16.3 操作员身份与外键事实（本轮实测，非推断）

一次性库里 `\d admin_jobs` 实读：`admin_jobs.requested_by → admin_users(id)`
（约束名 `admin_jobs_requested_by_fkey`）；`admin_logs.user_id`/`operator_id` 同样指向 `admin_users(id)`。
两侧写入口径不同：Python 内部入队 `requested_by=None`（不需要操作员行），Go 写侧把认证 UserID 写进
`requested_by` 并同值写入审计两列 —— 所以夹具**必须**种下 `admin_users` 行。落法：

- 驱动 `seed_operator`（`p6_disposable_pg_driver.py:144`，`OPERATOR_ID = 1`，`:39`）幂等插行并 `setval` 序列，
  BOOTSTRAP 标记回带 `operator_id`（`:187`）；
- 编排器把该值注入 `GI_P6_OPERATOR_ID`（`check_m5_p6_disposable_pg.py:441`），**不在 Go 侧重复写魔数**；
- Go 侧 fail-closed：`requirePostMigrate` 缺该 env 即 Fatalf（`p6_…_test.go:117`），
  `p6OpenFixtureDB` 在写任何一行前探测 `admin_users` 命中数（`:286`），缺口当场红且说出真名。

写侧闸门一律用 `t.Fatalf` 不用 `t.Skip`：判据 8–14 必须"跑到"，跑到数由编排器
`GO_EVIDENCE` 的 `expected/ran/passed/failed/skipped` 五元组钉死。

### 16.4 本轮抓出并修掉的两处真实缺陷

1. **提交体形状错（判据 8 首跑 400）**：`submitReindex` 的第 4 参数是 targets **数组内部**形状，
   而 Python 标记里的 `payload_targets` 是数组原文，直接塞进去拼成 `targets:[[…]]` →
   `payload.targets 必须是对象列表`。修 = 新增 `p6TargetsFromArrayJSON`，只做"剥外层方括号 +
   逐元素原始字节拼接"，任何重序列化都可能让两语言 hash 不同形，判据就退化成"各自算各自的对"。
2. **路由 catch-all 文案误导排障（判据 9–13 首跑 503）**：`writeReindexEnqueueError` 的 `default:`
   把**任何** store 错误都说成"命中去重索引但读不到既有任务行"。首跑真实原因是夹具缺 `admin_users`
   行使 `requested_by` 外键违例（同一条 SQL 把 `requested_by` 改 NULL 即可插入 —— psql 原始复现），
   却被这句话把排障方向整个带偏。修 = 拆成 `errors.Is(err, ErrReindexEnqueueAnomaly)` 专句
   （`:436-440`）+ 通用"存储层不可用"（`:441-447`），并在 `admin_jobs_native_test.go` 补双向断言：
   去重异常必须点名去重索引，外键违例**不得**复用该文案（含"同文案但非哨兵错误"的精确匹配证伪）。

### 16.5 Wave 8 总回归（本轮真实数字，全绿；均为**隔离证据**）

| 门禁 / 套件 | 结果（取自本轮日志原文） |
|---|---|
| P6 disposable PG + Go 七项写侧判据 | `P6_DISPOSABLE_SUMMARY criteria=14 failed_criteria=0 failed_steps=0 go_phases=2 skipped=0`，`RESULT: PASS`，全文 `✗` 计数 0；`GO_EVIDENCE pre_migrate expected=2 ran=2 passed=2 failed=0 skipped=0`、`post_migrate expected=11 ran=11 passed=11 failed=0 skipped=0`。**提交前在同一棵树上复跑一次**（`output/w8/p6_run_w8c.log`，`P6_REAL_EXIT=0`），关键行与首跑 `p6_run_w8b.log` 逐字一致，含收尾 dump `admin_jobs 最终 7 行；job 留痕 16 条` 与 `job#13 ... status=failed retry=3` |
| 收尾 dump（人工可复查） | `admin_jobs` 7 行 / job 留痕 16 条；`job#13 reindex_chunks status=failed retry=3 targets_hash=有`（拒绝分支未复活行的直接证据） |
| 统一边界门禁 | `SUMMARY total=20 failed=0`，`EXIT_run_unified_boundary_guards=0`（**提交前在同一棵树上复跑**，`output/w8/w8c_gates.log`；本轮早先一次为 `output/w8/w8_gates.log`，同数） |
| 迁移清理守卫 | `MIGRATION_CLEANUP_GUARDS_OK`，EXIT=0（`output/w8/w8_gates.log`；本轮其后未再触及迁移/守卫文件） |
| M5-A backfill 全量 | `✓ all M5-A acceptance checks passed`，`EXIT_check_m5a_revision_backfill=0`（提交前复跑，`w8c_gates.log`） |
| B0 reindex_chunks 闭环 | `✓ all M5-B0 reindex_chunks checks passed`，`EXIT_check_b0_reindex_chunks=0`（提交前复跑，`w8c_gates.log`） |
| Wave 3 转交连续场景 | `RESULT: PASS — Wave 3 连续场景（影子失败转交 / 复用 / 终态父子回写 / §8.5 拒写）全部证成`，`EXIT_check_m5_wave3_handoff=0`（提交前复跑，`w8c_gates.log`；`✓` 行 71，与 Wave 7 同数） |
| M5 dual_write / build_graph_revision | `M5_DUAL_WRITE_SUMMARY passed=23 failed=0` / `M5_BUILD_GRAPH_REVISION_SUMMARY passed=22 failed=0`（`w8_gates.log`；其后未改这两项覆盖的源码） |
| KB 作用域隔离 / build_graph 影子重试 | `passed=55 failed=0` / `✓` 行 24，EXIT=0（`w8_gates.log`；其后未改这两项覆盖的源码） |
| Go（`golang:1.27` 容器，`GOPROXY=off`） | `gofmt -l`（本轮 9 个 Go 文件，清单见 §16.8）输出空；`BUILD_EXIT=0`、`VET_EXIT=0`、`go test ./... -count=1` → 8 包 `ok`、0 FAIL（`output/w8/go_suite_w8c.log`）。**注意口径**：裸套件里 `p6_disposable_pg_integration_test.go` 受 env 门控会 skip（所以这轮 httpserver 只 1.5s），它证明的是"编译+读侧/单测不回归"；判据 8–14 **真跑到**的证据只认 P6 编排器日志里的 `GO_EVIDENCE expected/ran/passed/failed/skipped` 五元组 |
| 前端（`admin.ts` 注释口径修正） | 提交前复跑：`TSC_REAL_EXIT=0`、`ESLINT_REAL_EXIT=0`（`output/w8/w8c_tsc.log`；退出码由 `echo $?` 直接取，不经过管道） |
| 密钥扫描（HEAD 对照法） | 8 个已跟踪改动文件：工作树命中 34 条，`git show HEAD:` 同扫也是**同样 34 条**；提交前把两份输出解析成 `(文件, kind, match_sha256, 掩码值)` 四元组集合排序比对 → `head 34 worktree 34 IDENTICAL`，本轮零新增。6 个新增文件单独扫 `findings=0 result=pass`（`SECRET_SCAN_SUMMARY paths=6 files=6 ... findings=0 result=pass`）。34 条是 `go-backend`/`frontend/src` 这类 **CI 扫描范围外的待复核项**（CI 只扫 `artifacts`、`playwright-report`、`test-results`、`logs/dev/*.log`），**不等于全仓库扫描通过** |
| 一次性资源零残留 | `docker ps -a` / `docker network ls` 过滤 `gi-p6*` 均空（P6 编排器自查 + 提交前复跑后再查一次）；共享栈 4 个容器（`graphinsight-go-gateway` / `-postgres` / `-neo4j` / `-milvus`）状态仍是 `Up 6 hours`，本轮没有重启或改写它们 |

### 16.6 本轮维持的边界

不跑共享库 `--confirm`（`check_m5a_live_execution.py --confirm` 未运行）、不碰共享 PG/Neo4j/Milvus、
不进 S2、**不 push**、不动 stash（`GI-11` 的 `stash@{0}/stash@{1}` 原样保留）。
本地态：分支 `m5/dual-write`，Wave 8 之前的 HEAD `97645ac`（Wave 7）；本轮最终树已作为其后继
**本地提交** `1141700`（`feat(m5): Wave 8 Go 写侧提交链 + disposable PG 七项写侧判据（8–14）`，
15 files changed, 2517 insertions(+), 37 deletions(-)）。提交前在同一棵树上复跑过 P6 / Go 套件 /
4 项 Python 门禁 / tsc+eslint / 密钥对照扫描，数字见 §16.5 与 §16.9。
远端口径：`git rev-parse --abbrev-ref @{u}` → `fatal: no upstream configured for branch 'm5/dual-write'`；
`git ls-remote origin refs/heads/m5/dual-write` 空输出（远端无该分支引用）。
补记上述 hash 的这笔后续提交只改本文档，不含任何代码或夹具改动，提交后用 `git status` 复核过。

`reindex_chunks` 是否进 `supportedJobTypes` 通用建任务白名单仍是用户单独裁定项；本轮接通的是
**专用提交端点**，没有自行放行通用路径。

**stage 清单口径**：`git status` 会把 `adminstore/{client,configs,logs,monitor,monitor_test,rbac_bindings,rbac_seed,users}.go`
这 8 个文件也列为 modified，但逐个 `git hash-object` 与 `git rev-parse HEAD:<path>` 比对结果**全部 SAME**
（stat 缓存造成的假 dirty，`core.autocrlf=false` + `.gitattributes` 钉 `eol=lf`），本轮没有 stage 它们，
也不是把它们的改动漏在提交外。本次 stage 的 15 个文件：

```
backend/tests/_w8_gen_vectors.py                 （新增，hash 向量生成器，见 §16.7-1）
backend/tests/check_m5_p6_disposable_pg.py
backend/tests/p6_disposable_pg_driver.py
docs/ENTERPRISE_M5_WAVE4_AUDIT_PACKAGE_2026-10-05.md
frontend/src/types/admin.ts
go-backend/internal/adminstore/jobs.go
go-backend/internal/adminstore/reindex_queue.go       （新增）
go-backend/internal/adminstore/reindex_queue_test.go  （新增）
go-backend/internal/adminstore/targets_hash.go        （新增）
go-backend/internal/adminstore/targets_hash_test.go   （新增）
go-backend/internal/adminstore/testdata/targets_hash_vectors.json （新增）
go-backend/internal/httpserver/admin_jobs_native.go
go-backend/internal/httpserver/admin_jobs_native_test.go
go-backend/internal/httpserver/admin_control_plane_routes_test.go
go-backend/internal/httpserver/p6_disposable_pg_integration_test.go
```

### 16.7 待拍板 / 未闭环

1. `backend/tests/_w8_gen_vectors.py` 被 `adminstore/targets_hash_test.go:21` 作为跨语言向量的生成来源
   引用。**本轮裁定：随本轮提交入库**（纯 hash 复算，不建引擎、不触库），否则 checkout 后引用悬空、
   没人能重算那 11 条向量。若审核认为该生成器不该进主干，删除该文件不会影响 Go 用例
   —— 用例只读 `testdata/targets_hash_vectors.json`，生成器只是可复算的证明。
2. disposable PG 只证明 §16.3 的**写侧语义**，共享库上的真实 backfill/迁移仍要 `--confirm` 授权后另跑。
3. `admin_logs` 里 Python 留痕 `user_id/operator_id` 为 NULL（内部入队无操作员），Go 留痕带 id ——
   检索式已按"只认非 NULL 的必须等于操作员"落，若后续要求 Python 侧也带人，需要另一轮改动。

### 16.8 复核命令（Wave 8 口径）

```bash
cd backend
PYTHONPATH= python tests/check_m5_p6_disposable_pg.py    # 期望 criteria=14 failed_criteria=0 failed_steps=0 go_phases=2 skipped=0 / RESULT: PASS
PYTHONPATH= python tests/run_unified_boundary_guards.py  # 期望 SUMMARY total=20 failed=0
PYTHONPATH= python tests/check_m5a_revision_backfill.py  # 期望 ✓ all M5-A acceptance checks passed
PYTHONPATH= python tests/check_b0_reindex_chunks.py      # 期望 ✓ all M5-B0 reindex_chunks checks passed
PYTHONPATH= python tests/check_m5_wave3_handoff.py       # 期望 RESULT: PASS

# Go：Windows 宿主不能编译 httpserver，必须 linux 容器（GOPROXY=off 证明零新依赖）。
# gofmt 的文件清单 = 本轮改动的 9 个 Go 文件，逐字列出，期望输出为空。
MSYS_NO_PATHCONV=1 docker run --rm -v "E:/projects/GraphInsight:/src" -v "C:/Users/yh/go:/go" \
  -w /src/go-backend -e GOPROXY=off -e GOFLAGS=-mod=mod golang:1.27 \
  sh -c "gofmt -l internal/adminstore/reindex_queue.go internal/adminstore/reindex_queue_test.go \
         internal/adminstore/targets_hash.go internal/adminstore/targets_hash_test.go internal/adminstore/jobs.go \
         internal/httpserver/admin_jobs_native.go internal/httpserver/admin_jobs_native_test.go \
         internal/httpserver/admin_control_plane_routes_test.go internal/httpserver/p6_disposable_pg_integration_test.go; \
         go build ./... ; echo BUILD_EXIT=\$? ; \
         go vet ./...  ; echo VET_EXIT=\$?  ; \
         go test ./... -count=1 ; echo TEST_EXIT=\$?"
# 期望：gofmt 输出空 + BUILD_EXIT=0 + VET_EXIT=0 + TEST_EXIT=0（8 包 ok、0 FAIL，httpserver ≈10s）

# 跨语言 hash 向量重算（纯复算，不建引擎；Wave 8 当时 11 条，Wave 9 补齐后 28 条，重算后 testdata/targets_hash_vectors.json 零 diff）
PYTHONPATH= python tests/_w8_gen_vectors.py
```

### 16.9 逐字证据片段（提交进仓库的那一份，不依赖本地日志）

`output/` 被 `.gitignore:43 *.log` 排除，日志本身不入库，所以把提交前同一棵树上的实跑关键行
逐字抄在这里供对账；审核者可用 §16.8 的命令重跑并比对。

P6 disposable PG（`output/w8/p6_run_w8c.log`，`P6_REAL_EXIT=0`）：

```
    GO_EVIDENCE phase=pre_migrate expected=2 ran=2 passed=2 failed=0 skipped=0
    GO_EVIDENCE phase=post_migrate expected=11 ran=11 passed=11 failed=0 skipped=0
    admin_jobs 最终 7 行；job 留痕 16 条
    · job#13 reindex_chunks status=failed retry=3 targets_hash=有
P6_DISPOSABLE_SUMMARY criteria=14 failed_criteria=0 failed_steps=0 go_phases=2 skipped=0
RESULT: PASS
```

Go 套件（`output/w8/go_suite_w8c.log`，容器内 `echo` 真实退出码，非管道后的 `$?`）：

```
GOFMT_DONE          ← 上一行 gofmt -l 的 9 个文件清单输出为空
BUILD_EXIT=0
VET_EXIT=0
ok  graphinsight/go-backend/internal/adminstore  0.116s
ok  graphinsight/go-backend/internal/httpserver  1.546s
TEST_EXIT=0         ← 8 包 ok、0 FAIL
```

Python 门禁（`output/w8/w8c_gates.log`，每个脚本单独 `echo EXIT_*=$?` 取真实退出码）：

```
SUMMARY total=20 failed=0
EXIT_run_unified_boundary_guards=0
✓ all M5-A acceptance checks passed
EXIT_check_m5a_revision_backfill=0
✓ all M5-B0 reindex_chunks checks passed
EXIT_check_b0_reindex_chunks=0
RESULT: PASS — Wave 3 连续场景（影子失败转交 / 复用 / 终态父子回写 / §8.5 拒写）全部证成
EXIT_check_m5_wave3_handoff=0
```

向量重算与 LF 归一（本轮最后一处改动，故单独取证）：

```
wrote 11 vectors -> E:\projects\GraphInsight\go-backend\internal\adminstore\testdata\targets_hash_vectors.json
CRLF 0 LF 153 bytes 4207
--- PASS: TestCanonicalTargetsHashMatchesPythonVectors (0.00s)
ok  graphinsight/go-backend/internal/adminstore  0.008s   TEST_EXIT=0
```

生成器原先用 Windows 默认换行写出（153 行 CRLF），而 `.gitattributes` 是 `*.json text eol=lf`，
两者不一致会让"重算后零 diff"这条判据在任何 Windows 检出上假红。本轮把生成器钉成
`newline="\n"` 并重算，工作树字节与入库 blob 同为 LF；改完在容器内复跑对等用例（上面 4 条 PASS）
确认换行归一没有动到向量内容——JSON 里的换行是 `"l\nm"` 这类转义，不受文件 EOL 影响。



---

## 17. Wave 9（2026-10-05）：跨语言 hash 变体补齐（抓出并修掉真缺陷）+ 固定提交基线密钥复核 + 远端退出码留痕

Wave 9 授权原文（本轮范围的唯一依据）：

> "Wave 8 专用端点隔离验收材料接收，通用白名单保持关闭。补齐跨语言 targets 规范化 hash 变体测试，并用固定提交基线核实密钥扫描零新增；远端查询记录退出码。通过后允许推送审查分支、开草稿 PR，不合并、不部署、不执行共享库 --confirm，不进入 S2。"

### 17.1 W9-1 变体补齐：11 条 → 28 条，并因此抓出 W9-F1（Go 星外平面转义错算）

生成器 `backend/tests/_w8_gen_vectors.py` 的用例集从 11 条扩到 28 条。新增 17 条，每条钉一个此前无人证明的边界：

| 用例名 | 钉住的点 |
| --- | --- |
| `empty_list` | 空 targets 的规范化文本必须是字面 `[]`（不是 `null`/空串） |
| `numeric_string_order` | 排序按字符串字节序，`"1" < "10" < "2"`，不是数值序 |
| `case_and_prefix_order` | 大写字母排在小写之前；前缀短的排在前面（`A` < `B` < `a` < `ab`） |
| `cjk_vs_latin_order` | CJK 与拉丁混排的次序（UTF-8 字节序 == 码点序这条依据的真实回归） |
| `ascii_boundary_tilde_del` | `0x20`、`0x7E`、`0x7F` 三条 ASCII 边界的转义与次序 |
| `nul_and_low_controls` | `NUL`/`\x01`/`\x1f` 低控制位必须 `\u00XX` 转义 |
| `short_escapes` | Python 的短转义 `\b \f \t \r`（不是 `\u0008` 等等价长式） |
| `star_plane_emoji` | U+1F600 / U+2000B 的**代理对**转义（本轮抓出缺陷的用例） |
| `nfc_vs_nfd_acutes` | NFC `é`（U+00E9）与 NFD `e`+U+0301 视为不同 chunk_id，不做归一 |
| `hebrew_rtl` | RTL 文字按码点排序，不受显示方向影响 |
| `slash_lt_gt_amp` | `/ < > &` 必须原样输出（Go `encoding/json` 会 HTML 转义，故此处是对"手写编码器"的钉子） |
| `quote_backslash_mix` | `"` 与 `\` 的转义组合与重复反斜杠 |
| `line_separators_2028` | U+2028/U+2029 走 `\uXXXX`（Go 默认原样输出，是真实分歧点） |
| `negative_revision` | 负 `target_revision` 参与排序且不带 `+` |
| `i64_max_revision` | `9223372036854775807` 不被转科学计数法/不丢精度 |
| `same_chunk_rev_tie` | 同 chunk_id 多 revision 且含重复项时的稳定次序 |
| `fifty_targets_unpadded` | 50 个目标（逆序喂入）——规模化验证排序收敛 |

`testdata/targets_hash_vectors.json` 重算后的 `git diff --numstat` 是 **`474 insertions / 0 deletions`**：Wave 8 已交付的 11 条 hash 与 canonical 文本一个字节都没变，扩的是覆盖面，不是改契约。

新门禁 `backend/tests/check_m5_targets_hash_vectors.py`（已注册进统一门禁，case 名 `m5_targets_hash_vectors`）做的不是"跑一遍 Python"，而是**独立复算**：逐条断言 `hash == canonical_targets_hash(input)` 且 `sha256(canonical) == hash`（生成器里也有同一条自证，两处任一算错都会红），外加命名覆盖、乱序收敛、空列表字面量，以及"文件里除 `\n` 外不得出现裸 C0 字节"这条换行/编码钉子。

W9-F1（真实缺陷，已修）：`go-backend/internal/adminstore/targets_hash.go:98` 的高代理位写成了 `0xD7C0+(value>>10)`，正确是 `0xD800+(value>>10)`。

- 现象：`star_plane_emoji` 用例里 Go 产出 `\ud7fd\ude00`，Python 产出 `\ud83d\ude00`（U+1F600）。
- 直接后果：任何 `chunk_id` 含 U+10000 以上字符（emoji、CJK 扩展 B 区等），Go 与 Python 会算出**不同的 `targets_hash`**。§16.3 的去重是按 hash 精确匹配复用的，hash 分叉意味着同一目标在两语言写入路径下被当成不同目标——重复建行、复用失效，而不是报错，属于静默语义分裂。
- 修复范围仅此一处常数；低代理位 `0xDC00+(value&0x3FF)` 原先即正确。

红绿取证（本轮实跑，`output/w9/red_green_w9.log`）：把常数临时改回 `0xD7C0` 后 `go test -count=1` **必须红**，还原后**必须绿**，且脚本内断言还原后文件字节与修改前完全一致。

```
[RED] go test -count=1 退出码 = 1
    targets_hash_test.go:44: star_plane_emoji 规范化文本不一致
         Go: [{"chunk_id":"\ud7fd\ude00","target_revision":1},...]
         Py: [{"chunk_id":"\ud83d\ude00","target_revision":1},...]
FAIL	graphinsight/go-backend/internal/adminstore	0.225s
[GREEN] 还原后 go test -count=1 -v 退出码 = 0
--- PASS: TestCanonicalTargetsHashMatchesPythonVectors (0.00s)
RESTORE_IDENTICAL=True
```

### 17.2 W9-2 密钥扫描：改成固定提交基线，零新增成立且口径收紧

Wave 8 的做法（把工作树与"当前 HEAD"对比）在本轮不再可用：Wave 9 自己会产生提交，HEAD 一动，"零新增"就变成同义反复。因此本轮把基线钉在**Wave 7 的提交** `97645ac10658867b268216a042290b732cfdb641`（Wave 8 两个提交之后的树才是工作树，比较对象是一个不动的 SHA）。

工具：`backend/tests/_w9_secret_baseline.py`（**这是取证脚本，不是门禁**——它依赖 `git diff <SHA>` 这一时间相关输入，不适合进 `run_unified_boundary_guards.py`）。对每个文件分别物化"基线字节"（`git show SHA:path`）与"工作树字节"到两个临时目录，保留相对路径，各自跑一次 `check_artifacts_secrets` 扫描器，然后按 `(kind, match_sha256, value)` **计数**比较。

三处口径值得单列，因为它们决定了这个结论能不能信：

1. findings 用 `Counter` 计数而非集合——同一明文出现两次，基线 2 / 工作树 2 才算持平；若用集合去重，"重复出现"会被洗成"零新增"。
2. 扫描器退出码只接受 `0`（干净）或 `1`（有命中）；`2`（前置缺失，例如路径不存在）一律判失败，不允许把"没扫"读成"扫了且干净"。
3. 断言"扫描器自报 `findings=N` 等于逐行解析出的条数"，并在出现 `SECRET_FINDINGS_TRUNCATED`（40 行截断）时直接中止——否则"零新增"可能只是被截断后的假绿。

结果（`output/w9/secret_baseline_w9.log`，EXIT=0）：

```
BASELINE_SHA=97645ac10658867b268216a042290b732cfdb641
SCAN_SCOPE files=18
SECRET_BASELINE_SUMMARY files=18 new_findings=0 failed=0
```

18 个文件 = 与固定基线的 diff 文件集 ∪ 未跟踪文件（限 `backend/ go-backend/ frontend/ docs/ scripts/`）。两侧都命中且数量相同的两处**既有**命中照旧如实登记，不被本轮措辞掩盖：

- `frontend/src/types/admin.ts`：基线 11 / 工作树 11，两侧扫描器 EXIT 均为 1
- `go-backend/internal/httpserver/admin_control_plane_routes_test.go`：基线 23 / 工作树 23，两侧 EXIT 均为 1

维持不变的范围口径（不夸口）：CI 门禁扫描的作用域仍是 `artifacts/playwright-report/test-results/logs/dev`，`frontend/src`、`go-backend`、`docs` **不在 CI 作用域内**，属既有的"待复核项"。本轮结论是"**相对固定提交基线零新增**"，不是"全仓库扫描通过"。

### 17.3 W9-3 远端查询逐条退出码 + HTTPS 失败根因 + SSH:443 权威事实

全部留痕在 `output/w9/remote_probes_w9.log`，每条命令单独记录真实 `EXIT`（不经管道，避免 `pipefail`/`$?` 误读）。

本地侧：

```
$ git rev-parse --abbrev-ref HEAD        → m5/dual-write                          EXIT=0
$ git rev-parse --abbrev-ref @{u}        → fatal: no upstream configured …        EXIT=128
$ git remote -v                          → origin https://github.com/…(fetch/push) EXIT=0
```

HTTPS 远端侧（四条查询 + 三次重试，全部失败）：

```
$ git ls-remote origin refs/heads/m5/dual-write   EXIT=128  Failed to connect to github.com:443 after 21085 ms
$ git ls-remote origin refs/heads/main            EXIT=128  Failed to connect to github.com:443 after 21081 ms
$ git ls-remote --heads origin                    EXIT=128  Failed to connect to github.com:443 after 21126 ms
TRY1 EXIT=128 Recv failure: Connection was reset
TRY2 EXIT=128 Failed to connect to github.com:443 after 21127 ms
TRY3 EXIT=128 Failed to connect to github.com:443 after 21066 ms
```

根因不是"重试次数不够"，逐 IP 探测给出的是一眼能判的结论：

```
TCP github.com:443        fail EXIT=1 :: TimeoutError
TCP 20.205.243.166:443    fail EXIT=1 :: TimeoutError     ← github.com 当前解析到的地址，黑洞
TCP 140.82.116.4:443      ok  EXIT=0                        ← GitHub 真实 IP，通
TCP 140.82.112.4:443      ok  EXIT=0
TCP ssh.github.com:443    ok  EXIT=0
```

即本机 DNS 把 `github.com` 解析到 `20.205.243.166`，该 IP 的 443 只丢包。**处置**：不改 hosts、不关证书校验、不做静默重试；走 GitHub 官方 `ssh.github.com:443` 通道，并在本轮只读验证身份与远端事实：

```
$ ssh -p 443 -o BatchMode=yes -T git@ssh.github.com
OUTPUT=[Hi E8A281E6ACA2! You've successfully authenticated, but GitHub does not provide shell access.]
EXIT=1        ← GitHub 对 SSH 认证探测的约定返回码，非失败
```

权威远端事实（`git ls-remote ssh://git@ssh.github.com:443/E8A281E6ACA2/GraphInsight.git`，`LS_REMOTE_SSH_EXIT=0`）：

```
59332f42503443872d382dbce0710c7226175abb	HEAD
a201e723325b7c29d6269cbc6b8aa4f0e6c25340	refs/heads/audit/m5-gate0-coverage
59332f42503443872d382dbce0710c7226175abb	refs/heads/main
a201e723325b7c29d6269cbc6b8aa4f0e6c25340	refs/pull/1/head
```

据此确定的三件事（本地缓存态一律让位于此）：

1. 远端 `main` = `59332f4`，且它正是本地 `m5/dual-write` 的 merge-base；`m5/dual-write` 相对远端 `main` 领先 **33 个提交**，即审查分支的 PR 范围就是这 33 个提交。
2. **远端不存在 `refs/heads/m5/dual-write`**——审查分支此前从未推送过。
3. 本地 `main`（`13303dc`，含 26 个不在本分支上的 enterprise 文档提交）与远端 `main` 已经不同线；本轮不碰它，也不把它的内容算进 PR 范围。此前 `git branch -vv` 显示 `origin/main: ahead 26` 是本地 tracking ref 的过期缓存态，不作为判据。

### 17.4 Wave 9 总回归（本轮真实数字，全绿；隔离证据与真实取证分栏不变）

| 层 | 命令/日志 | 结果 |
| --- | --- | --- |
| Go 格式 | 容器内 `gofmt -l` | `GOFMT_DONE` 前清单为空 |
| Go 构建/静态 | `go build ./...` / `go vet ./...` | `BUILD_EXIT=0` / `VET_EXIT=0` |
| Go 套件 | `go test ./...`（`output/w9/go_suite_w9.log`） | `TEST_EXIT=0`，8 包 ok、0 FAIL |
| hash 对等 | `TestCanonicalTargetsHashMatchesPythonVectors` | 28/28 一致（修复前红，见 §17.1） |
| 新门禁 | `check_m5_targets_hash_vectors.py` | `SUMMARY total=28 failed=0`，EXIT=0 |
| 统一门禁 | `run_unified_boundary_guards.py`（`output/w9/unified_guards_w9.log`） | `SUMMARY total=21 failed=0`（Wave 8 为 20，本轮 +1） |
| 扫描器自检 | `check_artifact_secrets_selftest.py` | `assertions=53 failed=0 result=pass` |
| 一次性 PG 真实写入 | `output/w9/p6_disposable_pg_w9.log` | 判据 1–14 全 PASS，`GO_EVIDENCE phase=post_migrate expected=11 ran=11 passed=11 failed=0 skipped=0`，`RESULT: PASS`，`P6_EXIT=0` |

一次性 PG 仍是**隔离证据**：独立临时实例、非共享开发库；共享库的 `--confirm` 迁移本轮依旧未执行（授权明确禁止）。

### 17.5 本轮维持的边界与授权范围

- Wave 8 交付的**专用端点隔离验收**已被接收；`reindex_chunks` 仍**不在**通用作业创建白名单 `supportedJobTypes` 内，入口防御仍是路由 404（store 白名单 400 只是第二道）。白名单放行需要单独申请，本轮未申请。
- Go 侧无 409 主张（`JOB_409` 仍 NOT-IMPLEMENTED）；`requireJobKnowledgeBase` 的 409 是 KB_ARCHIVED/KB_INVALID_STATE 的另一主题，不冲突。
- 不合并、不部署、不执行共享库 `--confirm`、不进入 S2——本轮只做到"提交 + 推送审查分支 + 草稿 PR"。
- `stash@{0}`/`stash@{1}`（GI-11）仍未触碰，原样保留。

### 17.6 复核命令（Wave 9 口径）

```bash
# 以下脚本均自强制 UTF-8（sys.stdout.reconfigure），不需要外挂 -X utf8；退出码就是判据
cd backend

# 1) 重算向量（幂等，重跑后 testdata 零 diff；生成器自带 hash 自证）
python tests/_w8_gen_vectors.py

# 2) 新门禁：独立复算 + 变体覆盖（期望 SUMMARY total=28 failed=0，EXIT=0）
python tests/check_m5_targets_hash_vectors.py; echo EXIT=$?

# 3) Go 字节级对等（期望 28 条全过；星外平面用例即 W9-F1 的钉子）
cd ../go-backend && go test ./internal/adminstore/ -run TestCanonicalTargetsHashMatchesPythonVectors -count=1 -v

# 4) 统一门禁（期望 SUMMARY total=21 failed=0）
cd ../backend && python tests/run_unified_boundary_guards.py; echo EXIT=$?

# 5) 固定提交基线密钥复核（基线是位置参数、SHA 不随提交移动；期望 new_findings=0 failed=0）
python tests/_w9_secret_baseline.py 97645ac10658867b268216a042290b732cfdb641; echo EXIT=$?

# 6) 一次性 PG 真实写入判据（隔离实例，非共享库）
python tests/check_m5_p6_disposable_pg.py; echo EXIT=$?

# 7) 远端权威事实（HTTPS 被 DNS 黑洞时用官方 SSH:443；不要用本地 tracking ref 下结论）
git ls-remote ssh://git@ssh.github.com:443/E8A281E6ACA2/GraphInsight.git refs/heads/main
```

### 17.7 逐字证据片段（提交进仓库，不依赖本地日志）

```
wrote 28 vectors -> E:\projects\GraphInsight\go-backend\internal\adminstore\testdata\targets_hash_vectors.json
474	0	go-backend/internal/adminstore/testdata/targets_hash_vectors.json        ← git diff --numstat
[OK] m5_targets_hash_vectors duration=0.4s
SUMMARY total=28 failed=0        ← check_m5_targets_hash_vectors.py
SUMMARY total=21 failed=0        ← run_unified_boundary_guards.py（Wave 8=20，+1）
SECRET_SCAN_SELFTEST_SUMMARY assertions=53 failed=0 result=pass
GOFMT_DONE / BUILD_EXIT=0 / VET_EXIT=0 / TEST_EXIT=0（8 包 ok、0 FAIL）
GO_EVIDENCE phase=post_migrate expected=11 ran=11 passed=11 failed=0 skipped=0
RESULT: PASS / P6_EXIT=0
BASELINE_SHA=97645ac10658867b268216a042290b732cfdb641
SCAN_SCOPE files=18
SECRET_BASELINE_SUMMARY files=18 new_findings=0 failed=0
LS_REMOTE_SSH_EXIT=0 / refs/heads/main=59332f42503443872d382dbce0710c7226175abb（远端无 refs/heads/m5/dual-write）
```

### 17.8 本轮提交 stage 清单与假脏复核

Wave 9 实际改动的 7 个文件（提交只 stage 这些）：

1. `backend/tests/_w8_gen_vectors.py`（11 → 28 用例 + 生成器自证）
2. `backend/tests/check_m5_targets_hash_vectors.py`（新门禁）
3. `backend/tests/run_unified_boundary_guards.py`（注册 `m5_targets_hash_vectors`）
4. `backend/tests/_w9_secret_baseline.py`（固定基线密钥复核工具，非门禁）
5. `go-backend/internal/adminstore/targets_hash.go`（W9-F1 高代理位修正）
6. `go-backend/internal/adminstore/testdata/targets_hash_vectors.json`（重算，474/0）
7. `docs/ENTERPRISE_M5_WAVE4_AUDIT_PACKAGE_2026-10-05.md`（§16.8 锚点措辞 + 本节）

Windows stat-cache 假脏复跑（`git hash-object` 对 `git rev-parse HEAD:<path>`）：`git status` 报 M 的 13 个文件里，8 个 `go-backend/internal/adminstore/*.go`（`client.go`、`configs.go`、`logs.go`、`monitor.go`、`monitor_test.go`、`rbac_bindings.go`、`rbac_seed.go`、`users.go`）与工作树字节完全一致，判 SAME，未 stage；剩下 5 个 DIFF 文件（本节的 1/3/5/6/7）加 2 个新增文件（本节的 2/4）才是真实改动，7 个一起 stage。
