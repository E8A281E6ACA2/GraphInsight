# M5-A → M5-B 依赖方案：`reindex_chunks` 消费闭环（v2）

日期：2026-10-02　状态：**M5-B0 已实现；共享生产迁移未执行**　上游文档：`ENTERPRISE_M5_CHUNK_REVISION_DESIGN.md`（下称"设计书"）
适用前提：M5-A 共享生产验收仍未通过；M5-B1 完整 Go API 保持冻结。本文记录 B0 当前实现、临时空间证据和剩余迁移门，不改动任何验收阈值。

---

## 0. TLDR（结论先行）

1. **`reindex_chunks` 已由 Python 侧既有的进程内 job worker 消费**（`backend/admin/services/job_service.py` 的 `admin-job-worker` 线程 + `backend/services/job_runtime.py` 分发），不是新建独立进程，也不是 Go。B0 不新增 Go API。
2. **关键结论：门收敛不需要 Go API。** 入队方（backfill）与执行方（Python worker）都在 Python，Go 只提供人工触发入口和列表/重试界面。所以存在一个不违反"M5-B Go API 冻结"的最小切片（本文 §5 的 **M5-B0**）。
3. 收敛需要两个条件同时成立，缺一不可：**C1 Milvus v3 提供显式 `content_revision` 字段**（解决"新写入"腿）+ **C2 worker 能执行 `reindex_chunks`**（解决"已有 current 行但投影未收敛"腿）。只做 C1 收敛不了任何 `needs_reindex_targets`；只做 C2 在 v2 上无法把向量标成 `indexed`（会被 §8.5 冻结拦成 `revision_field_absent`）。
4. 即使 C1+C2 全绿，**仍有一类 target 永远不收敛**，必须人工处置而非重跑：`blocked`、孤儿 revision、`unrecoverable`、`scope_*` 冲突。门 CLOSED 的判据里必须把它们显式排除在"reindex 能解决"之外。

---

## 1. 现状事实（证据，非推断）

| 环节 | 今天的真实状态 | 证据 |
| --- | --- | --- |
| job 存储 | 单表 `admin_jobs`，含租约列 `claimed_by/claim_expires_at/last_heartbeat_at` 与 `targets_hash` | `backend/admin/models.py:159-182`；`backend/admin/migrate_job_worker_lease.py:36-48` |
| 唯一执行器 | FastAPI 启动时拉起的进程内线程，轮询间隔默认 2s | `backend/main.py:157`（关闭 :168）；`job_service.py:233-256,289-301` |
| 可执行类型 | Python 集合已包含 `reindex_chunks`，Go 集合仍冻结未接入 | `job_service.py:38-43`；分发 `job_runtime.py:87-96` |
| `reindex_chunks` 消费方 | **已存在**。Python worker 领取 backfill 入队的 job，并执行四道 current 复核、Neo4j/Milvus 投影和 CAS 状态回写 | `job_service.py:361-369`；`chunk_projection_reindex.py:450-704` |
| 入队方 | backfill 按 `doc_id` 分组直连 SQL `INSERT ... ON CONFLICT (job_type,kb_id,targets_hash) DO NOTHING`，旁路 `job_service.create_job` | `backend/admin/backfill_chunk_revisions.py:816-866`（`JOB_TYPE` 定义 :75） |
| Go 侧 | 只做 INSERT/UPDATE 与列表/重试/取消，**不执行任何 job**；靠 HTTP 唤醒 Python | `go-backend/internal/adminstore/jobs.go:26-30,131-163`；`admin_jobs_native.go:686-697,727-751`（唤醒目标 `backend/admin/api/endpoints/jobs.py:23,46-48`） |
| 现有 `reindex` job 的语义 | 只重建 Neo4j **全文索引**（基础设施），不碰 Milvus、不按 kb 收敛投影——不能被当作 `reindex_chunks` 复用 | `job_runtime.py:252-301` |
| 投影状态字段 | `chunk_revisions.graph_status/vector_status` + `*_content_revision`；backfill 与 worker 共用 `chunk_projection_state.update_projection_state` | `chunk_projection_state.py:24-66`；`backfill_chunk_revisions.py:768-789`；`chunk_projection_reindex.py:597-628` |
| 向量写入能力 | v3 路径要求显式 `content_revision INT64`，实际 upsert 数量/状态必须匹配；v2 缺字段仍拒写 | `vector_store.py:16-82,203-281` |
| 图写入能力 | `DocumentGraphService.build_graph` → Neo4j Cypher + `retrieval_orchestrator.index_chunks` | `backend/services/document_graph_service.py:238,571-573,637,668,797-803` |
| v2 上向量写入的现状 | backfill 检测到 collection 无 `content_revision` 字段即**整批拒写**，投影保持 `pending` 并打 `MILVUS_REVISION_FIELD_ABSENT` | `backfill_chunk_revisions.py:316-327,397-410,1050` |
| 门判据 | `needs_reindex_targets == []` 且 `blocked == 0` 且无 `unrecoverable/scope_unresolved/scope_mismatches` → CLOSED | `backfill_chunk_revisions.py:869-888`；单投影判定 :582-608 |

