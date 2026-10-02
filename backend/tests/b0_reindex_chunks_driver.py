#!/usr/bin/env python3
"""
M5-B0 reindex_chunks worker 测试驱动（由 check_b0_reindex_chunks.py 以子进程调用）

在临时 SQLite 库上跑冻结契约场景；索引侧（Neo4j/Milvus）以外层假实现注入，
但作用域校验、四道复核、CAS 回写、文档级聚合与 backfill 闭环都走真实代码。
引擎方言非 sqlite 时立即退出（9），防止误连开发库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import Base, engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

import admin.backfill_chunk_revisions as bf  # noqa: E402
import services.chunk_projection_reindex as worker  # noqa: E402
import services.job_runtime as runtime  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

KB = "kb-b0"
OTHER_KB = "kb-other"
TENANT = "t1"
PROJECT = "p1"
DOC = "doc-1"


class World:
    """假索引侧 + 调用记录。"""

    def __init__(self) -> None:
        self.neo: Dict[str, Dict[str, Any]] = {}
        self.mil: Dict[str, Dict[str, Any]] = {}
        self.graph_calls: List[str] = []
        self.vector_calls: List[str] = []
        self.fail_graph = False
        self.fail_vector = False
        self.move_after_write: Dict[str, int] = {}

    def fake_graph(self, kb_id, items):
        self.graph_calls.extend(item["chunk_id"] for item in items)
        if self.fail_graph:
            return {item["chunk_id"]: worker.OUTCOME_WRITE_FAILED for item in items}
        for item in items:
            self.neo[f"{kb_id}|{item['chunk_id']}"] = {
                "text": item["content"],
                "content_revision": item["target_revision"],
                "doc_id": item["doc_id"],
                "kb_id": kb_id,
            }
        return {item["chunk_id"]: worker.OUTCOME_INDEXED for item in items}

    def fake_vector(self, kb_id, items, *, tenant_id, project_id):
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


def _require_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        print(f"FATAL: engine dialect is {engine.dialect.name}, expected sqlite (isolation broken)")
        raise SystemExit(9)


def _sha(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _bootstrap() -> None:
    from admin.models import AdminJob, KnowledgeBase, KnowledgeBaseDocument

    Base.metadata.create_all(bind=engine, tables=[KnowledgeBase.__table__, KnowledgeBaseDocument.__table__, AdminJob.__table__])


def _seed_kb(kb_id: str = KB, tenant: str = TENANT, project: str = PROJECT) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:kb, :tenant, :project, :name, 'active', :prefix) ON CONFLICT (id) DO NOTHING"
            ),
            {"kb": kb_id, "tenant": tenant, "project": project, "name": f"kb {kb_id}", "prefix": f"kb/{kb_id}"},
        )


def _seed_doc(doc_id: str, kb_id: str = KB, graph_status: str = "pending", vector_status: str = "pending") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_base_documents (doc_id, kb_id, tenant_id, project_id, name, "
                "relative_path, source_type, size, sha256, version, status, graph_status, vector_status) "
                "VALUES (:doc, :kb, :tenant, :project, :name, :path, 'upload', 10, :sha, 1, 'indexed', "
                ":graph, :vector) ON CONFLICT (doc_id) DO NOTHING"
            ),
            {
                "doc": doc_id,
                "kb": kb_id,
                "tenant": TENANT,
                "project": PROJECT,
                "name": f"{doc_id}.md",
                "path": f"kb/{kb_id}/{doc_id}.md",
                "sha": _sha(doc_id),
                "graph": graph_status,
                "vector": vector_status,
            },
        )


def _seed_rev(
    chunk_id: str,
    *,
    kb_id: str = KB,
    doc_id: str = DOC,
    content: str = "正文内容",
    revision: int = 1,
    status: str = "current",
    graph_status: str = "pending",
    graph_rev=None,
    vector_status: str = "pending",
    vector_rev=None,
    tenant: str = TENANT,
    project: str = PROJECT,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, graph_content_revision, "
                "vector_content_revision, revision_source, reason, trace_id) VALUES "
                "(:kb, :tenant, :project, :doc, :chunk, :content, :hash, :content, :hash, :rev, "
                ":status, :graph, :vector, :graph_rev, :vector_rev, 'system_reparse', 'test', 'trace-test')"
            ),
            {
                "kb": kb_id,
                "tenant": tenant,
                "project": project,
                "doc": doc_id,
                "chunk": chunk_id,
                "content": content,
                "hash": _sha(content),
                "rev": revision,
                "status": status,
                "graph": graph_status,
                "vector": vector_status,
                "graph_rev": graph_rev,
                "vector_rev": vector_rev,
            },
        )


def _install(*, graph: bool = True, vector: bool = True, revision_field: bool = True) -> None:
    """把索引侧与能力判定换成假实现；CAS/复核/聚合仍走真实实现。"""
    worker._write_neo4j_projection = W.fake_graph
    worker._write_milvus_projection = W.fake_vector
    worker.get_projection_capabilities = lambda: {"graph": graph, "vector": vector}

    from services.vector_store import vector_store

    vector_store.has_content_revision_field = lambda *a, **k: revision_field

    # 并发编辑模拟：写索引后把 current 移到别的 revision
    real_load = worker._load_current_rows
    state = {"round": 0}

    def loaded(kb_id: str, chunk_ids: List[str]):
        rows = real_load(kb_id, chunk_ids)
        if state["round"] == 2:
            for chunk_id, new_rev in W.move_after_write.items():
                if chunk_id in rows:
                    rows[chunk_id]["content_revision"] = new_rev
        state["round"] += 1
        return rows

    worker._load_current_rows = loaded


def _payload(targets: List[Dict[str, Any]], **extra) -> Dict[str, Any]:
    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_id": DOC,
        "source": "backfill_m5a",
        "targets": targets,
    }
    payload.update(extra)
    return payload


def _dump(rows: List[Dict[str, Any]] = None) -> None:
    with engine.begin() as conn:
        chunk_rows = conn.execute(
            text(
                "SELECT kb_id, chunk_id, content_revision, graph_status, graph_content_revision, "
                "vector_status, vector_content_revision FROM chunk_revisions ORDER BY kb_id, chunk_id"
            )
        ).fetchall()
        doc_rows = conn.execute(
            text(
                "SELECT doc_id, graph_status, vector_status FROM knowledge_base_documents ORDER BY doc_id"
            )
        ).fetchall()
        job_rows = conn.execute(
            text("SELECT job_type, kb_id, status, targets_hash FROM admin_jobs ORDER BY id")
        ).fetchall()
    print("__REVISIONS__" + json.dumps([list(r) for r in chunk_rows], ensure_ascii=False))
    print("__DOCUMENTS__" + json.dumps([list(r) for r in doc_rows], ensure_ascii=False))
    print("__JOBS__" + json.dumps([list(r) for r in job_rows], ensure_ascii=False))
    print("__NEO4J__" + json.dumps(W.neo, ensure_ascii=False, sort_keys=True))
    print("__MILVUS__" + json.dumps(W.mil, ensure_ascii=False, sort_keys=True))
    print(
        "__CALLS__"
        + json.dumps({"graph": W.graph_calls, "vector": W.vector_calls}, ensure_ascii=False)
    )


def _run(payload: Dict[str, Any], *, job_id: int = 7) -> None:
    try:
        result = runtime.execute_job(job_id=job_id, job_type="reindex_chunks", payload=payload)
        print("__RESULT__" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - 场景就是要断言异常类型/错误码
        detail = getattr(exc, "error_code", None) or getattr(exc, "message", str(exc))
        print("__EXCEPTION__" + json.dumps({"type": type(exc).__name__, "code": str(detail)}, ensure_ascii=False))
        # ValidationException.details 里的 counts 是 worker 抛错前已算好的收敛明细
        inner = getattr(exc, "details", None) or {}
        if isinstance(inner, dict) and isinstance(inner.get("counts"), dict):
            print("__COUNTS__" + json.dumps(inner["counts"], ensure_ascii=False, sort_keys=True))
    _dump()


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def scenario_happy() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="第一段")
    # c-2 停在 revision 3：证明投影版本号来自 target，不是硬编码 1
    _seed_rev("c-2", content="第二段", revision=3)
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}, {"chunk_id": "c-2", "target_revision": 3}]))


def scenario_outdated_revision() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="旧内容", revision=2)
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_no_current_row() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x", revision=1, status="superseded")
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_empty_targets() -> None:
    _seed_kb()
    _install()
    _run(_payload([]))


def scenario_illegal_target() -> None:
    _seed_kb()
    _install()
    _run(_payload([{"chunk_id": "c-1"}]))


def scenario_missing_scope() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install()
    _run({"doc_id": DOC, "targets": [{"chunk_id": "c-1", "target_revision": 1}]})


def scenario_registry_scope_conflict() -> None:
    _seed_kb(KB, tenant="t-authoritative", project=PROJECT)
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_row_scope_conflict() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x", tenant="t-other")
    _seed_rev("c-2", content="y")
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}, {"chunk_id": "c-2", "target_revision": 1}]))


def scenario_capability_off() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install(graph=False, vector=False)
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_revision_field_absent() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install(revision_field=False)
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_graph_write_failed() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install()
    W.fail_graph = True
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_current_moved() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install()
    W.move_after_write = {"c-1": 5}
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_current_deleted_race() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x")
    _install()
    real_load = worker._load_current_rows
    state = {"round": 0}

    def delete_before_fresh(kb_id: str, chunk_ids: List[str]):
        rows = real_load(kb_id, chunk_ids)
        if state["round"] == 1:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM chunk_revisions WHERE kb_id = :kb AND chunk_id = :chunk "
                        "AND revision_status = 'current'"
                    ),
                    {"kb": kb_id, "chunk": "c-1"},
                )
            return {}
        state["round"] += 1
        return rows

    worker._load_current_rows = delete_before_fresh
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_idempotent_rerun() -> None:
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="x", graph_status="indexed", graph_rev=1, vector_status="indexed", vector_rev=1)
    _install()
    _run(_payload([{"chunk_id": "c-1", "target_revision": 1}]))


def scenario_cross_kb_isolation() -> None:
    _seed_kb()
    _seed_kb(OTHER_KB, tenant="t2", project="p2")
    _seed_doc(DOC)
    _seed_rev("c-shared", content="本 kb 无行", kb_id=OTHER_KB, doc_id="doc-2")
    _install()
    _run(_payload([{"chunk_id": "c-shared", "target_revision": 1}]))


def scenario_doc_aggregation() -> None:
    _seed_kb()
    _seed_doc("doc-ok")
    _seed_doc("doc-mixed")
    _seed_rev("a-1", doc_id="doc-ok", content="ok")
    _seed_rev("b-1", doc_id="doc-mixed", content="x")
    _seed_rev("b-2", doc_id="doc-mixed", content="y")
    _install()
    W.fail_graph = True
    _run(
        _payload(
            [
                {"chunk_id": "a-1", "target_revision": 1},
                {"chunk_id": "b-1", "target_revision": 1},
            ],
            doc_id=None,
        )
    )


def scenario_dispatch_separation() -> None:
    """reindex（全文索引）与 reindex_chunks 必须各走各的分支，不能互相顶替。"""
    marker = {"chunk": 0, "fulltext": 0}
    runtime.execute_reindex_chunks = lambda **k: marker.__setitem__("chunk", marker["chunk"] + 1) or {"ok": True}
    runtime.execute_reindex = lambda **k: marker.__setitem__("fulltext", marker["fulltext"] + 1) or {"ok": True}
    for job_type in ("reindex", "reindex_chunks", "reindex"):
        runtime.execute_job(job_id=1, job_type=job_type, payload={"kb_id": KB, "targets": []})
    try:
        runtime.execute_job(job_id=1, job_type="reindex_documents", payload={})
    except Exception as exc:  # noqa: BLE001
        print("__UNKNOWN__" + type(exc).__name__)
    print("__DISPATCH__" + json.dumps(marker))


def scenario_existing_fields_merge() -> None:
    """真实 _existing_milvus_fields：整行替换前必须读回 title/location/entities。"""
    captured: Dict[str, Any] = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def has_collection(self, name):
            return True

        def describe_collection(self, name):
            return {
                "fields": [
                    {"name": "chunk_id"},
                    {"name": "doc_id"},
                    {"name": "title"},
                    {"name": "location"},
                    {"name": "entities_json"},
                    {"name": "embedding_model"},
                ]
            }

        def query(self, **kwargs):
            captured["filter"] = kwargs.get("filter")
            captured["output_fields"] = kwargs.get("output_fields")
            return [
                {
                    "chunk_id": "c-1",
                    "doc_id": DOC,
                    "title": "标题",
                    "location": "p.1",
                    "entities_json": '["张三", ""]',
                    "embedding_model": "m-1",
                }
            ]

    import pymilvus

    pymilvus.MilvusClient = FakeClient
    merged = worker._existing_milvus_fields(KB, ["c-1"])
    print("__MERGE__" + json.dumps({"merged": merged, "call": captured}, ensure_ascii=False, sort_keys=True))


def scenario_upsert_guard_blocks_dynamic_field() -> None:
    """真实 upsert_chunks：collection 无显式字段时必须拒写，不许落进 dynamic metadata。"""
    from services.vector_store import VectorChunk, vector_store

    captured: Dict[str, Any] = {}

    class FakeClient:
        def upsert(self, **kwargs):
            captured["rows"] = kwargs.get("data")
            return {"upsert_count": len(kwargs.get("data") or [])}

    vector_store._get_client = lambda: FakeClient()
    # 缓存注入让真实的 has_content_revision_field 走"无显式字段"分支（不联网 describe）
    vector_store._revision_field = {"test_collection": False}
    vector_store.is_enabled = lambda: True
    vector_store.config = lambda: {"collection": "test_collection", "enabled": True, "provider": "milvus"}
    vector_store.ensure_collection = lambda **k: None
    chunk = VectorChunk(
        chunk_id="c-1",
        doc_id=DOC,
        text="x",
        kb_id=KB,
        tenant_id=TENANT,
        project_id=PROJECT,
        content_revision=3,
    )
    try:
        vector_store.upsert_chunks([chunk], [[0.1, 0.2]])
        print("__GUARD__" + json.dumps({"raised": False, "rows": captured.get("rows")}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print("__GUARD__" + json.dumps({"raised": True, "type": type(exc).__name__, "error": str(exc)[:200]}, ensure_ascii=False))


def scenario_schema_and_upsert_contract() -> None:
    from services.vector_store import VectorChunk, content_revision_field_is_int64, vector_store

    class SchemaClient:
        def __init__(self, field_type):
            self.field_type = field_type

        def has_collection(self, name):
            return True

        def describe_collection(self, name):
            return {"fields": [{"name": "content_revision", "data_type": self.field_type}]}

    schema = {
        "int64": content_revision_field_is_int64(SchemaClient(5), "v3"),
        "varchar": content_revision_field_is_int64(SchemaClient("VARCHAR"), "v3"),
        "missing_type": content_revision_field_is_int64(SchemaClient(None), "v3"),
    }
    vector_store._get_client = lambda: type(
        "MutationClient",
        (),
        {
            "upsert": lambda self, **kwargs: {"upsert_count": 0},
        },
    )()
    vector_store._revision_field = {"test_collection": True}
    vector_store.is_enabled = lambda: True
    vector_store.config = lambda: {"collection": "test_collection", "enabled": True, "provider": "milvus"}
    vector_store.ensure_collection = lambda **k: None
    chunk = VectorChunk(
        chunk_id="c-1", doc_id=DOC, text="x", kb_id=KB, tenant_id=TENANT, project_id=PROJECT, content_revision=1
    )
    try:
        vector_store.upsert_chunks([chunk], [[0.1, 0.2]])
        mutation = {"type": "none"}
    except Exception as exc:  # noqa: BLE001
        mutation = {"type": type(exc).__name__, "message": str(exc)}
    print("__SCHEMA_CONTRACT__" + json.dumps({"schema": schema, "mutation": mutation}, ensure_ascii=False, sort_keys=True))


def scenario_closed_loop() -> None:
    """backfill 入队 → worker 消费 → 索引侧更新 → 复跑 inventory 必须 CLOSED。"""
    _seed_kb()
    _seed_doc(DOC)
    _seed_rev("c-1", content="已有行但投影缺失", graph_status="pending", vector_status="pending")

    bf._load_neo4j_chunks = lambda kb: {
        chunk_id: dict(value)
        for key, value in W.neo.items()
        for chunk_id in [key.split("|", 1)[1]]
        if key.startswith(f"{kb}|")
        for value in [
            {
                "doc_id": value["doc_id"],
                "text": value["text"],
                "tenant_id": TENANT,
                "project_id": PROJECT,
                "parser_version": "",
                "content_revision": value["content_revision"],
            }
        ]
    }
    bf._load_milvus_chunks = lambda kb: {
        key.split("|", 1)[1]: {
            "doc_id": DOC,
            "text": value["text"],
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "parser_version": "",
            "content_revision": value["content_revision"],
        }
        for key, value in W.mil.items()
        if key.startswith(f"{kb}|")
    }
    bf._load_parsed_chunks = lambda kb: {}
    bf._graph_capability_enabled = lambda: True
    bf._vector_capability_enabled = lambda: True
    bf._milvus_client = lambda: (None, "test_collection")
    bf._milvus_has_revision_field = lambda client, coll: True

    inventory = bf.build_inventory(KB)
    needs = [dict(item, kb_id=KB) for item in inventory.needs_reindex_targets]
    enqueue = bf._enqueue_reindex_jobs(needs, "trace-loop")
    with engine.begin() as conn:
        job = conn.execute(
            text("SELECT payload FROM admin_jobs WHERE job_type = 'reindex_chunks' ORDER BY id LIMIT 1")
        ).fetchone()
    print("__ENQUEUE__" + json.dumps({**enqueue, "has_job": job is not None}, ensure_ascii=False))

    _install()
    payload = json.loads(job[0])
    result = runtime.execute_job(job_id=1, job_type="reindex_chunks", payload=payload)
    print("__CONSUMED__" + json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))

    after = bf.build_inventory(KB)
    gate = bf.evaluate_gate(after)
    print("__GATE__" + json.dumps(gate, ensure_ascii=False, sort_keys=True))
    second = bf._enqueue_reindex_jobs([dict(i, kb_id=KB) for i in after.needs_reindex_targets], "trace-loop-2")
    print("__REENQUEUE__" + json.dumps(second, ensure_ascii=False))
    _dump()


SCENARIOS = {
    "happy": scenario_happy,
    "outdated_revision": scenario_outdated_revision,
    "no_current_row": scenario_no_current_row,
    "empty_targets": scenario_empty_targets,
    "illegal_target": scenario_illegal_target,
    "missing_scope": scenario_missing_scope,
    "registry_scope_conflict": scenario_registry_scope_conflict,
    "row_scope_conflict": scenario_row_scope_conflict,
    "capability_off": scenario_capability_off,
    "revision_field_absent": scenario_revision_field_absent,
    "graph_write_failed": scenario_graph_write_failed,
    "current_moved": scenario_current_moved,
    "current_deleted_race": scenario_current_deleted_race,
    "idempotent_rerun": scenario_idempotent_rerun,
    "cross_kb_isolation": scenario_cross_kb_isolation,
    "doc_aggregation": scenario_doc_aggregation,
    "dispatch_separation": scenario_dispatch_separation,
    "existing_fields_merge": scenario_existing_fields_merge,
    "upsert_guard": scenario_upsert_guard_blocks_dynamic_field,
    "schema_and_upsert_contract": scenario_schema_and_upsert_contract,
    "closed_loop": scenario_closed_loop,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-B0 reindex_chunks driver")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args()
    _require_sqlite()
    _bootstrap()
    SCENARIOS[args.scenario]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
