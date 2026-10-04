# M5 共享 Milvus v3 迁移方案（仅设计，零执行）

状态：**DESIGN ONLY / ZERO WRITES**（2026-10-03）
适用仓库：`E:\projects\GraphInsight`（与 aistudio 无关）
上游权威：`docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` §8.5 / §14 / §15.4 / §16.1 / §16.2
执行边界：本文件不触发任何 Milvus/Neo4j/DB 写入，不切配置，不建/删 collection，
不跑 canary。所有写步骤标 `GATED / NOT-EXECUTED`，须逐项显式授权后才可执行。

---

## 0. 一句话结论

真实 v3 迁移曾以 `dual_write`（§16.1 S1：同一生产写同时落 v2+v3）为代码级阻塞项。**该能力已在
后端实现并带自动化验证（默认关闭，见文末"实现落地记录"）**，S0→S1 不再缺代码。但**实现 ≠ 迁移**：
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
| `dual_write` / `dualwrite` / `dual-write` | **已在后端实现（默认关闭）** | §16.1 S1 双写能力已落地：`MilvusVectorStore.resolve_dual_write` 生效判据 + `upsert_chunks`/`delete_doc`/`clear` 扇出主库与影子（`backend/services/vector_store.py`）。开关默认关，`milvus.dual_write=true` 现对应真实代码；但**开启 = 需单独授权的迁移动作**，且仍受 §5/§9 其余门禁约束。见文末"实现落地记录"。 |

结论性判定：**v3 集合的"建立 + 单目标写入 + 字段判据"当前代码即可支撑；"同一写扇出到
v2 与 v3 两个 collection"当前代码完全不支持。**

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

---

## 8. 明确不做（本方案与本轮的边界）

- 不写任何 v3/生产数据、不切配置、不跑 canary。（`dual_write` 的**代码实现**已由后续轮次落地并默认关闭，见文末落地记录；本轮及该后续轮均不产生真实 v3 数据。）
- 不推荐"现在就设 `milvus.dual_write=true`"——代码虽有，但真实开启仍是迁移动作，须先满足
  §9 全部前置（§5.1 canary runner 落地、§7 C3 处置归零、真实 v3 collection、单独授权）；
  未达前置就开启 = 把脏/未校验投影写入 v3，属危险操作。
- 不开 M5-B1，不共享 v3 写入。
- 不 push、不动 main；本文件后续修订落在 `m5/dual-write` 分支（原审计轮 §8 表述针对
  `audit/m5-gate0-coverage`，此处按当前分支更新）。

---

## 9. 进入执行前必须先满足的条件（阻塞清单）

1. ✅ `dual_write` 在后端实现并带自动化验证（S1 前置，责任：后端）——**已满足**：见文末
   "实现落地记录"。仅解锁"代码前置"，不等于可开启真实双写；开启仍受 2/3/4 与授权约束。
2. 授权运行 §5.1 合成 schema canary（写 Milvus，需单独批准）。
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

## 10. 与既有章节的对应

- 集合 schema 与拒写：§8.5（本文档 §3）。
- 迁移顺序（表/索引/backfill）：§14。
- 状态机与禁止态、回滚五步：§16.1（覆盖 §15.4 的 A/B/C/D）。
- backfill 失败恢复时序与 dedupe/retry：§16.2 / §16.3（迁移期局部重建的部分失败处置）。
- citation current/indexed revision 与双投影最新语义：§11 / §16.4。
- 双 KB、并发、旧任务覆盖、部分失败、stale 验收：§12。

本方案不改变上游设计的任何判据，只补充"当前代码 vs 设计要求"的真实缺口核对与 canary 分类。
