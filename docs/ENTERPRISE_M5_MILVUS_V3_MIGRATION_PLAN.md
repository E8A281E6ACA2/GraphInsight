# M5 共享 Milvus v3 迁移方案（仅设计，零执行）

状态：**DESIGN ONLY / ZERO WRITES**（2026-10-03）
适用仓库：`E:\projects\GraphInsight`（与 aistudio 无关）
上游权威：`docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` §8.5 / §14 / §15.4 / §16.1 / §16.2
执行边界：本文件不触发任何 Milvus/Neo4j/DB 写入，不切配置，不建/删 collection，
不跑 canary。所有写步骤标 `GATED / NOT-EXECUTED`，须逐项显式授权后才可执行。

---

## 0. 一句话结论

真实 v3 迁移曾以 `dual_write`（§16.1 S1：同一生产写同时落 v2+v3）为代码级阻塞项。**该能力已在
后端实现并带自动化验证（默认关闭）**，S0→S1 不再缺代码；2026-10-05 的两条并行实现线已在
`integration/m5-phase3-4-p6` 合并且**收敛到审计线语义**（见 §9.5 集成裁决记录）。但**实现 ≠ 迁移**：
真实 v3 迁移仍禁止在未授权下推进——开启双写是需单独批准的迁移动作，且 §5.1 合成 schema canary
runner 仍未实现、§7 C3 per-KB 处置清单与 §9 其余门禁尚未满足。在此之前，禁止对任何真实 KB
执行 v3 迁移、禁止宣称 canary 闭环。本方案把"现在能做的"（隔离的单目标 schema 建集合验证）与
"必须授权 + 补齐其余前置才能做的"（双写开启/切换）分开摆明，并给出各自的验收判据与门禁。

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
| `dual_write`（2026-10-05 集成后口径） | `config.py`（`MILVUS_DUAL_WRITE` / `MILVUS_SHADOW_COLLECTION`）、`runtime_config.py`、`vector_store.py`（`resolve_dual_write`） | §16.1 S1 双写已落地（默认关闭；shadow 由 `shadow_collection` 配置，与主库同名则拒绝）。自动化验证：`backend/tests/check_m5_dual_write.py`（统一门禁 `m5_dual_write` 项）。**开启 = 需单独授权的迁移动作**，仍受 §9 门禁约束。见 §9.1 与 §9.5 |

结论性判定（2026-10-05 更新）：**v3 集合的"建立 + 单目标写入 + 字段判据"当前代码即可支撑；
"同一写扇出到 v2 与 v3 两个 collection"已由 S1 双写实现（默认关闭），真实启用见 §9 门禁。**

---

## 2. 为什么不能"直接开 v3"（不变量）

§16.1 明令禁止的状态：**v3 写 + v2 读**（写新读旧 → 检索永远看不到新 revision，静默数据
错位）。要把读源从 v2 原子切到 v3，必须在切换前后保证读写同源。没有 `dual_write` 时：

