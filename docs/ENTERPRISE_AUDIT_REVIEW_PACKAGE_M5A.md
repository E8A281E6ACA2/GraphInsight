# 审计复核内容包：M5-A 四项阻断整改 + 任务 #55 + 任务 #31

编制时间：2026-10-02。交付对象：审计人员。
本包只汇总**可独立复核**的内容：每一条主张都给代码位置（`file:line`）、复跑命令、实测输出摘录，
以及"这条证据覆盖什么 / 不覆盖什么"。凡无法给出复跑命令的主张，一律写在 §7 未验证清单里，
不放在"已验证"章节。

**统一口径（先读这一条，否则计数会对不上）**

- 断言数一律按 `grep -c "^  ✓"` 计（仅统计以两空格 + `✓` 开头的步骤行），不统计汇总行与子进程回显。
  历史上报告里出现过的 `111 项 / 34 项` 是 `grep -c "✓"` 的宽口径，已废弃。
- 环境：Windows 本机无 `backend/.venv`，用系统 Python 3.14 + `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`；
  CI 为 Ubuntu runner + `backend/.venv/bin/python`。两侧断言数已实测一致。
- 本包不宣布 M5-A 验收通过；M5-B Go API 保持冻结（见 §10）。

---

## 1. 交付范围与远端权威状态

| 项 | 值 |
|---|---|
| 提交集 | `42b0b35^..f41f4ef`，共 13 笔 |
| 变更量 | 24 files changed, 5921 insertions(+), 57 deletions(-) |
| 远端 `main` | `f41f4ef2fdf83df8b98b40738cfcfc6cc73915d1`（`gh api repos/E8A281E6ACA2/GraphInsight/git/ref/heads/main` 取值） |
| 推送方式 | 全部 fast-forward；未使用 `--force`；未改 `remote.origin.url`；未改 hosts；未关证书校验 |
| 推送出口 | HTTPS 到 `github.com` 当时被连接重置，改走 GitHub 官方 SSH-over-443 `ssh://git@ssh.github.com:443/...` 临时 URL |
| 推送前后校验 | 推送前用 `git merge-base --is-ancestor <remote_sha> HEAD` 确认纯 FF，推送后用 API 复核远端 SHA 与本地 HEAD 一致（ahead 0 / behind 0） |

提交逐笔清单：

| SHA | 类型 | 意图 | 规模 |
|---|---|---|---|
| `42b0b35` | feat | M5-A `chunk_revisions` / `targets_hash` 迁移与 backfill 主体 | 6 files, +3008 |
| `ef88722` | feat | v3.2 chunk revision 契约类型与文档同步 | 5 files, +149/-9 |
| `81ce8f4` | fix | 关闭审计四项阻断（孤儿 revision / UNRECOVERABLE / v2 Milvus 查询 / 作用域校验）+ 结构校验 | 4 files, +568/-31 |
| `6eb0631` | test | 活栈取证套件与契约回归（只读 + 真实写入 + SQLite 矩阵） | 4 files, +951/-9 |
| `63f9932` | docs | M5-A 修复轮证据与契约澄清落档 | 3 files, +251/-2 |
| `886a7ca` | test | 任务 #55：迁移 smoke 真隔离（临时 env 文件 + 破坏性动作前方言守卫） | 2 files, +200/-30 |
| `9bb5f5b` | docs | 任务 #55 取证记录 + 断言计数口径统一 | 1 file, +140/-7 |
| `0c18cb6` | fix(ci) | 任务 #31：扫描器 4 处形状盲区闭合 + 误报改由扫描范围承接 | 4 files, +448/-18 |
| `c9433ef` | docs | #31 自检与范围证据落档 | 2 files, +93 |
| `1c49dfe` | docs | #31 推送出口绕行与 push 档 CI 证据 | 2 files, +65/-2 |
| `806fb38` | test | **本轮**：自检步骤名去凭据化 + 第 43 条回显守卫（修我自己的 CI 回归） | 1 file, +35/-17 |
| `ed6a8fc` | docs | **本轮**：§9.11.2 记录 dispatch 变红、根因、复现与修复 | 2 files, +90/-9 |
| `f41f4ef` | docs | **本轮**：M5-A 报告远端状态同步 | 1 file, +1/-1 |

> 审计要点：`806fb38`/`ed6a8fc` 是我方自查出的**自引入回归**的整改笔，不是新需求。
> 事件本身与逐条 sha 级复现见 §6.2，不要把它当作"门禁误报"处理。
>
> **口径说明（避免 SHA 漂移造成误判）**：§1 表列到 `f41f4ef` 为止，就是本次交付的**代码与证据**范围。
> 本审计包自身、以及随后为记录绿态 dispatch 取证而产生的文档笔，属"证据登记笔"，不在复核范围内；
> 审计时请先用 `gh api repos/E8A281E6ACA2/GraphInsight/git/ref/heads/main --jq '.object.sha'`
> 取当前远端 `main`，再用 `git log --oneline 42b0b35^..<该SHA>` 对照本表，尾部多出的 `docs(...)` 笔即为登记笔。

---

## 2. 审计裁定四项阻断问题 → 逐条落点

裁定原文（M5-A 轮）：本轮暂不验收，M5-B 继续冻结，须先修复四项并补结构校验、
`rows_skipped_existing` 输出与验收测试，且在完成真实 PostgreSQL / Neo4j / Milvus 执行态验证前
不得宣布通过。以下逐条给落点。

### 2.1 阻断项 1：inventory 必须纳入已有 current revision，禁止孤儿 revision 被静默跳过

- **实现落点**：`backend/admin/backfill_chunk_revisions.py:503` `_load_current_revisions()`
  读取已存在的 revision 行并进入 inventory；`backend/admin/backfill_chunk_revisions.py:611`
  `build_inventory()` 把"有 revision 但缺索引/投影证据"的 chunk 归为 orphan 并计入 blocked；
  输出行 `backend/admin/backfill_chunk_revisions.py:911` 附近打印 `orphan_revisions=` 与
  `ORPHAN_REVISION chunk_ids` 显式列名。
