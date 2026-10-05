# M5 共享 Milvus v3 迁移方案（仅设计，零执行）

状态：**DESIGN ONLY / ZERO WRITES**（2026-10-03）
适用仓库：`E:\projects\GraphInsight`（与 aistudio 无关）
上游权威：`docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` §8.5 / §14 / §15.4 / §16.1 / §16.2
执行边界：本文件不触发任何 Milvus/Neo4j/DB 写入，不切配置，不建/删 collection，
不跑 canary。所有写步骤标 `GATED / NOT-EXECUTED`，须逐项显式授权后才可执行。

---

## 0. 一句话结论

真实 v3 迁移曾以**当前代码不存在的能力** `dual_write` 为代码级阻塞项；该能力已于
2026-10-05 在本地 main 落地（默认关闭，见 §9.1 落地记录）。§16.1 的 S1（同一生产写
同时落 v2+v3）在代码里已有实现路径，但**真实 v3 迁移仍然关闭**：§9 阻塞清单其余各项
（授权、C3 处置、验收脚本）未满足前，禁止在共享环境开启 `dual_write`、禁止对任何真实
KB 执行 v3 迁移、禁止宣称 canary 闭环。本方案只把"现在能做的"（隔离的
单目标 schema 建集合验证）与"必须授权后才能做的"（双写/切换）分开摆明，并给出各自的
验收判据与门禁。

---

## 1. 代码现状核对（evidence，非假设）

以下均为本次逐行核对 `backend/services/vector_store.py` 所得，用于防止推荐不存在的能力：

| 事实 | 位置 | 含义 |
|---|---|---|
| 新建 collection 的 schema 已含显式 `content_revision` `INT64` | `vector_store.py:215` | 把 `vector_store.collection` 指向一个**全新名**时，当前代码会自动建出带显式 INT64 字段的 collection，无需新代码。 |
| `content_revision_field_is_int64(client, collection)` | `vector_store.py:34` | 真实的字段判据 helper，可用于校验 v3 字段类型。 |
| `has_content_revision_field()` | `vector_store.py:283` | 运行期探测某 collection 是否有显式 §8.5 字段（结果按名缓存，探测失败按"不支持"→拒写）。 |
| legacy 名归一化 `graphinsight_chunks → _v2` | `vector_store.py:134-140` | 配置为空或旧名一律落到 `graphinsight_chunks_v2`，即当前默认读/写目标是 v2。 |
| 写保护：有版本意图但缺显式字段 → 拒写 | `vector_store.py:249-257`（`VectorStoreSchemaError`，文案"…graphinsight_chunks_v3 后重建向量"） | §8.5 冻结：宁可拒写也不把 revision 降级进 dynamic metadata。 |
| `dual_write`（2026-10-05 更新） | `config.py:139`、`runtime_config.py:165`、`vector_store.py:145-159/268-336` | §16.1 S1 双写已在代码中实现（默认关闭；shadow 目标锁定 v3，仅 primary=v2 时生效）。自动化验证：`tests/check_vector_dual_write.py`（23 断言，统一门禁 `vector_dual_write` 项）。真实启用仍属 §9 授权范围。 |

结论性判定（2026-10-05 更新）：**v3 集合的"建立 + 单目标写入 + 字段判据"当前代码即可支撑；
"同一写扇出到 v2 与 v3 两个 collection"已由 S1 双写实现（默认关闭），真实启用见 §9 门禁。**

---

## 2. 为什么不能"直接开 v3"（不变量）

§16.1 明令禁止的状态：**v3 写 + v2 读**（写新读旧 → 检索永远看不到新 revision，静默数据
错位）。要把读源从 v2 原子切到 v3，必须在切换前后保证读写同源。没有 `dual_write` 时：

- 若直接切配置到 v3：v3 里没有存量向量 → 检索大面积空命中（数据丢失假象）。
- 若先逐 KB 重建 v3 再切：重建窗口内该 KB 读 v2 写 v3 = 违反读写同源不变量。
- 若边写 v2 边补 v3：这正是 `dual_write`，而它未实现。

所以 S1→S2→S3 这条主线**以 `dual_write` 为前置**。§15.4 的 A/B/C/D 已被 §16.1 状态机
覆盖，本方案一律以 §16.1 为准。

