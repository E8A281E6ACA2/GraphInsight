"""
知识库作用域隔离测试（M3 验收，纯单元；不依赖 Neo4j / Milvus / PostgreSQL / fastapi）

覆盖范围（docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2 / §4 M3）：
[a] build_graph 空/缺 doc_ids 或缺 kb_id -> KB_SCOPE_REQUIRED，且不触发任何注册表/Neo4j 访问
[b] clear_document_graph / delete_document_graph 缺 kb_id -> KB_SCOPE_REQUIRED，不触 DB
[c] job_runtime.execute_job payload 缺 kb_id/tenant_id/project_id -> KB_SCOPE_REQUIRED，先于任何工作
[d] entity_key 跨 kb 隔离与同 kb 稳定性
[e] Milvus 空 filter / 缺 kb 的 search/delete/clear 一律拒绝
[f] 静态 Cypher 断言：document_graph_service 中每条含 MERGE/DELETE 的语句都带 kb_id；
    Entity 约束为 (entity_key, kb_id) 复合唯一，旧全局 name 唯一约束已移除
[g] 注册表解析 + 跨库跳过（fakes + 捕获 Cypher）：doc 注册表 kb 与 payload kb 不一致 → 跳过并报告，
    所有捕获语句参数 kb_id == 目标 kb；解析产物落在 parsed/{kb_id}/{doc_id}/
[h] 注册表路径安全：storage_prefix / relative_path 逃逸拒绝

运行：python backend/tests/check_kb_scope_isolation.py
"""
from __future__ import annotations

import ast
import json
import re
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

import services.document_graph_service as dgs  # noqa: E402
import services.document_registry as registry  # noqa: E402
import services.job_runtime as job_runtime  # noqa: E402
from core.exceptions import AppException, ErrorCode, KnowledgeScopeError  # noqa: E402
from services.scope_contract import entity_key, milvus_kb_filter  # noqa: E402
from services.vector_store import require_scope_filter, vector_store  # noqa: E402

PASS: list = []
FAIL: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  ✗ {name} {detail}")


def expect_error(name: str, fn, code: str, error_cls: type = KnowledgeScopeError) -> None:
    try:
        fn()
    except error_cls as exc:
        check(name, exc.error_code == code, f"(got {exc.error_code})")
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"(unexpected {type(exc).__name__}: {exc})")
    else:
        check(name, False, "(no error raised)")


def _forbidden(name: str):
    def _raise(*_args, **_kwargs):
        raise AssertionError(f"{name} 不应被调用")

    return _raise


# ---------------------------------------------------------------- [a] build_graph 作用域强制点