- **复跑命令**：

  ```bash
  python backend/tests/check_m5a_revision_backfill.py
  ```

- **实测输出（本轮 2026-10-02 复跑，110 项断言全绿）**：

  ```text
  ✓ 修复#1 孤儿 revision 纳入 inventory：exit 3 且 orphan_revisions=1
  ✓ 修复#1 孤儿 revision 显式列名且计入 blocked（非静默跳过）
  ✓ 修复#1 孤儿行仍计入 rows_skipped_existing=1
  ✓ 修复#1 补回索引证据后 orphan 归零、门可关闭（exit 0）
  ```

  对应断言行：`backend/tests/check_m5a_revision_backfill.py:379-383`。
- **覆盖范围**：证明"孤儿 revision 不再被静默跳过"在 SQLite 契约层成立，且收敛路径
  （补证据 → orphan 归零 → 门 CLOSED → exit 0）成立。
- **不覆盖**：真实 PG/Neo4j 上的历史孤儿数据规模未取证（dev 库无孤儿样本），见 §7.4。

### 2.2 阻断项 2：严格实现"无解析产物且无 Milvus 向量 = UNRECOVERABLE_MISMATCH"

- **实现落点**：`backend/admin/backfill_chunk_revisions.py:575` `_has_recoverable_evidence()`
  （解析产物与 Milvus 向量两路证据都为空才判不可恢复）、`backend/admin/backfill_chunk_revisions.py:582`
  `_classify_projection()`。
- **复跑命令**：同 2.1。
- **实测输出**：

  ```text
  ✓ UNRECOVERABLE_MISMATCH：dry-run 拒绝（exit 2）
  ✓ 修复#2 仅 Neo4j 有文本判 UNRECOVERABLE（exit 2）
  ✓ 活栈纠偏：内容可恢复的 chunk 不再误标 UNRECOVERABLE_MISMATCH
  ```

  对应断言行：`backend/tests/check_m5a_revision_backfill.py:343` / `:388` / `:417`。
- **必须注意的口径分离（审计裁定未点名，是我方在活栈阶段自查纠出的邻近缺陷）**：
  "作用域三件套不全"独立判为 `SCOPE_UNRESOLVED`，不混进 `UNRECOVERABLE_MISMATCH`
  （`backend/tests/check_m5a_revision_backfill.py:413-416`，实测 `exit 2` 且
  `SCOPE_UNRESOLVED` 与 `scope_unresolved=1` 同现）。理由：前者是元数据缺失（可通过登记 KB 修复），
  后者是内容不可恢复，混判会让运维动作指向错误方向。
- **不覆盖**：真实业务链路"用户上传 → 解析 → 出 chunks.jsonl"未取证，见 §7.3。

### 2.3 阻断项 3：修复 v2 Milvus 缺少 `content_revision` 字段时的真实查询路径，并补非 mock 测试

- **真实根因（比裁定更进一层）**：不只是"查询请求了不存在的字段"，而是 backfill 侧
  Milvus collection 名与线上读写路径不一致。落点：
  `backend/admin/backfill_chunk_revisions.py:316` `_milvus_has_revision_field()`、
  `:333` `_milvus_query_output_fields()`（按实际 schema 动态裁剪 `output_fields`，
  不请求缺失字段）、`:330` `MILVUS_BASE_OUTPUT_FIELDS`、`:355` `_load_milvus_chunks()`；
  collection 名归一化（历史 `graphinsight_chunks` → `_v2`）见报告 §5.2。
- **非 mock 证据（真实 Milvus 实例，全程零写入）**：
  `backend/tests/check_m5a_live_stack_readonly.py:121-153` 直接调用被修复的两个函数并跑真实 query。
  复跑命令：

  ```bash
  cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py
  ```

  实测断言（20 项，摘录关键行；断言名取自源文件，实跑记录见报告 §3.3）：

  ```text
  ✓ 探测到的字段集合非空
  ✓ 动态 output_fields 是实际字段的子集（不会请求不存在字段）
  ✓ v2 缺 content_revision：动态 output_fields 已剔除
  ✓ §8.5 判定：milvus_revision_field=False（不伪标）
  ✓ 真实 query（动态 output_fields，限定 kb）未报错
  ✓ _load_milvus_chunks 真实读取未抛异常
  ```

- **契约层（避免只有活栈一次性证据）**：`backend/tests/m5a_backfill_driver.py:141` 把
  `_milvus_has_revision_field` 固定为 `False` 构造 v2 场景，驱动 SQLite 矩阵里的相关断言；
  collection 名归一化断言见 `backend/tests/check_m5a_revision_backfill.py:423-424`
  （实测 `__COLL_LEGACY__graphinsight_chunks_v2`）。
- **不覆盖（结构性阻断，不是"没跑"）**：dev 活栈的 v2 collection **schema 无 `content_revision` 字段**，
  且验收约束（设计文档 §8.5）禁止改 schema，因此"向量投影真实 backfill 成功"这条腿在当前 v2 上
  **不可能取证**，必须等 v3 collection 迁移。见 §7.2。

### 2.4 阻断项 4：backfill 写入前校验 KB/tenant/project 作用域一致性，冲突必须零写入并 fail-closed

- **实现落点**：`backend/admin/backfill_chunk_revisions.py:489` `_load_kb_scope()`
  （从 `knowledge_bases` 读 tenant/project）、`:540` `_add_scope_mismatch()`、
  `:546` `_check_row_scope()`（已有 revision 行逐字段比对）、`:560` `_check_plan_scope()`
  （写入前对每条 plan 的来源证据比对）。
