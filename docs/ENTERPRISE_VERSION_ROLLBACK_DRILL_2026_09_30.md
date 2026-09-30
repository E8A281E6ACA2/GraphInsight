# 版本级回滚演练记录（2026-09-30，Release Gate R1 项3）

## §1 目标与口径

开工令 Release Gate R1 项3 要求：在测试环境做一次真实的代码版本级回滚演练，部署当前版本后回滚到
pre-M4-R1 基线，回滚前后用同一套探针复跑，并断言三条安全不变量。

演练全程固定以下口径，未做任何调整：**探针集合与判定条件来自
`backend/tests/run_rollback_drill.py`，不因被测版本行为改变期望值；不 force push；不碰生产。**

1. 夹具只在当前发布版本上用真实 HTTP API 建立一次（第二个知识库、仅绑 `viewer@kb-b` 的低权限用户），
   不使用人工 SQL，凭据写在仓库外的临时目录。
2. 全程只使用本地隔离栈（独立 Postgres 库 + 一次性网关容器），常驻栈只做对照与恢复复探。
3. 每个被测版本额外跑一腿"软授权配置"（`RBAC_AUTHZ_MODE=local_jwt_soft` +
   `RBAC_ENFORCE_BUSINESS_API=false`），用于检验守卫是否只在 enforce 模式下生效。

TLDR：

- **`a053532`（pre-M4-R1 基线）不可作为回滚目标**：enforce 模式 9/15 探针失败，soft 模式 10/15 失败，
  三条安全不变量在该版本上根本不存在（源码级证据见 §4）。
- **代码级可回滚下限 = `78b1f28`**：该版本及之后（含当前 `4f4f205`）在 enforce 与 soft 两种模式下
  均 15/15 全绿；而 `45db134`（首次引入作用域强制）在 soft 模式下会被伪造身份头拿到 200 响应，
  即 `78b1f28` 关闭的旁路可被黑盒探针判定（§5）。
- 数据库侧无需 down-migration，代码回滚与库表回滚解耦（§6）。
- 演练结束后恢复动作已核验，常驻栈复探 15/15 全绿（§7）。

## §2 环境、变量控制与二进制身份链

| 项 | 值 |
|---|---|
| 宿主 | Windows 10.0.26200 + Git Bash + Docker Desktop |
| 隔离元数据库 | 容器 `gi-freshpg-e2e`，宿主 5436，库 `graphinsight_admin` |
| Python 能力层 | 宿主 `127.0.0.1:8001`（网关经 `http://host.docker.internal:8001` 调用） |
| Neo4j | `bolt://host.docker.internal:7687` |
| 被测网关 | 每腿一次性容器 `gi-drill-gw-<sha>-<mode>`，镜像 `cache.qs.al/library/alpine:3.21`，宿主端口 18091-18098 |
| 常驻对照网关 | 容器 `gi-freshgw-e2e`，`127.0.0.1:18090 -> 8081`，Cmd=`/src/bin/api-linux-66e1d18`，全程未重启 |

单变量控制：所有腿复用同一份 env 文件（由常驻容器 `docker inspect` 导出，含
`ADMIN_DATABASE_URL`、`ADMIN_SECRET_KEY`、`PYTHON_BACKEND_BASE_URL`、`NEO4J_*` 等；值不落文档），
每腿唯一变量是网关二进制；soft 腿只额外覆盖 `RBAC_AUTHZ_MODE` 与 `RBAC_ENFORCE_BUSINESS_API` 两项。

二进制身份链（消除"跑的不是那个版本"这类伪证）：

1. `git archive <sha> go-backend | tar -x -C /e/tmp/gi-drill-build/<sha>` 取提交态纯净树，规避
   Windows `core.autocrlf=true` 的 CRLF 伪报。
2. 容器内交叉编译：`GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o bin/api-drill-<sha> ./cmd/api`
   （`golang:1.24.13-bookworm`，依赖走 `gomodulecache` 卷）。
3. 宿主记录 `sha256sum`；容器启动后用 `docker top` 校验实际进程就是 `/src/bin/api-drill-<sha>`
   （每腿输出见 §7）。