---

## 3. 目标集合（v3）冻结契约 — §8.5

新集合 `graphinsight_chunks_v3` 的 schema 固定为（与 `vector_store.py:203-216` 一致）：

- 主键 `chunk_id VARCHAR(128)`；
- 作用域：`doc_id/kb_id/tenant_id/project_id`；
- 文本/元数据：`text(4096)/title(512)/location/content_hash/embedding_model/entities_json`；
- **`content_revision INT64`（显式字段，NOT 依赖 dynamic metadata）**；
- `vector FLOAT_VECTOR(dim)`，`index_type/metric_type` 沿用 `cfg`（HNSW/IP 以现网为准）。

铁律：
1. **绝不 drop、绝不 ALTER v2**。v2 是回滚落脚点，全程原样保留。
2. v3 一旦建出，用 `content_revision_field_is_int64` 复核字段类型为 INT64，非 int64 视为
   schema 不合格，禁止进入下一步。
3. 任何写入路径若 `has_content_revision_field()` 为假，必须沿用 §8.5 拒写，不得降级。

---

## 4. 读/写状态机（§16.1）与每步门禁

```
S0  现状：读写皆 v2（默认，安全）           —— 无需授权，已是当前态
S1  双写：写扇出 v2+v3，读仍 v2（v3 影子）   —— GATED，阻塞项：dual_write 未实现
S2  切换：原子把读源 v2→v3 + 重启           —— GATED，前置 S1 稳定且校验通过
S3  稳态：读写皆 v3，v2 只读留存备回滚       —— GATED
```

- **S0→S1 的前置不是配置开关，而是代码**。必须先实现 `dual_write`：在一次 upsert 内
  同时写 v2 与 v3，且任一失败要能定位并保持投影未收敛（与 §8.5 拒写语义一致）。在
  `dual_write` 存在且有自动化验证之前，**本步不存在，不得尝试用配置绕过**。
- **S2 切换**要求 v3 与 v2 的 per-KB 计数与抽样 revision 校验通过（见 §6）。
- **S2 之后回滚 = 五步**（§16.1，覆盖 §15.4）：
  1. 写冻结（对受影响 KB 返回 `503 INDEX_UNAVAILABLE`，停止新写）；
  2. 回放 v3→v2 幂等 upsert（把切读期间落 v3 的增量补回 v2）；
  3. 逐 `(chunk_id, revision, content_hash)` 权威清单对账 v2↔v3（计数相等仅旁证，见 §6）；
  4. 读源切回 v2 配置 + 重启；
  5. 解除写冻结。
  回滚只动配置与读源，**不删 v3**。

每步都写清责任方与判据；未达判据即停在当前态，不推进。

---

## 5. 两类 canary 的范围与可行性（不得混谈）

### 5.1 §8.5 单目标 schema canary（canary runner 尚未实现，GATED）
目的：在**隔离的合成 collection**上证明——v3 schema 能被建出、`content_revision` 是显式 INT64、
upsert→直接读回 revision 正确、幂等重跑不翻倍。

- **当前代码没有按实例注入 collection 的能力（作废旧表述）**：`MilvusVectorStore.__init__(self)`
  （`backend/services/vector_store.py:122`）**无参**，collection 一律在构造时由
  `get_vector_store_runtime_config()` 解析；该解析里配置中心优先——
  `_first_non_empty(loaded.get("collection"), settings.milvus_collection)`
  （`backend/services/runtime_config.py:161`），即**配置中心的值压过 env/settings**。
  因此"给 `VectorStore(...)` 传一个合成 collection 名"这一表述**与代码不符，作废**；
  单纯设 env 变量在配置中心已写入 `collection` 时也会被忽略，不是可靠隔离。
- **canary runner 尚未实现（NOT-IMPLEMENTED / GATED）**：真实的隔离注入需要一段专用一次性
  脚本**绕开** `MilvusVectorStore` 单例与配置中心——用独立持有的 pymilvus client、自带一份
  指向合成 `uri` + 合成 collection 名（如 `graphinsight_chunks_v3_canary_<rand>`）的连接配置，
  直接 `create_collection`/`upsert`；或给构造/配置读取新增一条**仅在该独立进程内生效**的显式
  覆盖入口（当前代码不存在此入口）。在该 runner 落地并自证"运行前后共享 config 文件与生产
  collection 字节级零变化"之前，5.1 **只能标 `NOT-IMPLEMENTED`，不得称"当前代码可做"**。