- **复跑命令**：同 2.1。
- **实测输出（2026-10-02 复跑原文，未改写）**：

  ```text
  ✓ 修复#4 新 chunk tenant 与 KB 登记冲突 → fail-closed（exit 2）
  ✓ 修复#4 冲突明细含 expected/actual
  ✓ 修复#4 冲突时零写入（PG 行、job、索引调用全空）
  ✓ 修复#4 已有行 project 与 KB 登记冲突 → fail-closed（exit 2）
  ✓ 修复#4 已有行冲突同样零新增写入
  ✓ 活栈纠偏：三件套不全判 SCOPE_UNRESOLVED 且 fail-closed（exit 2）
  ✓ 活栈纠偏：SCOPE_UNRESOLVED 同样零写入
  ```

  对应场景与断言：`backend/tests/check_m5a_revision_backfill.py:391-416`
  （场景 `scope_conflict_new` / `scope_conflict_row` / `scope_unresolved`）。
  "零写入"不是解读而是断言内容：脚本在拒绝前后各数一次写入面（PG 行、job、索引调用）并断言全空。
- **活栈侧同口径**：只读取证脚本把"有解析产物但未登记 KB"的目录自动探测出来，
  固化成 `SCOPE_UNRESOLVED` 拒绝腿（报告 §3.3），不再依赖手工命令。
- **不覆盖**：真实多租户并发写入下的竞态窗口未取证（`insert_conflicts_skipped` 作为竞态指标
  至今没有真实触发样本），见 §7.4。

---

## 3. 裁定补充项：迁移 schema/index 结构校验 + `rows_skipped_existing` 输出 + 验收测试

### 3.1 结构校验器

- 落点：`backend/admin/m5a_schema_check.py`（独立脚本，331 行）。关键设计：
  PostgreSQL 侧**不解析 `indexdef` 文本**，直接读 `pg_index` / `pg_attribute` 取权威列序与谓词
  （`backend/admin/m5a_schema_check.py:113-143`）。原因写在注释里：文本解析在 partial index 上
  必然出错（列括号后还跟 WHERE 子句括号）——这是首轮活栈取证命中的"结构校验器在 PG 上假失败"
  缺陷（报告 §5.1），属真实修复而非美化。
- 复跑命令（真实 PG，只读、非破坏）：

  ```bash
  cd backend && PYTHONPATH=. python admin/m5a_schema_check.py both
  ```

- 实测：dev 活库通过（报告 §3.2 摘录）。SQLite 侧同样有列/索引读取分支
  （`backend/admin/m5a_schema_check.py:102` / `:144-165`）。

### 3.2 `rows_skipped_existing`

- 落点：字段 `backend/admin/backfill_chunk_revisions.py:104`，递增 `:659`，
  打印 `:911`，**并在写入决策后以决策时点为准**回填到 fresh inventory（`:1060-1062`）——
  这条口径是防"本轮新建行被误算成已有跳过"的坑，代码里写明了。
- 验收测试：`backend/tests/check_m5a_revision_backfill.py:301`（新库首轮 `rows_skipped_existing=0`）
  与 `:305-306`（幂等重跑 `rows_skipped_existing=1` 且 `insert_conflicts_skipped=0`）。

### 3.3 执行态（真实写入）取证与顺带修复的第 5 项缺陷

- 授权范围：用户明确批准"dev 上用专用合成 KB 跑 / 只用合成专用 KB"，写入面严格限制在
  kb_id 前缀 `m5a-live-`（本轮样本 `m5a-live-20261001`），取证后按 kb_id 整块回收并复核回基线
  （`chunk_revisions=0`、`admin_jobs=21`），未触碰任何真实 KB。
- 该腿暴露并修复了一个 mock 结构上不可能发现的静默缺陷：**同一轮新写入但未收敛的 chunk
  没有排入 reindex job**（表现为 `[reindex] targets_total=1` 与最终报告
  `needs_reindex_targets=3` 矛盾）。落点与回归：报告 §5.4、
  SQLite 新增场景 `rfa_run` / `rfa_rerun`。
- 复跑需要显式授权窗口（不带 `--confirm` 只打印计划并 `exit 2`，这是刻意的确认门槛）：

  ```bash
  cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py            # 只看计划，exit 2
  cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py --confirm  # 授权执行 + 自动清理
  ```

---

## 4. 任务 #55：迁移测试 DB 伪隔离（高危项，请重点复核）

- **为什么高危**：`backend/tests/check_kb_migrations_smoke.py` 会真的执行迁移与回滚，其中含
  **drop 表**动作。若"隔离到 SQLite"是假的，被 drop 的就是开发 PostgreSQL 库的表。
- **根因**：项目 `admin/database.py` 用 `load_dotenv(find_dotenv(), override=True)`，
  会沿 `__file__` 向上找到 `backend/.env` 并**覆盖**进程环境变量。因此
  `GRAPHINSIGHT_BACKEND_ENV_FILE=""`（置空）根本不生效——变量指向的 env 文件不存在时会被忽略，
  真实 PG 地址仍被注入。这是"伪隔离"。
- **修复**：把该覆盖变量指向**临时目录里真实存在、内容就是 SQLite URL** 的 env 文件
  （`backend/tests/check_kb_migrations_smoke.py:104-149`，写文件见 `:162-166`），
  并在任何破坏性动作之前加**方言守卫**：先断言当前 engine 是 sqlite，不是 sqlite 就直接失败退出
  （`guard_isolation()`，`:104-121`；子进程侧同样断言 `dialect=sqlite`，`:135` / `:149`）。
- **防回归守卫（静态）**：`backend/tests/check_migration_cleanup_guards.py:514-546` 禁止
  sqlite 隔离测试把该变量置空或取成 `""`，并强制要求指向真实 env 文件的写法。
  该守卫还有**负向自证**：故意造两条违规探针文件时必须报错（报告 §9 摘录
  `PROBE_EXIT=1`，`AssertionError: ... 置空 env 覆盖变量（伪隔离）`），
  证明守卫不是空规则。
- **复跑命令**：

  ```bash
  python backend/tests/check_kb_migrations_smoke.py            # 19 项，EXIT=0
  python backend/tests/check_migration_cleanup_guards.py       # GUARD_EXIT=0
  ```