**剩余熵增点**：Python 的 `reindex_chunks` 白名单和消费链已验证；Go 侧 `supportedJobTypes` / `adminJobTypeFromPath` 仍冻结，B1 开工前必须增加跨语言 job_type 精确对账守卫，避免后续出现"能入队、能显示、永不执行"的静默状态。

---

## 2. 问题一：`reindex_chunks` 由哪个 worker 消费

**决定：由既有 Python 进程内 worker 消费，落点在 `job_runtime.py` 新增 `execute_reindex_chunks`，投影重建逻辑放 `backend/services/chunk_projection_reindex.py`（新模块）。**

理由（按熵减排序）：

1. 租约、心跳、超时、失败重试、stale 恢复、审计日志这套机制已经存在且被三个 job 类型验证过（`job_service.py:303-355,412-466,468-502,639-684`）。新建 worker = 再造一份这套东西。
2. 投影重建要调的能力（Neo4j 驱动、`vector_store`、`embedding_service`、解析产物读取）全在 Python；Go 走 HTTP 反向调 Python 才能干活，等于加一跳无收益的耦合。
3. 仓库无 Celery/APScheduler/消息队列/docker-compose worker 服务（已核对），引入外部队列属于超范围变更。

明确**不做**：不新增独立 worker 进程；不把执行逻辑写进 Go；不让 HTTP 请求同步等待重建（AGENTS.md 工作流规范：重操作优先进任务中心，前端不依赖长请求）。

约束继承（设计书 §8.2 / §15.5 / §16.3，逐条落进 worker 实现）：

- 只处理 `target_revision == 当前 current.content_revision` 的 target，其余记 `OUTDATED_SKIPPED`，不写索引。
- 每个投影写入前后各复核一次 current（四道复核，§6.1/§8.3），防 revision 2 的旧任务覆盖 revision 3。
- 幂等：已 `indexed` 且版本一致的投影不重做；部分失败只补未完成侧。
- fail-closed：能力未配置 → `skipped`（不伪装 `indexed`）；写入异常 → `failed` 并让 job 走 retry，超过 `max_retries` 判 failed。
- 作用域：payload 的 `kb_id/tenant_id/project_id` 必须过 `require_payload_scope`（`job_runtime.py:74-84`），且与 `chunk_revisions` 行、Milvus/Neo4j 记录三方一致，冲突零写入。

---

## 3. 问题二：job service / worker / Go API 的最小接入边界

### 3.1 M5-B0（Python-only，最小闭环，不碰 Go，已实现）

截至 2026-10-02，以下实现和验收已完成；证据见
`docs/ENTERPRISE_SPRINT_M5B0_ACCEPTANCE_2026-10-02.md`。真实证据只覆盖唯一临时 v3
collection，不代表共享生产迁移。

| # | 文件 | 最小改动 | 不改会怎样 |
| --- | --- | --- | --- |
| 1 | `backend/admin/services/job_service.py:38-43` | Python `SUPPORTED/RUNNABLE/KB_SCOPED_JOB_TYPES` 已加入 `reindex_chunks` | 已完成，隔离 worker 接线测试通过 |
| 2 | `backend/services/job_runtime.py:87-196` | 独立分发到 `execute_reindex_chunks` | 已完成，未复用旧 `reindex` |
| 3 | `backend/services/chunk_projection_reindex.py` | current 复核、Neo4j/Milvus 投影、CAS 回写、文档聚合 | 已完成；实体/关系抽取仍不在 B0 范围 |
| 4 | `backfill_chunk_revisions.py` + `chunk_projection_state.py` | backfill 与 worker 共用状态回写，检查 CAS rowcount 并聚合文档状态 | 已完成；聚合异常现 fail-closed |
| 5 | 测试 | SQLite 矩阵 + 唯一临时 v3 真实读回 | 已完成；共享生产迁移和 C3 治理仍未完成 |

`KB_SCOPED_JOB_TYPES` 一条尤其重要：`reindex_chunks` 的 `kb_id` 必须真实存在且 active（`job_service.py:40-42` 现语义），否则一个手滑的 payload 会把不存在 KB 的 job 反复重试。

