"""Helpers for Python internal entrypoints reached from the Go gateway."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request, status

from core import error_response


GO_ORCHESTRATOR_HEADER = "X-Go-Orchestrator"
GO_CONTROL_HEADER = "X-Go-Proxy"
GO_HEADER_VALUE = "graphinsight-go"
TRACE_HEADER = "X-Trace-Id"


def is_go_orchestrator_request(request: Request) -> bool:
    return request.headers.get(GO_ORCHESTRATOR_HEADER) == GO_HEADER_VALUE


def is_go_control_plane_request(request: Request) -> bool:
    return request.headers.get(GO_CONTROL_HEADER) == GO_HEADER_VALUE


def require_go_capability_request(request: Request):
    if not is_go_orchestrator_request(request):
        return error_response(
            message="禁止访问",
            code=status.HTTP_403_FORBIDDEN,
            error_code="FORBIDDEN",
        )
    trace_id = (request.headers.get(TRACE_HEADER) or request.headers.get(TRACE_HEADER.lower()) or "").strip()
    if trace_id:
        return None
    return error_response(
        message="缺少 trace_id",
        code=status.HTTP_400_BAD_REQUEST,
        error_code="MISSING_TRACE_ID",
    )


def header_scope_map(request: Request) -> Dict[str, Any]:
    """收集 x-tenant-id / x-project-id / x-kb-id / x-kb-ids 请求头（契约 §3.2）。"""
    values: Dict[str, Any] = {}
    aliases = {
        "x-tenant-id": "tenant_id",
        "x-project-id": "project_id",
        "x-kb-id": "kb_id",
        "x-kb-ids": "kb_ids",
    }
    for header_name, field_name in aliases.items():
        raw = request.headers.get(header_name)
        if isinstance(raw, str) and raw.strip():
            values[field_name] = raw
    return values


def query_scope_map(request: Request) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for field_name in ("tenant_id", "project_id", "kb_id", "kb_ids", "document_ids", "doc_ids"):
        raw = request.query_params.getlist(field_name)
        if not raw:
            continue
        values[field_name] = raw[0] if len(raw) == 1 else [item for item in raw if item]
    return values


def body_scope_map(payload: Any) -> Dict[str, Any]:
    """从已验证的请求模型收集作用域字段（仅包含显式传入的字段）。"""
    values: Dict[str, Any] = {}
    for field_name in ("tenant_id", "project_id", "kb_id", "kb_ids", "document_ids"):
        raw = getattr(payload, field_name, None)
        if raw is None:
            continue
        values[field_name] = raw
    return values


def resolve_request_scope(request: Request, payload: Any = None):
    """内部入口的严格作用域解析（契约 §2.4/§3.2，纵深防御层）。

    header / query / body 三个来源统一交给 scope_contract.resolve_search_target：
    缺少 kb 作用域 → KB_SCOPE_REQUIRED；多来源不一致 → KB_CROSS_SCOPE。
    Go 侧已做授权，但 Python 数据面不把“来自 Go”当作授权证明。
    """
    from services.scope_contract import resolve_search_target

    return resolve_search_target(
        header=header_scope_map(request),
        query=query_scope_map(request),
        body=body_scope_map(payload) if payload is not None else {},
    )


def operator_id_from_headers(request: Request) -> Optional[int]:
    raw = (request.headers.get("x-auth-user-id") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    return value if value > 0 else None