- **历史是否曾在活 PG 上 drop 过表**：报告 §9.2 给结论 + **明确标注不可判定窗口**，
  没有把"没查到证据"写成"没发生过"。请审计人员按该节口径评估，不要接受更乐观的表述。

---

## 5. 任务 #31：扫描器盲区闭合、范围化误报治理与自检

- **扫描器**：`backend/tests/check_artifact_secrets.py`。赋值形状正则的四处盲区已闭合
  （键名两侧可选引号、键前缀/后缀下划线形态、值含 `;` `{}`、JSON 引号键），
  放宽字符集带来的误报改由**值形状判别**（`:133` `_is_structural_value`、
  `_is_code_value` 的"点分标识符紧跟左括号"）与**扫描范围**承接。
- **范围层**：`DEFAULT_SHAPE_EXCLUDES`（`playwright-report/`、`test-results/`、`node_modules/`、
  `dist/`、`build/`、`*.min.js`、`*.min.css`、`*.map`）**只关闭赋值形状一档**，
  `run_credential` 字面值与 jwt / dsn / bcrypt 三个高置信形状照常扫；
  `SUMMARY` 增 `shape_scanned_files` / `shape_excluded_files` / `exclude_rules` 三键，
  并对每个被排除文件打印 `SECRET_SCAN_NOTE`（`layers_still_scanned=`）使范围可审计。
- **pass/fail 判据未变**：仍是 `findings=0` 才放行。**未放宽任何验收阈值。**
- **自检**：`backend/tests/check_artifact_secrets_selftest.py`，43 项断言（初版 42，
  第 43 项是 §6.2 那次 CI 变红后补的守卫），分六组：正样本 11、负样本 12、
  字面值与 fixture 放行 6、排除范围 7、输出与退出码契约 6、步骤名回显守卫 1。
- **守卫有效性负向对照**：把 `scan()` 的形状循环改成空迭代（即"门禁永远干净"），自检必须变红
  ——实测 `assertions=37 failed=14`（该实验发生在补 5 条代码形态断言之前，故当时总数为 37），
  随后按 sha256 逐字节还原并复跑 `43/0`。
- **接线**：`backend/tests/run_unified_boundary_guards.py` 新增 case `secret_scanner_selftest`
  （本地 `SUMMARY total=16 failed=0`）；`.github/workflows/ci.yml` 的 `backend-scripts` job
  py_compile 清单加入该自检文件。
- **复跑命令**：

  ```bash
  python backend/tests/check_artifact_secrets_selftest.py     # assertions=43 failed=0
  python backend/tests/run_unified_boundary_guards.py         # SUMMARY total=16 failed=0
  ```

---

## 6. CI 与远端证据矩阵（含一次我方自引入的回归，已认账并修复）

### 6.1 已取到的证据

| run | event | 档位 | 结论 | 取到什么 |
|---|---|---|---|---|
| `36937457198` | push | push 档（4 job）| **success** | CI 日志含 `SECRET_SCAN_SELFTEST_SUMMARY assertions=42 failed=0 result=pass`、`[OK] secret_scanner_selftest`、`SUMMARY total=16 failed=0`；Linux runner 与本地 Windows 计数一致；另以 `commits/c9433ef/check-suites` 第三方复核 Actions = completed/success |
| `36939737132` | workflow_dispatch（`run_release_acceptance=true`）| dispatch 档 | **fail** | step 16 报 `findings=7 result=fail`，产物 withhold。这是 §6.2 的那次回归。该 run 同时给到新三键实跑取值 `shape_scanned_files=5 shape_excluded_files=1 exclude_rules=8`，但**因运行整体为红，这组数值只能证明"CI 里确实按新参数执行并计数"，不能当绿灯证据** |
| `36942264746` | workflow_dispatch（`run_release_acceptance=true`，headSha=`f41f4ef`）| dispatch 档 | **success** | 修复后的绿态运行。step 16：`SECRET_SCAN_SUMMARY paths=4 files=6 bytes=690679 shape_scanned_files=5 shape_excluded_files=1 exclude_rules=8 credential_env_vars=2 allowed_fixtures=3 findings=0 result=pass`；同一 job 内 `SECRET_SCAN_SELFTEST_SUMMARY assertions=43 failed=0 result=pass`、`SUMMARY total=16 failed=0`。job 明细：Go backend tests / Backend unified boundary guards / Backend smoke script syntax / Frontend build / Full release acceptance 全 success，其余 6 个未启用 dispatch job skipped |

> push 档不执行任何 scanner step：6 个 scanner step 全在 `workflow_dispatch` 档 job
> （release-frontend-e2e、release-acceptance、rollback-matrix、frontend-e2e、perf-probe、perf-soak）。
> 因此"新 SUMMARY 键的绿态实跑取值"必须靠 dispatch 运行，不能用 push 档充数。

### 6.2 回归事件、根因与逐字节复现（请审计人员按此复核，勿当作误报）

- **现象**：run `36939737132` step 16「Scan upload paths for credential material」
  `findings=7 result=fail`，整条 release-acceptance 判红。
- **根因（单环）**：自检套件的**步骤名里写了样本凭据字面量**；release-acceptance 会把统一守卫的
  stdout 落成 `artifacts/release-acceptance/acceptance-summary.log`，而该路径在扫描范围内
  ⇒ 门禁判红**自己的测试夹具**。
- **逐条定位口径**：`match_sha256` 是对**整段 match token**（`match.group(0)`）取 sha256 前 12 位，
  不是对被捕获的 `value` 取（`backend/tests/check_artifact_secrets.py:184`）。
