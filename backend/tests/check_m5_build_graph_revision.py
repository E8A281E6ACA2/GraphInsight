#!/usr/bin/env python3
"""M5 Wave 2 §16.1 S1 revision 生命周期 + VectorChunk content_revision 接线守卫。

口径（用户 2026-10-04 定案，"不新增迁移，复用现有 current/superseded 与部分唯一索引，
在事务内完成 CAS 生命周期"）：
  - build_graph 在处理"内容变化 / 首次建图"路径时，必须先经
    services.chunk_revision_lifecycle.write_revisions_for_build_graph 建立权威 revision；
  - 之后 retrieval_orchestrator.index_chunks 拿到 revision map 并把每个 chunk 的
    content_revision 传进 VectorChunk（P1#2 修复：§8.5 shadow 显式 INT64 字段不再缺源）；
  - 每 chunk 用 chunk_projection_state.update_projection_state 做 CAS 回写。

必须锁死的不变量：
  1. 新 chunk（无 current）→ 事务后 rev=1 current 落库，revisions 返回 1；
  2. 同内容重跑 → 幂等：rev 不变、`kept` 记录、不新增行、current 仍只有一条；
  3. 内容变化 → 老 current 转 superseded，新 rev=max+1 current；两者共存但只有一条 current；
  4. 事务内并发抢先（模拟：SELECT 与 UPDATE 之间把老 current 手动改走）→ RuntimeError，
     整体 rollback，新行不落库，老行保持原状态；
  5. 部分唯一索引 `uq_chunk_revisions_current` 数据库级拦截"两条 current"；
  6. UNIQUE (kb_id, chunk_id, content_revision) 数据库级拦截"重复 revision"；
  7. chunk 缺 chunk_id / doc_id / 空 text → 该 chunk 被跳过、不写库、进 skipped 列表；
  8. kb_id / doc_id 顶层缺失 → ValueError 早于任何 DB 交互；
  9. index_chunks 传 content_revisions → VectorChunk.content_revision 逐 chunk 匹配；
 10. index_chunks 不传 content_revisions（旧 caller）→ VectorChunk.content_revision=None，
     向后兼容不炸（本波不动那些旁路调用点，Wave 3/4 再收口）。
 11. 真实 build_graph（假 Neo4j 会话 + 桩注册表 + 记录用 index_chunks）端到端锁死：
     revision 行先于任何投影写入落库、Chunk MERGE 参数带 content_revision、
     content_revisions 传给 index_chunks、CAS 回写 indexed、§6.2 文档级聚合生效、
     force=False 未变更跳过且不再建 revision。

隔离（两层）：
  - 场景 1-10 用显式注入的临时 SQLite 引擎（复用 backend/admin/migrate_chunk_revisions
    ._SQLITE_DDL 作为唯一建表来源），假 Milvus client 用 RecordingClient 抓 upsert 输入；
  - 场景 11 走真实 build_graph，其内部 revision 写入不注入 engine，因此本文件在任何
    backend 模块导入之前把 ADMIN_DATABASE_URL 钉到另一份临时 SQLite
    （GRAPHINSIGHT_BACKEND_ENV_FILE；config/admin.database 用 load_dotenv(override=True)，
    只有这个入口能盖过 backend/.env），并在场景入口再次断言方言必须是 sqlite——
    否则会把 revision 行写进共享开发库（exit 9，绝不静默写入）。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 必须在任何 backend 模块导入之前落定：真实 build_graph 会经 admin.database.engine 写库。
_ISO_TMP = tempfile.mkdtemp(prefix="m5-wave2-iso-")
_ISO_DB = Path(_ISO_TMP) / "wave2_default.db"
_ISO_ENV = Path(_ISO_TMP) / "wave2_default.env"
_ISO_ENV.write_text(f"ADMIN_DATABASE_URL=sqlite:///{_ISO_DB.as_posix()}\n", encoding="utf-8")
os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(_ISO_ENV)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

KB = "kb-wave2"
OTHER_KB = "kb-other"
TENANT = "t-1"
PROJECT = "p-1"
DOC = "doc-1"


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, condition: bool, detail: str) -> None:
        if condition:
            self.passed += 1
            print(f"CHECK {name} PASS {detail}")
            return
        self.failed.append(name)
        print(f"CHECK {name} FAIL {detail}")


_ENGINES: list = []


@contextlib.contextmanager
def _temp_workspace():
    """临时目录 + 引擎统一 dispose。

    Windows 下 SQLite 文件句柄不显式释放，目录清理会抛 PermissionError（错误被
    cleanup 掩盖，真实断言结果反而看不到），所以引擎登记在这里统一关闭。
    """
    tmp = Path(tempfile.mkdtemp(prefix="m5-wave2-"))
    try:
        yield tmp
    finally:
        for registered in _ENGINES:
            registered.dispose()
        _ENGINES.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _release_isolation() -> None:
    """释放默认引擎句柄并删除隔离用临时库（Windows 下不 dispose 会占用文件）。"""
    default_module = sys.modules.get("admin.database")
    if default_module is not None:
        default_module.engine.dispose()
    shutil.rmtree(_ISO_TMP, ignore_errors=True)


def _fresh_engine(tmp_path: Path):
    """按 migrate_chunk_revisions._SQLITE_DDL 建一份独立的临时 SQLite 引擎。

    不共享 admin.database.engine，避免影响其它测试和真实配置。DDL 直接引用生产源，
    不复制一份，防止 schema 漂移。
    """
    from sqlalchemy import create_engine

    from admin.migrate_chunk_revisions import _SQLITE_DDL

    engine = create_engine(f"sqlite:///{tmp_path.as_posix()}", future=True)
    with engine.begin() as conn:
        for statement in _SQLITE_DDL:
            conn.exec_driver_sql(statement)
    _ENGINES.append(engine)
    return engine


def _seed_current(engine, *, kb=KB, chunk_id, doc_id=DOC, revision, content, content_hash, status="current"):
    """直插一条 chunk_revisions 行用于 pre-condition（走生产 DDL，字段与 lifecycle 一致）。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, revision_source, reason, trace_id) "
                "VALUES (:kb, :tenant, :proj, :doc, :chunk, :src, :srchash, :content, :hash, :rev, "
                ":status, 'pending', 'pending', 'system_initial', 'test_seed', 'trace-seed')"
            ),
            {
                "kb": kb,
                "tenant": TENANT,
                "proj": PROJECT,
                "doc": doc_id,
                "chunk": chunk_id,
                "src": content,
                "srchash": content_hash,
                "content": content,
                "hash": content_hash,
                "rev": revision,
                "status": status,
            },
        )