- 硬约束（runner 实现时必须满足）：全程绝不 flip 现网服务的 `vector_store.collection`、绝不热改
  配置中心、绝不改任何被生产读写的单例；合成 collection 不与生产 collection 同名、无生产读流量
  指向；事后只 drop 合成 collection，绝不碰 v2 / 生产 v3 目标。
- 它**不**证明双写、不证明切换安全——只是"独立进程里建集合与字段契约"的冒烟。

### 5.2 双写 / 切换 canary（被阻塞，禁止宣称闭环）
目的：证明 S1 双写一致性与 S2 切换后检索无损。
- **依赖 `dual_write`，当前不存在。** 在实现并验证前，5.2 无法运行，也**不得**以任何形式
  宣称"canary 已闭环/迁移可回滚"。
- 5.1 通过也**不能**外推为 5.2 通过——两者证明的不变量不同。

---

## 6. 验收判据（写冻结 + 权威清单逐项对账，per-KB）

对每个真实 KB（迁移窗口内，非合成 canary），逐 KB 记录：

- 计数一致性（**旁证，非主判据**）：冻结态下 `v2_count == v3_count`（chunk 级计数，按 `kb_id`
  过滤，非全库总量）。计数相等只说明"条数对得上"，**不能**证明逐条版本/内容一致——主判据见下方
  "写冻结 + 权威清单逐项对账"。
- 抽样 N 条 `chunk_id`：v3 的 `content_revision` 为 int64 且等于该 chunk 投影版本；向量
  维度与 v2 一致。
- 幂等：同一批次 upsert 重跑，v3_count 不增、无重复主键。
- 直接读回（Neo4j/Milvus 双侧）：QA citation 的 current/indexed revision 语义与 §11 对齐，
  无 stale 命中回升。
- **对账口径改写**（替换"v2 零改动"）：真实 KB 在 S1 双写窗口内 v2 **本就持续被写**，"v2 前后
  count/revision 不变"不是有效判据、会自相矛盾。改用**写冻结 + 权威清单逐项对账**：
  - **`max(content_revision)` 不是有效水位（作废）**：`content_revision` 是**各 chunk 独立递增**的
    版本号，全局/单 KB 的最大值只反映"改得最多的那一个 chunk"，**无法反映其余 chunk 之后是否又
    变过**；用 `W0 = max(...)`、`W1 = max(...)` 做区间对账会漏掉"最大值不变但别的 chunk 已前滚"
    的情形。故不再用标量水位表述冻结点。
  - 切读前对每个 KB 施加**写冻结**（§16.1 回滚五步里的同一步），冻结生效后，读取该 KB 的
    **完整权威清单** `M = sorted([(chunk_id, current_revision, content_hash), ...])`（按 `chunk_id`
    排序，逐 chunk 快照，而不是取任何聚合值）。
  - **逐项对账** v3 与 `M`：对 `M` 里每一条 `chunk_id`，v3 中该 chunk 必须存在，且
    `v3.content_revision == current_revision` **且** `v3.content_hash == content_hash`；两侧
    `chunk_id` 集合必须**完全相等**（无 v3 缺、无 v3 多）。`v3_count == len(M)` 只作旁证，**主判据
    是逐 `(chunk_id, revision, content_hash)` 三元组相等**，不是计数相等。
  - **回滚点校验**：v2 只需在冻结态被验证"能读到 `M` 中每个 `chunk_id` 截至冻结的投影"，**不比对
    冻结前后 count/最大值是否相等**——count/标量水位比对都无意义。
  - 解冻后新增 revision 由 `dual_write` 保证 v2/v3 同步；同步正确性同样用"重新冻结→取新 `M`→
    逐项对账"复验，而非任何 `max(revision)` 断言。
- 残留检查：`scope_mismatches / orphan_revisions / unrecoverable / scope_unresolved` 归零或
  进入 §7 的显式处置清单，不得静默吸收。
