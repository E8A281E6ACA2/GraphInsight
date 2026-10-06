#!/usr/bin/env python3
"""
M5-A backfill 测试驱动（由 check_m5a_revision_backfill.py 以子进程调用）

注入假证据源（Neo4j/Milvus/解析产物/能力开关）到 backfill 模块函数，
在临时 SQLite 库上执行冻结契约场景；退出码 = 场景期望退出码。
引擎方言非 sqlite 时立即退出（9），防止误连开发库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

import admin.backfill_chunk_revisions as bf  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _require_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        print(f"FATAL: engine dialect is {engine.dialect.name}, expected sqlite (isolation broken)")
        raise SystemExit(9)


def _sha(value: str) -> str:
    import hashlib

    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _insert_revision(
    kb: str,
    chunk_id: str,
    doc_id: str,
    content: str,
    revision: int = 1,
    status: str = "current",
    graph_status: str = "pending",
    graph_rev=None,
    vector_status: str = "pending",
    vector_rev=None,
    revision_source: str = "system_reparse",
    tenant: str = "t1",
    project: str = "p1",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, graph_content_revision, vector_content_revision, "
                "revision_source, reason, trace_id) VALUES "
                "(:kb, :tenant, :project, :doc, :chunk, :content, :sighash, :content, :contenthash, :rev, "
                ":status, :gstatus, :vstatus, :grev, :vrev, :source, 'test_fixture', 'test')"
            ),
            {
                "kb": kb,
                "tenant": tenant,
                "project": project,
                "doc": doc_id,
                "chunk": chunk_id,
                "content": content,
                "sighash": _sha(content),
                "contenthash": _sha(content),
                "rev": revision,
                "status": status,
                "gstatus": graph_status,
                "vstatus": vector_status,
                "grev": graph_rev,
                "vrev": vector_rev,
                "source": revision_source,
            },
        )


def _ensure_kb_table(kb_id: str, tenant_id: str, project_id: str) -> None:
    """播种 knowledge_bases 行，作为作用域一致性校验的权威来源。"""
    from admin.database import Base
    from admin.models import KnowledgeBase

    Base.metadata.create_all(bind=engine, tables=[KnowledgeBase.__table__])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:kb, :tenant, :project, :name, 'active', :prefix) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "kb": kb_id,
                "tenant": tenant_id,
                "project": project_id,
                "name": f"fixture {kb_id}",
                "prefix": f"kb/{kb_id}",
            },
        )


def _ensure_doc_table() -> None:
    from admin.database import Base
    from admin.models import KnowledgeBaseDocument

    Base.metadata.create_all(bind=engine, tables=[KnowledgeBaseDocument.__table__])


def _seed_doc(kb_id: str, doc_id: str, graph_status: str = "pending", vector_status: str = "pending") -> None:
    _ensure_doc_table()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_base_documents (doc_id, kb_id, tenant_id, project_id, name, "
                "relative_path, source_type, size, sha256, version, status, graph_status, vector_status) "
                "VALUES (:doc, :kb, 't1', 'p1', :name, :path, 'upload', 10, :sha, 1, 'indexed', :graph, :vector) "
                "ON CONFLICT (doc_id) DO NOTHING"
            ),
            {
                "doc": doc_id,
                "kb": kb_id,
                "name": f"{doc_id}.md",
                "path": f"kb/{kb_id}/{doc_id}.md",
                "sha": _sha(doc_id),
                "graph": graph_status,
                "vector": vector_status,
            },
        )


def _patch_sources(
    neo: Dict[str, Dict[str, dict]] = None,
    mil: Dict[str, Dict[str, dict]] = None,
    parsed: Dict[str, Dict[str, dict]] = None,
    graph: bool = True,
    vector: bool = False,
    milvus_outcome=None,
    neo_fail: bool = False,
):
    neo_map, mil_map, parsed_map = neo or {}, mil or {}, parsed or {}
    bf._load_neo4j_chunks = lambda kb: {k: dict(v) for k, v in neo_map.get(kb, {}).items()}
    bf._load_milvus_chunks = lambda kb: {k: dict(v) for k, v in mil_map.get(kb, {}).items()}
    bf._load_parsed_chunks = lambda kb: {k: dict(v) for k, v in parsed_map.get(kb, {}).items()}
    bf._graph_capability_enabled = lambda: graph
    bf._vector_capability_enabled = lambda: vector
    calls: list = []

    def fake_neo(kb, plans):
        calls.extend(p.chunk_id for p in plans)
        if neo_fail:
            return {p.chunk_id: "failed" for p in plans}
        return {p.chunk_id: "indexed" for p in plans}

    def fake_mil(kb, plans):
        outcome = milvus_outcome or (lambda _kb, ps: {p.chunk_id: "revision_field_absent" for p in ps})
        return outcome(kb, plans)

    bf._backfill_neo4j = fake_neo
    bf._backfill_milvus = fake_mil
    bf._milvus_client = lambda: (None, "test_collection")
    bf._milvus_has_revision_field = lambda client, coll: False
    return calls


def _mil_indexed(_kb, plans):
    return {p.chunk_id: "indexed" for p in plans}


def _dump_state(calls: list = None) -> None:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT kb_id, chunk_id, doc_id, content_revision, revision_status, "
                "graph_status, graph_content_revision, vector_status, vector_content_revision, "
                "content, revision_source, reason FROM chunk_revisions "
                "ORDER BY kb_id, chunk_id, content_revision"
            )
        ).fetchall()
        jobs = []
        try:
            jobs = conn.execute(
                text("SELECT job_type, kb_id, status, targets_hash, payload FROM admin_jobs ORDER BY id")
            ).fetchall()
        except Exception:  # noqa: BLE001 - 迁移测试库可能没有 admin_jobs，dump 视为空
            pass
    print("__ROWS__" + json.dumps([list(r) for r in rows], ensure_ascii=False))
    docs = []
    try:
        with engine.begin() as conn:
            docs = conn.execute(
                text("SELECT kb_id, doc_id, graph_status, vector_status FROM knowledge_base_documents ORDER BY doc_id")
            ).fetchall()
    except Exception:  # noqa: BLE001
        docs = []
    print("__JOBS__" + json.dumps([list(j) for j in jobs], ensure_ascii=False))
    logs = []
    try:
        with engine.begin() as conn:
            logs = conn.execute(
                text(
                    "SELECT action, resource, resource_id, kb_id, trace_id, details, status "
                    "FROM admin_logs ORDER BY id"
                )
            ).fetchall()
    except Exception:  # noqa: BLE001 - 未建 admin_logs 的引导库视为空，缺表由退出码 4 取证
        logs = []
    print("__AUDITLOGS__" + json.dumps([list(x) for x in logs], ensure_ascii=False))
    print("__DOCS__" + json.dumps([list(d) for d in docs], ensure_ascii=False))
    if calls is not None:
        print("__CALLS__" + json.dumps(calls, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 场景（对应验收矩阵）
# ---------------------------------------------------------------------------


def s_new_and_degraded(kb="kb-a"):
    # graph 能力开启、vector 能力关闭：vector 投影 skipped/NULL → DEGRADED_SKIPPED
    parsed = {kb: {"c1": {"doc_id": "d1", "text": "内容一", "parser_version": "p1", "source_version": "h1"}}}
    neo = {kb: {"c1": {"doc_id": "d1", "text": "内容一", "tenant_id": "t1", "project_id": "p1", "parser_version": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    code = bf.run(kb, dry_run=False)
    _dump_state()
    return code


def s_no_downgrade():
    _insert_revision("kb-a", "e1", "d9", "E-keep", graph_status="indexed", graph_rev=1, vector_status="indexed", vector_rev=1)
    parsed = {
        "kb-a": {
            "e1": {"doc_id": "d9", "text": "E-keep"},
            "n1": {"doc_id": "d1", "text": "N-new"},
        }
    }
    neo = {
        "kb-a": {
            "e1": {"doc_id": "d9", "text": "E-keep", "tenant_id": "t1", "project_id": "p1", "content_revision": 1},
            "n1": {"doc_id": "d1", "text": "N-new", "tenant_id": "t1", "project_id": "p1", "content_revision": 1},
        },
    }
    mil = {
        "kb-a": {
            "e1": {"doc_id": "d9", "text": "E-keep", "tenant_id": "t1", "project_id": "p1", "content_revision": 1},
        },
    }
    calls = _patch_sources(neo=neo, mil=mil, parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state(calls)
    return code


def s_nr_setup():
    _insert_revision("kb-a", "x1", "d2", "X-old", graph_status="pending", graph_rev=None, vector_status="skipped", vector_rev=None)
    _dump_state()
    return 0


def _nr_env():
    parsed = {"kb-a": {"x1": {"doc_id": "d2", "text": "X-old"}}}
    _patch_sources(parsed=parsed, graph=True, vector=False)


def s_nr_run():
    _nr_env()
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_nr_dry_preview():
    _nr_env()
    code = bf.run("kb-a", dry_run=True)
    _dump_state()
    return code


def s_nr_finish():
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE chunk_revisions SET graph_status='indexed', graph_content_revision=1 "
                 "WHERE kb_id='kb-a' AND chunk_id='x1' AND revision_status='current'")
        )
    return 0


def s_nr_converged():
    parsed = {"kb-a": {"x1": {"doc_id": "d2", "text": "X-old"}}}
    neo = {"kb-a": {"x1": {"doc_id": "d2", "text": "X-old", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_blocked_gate():
    _insert_revision("kb-a", "y1", "d3", "Y-old", graph_status="pending", graph_rev=None, vector_status="skipped", vector_rev=None)
    parsed = {"kb-a": {"y1": {"doc_id": "d3", "text": "Y-old"}}}
    _patch_sources(parsed=parsed, graph=False, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_unrecoverable_dry():
    parsed = {"kb-a": {"u9": {"doc_id": "d4", "text": ""}}}
    _patch_sources(parsed=parsed, graph=True, vector=False)
    return bf.run("kb-a", dry_run=True)


def s_scope_unresolved():
    # 活栈实测纠偏：解析产物有文本（内容可恢复）但 KB 未登记、索引侧无证据，
    # 三件套不全必须单独计为 SCOPE_UNRESOLVED，不得混进 UNRECOVERABLE_MISMATCH。
    parsed = {"kb-missing": {"s1": {"doc_id": "d7", "text": "S-有内容"}}}
    _patch_sources(parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-missing", dry_run=True)
    _dump_state()
    return code


def s_collection_resolution():
    """backfill 必须共用 services.vector_store 的 collection 归一化（活栈纠偏的可重复版）。

    只替换 runtime 配置读取（数据源），不替换 vector_store.config() 的归一化分支本身。
    """
    import services.vector_store as vs

    original = vs.get_vector_store_runtime_config
    resolved: dict = {}
    try:
        for label, name in (("legacy", "graphinsight_chunks"), ("explicit", "kb_scope_coll_1536")):
            vs.get_vector_store_runtime_config = lambda _n=name: {"enabled": True, "collection": _n, "provider": "milvus"}
            resolved[label] = bf._milvus_collection_name()
    finally:
        vs.get_vector_store_runtime_config = original
    print("__COLL_LEGACY__" + str(resolved.get("legacy")))
    print("__COLL_EXPLICIT__" + str(resolved.get("explicit")))
    ok = resolved.get("legacy") == "graphinsight_chunks_v2" and resolved.get("explicit") == "kb_scope_coll_1536"
    return 0 if ok else 1


def s_dual_kb():
    # kb-b 已有 current 行；跑 kb-a backfill 不得触碰 kb-b（即使注入相同 chunk_id 也不串写）
    _insert_revision("kb-b", "z9", "db1", "B-keep", graph_status="indexed", graph_rev=1, vector_status="skipped", vector_rev=None)
    parsed = {
        "kb-a": {"z9": {"doc_id": "da1", "text": "A-new"}},
        "kb-b": {"z9": {"doc_id": "db1", "text": "B-keep"}},
    }
    neo = {
        "kb-a": {"z9": {"doc_id": "da1", "text": "A-new", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}},
        "kb-b": {"z9": {"doc_id": "db1", "text": "B-keep", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}},
    }
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_current_unique():
    _insert_revision("kb-a", "cu1", "d5", "CU-current", revision=1, status="current")
    rejected = False
    try:
        _insert_revision("kb-a", "cu1", "d5", "CU-second-current", revision=2, status="current")
    except IntegrityError:
        rejected = True
    print("__DUP_CURRENT_REJECTED__" + str(rejected))
    superseded_ok = True
    try:
        _insert_revision("kb-a", "cu1", "d5", "CU-history", revision=2, status="superseded")
    except IntegrityError:
        superseded_ok = False
    print("__SUPERSEDED_ALLOWED__" + str(superseded_ok))
    _dump_state()
    return 0 if (rejected and superseded_ok) else 1


def s_rfa_run():
    # §8.5：vector 能力开启但 collection 无 content_revision 字段 → pending + MILVUS_REVISION_FIELD_ABSENT
    # 同时锁住活栈取证发现的静默漏排：本轮新写入但 pending 的 chunk 必须同一轮排入 reindex job，
    # 重复调用（rfa_rerun 场景）复用同一 targets_hash 不新建 job。
    parsed = {"kb-a": {"f1": {"doc_id": "d1", "text": "内容一"}}}
    neo = {"kb-a": {"f1": {"doc_id": "d1", "text": "内容一", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=True)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_orphan_gate():
    """审计修复 #1：PG 有 current 行但 Neo4j/Milvus 均无该 chunk，必须显式阻断而非静默跳过。"""
    _insert_revision(
        "kb-a", "o1", "d7", "O-orphan",
        graph_status="indexed", graph_rev=1, vector_status="indexed", vector_rev=1,
    )
    _patch_sources(neo={}, mil={}, parsed={}, graph=False, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_orphan_converged():
    """孤儿行补齐索引证据后，不再计为 orphan，门可关闭。"""
    parsed = {"kb-a": {"o1": {"doc_id": "d7", "text": "O-orphan"}}}
    neo = {"kb-a": {"o1": {"doc_id": "d7", "text": "O-orphan", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    mil = {"kb-a": {"o1": {"doc_id": "d7", "text": "O-orphan", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, mil=mil, parsed=parsed, graph=False, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_unrecoverable_neo_only():
    """审计修复 #2：只有 Neo4j 残留文本、无解析产物且无 Milvus 记录 = UNRECOVERABLE。"""
    neo = {"kb-a": {"n9": {"doc_id": "d8", "text": "N-only", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, mil={}, parsed={}, graph=True, vector=True)
    code = bf.run("kb-a", dry_run=True)
    _dump_state()
    return code


def s_scope_conflict_new():
    """审计修复 #4：new_chunk 的 tenant 与 knowledge_bases 登记冲突 → 零写入 fail-closed。"""
    _ensure_kb_table("kb-a", "t1", "p1")
    parsed = {"kb-a": {"g1": {"doc_id": "d9", "text": "G-conflict"}}}
    neo = {"kb-a": {"g1": {"doc_id": "d9", "text": "G-conflict", "tenant_id": "t9", "project_id": "p1", "content_revision": 1}}}
    calls = _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state(calls)
    return code


def s_scope_conflict_row():
    """审计修复 #4：已有 revision 行 project 与 KB 登记冲突 → 零写入 fail-closed。"""
    _ensure_kb_table("kb-a", "t1", "p1")
    _insert_revision("kb-a", "g2", "d10", "G-row-conflict", graph_status="pending", vector_status="pending", project="pX")
    parsed = {"kb-a": {"g2": {"doc_id": "d10", "text": "G-row-conflict"}}}
    calls = _patch_sources(parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state(calls)
    return code


def s_direct_document_aggregation():
    _ensure_kb_table("kb-a", "t1", "p1")
    _seed_doc("kb-a", "d-direct")
    parsed = {"kb-a": {"direct-1": {"doc_id": "d-direct", "text": "direct content", "parser_version": "p1"}}}
    neo = {"kb-a": {"direct-1": {"doc_id": "d-direct", "text": "direct content", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    code = bf.run("kb-a", dry_run=False)
    _dump_state()
    return code


def s_direct_cas_failure():
    _ensure_kb_table("kb-a", "t1", "p1")
    _seed_doc("kb-a", "d-cas")
    parsed = {"kb-a": {"cas-1": {"doc_id": "d-cas", "text": "cas content"}}}
    neo = {"kb-a": {"cas-1": {"doc_id": "d-cas", "text": "cas content", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    original = bf._update_projection_state
    bf._update_projection_state = lambda *args, **kwargs: 0
    try:
        code = bf.run("kb-a", dry_run=False)
    finally:
        bf._update_projection_state = original
    _dump_state()
    return code


def s_backfill_document_aggregation_failure():
    _ensure_kb_table("kb-a", "t1", "p1")
    _seed_doc("kb-a", "d-agg-error")
    parsed = {"kb-a": {"agg-1": {"doc_id": "d-agg-error", "text": "aggregate error content"}}}
    neo = {"kb-a": {"agg-1": {"doc_id": "d-agg-error", "text": "aggregate error content", "tenant_id": "t1", "project_id": "p1", "content_revision": 1}}}
    _patch_sources(neo=neo, parsed=parsed, graph=True, vector=False)
    import services.chunk_projection_state as state

    original = state.aggregate_document_states

    def fail(*_args, **_kwargs):
        raise RuntimeError("document aggregation database unavailable")

    state.aggregate_document_states = fail
    try:
        bf.run("kb-a", dry_run=False)
    except Exception as exc:  # noqa: BLE001 - aggregation failure must escape run()
        print("__AGGREGATION_ERROR__" + json.dumps({"type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        code = 1
    else:
        print("__AGGREGATION_ERROR__" + json.dumps({"type": None}, ensure_ascii=False))
        code = 0
    finally:
        state.aggregate_document_states = original
    _dump_state()
    return code


SCENARIOS = {
    "new_and_degraded": lambda: s_new_and_degraded(),
    "new_and_degraded_rerun": lambda: s_new_and_degraded(),
    "no_downgrade": s_no_downgrade,
    "nr_setup": s_nr_setup,
    "nr_dry_preview": s_nr_dry_preview,
    "nr_run": s_nr_run,
    "nr_run_again": s_nr_run,
    "nr_finish": s_nr_finish,
    "nr_converged": s_nr_converged,
    "blocked_gate": s_blocked_gate,
    "unrecoverable_dry": s_unrecoverable_dry,
    "dual_kb": s_dual_kb,
    "current_unique": s_current_unique,
    "rfa_run": s_rfa_run,
    "rfa_rerun": lambda: s_rfa_run(),
    "orphan_gate": s_orphan_gate,
    "orphan_converged": s_orphan_converged,
    "unrecoverable_neo_only": s_unrecoverable_neo_only,
    "scope_conflict_new": s_scope_conflict_new,
    "scope_conflict_row": s_scope_conflict_row,
    "direct_document_aggregation": s_direct_document_aggregation,
    "direct_cas_failure": s_direct_cas_failure,
    "backfill_document_aggregation_failure": s_backfill_document_aggregation_failure,
    "scope_unresolved": s_scope_unresolved,
    "collection_resolution": s_collection_resolution,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="M5-A backfill test driver (sqlite only)")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args()
    _require_sqlite()
    return SCENARIOS[args.scenario]()


if __name__ == "__main__":
    raise SystemExit(main())
