"""
知识库作用域契约（冻结，见 docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2）

本模块是 Python 侧契约的唯一实现点：
- 作用域标识归一化与格式校验（§2.1，D5 规格）
- SearchTarget 与 effective_kb_ids 交集算法（§2.4）
- KBGrant / ChunkRevision / PipelineRun / StepRun 契约类型（§2.5-2.7）
- Neo4j entity_key / relation_key 与 Milvus kb filter helper（§2.2、§3.4）

规则：缺少 kb_id/kb_ids 一律 KB_SCOPE_REQUIRED，不保留 default KB 兜底；
header/query/body 同时携带且不一致 → KB_CROSS_SCOPE。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from core.exceptions import ErrorCode, KnowledgeScopeError

# 契约 §2.1（D5 规格）：小写字母或数字开头，总长 2-100
SCOPE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,99}$")

# 统一字符串错误码（与 Go 侧 internal/scope 保持一致）
CODE_KB_SCOPE_REQUIRED = ErrorCode.KB_SCOPE_REQUIRED
CODE_KB_NOT_FOUND = ErrorCode.KB_NOT_FOUND
CODE_KB_ACCESS_DENIED = ErrorCode.KB_ACCESS_DENIED
CODE_KB_ARCHIVED = ErrorCode.KB_ARCHIVED
CODE_KB_CROSS_SCOPE = ErrorCode.KB_CROSS_SCOPE
CODE_KB_DUPLICATE_NAME = ErrorCode.KB_DUPLICATE_NAME
CODE_KB_STORAGE_PATH_INVALID = ErrorCode.KB_STORAGE_PATH_INVALID
CODE_SCOPE_INVALID = ErrorCode.SCOPE_INVALID
CODE_CHUNK_REVISION_CONFLICT = ErrorCode.CHUNK_REVISION_CONFLICT


def normalize_scope_id(kind: str, value: Optional[str]) -> Optional[str]:
    """trim + 小写归一 + 格式校验；空值返回 None，非法值抛 SCOPE_INVALID。

    kind 仅用于错误信息（tenant_id / project_id / kb_id）。
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if not SCOPE_ID_PATTERN.match(normalized):
        raise KnowledgeScopeError(
            error_code=CODE_SCOPE_INVALID,
            message=f"{kind} 格式非法: {normalized!r}",
            details={"field": kind, "value_length": len(normalized)},
        )
    return normalized


def scope_error_required() -> KnowledgeScopeError:
    return KnowledgeScopeError(
        error_code=CODE_KB_SCOPE_REQUIRED,
        message="请求缺少 kb_id/kb_ids，所有知识库请求必须显式携带作用域",
    )


def scope_error_cross_scope(field_name: str) -> KnowledgeScopeError:
    return KnowledgeScopeError(
        error_code=CODE_KB_CROSS_SCOPE,
        message=f"请求作用域不一致: {field_name}",
        details={"field": field_name},
    )


def _merge_single(sources: Sequence[Mapping[str, Any]], field_name: str) -> Optional[str]:
    """单值字段（tenant_id/project_id）：header > query > body 取首个；
    多个来源同时携带且归一后不一致 → KB_CROSS_SCOPE。"""
    seen: Optional[str] = None
    for source in sources:
        raw = source.get(field_name)
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = normalize_scope_id(field_name, raw)
        if normalized is None:
            continue
        if seen is not None and normalized != seen:
            raise scope_error_cross_scope(field_name)
        if seen is None:
            seen = normalized
    return seen


def _resolve_kb_ids(header: Mapping[str, Any], query: Mapping[str, Any], body: Mapping[str, Any]) -> List[str]:
    """kb 作用域解析：kb_id 与 kb_ids 视为同一集合的不同写法。

    每个来源（header/query/body）归一为一个集合；不同来源给出不同集合 → KB_CROSS_SCOPE；
    全部缺失 → KB_SCOPE_REQUIRED（无 default 兜底）。
    """
    source_sets: List[List[str]] = []
    for source in (header, query, body):
        raw_values: List[str] = []
        single = source.get("kb_id")
        if isinstance(single, str) and single.strip():
            raw_values.append(single)
        multi = source.get("kb_ids")
        if multi:
            if isinstance(multi, str):
                raw_values.extend(item.strip() for item in multi.split(",") if item.strip())
            else:
                raw_values.extend(str(item).strip() for item in multi if str(item).strip())
        normalized: List[str] = []
        for value in raw_values:
            item = normalize_scope_id("kb_id", value)
            if item and item not in normalized:
                normalized.append(item)
        if normalized and normalized not in source_sets:
            source_sets.append(normalized)
    if len(source_sets) > 1:
        raise scope_error_cross_scope("kb_id/kb_ids")
    if not source_sets:
        raise scope_error_required()
    return source_sets[0]


