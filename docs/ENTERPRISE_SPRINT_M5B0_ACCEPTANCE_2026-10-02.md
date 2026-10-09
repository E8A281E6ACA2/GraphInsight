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
SQLite 隔离验收与真实临时空间读回均 exit 0。该历史记录当时未启动 M5-B1 Go API；当前代码另有受控 `/api/v1/admin/jobs/reindex-chunks` 提交链，但完整 B1 仍冻结。未触碰
共享 dev，未修改 v2 collection；共享生产迁移仍未执行，不能据此宣称共享生产
`CLOSED`。当前未 push。

## 2026-10-09 基线对账证据索引（不改写上述历史验收）

唯一当前入口：[能力—实现—验收对账](ENTERPRISE_IMPLEMENTATION_BACKLOG.md)。分支 `codex/m5-seed-v2-only`，HEAD `3a520b21e82b616b991f8a5c9e60c489dd8daab0`；本轮只改文档，未提交、未 push、未修改业务代码/CI/共享 dev。

### 本轮实际运行

| 检查 | 环境/命令 | 退出码与摘要 | 已验证/未验证边界 |
|---|---|---|---|
| 基线 | Windows PowerShell：`git branch --show-current; git rev-parse HEAD; git status --short --branch` | exit 0；开工仅未跟踪 output/、qodana.yaml | 与前轮3a520b2一致；两项未处理 |
| seed 隔离合同 | Windows、Python3.14.7；完整命令见唯一入口“本轮与历史验证记录”，通过现有 `_run_case` 运行 `m5_seed_shared_v2_contract` | 命令 exit 0，child exit 0，`passed=196 failed=0`，guard_pass=True | 临时文件系统、PG/Milvus 进程内假体；无数据库、无模型、未执行共享 seed；护栏仅Python socket覆盖 |
| Git 跟踪核对 | `git ls-files docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` | exit 0，命中文档 | 只证明已入Git，不证明契约或功能验收 |

计数勘误：保留脚本原始 `passed=196 failed=0` 数值；R6-4 悬空 symlink 场景在环境不支持时以 skip 记通过（`check_seed_shared_v2_contract.py:1267–1268`），本轮摘要未独立确认该场景，不能把196称为196项行为全部实际执行。测试/门禁未修改。

执行日志机器 UTC 日期为2026-10-08；阶段请求日期为2026-10-09，保留差异，不覆盖历史日期。

### 历史引用（本轮未重跑）

| 入口/记录 | 历史证据范围 | 副作用与本轮状态 |
|---|---|---|
| [M4-R1](ENTERPRISE_M4R1_ACCEPTANCE_REPORT.md) | 2026-09-29 scoped API/双KB/E2E与后续CI记录，版本身份见原提交表 | E2E上传、建图、删除；双KB脚本会建/改数据，不能直接认定隔离；本轮未执行 |
| 本记录 B0 live：`check_b0_reindex_chunks_live.py` | 2026-10-02 临时KB/真实PG/Neo4j/Milvus读回 | 创建、写入、清理临时真实空间；需授权，不在本轮执行 |
| [v3方案](ENTERPRISE_M5_MILVUS_V3_MIGRATION_PLAN.md) §9.9/§9.23 | 合成canary与独立容器非空取证，非共享KB迁移 | 创建/写入/删除合成集合与独立容器；外部原始证据本轮未读取/复验 |
| [回滚演练](ENTERPRISE_VERSION_ROLLBACK_DRILL_2026_09_30.md) | 旧scope/RBAC下限78b1f28，双模式版本腿 | 起停一次性网关、建立夹具；不证明当前M5回滚，本轮未执行 |
| [无模型soak](ENTERPRISE_PERF_SOAK_2026_09_30.md) | 360请求、无LLM/embedding，build提交即取消 | 创建取消probe job；不是只读容量测试，本轮未执行 |
| `run_release_acceptance.sh` / `run_backend_smoke_suite.py` | 现存发布入口，历史输出仅见对应日期记录 | 包含写入与清理；启动脚本/seed可能操作共享栈，本轮未执行 |
| `run_unified_boundary_guards.py` | 有现存Python socket护栏，历史28项通过不能当当前结果 | 本轮仅定向执行1项；全套、构建、Go、迁移smoke均未执行 |

### 文档最终检查

本轮已执行：`git diff --check` exit 0；修改文档仓库链接校验 `LINKS=47 BROKEN_OR_MACHINE=0`；最终 `git status --short --branch` 保留9个已修改文档及未跟踪 `.codebuddy/`、`output/`、`qodana.yaml`，均未清理/纳入。任何不存在的历史引用保留“本轮未找到”说明，不补写伪证据。

真实 Neo4j：未取证；Milvus：未取证；Go/Python运行态：未取证；生产闭环：未验收，Shared Gate OPEN、完整B1冻结。已验证边界仅为上述离线合同与静态文档对账；已知风险是历史外部证据未复验、当前远端CI/浏览器/真实模型容量缺证据。未push，未触碰共享dev。