- **端到端复现（沙箱，与 CI 逐 sha 相同）**：用 `0c18cb6` 的旧自检 + 旧扫描器
  （`git diff 0c18cb6 HEAD -- backend/tests/check_artifact_secrets.py` 为空，即与 HEAD 逐字节相同）
  跑旧自检，把 stdout 落成同名日志文件，再用 CI 同款 `--allow-fixture` 参数扫描：

  ```text
  修前：findings=7 result=fail
        dcc4b2a6f3e1 / 7433b91c7e0b / 53883c1e64a9 / 86cd51a4eed9
        / dfa61c5dae47 / 58db693c7363 / 677a9029b08c
        —— 与 run 36939737132 step 16 的 7 条完全一致
  修后：同一扫描器、同一参数，扫新自检 stdout → findings=0 result=pass
        （新自检自身 assertions=43 failed=0 result=pass）
  附加：把守卫套件整段 stdout（16 个 case，11824 bytes）落盘复扫 → findings=0 result=pass
  ```

- **修复顺序（TDD，先让守卫变红再改文本）**：先加第 43 条 `label_echo_guard`
  （把全部步骤名回写成日志交给扫描器，要求 `findings=0`）→ 旧标签下实测红
  （`assertions=43 failed=1`）→ 再把标签改成描述形状、不写凭据字面量（样本仍写临时文件喂扫描器，
  **断言语义一条没变**）→ 复跑 `43/0`。落点：`backend/tests/check_artifact_secrets_selftest.py:288-298`。
- **勘误**：调查过程中一度出现的 `fa725dc736a9` 是我用 grep 重拼 CI 日志产生的**伪命中**，
  CI 真实命中集合里没有它。已核对，不写进结论。
- **规则沉淀**：任何会被落进扫描路径的 stdout（守卫、验收、CI 日志）都不允许出现凭据形状字面量；
  这条已由第 43 项断言机器化，不再依赖人记住。

### 6.3 这条腿现在的状态（已闭合，但闭合的是哪一条要说清）

- **已闭合**：`SECRET_SCAN_SUMMARY` 三个新键在 CI 的**绿态**实跑取值已由 run `36942264746`
  取到（`shape_scanned_files=5 / shape_excluded_files=1 / exclude_rules=8`，`findings=0 result=pass`）。
  因此报告 §9.11.1 里"新 SUMMARY 键仅有本地取证"的表述可以升级为"CI 已实跑并取得绿态取值"。
- **没有因此变强的是拦截力**：这次绿态仍然只证明"扫描器在 CI 里按新参数执行、计数自洽、且对自己的
  测试输出保持安静"。CI 各扫描步骤的目标目录（`frontend/playwright-report`、`frontend/test-results`）
  本身在默认排除清单内，赋值形状层在这些腿上预期不参与判定，实际拦截力仍押在
  `run_credential` 字面值 + jwt/dsn/bcrypt 三形状 + `--secret-env-var` 上（已知代价 2 同一口径）。
  `shape_excluded_files=1` 就是这个排除分支的实跑体现。
- **仍未取证的 CI 腿**：`--no-default-excludes`（全量扫赋值形状）在 CI 里没有对应 step；
  排除范围与赋值形状层的联合效果只有在本地自检矩阵里成立。若审计要求"CI 侧也扫到赋值形状层"，
  需要新增一条 dispatch step（本轮未擅自加，因为那会改变 CI 判定面，需另行批准）。

---

## 7. 明确未验证 / 结构性缺口（审计裁定要求"不得当作已通过"的部分）

1. **`needs_reindex` 的收敛闭环是结构性缺口，不是"没跑"**：全仓 `reindex_chunks` 只命中 backfill
   与 M5-A 测试三处，**没有任何 worker/执行器消费该 job**，消费方属 M5-B Go API。
   因此"投影从 pending 收敛到 indexed、前置门 CLOSED"这条腿在**当前代码库上不可能取证**；
   只要一个 KB 存在未收敛投影，backfill 就永远 `exit 3`。
   `admin_jobs` 侧只验证到"真实入队 + `targets_hash` 复用 + 门保持 OPEN"。
2. **Milvus 向量侧真实写入未取证**：活栈 v2 collection 无 `content_revision` 字段，
   按设计文档 §8.5 禁止改 schema，故"向量投影真实 backfill 成功"必须等 v3 collection 迁移。
3. **真实业务链路未验**：执行态用的是合成 `knowledge_bases` 行 + 合成解析产物
   （`parsed_documents/m5a-live-20261001/`），不是"用户上传 → 解析 → 出 chunks.jsonl"。
   backfill 读写契约已验，端到端业务链路未验。
4. **规模与并发未做**：合成 KB 只有 3 个 chunk，dev 全库也只有 10 个 chunk，不具备容量与并发
   取证条件；`insert_conflicts_skipped` 作为竞态指标因此没有真实触发样本。
5. **文档状态未同步**：`docs/ENTERPRISE_ROADMAP_CHECKLIST.md` / `ENTERPRISE_IMPLEMENTATION_BACKLOG.md`
   尚无 M5-A 条目（该轮未擅自标注状态）。
6. **契约矩阵的一次环境抖动**：本轮首跑 `check_m5a_revision_backfill.py` 得 `108 pass / 2 fail`，
   失败用例报 `MemoryError`（子进程 `importlib.get_data`）导致建表未完成、级联
   `no such table: admin_jobs`；原地复跑两次得 `110 pass / 0 fail`（`EXIT=0`），
   同路径 `bf_scope1/3/4` 均通过。判定为环境瞬时抖动、非代码回归，
   但**该口径只有本地一次性证据、未进 CI**，审计上按"未闭环"处理。
7. **文档自身示例会被扫描器命中**：四份相关文档合计 `findings=11`
   （M4-R1 报告 9 + roadmap 1 + M5-A 报告 1 + **本审计包 0**），全部是文档化合成样本、无真凭据，
   且这四份文档都不在 CI 扫描路径内。若将来把 `docs/` 纳入扫描范围，必须先建 fixture 放行表，
   否则会立刻红。本包写作时刻意不含凭据字面量，就是为了让"文本对门禁安静"这条规则可被机器验证。
