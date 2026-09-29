"""知识库文档注册表数据访问层（M3）。

`knowledge_bases` / `knowledge_base_documents` 是文档元数据的权威来源
（契约 §4.2，Go 控制面负责写入，Python 数据面只读消费）。

本模块职责：
- 按 doc_id / kb_id 读取注册表行；
- 从注册表行提取规范化的作用域三元组（tenant_id/project_id/kb_id）；
- 解析文档物理路径：`document_storage_path / kb.storage_prefix / doc.relative_path`，
  并做路径逃逸校验（KB_STORAGE_PATH_INVALID）。

本模块不修改注册表数据；状态回写仍归 Go 控制面所有（所有权矩阵 §4.2）。
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional

from config import get_settings
from core import get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.scope_contract import normalize_scope_id

logger = get_logger()
settings = get_settings()


def _open_session():
    """惰性获取注册表会话（避免模块导入期建立数据库引擎）。"""
    from admin.database import SessionLocal

    return SessionLocal()


def get_knowledge_base(kb_id: Optional[str]) -> Optional[Any]:
    """按 kb_id 读取知识库行；kb_id 缺失/非法时返回 None。"""
    clean = normalize_scope_id("kb_id", kb_id)
    if not clean:
        return None
    from admin.models import KnowledgeBase

    db = _open_session()
    try:
        return db.query(KnowledgeBase).filter(KnowledgeBase.id == clean).first()
    finally:
        db.close()


def get_document(doc_id: str) -> Optional[Any]:
    """按 doc_id 读取文档注册表行。"""
    clean = str(doc_id or "").strip()
    if not clean:
        return None
    from admin.models import KnowledgeBaseDocument

    db = _open_session()
    try:
        return (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.doc_id == clean)
            .first()
        )
    finally:
        db.close()


def get_documents(doc_ids: List[str]) -> Dict[str, Any]:
    """批量按 doc_id 读取注册表行，返回 {doc_id: row} 映射。"""
    clean_ids: List[str] = []
    for item in doc_ids or []:
        value = str(item or "").strip()
        if value and value not in clean_ids:
            clean_ids.append(value)
    if not clean_ids:
        return {}
    from admin.models import KnowledgeBaseDocument

    db = _open_session()
    try:
        rows = (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.doc_id.in_(clean_ids))
            .all()
        )
    finally:
        db.close()
    return {str(row.doc_id): row for row in rows}


def list_kb_documents(kb_id: str) -> List[Any]:
    """列出一个知识库下的全部文档注册表行（含 archived，供清理任务使用）。"""
    clean = normalize_scope_id("kb_id", kb_id)
    if not clean:
        return []
    from admin.models import KnowledgeBaseDocument

    db = _open_session()
    try:
        return (
            db.query(KnowledgeBaseDocument)
            .filter(KnowledgeBaseDocument.kb_id == clean)
            .all()
        )
    finally:
        db.close()


def document_scope(doc: Any) -> Dict[str, str]:
    """从注册表行提取规范化的作用域三元组（注册表行是作用域权威）。"""
    return {
        "kb_id": normalize_scope_id("kb_id", getattr(doc, "kb_id", None)) or "",
        "tenant_id": normalize_scope_id("tenant_id", getattr(doc, "tenant_id", None)) or "",
        "project_id": normalize_scope_id("project_id", getattr(doc, "project_id", None)) or "",
    }


def assert_safe_relative(value: str, field_name: str) -> str:
    """校验 storage_prefix / relative_path：仅允许相对路径片段，禁止逃逸。"""
    raw = str(value or "").strip()
    if not raw:
        raise KnowledgeScopeError(
            ErrorCode.KB_STORAGE_PATH_INVALID,
            message=f"{field_name} 不能为空",
            details={"field": field_name},
        )
    if raw.startswith("/") or raw.startswith("\\") or "\x00" in raw or "\\" in raw:
        raise KnowledgeScopeError(
            ErrorCode.KB_STORAGE_PATH_INVALID,
            message=f"{field_name} 含非法字符或绝对路径",
            details={"field": field_name},
        )
    if PureWindowsPath(raw).drive:
        raise KnowledgeScopeError(
            ErrorCode.KB_STORAGE_PATH_INVALID,
            message=f"{field_name} 不允许 Windows 盘符",
            details={"field": field_name},
        )
    cleaned = raw.strip("/")
    pure = PurePosixPath(cleaned)
    if not cleaned or pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
        raise KnowledgeScopeError(
            ErrorCode.KB_STORAGE_PATH_INVALID,
            message=f"{field_name} 不允许绝对路径或路径逃逸",
            details={"field": field_name},
        )
    return cleaned


def resolve_kb_storage_root(kb: Any) -> Path:
    """知识库文件根目录：document_storage_path / storage_prefix。"""
    prefix = assert_safe_relative(str(getattr(kb, "storage_prefix", "") or ""), "storage_prefix")
    root = Path(settings.document_storage_path).resolve()
    return root / prefix


def resolve_document_file_path(doc: Any, kb: Optional[Any] = None) -> Path:
    """解析文档原始文件绝对路径：document_storage_path / storage_prefix / relative_path。

    路径必须严格位于该 KB 的 storage_prefix 下，否则抛 KB_STORAGE_PATH_INVALID。
    """
    kb_row = kb
    if kb_row is None:
        kb_row = get_knowledge_base(getattr(doc, "kb_id", None))
    if kb_row is None:
        raise KnowledgeScopeError(
            ErrorCode.KB_NOT_FOUND,
            message=f"知识库不存在，无法解析文档路径: {getattr(doc, 'kb_id', '')}",
            details={"kb_id": str(getattr(doc, "kb_id", "") or "")},
        )
    relative = assert_safe_relative(str(getattr(doc, "relative_path", "") or ""), "relative_path")
    base = resolve_kb_storage_root(kb_row)
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise KnowledgeScopeError(
            ErrorCode.KB_STORAGE_PATH_INVALID,
            message="文档路径逃逸出知识库存储前缀",
            details={"field": "relative_path"},
        )
    return target


__all__ = [
    "assert_safe_relative",
    "document_scope",
    "get_document",
    "get_documents",
    "get_knowledge_base",
    "list_kb_documents",
    "resolve_document_file_path",
    "resolve_kb_storage_root",
]
