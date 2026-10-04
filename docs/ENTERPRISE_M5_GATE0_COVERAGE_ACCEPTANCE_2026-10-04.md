# M5 Gate 0 全量覆盖证据合同验收记录（2026-10-04，含审计修订 2）

任务：`audit/m5-gate0-completion`（基线 `origin/main=59332f4`）
执行边界：只实现 + 隔离测试。**未连接、未写入共享 dev；未修改共享 v2；未创建共享 v3；
未运行 backfill、未入队任务、未启动 worker；未启动 M5-B1；未 push。**

结论口径：**Gate 0 contract implementation complete（隔离层）。**
共享生产 Gate 0 **不**宣布 CLOSED，仍为 OPEN。

> **审计状态**：第一版（提交 `343dd10`..`acbc66c`）经审核**未通过**，审核意见指出计数
> 读取错误被折算为 0、fail-closed 表述过度、worktree 清洁状态表述过度等问题。本记录
> 覆盖按审核意见完成的整改（见 §1.4），整改后仍为隔离层，不改变 Gate 0 = OPEN。

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
  假 `pymilvus`/`neo4j` 模块 + `build_inventory` 桩，**第一版为 7 个 CLI 级正负场景**
  （历史口径；现行为 13 个场景，见 §3 与 §1.4 修订 3）。

### 1.3 docs：`ceae362` + `acbc66c`（三份文档同步 + 本记录）

### 1.4 审计修订 2（本记录对应的三笔整改提交）

审核未通过意见及整改落点：

| # | 审核意见 | 整改落点 |
|---|---|---|
| 1 | 计数读取错误折算 0 | 五类证据源（PG `chunk_revisions/knowledge_bases/admin_jobs`、Neo4j、Milvus）全部改为逐源独立 `value/ok/reason`；失败源 `value=None`，权限/连接/SQL/缺表/统计不可读均为显式 reason |
| 2 | degraded reason 必须 FAILED/exit2/UNVERIFIED_EVIDENCE | before/after 快照任一 `ok=False` 即 `writes=UNVERIFIED_EVIDENCE` + `FAILED` + exit 2（漂移仍记 `UNVERIFIED_COUNTER_DRIFT` 并列出） |
| 3 | resolved current collection 不存在必须 FAILED | `current_missing=true` → `coverage_degraded` → FAILED |
| 4 | raw 与 resolved collection 分离 | `configured_raw`（legacy 原始配置名）与 `resolved`（§11.1/D3 归一化名）分别报告；`current` 分类只按 resolved 判定 |
| 5 | 显式 environment_id，PG 缺失拒 FULL | 新增 `GRAPHINSIGHT_ENVIRONMENT_ID`（非敏感字符集清洗）；PG 正式运行缺失 → `environment_id_missing=true` → fail-closed 拒 FULL |
| 6 | 四源失败且前后均为 0 的负向测试 | 场景 8-11：PG chunk_revisions / admin_jobs / Neo4j / Milvus stats 分别注入失败，断言前后 value 均为 None（等价旧实现的 0/0 通过假象不再存在）且 FAILED + `UNVERIFIED_EVIDENCE` |
| 7 | active KB 带 scope_unresolved 仍被探测 | 场景 12：探测序列包含该 KB、`counts.scope_unresolved=1`、`c3.scope_unresolved` 带 chunk 级原因、`c3_totals` 计入，verdict 不受影响 |
| 8 | 修正过度表述 | 迁移计划 §7.1 重写为"枚举化 fail-closed"（废除"所有异常均 fail-closed"）；本记录 §6.2 修正 worktree 状态表述 |

整改性质：同分支追加提交，仍不连接任何真实服务。

### 1.5 审计修订 3（"条件通过"复核意见整改）

第二轮复核裁定"代码整改基本通过、隔离验收通过、文档与复核指引条件通过"，两点修正：

| # | 复核意见 | 整改落点 |
|---|---|---|
| P1 | environment_id 清洗策略可能泄露敏感片段（删除非法字符会把 URI 凭据片段拼接进输出） | `_environment_id_sanitized` 清洗策略废除，改为**严格白名单** `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$`：空格/斜杠/`@`/控制字符等非法输入整体拒绝，记 `environment_id_invalid=true` 并 fail-closed（FAILED + exit 2 + `UNVERIFIED_EVIDENCE`），绝无"清洗后输出"；失败原因只含类别（`ENVIRONMENT_ID_INVALID: value rejected by strict allowlist`），不含原值；新增 CLI 级场景 13 断言 `postgres://user:pass@host` 注入后凭据片段零泄露 |
| P2 | 复核指引声称"所有异常均"grep 应无匹配不成立（文档在"废除/修正"语境中合法引用该历史表述）；Backlog 第一版条目仍写"7 个正负场景"易误读 | 复核指引修正为"肯定式句型排除法"（见 §5.1）：命中行必须全部处于废除/修正语境，任何肯定式使用即不通过；Backlog 第 7 条标注**[历史版本]**，本记录 §1.2 同步标注第一版 7 场景为历史口径，现行为 13 场景 |