8. **CI 字面值层的"参数为空即失效"窗口（绿态运行暴露，本轮未修）**：step 16 打印
   `SECRET_SCAN_NOTE env_var_unset name=ADMIN_TOKEN`，三个 `--secret-env-var` 实际只有 2 个有值
   （`credential_env_vars=2`）参与字面值比对。也就是说"本轮注入的 admin token 不得出现在产物里"
   这条约束在该 step 上**没有真正生效**，原因是 CI 侧凭据注入时机与 step 顺序，而非扫描器形状层。
   修复需要改动 workflow 判定面，**未擅自改动**，此处点名登记。
9. **CI 没有 `--no-default-excludes`（全量赋值形状）的 step**：排除范围与赋值形状层的联合效果
   目前只在本地自检矩阵成立，CI 侧未覆盖。

---

## 8. 审计人员独立复核清单（按顺序执行，全部为只读或临时库操作）

```bash
# 0) 环境（Windows 本机无 venv，用系统 Python + UTF-8；Linux/CI 用 backend/.venv/bin/python）
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# 1) 远端权威状态（不信本地 tracking ref）
gh api repos/E8A281E6ACA2/GraphInsight/git/ref/heads/main --jq '.object.sha'   # 期望 f41f4ef2fdf8...
git log --oneline 42b0b35^..f41f4ef | wc -l                                     # 期望 13
git diff --shortstat 42b0b35^ f41f4ef                                           # 期望 24 files, +5921/-57

# 2) M5-A 契约矩阵（临时 SQLite，无需活栈）
python backend/tests/check_m5a_revision_backfill.py        # 期望 EXIT=0，110 项 ✓
python backend/tests/check_m5a_revision_backfill.py 2>&1 | grep -c "^  ✓"   # 期望 110

# 3) 迁移 smoke 真隔离 + 静态防回归守卫
python backend/tests/check_kb_migrations_smoke.py          # 期望 19 项，EXIT=0
python backend/tests/check_migration_cleanup_guards.py     # 期望 GUARD_EXIT=0

# 4) 扫描器自检 + 守卫套件
python backend/tests/check_artifact_secrets_selftest.py    # 期望 assertions=43 failed=0 result=pass
python backend/tests/run_unified_boundary_guards.py        # 期望 SUMMARY total=16 failed=0

# 5) 门禁对自己的测试输出是否安静（本次回归的核心不变量）
python backend/tests/check_artifact_secrets_selftest.py > /tmp/selftest_stdout.log
python backend/tests/check_artifact_secrets.py --path /tmp/selftest_stdout.log \
  --allow-fixture graphinsight-dev-password --allow-fixture change-this-password \
  --allow-fixture ci-internal-token                        # 期望 findings=0 result=pass

# 6) 语法检查（改动过的模块）
python -m py_compile backend/admin/backfill_chunk_revisions.py \
    backend/admin/m5a_schema_check.py backend/admin/migrate_chunk_revisions.py \
    backend/admin/migrate_jobs_targets_hash.py backend/tests/m5a_backfill_driver.py \
    backend/tests/check_m5a_revision_backfill.py backend/tests/check_m5a_live_stack_readonly.py \
    backend/tests/check_m5a_live_execution.py backend/tests/check_kb_migrations_smoke.py \
    backend/tests/check_migration_cleanup_guards.py \
    backend/tests/check_artifact_secrets.py backend/tests/check_artifact_secrets_selftest.py

# 7) 活栈项（需要真实 PG/Neo4j/Milvus；第 8) 条含写入，必须显式 --confirm 且只用合成 KB）
cd backend && PYTHONPATH=. python admin/m5a_schema_check.py both
cd backend && PYTHONPATH=. python tests/check_m5a_live_stack_readonly.py
cd backend && PYTHONPATH=. python tests/check_m5a_live_execution.py            # 不带 --confirm 只打印计划并 exit 2
```

---

## 9. 变更面清单（24 files）

| 文件 | 变更量 | 归属 |
|---|---|---|
| `backend/admin/backfill_chunk_revisions.py` | +1117 | M5-A 主体 + 四项修复 + 作用域校验 + 结构口径 |
| `backend/admin/m5a_schema_check.py` | +331 | 迁移结构/索引校验（裁定补充项） |
| `backend/admin/migrate_chunk_revisions.py` | +270 | 迁移 + 回滚 |
| `backend/admin/migrate_jobs_targets_hash.py` | +193 | 迁移 + 回滚 |
| `backend/services/scope_contract.py` | +26/-… | 作用域归一化与 Milvus filter 复用 |
| `backend/core/exceptions.py` | +12 | 新增错误类型 |
| `go-backend/internal/scope/scope.go` | +35/-… | Go 侧契约类型同步 |
| `backend/tests/check_m5a_revision_backfill.py` | +472 | SQLite 契约矩阵（110 项） |
| `backend/tests/m5a_backfill_driver.py` | +437 | 场景驱动（含 v2 无字段场景） |
| `backend/tests/check_m5a_live_execution.py` | +452 | 活栈真实写入执行态（授权窗口 + 自动清理） |
| `backend/tests/check_m5a_live_stack_readonly.py` | +248 | 活栈只读非 mock（零写入自证） |
| `backend/tests/check_kb_migrations_smoke.py` | +190/-… | #55 真隔离 + 方言守卫 |
| `backend/tests/check_migration_cleanup_guards.py` | +40 | #55 静态防回归守卫（含负向自证） |
| `backend/tests/check_artifact_secrets.py` | +138/-… | #31 盲区闭合 + 范围层 + SUMMARY 三键 |
| `backend/tests/check_artifact_secrets_selftest.py` | +339 | #31 自检（43 项） |
| `backend/tests/run_unified_boundary_guards.py` | +6 | 接线 `secret_scanner_selftest` case |
| `.github/workflows/ci.yml` | +1 | py_compile 清单加入自检文件 |
| `.gitignore` | +1 | 临时产物忽略 |
| `docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` | +967 | 设计文档（§8.5 / §15.3 口径） |
| `docs/ENTERPRISE_M5A_FIX_ACCEPTANCE_REPORT.md` | +381 | M5-A 修复轮验收报告 |
| `docs/ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md` | +195 | #31 证据 + §9.11.1/§9.11.2 |
| `docs/ENTERPRISE_BACKEND_API_SPEC.md` | +67 | 契约同步 |
| `docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` | +18/-… | P0 契约同步 |
| `docs/ENTERPRISE_ROADMAP_CHECKLIST.md` | +42 | 路线状态与遗留项 |

