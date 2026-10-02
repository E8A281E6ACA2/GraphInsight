"""Python job execution runtime（M3：任务 payload 冻结作用域）。

任务 envelope（手册 §13.2）：知识数据类任务（build_graph / clear_kb）的 payload
必须包含 tenant_id / project_id / kb_id + 显式 doc_ids；worker 只处理 payload
中列出的 doc，不扫描目录，不做全局清理。

reindex 例外：它只重建 Neo4j 全文索引，属于基础设施操作而非知识数据操作，
不携带知识数据作用域（缺失 kb_id 不报错）。

reindex_chunks（M5-B0）：chunk 投影重建，属知识数据操作，同样要求 payload 冻结
kb_id/tenant_id/project_id + 显式 targets（设计 §8.2/§15.2）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from config import get_settings
from core import ValidationException, get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.document_graph_service import DocumentGraphService
from services.scope_contract import normalize_scope_id
from services.runtime_config import get_graph_build_runtime_defaults


logger = get_logger()
settings = get_settings()

SCOPE_REQUIRED_FIELDS = ("kb_id", "tenant_id", "project_id")


def _normalize_reasoning_profile(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"fast", "balanced", "deep"}:
        return normalized
    return fallback


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _safe_index_name(name: str) -> str:
    normalized = "".join(ch for ch in str(name or "").strip() if ch.isalnum() or ch == "_")
    if not normalized:
        return "chunkText"
    return normalized[:64]


def require_payload_scope(payload: Dict[str, Any]) -> Dict[str, str]:
    """任务 payload 作用域强制点：kb_id/tenant_id/project_id 必填。

    缺失 → ValidationException(error_code=KB_SCOPE_REQUIRED)；格式非法 → SCOPE_INVALID。
    在任何知识数据操作之前调用。
    """
    payload = payload or {}
    missing = [
        field
        for field in SCOPE_REQUIRED_FIELDS
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise ValidationException(
            f"任务 payload 缺少作用域字段: {', '.join(missing)}",
            error_code=ErrorCode.KB_SCOPE_REQUIRED,
            details={"missing_fields": missing},
        )
    normalized = {
        field: normalize_scope_id(field, payload.get(field)) or ""
        for field in SCOPE_REQUIRED_FIELDS
    }
    empty = [field for field, value in normalized.items() if not value]
    if empty:
        raise ValidationException(
            f"任务 payload 作用域字段归一后为空: {', '.join(empty)}",
            error_code=ErrorCode.KB_SCOPE_REQUIRED,
            details={"fields": empty},
        )
    return normalized


def execute_job(*, job_id: int, job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if job_type == "build_graph":
        return execute_build_graph(job_id=job_id, payload=payload)
    if job_type == "clear_kb":
        return execute_clear_kb(job_id=job_id, payload=payload)
    if job_type == "reindex_chunks":
        return execute_reindex_chunks(job_id=job_id, payload=payload)
    if job_type == "reindex":
        return execute_reindex(job_id=job_id, payload=payload)
    raise ValidationException(f"任务类型暂不支持执行: {job_type}")


def execute_build_graph(*, job_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 作用域强制点：payload 缺 kb_id/tenant_id/project_id 时在执行任何工作前失败
    scope = require_payload_scope(payload)
    force = _to_bool(payload.get("force"), False)
    source = str(payload.get("source") or "documents")
    note = payload.get("note")
    doc_ids = [str(item).strip() for item in (payload.get("doc_ids") or []) if str(item).strip()]
    complex_extraction = _to_bool(payload.get("complex_extraction"), False)
    parser_provider = str(payload.get("parser_provider") or "").strip().lower()
    explicit_profile = str(payload.get("reasoning_profile") or "").strip().lower()
    default_profile = "balanced" if complex_extraction else "fast"
    reasoning_profile = explicit_profile or default_profile
    if explicit_profile not in {"fast", "balanced", "deep"}:
        reasoning_profile = _normalize_reasoning_profile(
            get_graph_build_runtime_defaults(complex_extraction=complex_extraction),
            default_profile,
        )

    try:
        # worker 只处理 payload 显式列出的 doc_ids（contract §2.12 规则 2）
        stats = DocumentGraphService().build_graph(
            kb_id=scope["kb_id"],
            tenant_id=scope["tenant_id"],
            project_id=scope["project_id"],
            doc_ids=doc_ids,
            force=force,
            reasoning_profile=reasoning_profile,
            complex_extraction=complex_extraction,
            parser_provider=parser_provider or None,
        )
    except Exception:
        try:
            from services.neo4j_service import get_neo4j_service

            get_neo4j_service().ensure_connected(force_reconnect=True)
        except Exception as reconnect_exc:  # noqa: BLE001
            logger.warning(
                "建图失败后刷新 Neo4j 连接失败",
                context={"job_id": job_id, "error": str(reconnect_exc)},
            )
        raise

    failures = stats.get("failures", [])
    processed = stats.get("documents", 0)
    total = stats.get("total_documents", 0)
    skipped = stats.get("skipped_documents", 0)

    execution_status = "completed" if processed > 0 else "empty"
    if processed > 0:
        message = "构建完成"
    elif total > 0 and skipped == total:
        execution_status = "completed"
        message = "文档未变更，已跳过"
    elif failures:
        message = "解析失败，未产出图谱"
    else:
        message = "未发现可解析文档"

    return {
        "job_id": job_id,
        "job_type": "build_graph",
        "execution_status": execution_status,
        "message": message,
        "source": source,
        "force": force,
        "note": note,
        "kb_id": scope["kb_id"],
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "doc_ids": doc_ids,
        "reasoning_profile": reasoning_profile,
        "complex_extraction": complex_extraction,
        "parser_provider": parser_provider,
        "stats": stats,
        "failures": failures,
    }


def execute_reindex_chunks(*, job_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """重建 chunk 的 Neo4j/Milvus 投影（设计 §8.2）。

    与 `execute_reindex` 严格区分：后者只重建 Neo4j 全文索引（基础设施操作、不带知识
    数据作用域），本任务必须先由 require_payload_scope 冻结 kb/tenant/project 才允许动投影。
    """
    scope = require_payload_scope(payload)
    from services.chunk_projection_reindex import reindex_chunks

    return reindex_chunks(job_id=job_id, payload=payload, scope=scope)


def _remove_empty_dirs(root: Path) -> int:
    """自底向上删除 root 下的空目录（不含 root 本身），返回删除数量。"""
    removed = 0
    if not root.exists():
        return 0
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            next(directory.iterdir())
        except StopIteration:
            try:
                directory.rmdir()
                removed += 1
            except OSError:
                continue
        except OSError:
            continue
    return removed


def execute_clear_kb(*, job_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """清空单个知识库：注册表文件 + 图谱 + 向量 + 解析产物，全部限定在目标 KB 内。"""
    scope = require_payload_scope(payload)
    kb_id = scope["kb_id"]
    purge_graph = _to_bool(payload.get("purge_graph"), True)

    from services import document_registry

    kb_row = document_registry.get_knowledge_base(kb_id)
    if kb_row is None:
        raise KnowledgeScopeError(
            ErrorCode.KB_NOT_FOUND,
            message=f"知识库不存在: {kb_id}",
            details={"kb_id": kb_id},
        )

    removed_files = 0
    removed_errors: List[str] = []
    storage_root = document_registry.resolve_kb_storage_root(kb_row)
    for doc in document_registry.list_kb_documents(kb_id):
        try:
            file_path = document_registry.resolve_document_file_path(doc, kb_row)
        except KnowledgeScopeError as exc:
            removed_errors.append(f"{getattr(doc, 'doc_id', '')}: {exc.error_code}")
            continue
        try:
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
                removed_files += 1
        except Exception as exc:  # noqa: BLE001
            removed_errors.append(f"{file_path.name}: {exc}")
    removed_dirs = _remove_empty_dirs(storage_root)

    graph_stats = None
    if purge_graph:
        # clear_document_graph 内部同时清理该 KB 的向量（orchestrator.clear）与解析产物
        graph_stats = DocumentGraphService().clear_document_graph(kb_id)

    return {
        "job_id": job_id,
        "job_type": "clear_kb",
        "execution_status": "completed",
        "message": "知识库已清空",
        "kb_id": kb_id,
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "removed_errors": removed_errors[:20],
        "purge_graph": purge_graph,
        "graph": graph_stats,
    }


def execute_reindex(*, job_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """重建 Neo4j 全文索引。

    注意：这是基础设施操作（索引投影），不携带知识数据作用域；
    payload 缺少 kb_id 不视为错误（与 build_graph/clear_kb 不同）。
    """
    index_name = _safe_index_name(str(payload.get("index_name") or "chunkText"))
    from services.neo4j_service import get_neo4j_service

    neo4j_service = get_neo4j_service()
    before_state: List[Dict[str, Any]] = []
    after_state: List[Dict[str, Any]] = []

    neo4j_service.ensure_connected()
    with neo4j_service.session() as session:
        try:
            before_state = [
                dict(item)
                for item in session.run(
                    "SHOW INDEXES YIELD name, type, state WHERE name = $name RETURN name, type, state",
                    {"name": index_name},
                )
            ]
        except Exception:  # noqa: BLE001
            before_state = []

        session.run(f"DROP INDEX {index_name} IF EXISTS")
        session.run(f"CREATE FULLTEXT INDEX {index_name} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]")

        try:
            after_state = [
                dict(item)
                for item in session.run(
                    "SHOW INDEXES YIELD name, type, state WHERE name = $name RETURN name, type, state",
                    {"name": index_name},
                )
            ]
        except Exception:  # noqa: BLE001
            after_state = []

    return {
        "job_id": job_id,
        "job_type": "reindex",
        "execution_status": "completed",
        "message": "索引重建完成",
        "scope": "infra_fulltext_index",
        "index_name": index_name,
        "before": before_state,
        "after": after_state,
    }