def resolve_search_target(
    header: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
) -> "SearchTarget":
    """解析 SearchTarget（契约 §2.4）。

    三个来源都是字段到 str/list[str] 的映射；同一字段多来源不一致 → KB_CROSS_SCOPE；
    kb 作用域完全缺失 → KB_SCOPE_REQUIRED（无 default 兜底）。
    """
    header = header or {}
    query = query or {}
    body = body or {}
    sources = [header, query, body]

    tenant_id = _merge_single(sources, "tenant_id")
    project_id = _merge_single(sources, "project_id")
    kb_ids = _resolve_kb_ids(header, query, body)

    doc_ids: List[str] = []
    for raw in (body.get("document_ids"), body.get("doc_ids"), query.get("doc_ids")):
        if isinstance(raw, Iterable) and not isinstance(raw, str):
            for item in raw:
                value = str(item).strip()
                if value and value not in doc_ids:
                    doc_ids.append(value)

    return SearchTarget(
        tenant_id=tenant_id,
        project_id=project_id,
        kb_ids=kb_ids,
        document_ids=doc_ids,
    )


@dataclass
class SearchTarget:
    """检索目标（契约 §2.4）。P0 只实现 kb_ids/document_ids，folder/tag 为保留字段。"""

    kb_ids: List[str]
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    document_ids: List[str] = field(default_factory=list)
    folder_ids: List[str] = field(default_factory=list)  # 保留字段，暂不实现
    tag_ids: List[str] = field(default_factory=list)  # 保留字段，暂不实现

    def effective_kb_ids(self, authorized_kb_ids: Iterable[str]) -> List[str]:
        """请求范围 ∩ 服务端授权范围；交集为空 → KB_ACCESS_DENIED（契约 §2.4 规则 1）。"""
        authorized = {item for item in authorized_kb_ids if item}
        effective = [item for item in self.kb_ids if item in authorized]
        if not effective:
            raise KnowledgeScopeError(
                error_code=CODE_KB_ACCESS_DENIED,
                message="请求的知识库不在授权范围内",
                details={"requested": self.kb_ids},
            )
        return effective


@dataclass
class KBGrant:
    """知识库授权授予（契约 §2.5）。P0 复用 admin_user_role_bindings，不新建表。"""

    kb_id: str
    subject_type: str  # user | api_key | application
    subject_id: str
    role: str  # viewer | editor | admin
    capabilities: List[str] = field(default_factory=list)


@dataclass
class ChunkRevision:
    """Chunk 版本（契约 §2.6，M5 落库）。source_content 不可变；并发编辑返回 CHUNK_REVISION_CONFLICT。"""

    revision_id: str
    kb_id: str
    chunk_id: str
    source_content: str
    content: str
    content_revision: int
    edited_by: Optional[str] = None
    reason: Optional[str] = None
    status: str = "active"


@dataclass
class PipelineStep:
    """Pipeline 步骤运行（契约 §2.7）。"""

    name: str  # parse | chunk | extract | graph_write | vector_write
    status: str = "pending"
    output_count: int = 0
    error_summary: str = ""
    retry_count: int = 0
    duration_ms: int = 0
    provider_version: str = ""
    trace_id: Optional[str] = None


@dataclass
class PipelineRun:
    """Pipeline 总运行（契约 §2.7）。任务中心仍是调度/审计 owner。"""

    run_id: str
    task_type: str
    kb_id: str
    tenant_id: str
    project_id: str
    document_ids: List[str] = field(default_factory=list)
    pipeline_version: str = "ingestion-v1"
    status: str = "pending"
    steps: List[PipelineStep] = field(default_factory=list)


def entity_key(kb_id: str, normalized_name: str, entity_type: str) -> str:
    """知识库作用域内稳定实体键（契约 §2.2）。禁止跨 kb 合并同名实体。"""
    material = "|".join(
        (normalize_scope_id("kb_id", kb_id) or "", normalized_name.strip().lower(), entity_type.strip().lower())
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def relation_key(
    kb_id: str,
    subject_key: str,
    predicate: str,
    object_key: str,
    evidence_chunk_id: str,
) -> str:
    """知识库作用域内稳定关系键（契约 §2.2）。"""
    material = "|".join(
        (
            normalize_scope_id("kb_id", kb_id) or "",
            subject_key,
            predicate.strip().lower(),
            object_key,
            evidence_chunk_id,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def milvus_kb_filter(kb_ids: Sequence[str]) -> str:
    """构造 Milvus kb_id 过滤表达式（契约 §3.4：filter 为空即拒绝）。

    值来自 normalize_scope_id 校验，不含需转义的引号字符。
    """
    if not kb_ids:
        raise scope_error_required()
    normalized = [normalize_scope_id("kb_id", item) for item in kb_ids]
    normalized = [item for item in normalized if item]
    if not normalized:
        raise scope_error_required()
    return "kb_id in [" + ", ".join(f'"{item}"' for item in normalized) + "]"


def require_kb_scope(kb_ids: Optional[Sequence[str]]) -> List[str]:
    """无作用域直接拒绝的统一入口；任何 Neo4j/Milvus/全文查询前必须调用。"""
    if not kb_ids:
        raise scope_error_required()
    normalized = [normalize_scope_id("kb_id", item) for item in kb_ids]
    normalized = [item for item in normalized if item]
    if not normalized:
        raise scope_error_required()
    return normalized