def _count_current(engine, chunk_id, *, kb=KB):
    from sqlalchemy import text

    return int(
        engine.connect()
        .execute(
            text(
                "SELECT COUNT(*) FROM chunk_revisions "
                "WHERE kb_id = :kb AND chunk_id = :chunk AND revision_status = 'current'"
            ),
            {"kb": kb, "chunk": chunk_id},
        )
        .scalar()
        or 0
    )


def _count_rows(engine, *, kb=KB):
    """本 KB 的 revision 行总数：rollback 场景用来证明"没有半截写入落库"。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM chunk_revisions WHERE kb_id = :kb"),
                {"kb": kb},
            ).scalar()
            or 0
        )


def _count_superseded(engine, chunk_id, *, kb=KB):
    from sqlalchemy import text

    return int(
        engine.connect()
        .execute(
            text(
                "SELECT COUNT(*) FROM chunk_revisions "
                "WHERE kb_id = :kb AND chunk_id = :chunk AND revision_status = 'superseded'"
            ),
            {"kb": kb, "chunk": chunk_id},
        )
        .scalar()
        or 0
    )


def _read_current_row(engine, chunk_id, *, kb=KB):
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT content_revision, content_hash, content FROM chunk_revisions "
                "WHERE kb_id = :kb AND chunk_id = :chunk AND revision_status = 'current'"
            ),
            {"kb": kb, "chunk": chunk_id},
        ).fetchone()
    if row is None:
        return None
    return {"content_revision": int(row[0]), "content_hash": str(row[1]), "content": str(row[2])}


def _chunk(chunk_id, text_value, doc_id=DOC):
    return {"chunk_id": chunk_id, "doc_id": doc_id, "text": text_value}


def _first_param(parameters):
    """before_cursor_execute 的 parameters 形态随 dialect paramstyle 变化：

    SQLite 用 qmark，SQLAlchemy 会把命名绑定展开成位置 tuple；PG 用 pyformat 才是 dict。
    这里两种都取第一个值，避免 `'tuple' object has no attribute 'get'`。
    """
    if isinstance(parameters, dict):
        return parameters.get("revision_id")
    if isinstance(parameters, (tuple, list)) and parameters:
        head = parameters[0]
        if isinstance(head, (tuple, list)):  # executemany
            return head[0] if head else None
        return head
    return None


def _scenario_real_build_graph(check: "Check", tmp: Path) -> None:
    """12) 真实 build_graph 的 Wave 2 接线取证（GI-8d）。

    `check_build_graph_shadow_retry.py` 整体替换了 build_graph，覆盖不到本轮接线，
    因此这里只替换外部依赖（Neo4j 会话、文档注册表、index_chunks 记录桩），
    revision 生命周期、CAS 回写与 §6.2 文档级聚合都走真实代码 + 真实（隔离）SQLite。
    """
    from sqlalchemy import text as sqltext
    from types import SimpleNamespace
    from unittest.mock import patch

    from admin.database import Base
    from admin.database import engine as default_engine
    from admin.migrate_chunk_revisions import _SQLITE_DDL
    from admin.models import KnowledgeBaseDocument
    import services.document_graph_service as dgs
    import services.document_registry as registry
    from services import retrieval_orchestrator as ro_module

    if default_engine.dialect.name != "sqlite":
        print(
            f"FATAL: admin.database.engine 方言是 {default_engine.dialect.name}，"
            "真实 build_graph 只允许写临时 sqlite"
        )
        raise SystemExit(9)

    kb_w2 = "kb-w2"
    doc_w2 = "doc-w2"

    with default_engine.begin() as conn:
        for statement in _SQLITE_DDL:
            conn.exec_driver_sql(statement)
    Base.metadata.create_all(bind=default_engine, tables=[KnowledgeBaseDocument.__table__])
    with default_engine.begin() as conn:
        conn.execute(sqltext("DELETE FROM chunk_revisions"))
        conn.execute(
            sqltext(
                "INSERT INTO knowledge_base_documents (doc_id, kb_id, tenant_id, project_id, name, "
                "relative_path, source_type, size, sha256, version, status, graph_status, vector_status) "
                "VALUES (:doc, :kb, :tenant, :proj, :name, :path, 'upload', 10, :sha, 1, 'indexed', "
                "'pending', 'pending')"
            ),
            {
                "doc": doc_w2,
                "kb": kb_w2,
                "tenant": TENANT,
                "proj": PROJECT,
                "name": "w2.txt",
                "path": f"{kb_w2}/{doc_w2}/w2.txt",
                "sha": "sha-seed",
            },
        )

    def rows() -> dict:
        with default_engine.connect() as conn:
            found = conn.execute(
                sqltext(
                    "SELECT chunk_id, content_revision, revision_status, graph_status, vector_status, "
                    "graph_content_revision, vector_content_revision, reason FROM chunk_revisions "
                    "WHERE kb_id = :kb AND doc_id = :doc ORDER BY chunk_id"
                ),
                {"kb": kb_w2, "doc": doc_w2},
            ).fetchall()
        return {
            str(row[0]): {
                "rev": row[1],
                "status": row[2],
                "graph": row[3],
                "vector": row[4],
                "graph_rev": row[5],
                "vector_rev": row[6],
                "reason": row[7],
            }
            for row in found
        }

    captured: list = []
    doc_store: dict = {}
    probes: dict = {}

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
            stmt = " ".join(str(cypher).split())
            p = dict(params or {})
            captured.append((stmt, p))
            key = (p.get("doc_id"), p.get("kb_id"))
            if "RETURN d.hash AS hash" in stmt:
                return _FakeResult([doc_store.get(key, {"hash": None, "parser_provider": None})])
            if "MERGE (d:Document" in stmt:
                probes.setdefault("rows_before_document_merge", len(rows()))
                doc_store[key] = {
                    "hash": p.get("hash"),
                    "parser_provider": p.get("parser_provider"),
                }
            if "MERGE (ch:Chunk" in stmt:
                probes.setdefault("rows_before_chunk_merge", len(rows()))
            return _FakeResult([{"c": 0}])

    class _FakeNeo4j:
        def ensure_connected(self):
            return None

        def session(self):
            return _FakeSession()

    vector_calls: list = []

    def _record_index_chunks(chunks, **kwargs):
        vector_calls.append({"chunk_ids": [str(c.get("chunk_id") or "") for c in chunks], **kwargs})
        return {"enabled": True, "indexed": len(chunks), "failures": []}

    root = tmp / "real-bg"
    source_file = root / "documents" / TENANT / PROJECT / kb_w2 / doc_w2 / "w2.txt"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "Alpha 属于 Beta。Alpha 在本轮建图中表现稳定，Beta 记录了完整的观测数据与对照结果，"
        "用于验证 revision 生命周期与投影写入的先后顺序。第二段文本继续补充观测细节。",
        encoding="utf-8",
    )
    fake_kb = SimpleNamespace(id=kb_w2, status="active", storage_prefix=f"{TENANT}/{PROJECT}/{kb_w2}")
    fake_doc = SimpleNamespace(
        doc_id=doc_w2,
        kb_id=kb_w2,
        tenant_id=TENANT,
        project_id=PROJECT,
        name="w2.txt",
        relative_path=f"{doc_w2}/w2.txt",
    )

    def _patches():
        return (
            patch.object(dgs.DocumentGraphService, "_neo4j", lambda self: _FakeNeo4j()),
            patch.object(dgs.DocumentGraphService, "_extract_entities", lambda self, *_a, **_k: ["Alpha"]),
            patch.object(dgs.DocumentGraphService, "_extract_relations", lambda self, *_a, **_k: []),
            patch.object(registry, "get_knowledge_base", lambda _kb_id: fake_kb),
            patch.object(registry, "get_documents", lambda _ids: {doc_w2: fake_doc}),
            patch.object(registry, "resolve_document_file_path", lambda _doc, _kb=None: source_file),
            patch.object(ro_module.retrieval_orchestrator, "index_chunks", _record_index_chunks),
        )

    old_parsed_path = dgs.settings.parsed_document_storage_path
    dgs.settings.parsed_document_storage_path = str(root / "parsed")
    try:
        with contextlib.ExitStack() as stack:
            for ctx in _patches():
                stack.enter_context(ctx)
            service = dgs.DocumentGraphService()
            result = service.build_graph(kb_id=kb_w2, doc_ids=[doc_w2], force=True)

        first = rows()
        chunk_ids = sorted(first.keys())
        check.ok(
            "real_build_graph_creates_rev1_current_rows",
            result.get("documents") == 1
            and chunk_ids
            and all(
                item["rev"] == 1
                and item["status"] == "current"
                and item["reason"] == "build_graph_m5_wave2"
                for item in first.values()
            ),
            f"docs={result.get('documents')} rows={first}",
        )
        check.ok(
            "revision_rows_exist_before_projection_write",
            probes.get("rows_before_document_merge", 0) >= len(chunk_ids)
            and probes.get("rows_before_chunk_merge", 0) >= len(chunk_ids),
            f"probes={probes} chunks={len(chunk_ids)}",
        )
        rev_in_chunk_merge = sorted(
            {
                item.get("content_revision")
                for stmt, params in captured
                if "MERGE (ch:Chunk" in stmt
                for item in params.get("chunks", [])
            }
        )
        check.ok(
            "chunk_merge_params_carry_content_revision",
            rev_in_chunk_merge == [1],
            f"got {rev_in_chunk_merge}",
        )
        expected_map = {chunk_id: 1 for chunk_id in vector_calls[0]["chunk_ids"]} if vector_calls else {}
        check.ok(
            "build_graph_passes_authoritative_revisions_to_index_chunks",
            len(vector_calls) == 1 and vector_calls[0].get("content_revisions") == expected_map and bool(expected_map),
            f"calls={vector_calls}",
        )
        after = rows()
        check.ok(
            "projection_state_cas_written_back_indexed",
            all(
                item["graph"] == "indexed"
                and item["graph_rev"] == 1
                and item["vector"] == "indexed"
                and item["vector_rev"] == 1
                for item in after.values()
            ),
            f"rows={after}",
        )
        check.ok(
            "document_states_aggregated_in_result",
            result.get("document_states")
            == [{"doc_id": doc_w2, "graph_status": "indexed", "vector_status": "indexed"}],
            f"got {result.get('document_states')}",
        )
        with default_engine.connect() as conn:
            doc_row = conn.execute(
                sqltext(
                    "SELECT graph_status, vector_status FROM knowledge_base_documents WHERE doc_id = :doc"
                ),
                {"doc": doc_w2},
            ).fetchone()
        check.ok(
            "knowledge_base_documents_updated_by_aggregation",
            tuple(doc_row or ()) == ("indexed", "indexed"),
            f"got {doc_row}",
        )

        captured.clear()
        vector_calls.clear()
        before_second = {chunk_id: item["rev"] for chunk_id, item in rows().items()}
        with contextlib.ExitStack() as stack:
            for ctx in _patches():
                stack.enter_context(ctx)
            result2 = service.build_graph(kb_id=kb_w2, doc_ids=[doc_w2], force=False)
        check.ok(
            "unchanged_doc_skipped_without_new_revision",
            result2.get("skipped_documents") == 1
            and result2.get("documents") == 0
            and {chunk_id: item["rev"] for chunk_id, item in rows().items()} == before_second
            and not vector_calls,
            f"skipped={result2.get('skipped_documents')} docs={result2.get('documents')} "
            f"rows={rows()} calls={len(vector_calls)}",
        )

        source_file.write_text(
            "Alpha 属于 Beta。内容已变更：本轮重解析会推进 content_revision 到 2，"
            "并把上一条 current 置为 superseded，验证真实建图路径下的 CAS 生命周期。"
            "补充一段以保证重新分块后文本确有差异。",
            encoding="utf-8",
        )
        captured.clear()
        vector_calls.clear()
        with contextlib.ExitStack() as stack:
            for ctx in _patches():
                stack.enter_context(ctx)
            result3 = service.build_graph(kb_id=kb_w2, doc_ids=[doc_w2], force=False)
        final = rows()
        bumped = {cid for cid, item in final.items() if item["status"] == "current" and item["rev"] == 2}
        third_map = vector_calls[0].get("content_revisions") if vector_calls else {}
        check.ok(
            "content_change_bumps_revision_in_real_build_graph",
            result3.get("documents") == 1
            and bool(bumped)
            and set(third_map or {}) == set(vector_calls[0]["chunk_ids"])
            and all(value == 2 for value in (third_map or {}).values()),
            f"docs={result3.get('documents')} bumped={sorted(bumped)} map={third_map} rows={final}",
        )
    finally:
        dgs.settings.parsed_document_storage_path = old_parsed_path


def main() -> int:
    from services.chunk_revision_lifecycle import write_revisions_for_build_graph

    check = Check()

    with _temp_workspace() as tmp:

        # 1) 首轮：无 current → rev=1 inserted
        engine = _fresh_engine(tmp / "rev1.db")
        result = write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[_chunk("c-1", "hello"), _chunk("c-2", "world")],
            engine=engine,
        )
        row1 = _read_current_row(engine, "c-1")
        row2 = _read_current_row(engine, "c-2")
        check.ok(
            "first_run_inserts_rev1",
            result["revisions"] == {"c-1": 1, "c-2": 1}
            and sorted(result["inserted"]) == ["c-1", "c-2"]
            and not result["kept"]
            and not result["superseded"]
            and row1 and row1["content_revision"] == 1 and row1["content"] == "hello",
            f"revisions={result['revisions']} row1={row1}",
        )

        # 2) 幂等重跑：同 content_hash → 保持 rev，不新增行
        import hashlib

        h1 = hashlib.sha256("hello".encode("utf-8")).hexdigest()
        h2 = hashlib.sha256("world".encode("utf-8")).hexdigest()
        result2 = write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[_chunk("c-1", "hello"), _chunk("c-2", "world")],
            engine=engine,
        )
        check.ok(
            "same_content_idempotent",
            result2["revisions"] == {"c-1": 1, "c-2": 1}
            and sorted(result2["kept"]) == ["c-1", "c-2"]
            and not result2["inserted"]
            and not result2["superseded"]
            and _count_current(engine, "c-1") == 1
            and _count_superseded(engine, "c-1") == 0,
            f"revisions={result2['revisions']} kept={sorted(result2['kept'])} curr_count={_count_current(engine, 'c-1')}",
        )
        check.ok(
            "sha256_hash_matches_backfill_convention",
            row1["content_hash"] == h1 and row2["content_hash"] == h2,
            "content_hash = sha256(text)（与 admin/backfill_chunk_revisions._sha256 同源）",
        )

        # 3) 内容变化 → old→superseded + new rev=2 current
        result3 = write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[_chunk("c-1", "HELLO changed")],
            engine=engine,
        )
        new_row = _read_current_row(engine, "c-1")
        check.ok(
            "content_change_bumps_revision",
            result3["revisions"] == {"c-1": 2}
            and result3["superseded"] == ["c-1"]
            and new_row and new_row["content_revision"] == 2 and new_row["content"] == "HELLO changed"
            and _count_current(engine, "c-1") == 1
            and _count_superseded(engine, "c-1") == 1,
            f"revisions={result3['revisions']} new_row={new_row} superseded_n={_count_superseded(engine, 'c-1')}",
        )

        # 4) 事务 rollback：模拟 lifecycle SELECT 之后、CAS UPDATE 之前 current 被并发移动。
        #    手段：before_cursor_execute 拦下 lifecycle 的 CAS UPDATE，在它真正执行前用同一
        #    DBAPI 连接上的侧 cursor 把目标行改成 superseded（同事务，随 lifecycle 一起回滚），
        #    使 CAS 谓词 revision_status='current' 命中 0 行 → RuntimeError → 整事务 rollback。
        engine_rollback = _fresh_engine(tmp / "rollback.db")
        _seed_current(
            engine_rollback,
            chunk_id="c-x",
            revision=5,
            content="old",
            content_hash="hash-old",
        )
        _seed_current(
            engine_rollback,
            chunk_id="c-y",
            revision=5,
            content="y-old",
            content_hash="hash-y",
        )

        from sqlalchemy import event

        interfered: list[str] = []

        def _concurrence_hook(conn, cursor, statement, parameters, context, executemany):
            if "UPDATE chunk_revisions SET revision_status = 'superseded'" not in statement:
                return
            revision_id = _first_param(parameters)
            if revision_id is None:
                return
            # 只对 c-y 的 CAS 出手（c-x 先 bump 成功，用来证明它随后被整体回滚）。
            # 走底层 sqlite3 cursor：与 lifecycle 同一连接=同一事务，能随外层 rollback 一起撤销，
            # 又不会重入 SQLAlchemy 的事件链（用 conn.execute 会再触发本 hook）。
            proxied = getattr(conn, "connection", None)
            raw = getattr(proxied, "dbapi_connection", proxied) if proxied is not None else conn
            side = raw.cursor()
            try:
                target = side.execute(
                    "SELECT chunk_id FROM chunk_revisions WHERE revision_id = ?",
                    (int(revision_id),),
                ).fetchone()
                if not target or str(target[0]) != "c-y":
                    return
                side.execute(
                    "UPDATE chunk_revisions SET revision_status = 'superseded' WHERE revision_id = ?",
                    (int(revision_id),),
                )
                interfered.append("c-y")
            finally:
                side.close()

        event.listen(engine_rollback, "before_cursor_execute", _concurrence_hook)

        raised = None
        try:
            write_revisions_for_build_graph(
                kb_id=KB,
                tenant_id=TENANT,
                project_id=PROJECT,
                doc_id=DOC,
                chunks=[_chunk("c-x", "x-new"), _chunk("c-y", "y-new")],
                engine=engine_rollback,
            )
        except RuntimeError as exc:
            raised = exc
        finally:
            event.remove(engine_rollback, "before_cursor_execute", _concurrence_hook)

        # 期望：lifecycle raise → 事务 rollback；c-x 的 bump 与 hook 的并发写都不落库
        #（表里仍是最初 seed 的两行，都还是 rev=5 current）。
        final_x = _read_current_row(engine_rollback, "c-x")
        final_y = _read_current_row(engine_rollback, "c-y")
        check.ok(
            "concurrent_move_rolls_back",
            raised is not None
            and "current" in str(raised)
            and interfered == ["c-y"]
            and final_x
            and final_x["content_revision"] == 5
            and final_x["content"] == "old"
            and final_y
            and final_y["content_revision"] == 5
            and final_y["content"] == "y-old"
            and _count_rows(engine_rollback) == 2
            and _count_superseded(engine_rollback, "c-x") == 0,
            f"raised={type(raised).__name__ if raised else None} interfered={interfered} "
            f"x={final_x} y={final_y} rows={_count_rows(engine_rollback)}",
        )

        # 5) 部分唯一索引 uq_chunk_revisions_current 数据库级拦截第二条 current
        engine_pu = _fresh_engine(tmp / "partial_unique.db")
        _seed_current(engine_pu, chunk_id="c-pu", revision=1, content="a", content_hash="h1")
        from sqlalchemy.exc import IntegrityError

        dup_raised = None
        try:
            _seed_current(engine_pu, chunk_id="c-pu", revision=2, content="b", content_hash="h2")
        except IntegrityError as exc:
            dup_raised = exc
        check.ok(
            "partial_unique_index_blocks_two_current",
            dup_raised is not None,
            f"second current rejected: {type(dup_raised).__name__ if dup_raised else None}",
        )

        # 6) UNIQUE (kb_id, chunk_id, content_revision) 拦截重复 revision
        engine_ur = _fresh_engine(tmp / "uq_rev.db")
        _seed_current(engine_ur, chunk_id="c-ur", revision=7, content="a", content_hash="h1", status="superseded")
        dup_raised2 = None
        try:
            _seed_current(engine_ur, chunk_id="c-ur", revision=7, content="b", content_hash="h2", status="current")
        except IntegrityError as exc:
            dup_raised2 = exc
        check.ok(
            "unique_revision_blocks_replay",
            dup_raised2 is not None,
            f"same revision rejected: {type(dup_raised2).__name__ if dup_raised2 else None}",
        )

        # 7) chunk 缺 chunk_id / doc_id / 空 text → 跳过、不写库
        engine_sk = _fresh_engine(tmp / "skip.db")
        result7 = write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[
                {"chunk_id": "", "doc_id": DOC, "text": "no-id"},
                {"chunk_id": "c-ok", "doc_id": "", "text": "no-doc"},
                {"chunk_id": "c-blank", "doc_id": DOC, "text": "   "},
                _chunk("c-good", "solid"),
            ],
            engine=engine_sk,
        )
        check.ok(
            "bad_chunks_skipped_only_good_written",
            result7["revisions"] == {"c-good": 1}
            and len(result7["skipped"]) == 3
            and _read_current_row(engine_sk, "c-ok") is None
            and _read_current_row(engine_sk, "c-blank") is None,
            f"revisions={result7['revisions']} skipped={result7['skipped']}",
        )

        # 8) kb_id 空 / doc_id 空 → 顶层 ValueError 早于任何 DB 交互
        engine_v = _fresh_engine(tmp / "validate.db")
        raised_8a = None
        try:
            write_revisions_for_build_graph(
                kb_id="",
                tenant_id=TENANT,
                project_id=PROJECT,
                doc_id=DOC,
                chunks=[_chunk("c-v", "x")],
                engine=engine_v,
            )
        except ValueError as exc:
            raised_8a = exc
        raised_8b = None
        try:
            write_revisions_for_build_graph(
                kb_id=KB,
                tenant_id=TENANT,
                project_id=PROJECT,
                doc_id="",
                chunks=[_chunk("c-v", "x")],
                engine=engine_v,
            )
        except ValueError as exc:
            raised_8b = exc
        check.ok(
            "top_level_scope_required",
            raised_8a is not None and raised_8b is not None,
            f"kb empty→{type(raised_8a).__name__ if raised_8a else None} doc empty→{type(raised_8b).__name__ if raised_8b else None}",
        )

        # 9) 跨 KB 不串写：同 chunk_id 在另一 KB 建新 current 不影响本 KB
        engine_cross = _fresh_engine(tmp / "cross.db")
        _seed_current(engine_cross, kb=OTHER_KB, chunk_id="c-shared", revision=9, content="other", content_hash="h-other")
        result9 = write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[_chunk("c-shared", "mine")],
            engine=engine_cross,
        )
        mine = _read_current_row(engine_cross, "c-shared", kb=KB)
        theirs = _read_current_row(engine_cross, "c-shared", kb=OTHER_KB)
        check.ok(
            "kb_isolation",
            result9["revisions"] == {"c-shared": 1}
            and mine and mine["content_revision"] == 1
            and theirs and theirs["content_revision"] == 9,
            f"mine={mine} theirs_rev={theirs['content_revision'] if theirs else None}",
        )

        # 10) update_projection_state 的 CAS：Wave 2 lifecycle 建 rev=1 之后回写 indexed，
        #     用不同 expected_revision 应 rowcount=0（并发保护）。
        from services.chunk_projection_state import update_projection_state

        engine_cas = _fresh_engine(tmp / "cas.db")
        # 借用 lifecycle 建 current rev=1
        write_revisions_for_build_graph(
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            doc_id=DOC,
            chunks=[_chunk("c-cas", "v1")],
            engine=engine_cas,
        )

        # lifecycle 的写走独立 engine 参数，但 update_projection_state 走 admin.database.engine，
        # 为了测 CAS 我们直接手写一次等价 SQL 观察 rowcount 语义，不改动 update_projection_state
        # 源码（该函数在 check_b0_reindex_chunks 里已被反复验证）。这里只断言 lifecycle 建的 current
        # 行有 revision=1，供 caller 用作 expected_revision 参数（回归到 lifecycle 契约）。
        from sqlalchemy import text as sqltext

        with engine_cas.begin() as conn:
            hit = conn.execute(
                sqltext(
                    "UPDATE chunk_revisions SET graph_status = 'indexed', graph_content_revision = 1 "
                    "WHERE kb_id = :kb AND chunk_id = :chunk AND content_revision = 1 "
                    "AND revision_status = 'current'"
                ),
                {"kb": KB, "chunk": "c-cas"},
            )
            rowcount_1 = int(hit.rowcount or 0)
        with engine_cas.begin() as conn:
            miss = conn.execute(
                sqltext(
                    "UPDATE chunk_revisions SET graph_status = 'indexed', graph_content_revision = 2 "
                    "WHERE kb_id = :kb AND chunk_id = :chunk AND content_revision = 2 "
                    "AND revision_status = 'current'"
                ),
                {"kb": KB, "chunk": "c-cas"},
            )
            rowcount_2 = int(miss.rowcount or 0)
        check.ok(
            "cas_semantics_on_lifecycle_row",
            rowcount_1 == 1 and rowcount_2 == 0,
            f"expected=1 → rowcount={rowcount_1}; expected=2 (moved) → rowcount={rowcount_2}",
        )
        # 静默引用，避免 linter 把 unused import 判成噪声
        _ = update_projection_state

    # 11) retrieval_orchestrator.index_chunks 把 content_revisions 逐 chunk 传进 VectorChunk
    from services.vector_store import VectorChunk  # noqa: F401  (typing convenience)

    class RecordingClient:
        def __init__(self):
            self.upserts: dict[str, list] = {}

        def has_collection(self, name):
            return True

        def upsert(self, *, collection_name, data):
            self.upserts.setdefault(collection_name, []).append(data)
            return {"upsert_count": len(data)}

        def delete(self, *, collection_name, filter):  # noqa: A002
            self.upserts.setdefault(collection_name, []).append(filter)
            return {"delete_count": 1}

    from services import vector_store as vs_module
    from services import retrieval_orchestrator as ro_module
    from services import embedding_service as es_module

    PRIMARY = "graphinsight_chunks_v2"

    # 打桩：embedding 不联网，vector_store 假 config / 假 client
    orig_vs_cfg = vs_module.vector_store.config
    orig_vs_enabled = vs_module.vector_store.is_enabled
    orig_vs_get_client = vs_module.vector_store._get_client
    orig_vs_ensure = vs_module.vector_store.ensure_collection
    orig_vs_revision = vs_module.vector_store._revision_field
    orig_embed_cfg = es_module.embedding_service.config
    orig_embed_enabled = es_module.embedding_service.is_enabled
    orig_embed_texts = es_module.embedding_service.embed_texts
    orig_embed_hash = es_module.embedding_service.content_hash

    try:
        vs_module.vector_store.config = lambda: {
            "enabled": True,
            "provider": "milvus",
            "collection": PRIMARY,
            "dual_write": False,
            "shadow_collection": "",
        }
        vs_module.vector_store.is_enabled = lambda: True
        recording = RecordingClient()
        vs_module.vector_store._get_client = lambda: recording
        vs_module.vector_store.ensure_collection = lambda *a, **kw: None
        vs_module.vector_store._revision_field = {PRIMARY: True}

        es_module.embedding_service.config = lambda: {"model": "stub-embed", "dimension": 2, "batch_size": 32}
        es_module.embedding_service.is_enabled = lambda: True
        es_module.embedding_service.embed_texts = lambda texts: [[0.1, 0.2] for _ in texts]
        es_module.embedding_service.content_hash = lambda t: f"hash::{t}"

        chunks_payload = [
            {"chunk_id": "c-a", "doc_id": DOC, "text": "alpha"},
            {"chunk_id": "c-b", "doc_id": DOC, "text": "beta"},
        ]

        # 11a) 传 content_revisions → VectorChunk.content_revision 逐 chunk 匹配
        result_a = ro_module.retrieval_orchestrator.index_chunks(
            chunks_payload,
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
            content_revisions={"c-a": 3, "c-b": 4},
        )
        rows_a = recording.upserts.get(PRIMARY, [[]])[0]
        rev_by_chunk = {row["chunk_id"]: row.get("content_revision") for row in rows_a}
        check.ok(
            "index_chunks_passes_content_revisions",
            result_a.get("indexed") == 2 and rev_by_chunk == {"c-a": 3, "c-b": 4},
            f"indexed={result_a.get('indexed')} revs={rev_by_chunk}",
        )

        # 11b) 不传 content_revisions（旧 caller 兼容）→ VectorChunk.content_revision=None
        recording.upserts.clear()
        result_b = ro_module.retrieval_orchestrator.index_chunks(
            chunks_payload,
            kb_id=KB,
            tenant_id=TENANT,
            project_id=PROJECT,
        )
        rows_b = recording.upserts.get(PRIMARY, [[]])[0]
        rev_by_chunk_b = {row["chunk_id"]: row.get("content_revision") for row in rows_b}
        check.ok(
            "index_chunks_without_revisions_stays_none",
            result_b.get("indexed") == 2
            and rev_by_chunk_b == {"c-a": None, "c-b": None},
            f"revs={rev_by_chunk_b}",
        )
    finally:
        vs_module.vector_store.config = orig_vs_cfg
        vs_module.vector_store.is_enabled = orig_vs_enabled
        vs_module.vector_store._get_client = orig_vs_get_client
        vs_module.vector_store.ensure_collection = orig_vs_ensure
        vs_module.vector_store._revision_field = orig_vs_revision
        es_module.embedding_service.config = orig_embed_cfg
        es_module.embedding_service.is_enabled = orig_embed_enabled
        es_module.embedding_service.embed_texts = orig_embed_texts
        es_module.embedding_service.content_hash = orig_embed_hash

    # 12) 真实 build_graph 接线取证（revision 先于投影写入 + CAS 回写 + §6.2 聚合）
    bg_tmp = Path(tempfile.mkdtemp(prefix="m5-wave2-bg-"))
    try:
        _scenario_real_build_graph(check, bg_tmp)
    finally:
        shutil.rmtree(bg_tmp, ignore_errors=True)
        _release_isolation()

    print(
        "M5_BUILD_GRAPH_REVISION_SUMMARY "
        f"passed={check.passed} failed={len(check.failed)}"
    )
    return 0 if not check.failed else 1


if __name__ == "__main__":
    sys.exit(main())