| 版本标签 | 完整 SHA | 二进制 sha256（前 16） | go-backend `.go` 文件数 | 说明 |
|---|---|---|---|---|
| `4f4f205` | `4f4f2054c3345b51450a4b6c07746bb18a66d4ca` | `e0d5fd13cd107b9a` | 78 | 当前 HEAD |
| `78b1f28` | `78b1f284ea2898dd91f2fc802fa876d60c913d6f` | `e0d5fd13cd107b9a` | 78 | 关闭剩余 KB 作用域旁路 |
| `45db134` | `45db1340e7e85b22024b774ea1be4fe7b6154cfa` | `cbbfb8763b4b6640` | 77 | 首次引入作用域强制 |
| `a053532` | `a053532d0855315752c8e86bf0bec0a843c7645a` | `e51cd6f996367999` | 61 | pre-M4-R1 基线（回滚目标） |

`4f4f205` 与 `78b1f28` 二进制 sha256 相同是预期结果：`git diff 78b1f28..4f4f205 -- go-backend` 为空
（两者只差测试脚本），Go 构建可复现。

## §3 enforce 模式探针矩阵（`RBAC_AUTHZ_MODE=go_db`）

15 条探针 = 主链路 6 条（健康/登录/KB 目录/上传/文档注册表/问答）+ 不变量1 无默认 KB 兜底 4 条
+ 不变量2 无缺 kb_id 透传 1 条 + 不变量3 无跨 KB 泄漏 3 条 + 伪造入站身份头 1 条。