- 若直接切配置到 v3：v3 里没有存量向量 → 检索大面积空命中（数据丢失假象）。
- 若先逐 KB 重建 v3 再切：重建窗口内该 KB 读 v2 写 v3 = 违反读写同源不变量。
- 若边写 v2 边补 v3：这正是 `dual_write`，现已实现（默认关闭，见文末落地记录）。

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
S1  双写：写扇出 v2+v3，读仍 v2（v3 影子）   —— GATED；代码已实现(默认关闭)，开启需授权+补齐其余前置
S2  切换：原子把读源 v2→v3 + 重启           —— GATED，前置 S1 稳定且校验通过
S3  稳态：读写皆 v3，v2 只读留存备回滚       —— GATED
```

- **S0→S1 的前置不是配置开关，而是代码**。必须先实现 `dual_write`：在一次 upsert 内
  同时写 v2 与 v3，且任一失败要能定位并保持投影未收敛（与 §8.5 拒写语义一致）。在
  `dual_write` 存在且有自动化验证之前，**本步不存在，不得尝试用配置绕过**。
  **现状更新**：该代码前置已满足——`MilvusVectorStore.resolve_dual_write` + `upsert_chunks`
  /`delete_doc`/`clear` 扇出、影子失败抛 `DualWriteShadowError`（可定位、令投影判未收敛、按主键
  幂等重放收敛）已由 `backend/tests/check_m5_dual_write.py` 自动化验证；开关默认关闭。开启仍需
  §9 其余门禁 + 单独授权，不得仅因"代码有了"就设 `milvus.dual_write=true`。
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
- **依赖 `dual_write`——代码已实现（默认关闭），但 5.2 仍不可执行**：5.2 要真跑必须
  ①有授权、②存在真实 v3 目标 collection 且 §5.1 schema canary 已过、③§7 C3 处置清单归零。
  三者齐备前，5.2 **不得**以任何形式宣称"canary 已闭环/迁移可回滚"。
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

- 不写任何 v3/生产数据、不切配置、不跑 canary。（`dual_write` 的**代码实现**已落地并默认关闭，
  见 §9.1 与 §9.5；本轮不产生真实 v3 数据。）
- 不推荐"现在就设 `milvus.dual_write=true`"——代码虽有，但真实开启仍是迁移动作，须先满足
  §9 全部前置（§5.1 canary runner 落地、§7 C3 处置归零、真实 v3 collection、单独授权）；
  未达前置就开启 = 把脏/未校验投影写入 v3，属危险操作。
- 不开 M5-B1，不共享 v3 写入。
- 分支边界：2026-10-04 审计轮的"不 push、不动 main、提交只落 `audit/m5-gate0-coverage`"仅描述该轮；
  当前集成轮在 `integration/m5-phase3-4-p6` 分支本地提交，**仍然不 push**。

---

## 9. 进入执行前必须先满足的条件（阻塞清单）

1. ✅ `dual_write` 在后端实现并带自动化验证（S1 前置，责任：后端）——**已满足**：见 §9.1（审计线
   实现）与 §9.5（两线合并裁决）。仅解锁"代码前置"，不等于可开启真实双写；开启仍受 2/3/4 与授权约束。
2. ✅ 授权运行 §5.1 合成 schema canary（写 Milvus，需单独批准）——**已满足**（2026-10-05，用户书面授权后执行，见 §9.9；仅合成集合，v2 零写入）。**开工前置审计已完成**：见
   `docs/ENTERPRISE_M5_MILVUS_V3_CANARY_PREAUDIT.md`（三条写路径的显式 INT64 检查与 upsert
   返回数量/状态校验均已具备，含 4 项发现 F1–F4 与 canary 强制要求；F2/F3 已整改，见 §9.8）；
   canary 编排器已就绪（授权门控，默认零连接、只打印计划）。
3. C3 处置清单（§7）中所有待迁 KB 的 blocked/orphan/unrecoverable/scope_* 归零或有批准
   的例外。
4. 双写一致性 + 切换 + 回滚五步的验收脚本就绪（§6 判据落到可执行断言）。

以上任一未满足，真实 v3 迁移保持关闭，M5 gate 不宣布通过。

---

## 9.1 实现落地记录（2026-10-04 · dual_write 代码轮，分支 `m5/dual-write`）

本节只记录**代码事实**（均可 `git`/文件复查），不改变 §4–§9 的任何门禁判定：真实 v3 迁移仍关闭。

落地的能力（`MilvusVectorStore`，默认全部关闭 = S0 现网安全态）：

- 配置入口：`config.py` 增 `milvus_dual_write`（env `MILVUS_DUAL_WRITE`，默认 false）、
  `milvus_shadow_collection`（env `MILVUS_SHADOW_COLLECTION`，默认空）；
  `services/runtime_config.py:get_vector_store_runtime_config` 输出 `dual_write` +
  `shadow_collection`，配置中心优先压过 env/settings。
- 生效判据：`services/vector_store.py:MilvusVectorStore.resolve_dual_write` —— active 需同时满足
  开关开、store 已启用、shadow 非空、且 **shadow ≠ 主 collection**（同名退化为同集合重复写，拒绝）。
- 扇出写：`upsert_chunks` → `_upsert_to`/`_build_rows`，`ensure_collection` 支持按 collection 名。
  **主库（读源）先写**，主库失败直接抛出且影子零调用；影子（v3）写失败（主库已成）→ `logger.error`
  + 抛 `DualWriteShadowError`（携带 primary/shadow 集合名），令投影判未收敛、调用方按主键幂等重放收敛，
  绝不静默吸收（§4/§6/§8.5）；影子确认数≠主库同样判未收敛。影子 collection 缺显式
  `content_revision` 字段 → 复用 §8.5 门拒写并包成影子错误。
- 扇出删除：`delete_doc`/`clear` 同步删影子，避免 §6 "chunk_id 集合 v3 多"；影子删除失败上抛。
- 读源不变：双写生效时 `config().collection` 仍为主库，`search` 路径不受影响。
- `DualWriteShadowError` 为 `VectorStoreUpsertError` 子类，调用方原有 catch 仍能捕获"影子未收敛"。

自动化验证：`backend/tests/check_m5_dual_write.py`（假 client，无 pytest / 不连真实 Milvus），
16 条断言覆盖上述全部不变量；已注册进 `tests/run_unified_boundary_guards.py`（guard 名
`m5_dual_write`）。**未做**：真实建集合、真实 v3 写入、切读源、跑 §5.1/§5.2 canary——这些仍需
单独授权，且 §5.1 canary runner 本身仍未实现（NOT-IMPLEMENTED）。

---

### 9.2 实现落地记录（2026-10-05 · revision 生命周期轮，分支 `m5/dual-write`）

本节同样只记录**代码事实**（可 `git`/文件复查），不改变 §4–§9 的任何门禁判定：真实 v3 迁移仍关闭。
它解决的是 §6 对账判据"`v3.content_revision == current_revision`"此前**无源**的问题——
S1 双写要落显式 INT64 revision，前提是本仓建图路径自己能建立权威 revision。

- 新增 `backend/services/chunk_revision_lifecycle.py:write_revisions_for_build_graph`：单事务内
  为每个 chunk 建立/推进权威 current revision。无 current → `rev=1`；`content_hash` 与既有
  current 相同 → 保持不动（幂等重试）；内容变化 → 老行 `UPDATE … WHERE revision_id=? AND
  revision_status='current'` 转 superseded（rowcount≠1 即 `RuntimeError` 整体回滚），再 INSERT
  `rev=max+1` 的 current。`content_hash = sha256(text)`，与 §9 backfill
  （`admin/backfill_chunk_revisions._sha256`）同源。**不新增迁移**：复用 `chunk_revisions` 既有
  `UNIQUE (kb_id, chunk_id, content_revision)` 与部分唯一索引 `uq_chunk_revisions_current`。
- `build_graph` 接线（`services/document_graph_service.py`）：revision 在"文档确有变更"分支之后、
  **任何 Neo4j 投影写入之前**建立；返回的 revision map 逐 chunk 注入 `chunk_payload`，Chunk MERGE
  写 `ch.content_revision = coalesce(c.content_revision, ch.content_revision)`，同一 map 传给
  `retrieval_orchestrator.index_chunks(content_revisions=…)` → `VectorChunk.content_revision`，
  §8.5 影子 collection 的显式 INT64 字段自此有源。
- 投影回写：每个 doc 建图后按 CAS 回写 `chunk_revisions` 投影状态（graph 落 indexed+版本；
  `index_chunks` 有失败则 vector 保持 pending 且版本置 NULL），并在 `build_graph` 末尾按 §6.2
  优先级重算文档级 `graph_status/vector_status`，结果以 `document_states` 返回。
- 自动化验证：`backend/tests/check_m5_build_graph_revision.py`（临时 SQLite + 假 Milvus client，
  不连真实 Neo4j/Milvus，22 条断言）。除单元口径外含**真实 build_graph 端到端取证**：revision 行
  先于 Document/Chunk MERGE 存在、Chunk 参数带 `content_revision`、`content_revisions` 传到
  `index_chunks`、CAS 回写 indexed、§6.2 聚合更新 `knowledge_base_documents`、`force=False`
  未变更跳过且不再建 revision、内容变更推进到 rev=2。已注册为统一门禁
  `m5_build_graph_revision`；本轮门禁全量 `SUMMARY total=19 failed=0`。
- 测试隔离修复（如实认账）：`backend/tests/check_kb_scope_isolation.py` 自称"纯单元"，但其
  scenario [g] 跑真实 `build_graph`，Wave 2 接线后曾把 1 条 revision 行写进**共享开发库**。
  现该脚本在任何 backend 模块导入前把 `ADMIN_DATABASE_URL` 钉到临时 SQLite
  （`GRAPHINSIGHT_BACKEND_ENV_FILE`，唯一能盖过 `backend/.env` 的入口），入口断言方言必须为
  sqlite（否则 exit 9），并新增"revision 行只落临时库"复核（passed=55 failed=0）。
  泄漏发生时开发库的完整行内容已留档：`revision_id=17, kb_id='kb-a', doc_id='doc-a',
  chunk_id='doc-a-000', content_revision=1, revision_status=current, graph_status=indexed,
  vector_status=indexed, reason='build_graph_m5_wave2'`。经授权后该行已于 2026-10-05 删除
  （`DELETE 1`，删后 `SELECT count(*) FROM chunk_revisions` = 0；`knowledge_base_documents`
  无 doc-a/doc-b 残留、`admin_jobs` 无该 reason 残留），删除前整表数据备份在
  `artifacts/dev_db_backups/chunk_revisions_stray_row_2026-10-05.sql`（gitignored，含
  `setval` 序列位）。
- **未做**：真实 Neo4j/Milvus 侧的 revision 写入验证、v3 建集合、切读源、§5.1/§5.2 canary——
  这些仍需单独授权；影子失败持久转交与连续场景在 Wave 3（见 §9.3）。

---

### 9.3 实现落地记录（2026-10-05 · Wave 3 影子失败持久转交与连续场景，分支 `m5/dual-write`）

同样只记录**代码事实 + 隔离取证**，门禁判定不变：真实 v3 迁移仍关闭、`dual_write` 仍为 false、
未进入 S2，本节不构成"真实投影闭环"或"方向 B 通过"的任何证据。

四项落地（口径均已在 §16.3 / §15.5 / §8.5 冻结，实现只落既有契约，**不新增迁移**）：

1. **入队唯一实现** `backend/services/reindex_queue.py`：`canonical_targets_hash`(:60) 规范化算法与
   M5-A backfill 逐字一致；`enqueue_on_connection`(:220) 落 §16.3 全分支表——pending/running/succeeded
   → `reused`，failed 且额度未用尽 → 原地 `retry_count+1` 的 `retried`，额度用尽 → `rejected`
   （`reason='retry_exhausted'`，禁止无限自动重试），cancelled → `retry_count=0` 的 `reset`；
   Postgres 冲突回读走 `SELECT … FOR UPDATE`(:84)，SQLite 靠唯一索引 + 单写者事务。
   `enqueue_reindex_jobs`(:343) 自带 `engine.begin()`，`targets_from_payload`(:184) 让提交路径与
   终态回写共用同一份 payload 判据。三处调用方全部改走本模块：backfill
   （`admin/backfill_chunk_revisions.py:803-812`）、build_graph 转交
   （`services/document_graph_service.py:895-900`，`source="build_graph_m5_wave3"`）、任务中心提交
   （`admin/services/job_service.py:248`）。
2. **影子失败持久转交**：`index_chunks` 逐 chunk 结构化失败 → `chunk_revisions` 只把**未收敛那条腿**
   置 `vector_status='failed'` 且版本置 NULL（graph 侧不受 vector 失败影响），随后自动入队
   `reindex_chunks`；§16.1"影子脏写不得被静默吸收"在此闭环为"落库 + 可重放任务 + 审计"。
3. **`reindex_chunks` 类型补齐**（提交 aae175a）：JobType 字面量、`JobItem.targets_hash`、
   `AdminJob.targets_hash` ORM 列。ORM 列**不带 `index=True`**——§16.3 唯一索引是迁移脚本用原生 SQL
   建的部分唯一索引 `uq_admin_jobs_targets_hash`，ORM 再声明单列索引会让 `create_all` 出的库多出一个
   已迁移库没有的 `ix_admin_jobs_targets_hash`，而 SQLite 的 `ALTER TABLE … DROP COLUMN` 在列仍被索引
   引用时直接报错，把迁移回滚堵死（`check_m5a_revision_backfill.py` [B] 三条断言因此转红，本轮修回）。
   注意：Go 控制面仍不接受 `reindex_chunks`——白名单标识符是 `supportedJobTypes`
   （`go-backend/internal/adminstore/jobs.go:26-30`，仅 build_graph/clear_kb/reindex），
   `validateJobCreateRequest`（`jobs.go:647-650`）在 INSERT 之前返回 `ErrJobValidation`，
   映射为 HTTP 400 `INVALID_BODY`（`go-backend/internal/httpserver/admin_jobs_native.go:758-761`）。
   §7 reindex-chunks HTTP 端点整体 NOT-IMPLEMENTED；§16.3 文里的 HTTP 409 / `JOB_409` 映射在
   Go 与 Python 两侧都不存在（NOT-IMPLEMENTED，Wave 4-2 实测核实）。
4. **作业（父）终态 → 投影（子）回写**：`services/chunk_projection_state.py:132 write_back_job_failure`
   两条腿各自用 `CASE WHEN < > 'indexed'` 只降级未收敛侧，已 indexed 的腿不回退；CAS 条件
   `content_revision=target_revision AND revision_status='current'`，current 已被并发移动则计入
   `current_moved` 不补写（§8.3）；`keep_vector_untouched=True`（§8.5 `INDEX_UNAVAILABLE`＝"没能力写"，
   vector 保持 pending 等 v3）。触发点 `admin/services/job_service.py:841-842`：仅 `retry_scheduled` 为
   假、仅 `reindex_chunks`，随后跑 `aggregate_document_states` 落 §6.2 文档态并写
   `kb_chunk_reindex_failed` 审计。提交路径的超限拒绝用 3004 `OPERATION_NOT_ALLOWED` + 结构化 details
   （job_id/targets_hash/retry_count/max_retries），并回滚入队事务不留孤儿 pending 行。

自动化验证（全部临时 SQLite + 假 client，不连真实 Neo4j/Milvus/PG，`dual_write` 只在隔离 driver
进程内打桩，真实配置未开启）：新增 `backend/tests/m5_wave3_handoff_driver.py`（5 个场景：
continuity / terminal_exhausted / terminal_crash / retry_not_terminal / index_unavailable）+
`backend/tests/check_m5_wave3_handoff.py`（**71 条断言，0 失败**），注册为统一门禁 `m5_wave3_handoff`；
本轮回归 `SUMMARY total=20 failed=0`，另跑 `check_b0_reindex_chunks`（通过）、
`check_kb_scope_isolation`（passed=55 failed=0）、`check_m5a_revision_backfill`（[A]–[D] 全绿）。
反假绿设计：每个"0 条审计"断言都与同库的正向审计证据（`job_failed`/`job_retry_scheduled` 计数）配对，
因为 `log_crud.create()` 在 `admin_logs` 缺失时会静默 no-op，故 bootstrap 显式建该表。

- **未做**：真实 Neo4j/Milvus/PG 上的转交与回写验证（`check_m5a_live_execution.py --confirm` 会写真实库，
  仍未获授权、未运行）、v3 建集合与切读源、§5.1/§5.2 canary、Go 侧 reindex-chunks 端点与 409 映射。

---

### 9.4 实现落地记录（2026-10-05 · Wave 4 验证轮，分支 `m5/dual-write`）

用户指令边界：本轮**仅**总回归 + 前端/Go 任务中心验证 + 隔离 fail-closed 守卫 + 审计交付；
不运行任何 `--confirm`、不连真实 Neo4j/Milvus、不 push、不 amend。交审后才裁方向 B 与 push。
完整证据与复核命令见 `docs/ENTERPRISE_M5_WAVE4_AUDIT_PACKAGE_2026-10-05.md`（下称"Wave 4 审计包"）。

- **总回归**（新鲜实跑，均 EXIT=0）：统一门禁两轮 `SUMMARY total=20 failed=0`；
  `check_m5_wave3_handoff` 71 条断言 0 失败、`check_m5_dual_write passed=23 failed=0`、
  `check_m5_build_graph_revision passed=22 failed=0`、`check_kb_scope_isolation passed=55 failed=0`、
  `check_build_graph_shadow_retry` / `check_b0_reindex_chunks` / `check_m5a_revision_backfill`（[A]–[D]）通过。
- **Go / 前端口径改正（本轮实核，覆盖 §9.3 的错误表述）**：白名单实名是
  `supportedJobTypes`（`go-backend/internal/adminstore/jobs.go:26-30`），不是 `allowedJobTypes`；
  不支持的类型由 `validateJobCreateRequest`（`jobs.go:647-650`）在 INSERT 前返回
  `ErrJobValidation` → HTTP **400 `INVALID_BODY`**（`internal/httpserver/admin_jobs_native.go:758-761`）。
  仓库内不存在 `JOB_409` / 409 作业映射（Go、Python 两侧均 NOT-IMPLEMENTED）。同口径已同步
  `services/reindex_queue.py` 与 `admin/services/job_service.py` 的 docstring。
- **任务中心处置事实**（只报不改）：写侧关门（Go 白名单 400）、读侧开门（`ListJobs`/`GetJob`/
  `RetryJob`/`CancelJob` 无类型闸门）、`targets_hash` 对 Go DTO 与前端完全不可见、前端
  `JobType`/`jobTypeOptions` 缺 `reindex_chunks`、Python `JobService.create_job` 无 HTTP 调用方、
  白名单判定无 Go 单测。逐条 file:line 见 Wave 4 审计包 §4.1（W4-F1～F6）。
- **Go 侧证据边界**：`go test ./internal/adminstore/` ok（含 3 个 Job 测试通过）；
  `internal/httpserver` 在 Windows 宿主**构建失败**（`admin_monitor_native.go:896-899` 用 Unix-only
  `syscall.Statfs`，该文件最后改动 `a053532`/2026-07-27，本轮提交集不含任何 `.go`）——既有平台限制，
  非本轮回归；`GOOS=linux go vet ./internal/httpserver/` 与 `GOOS=linux go build ./...` 均 EXIT=0。
- **隔离 fail-closed 静态守卫**（`backend/tests/check_migration_cleanup_guards.py`，既有
  `migration_cleanup` 门禁项内扩展，不新增 case）：对所有 tracked 且含 `create_all(`/`create_engine(`
  的 `backend/tests/*.py` 要求 ①禁裸 `DATABASE_URL` ②方言闸门 ③非 driver 必须钉
  `GRAPHINSIGHT_BACKEND_ENV_FILE` + `ADMIN_DATABASE_URL` + `sqlite:///`；扫描面只用 `git ls-files`，
  git 失败/追踪文件缺失/扫描面为空一律 raise。真树 findings=0；红证用**真实脚本源码**在内存里把
  `ADMIN_DATABASE_URL` 退化成 `DATABASE_URL`（本轮事故形态）→ 必判红。见审计包 §5。
- **事故记录（用户 2026-10-05 钦定口径，逐字）**：发生共享开发 PG 连接及 DDL 尝试，已检查范围内
  未观察到持久化变化；因无事前全库快照，保留不可完全判定窗口。根因是探针脚本把注入变量写成
  `DATABASE_URL`（正确名 `ADMIN_DATABASE_URL`）被 `admin/database.py` 静默忽略；"已检查范围"清单与
  为何不做二次连库复核，见审计包 §6。本轮防再犯措施即上条静态守卫。

---

### 9.5 两线合并与集成裁决记录（2026-10-05 · 分支 `integration/m5-phase3-4-p6`）

2026-10-05 存在两条并行实现线：① **审计线** `m5/dual-write`（Wave 0–5；含 Wave 1 加固——
配置非法 fail-closed、`shadow_collection` 配置化、`DualWriteShadowError` 可定位）；② **集成线 main**
上的"§4 最小移植"（`MILVUS_DUAL_WRITE` + 硬编码 shadow=v3；requested-but-inactive 仅 warning）。
按用户指令把①（`15662b1` P6 链）与②所在文档链（`13303dc`）合入同一集成分支。**冲突裁决显式如下，
不默认任何一方**：

| 冲突面 | 裁决 | 理由 |
|---|---|---|
| `backend/config.py` | 取审计线：保留 `MILVUS_DUAL_WRITE`（严格 `=="true"`）并取 `MILVUS_SHADOW_COLLECTION`；纯空行噪声保留集成线 | 审计线为超集且解析更严 |
| `backend/services/runtime_config.py` | 取审计线：同时输出 `dual_write` 与 `shadow_collection` | 支撑 `resolve_dual_write` 生效判据 |
| `backend/services/vector_store.py` | **整体取审计线** | 审计线含 Wave 1 加固（requested-but-inactive 时 upsert/delete/clear **fail-closed 抛 `DualWriteConfigError`**、`DualWriteShadowError` 可定位、`ensure_collection(collection=)` 已具备）；最小移植线无这些守卫，属**被取代实现** |
| `backend/tests/check_vector_dual_write.py`（最小移植的镜像脚本） | **退役删除** | 其 T3 断言"requested-but-inactive 只 warning 不生效"与审计线 fail-closed 语义**互相矛盾**；同树共存会让门禁自相冲突。审计线 `check_m5_dual_write.py` 覆盖更全（含"非法配置零调用"与"影子删除失败") |
| 统一门禁 case | 移除 `vector_dual_write`；保留审计线 `m5_dual_write`；保留集成线新增 `backfill_manifest` | 同一语义不留两个判据 |
| `backend/admin/backfill_chunk_revisions.py` | 两线**自动合并**：集成线新增运行清单（§9.6）叠加审计线既有改动 | 改动面不重叠，回归须验证两者共存 |

**合并残留清理（同轮实测）**：自动合并把集成线最小移植的 config() 内 dual_target 解析块与 health()
的双写键保留在了非冲突区，与审计线 
esolve_dual_write() 并存（同一方法两条口径）。已清理：config()
删除退役块（仅留注释说明由 
esolve_dual_write 统一承担）；health() 改为**同一判据**透出
dual_write(active) / dual_write_requested / dual_shadow_collection / dual_write_reason。
复核：git diff --ignore-cr-at-eol --ignore-all-space m5/dual-write -- backend/config.py 空（仅空白差异）、
ector_store.py 相对分支仅剩该 health 增补（13+/1-）、check_m5_dual_write.py 23/23、
统一门禁 SUMMARY total=21 failed=0。

**跨链新暴露的守卫精度问题（合并轮实测，已整改）**：`m5/dual-write` 侧的严格静态守卫
（`check_migration_cleanup_guards.py` 的 R-ENV / R-DIALECT）与本线 Gate 0 链的
`check_m5_gate0_coverage.py`（`create_engine("sqlite://")` **内存库**、无外部库标记）在合并后才同时出现，
原判据把"显式内存 sqlite"读成"未钉隔离目标"。整改（只提精度，不放宽硬保证）：

1. R-ENV 接受显式 sqlite URL：`sqlite://`（内存）或 `sqlite:///`（临时文件）均算钉住目标
   （内存库天然不可能连外部库）；硬保证由方言闸门承担。
2. 新增"纯内存 sqlite 豁免"：`sqlite://` 且无 `sqlite:///`、无 `postgresql`/`psycopg`/`mysql`/`mariadb`
   标记时，免于 env 覆盖入口要求（其余判据仍全查）。
3. `check_m5_gate0_coverage.py` 补真实方言 fail-closed：`if engine.dialect.name != "sqlite": raise SystemExit(...)`。
4. 守卫负向自证样本仍全绿（样本用文件库 `sqlite:///`，不被内存豁免放过）。

**本轮逼近事件（如实记录，与 Wave 4 事故性质不同）**：`check_backfill_manifest.py` 初版把方言探针放在
"写 env 文件"之前，`admin/database.py` 因 `GRAPHINSIGHT_BACKEND_ENV_FILE` 指向的文件尚不存在而回落
`backend/.env`，探针读到 `postgresql`。**未建立任何连接**：`admin/database.py:32` 是惰性
`create_engine`（导入不连库），探针只读 `engine.dialect.name`，而方言由 URL 字符串解析、无需连接——
自证：`create_engine("postgresql://u:p@127.0.0.1:1/none")` 的 `dialect.name` 仍为 `postgresql`（端口 1 不可达）。
**无 DDL、无查询、无写入。** 探针顺序已修正（先 `use_db` 写 env 文件再探针），并作为该判据的正向回归保留。

**被淘汰的语义差异（记录以备审计）**：S2 形态（`dual_write=true` 但 `shadow_collection` 未配置或与主库
同名）在审计线**任何 mutation 前 fail-closed**；在最小移植线是"warning + 不生效"（静默退化）。
合并后以审计线为准。真实环境开启双写仍须 §9 全部前置 + 单独授权，M5-B1 冻结不变。

---

### 9.6 实现落地记录（2026-10-05 · backfill 运行清单轮，integration worktree，本地 main）

对应 v3 backfill 路线图阶段 2 的"完整 manifest"项：backfill 每轮执行（含 dry-run 与异常
中止）落盘一份**单 JSON 文档**运行清单，使"真实数据读回 / 失败注入 / 回滚 / 幂等重跑"
（阶段 3 disposable 验证）有可逐轮对账的权威记录。本节同样只记**代码事实**，不改变
§4–§8 任何门禁判定。

| 项 | 落点（file:line，现树实测） | 语义 |
|---|---|---|
| 清单常量 | `backfill_chunk_revisions.py:91-92` | `schema=m5a-backfill-manifest-v1`；目录来源 `--manifest-dir` > `BACKFILL_MANIFEST_DIR` > 未设置（不落盘） |
| 三态目录解析 | `:97` | `_UNSET`（按环境变量）/ `None`（显式关闭）/ `Path`（显式目录）三态，避免"未设置却落盘"污染隔离测试 |
| 骨架 | `:107` | trace_id / kb_id / mode / started_at / capabilities / inventory / gate_decision / gate_final（dry-run 为空）/ writes（含 `inserted_chunk_ids`、`state_write_failed_ids`）/ reindex_jobs / exception / exit_code / manifest_write_ok；writes 恒在且零值起步 |
| 落盘 | `:143` | 单文件 `<trace_id>.json`，`indent=1` 供人读、`json.load()` 必须直过（证据合同）；成功打印 `RUN_MANIFEST path=`，失败打印 `RUN_MANIFEST_WRITE_FAILED`（stderr），**不改变门判定退出码** |
| 出口包装 | `:1260`（`run`）/ `:1031`（`_run_impl`） | `run()` 负责计时 + 生成 trace_id + `finally` 落盘；异常也留痕（`exception` + `exit_code=1`）后原样上抛，禁止吞掉；各拒绝/完成分支在 `_run_impl` 落 `outcome` |
| CLI | `:1292-1296` | `--manifest-dir`、`--no-manifest` |

**隔离取证（本轮实跑，临时 SQLite + 假证据源，零真实连接）**：

- `python backend/tests/check_backfill_manifest.py` → `✓ all backfill run-manifest checks passed`，EXIT=0。
  覆盖：T1 执行清单单 JSON + schema/身份齐备 / T2 写入腿与 DB 实际一致 / T3 决策门与最终门 +
  outcome / T4 dry-run 清单 writes 全零且无最终门 / T5 幂等重跑两轮各一份清单（trace_id 互异、
  次轮零新增 + `rows_skipped_existing=1`）/ T6 失败注入：写入腿抛异常仍留清单
  （`exception` + `exit_code=1`）/ T7 未启用时零副作用（不建目录、无 RUN_MANIFEST）/ T8
  `RUN_MANIFEST path=` 指向的文件名 = `trace_id.json`。
- 注册统一门禁后全量：`run_unified_boundary_guards.py` → `SUMMARY total=18 failed=0`，EXIT=0。
- 既有回归：`check_m5a_revision_backfill.py` → `✓ all M5-A acceptance checks passed`，EXIT=0
  （`run()` 签名向后兼容，驱动 `bf.run(kb, dry_run)` 两参调用未受影响）。

**判据与陷阱（供编排器引用）**：

1. 清单是**审计产物**不是门判定器：写失败只响铃，退出码仍由数据库状态与 `evaluate_gate` 决定；
   编排器须对"清单缺失 / `manifest_write_ok=false`"fail-closed，不得把 RUN_MANIFEST 缺失读成通过。
2. 同轮复跑产生新 `trace_id` 新文件，历史清单**不覆盖不追加**；跨轮对账只能按 `started_at`
   （文件名含随机后缀，字典序 ≠ 执行顺序——本轮 T5 首版即因此判反，已改判据）。
3. 未设置目录时**完全不落盘**，故共享环境 dry-run（阶段 1 编排器）行为零变化。

---

---

### 9.7 disposable 环境验证记录（2026-10-05 · 路线图阶段 3，一次性 docker 容器）

v3 backfill 路线图阶段 3 要求"真实数据读回 / 失败注入 / 回滚 / 幂等重跑"四项在 disposable
环境完成。本轮在**一次性 docker 容器**上完成（仅绑定 127.0.0.1、跑完即删并验证销毁）：

| 引擎 | 容器 | 用途 |
|---|---|---|
| PostgreSQL 16（`postgres:16-alpine`） | `gi-phase3-pg` @ 127.0.0.1:55432 | 真实 PG：建表 / 写入 / 读回 / 结构级回滚 |
| Neo4j 5.26.26 | `gi-phase3-neo4j` @ 127.0.0.1:29531（`NEO4J_AUTH=none`） | 真实图库：Chunk 播种、回写读回、失败注入 |
| Milvus | **本轮不拉起** | 见下方"未取证"声明 |

四项结果（编排器 `E:/projects/gate0-evidence/2026-10-05/phase3/run_phase3_disposable.py`，
证据同目录 `phase3_steps.json` / `phase3_meta.txt` / `PHASE3_EVIDENCE_REPORT.txt` / `manifests/`）：

| 项 | 判据 | 结果 |
|---|---|---|
| 真实数据读回 | PG `chunk_revisions` 读回 3 行 current（revision=1、graph indexed/1、vector skipped）；Neo4j `c.content_revision` 读回 =1 | PASS（双引擎真实读回，非 mock） |
| 失败注入 | Neo4j 指向不可达端口 → 非零退出 + 清单留 `exception`/`exit_code=1` + **零新增写入**（无半截数据） | PASS（fail-closed） |
| 回滚 | `chunk_revisions` 整表回滚消失 + `admin_jobs.targets_hash` 列回滚消失（口径：后者只删索引+列，表仍在）→ 再迁移 → 再跑通 `rows_new=3` | PASS（真实 PG 结构级） |
| 幂等重跑 | 恢复后 `rows_new=0`、总行数不翻倍；三轮共 3 份清单（历史不覆盖） | PASS |

本轮实测：**CHECKS_TOTAL=33、CHECKS_FAILED=0、ROUND_EXIT=0**
（`RUN_ID=RUN-20261005T185413-phase3-disposable`）；清理阶段删除 Neo4j 播种节点与临时
解析产物，并销毁全部一次性容器后复核 `docker ps -a` 无残留。

**未取证声明（必须随本节一起读）**：Milvus 腿本轮**关闭**（`VECTOR_STORE_ENABLED=false`，
`_load_milvus_chunks` 在 store 关闭时早返回 `{}`，既不连也不写）。一次性 Milvus standalone
在本机无法启动（`panic: failed to create etcd client: context deadline exceeded`，内嵌 etcd
超时；项目 compose 栈因带 `graphinsight_milvus_data` 卷可正常启动，但那属于 local-live
而非 disposable）。真实 Milvus 的 schema / 读回属 **§5.1 合成 schema canary（§9 阻塞项 2，
需单独批准）**，且路线图排在阶段 4 书面授权之后——**本轮未取证，不得据此宣称真实向量迁移
或生产收敛**。阶段 4 获授权后须补：真实 v3 集合建集合、显式 INT64 `content_revision`
写入与读回、切读源验证。

---

---

### 9.8 canary 前置审计发现 F2/F3 整改（2026-10-05 · 分支 `integration/m5-phase3-4-p6`）

前置审计（`docs/ENTERPRISE_M5_MILVUS_V3_CANARY_PREAUDIT.md` §4）的 F2/F3 已整改，判据在隔离层可回归：

| 发现 | 整改 | 落点（file:line 现树实测） |
|---|---|---|
| F2：worker 无写入后回读校验（`indexed` 仅依据 upsert 数量） | 新增 `_verify_milvus_readback()`：写入数量确认通过后再回读 `chunk_id/text/content_revision`。三态**绝不把"没校验"读成"已校验"**——`True`（一致）/`False`（不一致 → 记 `write_failed`，fail-closed）/`None`（store 未启用、client 不支持 query、读回抛错 → 告警并保留数量口径） | `backend/services/chunk_projection_reindex.py`（`_verify_milvus_readback` + `_write_milvus_projection` 调用点） |
| F3：`_load_milvus_chunks` schema 探测失败时无条件请求 `content_revision`（v2 上直接 query 报错） | 回退改为：仅当显式 INT64 字段确实存在时才追加 `content_revision`（复用写侧同一 helper `_milvus_has_revision_field`） | `backend/admin/backfill_chunk_revisions.py`（`_load_milvus_chunks`） |

**回归网（新增，钉住整改）**：`backend/tests/check_b0_reindex_chunks.py` 新增 E3 分节 5 断言——上写行携带显式
`content_revision`、回读一致 → `vector_indexed`、回读不一致 → `write_failed`（`vector_content_revision` 保持 NULL）、
告警可定位、图腿不受牵连；驱动新增 `readback_verified` / `readback_mismatch` 场景，向量腿走**真实**
`_write_milvus_projection` + 真实 `upsert_chunks`，只替换 client。

**本轮实测**：`check_b0_reindex_chunks.py` 全绿；统一门禁 `SUMMARY total=21 failed=0`；M5 家族 8 项全绿
（`m5_dual_write` 23/23、`m5a` 全绿、`m5_wave3_handoff` PASS、`build_graph_shadow_retry` PASS、
`m5_build_graph_revision` 22/22、`kb_scope_isolation` 全绿、`backfill_manifest` 全绿）；
F2/F3 整改后 Phase 4 **仅**跑未授权预检（零连接、零落盘、exit 3）。

**如实记录的两条边界**：① F2 的 `None` 分支在隔离假体（client 无 `query`）下仍保留数量口径；真实 Milvus 上的
强校验由 Phase 4 canary 的"写入后读回"承担，不得据此宣称"worker 在所有环境下都已回读校验"。
② 本轮踩到测试夹具陷阱：`from services import embedding_service` 取到的是**模块**，
必须取单例 `from services.embedding_service import embedding_service`，否则补丁静默无效、
向量腿会走真实 embedding（已在驱动注释中固化该口径）。

---

### 9.9 §5.1 合成 schema canary 执行记录（2026-10-05 · 用户书面授权后执行）

**授权原文**：仅允许连接 `127.0.0.1:19531`，仅创建并删除合成 `graphinsight_chunks_v3_canary_*` 集合，
禁止访问 19530、禁止写入任何 v2/v3 生产集合、禁止启动迁移与 M5-B1。

| 项 | 值（均取自本轮输出，未手抄） |
|---|---|
| 编排器 | `E:/projects/gate0-evidence/2026-10-05/phase4/run_phase4_milvus_canary.py --authorize` |
| RUN_ID / EXEC_TIME | `RUN-20261005T222930-phase4-canary` / `2026-10-05T22:29:30` |
| 目标 | `http://127.0.0.1:19531`（白名单钉死；19530 为显式禁连项） |
| 合成集合 | `graphinsight_chunks_v3_canary_20261005`（建 → 用 → drop，跑完不存在） |
| 结果 | `CHECKS_TOTAL=20`、`CHECKS_FAILED=0`、`ROUND_EXIT=0`、`OVERALL = ALL PASS` |

**本轮证成的判据**：

1. **显式 INT64**：合成集合 `content_revision` 字段经 `describe_collection` 复核为 INT64（非 dynamic metadata）。
2. **写入可读回（补 F2/F4）**：`flush → load → query(consistency_level=Strong)` 读回 3 行，逐项核对
   `text/kb_id/tenant_id/project_id/content_revision/vector` 全部一致；`content_revision` 读回为 7/11/3（与写入相同）。
3. **upsert 确认契约（F4 实证）**：真实返回 `{'upsert_count': 3, 'cost': 0, 'ids': ['canary-a1','canary-a2','canary-a3']}`
   —— `require_upsert_count` 的键名解析与实际库一致，数量 = 期望 3。
4. **v2 零写入（三重）**：集合清单前后一致、`graphinsight_chunks_v2` schema 前后完全一致、
   **v2 `count(*)` 前=0 / 后=0，且两次均为可读整数**（`counts_readable=true`、`counts_identical=true`）；mutation 调用台账
   仅含合成集合（create_collection / create_index / upsert / flush / load_collection / drop_collection）。
5. **失败注入 fail-closed**：维度不符、集合不存在两类注入均抛错；注入后仍为 3 行（无半截数据）。
6. **清理与残留**：drop 后集合清单与轮前一致；跑后只读复核 `list_collections() = ['graphinsight_chunks_v2']`，无 canary 残留。
7. **边界**：本轮未构建 PG/Neo4j 客户端、未 flip 配置、未触碰服务单例、未连 19530、未 push、未启动 M5-B1。

**口径边界（不得越读）**：本节是 **§5.1 合成集合 schema/写读回 canary**，**不是**"单 KB 真实 KB 的 v3 canary"
（无真实 KB 数据、未切读源、无 C3 per-KB 收敛对账）。路线图阶段 4 的真实单 KB canary 与阶段 5 的逐 KB
backfill 仍未开始；共享生产 Gate 继续 OPEN，M5-B1 继续冻结。

**证据文件**（同 RUN_ID/EXEC_TIME，单 JSON 文档可 `json.load` 直过）：
`E:/projects/gate0-evidence/2026-10-05/phase4/` 下 `phase4_steps.json` / `phase4_meta.txt` /
`PHASE4_EVIDENCE_REPORT.txt`；离线自查脚本 `check_phase4_count_semantics.py` /
`check_phase4_doc_consistency.py` / `check_phase4_evidence_contract.py` 同目录。

**文案更正（复核方 2026-10-05 提出，未改动任何数字/判据/结论）**：原报告与 meta 的红线表述
"未写 v2/v3" 易被理解为"连本轮合成 canary v3 也未写"。准确口径已统一为
**"未写入任何生产 v2/v3 集合；仅写入本轮合成 canary 集合（`graphinsight_chunks_v3_canary_*`，跑完已 drop）"**。
处理方式：① 生成器模板已改（报告 RED LINES 与 meta NOTE 两处，并附合成集合名）；
② 已产出的两份文本**保留原文**并追加 `CORRIGENDUM` 段（不改数字，合同复验仍过：
`check_phase4_evidence_contract.py` 全绿）；③ 一致性检查加防复发断言（生成器与证据都必须带精确口径）。
另：`upsert_raw_result` 自本轮起同时记录**结构化对象**与原文 `repr`（便于机器核对键名/数量）。

---

### 9.10 共享 KB 只读 C3 清单 + 真实单 KB canary 准备（2026-10-05 · 下一阶段第一/二步）

**第一步：共享 KB 只读 C3 清单（零写入）**。编排器 `E:/projects/gate0-evidence/2026-10-05/phase5/run_phase5_shared_c3_inventory.py`
（受控描述符 `E:/projects/.gate0-controlled/local-live.env`，环境 id `local-dev-gate0-ro`）：

| 项 | 值（取自本轮输出） |
|---|---|
| RUN_ID | `RUN-20261005T231214-phase5-c3-inventory` |
| 覆盖 | `coverage_verdict=FULL`、`active/probed/omitted/failed = 1/1/0/0` |
| 零写入证明 | `writes=0`、`counter_drift=[]` |
| Gate | `SHARED_PRODUCTION_GATE=OPEN`（保持） |
| 运行树 | `commit_sha=29f1bd0` |
| 处置结果 | 1 个 KB `34905f75-38ca-4cfa-bf22-c89c96107fa8`（`active`，§9 五类全 0，`gate.closed=true`，`needs_reindex=0`）→ **ready canary 候选** |

证据：`phase5_inventory.json`（含 `raw_lines`，无文本头）、`phase5_disposal_table.json`（逐 KB 处置 + 候选 + `candidates_ready`）、
`phase5_meta.txt`、`PHASE5_EVIDENCE_REPORT.txt`；12 项检查 + 10 项合同校验全绿。

**第二步：真实单 KB canary 四项验证的准备**（可执行部分已在 disposable 彩排，真实 Milvus/KB 步骤未执行）：

| 项 | 状态 |
|---|---|
| R1 运行清单（manifest） | ✅ 彩排（`RUN-20261005T231306-phase6-rehearsal`：字段齐备、`RUN_MANIFEST` 路径一致） |
| R2 worker/backfill 读回 | ✅ PG/Neo4j 腿部彩排；Milvus 读回机制已由 §5.1 canary 证（phase4），**真实 KB 的 Milvus 读回待授权** |
| R3 回滚 | ✅ 迁移级（表/列消失 + 再迁移 + 再跑通）；**§16.1 五步（写冻结/v3→v2 回放/权威清单对账/切读源/解冻）未执行**，依赖 Milvus 与授权 |
| R4 幂等重跑 | ✅ 彩排（`rows_new=0`、总行数不增、两轮两份清单 `trace_id` 互异） |

就绪档案：`E:/projects/gate0-evidence/2026-10-05/phase6-prep/PHASE6_CANARY_READINESS.md`（前置条件 P1–P6、
四项执行规程、§16.1 五步映射、停止条件、证据合同、待授权事项清单）。档案与机器证据的一致性由
`check_phase6_readiness_consistency.py` 守住（RUN_ID/候选 KB/统计数字/未执行声明/口径，逐条对齐，全绿）。

**边界**：本轮未写入任何生产或共享 v2/v3 集合（phase5 只读、彩排仅一次性容器）；未修改 v2；未启动全量迁移与 M5-B1；
未 push。共享生产 Gate 仍 **OPEN**；真实单 KB canary 需新的书面授权（清单见档案 §6）。

---

### 9.11 真实单 KB v3 canary：执行前只读预检判定 BLOCKED（2026-10-05）

用户于 2026-10-05 授权对 KB `34905f75-38ca-4cfa-bf22-c89c96107fa8` 执行真实 single-KB v3 canary
（允许：开 S1 dual-write / 建或用 `graphinsight_chunks_v3` / 该 KB backfill / 三引擎读回 / §16.1 回滚与幂等重跑；
禁止：写 v2 / 碰其他 KB / 全量迁移 / M5-B1 / push；失败即停并按 CLOSED_DEGRADED 或 FAILED 汇报）。

**执行前只读可行性预检**（`E:/projects/gate0-evidence/2026-10-05/phase7-precheck/`，
`RUN-20261005T234646-phase7-precheck`，`CHECKS_TOTAL=9`/`CHECKS_FAILED=5`/`ROUND_EXIT=2`；
两次探针均 `writes=0` / `counter_drift=[]`）判定 **BLOCKED（exit 2）**，
故**未执行任何写入**（遵循"失败即停止"）。五条阻塞（事实均取自本轮输出）：

| blocker id | 事实 | 影响 |
|---|---|---|
| `EMPTY_KB` | 目标 KB 在全部来源均无数据：Neo4j `chunk_nodes=0`、Milvus `entities=0`、PG `chunk_revisions=0`（仅 `knowledge_bases=1`） | canary 无迁移对象；任何"通过"都是**空态假绿**，不得作为真实迁移证据（Gate 0 合同明令） |
| `VECTOR_CAPABILITY_OFF` | 该 KB `vector_enabled=false`、`milvus_collection=""` | 即便有数据，Milvus 腿也会记 skipped/degraded，不会写 v3 |
| `V3_ABSENT` | `graphinsight_chunks_v3` 当前不存在（`target_exists=false`） | 创建它会在共享环境**永久新增**一个集合；空 KB 下无法被数据验证 |
| `AUTHORIZATION_CONFLICT` | 授权项 1（S1 dual-write）按 §16.1 必然写扇出 v2+v3，与禁止项 1（不得写 v2）互斥 | "真 S1 但不写 v2"在现有代码下不可达 |
| `BACKFILL_BYPASSES_FANOUT` | `admin/backfill_chunk_revisions.py:506` 直接 `client.upsert(collection_name=primary)`，**未走** `services/vector_store.upsert_chunks` 的 dual-write 扇出 | 即使开 dual-write，backfill 只写 primary(v2)；**v3 不会因此获得该 KB 的数据**——"S1 下用 backfill 补齐 v3"这条路径当前不存在 |

**结论口径**：本轮 canary **FAILED（执行前阻塞）**，不是 CLOSED、也不是 CLOSED_DEGRADED；共享生产 Gate 仍 **OPEN**；
未写 v2、未建 v3、未跑 backfill、未启动迁移与 M5-B1、未 push。

**因此需要先解决的项（按依赖排序）**：

1. **修 backfill 扇出缺口（代码，可先在隔离层做，无需共享写）**：让 `_backfill_milvus` 走
   `upsert_chunks`（获得 S1 扇出 + 影子失败 fail-closed），并补回归网；否则真实迁移的"补齐 v3"无路径。
2. **裁定空态 KB 的 canary 语义**：环境当前是空态（0 chunk）。若要"真实数据读回"，须先有**非空 KB**
   （真实入库/解析产物/索引），这本身需要数据写入授权；若只验机制，须明确标注"不证明真实向量迁移"。
3. **开关 vector 能力**：`vector_enabled=false` 意味着 embedding/store 未配置；真实 v3 写入需要该能力
   （配置 + 重启），亦需授权。
4. **消解授权互斥**：明确是否允许 S1 期间对**目标 KB** 的 v2 行做幂等 upsert（否则真 S1 不可达）。

就绪档案已同步更新（P0 非空前置、P1b 能力、B-A/B-B 两条结构性阻塞，含 blocker id 映射），并由
`check_phase6_readiness_consistency.py` 守住档案↔证据一致性（全绿）。

---

### 9.12 迁移写入契约修复（2026-10-05 · 工单 1–5，只改代码/测试/文档）

授权方复核 §9.11 后纠正了上一轮授权的互斥表述，并给出代码工单：**不申请扩大共享写入**，先修写入契约。
本轮**未连接共享服务做任何写入**、未改受控配置、未重启共享服务、未建共享 v3、未跑共享 backfill、未 push。

#### 9.12.1 写入契约（工单 1：在线 S1 双写与历史回填**分开定义**）

统一入口仍是 `MilvusVectorStore.upsert_chunks`（**唯一**扇出实现），新增 `intent` 维度：

| 意图 | 目标 | 语义 |
|---|---|---|
| `online_s1`（默认） | primary + shadow | primary 先写、shadow 扇出；影子失败 `DualWriteShadowError`（可定位、可重放收敛）。**legacy v2（无显式字段）**：primary **剥版本写**（不写 metadata、不改 v2 schema），版本由 shadow 完整承载；此时 shadow 必须已有显式 INT64 字段，否则写前 fail-closed；**无影子时拒写**（§8.5） |
| `backfill_versioned` | **仅 shadow** | 只补版本承载侧；不写 primary、不覆写既有 v2 行。写前判定：shadow 生效 + 显式 INT64 字段 + **primary 已持有这些 chunk**（§16.1 读源不变量，读不到 primary 亦拒）；写后必须**读回一致**才返回 |

机读拒绝原因（`VectorWriteContractError.reason`）：`SHADOW_NOT_ACTIVE` / `SHADOW_FIELD_MISSING` /
`PRIMARY_VECTOR_MISSING` / `PRIMARY_UNREADABLE` / `VERIFY_MISMATCH` / `VERIFY_UNAVAILABLE` /
`KB_NOT_ALLOWED` / `SCOPE_MISMATCH` / `COLLECTION_NOT_WRITABLE`。

回填的逐 chunk 结果映射（`admin/backfill_chunk_revisions.py`）：`SHADOW_*` → `revision_field_absent`（pending）；
`PRIMARY_*` → **`primary_vector_missing`**（pending，新计数 `milvus_primary_missing` 进 manifest）；
`VERIFY_*`/其它 → `failed`。**写后读回不一致或无法校验一律不标 indexed**（宁可 pending）。

统一化的另一处：worker 的写后读回改为调用 store 的 `verify_versioned_rows`（**只剩一份比较实现**）。

#### 9.12.2 目标 KB 写入范围限制（工单 2）

新增配置（AGENTS.md 规范 9：默认值/环境变量/后台可见）：

| 键 | 环境变量 | 语义 |
|---|---|---|
| `write_scope.kb_ids` | `MILVUS_WRITE_SCOPE_KB_IDS`（逗号分隔） | **非空**时，任何不在名单内的 KB 在**任何 mutation 之前**被拒（`KB_NOT_ALLOWED`，零写入）；**留空 = 不限制**（保持历史行为） |
| `write_scope.tenant_id` / `project_id` | `MILVUS_WRITE_SCOPE_TENANT/PROJECT` | 可选；非空则逐 chunk 校验（`SCOPE_MISMATCH`） |
| 可写集合 | —（由 `resolve_dual_write` 决定） | 目标集合只能是本次解析出的 primary/shadow，否则 `COLLECTION_NOT_WRITABLE` |

守卫落在 `upsert_chunks`（两种意图）、`delete_doc`、`clear` 三处 mutation 入口；**因为 S1 是进程全局配置**，
该 KB 限制是把全局开关收敛回授权范围的**可执行**手段（工单 2 的口径修正）。

#### 9.12.3 回归矩阵（工单 3）

新增 `backend/tests/check_vector_write_contract.py`（进程内假体，**30 断言全绿**）并注册统一门禁
（`vector_write_contract`，套件 `SUMMARY total=22 failed=0`）。覆盖：C1 legacy v2 无字段→primary 剥版本/影子带版本、
C2 primary 有 INT64→两侧带版本、C3 无影子拒写、C4 影子缺字段写前拒（primary 也零写）、C5 primary 失败→影子零写、
C6 影子失败→`DualWriteShadowError`（含两侧集合名）、C7 部分 upsert（确认数不一致）→抛错、
C8 回填只写影子（v2 零写）、C9 primary 缺 chunk→拒写、C10 shadow 未生效→拒写、
C11 写后读回不一致→拒标 indexed、C12 **revision 2/3 原样保留（绝不重置为 1）**+同批重跑幂等、
C13 范围守卫（非目标 KB 拒/目标放行）、C14 tenant 不一致→拒。
CAS 失败场景沿用 `check_b0_reindex_chunks.py`（worker 层 `direct_cas_failure`），不重复实现。

#### 9.12.4 预检修正（工单 4）

`E:/projects/gate0-evidence/2026-10-05/phase7b-precheck/run_phase7b_canary_precheck.py`（v2；v1 保留为历史）：
四个来源**分别统计且都以目标 KB 为 scope**（PG current revision / Neo4j Chunk / Milvus 按 kb 过滤 /
**解析产物 chunks.jsonl**）；每来源 value/ok/reason 三态，**读取失败记"未知"，绝不折成 0**。
blocker 由**配置 + 代码合同 + 授权范围**推导（`DATA_UNKNOWN` / `EMPTY_KB`（要求四来源全部可读且全 0）/
`VECTOR_CAPABILITY_OFF` / `COLLECTIONS_UNKNOWN` / `V3_ABSENT` / `BACKFILL_LEG_NOT_READY` / `WRITE_SCOPE_UNSET`），
不再硬编码 `AUTHORIZATION_CONFLICT`、`BACKFILL_BYPASSES_FANOUT`。

**v2 实跑结果（`RUN-20261006T082303-phase7b-precheck`，只读、exit 2）**：`neo4j_chunks=0`（ok）、
**`parsed_chunks=2`（ok）**、`pg_current_revisions=UNKNOWN`、`milvus_rows=UNKNOWN`、集合清单 `UNKNOWN`；
blocker = `DATA_UNKNOWN` + `VECTOR_CAPABILITY_OFF` + `COLLECTIONS_UNKNOWN` + `BACKFILL_LEG_NOT_READY` + `WRITE_SCOPE_UNSET`。
**这直接纠正了 §9.11 的表述**：该 KB 并非"全部来源为空"（解析产物有 2 条），且 PG/Milvus 读数失败属"未知"。
环境事实：共享 dev 栈在本轮期间被**重启**（`graphinsight-*` Up ~9 分钟，`graphinsight-go-gateway` Exited 255），
重启后 PG(127.0.0.1:5434) 与 Milvus(127.0.0.1:19531) 从宿主不可达（本机只读探针实测）；**本轮未改任何共享配置**。

#### 9.12.5 文档与验收口径修正（工单 5）

`ready canary` 条件修正为**必要条件集合**（C3 为零只是其中之一）：

1. **非空且可验证**：四来源（PG current / Neo4j / Milvus / 解析产物）以目标 KB scope 统计，
   **全部可读**且至少一处 > 0（任一未知 → 不得宣称非空，也不得宣称空）；
2. C3 五类为 0（必要条件，非充分条件）；
3. **vector 能力就绪**（`vector_store.enabled`、embedding 配置）且 shadow 集合具备显式 INT64 字段；
4. **合法写目标**（v3 集合存在或经授权创建，且不在生产集合名黑名单内被误写）；
5. **KB 范围限制已配置**（`write_scope.kb_ids` 非空，使全局 S1 收敛回目标 KB）；
6. **可执行回滚方案**（§16.1 五步在目标环境可执行：写冻结能力、v3→v2 回放、权威清单对账、读源切回、解冻）。

**当前状态**：预检 BLOCKED（上述 1/3/5 未满足，PG/Milvus 读数未知）；真实单 KB canary **未执行**；
共享生产 Gate 继续 **OPEN**；M5-B1 继续冻结。

---

### 9.13 复核问题整改（2026-10-06 · 四条 P1 + 两条 P2，只改代码/测试/文档）

复核方以"注入故障"方式发现四条 P1 与两条 P2，逐条整改如下（本节只记代码与测试事实，
门禁判定不变：真实 v3 迁移仍关闭、`MILVUS_DUAL_WRITE` 默认 false、未连任何真实服务）。

#### 9.13.1 P1-1 worker 未接通 legacy v2 的 S1 路径

| 项 | 内容 |
|---|---|
| 原缺陷 | `chunk_projection_reindex.py` 在调用统一入口**之前**用 `has_content_revision_field()`（只看 primary）拦截，legacy v2 永远走不到契约，S1 路径不可达 |
| 整改 | 删除该前置拦截，判据统一交给 `vector_store.upsert_chunks`（唯一写入契约）。逐 chunk 结果映射：`SHADOW_NOT_ACTIVE`/`SHADOW_FIELD_MISSING`（或裸 `VectorStoreSchemaError`）→ `revision_field_absent`（保持 pending）；终态 `INDEX_UNAVAILABLE`（`ValidationException`，不自动重试）改由**契约结果派生**（`if OUTCOME_REVISION_FIELD_ABSENT in vector_outcome.values()`），而不是前置检查 |
| 验证 | `check_b0_reindex_chunks.py` E4：走**真实** `_write_milvus_projection` + 真实 `upsert_chunks`（假 client），断言 v2 收到写入且**不含** `content_revision`、v3 带显式版本=1、PG 收敛 `vector_status=indexed` + `vector_content_revision=1`、计数 `vector_indexed=1` |
| 既有场景迁移 | `b0` 驱动 `revision_field_absent` 与 `m5_wave3_handoff` 的 §8.5 场景原先打桩**假向量写入器**（测的正是被删掉的前置路径），已改为真实契约路径：契约在写之前拒绝 → 零 upsert（Wave3 新增 `real_upserts == {}` 断言） |

#### 9.13.2 P1-2 写后未验证仍可 indexed（且只验 primary）

| 项 | 内容 |
|---|---|
| 原缺陷 | worker 读回只在 `verify_versioned_rows` 返回 `False` 时降级，`None`（不可读/无法校验）**保持 indexed**；且只读回 primary —— 注入"primary 新、shadow=999"仍判 indexed |
| 整改 | store 新增 `write_targets(intent)` 与 `verify_write_targets(expected)`：**对本次意图涉及的全部写入目标**（primary + 生效 shadow）统一读回；`carries_version` 的目标逐 chunk 比对版本，剥版本的目标期望该键不存在；**任一目标 `False` 或 `None` → `ok=False`** → worker 记 `write_failed`。已退役的本地单目标读回函数删除（只留一份实现） |
| 验证 | E4 两条负向：`readback_shadow_mismatch`（仅 shadow 版本不符）与 `readback_unavailable`（client 无 `query`）→ 均断言 PG `vector_status=failed`、版本 NULL、`vector_failed=1`、`vector_indexed=0` |

#### 9.13.3 P1-3 "剥版本"可被 metadata 绕过

| 项 | 内容 |
|---|---|
| 原缺陷 | `_build_rows` 里 `**(chunk.metadata or {})` 展开在显式字段之前，metadata 里的 `content_revision=99` 能覆盖"剥版本"结果，甚至覆盖 `chunk_id`/`vector`/`kb_id` |
| 整改 | 新增 `RESERVED_ROW_FIELDS`（身份/作用域/内容/向量/版本共 13 键）：metadata 中的保留键**丢弃并告警**；显式字段在 metadata 之后覆盖；`content_revision is None` 时**彻底 `pop` 该键**（剥版本 = 键不存在，不是空值） |
| 验证 | 契约矩阵 C15（显式 2 + metadata 99 → 落库 2）、C16（剥版本后 v2 行**不含**该键、影子仍带 1）、C17（metadata 不能覆盖 `chunk_id`/`vector`/`kb_id`） |

#### 9.13.4 P1-4 范围守卫放行漏洞

| 项 | 内容 |
|---|---|
| 原缺陷 | `_guard_write_scope` 在 KB 名单为空时直接 `return`，**tenant/project 限制被一并跳过**；且 `MILVUS_WRITE_SCOPE_KB_IDS=","` 解析为空后被当成"无限制" |
| 整改 | 各条件**独立生效**：① collection 白名单；② 名单"配置过但解析为空" → `SCOPE_CONFIG_INVALID`（非法**不得**退化为无限制）；③ 名单非空 → 逐 chunk 命中；④ tenant/project 只要配置就校验（**与 KB 名单是否为空无关**）。配置层保留原始字符串（`milvus_write_scope_kb_ids_raw`）并经 runtime_config 透出 `kb_ids_configured`。同一守卫覆盖 `upsert_chunks`（两意图）、`delete_doc`、`clear`，任一不合规**整批拒绝、零 mutation** |
| 验证 | C18（非法配置 → `SCOPE_CONFIG_INVALID` 且零写入）、C19（kb_ids 未配置但 tenant 不符 → 仍拒）、C20（`delete_doc`/`clear` 名单与非法配置均拒且零 mutation） |

#### 9.13.5 P2-1 预检收紧

`phase7b-precheck/run_phase7b_canary_precheck.py`：① Milvus 查询**返回空结果**不再折算为 0，记
`empty_query_result`（未知）；② 解析产物改为**逐行 `json.loads` + 校验 `chunk_id`**，输出
`parsed_stats{files,valid,invalid_json,missing_chunk_id}`，目录在但无有效 chunk 记未知；
③ 异常原因只用**脱敏类别**（`connect_failed`/`timeout`/`auth_failed`/`not_configured`/`invalid_data`/`unknown`）
+ 异常类型名，**不保存异常原文**（已核验证据文件中无主机/端口/原文残留）。
本轮实跑：`neo4j_chunks=0`（ok）、`parsed_chunks=2`（ok，2 文件 2 有效 0 坏行）、`pg_current_revisions` 与
`milvus_rows`、集合清单均为 `category=connect_failed` 的未知；VERDICT=BLOCKED（exit 2）。

#### 9.13.6 P2-2 统一门禁必须真正隔离网络（本轮边界披露）

**复核方复跑时的边界偏差**：`total=22 failed=0` 当时**不等于**"全程离线"——`docqa_internal` 与
`nl2cypher_internal` 子项导入 `main:app` 时经 `services.neo4j_service._connect()` **真连 Neo4j**
（观测到 `::1:7687` 鉴权失败），连接失败被容错吞掉后子项仍报绿。整改：

| 项 | 内容 |
|---|---|
| 网络护栏 | 新增 `backend/tests/netguard/sitecustomize.py`（runner 注入 `PYTHONPATH` 自动加载）：**禁一切非回环连接** + **禁真实服务端口**（5432/5434/6379/7474/7687/9003/19530/19531/29530/29531/9200/11434），放行其余回环（Windows `asyncio` ProactorEventLoop 依赖 `socket.socketpair()` 的回环临时端口，无差别封禁会连测试框架一起打掉）。命中抛 `NEUTRALIZED_NETWORK:<host>:<port>` |
| 判红机制 | runner 检测到该标记 → **该子项直接判失败**（即使退出码为 0） |
| 服务替身 | 新增 `backend/tests/_offline_services.py`：导入 app **之前**安装 Neo4j 驱动替身（不建连接；替身被真正使用即抛 `OfflineServiceUsedError`，不静默返回空结果）；DocQA/NL2Cypher 两子项已接入 |
| 负向对照 | 实测：`127.0.0.1:7687` → 拦截；`1.1.1.1:443` → 拦截；普通回环高位端口 → 放行（非护栏误伤） |
| 结果 | 在**真实护栏**下 `run_unified_boundary_guards.py` → `SUMMARY total=22 failed=0`（此时"全离线"是被强制保证的，而非"恰好没连"） |

#### 9.13.7 回归结果（本轮实跑）

| 命令 | 结果 |
|---|---|
| `run_unified_boundary_guards.py`（含真实网络护栏） | `SUMMARY total=22 failed=0`，EXIT=0 |
| `check_vector_write_contract.py` | `RESULT: 44 passed, 0 failed`，EXIT=0（原 30 + C15–C20 共 14） |
| `check_b0_reindex_chunks.py` | `✓ all M5-B0 reindex_chunks checks passed`，EXIT=0（含 E4） |
| `check_m5_wave3_handoff.py` | `RESULT: PASS`，EXIT=0 |
| `check_m5_dual_write.py` | `M5_DUAL_WRITE_SUMMARY passed=23 failed=0`，EXIT=0 |
| `check_m5a_revision_backfill.py` | `✓ all M5-A acceptance checks passed`，EXIT=0 |
| `check_backfill_manifest.py` | `✓ all backfill run-manifest checks passed`，EXIT=0 |
| 预检 v2（只读） | VERDICT=BLOCKED，exit 2（fail-closed） |

**连接范围披露**：本轮代码与测试**零真实服务连接**（进程内假体 + 临时隔离库）；预检为只读，实际连接受阻
（PG/Milvus `connect_failed`、Neo4j 读到 0）；未写入任何生产或共享 v2/v3、未建 v3、未跑共享 backfill、
未改受控配置、未重启共享服务、未 push；M5-B1 继续冻结。

## 10. 与既有章节的对应

- 集合 schema 与拒写：§8.5（本文档 §3）。
- 迁移顺序（表/索引/backfill）：§14。
- 状态机与禁止态、回滚五步：§16.1（覆盖 §15.4 的 A/B/C/D）。
- backfill 失败恢复时序与 dedupe/retry：§16.2 / §16.3（迁移期局部重建的部分失败处置）。
- citation current/indexed revision 与双投影最新语义：§11 / §16.4。
- 双 KB、并发、旧任务覆盖、部分失败、stale 验收：§12。

本方案不改变上游设计的任何判据，只补充"当前代码 vs 设计要求"的真实缺口核对与 canary 分类。