- 每个写步骤执行前后各打印一次 C3 全状态汇总（`C3_SUMMARY`，含 by_status/c3_totals/
  needs_reindex_total），作为"该 KB 在迁移态下的库存快照"。

---

## 7. per-KB 处置清单（来自 C3 全状态只读清单，迁移前置）

迁移前必须以只读 C3 清单（`backend/admin/report_m5_c3_inventory.py`）产出处置表，逐 KB 列：
`kb_id / kb_status(active|archived|deleting|unregistered) / needs_reindex / blocked /
orphan_revisions / unrecoverable / scope_unresolved / scope_mismatches / 迁移处置`。

- `archived/deleting`：默认不迁（无读流量），仅在确认有恢复需求时单列。
- `blocked/orphan/unrecoverable/scope_*` 非零：属人工数据治理（§12/reindex 不能解决），
  必须**先处置后迁移**，否则把脏投影复制进 v3 只是把问题搬家。
- 该清单是"迁移前置门禁"的数据来源，产出动作本身是只读的。

### 7.1 Gate 0 全量覆盖证据合同（2026-10-04 实现并经审计修订，仅隔离层）

`backend/admin/report_m5_c3_inventory.py` 已从"逐 KB C3 输出"升级为带全量覆盖证据的
gate 报告，支撑 §7 处置清单作为迁移前置门禁的可审计性。以下为**审计修订 2 后**的合同
（第一版把计数读取错误折算为 0、并使用~~"所有异常均 fail-closed"~~的过度表述，均已按审计
意见纠正）：

1. **覆盖计数**：`registered_kb_total / active_kb_total / eligible_kb_total /
   probed_kb_total / omitted_kb_total / failed_kb_total`，输出在 `C3_SUMMARY` 与
   `GATE0_COVERAGE` 行。
2. **逐项原因**：`omitted_kbs / failed_kbs` 每项含 `kb_id / status / reason`
   （`STATUS_NOT_ELIGIBLE:<status>`、`UNREGISTERED_KB`、`KB_PROBE_FAILED:<ExcType>`、
   `KB_REGISTRY_QUERY_FAILED:<ExcType>`）。
3. **纳入策略**：仅 `active` KB 可探测（eligible）；`archived/deleting/soft-deleted`
   等一切非 active 状态一律 omitted 且**绝不探测**；`--kb` 显式指定但注册表无行的
   id 记 `UNREGISTERED_KB`；注册表查询失败降级为可审计原因，绝不伪装成"零 KB 注册"。
4. **显式计数合同（审计修订核心）**：全部证据源（PG `chunk_revisions/knowledge_bases/
   admin_jobs`、Neo4j Chunk 计数、Milvus entities）每源独立返回 `value/ok/reason` 三元组；
   权限不足、连接失败、SQL 错误、缺表、统计不可读一律记录为显式 reason 且 `value=None`，
   **绝不折算为 0**。任何源失败时 before/after 值不存在等价 0 的假象。
5. **覆盖判定（fail-closed 枚举，非全称表述）**：`coverage_verdict = FULL / PARTIAL /
   FAILED`。以下条件**逐一枚举**，任一命中即 `FAILED` 且退出码 2，不得宣称全量覆盖成功：
   目标 KB 读取失败、注册表查询失败、before/after 任一快照存在 degraded reason（含
   单源读取失败、current collection 缺失、Milvus 证据降级）、计数漂移、§8.5 v3 目标
   collection 已存在、PostgreSQL 正式运行缺失显式 environment_id。除此之外的异常路径
   不在本合同担保范围内，不使用~~"所有异常均 fail-closed"~~的表述。
6. **collection 只读枚举与名称分离（审计修订）**：`configured_raw`（未归一化的原始
   配置名，可为 legacy 名）与 `resolved`（§11.1/D3 归一化后的现读写名）分别报告；
   `current` 分类只按 `resolved` 判定。`resolved` 名在实例上不存在记
   `current_missing=true` 并 fail-closed。`list_collections` 只读，绝不创建、修改或删除。
7. **零写对账**：五类证据源在探测前后各快照一次；值漂移记 `writes=UNVERIFIED_COUNTER_
   DRIFT`；快照内任何 degraded reason 记 `writes=UNVERIFIED_EVIDENCE`——两者均 fail-closed。