### 3.2 M5-B1（Go API，B0 绿了才开工）

- `adminstore/jobs.go` 的 `supportedJobTypes` 与 `adminJobTypeFromPath` 的映射同步加 `reindex_chunks`；这是**白名单补漏**，不是新设计。
- 设计书 §7 的 5 个端点（chunk 读 / PATCH / rollback / reindex-chunks / reindex-document）按原方案实现；payload 形态沿用 §15.2/§15.9 的统一 `targets:[{chunk_id,target_revision}]`，不新增第三种形态。
- Go 建 job 后仍走 `POST /api/internal/jobs/wake`（`admin_jobs_native.go:727-751`）唤醒 Python，不引入新通信方式。
- backfill 直连 SQL 旁路 `create_job` 的口径差（§1 表格）在 B1 必须收：要么 backfill 改调 `job_service.create_job`，要么把 `create_job` 的作用域校验下沉为共享函数供两条路径复用。**推荐后者**——backfill 的 `ON CONFLICT DO NOTHING` 幂等语义要保留，改调用点会引入行为变化。

### 3.3 边界外（本方案明确不负责）

前端 chunk 编辑界面、QA 引用侧双投影展示（设计书 §15.6）、监控告警接线——都排在 B1 之后。

---

## 4. 问题三：Milvus v3 如何提供 `content_revision`

沿用设计书 §8.5 + §15.4 + §16.1 已冻结的口径，本文只补"实现顺序"和当前代码的缺口：

1. **schema**：临时 v3 collection 已按 `vector_store.py` 契约创建，包含显式 `content_revision INT64`；共享 v3 迁移仍未执行。**禁止在 v2 改 schema，禁止自动 drop v2。**
2. **落点**：`ensure_collection` 增 v3 分支与"缺 `content_revision` 字段则拒写"守卫（现有 `_collection_has_kb_fields` 同模式，`vector_store.py:328-342`）；`upsert_chunks` 写显式 `content_revision`，且不得被 `chunk.metadata` 覆盖（沿用 :190-193"作用域以显式字段为准"的写法）。
3. **配置**：新增 `milvus.dual_write`（默认 false）+ `milvus.collection` 切 v3。新增配置项必须同时处理默认值、环境变量来源、脱敏与后台展示（AGENTS.md 后端规范 9）；`config()` 今天只归一化 `collection`（`vector_store.py:56-73`），dual_write 需要在这里显式化，不能散在调用点读 env。
4. **读写的唯一切换点**：S0(v2/v2) → S1(写 v2+v3，读 v2) → S2(读写都 v3，重启生效) → S3(稳态)。禁止"只写 v3 + 只读 v2"（§16.1 不变量）。
5. **backfill 自动跟随**：`_milvus_collection_name()` 已经和线上读写路径共用 `vector_store.config()`（`backfill_chunk_revisions.py:280-303`，活栈实测过历史配置名坑），所以配置切到 v3 后 backfill 的 `revision_field_absent` 拒写腿会自动消失——这条链**不需要**为 v3 单独改代码，也不需要新增第二份 collection 解析逻辑。
6. 待修正的实现口径：`_backfill_milvus` 里 `content_revision` 仍硬编码为 `1`（:416）。S1 双写期新 chunk 确实是 revision 1，但**回放/回滚场景**必须改成取该行 `current.content_revision`，否则会把已有 revision 3 的 chunk 写成 1，属于后续 #73 风险；本轮不扩大 B0 范围。

---

## 5. 问题四：`needs_reindex_targets` 何时真正收敛到 CLOSED

### 5.1 收敛的定义（可执行判据）

对某个 kb 复跑 `python backend/admin/backfill_chunk_revisions.py --kb <id> --dry-run`，得到：

```text
[inventory] ... needs_reindex_targets=0 blocked=0 unrecoverable=0 scope_unresolved=0 scope_mismatch=0
[gate] CLOSED mode=dry-run needs_reindex=0 blocked=0
exit 0
```

`exit 0` 且 `CLOSED` 同时成立才算收敛。任一 `needs_reindex>0` → exit 3（门 OPEN）；`scope_*` / `unrecoverable` → exit 2。

### 5.2 四条必要条件与各自的责任方

