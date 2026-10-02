# M5-B0 B0-1 Audit Rework Acceptance Record

Date: 2026-10-02

## Decision

The B0-1 audit rework is implemented and evidenced in isolated SQLite tests and
one disposable real PostgreSQL/Neo4j/Milvus v3 namespace. Shared production
migration is **not** executed in this change, so the shared environment is not
claimed as `CLOSED`.

No M5-B1 Go API was started. Shared dev was not touched, and no shared Milvus
v2 collection was changed.

## Evidence layers

### 1. Isolated contract/state-machine evidence

These checks use temporary SQLite databases and controlled graph/vector test
doubles. They prove code contracts and state transitions only; they are not
production evidence.

```text
python backend/tests/check_b0_reindex_chunks.py                         exit 0
python backend/tests/check_m5a_revision_backfill.py                     exit 0
python -m py_compile backend/services/vector_store.py \
  backend/admin/backfill_chunk_revisions.py \
  backend/services/chunk_projection_reindex.py \
  backend/services/chunk_projection_state.py \
  backend/tests/check_b0_reindex_chunks_live.py                         exit 0
```

Covered contracts include:

- fresh second recheck missing a current row becomes `current_moved`; the
  delete race performs no Neo4j/Milvus write;
- `content_revision` is accepted only as an explicit Milvus `INT64`, with a
  typed schema error for missing/invalid fields;
- Milvus upsert acknowledgement status/count is checked, and partial or
  unknown writes become `failed`, never `indexed`;
- backfill uses shared CAS state writes, checks `rowcount == 1`, and aggregates
  document state, including the all-targets-excluded path.

### 2. Disposable real-space readback evidence

Command:

```text
$env:PYTHONPATH="backend"
python backend/tests/check_b0_reindex_chunks_live.py --confirm            exit 0
```

The harness creates a unique synthetic KB and a unique
`graphinsight_chunks_v3_*` collection. It uses a deterministic local test
vector, runs the Python worker, then reads back the real Neo4j `Chunk` and the
temporary Milvus collection. Both readbacks assert `text`, `kb_id`,
`tenant_id`, `project_id`, `content_revision`, and vector presence/dimension.
The harness independently cleans PostgreSQL rows, Neo4j nodes, and the Milvus
collection, then asserts zero residuals for each resource. It does not call an
external embedding service.

### 3. Shared production migration

Not executed. There was no write to shared dev, no write to a v2 collection,
and no M5-B1 Go API startup. A future production migration requires separate
authorization and evidence; this record must not be read as shared-production
acceptance.

## Implementation boundary and risks

- The graph leg rebuilds the `Chunk` text projection and revision only. Entity
  and relationship extraction remains outside B0-1 by contract.
- Real-space evidence uses a disposable namespace and deterministic vectors;
  it does not prove a shared collection migration.
- Runtime error details are returned by the worker result path; a future audit
  may additionally require persisting all `outcomes/counts` into the job log
  for post-failure diagnostics.

## Commits

- `df29a72 fix(backend): harden B0 projection and Milvus contracts`
- `373544e test(admin): cover B0 live readback and backfill state semantics`
- `44393e7 fix(test): stabilize disposable Milvus readback`
- This acceptance record is the documentation commit that follows these three.

## Auditor wording

B0-1 审计返工已完成并按三层证据记录：`df29a72` 修复 worker 第二道复核、
Milvus v3 `content_revision INT64` 契约、真实 upsert 数量校验和 backfill
CAS/文档聚合；`373544e` 补齐 current 删除竞态、缺字段/错误类型、部分写入、
直接 backfill 聚合、真实 Neo4j/Milvus 临时空间读回及三类资源清理残留断言。
SQLite 隔离验收与真实临时空间读回均 exit 0。未启动 M5-B1 Go API，未触碰
共享 dev，未修改 v2 collection；共享生产迁移仍未执行，不能据此宣称共享生产
`CLOSED`。当前未 push。