8. **环境标识（审计修订 3：严格校验；修订 4：原始值不裁剪）**：输出
   `timestamp / commit_sha / environment_id`；
   `environment_id` 取自 `GRAPHINSIGHT_ENVIRONMENT_ID`，按白名单
   `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$` **整体校验**——空格/斜杠/`@`/控制字符等非法输入
   直接拒绝为 `environment_id_invalid` 并 fail-closed（FAILED + exit 2 +
   `UNVERIFIED_EVIDENCE`），**不做"清洗后拼接输出"**，杜绝
   `postgres://user:pass@host` 类凭据片段经部分剥离泄露；失败原因只含类别、不含原值。
   全程不输出 URI/token/密码。PostgreSQL 正式运行缺失该变量时拒绝产出 FULL（fail-closed）。
   审计修订 4：**原始值直接匹配、全路径无 strip**——仅原始值为空（未设置或空串）记
   MISSING；含任何前导/尾随/内部非法字符（含空白与控制字符）一律 INVALID，
   `" prod"`、`"prod "`、`"\tprod"`、`"prod\n"`、`"   "` 等填充输入不得被静默裁剪后放行
   （CLI 级场景 14 逐值钉死）。
9. **局部探测**：`--kb` 运行标 `scope=SCOPE_SLICE`，verdict 封顶 `PARTIAL`，
   永远不得表述为 `FULL`。

隔离验证：`backend/tests/check_m5_c3_inventory.py`（formatter/verdict/分类/对账/
value-ok-reason/环境标识契约）与 `backend/tests/check_m5_gate0_coverage.py`（SQLite +
假 pymilvus/neo4j 的 CLI 正负场景 14 个：全覆盖、状态排除、缺 scope、单 KB 读取异常、
多 collection、计数漂移、注册表降级、**PG chunk_revisions/admin_jobs/Neo4j/Milvus
四源分别失败且前后值等价 0 的负向矩阵**、active KB 带 scope_unresolved 仍被探测并进入
C3 报告、environment_id 非法值与填充空白 fail-closed）。

**边界声明：以上仅为隔离测试证明的合同实现（Gate 0 contract implementation
complete）。共享生产 Gate 0 仍未 CLOSED——共享证据采集仍须在授权后于真实
PostgreSQL/Neo4j/Milvus 上运行本报告，且 §5.1 canary runner、§7 处置、§9 其余门禁
照旧未满足。**

---

## 8. 明确不做（本方案与本轮的边界）

- 不写任何 v3/生产数据、不切配置、不跑 canary。（`dual_write` 的**代码实现**已于
  2026-10-05 落地并默认关闭，见 §9.1 落地记录；本轮不产生真实 v3 数据。）
- 不开 M5-B1，不共享 v3 写入。
- 不推荐"现在就设 `milvus.dual_write=true`"——真实启用属 §9 授权范围；在共享环境显式
  开启即进入 S1，需配套写冻结与回滚预案（§16.1）。
- 历史轮边界存档：2026-10-04 审计轮"不 push、不动 main、提交只落
  `audit/m5-gate0-coverage`"仅描述该轮边界；当前集成轮按用户指示在本地 main 提交，
  仍然不 push。

---

## 9. 进入执行前必须先满足的条件（阻塞清单）

1. ✅ `dual_write` 在后端实现并带自动化验证（S1 前置，责任：后端）——**已满足**（2026-10-05）：
   代码见 §9.1；验证 `backend/tests/check_vector_dual_write.py` 23/23 PASS，已注册统一门禁
   `vector_dual_write`（套件 `SUMMARY total=17 failed=0`，EXIT=0）。
2. 授权运行 §5.1 合成 schema canary（写 Milvus，需单独批准）。
3. C3 处置清单（§7）中所有待迁 KB 的 blocked/orphan/unrecoverable/scope_* 归零或有批准
   的例外。
4. 双写一致性 + 切换 + 回滚五步的验收脚本就绪（§6 判据落到可执行断言）。

以上任一未满足，真实 v3 迁移保持关闭，M5 gate 不宣布通过。

---

### 9.1 实现落地记录（2026-10-05 · S1 双写最小移植轮，integration worktree，本地 main）

