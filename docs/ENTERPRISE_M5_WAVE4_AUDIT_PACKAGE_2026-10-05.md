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
| W4-F2 | Go 建任务白名单 `supportedJobTypes`（`go-backend/internal/adminstore/jobs.go:26-30`）= `build_graph/clear_kb/reindex`，`validateJobCreateRequest`（`jobs.go:647-650`）在 `BeginTx`/`INSERT`（`jobs.go:125-130`）**之前**返回 `ErrJobValidation`，由 `admin_jobs_native.go:758-761` 映射为 HTTP 400 `INVALID_BODY` | 前端/运维无法通过 Go 控制面新建 reindex_chunks；但拒绝路径不写库，fail-closed 成立 |
| W4-F3 | 读侧不校验类型白名单：`buildJobWhere`（`jobs.go:570`）只在 `jobs.go:574-576` 对 `job_type` 做**等值过滤**（值任意，不在 `supportedJobTypes` 也照样进 WHERE）；`ListJobs`（`jobs.go:325`）、`GetJob`（`jobs.go:386`）、`RetryJob`（`jobs.go:181`）、`CancelJob`（`jobs.go:258`）均无 job_type 闸门 | 已存在的 reindex_chunks 行**能被列出、按类型筛出、查看、重试、取消**——写侧关门、读侧开门，语义不对称；`?job_type=reindex_chunks` 在 Go 层是合法读过滤 |
| W4-F4 | `targets_hash` 对上层不可见：Go `JobItem` 结构体（`jobs.go:32-50`）无该字段，`grep -rn "targets_hash" go-backend --include=*.go` = 0 命中；前端同样 0 命中（`grep -rn targets_hash frontend/src` 空） | §16.3 去重结果（reused/retried/rejected）在任务中心**无法核对**，审计只能读 DB |
| W4-F5 | 前端类型与筛选项缺项：`frontend/src/types/admin.ts:732` `JobType = 'build_graph' | 'clear_kb' | 'reindex'`（无 `reindex_chunks`），`frontend/src/pages/Admin/JobsPage.tsx:48-52` `jobTypeOptions` 同样缺项；表格直出原始值（`JobsPage.tsx:472` `{item.job_type}`），URL 参数按选项校验（`JobsPage.tsx:144-146`） | 行能显示（裸字符串），但类型层不认、筛选下拉选不到、`?job_type=reindex_chunks` 深链被忽略 |
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
| Go 控制面 reindex_chunks 建任务入口 | **NOT-IMPLEMENTED**（白名单拒写 → 400 `INVALID_BODY`） |
| HTTP 409 / `JOB_409` 作业状态映射 | **NOT-IMPLEMENTED**（Go、Python 两侧均不存在；本轮实改文档旧误述） |
| `targets_hash` 在 Go DTO / 前端类型 / 任务中心 UI 的暴露 | **NOT-IMPLEMENTED** |
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