| 条件 | 责任方 | 现在能不能取证 |
| --- | --- | --- |
| **C1** v3 有显式 `content_revision`，向量侧允许被标 `indexed` | `vector_store` + v3 配置迁移 | 临时 v3 已验证；共享 v3 未迁移，不能作为生产证据 |
| **C2** `reindex_chunks` 被 worker 真正执行并回写投影 | Python worker / `chunk_projection_reindex.py` | 已在 SQLite 和临时真实空间验证；共享生产尚未执行 |
| **C3** `blocked` / 孤儿 revision / `unrecoverable` / `scope_*` 归零 | 人工数据治理，**不是 reindex 能解决的** | 部分。需逐 kb 出清单后人工处置（删孤儿行或补证据源） |
| **C4** 幂等重跑不新增行、不重复补写、入队复用 `targets_hash` | backfill + worker 两侧共用 §16.3 规则 | 已在 M5-A 取证（同一批 targets 第二轮复用） |

**只有 C1∧C2∧C3 同时成立，CLOSED 才是可取证的。** 当前只证明了临时空间的 C1∧C2；共享生产的 v3 迁移和 C3 人工治理仍未完成，因此共享生产门仍保持 OPEN。

### 5.3 收敛的最短可取证路径

```text
T0 当前            ：B0 worker 与临时 v3 已验证；共享 v3 尚未迁移，C3 清单尚未清零 → 共享门 OPEN
T1 只做 C1（v3）   ：新 chunk 的向量腿可写 indexed；已有 current 行的 needs_reindex 集合不变 → 门仍 OPEN
                    ⇒ 单独做 C1 对收敛无收益，不要押注这条
T2 只做 C2（worker）：graph 腿可收敛；vector 腿在 v2 上被拒写 → 门仍 OPEN
T3 C1 + C2         ：两类 target 都可收敛；剩余 blocked/孤儿/unrecoverable/scope_* 显式列清单
T4 C3 人工清零     ：复跑 backfill 得 CLOSED + exit 0 ⇒ 此时才允许宣布 M5-A 的"门收敛"腿通过
```

T3→T4 之间必须有一份**逐 kb 不可收敛清单**输出（脚本或 SQL 报告，含 `chunk_id` + 归类原因），否则运维无法区分"等 reindex"和"必须人工"。当前 inventory 已有这些桶（`orphan_revisions` / `unrecoverable` / `blocked` / `scope_mismatches`），缺的只是把它们打印成可执行清单——这条是 B0 之后的 C3 治理任务。

### 5.4 与 M5-A / M5-B 冻结令的关系

- **M5-B0（§3.1）不触碰 Go**，不违反"M5-A 未验收前不得开始 M5-B Go API"。它是让 M5-A 唯一断点变得可取证的必要前置，不含任何新 API 面。
- B0 已在不启动 Go API 的前提下完成；其临时空间证据不等于共享生产验收。
- B1（Go API）继续保持冻结，直到：P0 聚合异常修复、共享环境只读 C3 清单、隔离/临时 v3 迁移证据和文档同步均由审核确认。

---

## 6. 风险登记

| 风险 | 影响 | 缓解（必须与实现同批交付） |
| --- | --- | --- |
| 4 份 `job_type` 白名单漂移 | 静默 pending / 或 Go 能建 Python 不跑 | 跨语言 job_type 精确对账静态守卫（沿用 R2-2 RBAC 对账同一手法） |
| worker 与在线摄取路径写同一投影 | 互相覆盖、状态闪烁 | 四道 current 复核（§8.3）+ `target_revision` 语义；worker 只按 chunk 粒度写，不做文档级全量重扫 |
| S1 双写期 v3 upsert 持续失败 | 迁移卡住 | §16.1 写冻结降级：回 S0 并暂停编辑/重建入口返回 503 `INDEX_UNAVAILABLE`，禁止"v2 只读 + 新写丢弃" |
| 进程内 worker 随 API 重启丢执行中 job | 投影半写 | 既有 stale lease 恢复（`job_service.py:303-355`）+ 投影写入幂等；不引入新机制 |
| 误把 `reindex`（全文索引）当 `reindex_chunks` 复用 | 语义混淆、门永不收敛 | 两者保持不同 job_type，文档与本表明确 `job_runtime.py:252-301` 与 chunk 投影无关 |

---

## 7. 需要用户拍板的三个点

1. **B0 是否授权开工**（改 `RUNNABLE_JOB_TYPES` + 新增投影重建执行体）。不授权则 M5-A 的"CLOSED 腿"永远无法取证，只能以"CLOSED 结构性不可达"提交审计。
2. **v3 迁移窗口**：C1 需要在 dev Milvus 上真实建 collection 并做 S1 双写——属于共享环境变更，需明确授权与回滚预演时间点。
3. **不可收敛清单的处置口径**：孤儿 revision 是删行、还是保留并永久标注 blocked，直接影响 M5-A 能否宣布 CLOSED。
