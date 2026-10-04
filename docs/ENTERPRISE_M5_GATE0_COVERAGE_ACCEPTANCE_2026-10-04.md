# M5 Gate 0 全量覆盖证据合同验收记录（2026-10-04）

任务：`audit/m5-gate0-completion`（基线 `origin/main=59332f4`）
执行边界：只实现 + 隔离测试。**未连接、未写入共享 dev；未修改共享 v2；未创建共享 v3；
未运行 backfill、未入队任务、未启动 worker；未启动 M5-B1；未 push。**

结论口径：**Gate 0 contract implementation complete（隔离层）。**
共享生产 Gate 0 **不**宣布 CLOSED，仍为 OPEN。

---

## 1. 交付内容

### 1.1 feat：`343dd10` feat(admin): complete M5 Gate 0 coverage evidence

`backend/admin/report_m5_c3_inventory.py`（+473/-17）：

1. 覆盖计数：`registered_kb_total / active_kb_total / eligible_kb_total /
   probed_kb_total / omitted_kb_total / failed_kb_total`，写入 `C3_SUMMARY` 与
   新增 `GATE0_COVERAGE` 输出行。
2. `omitted_kbs / failed_kbs` 逐项 `kb_id / status / reason`；reason 取值
   `STATUS_NOT_ELIGIBLE:<status>` / `UNREGISTERED_KB` / `KB_PROBE_FAILED:<ExcType>` /
   `KB_REGISTRY_QUERY_FAILED:<ExcType>`。
3. 纳入策略：仅 `active` 可探测；`archived/deleting/soft-deleted` 等一切非 active
   状态 omitted 且不探测；`--kb` 未注册 id 记 `UNREGISTERED_KB`；注册表查询失败降级
   为可审计原因，不伪装"零 KB"。
4. `coverage_verdict = FULL/PARTIAL/FAILED`，fail-closed 触发面：目标 KB 读取失败、
   before/after 计数漂移、注册表降级、Milvus 证据降级、§8.5 v3 目标 collection 已存在；
   FAILED 时退出码 2。
5. Milvus collection 只读枚举（仅 `list_collections`）并分类
   `current/target/historical/unknown`；不创建、不修改、不删除。
6. PG（`chunk_revisions/knowledge_bases`）、Neo4j（Chunk 计数）、Milvus（entities）、
   `admin_jobs` before/after 只读快照对账；一致才 `writes=0`，漂移即
   `UNVERIFIED_COUNTER_DRIFT` + FAILED。
7. 输出 `timestamp / commit_sha / environment_id`（如 `admin_db:postgresql`），
   不输出 URI/token/密码。
8. `--kb` 运行标 `scope=SCOPE_SLICE` 且 verdict 封顶 `PARTIAL`，不得表述 FULL。

### 1.2 test：`7179e06` test(admin): cover M5 Gate 0 omissions and failures

- `backend/tests/check_m5_c3_inventory.py`（+81）：追加 verdict 状态机矩阵、
  SCOPE_SLICE 封顶、失败压制成 FAILED、collection 分类、before/after 对账断言。
- `backend/tests/check_m5_gate0_coverage.py`（新增，+497）：SQLite 内存注册表 +
  假 `pymilvus`/`neo4j` 模块 + `build_inventory` 桩，7 个 CLI 级正负场景（见 §3）。

### 1.3 docs：本记录 + 三份文档同步（见 §5）

---

## 2. 验收命令与退出码（隔离环境实跑）

| 命令 | exit code | 关键输出 |
|---|---|---|
| `python -m py_compile backend/admin/report_m5_c3_inventory.py backend/tests/check_m5_c3_inventory.py backend/tests/check_m5_gate0_coverage.py` | 0 | — |
| `python backend/tests/check_m5_c3_inventory.py` | 0 | `C3_INVENTORY_CONTRACT_OK` |
| `python backend/tests/check_m5_gate0_coverage.py` | 0 | `GATE0_COVERAGE_CONTRACT_OK scenarios=7` |
| `git diff --check`（提交前，工作树仅含本轮改动时） | 0 | 无空白错误 |
| `git status --short`（本轮路径范围） | 0 | 仅本轮三个文件，干净 |

测试性质：全部为隔离测试（SQLite 内存库、fake pymilvus/neo4j、stub `build_inventory`、
git rev-parse 打桩）。只证明格式器/状态机/CLI 合同，不代表任何真实服务行为。

---

## 3. 正负场景覆盖矩阵（check_m5_gate0_coverage.py，7/7 通过）

