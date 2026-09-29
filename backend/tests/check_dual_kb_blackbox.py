"""
数据层双 KB 黑盒隔离测试（M3/M4 验收门）

对真实服务（PostgreSQL 注册表 + Neo4j 图谱 + Milvus 向量）做数据层黑盒验证。

验收前置（M4-R1 审计 P1-4）：双 KB 完成标准要求 PG、Neo4j、Milvus 全部可用。
缺任一核心服务时明确返回 NOT_RUN（退出码 2），不执行半程断言（避免图谱段/向量段
在缺少 Neo4j 时调用 build_graph 崩溃），也不返回看似成功的退出码。

环境约定：
  ADMIN_DATABASE_URL          PG 注册表连接（必需）
  NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
  MILVUS_URI                  （脚本缺省补 VECTOR_STORE_ENABLED=true）
  embedding 使用确定性 hash 伪向量（8 维，monkeypatch），无需 LLM / API key。

验证点：
  1) kb-a 图谱总量在写入 kb-b 文档后保持不变
  2) 同名实体（品种A）在两个 KB 中是两个节点、entity_key 不同、kb_id 不同
  3) 作用域化清空 kb-a 只删除 kb-a 的节点/关系/向量/产物，kb-b 不受影响
  4) Milvus 带 kb-a 过滤的检索只返回 kb-a 的 chunk
  5) retrieve() 不带 kb -> KB_SCOPE_REQUIRED；带 kb 时结果不跨库
  6) Neo4j 存在 (entity_key, kb_id) 复合唯一约束
  结束后清理两个 KB 的图谱/向量/注册表行/文件。

退出码：0=通过；1=失败；2=NOT_RUN（核心服务缺失，未达验收前置，非通过）。
运行：python backend/tests/check_dual_kb_blackbox.py
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

# 环境变量必须在导入 backend 配置之前就位（config/runtime_config 在导入期读取 env）。
os.environ.setdefault("VECTOR_STORE_ENABLED", "true")
os.environ.setdefault("EMBEDDING_DIMENSION", "8")
os.environ.setdefault("GRAPHINSIGHT_BACKEND_ENV_FILE", "")
# 默认凭据与 docker-compose.dev.yml 对齐（审计要求：脚本开箱即用，env 仍可覆盖）。
os.environ.setdefault("ADMIN_DATABASE_URL", "postgresql://graphinsight:graphinsight-dev-password@127.0.0.1:5434/graphinsight_admin")
os.environ.setdefault("NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "change-this-password")
os.environ.setdefault("NEO4J_DATABASE", "neo4j")
os.environ.setdefault("MILVUS_URI", "http://127.0.0.1:19530")

FAKE_DIM = 8
TENANT = "tenant-a"
PROJECT = "project-a"
KB_A = "kb-a"
KB_B = "kb-b"
DOC_A = "doc-blackbox-a"
DOC_B = "doc-blackbox-b"
SHARED_ENTITY = "品种A"

PASS: list[str] = []
FAIL: list[str] = []
SKIPS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def skip(reason: str) -> None:
    SKIPS.append(reason)
    print(f"  SKIP: {reason}")


def expect_error(name: str, fn, code: str) -> None:
    from core.exceptions import KnowledgeScopeError

    try:
        fn()
    except KnowledgeScopeError as exc:
        check(name, exc.error_code == code, f"(got {exc.error_code})")
        return
    except TypeError:
        check(name, False, "(kb_ids is a required keyword argument, TypeError instead of scope error)")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"(unexpected {type(exc).__name__}: {exc})")
        return
    check(name, False, "(no error raised)")


# ---------------------------------------------------------------- 伪 embedding（确定性 hash 向量）


def fake_vector(text: str, dim: int = FAKE_DIM) -> list[float]:
    digest = hashlib.sha256(("fake-embed|" + str(text or "")).encode("utf-8", errors="ignore")).digest()
    raw = [digest[i % len(digest)] / 255.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [round(v / norm, 6) for v in raw]


@contextmanager
def fake_embedding():
    """用确定性伪向量替换 embedding 服务，使索引/检索无需外部 API。"""
    import services.retrieval_orchestrator as ro

    svc = ro.embedding_service
    originals = {name: getattr(svc, name) for name in ("is_enabled", "embed_texts", "embed_query", "content_hash", "config")}

    def _is_enabled() -> bool:
        return True

    def _config() -> dict:
        return {"enabled": True, "model": "fake-hash", "dimension": FAKE_DIM, "batch_size": 8}

    def _embed_texts(texts):
        return [fake_vector(t) for t in texts]

    def _embed_query(text):
        return fake_vector(text)

    def _content_hash(text: str) -> str:
        return hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()

    try:
        setattr(svc, "is_enabled", _is_enabled)
        setattr(svc, "embed_texts", _embed_texts)
        setattr(svc, "embed_query", _embed_query)
        setattr(svc, "content_hash", _content_hash)
        setattr(svc, "config", _config)
        yield
    finally:
        for name, value in originals.items():
            setattr(svc, name, value)


@contextmanager
def synthetic_extraction():
    """建图时注入固定的合成实体/关系（两个 KB 使用同名实体 品种A）。"""
    import services.document_graph_service as dgs

    def fake_entities(_self, *_args, **_kwargs):
        return [SHARED_ENTITY, "作物A"]

    def fake_relations(_self, *_args, **_kwargs):
        return [
            {
                "source": SHARED_ENTITY,
                "target": "作物A",
                "label": "属于",
                "rel_type": "BELONGS_TO",
                "relation_type": "属于",
                "confidence": 0.9,
                "evidence": f"{SHARED_ENTITY} 属于 作物A",
            }
        ]

    with patch.object(dgs.DocumentGraphService, "_extract_entities", fake_entities), patch.object(
        dgs.DocumentGraphService, "_extract_relations", fake_relations
    ):
        yield


@contextmanager
def sandbox_storage():
    """把 documents/parsed 根目录指到临时目录，结束自动清理文件产物。"""
    from config import get_settings

    settings = get_settings()
    old_docs = settings.document_storage_path
    old_parsed = settings.parsed_document_storage_path
    with tempfile.TemporaryDirectory(prefix="gi_blackbox_") as tmp:
        docs_root = Path(tmp) / "documents"
        parsed_root = Path(tmp) / "parsed"
        docs_root.mkdir()
        parsed_root.mkdir()
        settings.document_storage_path = str(docs_root)
        settings.parsed_document_storage_path = str(parsed_root)
        try:
            yield docs_root
        finally:
            settings.document_storage_path = old_docs
            settings.parsed_document_storage_path = old_parsed


# ---------------------------------------------------------------- 服务可用性探测


def probe_postgres() -> bool:
    try:
        from sqlalchemy import text

        from admin.database import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        skip(f"PostgreSQL 注册表不可用（ADMIN_DATABASE_URL={os.getenv('ADMIN_DATABASE_URL', '')!r}）：{type(exc).__name__}: {exc}")
        return False


def probe_neo4j():
    try:
        import neo4j  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        skip(f"neo4j 驱动未安装，跳过图谱断言：{exc}")
        return None
    try:
        from services.neo4j_service import get_neo4j_service

        service = get_neo4j_service()
        service.driver.verify_connectivity()
        return service
    except Exception as exc:  # noqa: BLE001
        skip(f"Neo4j 不可达（NEO4J_URI={os.getenv('NEO4J_URI', '')!r}），跳过图谱断言：{type(exc).__name__}: {exc}")
        return None


def probe_milvus():
    try:
        import pymilvus  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        skip(f"pymilvus 未安装，跳过向量断言：{exc}")
        return None
    try:
        from services.vector_store import vector_store

        if not vector_store.is_enabled():
            skip("VECTOR_STORE_ENABLED 未开启（或 provider 不是 milvus），跳过向量断言")
            return None
        client = vector_store._get_client()
        client.has_collection(vector_store.config()["collection"])  # 探活
        return vector_store
    except Exception as exc:  # noqa: BLE001
        skip(f"Milvus 不可达（MILVUS_URI={os.getenv('MILVUS_URI', '')!r}），跳过向量断言：{type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------- 注册表 / 文件准备


def reset_registry_rows() -> None:
    from admin.models import KnowledgeBase, KnowledgeBaseDocument
    from admin.database import SessionLocal

    db = SessionLocal()
    try:
        db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.kb_id.in_([KB_A, KB_B])).delete(
            synchronize_session=False
        )
        db.query(KnowledgeBase).filter(KnowledgeBase.id.in_([KB_A, KB_B])).delete(synchronize_session=False)
        db.commit()
        db.add_all(
            [
                KnowledgeBase(
                    id=KB_A,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    name="Blackbox KB A",
                    status="active",
                    storage_prefix=f"{TENANT}/{PROJECT}/{KB_A}",
                ),
                KnowledgeBase(
                    id=KB_B,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    name="Blackbox KB B",
                    status="active",
                    storage_prefix=f"{TENANT}/{PROJECT}/{KB_B}",
                ),
                KnowledgeBaseDocument(
                    doc_id=DOC_A,
                    kb_id=KB_A,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    name="a.txt",
                    relative_path=f"{DOC_A}/versions/v1/source/a.txt",
                    source_type="upload",
                    mime_type="text/plain",
                    size=1,
                    sha256=hashlib.sha256(b"a").hexdigest(),
                    version=1,
                    status="uploaded",
                ),
                KnowledgeBaseDocument(
                    doc_id=DOC_B,
                    kb_id=KB_B,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    name="b.txt",
                    relative_path=f"{DOC_B}/versions/v1/source/b.txt",
                    source_type="upload",
                    mime_type="text/plain",
                    size=1,
                    sha256=hashlib.sha256(b"b").hexdigest(),
                    version=1,
                    status="uploaded",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()


def drop_registry_rows() -> None:
    try:
        from admin.models import KnowledgeBase, KnowledgeBaseDocument
        from admin.database import SessionLocal

        db = SessionLocal()
        try:
            db.query(KnowledgeBaseDocument).filter(KnowledgeBaseDocument.kb_id.in_([KB_A, KB_B])).delete(
                synchronize_session=False
            )
            db.query(KnowledgeBase).filter(KnowledgeBase.id.in_([KB_A, KB_B])).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  (cleanup) 注册表清理失败：{exc}")


def write_source_files(docs_root: Path) -> None:
    text_a = (
        f"{SHARED_ENTITY} 属于 作物A。{SHARED_ENTITY} 在试验中表现稳定，"
        "叶片与籽粒观测数据完整，试验记录了完整的对照结果。"
    )
    text_b = (
        f"{SHARED_ENTITY} 属于 作物A。{SHARED_ENTITY} 在第二个知识库的试验中表现良好，"
        "观测与对照数据完整，用于验证同名实体跨库隔离。"
    )
    for doc_id, file_name, text in ((DOC_A, "a.txt", text_a), (DOC_B, "b.txt", text_b)):
        kb = KB_A if doc_id == DOC_A else KB_B
        target = docs_root / TENANT / PROJECT / kb / doc_id / "versions" / "v1" / "source"
        target.mkdir(parents=True, exist_ok=True)
        (target / file_name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 图谱 / 向量操作


def build_kb_doc(kb_id: str, doc_id: str) -> dict:
    import services.document_registry as registry
    from services.document_graph_service import DocumentGraphService

    return DocumentGraphService().build_graph(kb_id=kb_id, doc_ids=[doc_id], force=True)


def graph_totals(kb_id: str) -> dict:
    from services.document_graph_service import DocumentGraphService

    return DocumentGraphService().get_graph_totals(kb_id)


def entity_rows(service) -> list:
    result = service.driver.session().run(
        f"MATCH (e:Entity {{name: $name}}) RETURN e.kb_id AS kb, e.entity_key AS key, count(*) AS c",
        {"name": SHARED_ENTITY},
    )
    return [dict(record) for record in result]


def entity_constraint_present(service) -> tuple[bool, str]:
    rows = service.driver.session().run("SHOW CONSTRAINTS")
    details = []
    for record in rows:
        item = {key: str(record.get(key)) for key in record.keys()}
        blob = " ".join(item.values())
        if "entity" not in blob.lower():
            continue
        details.append(blob)
        if "entity_key" in blob and "kb_id" in blob and (
            "UNIQUE" in blob.upper() or "UNIQUENESS" in blob.upper()
        ):
            return True, blob
    return False, " | ".join(details)[:300]


# ---------------------------------------------------------------- 各断言段落


def section_graph_isolation(service) -> None:
    print("[graph] 双 KB 建图与同名实体隔离")
    with synthetic_extraction():
        result_a = build_kb_doc(KB_A, DOC_A)
        check("kb-a 建图成功", (result_a.get("documents") or 0) >= 1, f"(result={ {k: result_a.get(k) for k in ('documents', 'chunks', 'entities')} })")
        totals_a_before = graph_totals(KB_A)

        result_b = build_kb_doc(KB_B, DOC_B)
        check("kb-b 建图成功", (result_b.get("documents") or 0) >= 1, f"(result={ {k: result_b.get(k) for k in ('documents', 'chunks', 'entities')} })")

        totals_a_after = graph_totals(KB_A)
        check(
            "写入 kb-b 后 kb-a 图谱总量不变",
            totals_a_before == totals_a_after,
            f"(before={totals_a_before}, after={totals_a_after})",
        )

        rows = entity_rows(service)
        kbs = {str(row.get("kb")) for row in rows}
        keys = {str(row.get("key")) for row in rows}
        check(
            "同名实体在两个 KB 各有一个节点且 kb_id 不同",
            {KB_A, KB_B} <= kbs and len(keys) == 2,
            f"(rows={rows})",
        )

        ok, detail = entity_constraint_present(service)
        check("存在 (entity_key, kb_id) 复合唯一约束", ok, f"({detail})")

        # 作用域化清空 kb-a：图谱只删 kb-a
        from services.document_graph_service import DocumentGraphService

        cleared = DocumentGraphService().clear_document_graph(KB_A)
        totals_a_cleared = graph_totals(KB_A)
        totals_b_after_clear = graph_totals(KB_B)
        check(
            "clear kb-a 只删除 kb-a 节点/关系",
            all(v == 0 for v in totals_a_cleared.values()) and totals_b_after_clear == totals_a_after,
            f"(cleared={cleared}, kb_a={totals_a_cleared}, kb_b={totals_b_after_clear})",
        )


def section_vector_isolation(vector_store) -> None:
    print("[vector] Milvus 作用域过滤与清理")
    from services.scope_contract import milvus_kb_filter

    # 前面的图谱段已 clear kb-a（按 M3 设计会联动清理该库向量）。
    # 这里幂等重建两个 KB 的图谱与向量，再做向量段断言。
    from services.document_graph_service import DocumentGraphService

    with synthetic_extraction():
        DocumentGraphService().build_graph(kb_id=KB_A, doc_ids=[DOC_A], force=True)
        DocumentGraphService().build_graph(kb_id=KB_B, doc_ids=[DOC_B], force=True)

    for label, kb, doc in (("kb-a", KB_A, DOC_A), ("kb-b", KB_B, DOC_B)):
        hits = vector_store.search(fake_vector(SHARED_ENTITY), limit=10, filter_expr=milvus_kb_filter([kb]))
        check(
            f"{label} 过滤检索只返回本库 chunk",
            bool(hits) and all((h.metadata or {}).get("kb_id") == kb and (h.metadata or {}).get("doc_id") == doc for h in hits),
            f"(hits={[(h.chunk_id, (h.metadata or {}).get('kb_id'), (h.metadata or {}).get('doc_id')) for h in hits][:5]})",
        )

    from services.retrieval_orchestrator import retrieval_orchestrator

    retrieval_orchestrator.clear([KB_A])
    hits_a = vector_store.search(fake_vector(SHARED_ENTITY), limit=10, filter_expr=milvus_kb_filter([KB_A]))
    hits_b = vector_store.search(fake_vector(SHARED_ENTITY), limit=10, filter_expr=milvus_kb_filter([KB_B]))
    check(
        "按 kb 清空向量只影响 kb-a",
        not hits_a and bool(hits_b),
        f"(kb_a_hits={len(hits_a)}, kb_b_hits={len(hits_b)})",
    )
    retrieval_orchestrator.delete_doc(DOC_B, KB_B)
    hits_b_after = vector_store.search(fake_vector(SHARED_ENTITY), limit=10, filter_expr=milvus_kb_filter([KB_B]))
    check("delete_doc(kb-b) 后 kb-b 向量清空", not hits_b_after, f"(hits={len(hits_b_after)})")


def section_retrieval_guard(neo4j_available: bool) -> None:
    print("[retrieval] retrieve() 作用域强制点")
    from services.retrieval_orchestrator import retrieval_orchestrator

    # kb_ids 是必填关键字参数：缺参时 TypeError；显式空集合时 KB_SCOPE_REQUIRED
    omitted_failed = False
    try:
        retrieval_orchestrator.retrieve("品种A", 3)  # type: ignore[call-arg]
    except TypeError:
        omitted_failed = True
    except Exception:  # noqa: BLE001
        omitted_failed = False
    check("retrieve 缺 kb_ids 参数被签名拒绝", omitted_failed)
    expect_error("retrieve kb_ids=[] -> KB_SCOPE_REQUIRED", lambda: retrieval_orchestrator.retrieve("品种A", 3, kb_ids=[]), "KB_SCOPE_REQUIRED")
    expect_error("diagnose kb_ids=[] -> KB_SCOPE_REQUIRED", lambda: retrieval_orchestrator.diagnose("品种A", 3, modes=["keyword"], kb_ids=[]), "KB_SCOPE_REQUIRED")

    if not neo4j_available:
        skip("Neo4j 不可用，跳过作用域内检索正例")
        return
    result = retrieval_orchestrator.retrieve(SHARED_ENTITY, 5, kb_ids=[KB_A])
    items = result.get("items") or []
    # M4-R1：严格断言——后置过滤是 fail-closed，任何返回项必须携带精确的 kb_id/doc_id，
    # None 或空值都不允许（审计指出 `in (None, KB_A)` 是测试假阳性）。
    check(
        "kb-a 范围检索不返回 kb-b chunk（严格作用域断言）",
        items and all((item.get("kb_id") == KB_A) and (item.get("doc_id") == DOC_A) for item in items),
        f"(items={[(i.get('id'), i.get('kb_id'), i.get('doc_id')) for i in items][:5]})",
    )
    trace_scope = (result.get("trace") or {}).get("scope") or {}
    check("trace 记录请求作用域", trace_scope.get("kb_ids") == [KB_A], f"(scope={trace_scope})")


# ---------------------------------------------------------------- main


def main() -> int:
    print("=" * 60)
    print("GraphInsight dual-KB blackbox isolation check (live services)")
    print("=" * 60)

    pg_ok = probe_postgres()
    neo4j_service = probe_neo4j()
    vector_store = probe_milvus()

    missing = []
    if not pg_ok:
        missing.append("PostgreSQL")
    if neo4j_service is None:
        missing.append("Neo4j")
    if vector_store is None:
        missing.append("Milvus")

    # 完成标准要求三栈全部可用：缺任一即 NOT_RUN，不执行半程断言，不返回成功退出码。
    if missing:
        print("-" * 60)
        print(f"passed={len(PASS)} failed={len(FAIL)} skipped={len(SKIPS)}")
        print(f"NOT_RUN: 核心服务缺失 {missing}，双 KB 数据层黑盒需要 PG+Neo4j+Milvus 全部可用")
        print("NOT_RUN（未执行，非通过；退出码 2）")
        return 2

    try:
        with fake_embedding(), sandbox_storage() as docs_root:
            reset_registry_rows()
            write_source_files(docs_root)
            try:
                section_graph_isolation(neo4j_service)
                section_vector_isolation(vector_store)
                section_retrieval_guard(neo4j_available=True)
            finally:
                _cleanup(neo4j_service, vector_store)
    finally:
        drop_registry_rows()

    print("-" * 60)
    print(f"passed={len(PASS)} failed={len(FAIL)} skipped={len(SKIPS)}")
    if FAIL:
        for item in FAIL:
            print(f"  FAILED: {item}")
        return 1
    print("✓ dual-KB blackbox checks passed")
    return 0


def _cleanup(neo4j_service, vector_store) -> None:
    from services.document_graph_service import DocumentGraphService

    for kb in (KB_A, KB_B):
        try:
            DocumentGraphService().clear_document_graph(kb)
        except Exception as exc:  # noqa: BLE001
            print(f"  (cleanup) clear_document_graph({kb}) 失败：{exc}")
        if vector_store is not None:
            try:
                from services.retrieval_orchestrator import retrieval_orchestrator

                retrieval_orchestrator.clear([kb])
            except Exception as exc:  # noqa: BLE001
                print(f"  (cleanup) vector clear({kb}) 失败：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
