"""Internal capability routes for vector store maintenance."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from api.internal_access import require_go_capability_request
from core import error_response, success_response
from core.exceptions import KnowledgeScopeError
from services.retrieval_orchestrator import retrieval_orchestrator

internal_router = APIRouter()


class VectorDeleteDocRequest(BaseModel):
    """单文档向量清理请求（kb 必填，禁止全库操作）。"""

    doc_id: str = Field(..., min_length=1, max_length=64)
    kb_id: str = Field(..., min_length=1, max_length=100)


@internal_router.post("/internal/vector/delete-doc", summary="内部单文档向量清理入口")
async def internal_vector_delete_doc(
    payload: VectorDeleteDocRequest,
    request: Request,
):
    denied = require_go_capability_request(request)
    if denied is not None:
        return denied
    try:
        deleted = retrieval_orchestrator.delete_doc(payload.doc_id, payload.kb_id)
    except KnowledgeScopeError as exc:
        return error_response(
            message=exc.message,
            code=exc.status_code,
            error_code=exc.error_code,
            details=exc.details,
        )
    return success_response(
        data={"deleted": bool(deleted), "kb_id": payload.kb_id, "doc_id": payload.doc_id},
        message="ok",
    )