---

## 2. 验收命令与退出码（隔离环境实跑，修订 3 整改后）

| 命令 | exit code | 关键输出 |
|---|---|---|
| `python -m py_compile backend/admin/report_m5_c3_inventory.py backend/tests/check_m5_c3_inventory.py backend/tests/check_m5_gate0_coverage.py` | 0 | — |
| `python backend/tests/check_m5_c3_inventory.py` | 0 | `C3_INVENTORY_CONTRACT_OK` |
| `python backend/tests/check_m5_gate0_coverage.py` | 0 | `GATE0_COVERAGE_CONTRACT_OK scenarios=13` |
| `git diff --check`（提交前，本轮路径范围） | 0 | 无空白错误 |
| `git status --short`（本轮路径范围 `backend docs .gitignore`） | 0 | 仅本轮改动文件 |

测试性质：全部为隔离测试（SQLite 内存库、fake pymilvus/neo4j、stub `build_inventory`、
git rev-parse 打桩）。只证明格式器/状态机/CLI 合同，不代表任何真实服务行为。

---

## 3. 正负场景覆盖矩阵（check_m5_gate0_coverage.py，13/13 通过）

| # | 场景 | 期望 | 实测 |
|---|---|---|---|
| 1 | 正常全覆盖（2 active + 3 非active + 多 collection 无 v3） | `FULL`、`writes=0`、exit 0、五类证据源 `value/ok/reason` 全真、before==after、非 active 全部 omitted 且从未被探测 | 通过 |
| 2 | `--kb` 局部探测 + 未注册 id | `SCOPE_SLICE`、`UNREGISTERED_KB` omitted、探测齐全仍封顶 `PARTIAL` | 通过 |
| 3 | `--kb` 指向非 active（零可探测） | `eligible=0` → `FAILED`、exit 2 | 通过 |
| 4 | 单 KB 读取异常（另一 KB 成功） | `failed_kbs` 逐项原因、`FAILED` 压过成功、exit 2、`COVERAGE_FAIL_CLOSED` | 通过 |
| 5 | 多 collection 且 §8.5 v3 目标已存在 | `target=[graphinsight_chunks_v3]`、fail-closed `FAILED`、exit 2 | 通过 |
| 6 | before/after 计数漂移（3→4） | `writes=UNVERIFIED_COUNTER_DRIFT`、`FAILED`、exit 2、漂移逐项列出 | 通过 |
| 7 | KB 注册表查询失败 | 降级原因可审计（`KB_REGISTRY_QUERY_FAILED`）、`FAILED`、exit 2，不伪装零 KB | 通过 |
| 8 | PG `chunk_revisions` 读取失败（前后值等价旧实现的 0） | `value=None`+reason、`writes=UNVERIFIED_EVIDENCE`、`FAILED`、exit 2 | 通过 |
| 9 | `admin_jobs` 读取失败（同上等价 0） | 同上 fail-closed | 通过 |
| 10 | Neo4j 计数失败（同上等价 0） | 同上 fail-closed | 通过 |
| 11 | Milvus stats 不可读（同上等价 0） | 同上 fail-closed | 通过 |
| 12 | active KB 带 scope_unresolved | 仍被探测并进入 `C3_REPORT`（`counts.scope_unresolved=1`、chunk 级原因）、`c3_totals` 计入、verdict 不变 | 通过 |
| 13 | environment_id 非法值（`postgres://user:pass@host`） | 整体拒绝为 INVALID、`environment_id_invalid=true`、FAILED + exit 2 + `UNVERIFIED_EVIDENCE`、凭据片段零泄露、失败原因不含原值 | 通过 |

附加断言：输出不含 `postgres://`、`bolt://`、`19530`、`password`、`token`；
`environment_id` 取自夹具显式注入（`admin_db:sqlite` 等价语义）；`commit_sha` 来自真实
git rev-parse（测试内打桩为固定值以保确定性）；场景 1 额外断言
`configured_raw=graphinsight_chunks`（legacy）≠ `resolved=graphinsight_chunks_v2`
（归一化），current 分类只按 resolved 判定且 `current_missing=false`。

---

## 4. 证据分层声明（审计口径）

1. **隔离测试**：SQLite 内存库 + fake pymilvus/neo4j + stub build_inventory，
   13 场景 + formatter 契约全绿（exit 0）。仅证明代码合同与状态机。
2. **真机取证**：**未取证**。本轮未连接任何真实 PostgreSQL/Neo4j/Milvus 运行报告；
   共享 dev 的 `report_m5_c3_inventory.py` 实跑未执行（任务明确禁止）。