---

## 10. 立场声明与修订记录

- **M5-A 不宣布验收通过**；**M5-B Go API 保持冻结**，直到裁定方认可 §7 的缺口处置方式。
- 全程未使用 force push；未修改任何验收阈值（pass/fail 判据仍为 `findings=0`）；
  未在文档、日志或测试输出中写入真实 token / 密码 / API key；
  活栈写入只发生在 `m5a-live-` 前缀合成 KB，取证后整块回收并复核回基线。
- 修订记录：
  - v1（2026-10-02）：覆盖 `42b0b35..1c49dfe` 10 笔的审计复核包首版。
  - v1.1（2026-10-02）：补入 `806fb38`/`ed6a8fc`/`f41f4ef` 三笔（自引入 CI 回归的整改与记录），
    §6.2 改为 sha 级逐条复现表述，§7 补第 6 条（契约矩阵环境抖动）与第 7 条（文档自身命中口径）。
  - v1.2（2026-10-02）：绿态 dispatch 取证到位（run `36942264746`，headSha=`f41f4ef`，
    `findings=0 result=pass` + `assertions=43 failed=0` + `SUMMARY total=16 failed=0`），
    §6.1 增该运行、§6.3 由"未闭合"改写为"已闭合，但闭合的是哪一条"；
    §2.4 的断言引用改为本轮 110 项矩阵复跑原文（初稿凭记忆写的三条标签不准确，已勘误），
    §2.3 活栈断言名标明"取自源文件，实跑记录见报告 §3.3"。
  - v1.3（2026-10-02）：追加 §11——验收基础设施整改轮（UTF-8 链 + 复跑取证 + M5-A/M5-B 依赖方案）；
    §11.6 对本包 §7 与报告 §6 的"门收敛在 M5-B 之前不可能取证"作**措辞勘误**（前置是 M5-B0 + Milvus v3，不是整个 M5-B），
    §11.5 澄清一条扫描命中的实际形态（已打码，非泄露）。**立场与结论未变**。

---

## 11. 追加轮：验收基础设施整改（2026-10-02，UTF-8 链 / 复跑取证 / 依赖方案）

### 11.1 交付范围与 git 状态

| 笔 | 内容 | 类型 |
| --- | --- | --- |
| `7c1ccba` | 5 个验收脚本自强制 UTF-8 + `returncode` 断言 + 失败全量转储 + 静态防回归守卫 | 代码 |
| `7b9e09a` | 报告新增 §10（含 §10.4 全仓同类缺口清单）、§8 v4；M4R1 报告 `:110` 就地标注失效 | 文档 |
| `634444e` | 新增 `docs/ENTERPRISE_M5AB_REINDEX_DEPENDENCY_PLAN.md` v1（提案，未改代码）+ 报告 §11/§8 v5 | 文档 |

远端权威态仍为 `origin/main = 2542691`；**本轮三笔只在本地**（`git rev-list --left-right --count origin/main...HEAD` = `0 3`，工作区干净），未 push、未 force。

### 11.2 三项指令 → 落点对照（逐条可验）

| 要求 | 落点 | 证据 |
| --- | --- | --- |
| 父进程 stdout/stderr 强制 UTF-8 | `check_kb_migrations_smoke.py`、`check_m5a_live_stack_readonly.py`、`check_m5a_live_execution.py`、`run_unified_boundary_guards.py`、`check_migration_cleanup_guards.py` 均在导入后立即 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` | 静态守卫逐文件断言，§11.3 第 5 条命令即其执行入口 |
| 所有 Python 子进程传 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` | 统一 `_utf8_env()`（只读、执行两脚本）与 `env=` 注入（smoke、守卫入口） | 守卫用 `ast` 精确识别 `subprocess.run/check_output/call/check_call` 缺 `env=` 的行号并判红 |
| 已登记 KB 的 CLI 检查必须断言 `returncode == 0` | 原步骤 `CLI dry-run 有结构化输出（exit=1）` 是**假绿灯**（子进程已崩仍判通过），现改为 `proc.returncode == 0 and "[capabilities]" in out and "[inventory]" in out` | `check_m5a_live_stack_readonly.py`；未登记 KB 腿改为断言 `exit == 2`（`SCOPE_UNRESOLVED` 语义） |
| 失败时输出完整 exit code 与 stderr | `_dump_failure()`（smoke）与 `_run_backfill_cli`/`run_cli` 内联转储；守卫入口对失败腿取消 12000 字符截断 | 报告 §10.2 |
| 不允许用 `-X utf8` 掩盖脚本缺陷 | 该"运行前提"已废止，M4R1 报告原地处打失效标注；本包所有复跑命令一律 `env -u PYTHONUTF8 -u PYTHONIOENCODING python …` | §11.3 |
| 第二项五条命令普通 `python` 全 exit 0 | 见 §11.3 实测 | 已复核 |
| 第三项依赖方案 | 新文档；本包 §11.6 记其对旧措辞的勘误 | 已交付（**提案，未实现**） |

### 11.3 审计人员独立复核（Windows，只读 / 临时库）

前置自检（证明默认码仍是 gbk，避免"被环境变量偶然救场"）：

```bash
python -c "import sys; print(sys.stdout.encoding)"      # 期望 gbk / cp936
```

五条命令（**不要**加 `-X utf8`）：

```bash
env -u PYTHONUTF8 -u PYTHONIOENCODING python backend/tests/check_m5a_revision_backfill.py
env -u PYTHONUTF8 -u PYTHONIOENCODING python backend/admin/m5a_schema_check.py both
env -u PYTHONUTF8 -u PYTHONIOENCODING python backend/tests/check_m5a_live_stack_readonly.py
env -u PYTHONUTF8 -u PYTHONIOENCODING python backend/tests/check_kb_migrations_smoke.py
env -u PYTHONUTF8 -u PYTHONIOENCODING python backend/tests/check_migration_cleanup_guards.py
```