| 探针 | 4f4f205 | 78b1f28 | 45db134 | a053532 |
|---|---|---|---|---|
| `main_health` | PASS | PASS | PASS | PASS |
| `main_login` | PASS | PASS | PASS | PASS |
| `main_kb_directory` | PASS | PASS | PASS | FAIL |
| `main_upload` | PASS | PASS | PASS | PASS |
| `main_document_registry` | PASS | PASS | PASS | PASS |
| `main_docqa` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_docqa` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_nl2cypher` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_graph_build` | PASS | PASS | PASS | FAIL |
| `no_kb_passthrough_cross_scope` | PASS | PASS | PASS | FAIL |
| `no_default_kb_documents_list` | PASS | PASS | PASS | FAIL |
| `cross_kb_leak_low_login` | PASS | PASS | PASS | PASS |
| `cross_kb_leak_directory` | PASS | PASS | PASS | FAIL |
| `cross_kb_leak_docqa_denied` | PASS | PASS | PASS | FAIL |
| `forged_identity_header_rejected` | PASS | PASS | PASS | PASS |
| **合计（passed/failed）** | **15/0** | **15/0** | **15/0** | **6/9** |

## §4 回滚腿（`a053532`）失败明细与源码根因

| 探针 | 实测 | 根因（代码证据） |
|---|---|---|
| `main_kb_directory` | `404 NOT_FOUND` | `a053532` 的 Go 网关未注册 `/api/knowledge-bases`（该树 `internal/httpserver/*.go` 中 `knowledge-bases` 命中 0 条）。当前边界下 Python public 业务面已下线，回滚网关等于砍掉 KB 目录链路 |
| `main_docqa` | `400`（无结构化 error_code） | 老网关请求契约与当前 Python 能力层不一致，问答主链路直接失败 |
| `no_default_kb_api_docqa` / `no_default_kb_api_nl2cypher` | `400`，非 `KB_SCOPE_REQUIRED` | 拒绝来自老的参数校验，不是作用域授权判定；口径要求结构化 `KB_SCOPE_REQUIRED` |
| `no_default_kb_api_graph_build` | `200` | 缺 `kb_id` 仍受理建图任务 = 默认 KB 兜底，正是 M4-R1 要关闭的口子 |
| `no_kb_passthrough_cross_scope` | `200` | header 与 body 作用域冲突时直接透传下游 |
| `no_default_kb_documents_list` | `200` | 无作用域的文档列表返回数据（跨库可读） |
| `cross_kb_leak_directory` | `404 NOT_FOUND` | 同 `main_kb_directory`，低权限可见性断言无法执行 |
| `cross_kb_leak_docqa_denied` | `400`，非 `403 KB_ACCESS_DENIED` | 被拒原因是校验而非授权判定，不能计为"跨库拦截生效" |
| `forged_identity_header_rejected`（仅 soft 腿） | `200` | 未认证 + 伪造 `x-auth-user-name` 即读到他库内容 |

汇总根因（一句话可复查）：`a053532` 整棵 `go-backend` 中
`KB_SCOPE_REQUIRED|KB_CROSS_SCOPE|KB_ACCESS_DENIED` 命中 **0** 条，`45db134` / `78b1f28` 各命中
**34** 条；`git diff a053532..78b1f28 -- go-backend` 涉及 40 个文件。
即安全不变量在回滚基线上不存在，探针失败是真实回归而非环境抖动。

## §5 soft 模式探针矩阵与回滚下限

soft 腿配置：`RBAC_AUTHZ_MODE=local_jwt_soft`、`RBAC_ENFORCE_BUSINESS_API=false`。

| 探针 | 4f4f205 | 78b1f28 | 45db134 | a053532 |
|---|---|---|---|---|
| `main_health` | PASS | PASS | PASS | PASS |
| `main_login` | PASS | PASS | PASS | PASS |
| `main_kb_directory` | PASS | PASS | PASS | FAIL |
| `main_upload` | PASS | PASS | PASS | PASS |
| `main_document_registry` | PASS | PASS | PASS | PASS |
| `main_docqa` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_docqa` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_nl2cypher` | PASS | PASS | PASS | FAIL |
| `no_default_kb_api_graph_build` | PASS | PASS | PASS | FAIL |
| `no_kb_passthrough_cross_scope` | PASS | PASS | PASS | FAIL |
| `no_default_kb_documents_list` | PASS | PASS | PASS | FAIL |
| `cross_kb_leak_low_login` | PASS | PASS | PASS | PASS |
| `cross_kb_leak_directory` | PASS | PASS | PASS | FAIL |
| `cross_kb_leak_docqa_denied` | PASS | PASS | PASS | FAIL |
| `forged_identity_header_rejected` | PASS | PASS | **FAIL** | **FAIL** |
| **合计（passed/failed）** | **15/0** | **15/0** | **14/1** | **5/10** |

关键判据（原始输出）：

```text
45db134-soft  PROBE id=forged_identity_header_rejected verdict=FAIL status=200 error_code=HTTP_200
78b1f28-soft  PROBE id=forged_identity_header_rejected verdict=PASS status=401 error_code=UNAUTHORIZED
4f4f205-soft  PROBE id=forged_identity_header_rejected verdict=PASS status=401 error_code=UNAUTHORIZED
```

`git diff 45db134..78b1f28 -- go-backend` 的产码改动集中在两个文件：`authz_middleware.go`（30 行）
与 `qa_scope.go`（46 行），把"未认证主体 → 沿用第一阶段 soft 放行""授权服务未接入 → soft 放行"
两处降级路径改成无条件 fail-closed，并新增 `clearPropagatedAuthContext()` 清理入站伪造身份头。
这正是 soft 腿唯一失败的探针，因此：

1. 探针套件对 `78b1f28` 类旁路**是可判定的，但前提是该腿跑在 soft 模式**；只看 enforce 腿会得出
   "`45db134` 也安全"的错误下限（本轮先只跑 enforce 时确实出现过这个误判，补 soft 腿后纠正）。
2. 回滚下限据此判定为 **`78b1f28`**，不是 `45db134`。
3. 发布门禁口径：任何回滚演练腿必须同时覆盖 enforce 与 soft 两种配置，缺 soft 即视为未演练。

## §6 数据库与迁移状态

| 项 | 演练前 | 各腿 | 恢复后 |
|---|---|---|---|
| `public` 表数 | 11 | 11（未变） | 11 |
| 表清单 | `admin_configs`、`admin_jobs`、`admin_logs`、`admin_permissions`、`admin_qa_traces`、`admin_role_permissions`、`admin_roles`、`admin_user_role_bindings`、`admin_users`、`knowledge_base_documents`、`knowledge_bases` | 同左 | 同左 |
| 迁移工具 | 未使用 alembic（`alembic_version` 不存在），采用 `init_db()` + 各 `migrate_*.py` | 无任何腿执行 DDL | 同左 |
| 网关 ERROR 日志 | - | 八条腿全部 0 行 | 0 行 |

结论：同一份 schema 可直接被 `a053532` 与 `4f4f205` 两个二进制加载运行，M4-R1 这一段改造
**不需要 down-migration，代码回滚与库表回滚解耦**。库表与配置级回滚链路由
`backend/tests/run_migration_rollback_smoke.py` 与 `backend/tests/run_config_rollback_drill.py` 覆盖。

## §7 结果汇总、日志与恢复动作

```text
enforce (go_db)                       soft (local_jwt_soft + enforce=false)
ROLLBACK_DRILL_SUMMARY version=4f4f205 result=pass passed=15 failed=0    version=4f4f205 result=pass passed=15 failed=0
ROLLBACK_DRILL_SUMMARY version=78b1f28 result=pass passed=15 failed=0    version=78b1f28 result=pass passed=15 failed=0
ROLLBACK_DRILL_SUMMARY version=45db134 result=pass passed=15 failed=0    version=45db134 result=fail passed=14 failed=1
ROLLBACK_DRILL_SUMMARY version=a053532 result=fail passed=6  failed=9    version=a053532 result=fail passed=5  failed=10
ROLLBACK_DRILL_SUMMARY version=restored-live result=pass passed=15 failed=0
```

每腿都记录了 `SRC_SHA`（`git rev-parse <label>`）、`BIN_SHA256`（宿主二进制摘要）与
`RUNNING_PROC`（`docker top` 实际进程路径），三者一一对应，见：

可重复性抽样：`45db134` 的 enforce 腿在两个不同端口上共执行 3 次（含一次脚本参数写错的重复），
三次结果均为 `result=pass passed=15 failed=0`（`gi_drill2_45db134.log` 与
`gi_drill2_45db134_enforce.log`），说明探针结论不依赖偶发时序。

```text
%TEMP%\gi_drill2_<label>.log            enforce 腿探针原始输出
%TEMP%\gi_drill2_<label>_soft.log       soft 腿探针原始输出
%TEMP%\gi_drill2_<label>_gwerr.log      网关 ERROR 采样（本轮全部 0 字节）
%TEMP%\gi_drill2_restored_live.log      恢复态复探
%TEMP%\gi_drill2_env.txt                复用 env（含密钥，勿外传、勿入库）
%TEMP%\gi_drill_state.json              夹具（kb_a/kb_b/低权限账号口令，勿外传、勿入库）
```

恢复动作与核验：

1. 八条腿的一次性容器由腿执行器在结束时 `docker rm -f` 回收；复核
   `docker ps -a --filter name=gi-drill-gw --format '{{.Names}}' | wc -l` = **0**。
2. 常驻网关 `gi-freshgw-e2e` 全程未重启（`RestartCount=0`，`StartedAt=2026-09-30T02:13:23Z`），
   Cmd 仍是 `/src/bin/api-linux-66e1d18`，未被演练污染。
3. 恢复态对 18090 复探 15/15 全绿（`gi_drill2_restored_live.log`）。
4. 演练未修改仓库数据文件，也未改 `a053532` / `78b1f28` 的临时 worktree。

## §8 复现步骤

```bash
# 1. 取提交态源树并交叉编译（每个版本一次）
mkdir -p /e/tmp/gi-drill-build/<sha>
git archive <sha> go-backend | tar -x -C /e/tmp/gi-drill-build/<sha>
MSYS_NO_PATHCONV=1 docker run --rm \
  -v E:/tmp/gi-drill-build/<sha>/go-backend:/src -v gomodulecache:/go/pkg/mod -w /src \
  -e GOOS=linux -e GOARCH=amd64 -e CGO_ENABLED=0 \
  golang:1.24.13-bookworm sh -c 'go build -o /src/bin/api-drill-<sha> ./cmd/api'

# 2. 起一次性网关（env 与常驻容器一致；soft 腿再加 -e RBAC_AUTHZ_MODE=local_jwt_soft
#    -e RBAC_ENFORCE_BUSINESS_API=false），核对 docker top 的进程路径
docker run -d --name gi-drill-gw-<sha>-<mode> --env-file <env_file> \
  -v E:/tmp/gi-drill-build/<sha>/go-backend:/src -p 127.0.0.1:<port>:8081 \
  cache.qs.al/library/alpine:3.21 /src/bin/api-drill-<sha>

# 3. setup 只在当前版本跑一次；probe 对每个版本 × 每种授权模式各跑一次
python -X utf8 backend/tests/run_rollback_drill.py --base-url http://127.0.0.1:<port> \
  --phase probe --version-label <sha> --state-file <state_file> --admin-password-file <pw_file>

# 4. 回收
docker rm -f gi-drill-gw-<sha>-<mode>
```

## §9 状态

- [x] 当前版本主链路 + 三条不变量全绿，二进制身份可追（`4f4f205`，enforce / soft 双模式）
- [x] 回滚到 pre-M4-R1 基线 `a053532` 实跑并定位失败根因（enforce 9 条 + soft 10 条失败，源码证据齐全）
- [x] 回滚下限判定：代码级下限 = `78b1f28`，`a053532` 与 `45db134` 均不可作为回滚目标
- [x] 数据库无需 down-migration（§6）
- [x] 恢复动作完成并复探全绿（§7）
- [ ] 把 soft 模式腿固化进 CI（当前 CI 发布验收只跑 enforce），避免演练退化回单模式
- [ ] 按 §5/§7 口径把回滚下限写入 `docs/ENTERPRISE_OPERATIONS_RUNBOOK.md` 回滚章节