3. **生产闭环**：**未达成**。共享生产 Gate 0 保持 OPEN；`shared_production_gate=OPEN`
   为报告固定输出；§5.1 canary runner、C3 处置、授权流程均未满足。

---

## 5. 文档同步（`docs` 提交）

- `docs/ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md`：§7.1 重写为审计修订版合同
  （value/ok/reason、枚举化 fail-closed、raw/resolved 分离、environment_id 门禁），
  明确废除"所有异常均 fail-closed"的全称表述。
- `docs/ENTERPRISE_ROADMAP_CHECKLIST.md`：追加审计修订 2 条目（12 场景、整改内容）。
- `docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md`：M5-B0 验收进展追加第 8 条修订记录。
- 本验收记录：修正过度表述并登记审核退回与整改闭环。

### 5.1 独立复核指引（修订 3 修正版）

第一轮汇报中"`git grep -n "所有异常均" -- docs/` 应无匹配"的预期**不成立**：整改后的
文档在"废除/修正/过度表述"语境中会合法引用该历史表述。正确复核方式为**肯定式句型
排除法**——检查是否存在把"所有异常均 fail-closed"当作事实主张的用法：

```
git grep -n "所有异常均" <rev> -- docs/ | grep -v "废除\|修正\|过度表述\|不得\|历史"
```

期望：**无输出**（exit 1）。若仍有命中，说明存在肯定式使用，复核不通过。
当前 HEAD `e823669` 之后按此模式复核通过。

其余复核命令不变：

```
git log --oneline -7 <rev>
python backend/tests/check_m5_gate0_coverage.py   # 期望 scenarios=13，exit 0
git grep -n "_ENVIRONMENT_ID_PATTERN\|environment_id_invalid" <rev> -- backend/admin/report_m5_c3_inventory.py
```

场景数口径提示：`343dd10..acbc66c` 为第一版 7 场景（历史），`d423f65..7b6cbcc` 为
12 场景（修订 2），environment_id 严格校验提交后为 13 场景（修订 3）。以 HEAD 为准。

---

## 6. 已知风险与未验证边界

### 6.1 未验证边界

1. **未验证共享真实服务**：Milvus `list_collections`/`get_collection_stats` 与 Neo4j
   count 查询仅经 fake 驱动验证过合同，真实实例上的分页/权限/统计延迟行为未取证。
2. **统一门禁未注册**：两个 check 脚本未加入 `run_unified_boundary_guards.py`
   （59332f4 基线上前一轮 C3 契约也未注册；保持最小交付面，是否注册待用户决定）。
3. **非 PG 豁免口**：`C3_INVENTORY_ALLOW_NON_PG=1` 仅为隔离测试打开 SQLite 通道；
   共享生产证据采集禁止设置该变量（已在代码注释与本记录声明）。
4. **提交拆分偏差（历史）**：第一版验收报告因 `.gitignore` 的 `docs/*` 白名单机制
   未能进入首个 docs 提交（ceae362），补白名单后以第四笔提交 `acbc66c` 单独入库。

### 6.2 状态表述修正（对应审核意见 #8）

1. **"所有异常均 fail-closed"表述废除**：实际合同为**枚举式** fail-closed（§1.4 表
   列出的七类触发面）。未列入枚举的异常路径（例如探测循环之外的意外崩溃）按普通错误
   传播处理，不在本合同担保范围内。第一版文档的"所有异常均 fail-closed"为过度表述，
   已在迁移计划 §7.1 与本记录同步修正。
2. **worktree 状态表述修正**：第一版称"主工作树已还原干净"，为过度表述。事实为：
   本轮在主工作树触碰过的 `backend/config.py` CRLF 行尾伪差异已还原；但主工作树整体
   由 `m5/dual-write` 会话持续活跃提交（如 `174d7df`），其完整状态未由本会话清点，
   不做"干净"断言。gate0 worktree 内约 29 个 `frontend/src/**` 第三方并发编辑
   （视频节点/图谱渲染相关，未提交、非本任务范围）保持原样披露，未纳入本轮任何提交。
3. **fail-closed 语义扩展（历史风险，已转为合同明文）**：第一版把计数漂移/注册表
   降级/Milvus 证据降级/v3 目标已存在纳入 FAILED 判定属从严解释；审计修订 2 后该
   触发面连同四类新条件一起在 §7.1 明文枚举，不再是隐式行为。

## 7. Push 与共享资源触碰声明

- 是否 push：**否**（分支 `audit/m5-gate0-completion` 仅存在于本地 worktree）。
- 是否触碰共享 dev / 共享 v2 / 共享 v3：**全部否**（无网络连接证据源，
  测试全程离线夹具）。
- 是否启动 worker / backfill / 入队：**否**。
- 是否进入 dual-write 下一阶段 / 启动 M5-B1：**否**（合并冻结中）。
