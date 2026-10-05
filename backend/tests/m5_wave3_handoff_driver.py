#!/usr/bin/env python3
"""
M5 Wave 3 连续场景驱动（由 check_m5_wave3_handoff.py 以子进程调用）

五个场景把 Wave 3 的四条接线串成一条真实链路（临时 SQLite + 假 Neo4j/假 Milvus，
零开发库写入）：
  1. continuity：真实 build_graph 的影子写失败 → 逐 chunk `vector_status='failed'` →
     自动转交 reindex_chunks（targets_hash 64 hex）→ 真实 `job_service.create_job` 同
     hash 复用（不新增行）→ 真实 `run_job` 消费同一份 handoff payload → 双侧 indexed +
     §6.2 文档聚合 → 作业 succeeded；
  2. terminal_exhausted：重试额度用尽的终态失败 → 父子回写只降级未收敛的那条腿
     （已 indexed 的 graph 腿不回退）+ 审计 `kb_chunk_reindex_failed` + 再次提交被拒 3004；
  2b. terminal_crash：worker 在状态回写之前抛异常（超时/崩溃形态）→ 投影列还停在 pending，
     终态父子回写把未收敛的两腿都落 failed；
  3. retry_not_terminal：额度未用尽时只排退避重试，**不做**终态父子回写（边界判据）；
  4. index_unavailable：§8.5 collection 缺显式 content_revision 字段 → vector 保持
     pending（不是 failed），终态回写整列不动 vector 侧。

替换的只有外部依赖（Neo4j 会话、文档注册表、embedding、Milvus client、worker 的索引写
函数）；作业状态机、CAS 回写、§16.3 去重分支、§6.2 聚合、审计全部走真实代码。
引擎方言非 sqlite 立即退出（9）。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import Base, SessionLocal, engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

KB = "kb-w3"
TENANT = "t1"
PROJECT = "p1"
DOC = "doc-w3"
PRIMARY = "graphinsight_chunks_v2"
SHADOW = "graphinsight_chunks_v3"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _require_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        print(f"FATAL: engine dialect is {engine.dialect.name}, expected sqlite (isolation broken)")
        raise SystemExit(9)


def _sha(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _seed_kb(kb_id: str = KB) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:kb, :tenant, :project, :name, 'active', :prefix) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "kb": kb_id,
                "tenant": TENANT,
                "project": PROJECT,
                "name": f"kb {kb_id}",
                "prefix": f"kb/{kb_id}",
            },
        )


def _seed_doc(doc_id: str, kb_id: str = KB) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_base_documents (doc_id, kb_id, tenant_id, project_id, name, "
                "relative_path, source_type, size, sha256, version, status, graph_status, vector_status) "
                "VALUES (:doc, :kb, :tenant, :project, :name, :path, 'upload', 10, :sha, 1, 'indexed', "
                "'pending', 'pending') ON CONFLICT (doc_id) DO NOTHING"
            ),
            {
                "doc": doc_id,
                "kb": kb_id,
                "tenant": TENANT,
                "project": PROJECT,
                "name": f"{doc_id}.txt",
                "path": f"kb/{kb_id}/{doc_id}.txt",
                "sha": _sha(doc_id),
            },
        )


def _seed_rev(
    chunk_id: str,
    *,
    doc_id: str = DOC,
    revision: int = 1,
    graph_status: str = "pending",
    graph_rev: int | None = None,
    vector_status: str = "pending",
    vector_rev: int | None = None,
    content: str = "需要重建投影的正文内容",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, graph_content_revision, "
                "vector_content_revision, revision_source, reason, trace_id) VALUES "
                "(:kb, :tenant, :project, :doc, :chunk, :content, :hash, :content, :hash, :rev, "
                "'current', :graph, :vector, :graph_rev, :vector_rev, 'system_reparse', 'wave3', 't')"
            ),
            {
                "kb": KB,
                "tenant": TENANT,
                "project": PROJECT,
                "doc": doc_id,
                "chunk": chunk_id,
                "content": content,
                "hash": _sha(content),
                "rev": revision,
                "graph": graph_status,
                "vector": vector_status,
                "graph_rev": graph_rev,
                "vector_rev": vector_rev,
            },
        )


def _revisions(kb_id: str = KB) -> Dict[str, Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT chunk_id, content_revision, graph_status, graph_content_revision, "
                "vector_status, vector_content_revision FROM chunk_revisions "
                "WHERE kb_id = :kb AND revision_status = 'current' ORDER BY chunk_id"
            ),
            {"kb": kb_id},
        ).fetchall()
    return {
        str(r[0]): {"rev": r[1], "graph": r[2], "graph_rev": r[3], "vector": r[4], "vector_rev": r[5]}
        for r in rows
    }


def _documents() -> Dict[str, Dict[str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT doc_id, graph_status, vector_status FROM knowledge_base_documents ORDER BY doc_id")
        ).fetchall()
    return {str(r[0]): {"graph": r[1], "vector": r[2]} for r in rows}


def _job_rows() -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, job_type, status, kb_id, retry_count, max_retries, targets_hash, payload, "
                "error_message FROM admin_jobs ORDER BY id"
            )
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "job_type": r[1],
            "status": r[2],
            "kb_id": r[3],
            "retry_count": int(r[4] or 0),
            "max_retries": int(r[5] or 0),
            "targets_hash": r[6],
            "payload": json.loads(r[7]) if r[7] else {},
            "error_message": r[8] or "",
        }
        for r in rows
    ]


def _log_actions(action: str) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT action, resource_id, details, status FROM admin_logs WHERE action = :a ORDER BY id"),
            {"a": action},
        ).fetchall()
    out = []
    for r in rows:
        try:
            details = json.loads(r[2]) if r[2] else {}
        except json.JSONDecodeError:
            details = {"raw": r[2]}
        out.append({"action": r[0], "resource_id": r[1], "details": details, "status": r[3]})
    return out


# ---------------------------------------------------------------------------
# 假索引侧（reindex worker 用）
# ---------------------------------------------------------------------------


class World:
    def __init__(self) -> None:
        self.neo: Dict[str, Dict[str, Any]] = {}
        self.mil: Dict[str, Dict[str, Any]] = {}
        self.graph_calls: List[str] = []
        self.vector_calls: List[str] = []
        self.fail_graph = False
        self.fail_vector = False
        self.graph_raises = False

    def fake_graph(self, kb_id, items):
        import services.chunk_projection_reindex as worker

        self.graph_calls.extend(item["chunk_id"] for item in items)
        if self.graph_raises:
            # 模拟 worker 在状态回写之前崩溃/超时：投影列还停在 pending，终态父子回写必须兜住
            raise RuntimeError("simulated worker crash before state write-back")
        if self.fail_graph:
            return {item["chunk_id"]: worker.OUTCOME_WRITE_FAILED for item in items}
        for item in items:
            self.neo[f"{kb_id}|{item['chunk_id']}"] = {
                "text": item["content"],
                "content_revision": item["target_revision"],
                "doc_id": item["doc_id"],
            }
        return {item["chunk_id"]: worker.OUTCOME_INDEXED for item in items}

    def fake_vector(self, kb_id, items, *, tenant_id, project_id):
        import services.chunk_projection_reindex as worker

        self.vector_calls.extend(item["chunk_id"] for item in items)
        if self.fail_vector:
            return {item["chunk_id"]: worker.OUTCOME_WRITE_FAILED for item in items}
        for item in items:
            self.mil[f"{kb_id}|{item['chunk_id']}"] = {
                "text": item["content"],
                "content_revision": item["target_revision"],
                "tenant_id": tenant_id,
                "project_id": project_id,
            }
        return {item["chunk_id"]: worker.OUTCOME_INDEXED for item in items}


W = World()


def _install_worker(
    *,
    fail_vector: bool = False,
    fail_graph: bool = False,
    revision_field: bool = True,
    graph_raises: bool = False,
) -> None:
    """worker 的索引写与能力判定换假实现；CAS 复核、聚合、作业状态机保持真实。"""
    import services.chunk_projection_reindex as worker
    from services.vector_store import vector_store

    W.fail_graph = fail_graph
    W.fail_vector = fail_vector
    W.graph_raises = graph_raises
    worker._write_neo4j_projection = W.fake_graph
    worker._write_milvus_projection = W.fake_vector
    worker.get_projection_capabilities = lambda: {"graph": True, "vector": True}
    worker._existing_milvus_fields = lambda kb_id, chunk_ids: {}
    vector_store.has_content_revision_field = lambda *a, **k: revision_field


class ShadowFailingClient:
    """v2 主库写成功、v3 影子写抛错（§16.1 S1 影子脏写的原始形态）。"""

    def __init__(self) -> None:
        self.upserts: Dict[str, list] = {}
        self.shadow_attempted = False

    def has_collection(self, name):
        return True

    def upsert(self, *, collection_name, data):  # noqa: A002 - 匹配 pymilvus 关键字签名
        if collection_name == SHADOW:
            self.shadow_attempted = True
            raise RuntimeError("simulated v3 shadow upsert failure")
        self.upserts.setdefault(collection_name, []).append(data)
        return {"upsert_count": len(data)}


def _install_shadow_vector_store() -> ShadowFailingClient:
    from services.embedding_service import embedding_service
    from services.vector_store import vector_store

    client = ShadowFailingClient()
    vector_store._get_client = lambda: client
    vector_store._revision_field = {PRIMARY: True, SHADOW: True}
    vector_store.is_enabled = lambda: True
    vector_store.config = lambda: {
        "enabled": True,
        "provider": "milvus",
        "collection": PRIMARY,
        "dual_write": True,
        "shadow_collection": SHADOW,
    }
    vector_store.ensure_collection = lambda **k: None
    vector_store.has_content_revision_field = lambda *a, **k: True

    embedding_service.is_enabled = lambda: True
    embedding_service.config = lambda: {"model": "m-test", "batch_size": 32}
    embedding_service.embed_texts = lambda texts: [[0.1, 0.2] for _ in texts]
    embedding_service.content_hash = lambda value: "h-test"
    return client


def _run_real_build_graph(tmp: Path) -> Dict[str, Any]:
    """真实 build_graph（只换 Neo4j/注册表/解析路径），index_chunks 与 vector_store 保持真实。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    import services.document_graph_service as dgs
    import services.document_registry as registry

    class _FakeResult:
        def __init__(self, records):
            self._records = records

        def single(self):
            return self._records[0] if self._records else None

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, cypher, params=None):
            return _FakeResult([{"c": 0}])

    class _FakeNeo4j:
        def ensure_connected(self):
            return None

        def session(self):
            return _FakeSession()

    root = tmp / "real-bg"
    source_file = root / "documents" / TENANT / PROJECT / KB / DOC / "w3.txt"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "Alpha 属于 Beta。Alpha 在 Wave 3 连续场景里用于验证影子写失败会逐 chunk 落 failed，"
        "并按 §16.3 转交 reindex_chunks。第二段继续补充观测细节以便切出第二个 chunk。",
        encoding="utf-8",
    )
    fake_kb = SimpleNamespace(id=KB, status="active", storage_prefix=f"{TENANT}/{PROJECT}/{KB}")
    fake_doc = SimpleNamespace(
        doc_id=DOC, kb_id=KB, tenant_id=TENANT, project_id=PROJECT, name="w3.txt", relative_path=f"{DOC}/w3.txt"
    )

    old_parsed = dgs.settings.parsed_document_storage_path
    dgs.settings.parsed_document_storage_path = str(root / "parsed")
    try:
        with contextlib.ExitStack() as stack:
            for ctx in (
                patch.object(dgs.DocumentGraphService, "_neo4j", lambda self: _FakeNeo4j()),
                patch.object(dgs.DocumentGraphService, "_extract_entities", lambda self, *_a, **_k: ["Alpha"]),
                patch.object(dgs.DocumentGraphService, "_extract_relations", lambda self, *_a, **_k: []),
                patch.object(registry, "get_knowledge_base", lambda _kb_id: fake_kb),
                patch.object(registry, "get_documents", lambda _ids: {DOC: fake_doc}),
                patch.object(registry, "resolve_document_file_path", lambda _doc, _kb=None: source_file),
            ):
                stack.enter_context(ctx)
            service = dgs.DocumentGraphService()
            return service.build_graph(kb_id=KB, doc_ids=[DOC], force=True)
    finally:
        dgs.settings.parsed_document_storage_path = old_parsed


HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_hash(targets: List[Dict[str, Any]]) -> str:
    from services.reindex_queue import canonical_targets_hash

    return canonical_targets_hash(targets)


# ---------------------------------------------------------------------------
# 场景 1：影子失败 → 转交 → 复用 → worker 消费 → 闭环
# ---------------------------------------------------------------------------


def scenario_continuity(tmp: Path) -> None:
    _seed_kb()
    _seed_doc(DOC)
    _install_shadow_vector_store()

    stats = _run_real_build_graph(tmp)
    failed_ids = sorted(stats.get("vector_failed_chunks") or [])
    handoff = stats.get("reindex_handoff") or {}
    before = _revisions()
    jobs = _job_rows()
    reindex_jobs = [j for j in jobs if j["job_type"] == "reindex_chunks"]
    handoff_job = reindex_jobs[0] if reindex_jobs else {}
    payload_targets = (handoff_job.get("payload") or {}).get("targets") or []

    print(
        "__HANDOFF__"
        + json.dumps(
            {
                "failed_ids": failed_ids,
                "handoff": handoff,
                "detail_count": len(stats.get("vector_failure_details") or []),
                "is_shadow": bool((stats.get("vector_failure_details") or [{}])[0].get("is_shadow")),
                "job_count": len(reindex_jobs),
                "targets_hash": handoff_job.get("targets_hash"),
                "hash_shape_ok": bool(HASH_RE.match(str(handoff_job.get("targets_hash") or ""))),
                "hash_matches_payload": bool(payload_targets)
                and handoff_job.get("targets_hash") == _canonical_hash(payload_targets),
                "payload_source": (handoff_job.get("payload") or {}).get("source"),
                "payload_target_ids": sorted(str(t.get("chunk_id")) for t in payload_targets),
                "payload_revisions": sorted({int(t.get("target_revision")) for t in payload_targets}),
                "rev_graph_state": {cid: row["graph"] for cid, row in sorted(before.items())},
                "rev_vector_state": {cid: row["vector"] for cid, row in sorted(before.items())},
                "rev_vector_rev": {cid: row["vector_rev"] for cid, row in sorted(before.items())},
                "doc_state": _documents().get(DOC),
                "untracked": stats.get("reindex_handoff_untracked"),
            },
            ensure_ascii=False,
        )
    )

    # 真实提交路径：同 hash 复用，不新增行
    from admin.schemas.jobs import JobCreateRequest, JobQuery
    from admin.services.job_service import job_service as svc

    errors: Dict[str, str] = {}
    resubmit: Dict[str, Any] = {}
    listed: Dict[str, Any] = {}
    db = SessionLocal()
    try:
        same = {"kb_id": KB, "doc_id": DOC, "targets": [dict(t) for t in payload_targets]}
        item = svc.create_job(
            db,
            job_type="reindex_chunks",
            request=JobCreateRequest(
                tenant_id=TENANT, project_id=PROJECT, kb_id=KB, payload=same, max_retries=3
            ),
            requested_by=None,
            trace_id="trace-w3-resubmit",
        )
        listed_items, total = svc.list_jobs(db, JobQuery(job_type="reindex_chunks", kb_id=KB))
        fetched = svc.get_job(db, item.id)
        resubmit = {
            "item_id": item.id,
            "item_status": item.status,
            "item_targets_hash": item.targets_hash,
            "row_count_after": len([j for j in _job_rows() if j["job_type"] == "reindex_chunks"]),
            "list_total": total,
            "list_hashes": [i.targets_hash for i in listed_items],
            "get_hash": fetched.targets_hash,
            "job_reused_count": len(_log_actions("job_reused")),
            "job_created_count": len(_log_actions("job_created")),
        }
        for label, payload in (
            ("empty", {"kb_id": KB, "doc_id": DOC, "targets": []}),
            ("no_chunk_id", {"kb_id": KB, "doc_id": DOC, "targets": [{"target_revision": 1}]}),
            ("bad_revision", {"kb_id": KB, "doc_id": DOC, "targets": [{"chunk_id": "c-1", "target_revision": 0}]}),
        ):
            try:
                svc.create_job(
                    db,
                    job_type="reindex_chunks",
                    request=JobCreateRequest(
                        tenant_id=TENANT, project_id=PROJECT, kb_id=KB, payload=payload, max_retries=3
                    ),
                    requested_by=None,
                    trace_id=f"trace-w3-{label}",
                )
                errors[label] = "NO_RAISE"
            except Exception as exc:  # noqa: BLE001 - 断言异常类型与错误码
                errors[label] = f"{type(exc).__name__}:{getattr(exc, 'error_code', '')}"
    finally:
        db.close()
    print("__SUBMIT__" + json.dumps({"errors": errors, **resubmit}, ensure_ascii=False))

    # 真实 worker 消费同一份 handoff payload
    job_id = int(handoff_job["id"])
    _install_worker()
    svc.run_job(job_id)
    after = _revisions()
    final_jobs = [j for j in _job_rows() if j["id"] == job_id]
    print(
        "__CLOSED__"
        + json.dumps(
            {
                "job_status": final_jobs[0]["status"] if final_jobs else "",
                "job_retry": final_jobs[0]["retry_count"] if final_jobs else None,
                "rev_graph": {cid: row["graph"] for cid, row in sorted(after.items())},
                "rev_vector": {cid: row["vector"] for cid, row in sorted(after.items())},
                "rev_graph_rev": {cid: row["graph_rev"] for cid, row in sorted(after.items())},
                "rev_vector_rev": {cid: row["vector_rev"] for cid, row in sorted(after.items())},
                "doc_state": _documents().get(DOC),
                "neo_written": sorted(k.split("|", 1)[1] for k in W.neo),
                "mil_written": sorted(k.split("|", 1)[1] for k in W.mil),
                "mil_revisions": sorted({v["content_revision"] for v in W.mil.values()}),
                "job_count_total": len([j for j in _job_rows() if j["job_type"] == "reindex_chunks"]),
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 2：重试额度用尽的终态失败 → 父子回写 + 审计 + 提交被拒
# ---------------------------------------------------------------------------


def scenario_terminal_exhausted(tmp: Path) -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-idx", graph_status="indexed", graph_rev=1, vector_status="indexed", vector_rev=1)
    _seed_rev("c-bad", graph_status="pending", vector_status="pending")

    targets = [{"chunk_id": "c-bad", "target_revision": 1}]
    hash_value = _canonical_hash(targets)
    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_id": DOC,
        "source": "build_graph_m5_wave3",
        "targets": targets,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries, targets_hash) VALUES ('reindex_chunks', 'pending', :t, :p, :kb, "
                ":payload, 2, 2, :hash)"
            ),
            {"t": TENANT, "p": PROJECT, "kb": KB, "payload": json.dumps(payload, ensure_ascii=False), "hash": hash_value},
        )
        job_id = int(
            conn.execute(text("SELECT id FROM admin_jobs WHERE targets_hash = :hash"), {"hash": hash_value}).scalar_one()
        )

    _install_worker(fail_vector=True)
    from admin.services.job_service import job_service as svc

    svc._schedule_retry = lambda *a, **k: None
    svc.run_job(job_id)

    row = [j for j in _job_rows() if j["id"] == job_id][0]
    revs = _revisions()
    audits = _log_actions("kb_chunk_reindex_failed")

    # 额度已用尽后再提交：§16.3 拒绝（3004 + reason=retry_exhausted），且不新增行
    from admin.schemas.jobs import JobCreateRequest

    rejected: Dict[str, Any] = {}
    db = SessionLocal()
    try:
        try:
            svc.create_job(
                db,
                job_type="reindex_chunks",
                request=JobCreateRequest(
                    tenant_id=TENANT, project_id=PROJECT, kb_id=KB, payload=dict(payload), max_retries=2
                ),
                requested_by=None,
                trace_id="trace-w3-reject",
            )
            rejected = {"raised": None}
        except Exception as exc:  # noqa: BLE001
            rejected = {
                "raised": type(exc).__name__,
                "error_code": str(getattr(exc, "error_code", "")),
                "details": getattr(exc, "details", None),
            }
    finally:
        db.close()

    print(
        "__TERMINAL__"
        + json.dumps(
            {
                "job_status": row["status"],
                "retry_count": row["retry_count"],
                "max_retries": row["max_retries"],
                "retry_planned": "已计划自动重试" in row["error_message"],
                "c_idx": revs.get("c-idx"),
                "c_bad": revs.get("c-bad"),
                "doc_state": _documents().get(DOC),
                "audit_count": len(audits),
                "audit_details": audits[0]["details"] if audits else {},
                "job_failed_count": len(_log_actions("job_failed")),
                "audit_resource_id": audits[0]["resource_id"] if audits else None,
                "job_count_total": len([j for j in _job_rows() if j["job_type"] == "reindex_chunks"]),
                "rejected": rejected,
                "audit_count_after_reject": len(_log_actions("kb_chunk_reindex_failed")),
                "worker_vector_calls": sorted(W.vector_calls),
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 2b：worker 在状态回写前崩溃 → 终态父子回写把未收敛的两腿都落 failed
# ---------------------------------------------------------------------------


def _seed_reindex_job(targets: List[Dict[str, Any]], *, retry_count: int, max_retries: int) -> int:
    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_id": DOC,
        "source": "build_graph_m5_wave3",
        "targets": targets,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries, targets_hash) VALUES ('reindex_chunks', 'pending', :t, :p, :kb, "
                ":payload, :retry, :max_retries, :hash)"
            ),
            {
                "t": TENANT,
                "p": PROJECT,
                "kb": KB,
                "payload": json.dumps(payload, ensure_ascii=False),
                "retry": retry_count,
                "max_retries": max_retries,
                "hash": _canonical_hash(targets),
            },
        )
        return int(
            conn.execute(
                text("SELECT id FROM admin_jobs WHERE job_type = 'reindex_chunks' ORDER BY id LIMIT 1")
            ).scalar_one()
        )


def scenario_terminal_crash(tmp: Path) -> None:
    """终态父子回写的**本职**场景：worker 没机会写状态就炸（超时/崩溃）。

    `terminal_exhausted` 里 worker 自己已把 vector 腿落 failed、graph 腿本轮收敛成 indexed，
    所以那条链只能证"保守到侧不回退已收敛的腿"；要证"未收敛的两腿都被兜住"必须让异常发生在
    `_write_neo4j_projection` 里（状态回写之前），投影列停在 pending，由作业终态回写收口。
    """
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-idx", graph_status="indexed", graph_rev=1, vector_status="indexed", vector_rev=1)
    _seed_rev("c-bad", graph_status="pending", vector_status="pending")

    job_id = _seed_reindex_job(
        [{"chunk_id": "c-bad", "target_revision": 1}], retry_count=2, max_retries=2
    )

    _install_worker(graph_raises=True)
    from admin.services.job_service import job_service as svc

    svc._schedule_retry = lambda *a, **k: None
    svc.run_job(job_id)

    row = [j for j in _job_rows() if j["id"] == job_id][0]
    revs = _revisions()
    audits = _log_actions("kb_chunk_reindex_failed")
    print(
        "__CRASH__"
        + json.dumps(
            {
                "job_status": row["status"],
                "retry_count": row["retry_count"],
                "error_message": row["error_message"][:200],
                "graph_calls": sorted(W.graph_calls),
                "vector_calls": sorted(W.vector_calls),
                "c_idx": revs.get("c-idx"),
                "c_bad": revs.get("c-bad"),
                "doc_state": _documents().get(DOC),
                "audit_count": len(audits),
                "audit_details": audits[0]["details"] if audits else {},
                "job_failed_count": len(_log_actions("job_failed")),
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 3：额度未用尽 → 只排重试，不做终态父子回写
# ---------------------------------------------------------------------------


def scenario_retry_not_terminal(tmp: Path) -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-bad", graph_status="pending", vector_status="pending")

    targets = [{"chunk_id": "c-bad", "target_revision": 1}]
    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_id": DOC,
        "source": "build_graph_m5_wave3",
        "targets": targets,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries, targets_hash) VALUES ('reindex_chunks', 'pending', :t, :p, :kb, "
                ":payload, 0, 2, :hash)"
            ),
            {
                "t": TENANT,
                "p": PROJECT,
                "kb": KB,
                "payload": json.dumps(payload, ensure_ascii=False),
                "hash": _canonical_hash(targets),
            },
        )
        job_id = int(
            conn.execute(
                text("SELECT id FROM admin_jobs WHERE job_type = 'reindex_chunks' ORDER BY id LIMIT 1")
            ).scalar_one()
        )

    sched: List[Dict[str, Any]] = []
    _install_worker(fail_vector=True)
    from admin.services.job_service import job_service as svc

    svc._schedule_retry = lambda jid, attempt, delay: sched.append(
        {"job_id": jid, "attempt": attempt, "delay": delay}
    )
    svc.run_job(job_id)

    row = [j for j in _job_rows() if j["id"] == job_id][0]
    revs = _revisions()
    print(
        "__RETRY_ONLY__"
        + json.dumps(
            {
                "job_status": row["status"],
                "retry_count": row["retry_count"],
                "retry_planned": "已计划自动重试" in row["error_message"],
                "sched": sched,
                "c_bad": revs.get("c-bad"),
                "doc_state": _documents().get(DOC),
                "job_failed_count": len(_log_actions("job_failed")),
                "retry_scheduled_log": len(_log_actions("job_retry_scheduled")),
                "audit_count": len(_log_actions("kb_chunk_reindex_failed")),
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 4：§8.5 缺显式字段 → 终态回写整列不动 vector 侧
# ---------------------------------------------------------------------------


def scenario_index_unavailable(tmp: Path) -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", graph_status="pending", vector_status="pending")

    targets = [{"chunk_id": "c-1", "target_revision": 1}]
    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_id": DOC,
        "source": "build_graph_m5_wave3",
        "targets": targets,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries, targets_hash) VALUES ('reindex_chunks', 'pending', :t, :p, :kb, "
                ":payload, 0, 0, :hash)"
            ),
            {
                "t": TENANT,
                "p": PROJECT,
                "kb": KB,
                "payload": json.dumps(payload, ensure_ascii=False),
                "hash": _canonical_hash(targets),
            },
        )
        job_id = int(
            conn.execute(
                text("SELECT id FROM admin_jobs WHERE job_type = 'reindex_chunks' ORDER BY id LIMIT 1")
            ).scalar_one()
        )

    _install_worker(revision_field=False)
    from admin.services.job_service import job_service as svc

    svc._schedule_retry = lambda *a, **k: None
    svc.run_job(job_id)

    row = [j for j in _job_rows() if j["id"] == job_id][0]
    revs = _revisions()
    audits = _log_actions("kb_chunk_reindex_failed")
    print(
        "__BLOCKED__"
        + json.dumps(
            {
                "job_status": row["status"],
                "error_message": row["error_message"][:220],
                "retry_count": row["retry_count"],
                "vector_calls": sorted(W.vector_calls),
                "c_1": revs.get("c-1"),
                "doc_state": _documents().get(DOC),
                "audit_details": audits[0]["details"] if audits else {},
                "audit_count": len(audits),
                "job_failed_count": len(_log_actions("job_failed")),
                "retry_scheduled_count": len(_log_actions("job_retry_scheduled")),
            },
            ensure_ascii=False,
        )
    )


SCENARIOS = {
    "continuity": scenario_continuity,
    "terminal_exhausted": scenario_terminal_exhausted,
    "terminal_crash": scenario_terminal_crash,
    "retry_not_terminal": scenario_retry_not_terminal,
    "index_unavailable": scenario_index_unavailable,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 Wave 3 continuity driver")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args()
    _require_sqlite()
    with tempfile.TemporaryDirectory() as td:
        SCENARIOS[args.scenario](Path(td))
    return 0


if __name__ == "__main__":
    sys.exit(main())
