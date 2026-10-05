# Milvus v3 canary 前置审计（Phase 4 开工条件）

编制时间：2026-10-05。分支：本地 `main`（integration worktree，HEAD 以 `git rev-parse --short HEAD` 现取）。
审计对象：`vector_store` / `backfill` / `worker` 三条写路径是否满足两条 canary 前置判据——
① 显式 `content_revision INT64` schema 检查；② 真实 upsert 返回**数量与状态**校验。
**本审计只读代码与已安装库，不连接任何 Milvus、不写入、不建集合。**

## 0. 结论

三条写路径**均已具备**两条判据（表见 §1–§3），口径统一来自 `services/vector_store.py` 的同一份
helper。**但 canary 仍不可省**：审计发现四处发现（§4），其中 F2/F4 必须由 Phase 4 在真实
Milvus 上显式取证后才能宣称"写入可读回"。**真实 Milvus 写入需书面授权，本轮未执行。**

## 1. 判据①/② 的三路径对照（file:line 均为现树实测）

| 组件 | 显式 INT64 schema 检查 | upsert 返回数量/状态校验 |
|---|---|---|
| `services/vector_store.py`（写路径根） | `content_revision_field_is_int64`（`:34-48`，字段名精确匹配 + `_is_int64_field` 判 `DataType.INT64`/`"INT64"`）；`has_content_revision_field`（`:338`，运行期探测 + 按名缓存，探测失败按"不支持"）；建集合时显式写入 `content_revision INT64`（`ensure_collection`，`add_field` 在 `:234`） | `require_upsert_count`（`:51-81`）：解析 `upsert_count`/`insert_count`/`mutation_count`，status code 非 0 抛错，**解析不到数量即抛**，数量不等于 expected 即抛；primary 腿（`:311-312`）与 v3 影子腿（`:326-335`）都调用 |
| `admin/backfill_chunk_revisions.py`（M5-A backfill） | `_backfill_milvus` 前置 `_milvus_has_revision_field`（`:473`）→ 无显式字段则整批记 `revision_field_absent`，**不写不可靠版本**；helper 转发到同一 `content_revision_field_is_int64`（`:389-392`） | `require_upsert_count(client.upsert(...), len(rows))`（`:504-506`）；embedding 数量不符或 upsert 异常 → 逐 chunk `failed`（`:486-510`） |
| `services/chunk_projection_reindex.py`（M5-B0 worker） | 依赖 `upsert_chunks` 内部同判据边界守卫（文件头契约 `:21` 明示，函数注释 `:362-363` 明示），写前先 `ensure_collection`（`:390`）；**worker 自身不做 INT64 直判** | `written = vector_store.upsert_chunks(...)`（`:412`）；`written == len(chunks)` 才置 `indexed`，否则 `write_failed`（`:418`），异常同样 `write_failed`（`:413-417`） |

判据实现**单一真相源**：`content_revision_field_is_int64` / `require_upsert_count` / `upsert_chunks`
三处都在 `services/vector_store.py`，backfill 与 worker 都收敛到它，未出现第二份判定逻辑
（`backfill` 的 `_milvus_has_revision_field` 仅是转发）。

## 2. `require_upsert_count` 的真实返回形态取证（pymilvus 3.0.2 实测）

- `MilvusClient.upsert` 返回 `OmitZeroDict({"upsert_count": N, "ids": [...], "cost": ...})`
  （pymilvus `milvus_client/milvus_client.py`，`_upsert` 的 `return OmitZeroDict(...)`）。
- `OmitZeroDict` 是 **dict 子类**，零值只在 `__str__`/`omit_zero_len` 被过滤，**底层 dict 仍保留
  `upsert_count=0` 键**（实测 `inspect.getsource(OmitZeroDict)`）。因此：
  - `result.get("upsert_count")` 能取到 0 → `require_upsert_count` 判 `count mismatch` 抛错，
    **零确认写入不会被误判为成功**（fail-closed 成立）。
  - dict 路径无 `status` 键 → status 分支跳过，不误报；ORM 路径（含 `status.code`）仍被覆盖。
- `MilvusClient.upsert` 的 `ids` 为返回主键列表，**不参与**数量判据（数量才是确认口径）。

## 3. 写路径的边界守卫（与 canary 直接相关）

- **维度冲突不静默重建**：`ensure_collection` 发现既有 collection 维度与当前 embedding 维度不一致
  时抛 `VectorStoreSchemaError`（`:196-208`），不做 drop/rebuild。
