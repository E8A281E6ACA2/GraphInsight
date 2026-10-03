# M5 共享 Milvus v3 迁移方案（仅设计，零执行）

状态：**DESIGN ONLY / ZERO WRITES**（2026-10-03）
适用仓库：`E:\projects\GraphInsight`（与 aistudio 无关）
上游权威：`docs/ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md` §8.5 / §14 / §15.4 / §16.1 / §16.2
执行边界：本文件不触发任何 Milvus/Neo4j/DB 写入，不切配置，不建/删 collection，
不跑 canary。所有写步骤标 `GATED / NOT-EXECUTED`，须逐项显式授权后才可执行。

---

## 0. 一句话结论

真实 v3 迁移被一个**当前代码不存在的能力**阻塞：`dual_write`。§16.1 的 S1（同一生产写
同时落 v2+v3）在代码里没有实现路径，因此**在 `dual_write` 实现并验证之前，禁止进入 S1、
禁止对任何真实 KB 执行 v3 迁移、禁止宣称 canary 闭环**。本方案只把"现在能做的"（隔离的
单目标 schema 建集合验证）与"必须先补代码才能做的"（双写/切换）分开摆明，并给出各自的
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
| **`dual_write` / `dualwrite` / `dual-write`** | **全 `backend/` 非文档零命中** | **§16.1 S1 双写在代码中不存在。`milvus.dual_write=true` 目前只是设计要求，不是可用能力。** |

结论性判定：**v3 集合的"建立 + 单目标写入 + 字段判据"当前代码即可支撑；"同一写扇出到
v2 与 v3 两个 collection"当前代码完全不支持。**

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
  3. 校验 v2_count == v3_count（per-KB，见 §6）；
  4. 读源切回 v2 配置 + 重启；
  5. 解除写冻结。
  回滚只动配置与读源，**不删 v3**。

每步都写清责任方与判据；未达判据即停在当前态，不推进。

---

## 5. 两类 canary 的范围与可行性（不得混谈）

### 5.1 §8.5 单目标 schema canary（当前代码可做，但仍 GATED）
目的：在**隔离的合成 KB**上证明——v3 能被现网代码建出、`content_revision` 是显式 INT64、
upsert→直接读回 revision 正确、幂等重跑不翻倍。

- **运行形态（硬约束）：必须在独立进程 / 专用一次性脚本里跑，自带一份指向合成
  collection 的连接配置；绝不修改共享 runtime config——不得 flip 现网服务的
  `vector_store.collection`、不得热改配置中心、不得改任何被生产读写的单例。** 现网代码只是
  被这段脚本"以独立实例"复用（`VectorStore(...)` 传入合成 collection 名），生产进程全程不动。
- 作用域：合成 `kb_id`（如 `canary-synthetic-<rand>`）+ 合成 collection（如
  `graphinsight_chunks_v3_canary_<rand>`），**无任何生产读流量指向它，也不与生产 collection
  同名**。
- 读写同源：该合成 collection 读源=写源=同一个 v3 目标，不违反 §16.1 不变量（生产读写全程
  停在 v2、配置未变）。
- 仍需授权：它**确实向 Milvus 写入**（建集合 + upsert 合成数据），故标
  `GATED / NOT-EXECUTED`，本文档不执行；执行前后必须断言共享 config 文件与生产 collection
  字节级零变化。
- 它**不**证明双写、不证明切换安全——只是"隔离进程里建集合与字段契约"的冒烟，且必须可被
  事后销毁（只 drop 合成 collection，绝不碰 v2 / 生产 v3 目标）。

### 5.2 双写 / 切换 canary（被阻塞，禁止宣称闭环）
目的：证明 S1 双写一致性与 S2 切换后检索无损。
- **依赖 `dual_write`，当前不存在。** 在实现并验证前，5.2 无法运行，也**不得**以任何形式
  宣称"canary 已闭环/迁移可回滚"。
- 5.1 通过也**不能**外推为 5.2 通过——两者证明的不变量不同。

---

## 6. 验收判据（写冻结水位 / 一致性对账，per-KB）

对每个真实 KB（迁移窗口内，非合成 canary），逐 KB 记录：

- `v2_count == v3_count`（chunk 级计数，按 `kb_id` 过滤，非全库总量）。
- 抽样 N 条 `chunk_id`：v3 的 `content_revision` 为 int64 且等于该 chunk 投影版本；向量
  维度与 v2 一致。
- 幂等：同一批次 upsert 重跑，v3_count 不增、无重复主键。
- 直接读回（Neo4j/Milvus 双侧）：QA citation 的 current/indexed revision 语义与 §11 对齐，
  无 stale 命中回升。
- **对账口径改写**（替换"v2 零改动"）：真实 KB 在 S1 双写窗口内 v2 **本就持续被写**，"v2 前后
  count/revision 不变"不是有效判据、会自相矛盾。改用**写冻结水位对账**：
  - 迁移前记录该 KB 的**冻结点** `W0 = max(content_revision)`（及 per-KB chunk 计数快照）；
  - 切读前对每个 KB 施加**写冻结**（§16.1 回滚五步里的同一步），使 `content_revision` 停在
    水位 `W1`，禁止新写进入；
  - 对账 `v3 已覆盖到 W1`：per-KB 逐 chunk `v3.content_revision == 当前投影 revision`，且
    `v3_count == W1 时的期望 chunk 数`；
  - **v2 只需作为"到 W0/W1 为止的合法回滚点"被验证可读**，不要求 count 不变——校验"v2 能
    读到 ≤W1 的全部历史"即可，不比对"前后是否相等"。
  - 解冻后新增 revision 由 `dual_write` 保证 v2/v3 同步，无需再断言 v2 静止。
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

- 不实现 `dual_write`、不写任何 v3/生产数据、不切配置、不跑 canary。
- 不开 M5-B1，不共享 v3 写入。
- 不推荐"现在就设 `milvus.dual_write=true`"——该配置无对应代码，设了也是空开关，属
  §0/§2 所述危险操作。
- 不 push、不动 main；本文件的提交只落 `audit/m5-gate0-coverage` 分支。

---

## 9. 进入执行前必须先满足的条件（阻塞清单）

1. `dual_write` 在后端实现并带自动化验证（S1 前置，责任：后端）。
2. 授权运行 §5.1 合成 schema canary（写 Milvus，需单独批准）。
3. C3 处置清单（§7）中所有待迁 KB 的 blocked/orphan/unrecoverable/scope_* 归零或有批准
   的例外。
4. 双写一致性 + 切换 + 回滚五步的验收脚本就绪（§6 判据落到可执行断言）。

以上任一未满足，真实 v3 迁移保持关闭，M5 gate 不宣布通过。

---

## 10. 与既有章节的对应

- 集合 schema 与拒写：§8.5（本文档 §3）。
- 迁移顺序（表/索引/backfill）：§14。
- 状态机与禁止态、回滚五步：§16.1（覆盖 §15.4 的 A/B/C/D）。
- backfill 失败恢复时序与 dedupe/retry：§16.2 / §16.3（迁移期局部重建的部分失败处置）。
- citation current/indexed revision 与双投影最新语义：§11 / §16.4。
- 双 KB、并发、旧任务覆盖、部分失败、stale 验收：§12。

本方案不改变上游设计的任何判据，只补充"当前代码 vs 设计要求"的真实缺口核对与 canary 分类。