| # | 场景 | 期望 | 实测 |
|---|---|---|---|
| 1 | 正常全覆盖（2 active + 3 非active + 多 collection 无 v3） | `FULL`、`writes=0`、exit 0、非 active 全部 omitted 且从未被探测 | 通过 |
| 2 | `--kb` 局部探测 + 未注册 id | `SCOPE_SLICE`、`UNREGISTERED_KB` omitted、探测齐全仍封顶 `PARTIAL` | 通过 |
| 3 | `--kb` 指向非 active（零可探测） | `eligible=0` → `FAILED`、exit 2 | 通过 |
| 4 | 单 KB 读取异常（另一 KB 成功） | `failed_kbs` 逐项原因、`FAILED` 压过成功、exit 2、`COVERAGE_FAIL_CLOSED` | 通过 |
| 5 | 多 collection 且 §8.5 v3 目标已存在 | `target=[graphinsight_chunks_v3]`、fail-closed `FAILED`、exit 2 | 通过 |
| 6 | before/after 计数漂移 | `writes=UNVERIFIED_COUNTER_DRIFT`、`FAILED`、exit 2、漂移逐项列出 | 通过 |
| 7 | KB 注册表查询失败 | 降级原因可审计（`KB_REGISTRY_QUERY_FAILED`）、`FAILED`、exit 2，不伪装零 KB | 通过 |

附加断言：输出不含 `postgres://`、`bolt://`、`19530`、`password`、`token`；
`environment_id=admin_db:sqlite`；`commit_sha` 来自真实 git rev-parse（测试内打桩为
固定值以保确定性）。

---

## 4. 证据分层声明（审计口径）

1. **隔离测试**：SQLite 内存库 + fake pymilvus/neo4j + stub build_inventory，
   7 场景 + formatter 契约全绿（exit 0）。仅证明代码合同与状态机。
2. **真机取证**：**未取证**。本轮未连接任何真实 PostgreSQL/Neo4j/Milvus 运行报告；
   共享 dev 的 `report_m5_c3_inventory.py` 实跑未执行（任务明确禁止）。
3. **生产闭环**：**未达成**。共享生产 Gate 0 保持 OPEN；`shared_production_gate=OPEN`
   为报告固定输出；§5.1 canary runner、C3 处置、授权流程均未满足。

---

## 5. 文档同步（`docs` 提交）

- `docs/ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md`：新增 §7.1 Gate 0 全量覆盖证据合同
  （实现内容 + 隔离边界声明）。
- `docs/ENTERPRISE_ROADMAP_CHECKLIST.md`：M5 快照追加 2026-10-04 合同实现条目。
- `docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md`：M5-B0 验收进展追加第 7 条；
  M5-B0-C3 验收标准补 `coverage_verdict=FULL` / `writes=0` 要求。
- 本验收记录：`docs/ENTERPRISE_M5_GATE0_COVERAGE_ACCEPTANCE_2026-10-04.md`。

---

## 6. 已知风险与未验证边界

1. **未验证共享真实服务**：Milvus `list_collections`/`get_collection_stats` 与 Neo4j
   count 查询仅经 fake 驱动验证过合同，真实实例上的分页/权限/统计延迟行为未取证。
2. **concurrent-worktree 协调**：本轮作业期间发现另一个会话在同一主工作树活跃提交
   （`m5/dual-write`，如 `174d7df`），且本会话早期对该报告器的两次编辑曾被并发切换回退。
   已改用独立 `git worktree`（`E:/projects/GraphInsight-gate0`）完成本轮交付，主工作树
   已还原干净，未触碰对方分支。此外，docs 提交后在本 worktree 观察到约 29 个
   `frontend/src/**` 文件被第三方并发编辑（视频节点/图谱渲染相关真实内容改动，非本任务
   范围，未提交、未包含在本轮任何提交中）；该分支若被他人继续提交，需审计方注意区分。
3. **统一门禁未注册**：两个 check 脚本未加入 `run_unified_boundary_guards.py`
   （59332f4 基线上前一轮 C3 契约也未注册；保持最小交付面，是否注册待用户决定）。
4. **fail-closed 语义扩展**：除任务明确要求的"KB 读取失败"外，本实现把计数漂移/
   注册表降级/Milvus 证据降级/v3 目标已存在也纳入 FAILED 判定，属从严解释，已在
   文档写明；如需放宽为"仅读取失败"，改动点集中在 `coverage_fail_closed` 计算。
5. **非 PG 豁免口**：`C3_INVENTORY_ALLOW_NON_PG=1` 仅为隔离测试打开 SQLite 通道；
   共享生产证据采集禁止设置该变量（已在代码注释与本记录声明）。
6. **提交拆分偏差**：验收报告因 `.gitignore` 的 `docs/*` 白名单机制未能进入首个 docs
   提交（ceae362），补白名单后以第四笔提交 `docs(enterprise): add Gate 0 acceptance
   record` 单独入库，任务规定的三笔拆分主体不变。

## 7. Push 与共享资源触碰声明

- 是否 push：**否**（分支 `audit/m5-gate0-completion` 仅存在于本地 worktree）。
- 是否触碰共享 dev / 共享 v2 / 共享 v3：**全部否**（无网络连接证据源，
  测试全程离线夹具）。
- 是否启动 worker / backfill / 入队：**否**。