- **旧 schema 拒写**：既有 collection 缺 `kb_id/tenant_id/project_id` 时抛错（`:209-214`）。
- **§8.5 双守卫**：primary 缺显式字段且**无 v3 影子腿** → 拒写（`upsert_chunks` `:271-279`）；
  有影子腿时 primary 剥版本写、版本由 v3 承载（`:303-310`）；v3 腿先 `ensure_collection`
  （新建必带显式 INT64）再守卫（`:319-325`），upsert 失败 fail-closed 上抛（`:326-335`）。
- **backfill 自建 client**：`_milvus_client()`（backfill `:378-386`）每次新建 `MilvusClient`
  （uri/token/db 取自 settings），**不复用 `vector_store` 单例**——与 §5.1 要求的"绕开单例"天然一致，
  canary 可据此设计。

## 4. 发现清单（只报不改；F2/F4 是 canary 的必验项）

| ID | 事实（file:line） | 影响与 canary 要求 |
|---|---|---|
| F1 | worker 自身不做 INT64 直判，依赖 `upsert_chunks` 边界守卫（`chunk_projection_reindex.py:362-363`） | 口径一致，可接受；canary 需**实测 worker 路径**（非仅 backfill）在无字段集合上的拒写，避免"守卫只被单测覆盖" |
| F2 | worker 无**写入后**回读校验：`_existing_milvus_fields` 的 `client.query` 在写前用于合并字段（`:265-350`，query 在 `:319`），`indexed` 依据是 upsert 返回数量 | **canary 必须补写入后读回**（text/kb_id/tenant_id/project_id/content_revision/vector），否则"数量确认"不等于"数据可读回" |
| F3 | `_load_milvus_chunks` 在 schema 探测失败时回退请求含 `content_revision` 的字段集（backfill `:398-417`、`:429-431`） | 对无该字段的 v2 直接 query 会报错；当前被 §8.5 先拒写挡住，属**发现级**（读取侧口径），本轮不改 |
| F4 | `require_upsert_count` 的数量解析依赖真实返回键名（`upsert_count`/`insert_count`/`mutation_count`） | canary 需**记录真实返回 dict 原文**（脱敏）以确认键名与数量语义，防库版本差异导致"解析不到就抛"的误判 |

## 5. Phase 4 canary 的强制要求（写进编排器判据）

1. **目标地址钉死**：只允许 `127.0.0.1:19531`（项目 compose 栈 `graphinsight-milvus`）。
   **`127.0.0.1:19530` 属其它项目（`data-stellarls-milvus-1`），严禁连接**；编排器须硬编码白名单并在启动前回显实测 URI。
2. **集合隔离**：只建 `graphinsight_chunks_v3_canary_<rand>` 形态的合成集合；
   **绝不触碰** `graphinsight_chunks_v2` / `graphinsight_chunks_v3`；绝不 flip 现网服务配置、不热改配置中心。
3. **写入内容**：合成 Chunk + 真实维度向量；`content_revision` 走显式 INT64 字段，不被 `metadata` 覆盖。
4. **写入后读回**：`text`/`kb_id`/`tenant_id`/`project_id`/`content_revision`/`vector` 逐项读回比对（补 F2/F4）。
5. **v2 零写入**：读 v2 计数前后不变；并断言本轮从未对 v2/其它集合发起 upsert/delete。
6. **flush / load / Strong consistency**：写入后 flush、load，并以 Strong 一致性查询读回。
7. **失败注入**：注入维度不符/集合缺失/数量不符之一，验证 fail-closed 且无半截数据。
8. **清理**：drop 合成集合并复核不存在；Milvus 集合清单、PG、Neo4j 均无残留；一次性容器销毁后复核。
9. **证据**：单 `RUN_ID`/`EXEC_TIME` 贯穿全部文件、JSON `json.load()` 可直过、meta 覆盖写、校验 fail-closed。

## 6. 未取证声明

- 本审计**未连接任何 Milvus**（含 19530/19531），未建集合、未写入、未 flush/load；
  §4 的 F1–F4 是**代码级**发现，不是真实运行结论。
- 真实 v3 建集合、显式 INT64 写入与读回、v2 零写入、切读源验证**均未执行**，
  属 Phase 4 书面授权后的范围；**M5-B1 继续冻结**，共享生产 Gate 继续 OPEN。
- 一次性 Milvus standalone 在本机起不来（内嵌 etcd 超时 panic，见迁移方案 §9.3），
  Phase 4 若需真实 Milvus 只能使用项目 19531；该事实已记入编排器前置检查。