本节只记录**代码事实**（均可 `git`/文件复查），不改变 §4–§8 的任何门禁判定：真实 v3 迁移
仍关闭、`MILVUS_DUAL_WRITE` 默认 false、共享环境未做任何配置变更或连接。

| 项 | 落点（file:line，均为现树实测） | 语义 |
|---|---|---|
| 配置开关 | `backend/config.py:139` | `MILVUS_DUAL_WRITE`（默认 false）布尔解析；后台展示经 runtime_config vector_store 类目透出（AGENTS.md 后端规范 9） |
| 运行时透传 | `backend/services/runtime_config.py:165` | `dual_write` 键进入 vector_store 运行时配置（配置中心优先，settings 兜底） |
| 切换点显式化 | `backend/services/vector_store.py:145-159` | dual_write 仅在 primary=`graphinsight_chunks_v2` 时产生 shadow=`graphinsight_chunks_v3`；primary 非 v2 时 warning 且不生效（S2 形态）——"只写 v3 + 只读 v2"结构性不可能（§16.1 不变量） |
| S1 写扇出 | `vector_store.py:268-336` | primary（v2）先写；带版本 chunk 且 v2 缺显式字段时**剥版本写 v2、版本由 v3 承载**（:303-310，§8.5 禁 dynamic metadata，不写 ≠ 降级）；v3 先 `ensure_collection`（新建必含显式 `content_revision` INT64）再守卫（已存在但缺字段仍拒写，:319-325）；v3 upsert 失败 fail-closed 上抛 `VectorStoreUpsertError`（:326-335），由运维按 §16.1 写冻结降级回 S0，禁止丢弃新写 |
| 删除/清空同步 | `vector_store.py:360`（delete_doc）、`:389`（clear）内的 dual_target 分支 | S1 期 v3 影子侧同步删除，避免 S2 切换后已删文档残留 |
| 健康透出 | `vector_store.py:181-182` | `health()` 返回 `dual_write` / `dual_target_collection`（布尔非敏感） |

**隔离取证（本轮实跑，零真实连接）**：

- `python backend/tests/check_vector_dual_write.py` → `RESULT: 23 passed, 0 failed`，EXIT=0。
  覆盖：T1 默认关闭 / T2 S1 激活 / T3 S2 不生效 / T4 双落且版本由 v3 承载 / T5 v3 缺显式字段
  拒写 / T6 v3 upsert 失败上抛 / T7 无双写单写与 §8.5 拒写 / T8 delete/clear 同步 / T9 health 透出。
- 注册统一门禁后全量：`python backend/tests/run_unified_boundary_guards.py` →
  `SUMMARY total=17 failed=0`，EXIT=0（含 `vector_dual_write` 项 `RESULT: 23 passed, 0 failed`）。
- 语法检查：`py_compile config.py services/runtime_config.py services/vector_store.py
  tests/check_vector_dual_write.py` → EXIT=0。

**边界与已知分支线**：以上全部为进程内 FakeClient 隔离证据，未连接真实
Milvus/PostgreSQL/Neo4j；未在任何环境开启 `dual_write`；未 push。实现规格以 M5AB 计划 §4
为准（`milvus.dual_write` + `milvus.collection` 切换，shadow 隐含 v3）；`m5/dual-write`
分支后续演进的 `shadow_collection` 配置化 / `DualWriteConfigError` / `resolve_dual_write`
形态是**另一条已知分支线**，两线合并时需显式裁决收敛，不得默认某一口径。

---

## 10. 与既有章节的对应

- 集合 schema 与拒写：§8.5（本文档 §3）。
- 迁移顺序（表/索引/backfill）：§14。
- 状态机与禁止态、回滚五步：§16.1（覆盖 §15.4 的 A/B/C/D）。
- backfill 失败恢复时序与 dedupe/retry：§16.2 / §16.3（迁移期局部重建的部分失败处置）。
- citation current/indexed revision 与双投影最新语义：§11 / §16.4。
- 双 KB、并发、旧任务覆盖、部分失败、stale 验收：§12。

本方案不改变上游设计的任何判据，只补充"当前代码 vs 设计要求"的真实缺口核对与 canary 分类。
