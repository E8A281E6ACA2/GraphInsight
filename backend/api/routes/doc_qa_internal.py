"""Internal capability routes for DocQA."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.internal_access import (
    is_go_control_plane_request,
    operator_id_from_headers,
    require_go_capability_request,
    resolve_request_scope,
)
from api.routes.doc_qa import (
    DeepResearchRequest,
    DocQARequest,
    handle_deep_research,
    handle_doc_qa,
    handle_doc_qa_health,
)
from core import error_response, success_response
from services.retrieval_orchestrator import retrieval_orchestrator
from services.runtime_db import get_runtime_db

internal_router = APIRouter()


class RetrievalDiagnosticsRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    modes: List[str] = Field(
        default_factory=lambda: ["keyword", "vector", "hybrid", "graph_hybrid"],
        min_length=1,
        max_length=4,
    )
    # M4：诊断同样只允许在显式作用域内执行
    kb_id: str | None = None
    kb_ids: List[str] | None = None


@internal_router.post("/internal/docqa", summary="内部文档问答能力入口")
async def internal_doc_qa(
    payload: DocQARequest,
    request: Request,
    db: Session = Depends(get_runtime_db),
):
    denied = require_go_capability_request(request)
    if denied is not None:
        return denied
    # 作用域强制点（契约 §2.4）：header/query/body 联合解析，缺失即 KB_SCOPE_REQUIRED
    scope = resolve_request_scope(request, payload)
    return handle_doc_qa(
        payload=payload,
        request=request,
        db=db,
        operator_id=operator_id_from_headers(request),
        scope=scope,
    )


@internal_router.post("/internal/docqa/deep-research", summary="内部文档深度调研能力入口")
async def internal_doc_qa_deep_research(
    payload: DeepResearchRequest,
    request: Request,
    db: Session = Depends(get_runtime_db),
):
    denied = require_go_capability_request(request)
    if denied is not None:
        return denied
    scope = resolve_request_scope(request, payload)
    return handle_deep_research(
        payload=payload,
        request=request,
        db=db,
        operator_id=operator_id_from_headers(request),
        scope=scope,
    )


@internal_router.get("/internal/docqa/health", summary="内部文档问答健康检查能力入口")
async def internal_doc_qa_health(
    request: Request,
    probe_llm: bool = False,
):
    denied = require_go_capability_request(request)
    if denied is not None:
        return denied
    return handle_doc_qa_health(probe_llm=probe_llm, request=request, internal=True)


@internal_router.post("/internal/docqa/retrieval-diagnostics", summary="内部检索诊断能力入口")
async def internal_doc_qa_retrieval_diagnostics(
    payload: RetrievalDiagnosticsRequest,
    request: Request,
):
    if not is_go_control_plane_request(request):
        return error_response(
            message="禁止访问",
            code=status.HTTP_403_FORBIDDEN,
            error_code="FORBIDDEN",
        )
    scope = resolve_request_scope(request, payload)
    data = retrieval_orchestrator.diagnose(
        question=payload.question,
        top_k=payload.top_k,
        modes=payload.modes,
        kb_ids=scope.kb_ids,
        document_ids=scope.document_ids,
    )
    return success_response(data=data, message="ok")
