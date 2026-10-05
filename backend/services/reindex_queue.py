"""reindex_chunks 入队的唯一实现：targets_hash 规范化 + §16.3 冲突回读（M5 Wave 3）。

背景：§16.3 冻结的去重语义此前只在 `admin/backfill_chunk_revisions.py` 里落了半条
（`ON CONFLICT DO NOTHING` 把冲突记成 `reused`，从不回读既有行），而 build_graph 的
向量/影子失败路径完全不入队。两处各写一套 SQL 会让"同一批 targets 第二轮复用"这一
验收口径静默分叉，所以这里做成单一实现，backfill / build_graph 转交 / 任务中心提交
都必须走本模块。

复用面（**不新增迁移**，用户 2026-10-04 定案）：
- `admin_jobs.targets_hash VARCHAR(64) NULL` 与部分唯一索引
  `uq_admin_jobs_targets_hash (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL`
  （`admin/migrate_jobs_targets_hash.py` 建）；
- ON CONFLICT 谓词与索引谓词逐字一致（v3.2.1 冻结写法）。

§16.3 分支（锁后按既有行状态处理，一律不新建行）：
- pending / running / succeeded → `reused`（复用既有 job，不重复入队、不重入）；
- failed → `retry_count < max_retries` 时原地重置 `status='pending', retry_count+1` → `retried`；
  额度已用尽 → `rejected`（附 `reason='retry_exhausted'`），**禁止自动无限重试**；
- cancelled → 原地重置 `status='pending', retry_count=0`（人工取消不消耗配额）→ `reset`。

并发：Postgres 路径在冲突回读前 `SELECT ... FOR UPDATE` 锁既有行（§16.3 v3.2 收口）；
SQLite 无行锁语义，靠单写者事务 + 唯一索引兜底，因此锁只在方言为 postgresql 时下发。

不覆盖：Go 控制面的 reindex_chunks HTTP 入口。`go-backend/internal/adminstore/jobs.go:26-30`
的 `supportedJobTypes` 白名单仍无 `reindex_chunks`，`validateJobCreateRequest`
（`jobs.go:647-650`）在 INSERT 之前返回 `ErrJobValidation`，由
`go-backend/internal/httpserver/admin_jobs_native.go:758-761` 映射为 HTTP 400
`INVALID_BODY`；仓库内不存在 409 / `JOB_409` 的作业状态映射（NOT-IMPLEMENTED）。
本模块按 §16.3 不新增错误码，只把"超限拒绝"作为结构化结果返回给调用方。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy import text

JOB_TYPE = "reindex_chunks"

# §16.3 状态字面量（admin_jobs.status）
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

OUTCOME_ENQUEUED = "enqueued"
OUTCOME_REUSED = "reused"
OUTCOME_RETRIED = "retried"
OUTCOME_RESET = "reset"
OUTCOME_REJECTED = "rejected"

REJECT_REASON_RETRY_EXHAUSTED = "retry_exhausted"


def _engine_default():
    from admin.database import engine

    return engine


def canonical_targets_hash(targets: Iterable[Mapping[str, Any]]) -> str:
    """targets → 64 位十六进制 sha256（与 M5-A backfill 既有口径逐字一致）。

    规范化 = 只取 `chunk_id`/`target_revision`、按 (chunk_id, target_revision) 排序、
    `json.dumps(separators=(",", ":"), sort_keys=True, ensure_ascii=True)`。
    任何字段/排序/序列化改动都会改变 hash，进而让"重跑复用同一 job"失效。
    """
    ordered = sorted(
        [
            {"chunk_id": str(item["chunk_id"]), "target_revision": int(item["target_revision"])}
            for item in targets
        ],
        key=lambda item: (item["chunk_id"], item["target_revision"]),
    )
    canon = json.dumps(ordered, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _admin_jobs_table_exists(conn) -> bool:
    from sqlalchemy import inspect as sa_inspect

    return bool(sa_inspect(conn).has_table("admin_jobs"))


def _lock_existing_job(conn, *, kb_id: str, targets_hash: str) -> Optional[Dict[str, Any]]:
    """冲突回读既有行；Postgres 下加行锁，让两个并发同 hash 重提交串行分支。"""
    try:
        dialect = conn.engine.dialect.name
    except Exception:  # noqa: BLE001 - 连接包装层没有 engine 时退回无锁读
        dialect = ""
    sql = (
        "SELECT id, status, retry_count, max_retries FROM admin_jobs "
        "WHERE job_type = :job_type AND kb_id = :kb_id AND targets_hash = :targets_hash "
        "ORDER BY id LIMIT 1"
    )
    if dialect == "postgresql":
        sql += " FOR UPDATE"
    row = conn.execute(
        text(sql),
        {"job_type": JOB_TYPE, "kb_id": kb_id, "targets_hash": targets_hash},
    ).fetchone()
    if row is None:
        return None
    return {
        "job_id": int(row[0]),
        "status": str(row[1] or ""),
        "retry_count": int(row[2] or 0),
        "max_retries": int(row[3] or 0),
    }


def _reset_failed_job(conn, *, job_id: int, trace_id: str) -> None:
    """failed → pending 且 retry_count+1（§16.3 原地 retry，不新建行）。"""
    conn.execute(
        text(
            "UPDATE admin_jobs SET status = 'pending', retry_count = retry_count + 1, "
            "started_at = NULL, finished_at = NULL, error_message = NULL, result = NULL, "
            "claimed_by = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL, "
            "trace_id = COALESCE(NULLIF(:trace_id, ''), trace_id) "
            "WHERE id = :job_id"
        ),
        {"job_id": job_id, "trace_id": trace_id},
    )


def _reset_cancelled_job(conn, *, job_id: int, trace_id: str) -> None:
    """cancelled → pending 且 retry_count 归零（人工取消不消耗重试配额）。"""
    conn.execute(
        text(
            "UPDATE admin_jobs SET status = 'pending', retry_count = 0, "
            "started_at = NULL, finished_at = NULL, error_message = NULL, result = NULL, "
            "claimed_by = NULL, claim_expires_at = NULL, last_heartbeat_at = NULL, "
            "trace_id = COALESCE(NULLIF(:trace_id, ''), trace_id) "
            "WHERE id = :job_id"
        ),
        {"job_id": job_id, "trace_id": trace_id},
    )


def _group_targets(targets: Iterable[Mapping[str, Any]]) -> Dict[tuple, List[Dict[str, Any]]]:
    """按 (kb_id, doc_id) 分组并校验每条 target 必填字段（§8.1 无隐式扩范围）。

    分组键沿用 M5-A backfill 口径：同一文档的失败目标合成一个 job，跨文档不合并，
    否则 targets_hash 会把无关文档的重建绑在一起，复用判定失去文档级可操作性。
    """
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for raw in targets:
        chunk_id = str(raw.get("chunk_id") or "").strip()
        kb_id = str(raw.get("kb_id") or "").strip()
        if not chunk_id or not kb_id:
            raise ValueError("reindex 转交需要每条 target 都带 kb_id 与 chunk_id")
        try:
            target_revision = int(raw.get("target_revision"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"target_revision 非法: chunk_id={chunk_id}") from exc
        if target_revision < 1:
            raise ValueError(f"target_revision 必须 >= 1: chunk_id={chunk_id}")
        doc_id = str(raw.get("doc_id") or "").strip()
        groups.setdefault((kb_id, doc_id), []).append(
            {
                "kb_id": kb_id,
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "target_revision": target_revision,
                "tenant_id": str(raw.get("tenant_id") or ""),
                "project_id": str(raw.get("project_id") or ""),
            }
        )
    return groups


def _empty_report() -> Dict[str, Any]:
    return {
        "enqueued": 0,
        "reused": 0,
        "retried": 0,
        "reset": 0,
        "rejected": 0,
        "targets": 0,
        "jobs": [],
        "rejected_detail": [],
    }


def targets_from_payload(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """job payload → reindex targets（提交路径与终态回写共用同一份解析口径）。

    payload 形状按 §8.2 冻结：`{kb_id, tenant_id, project_id, doc_id?, targets:[{chunk_id,
    target_revision}]}`。targets 为空视为 §15.7 `REINDEX_SCOPE_REQUIRED`（调用方转成
    ValidationException），不返回空列表——空 targets 的 job 一旦入队，worker 会"成功"
    消费掉一个什么都不重建的任务。

    结构校验直接复用 `_group_targets`（入队时的同一判据），否则缺 chunk_id / 非法
    target_revision 的提交会带着裸 ValueError 冒到 API 层变成 500。
    """
    raw_targets = payload.get("targets") if isinstance(payload, Mapping) else None
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("reindex_chunks payload.targets 不能为空")
    kb_id = str(payload.get("kb_id") or "").strip()
    doc_id = str(payload.get("doc_id") or "").strip()
    tenant_id = str(payload.get("tenant_id") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    targets: List[Dict[str, Any]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping):
            raise ValueError("reindex_chunks payload.targets 必须是对象列表")
        targets.append(
            {
                "kb_id": kb_id,
                "doc_id": doc_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "chunk_id": raw.get("chunk_id"),
                "target_revision": raw.get("target_revision"),
            }
        )
    _group_targets(targets)
    return targets


def enqueue_on_connection(
    conn,
    targets: Iterable[Mapping[str, Any]],
    *,
    source: str,
    trace_id: str = "",
    max_retries: int = 3,
) -> Dict[str, Any]:
    """在调用方已开启的事务连接上执行 §16.3 入队（任务中心提交路径用）。

    和 `enqueue_reindex_jobs` 同一份分支表，只是不自带 `engine.begin()`：提交路径必须让
    "去重判定 + 建行 + 审计"落在 API session 的同一事务里，否则 `db.rollback()` 只能回滚
    一半，重复提交就会留下孤零零的 pending job。
    """
    if not _admin_jobs_table_exists(conn):
        raise RuntimeError("admin_jobs 表不存在，请先执行任务中心迁移")
    groups = _group_targets(targets)
    if not groups:
        return _empty_report()
    report = _empty_report()

    for (kb_id, doc_id), group in sorted(groups.items()):
        payload_targets = [
            {"chunk_id": item["chunk_id"], "target_revision": item["target_revision"]} for item in group
        ]
        payload_targets.sort(key=lambda t: (t["chunk_id"], t["target_revision"]))
        targets_hash = canonical_targets_hash(payload_targets)
        tenant_id = next((item["tenant_id"] for item in group if item["tenant_id"]), "")
        project_id = next((item["project_id"] for item in group if item["project_id"]), "")
        payload = {
            "kb_id": kb_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "doc_id": doc_id or None,
            "source": source,
            "targets": payload_targets,
        }
        result = conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries, trace_id, targets_hash) "
                "VALUES (:job_type, 'pending', :tenant_id, :project_id, :kb_id, :payload, "
                "0, :max_retries, :trace_id, :targets_hash) "
                "ON CONFLICT (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL DO NOTHING"
            ),
            {
                "job_type": JOB_TYPE,
                "tenant_id": tenant_id or None,
                "project_id": project_id or None,
                "kb_id": kb_id,
                "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "max_retries": max_retries,
                "trace_id": trace_id,
                "targets_hash": targets_hash,
            },
        )
        report["targets"] += len(payload_targets)
        entry: Dict[str, Any] = {
            "kb_id": kb_id,
            "doc_id": doc_id,
            "targets_hash": targets_hash,
            "target_count": len(payload_targets),
        }
        if result.rowcount:
            new_id = conn.execute(
                text(
                    "SELECT id FROM admin_jobs WHERE job_type = :job_type AND kb_id = :kb_id "
                    "AND targets_hash = :targets_hash ORDER BY id DESC LIMIT 1"
                ),
                {"job_type": JOB_TYPE, "kb_id": kb_id, "targets_hash": targets_hash},
            ).fetchone()
            report["enqueued"] += 1
            entry["outcome"] = OUTCOME_ENQUEUED
            entry["job_id"] = int(new_id[0]) if new_id else None
            report["jobs"].append(entry)
            continue

        existing = _lock_existing_job(conn, kb_id=kb_id, targets_hash=targets_hash)
        if existing is None:
            # 唯一索引拦了新增却读不到行：历史 NULL hash 行不参与唯一性，正常不该发生。
            report["reused"] += 1
            entry["outcome"] = OUTCOME_REUSED
            entry["job_id"] = None
            report["jobs"].append(entry)
            continue

        entry["job_id"] = existing["job_id"]
        status = existing["status"]
        if status in (STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCEEDED):
            report["reused"] += 1
            entry["outcome"] = OUTCOME_REUSED
        elif status == STATUS_FAILED:
            if existing["retry_count"] < existing["max_retries"]:
                _reset_failed_job(conn, job_id=existing["job_id"], trace_id=trace_id)
                report["retried"] += 1
                entry["outcome"] = OUTCOME_RETRIED
            else:
                report["rejected"] += 1
                entry["outcome"] = OUTCOME_REJECTED
                report["rejected_detail"].append(
                    {
                        "job_id": existing["job_id"],
                        "kb_id": kb_id,
                        "doc_id": doc_id,
                        "targets_hash": targets_hash,
                        "reason": REJECT_REASON_RETRY_EXHAUSTED,
                        "retry_count": existing["retry_count"],
                        "max_retries": existing["max_retries"],
                        "chunk_ids": [t["chunk_id"] for t in payload_targets],
                    }
                )
        elif status == STATUS_CANCELLED:
            _reset_cancelled_job(conn, job_id=existing["job_id"], trace_id=trace_id)
            report["reset"] += 1
            entry["outcome"] = OUTCOME_RESET
        else:
            # 未知状态一律按复用处理，不冒险重置别人正在管的行。
            report["reused"] += 1
            entry["outcome"] = OUTCOME_REUSED
        report["jobs"].append(entry)
    return report


def enqueue_reindex_jobs(
    targets: Iterable[Mapping[str, Any]],
    *,
    source: str,
    trace_id: str = "",
    max_retries: int = 3,
    engine=None,
) -> Dict[str, Any]:
    """§16.3 幂等入队：新 targets_hash 建行，冲突则回读既有行按状态分支。

    `targets` 每条需要 `kb_id/chunk_id/target_revision`，可带 `doc_id/tenant_id/project_id`。
    返回 `{enqueued, reused, retried, reset, rejected, targets, jobs, rejected_detail}`；
    `jobs` 逐组给 `{kb_id, doc_id, targets_hash, outcome, job_id}`，调用方据此写审计。
    `admin_jobs` 不存在时抛 RuntimeError（不静默跳过转交）。
    """
    conn_engine = engine or _engine_default()
    groups = _group_targets(targets)
    if not groups:
        return _empty_report()
    with conn_engine.begin() as conn:
        return enqueue_on_connection(
            conn,
            targets,
            source=source,
            trace_id=trace_id,
            max_retries=max_retries,
        )