本轮最后一次实跑结果（2026-10-02）：五条全部 `EXIT=0`；断言数按 `grep -c "^  ✓"` 口径为
**矩阵 110 / 只读 20 / 迁移 smoke 19**，schema 校验 69 项通过，守卫输出末行 `MIGRATION_CLEANUP_GUARDS_OK`。
统一守卫入口 `backend/tests/run_unified_boundary_guards.py` 同批 `EXIT=0`，末行 `SUMMARY total=16 failed=0`。

判据说明（避免误判为"应该 exit 0 却不是"）：backfill CLI 的退出码是语义化的——已登记 KB dry-run = 0；
`SCOPE_UNRESOLVED`/`SCOPE_MISMATCH` = 2；执行完成但前置门 OPEN = 3；未带 `--confirm` = 2。
期望值不为 0 属设计上的刻意不收敛，不是缺陷。

`check_m5a_live_execution.py --confirm` **本轮未执行**（写 dev 活栈需单独授权，其 `EXIT=2` 只打印计划）。

### 11.4 越界声明与我方认账

1. **越界**：用户点名 3 个脚本，实际改了 5 个（多出统一守卫入口与静态守卫文件）。理由：同一链路同一缺陷类，统一入口直跑必崩；不越界则"整改后全绿"不成立。已如实登记。
2. **未越界但留坑**：全仓同类 UTF-8 缺口共 22 个文件（13 个 `migrate_*.py` + backfill/reset/seed CLI + 4 个套件入口 + 18 处子进程调用）本轮**未修改**，清单见报告 §10.4。这些文件直跑仍会崩，需拍板是否收。
3. **假绿灯性质最重**：只读脚本曾长期在子进程崩溃的情况下判"通过"，属"门禁自身说谎"，不是环境问题；整改采用"断言只加不减"，未放宽任何既有阈值。

### 11.5 需澄清的一条扫描命中（防误报为泄露）

`check_artifact_secrets.py` 对 `docs/ENTERPRISE_M5A_FIX_ACCEPTANCE_REPORT.md:277` 报 1 条
`dsn_with_credentials`（`match_sha256=49c06b1a354b`，40 字符）。该行是一条本地开发库连接串，
**其口令字段确认为 4 个星号（已打码）**，与本包 §10 立场声明"未写入真实密码"一致。
（此处刻意不复述该串形状：初稿复述过一次，直接把本包自身从 `findings=0` 打成 `findings=1`，
即"门禁被自己的输出判红"，已删除。）复核方法（只输出布尔值，不打印明文）：

```bash
python -c "import re;l=open('docs/ENTERPRISE_M5A_FIX_ACCEPTANCE_REPORT.md',encoding='utf-8').read().splitlines();c=re.search(r'://([^@\s]*)@',l[276]).group(1);print(set(c.split(':',1)[1])=={'*'})"
```

期望 `True`（本轮实测 `True`）。当前四份文档合计 `findings=10`（M5A 报告 1 条形状命中 + M4R1 报告 9 条历史标注样本），
新增设计文档单独扫描 `findings=0`。**本包自身必须保持 `findings=0`，任何后续追加章节都要复扫自证。**
此前汇报过的"11"取自不同路径集合，非同一基线，特此对齐。

### 11.6 对 §7 / 报告 §6 的措辞勘误（重要，请以此为准）

旧表述："`reindex_chunks` 无消费方 ⇒ 门收敛这条腿**在 M5-B 之前不可能取证**"。该句把"消费方"与"M5-B"错误绑定。代码事实：

1. 入队方与执行方都在 Python（`backfill_chunk_revisions.py:816-866` 直连 SQL 入队；`job_service.py:233-301` 是唯一执行器；`job_service.py:361-369` 按 `RUNNABLE_JOB_TYPES = {build_graph, clear_kb, reindex}` 过滤 ⇒ `reindex_chunks` 永远停在 `pending`，不执行也不报错）；Go 只做 INSERT / 列表 / 唤醒，从不执行 job（`admin_jobs_native.go:727-751`）。
2. 因此门收敛的前置是 **C1 Milvus v3 显式 `content_revision` 字段** + **C2 Python worker 消费 `reindex_chunks`**，两者构成不触碰 Go 的最小切片（M5-B0）；只做 C1 收敛不了任何既有 `needs_reindex_targets`，只做 C2 会被 v2 缺字段的拒写路径（`backfill_chunk_revisions.py:397-410`，`MILVUS_REVISION_FIELD_ABSENT`）拦回 `pending`。
3. 另有 **C3**：`blocked` / 孤儿 revision / `unrecoverable` / `scope_*` 归零属人工数据治理，reindex 无法解决——门 CLOSED 的判据里已把它们与"可收敛"分开（`backfill_chunk_revisions.py:869-888`）。

**该勘误不改变任何结论**：M5-A 仍不宣布通过，M5-B Go API 仍冻结，"门收敛腿无证据"这一判定不变，只是把"等什么、谁负责"说准。依赖方案另暴露两处既有口径问题（`content_revision` 硬编码为 `1` 的版本假降风险；`job_type` 白名单在 Python/Go 共 4 份且零对账守卫），均登记未修。

### 11.7 待裁定方 / 用户拍板的三点

1. 是否授权 **M5-B0** 开工（会改 `RUNNABLE_JOB_TYPES` 并新增真实写索引的执行体）。不授权则"CLOSED 腿"只能以结构性不可达结案。
2. Milvus v3 迁移窗口（dev 共享环境建 collection + S1 双写 + 回滚预演）的时间与授权。
3. §11.4 第 2 条那 22 个文件是否纳入本轮验收面（推荐：M5-B0 前收，CI 直跑入口风险最高）。
