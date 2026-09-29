"""Runtime QA trace recording for Python capability routes."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from admin.schemas.qa_traces import QATraceCreate
from admin.services.qa_trace_service import qa_trace_service
from services.scope_contract import SearchTarget


def _scope_snapshot(scope: Optional[SearchTarget]) -> Optional[Dict[str, Any]]:
    """请求作用域快照（契约 §12.4：trace 必须可复核请求范围）。"""
    if scope is None:
        return None
    return {
        "kb_ids": list(scope.kb_ids or []),
        "document_ids": list(scope.document_ids or []),
        "tenant_id": scope.tenant_id,
        "project_id": scope.project_id,
    }


def record_qa_trace(
    db: Session,
    *,
    request: Request,
    operator_id: Optional[int],
    qa_type: str,
    question: str,
    top_k: int,
    started_at: float,
    result: Optional[Dict[str, Any]] = None,
    status: str = "success",
    error: Optional[str] = None,
    scope: Optional[SearchTarget] = None,
) -> None:
    trace_payload = (result or {}).get("trace") if isinstance(result, dict) else None
    citations = (result or {}).get("citations") if isinstance(result, dict) else []
    answer_preview = (
        (result or {}).get("answer")
        or (result or {}).get("final_conclusion")
        or (result or {}).get("summary")
        or ""
    )
    retrieval = trace_payload.get("retrieval") if isinstance(trace_payload, dict) else None
    generation = trace_payload.get("generation") if isinstance(trace_payload, dict) else None
    response = trace_payload.get("response") if isinstance(trace_payload, dict) else None
    model = generation.get("model") if isinstance(generation, dict) else None
    retrieval_count = int((retrieval or {}).get("count") or len(citations or [])) if isinstance(retrieval, dict) else len(citations or [])

    # 请求作用域写入 snapshot；retrieval snapshot 已带 scope 时以链路内记录为准
    scope_snapshot = _scope_snapshot(scope)
    if scope_snapshot is not None:
        if isinstance(retrieval, dict):
            retrieval = {**retrieval, "scope": retrieval.get("scope") or scope_snapshot}
        else:
            retrieval = {"scope": scope_snapshot}

    qa_trace_service.create_trace(
        db,
        QATraceCreate(
            trace_id=getattr(request.state, "trace_id", None),
            qa_type=qa_type,
            status=status,
            question=question,
            operator_id=operator_id,
            tenant_id=scope.tenant_id if scope is not None else None,
            project_id=scope.project_id if scope is not None else None,
            kb_id=(scope.kb_ids[0] if scope.kb_ids else None) if scope is not None else None,
            model=model,
            top_k=top_k,
            latency_ms=int(round((time.perf_counter() - started_at) * 1000)),
            retrieval_count=retrieval_count,
            citation_count=len(citations or []),
            answer_preview=str(answer_preview or "")[:1200],
            retrieval_snapshot=retrieval,
            generation_snapshot=generation,
            response_snapshot=response,
            error_message=error,
        ),
    )