def test_build_graph_requires_scope() -> None:
    print("[a] build_graph 缺 kb_id / 缺 doc_ids -> KB_SCOPE_REQUIRED 且不触 DB")
    with patch.object(dgs.DocumentGraphService, "_neo4j", _forbidden("Neo4j")), patch.object(
        registry, "get_knowledge_base", _forbidden("registry.get_knowledge_base")
    ), patch.object(registry, "get_documents", _forbidden("registry.get_documents")):
        service = dgs.DocumentGraphService()
        expect_error("doc_ids 为空列表", lambda: service.build_graph(kb_id="kb-a", doc_ids=[]), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("doc_ids 缺省", lambda: service.build_graph(kb_id="kb-a"), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("kb_id 缺失", lambda: service.build_graph(kb_id="", doc_ids=["doc-1"]), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("kb_id 为 None", lambda: service.build_graph(kb_id=None, doc_ids=["doc-1"]), ErrorCode.KB_SCOPE_REQUIRED)


# ---------------------------------------------------------------- [b] delete/clear 作用域强制点


def test_delete_clear_require_kb() -> None:
    print("[b] delete/clear 缺 kb_id -> KB_SCOPE_REQUIRED，任何 DB 访问之前拒绝")
    with patch.object(dgs.DocumentGraphService, "_neo4j", _forbidden("Neo4j")):
        service = dgs.DocumentGraphService()
        expect_error("delete_document_graph 缺 kb", lambda: service.delete_document_graph("doc-1", ""), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("delete_document_graph kb=None", lambda: service.delete_document_graph("doc-1", None), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("delete_document_graph 缺 doc", lambda: service.delete_document_graph("", "kb-a"), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("clear_document_graph 缺 kb", lambda: service.clear_document_graph(""), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("clear_document_graph kb=None", lambda: service.clear_document_graph(None), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("preview_clear 缺 kb", lambda: service.preview_clear_document_graph(None), ErrorCode.KB_SCOPE_REQUIRED)
        expect_error("get_graph_totals 缺 kb", lambda: service.get_graph_totals(""), ErrorCode.KB_SCOPE_REQUIRED)


# ---------------------------------------------------------------- [c] job payload 作用域强制点


class _ForbiddenGraphService:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("payload 缺作用域时不应构造 DocumentGraphService")


def test_job_payload_scope_required() -> None:
    print("[c] execute_job payload 缺 kb_id/tenant_id/project_id -> KB_SCOPE_REQUIRED，先于任何工作")
    with patch.object(job_runtime, "DocumentGraphService", _ForbiddenGraphService):
        expect_error(
            "payload 为空",
            lambda: job_runtime.execute_job(job_id=1, job_type="build_graph", payload={}),
            ErrorCode.KB_SCOPE_REQUIRED,
            error_cls=AppException,
        )
        expect_error(
            "缺 kb_id",
            lambda: job_runtime.execute_job(
                job_id=1,
                job_type="build_graph",
                payload={"tenant_id": "tenant-a", "project_id": "project-a", "doc_ids": ["doc-1"]},
            ),
            ErrorCode.KB_SCOPE_REQUIRED,
            error_cls=AppException,
        )
        expect_error(
            "缺 tenant_id",
            lambda: job_runtime.execute_job(
                job_id=1,
                job_type="clear_kb",
                payload={"kb_id": "kb-a", "project_id": "project-a"},
            ),
            ErrorCode.KB_SCOPE_REQUIRED,
            error_cls=AppException,
        )
        expect_error(
            "缺 project_id",
            lambda: job_runtime.execute_job(
                job_id=1,
                job_type="clear_kb",
                payload={"kb_id": "kb-a", "tenant_id": "tenant-a"},
            ),
            ErrorCode.KB_SCOPE_REQUIRED,
            error_cls=AppException,
        )
        scope = job_runtime.require_payload_scope(
            {"kb_id": "KB-A", "tenant_id": "Tenant-A", "project_id": "Project-A"}
        )
        check("作用域归一化输出", scope == {"kb_id": "kb-a", "tenant_id": "tenant-a", "project_id": "project-a"}, f"(got {scope})")


# ---------------------------------------------------------------- [d] entity_key 隔离


def test_entity_key_isolation() -> None:
    print("[d] 同名实体跨 kb 不合并 / 同 kb 稳定")
    key_a = entity_key("kb-a", "品种A", "品种")
    key_b = entity_key("kb-b", "品种A", "品种")
    check("不同 kb 同名实体 key 不同", key_a != key_b)
    check("同 kb 同名实体 key 稳定", entity_key("kb-a", "品种A", "品种") == key_a)
    check("大小写归一", entity_key("kb-a", "品种a", "品种") == key_a)
    check("默认实体类型键与显式类型一致口径", entity_key("kb-a", "Alpha", dgs.DEFAULT_ENTITY_TYPE) == entity_key("kb-a", "alpha", "entity"))


# ---------------------------------------------------------------- [e] Milvus 作用域 guard


def test_vector_scope_guard() -> None:
    print("[e] Milvus 空 filter / 缺 kb 一律拒绝")
    expect_error("require_scope_filter 空串拒绝", lambda: require_scope_filter(""), ErrorCode.KB_SCOPE_REQUIRED)
    expect_error("require_scope_filter None 拒绝", lambda: require_scope_filter(None), ErrorCode.KB_SCOPE_REQUIRED)
    check("非空 filter 原样返回", require_scope_filter('kb_id in ["kb-a"]') == 'kb_id in ["kb-a"]')
    expect_error("delete_doc 缺 kb 拒绝", lambda: vector_store.delete_doc("doc-1", ""), ErrorCode.KB_SCOPE_REQUIRED)
    expect_error("clear 空 kb_ids 拒绝", lambda: vector_store.clear([]), ErrorCode.KB_SCOPE_REQUIRED)
    check(
        "milvus_kb_filter 形状",
        milvus_kb_filter(["kb-a", "kb-b"]) == 'kb_id in ["kb-a", "kb-b"]',
        f"(got {milvus_kb_filter(['kb-a', 'kb-b'])})",
    )


# ---------------------------------------------------------------- [f] 静态 Cypher 断言


def test_static_cypher_scope() -> None:
    print("[f] 静态断言：所有 MERGE/DELETE 语句带 kb_id；Entity 复合唯一约束")
    source = (backend_dir / "services" / "document_graph_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    has_composite_constraint = False
    has_legacy_name_unique = False
    has_drop_legacy = False
    merge_or_delete_count = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = " ".join(node.value.split())
        if "REQUIRE e.name IS UNIQUE" in text:
            has_legacy_name_unique = True
        if "entity_scope_key" in text and "e.entity_key" in text and "e.kb_id" in text and "IS UNIQUE" in text:
            has_composite_constraint = True
        if "DROP CONSTRAINT entity_name" in text:
            has_drop_legacy = True
        if "MERGE " in text or re.search(r"\bDELETE\b", text):
            merge_or_delete_count += 1
            if "kb_id" not in text:
                offenders.append(text[:120])
    check(
        f"共检查 {merge_or_delete_count} 条 MERGE/DELETE 语句全部包含 kb_id",
        merge_or_delete_count > 0 and not offenders,
        f"(offenders={offenders})",
    )
    check("Entity (entity_key, kb_id) 复合唯一约束存在", has_composite_constraint)
    check("旧全局 name 唯一约束已移除", has_drop_legacy and not has_legacy_name_unique)

    vector_source = (backend_dir / "services" / "vector_store.py").read_text(encoding="utf-8")
    for field in ("kb_id", "tenant_id", "project_id"):
        check(f"Milvus schema 包含 {field} 字段", f'"{field}"' in vector_source)
    check("默认 collection 切换为 kb 隔离版本", "graphinsight_chunks_v2" in vector_source)


# ---------------------------------------------------------------- [g] 注册表解析 + 跨库跳过（捕获 Cypher）


class _FakeResult:
    def __init__(self, records):
        self._records = records

    def single(self):
        return self._records[0] if self._records else None


class _FakeSession:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, cypher, params=None):
        text = " ".join(str(cypher).split())
        self._log.append((text, dict(params or {})))
        if "RETURN d.hash" in text:
            return _FakeResult([{"hash": None, "parser_provider": None}])
        return _FakeResult([{"c": 0}])


class _FakeNeo4j:
    def __init__(self, log):
        self._log = log

    def ensure_connected(self):
        return None

    def session(self):
        return _FakeSession(self._log)


def test_registry_scoped_build_and_cross_scope_skip() -> None:
    print("[g] 注册表解析：只处理 payload doc_ids；跨库 doc 跳过并报告；Cypher 参数锁定 kb")
    captured: list = []
    vector_calls: list = []

    # 测试环境无 neo4j/openai：向 retrieval_orchestrator 注入捕获桩（真实环境走真实模块）
    stub_module = types.ModuleType("services.retrieval_orchestrator")

    def _fake_index_chunks(chunks, **kwargs):
        vector_calls.append({"chunks": len(chunks), **kwargs})
        return {"enabled": True, "indexed": len(chunks), "failures": []}

    stub_module.retrieval_orchestrator = SimpleNamespace(index_chunks=_fake_index_chunks)
    stub_injected = "services.retrieval_orchestrator" not in sys.modules
    sys.modules["services.retrieval_orchestrator"] = stub_module

    fake_kb = SimpleNamespace(id="kb-a", status="active", storage_prefix="tenant-a/project-a/kb-a")
    doc_a = SimpleNamespace(
        doc_id="doc-a",
        kb_id="kb-a",
        tenant_id="tenant-a",
        project_id="project-a",
        name="a.txt",
        relative_path="doc-a/versions/v1/source/a.txt",
    )
    doc_b = SimpleNamespace(
        doc_id="doc-b",
        kb_id="kb-b",
        tenant_id="tenant-a",
        project_id="project-a",
        name="b.txt",
        relative_path="doc-b/versions/v1/source/b.txt",
    )

    def fake_entities(_self, *_args, **_kwargs):
        return ["Alpha"]

    def fake_relations(_self, *_args, **_kwargs):
        return [
            {
                "source": "Alpha",
                "target": "Beta",
                "label": "属于",
                "rel_type": "BELONGS",
                "relation_type": "属于",
                "confidence": 0.9,
                "evidence": "Alpha 属于 Beta",
            }
        ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        docs_root = tmp_root / "documents"
        parsed_root = tmp_root / "parsed"
        file_a = docs_root / "tenant-a" / "project-a" / "kb-a" / "doc-a" / "a.txt"
        file_b = docs_root / "tenant-a" / "project-a" / "kb-b" / "doc-b" / "b.txt"
        file_a.parent.mkdir(parents=True)
        file_b.parent.mkdir(parents=True)
        file_a.write_text(
            "Alpha 属于 Beta。Alpha 在试验中表现稳定，Beta 记录了完整的观测数据与对照结果，用于验证同名实体隔离。",
            encoding="utf-8",
        )
        file_b.write_text("Beta 属于 Gamma。Gamma 在试验中表现良好，数据完整。", encoding="utf-8")

        old_parsed_path = dgs.settings.parsed_document_storage_path
        dgs.settings.parsed_document_storage_path = str(parsed_root)
        try:
            with patch.object(dgs.DocumentGraphService, "_neo4j", lambda self: _FakeNeo4j(captured)), patch.object(
                dgs.DocumentGraphService, "_extract_entities", fake_entities
            ), patch.object(
                dgs.DocumentGraphService, "_extract_relations", fake_relations
            ), patch.object(
                registry, "get_knowledge_base", lambda kb_id: fake_kb
            ), patch.object(
                registry, "get_documents", lambda ids: {"doc-a": doc_a, "doc-b": doc_b}
            ), patch.object(
                registry,
                "resolve_document_file_path",
                lambda doc, kb=None: file_a if doc.doc_id == "doc-a" else file_b,
            ):
                service = dgs.DocumentGraphService()
                result = service.build_graph(kb_id="kb-a", doc_ids=["doc-a", "doc-b"], force=True)
        finally:
            dgs.settings.parsed_document_storage_path = old_parsed_path
            if stub_injected:
                sys.modules.pop("services.retrieval_orchestrator", None)

        check("只处理属于目标 kb 的 doc", result.get("documents") == 1, f"(got {result.get('documents')})")
        check(
            "跨库 doc 被跳过并报告",
            result.get("skipped_cross_scope") == [{"doc_id": "doc-b", "kb_id": "kb-b"}],
            f"(got {result.get('skipped_cross_scope')})",
        )
        check("结果作用域标记 kb_scoped", result.get("scope") == "kb_scoped" and result.get("kb_id") == "kb-a")

        merge_document = [(c, p) for c, p in captured if "MERGE (d:Document" in c]
        check("Document MERGE 被执行", len(merge_document) == 1, f"(got {len(merge_document)})")
        if merge_document:
            _cypher, params = merge_document[0]
            check(
                "Document MERGE 键含 doc_id + kb_id 且 SET 三元组",
                params.get("kb_id") == "kb-a"
                and params.get("tenant_id") == "tenant-a"
                and params.get("project_id") == "project-a"
                and params.get("doc_id") == "doc-a",
                f"(got {params})",
            )
            check("MERGE 模板包含 kb_id 占位", "kb_id: $kb_id" in merge_document[0][0])

        all_kb_params = [p["kb_id"] for _c, p in captured if "kb_id" in p]
        check(
            f"所有 {len(all_kb_params)} 条语句的 kb_id 参数均为目标 kb",
            all_kb_params and set(all_kb_params) == {"kb-a"},
            f"(got {sorted(set(all_kb_params))})",
        )

        batch = [(c, p) for c, p in captured if "MERGE (ch:Chunk" in c and "entity_key: ent.entity_key" in c]
        check("Chunk/Entity 批量写入存在", len(batch) >= 1, f"(got {len(batch)})")
        expected_key = entity_key("kb-a", "Alpha", dgs.DEFAULT_ENTITY_TYPE)
        other_key = entity_key("kb-b", "Alpha", dgs.DEFAULT_ENTITY_TYPE)
        entity_nodes = [item for _p, params in batch for chunk in params.get("chunks", []) for item in chunk.get("entity_nodes", [])]
        check(
            "Entity MERGE 携带 kb-a 的 entity_key",
            any(item.get("entity_key") == expected_key for item in entity_nodes),
            f"(got {entity_nodes})",
        )
        check("entity_key 与 kb-b 的同名实体不同", expected_key != other_key)

        manifest_path = parsed_root / "kb-a" / "doc-a" / "manifest.json"
        check("解析产物落在 parsed/{kb_id}/{doc_id}/", manifest_path.exists(), f"(missing {manifest_path})")
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            check("manifest 记录 kb_id", manifest.get("kb_id") == "kb-a", f"(got {manifest.get('kb_id')})")
        check("未为跨库 doc 产出 kb-b 产物目录", not (parsed_root / "kb-b").exists())
        check(
            "向量写入按 doc 作用域传播（kb_id/tenant/project）",
            bool(vector_calls)
            and all(
                call.get("kb_id") == "kb-a"
                and call.get("tenant_id") == "tenant-a"
                and call.get("project_id") == "project-a"
                for call in vector_calls
            ),
            f"(got {vector_calls})",
        )


# ---------------------------------------------------------------- [h] 注册表路径安全


def test_registry_path_safety() -> None:
    print("[h] storage_prefix / relative_path 路径逃逸拒绝")
    expect_error(
        "relative_path .. 逃逸",
        lambda: registry.assert_safe_relative("../escape.txt", "relative_path"),
        ErrorCode.KB_STORAGE_PATH_INVALID,
    )
    expect_error(
        "relative_path 绝对路径",
        lambda: registry.assert_safe_relative("/etc/passwd", "relative_path"),
        ErrorCode.KB_STORAGE_PATH_INVALID,
    )
    expect_error(
        "relative_path Windows 盘符",
        lambda: registry.assert_safe_relative("C:/data/x.txt", "relative_path"),
        ErrorCode.KB_STORAGE_PATH_INVALID,
    )
    expect_error(
        "relative_path 反斜杠",
        lambda: registry.assert_safe_relative("a\\b.txt", "relative_path"),
        ErrorCode.KB_STORAGE_PATH_INVALID,
    )
    ok = registry.assert_safe_relative("tenant-a/project-a/kb-001", "storage_prefix")
    check("合法多段 prefix 通过", ok == "tenant-a/project-a/kb-001")

    kb = SimpleNamespace(id="kb-001", storage_prefix="tenant-a/project-a/kb-001")
    doc = SimpleNamespace(kb_id="kb-001", relative_path="doc-1/versions/v1/source/a.txt")
    resolved = registry.resolve_document_file_path(doc, kb)
    parts = resolved.parts
    check(
        "物理路径 = document_storage_path / storage_prefix / relative_path",
        parts[-8:] == ("tenant-a", "project-a", "kb-001", "doc-1", "versions", "v1", "source", "a.txt"),
        f"(got {resolved})",
    )
    escape_doc = SimpleNamespace(kb_id="kb-001", relative_path="../../other/x.txt")
    expect_error(
        "逃逸 doc 路径拒绝",
        lambda: registry.resolve_document_file_path(escape_doc, kb),
        ErrorCode.KB_STORAGE_PATH_INVALID,
    )


def main() -> int:
    print("=" * 60)
    print("GraphInsight KB scope isolation tests (M3, pure-unit)")
    print("=" * 60)
    test_build_graph_requires_scope()
    test_delete_clear_require_kb()
    test_job_payload_scope_required()
    test_entity_key_isolation()
    test_vector_scope_guard()
    test_static_cypher_scope()
    test_registry_scoped_build_and_cross_scope_skip()
    test_registry_path_safety()
    print("-" * 60)
    print(f"passed={len(PASS)} failed={len(FAIL)}")
    if FAIL:
        for item in FAIL:
            print(f"  FAILED: {item}")
        return 1
    print("✓ all KB scope isolation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
