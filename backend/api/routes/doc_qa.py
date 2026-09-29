"""
文档问答共享能力
返回带引用摘要的回答
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import error_response, get_logger, success_response
from core.exceptions import KnowledgeScopeError
from core.observability import get_qa_observability
from services.doc_qa_service import doc_qa_service
from services.model_runtime_policy import normalize_reasoning_profile
from services.qa_trace_runtime import record_qa_trace
from services.scope_contract import SearchTarget

logger = get_logger()
qa_metrics = get_qa_observability()


def _resolve_reasoning_profile(explicit_value: Optional[str], scenario: str) -> str:
    normalized = normalize_reasoning_profile(explicit_value, "deep" if scenario == "deep_research" else "balanced")
    if normalized in {"fast", "balanced", "deep"}:
        return normalized
    if scenario == "deep_research":
        return "deep"
    return "balanced"


class CitationItem(BaseModel):
    id: str
    title: str
    snippet: str
    location: Optional[str] = None
    entity_names: List[str] = Field(default_factory=list)
    retrieval_score: Optional[float] = None
    confidence: Optional[float] = None
    confidence_level: Optional[str] = None
    # M4：citation 携带作用域与证据定位（契约 §12.1/§16 M4）
    kb_id: Optional[str] = None
    doc_id: Optional[str] = None
    chunk_id: Optional[str] = None
    content_revision: Optional[int] = None


class ConversationTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=2000)


class DocQARequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(2, ge=1, le=5)
    require_citation: bool = Field(True)
    reasoning_profile: str | None = Field(default=None, pattern="^(fast|balanced|deep)$")
    conversation_history: List[ConversationTurn] = Field(default_factory=list, max_length=8)
    # M4：显式作用域；internal 入口还会与 header/query 联合校验（契约 §3.2）
    kb_id: str | None = None
    kb_ids: List[str] | None = None
    document_ids: List[str] | None = None


class DocQAResponse(BaseModel):
    answer: str
    citations: List[CitationItem]


class DeepResearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(8, ge=3, le=20, description="每个子问题的检索片段上限")
    max_sub_questions: int = Field(4, ge=2, le=8, description="子问题拆解上限")
    reasoning_profile: str | None = Field(default=None, pattern="^(fast|balanced|deep)$")
    # M4：显式作用域
    kb_id: str | None = None
    kb_ids: List[str] | None = None


class DeepResearchResponse(BaseModel):
    question: str
    summary: str
    final_conclusion: str
    report: str
    sub_questions: List[str]
    citations: List[CitationItem]
    confidence: Dict[str, Any]
    evidence_stats: Dict[str, Any]


def _docqa_health_status(result: Dict[str, Any]) -> str:
    neo4j_ok = bool((result.get("neo4j") or {}).get("ok"))
    if not neo4j_ok:
        return "unhealthy"

    retrieval = result.get("retrieval") or {}
    llm = result.get("llm") or {}
    if not bool(retrieval.get("ok")) or not bool(llm.get("ok")):
        return "degraded"

    return "healthy"


def _build_citations(result: Dict[str, Any]) -> List[CitationItem]:
    citations = []
    for item in result.get("citations", []):
        try:
            chunk_id = str(item.get("chunk_id") or item.get("id") or "")
            content_revision = item.get("content_revision")
            citations.append(
                CitationItem(
                    id=str(item.get("id") or ""),
                    title=str(item.get("title") or "文档片段"),
                    snippet=item.get("snippet") or item.get("text", "")[:140],
                    location=item.get("location"),
                    entity_names=item.get("entity_names") or [],
                    retrieval_score=item.get("retrieval_score"),
                    confidence=item.get("confidence"),
                    confidence_level=item.get("confidence_level"),
                    kb_id=item.get("kb_id"),
                    doc_id=item.get("doc_id"),
                    chunk_id=chunk_id or None,
                    content_revision=int(content_revision) if content_revision is not None else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("构造引用摘要失败", context={"error": str(exc), "item": item})
    return citations


def handle_doc_qa(
    *,
    payload: DocQARequest,
    request: Request,
    db: Session,
    operator_id: Optional[int],
    scope: SearchTarget,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    try:
        result = doc_qa_service.answer(
            payload.question,
            payload.top_k,
            reasoning_profile=payload.reasoning_profile,
            conversation_history=[item.model_dump() for item in payload.conversation_history],
            kb_ids=scope.kb_ids,
            document_ids=scope.document_ids,
        )
        if isinstance(result.get("trace"), dict):
            generation = result["trace"].setdefault("generation", {})
            generation["reasoning_profile"] = _resolve_reasoning_profile(payload.reasoning_profile, "docqa")
        citations = _build_citations(result)
        response = DocQAResponse(answer=result.get("answer", ""), citations=citations)
        logger.info(
            "文档问答请求",
            context={"question": payload.question, "citations": len(citations), "kb_ids": scope.kb_ids},
        )
        qa_metrics.record_qa(
            qa_type="docqa",
            success=True,
            citation_count=len(citations),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="docqa",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            result=result,
            scope=scope,
        )
        return success_response(data=response.model_dump(), message="ok")
    except KnowledgeScopeError as exc:
        logger.warning(
            "文档问答作用域被拒绝",
            context={"error_code": exc.error_code, "kb_ids": scope.kb_ids},
        )
        qa_metrics.record_qa(
            qa_type="docqa",
            success=False,
            citation_count=0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=exc.error_code,
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="docqa",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            status="failed",
            error=exc.message,
            scope=scope,
        )
        return error_response(
            message=exc.message,
            code=exc.status_code,
            error_code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("文档问答失败", context={"error": str(exc)})
        qa_metrics.record_qa(
            qa_type="docqa",
            success=False,
            citation_count=0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=str(exc),
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="docqa",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            status="failed",
            error=str(exc),
            scope=scope,
        )
        return error_response(message="问答失败", code=500)


def handle_deep_research(
    *,
    payload: DeepResearchRequest,
    request: Request,
    db: Session,
    operator_id: Optional[int],
    scope: SearchTarget,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    try:
        result = doc_qa_service.deep_research(
            payload.question,
            top_k=payload.top_k,
            max_sub_questions=payload.max_sub_questions,
            reasoning_profile=payload.reasoning_profile,
            kb_ids=scope.kb_ids,
            document_ids=scope.document_ids,
        )
        if isinstance(result.get("trace"), dict):
            generation = result["trace"].setdefault("generation", {})
            generation["reasoning_profile"] = _resolve_reasoning_profile(payload.reasoning_profile, "deep_research")
        citations = _build_citations(result)
        response = DeepResearchResponse(
            question=result.get("question", payload.question),
            summary=result.get("summary", ""),
            final_conclusion=result.get("final_conclusion", ""),
            report=result.get("report", ""),
            sub_questions=result.get("sub_questions", []),
            citations=citations,
            confidence=result.get("confidence", {}),
            evidence_stats=result.get("evidence_stats", {}),
        )
        logger.info(
            "文档深度调研请求",
            context={
                "question": payload.question,
                "sub_questions": len(response.sub_questions),
                "citations": len(citations),
                "kb_ids": scope.kb_ids,
            },
        )
        qa_metrics.record_qa(
            qa_type="deep_research",
            success=True,
            citation_count=len(citations),
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="deep_research",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            result=result,
            scope=scope,
        )
        return success_response(data=response.model_dump(), message="ok")
    except KnowledgeScopeError as exc:
        logger.warning(
            "深度调研作用域被拒绝",
            context={"error_code": exc.error_code, "kb_ids": scope.kb_ids},
        )
        qa_metrics.record_qa(
            qa_type="deep_research",
            success=False,
            citation_count=0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=exc.error_code,
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="deep_research",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            status="failed",
            error=exc.message,
            scope=scope,
        )
        return error_response(
            message=exc.message,
            code=exc.status_code,
            error_code=exc.error_code,
            details=exc.details,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("文档深度调研失败", context={"error": str(exc)})
        qa_metrics.record_qa(
            qa_type="deep_research",
            success=False,
            citation_count=0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=str(exc),
        )
        record_qa_trace(
            db,
            request=request,
            operator_id=operator_id,
            qa_type="deep_research",
            question=payload.question,
            top_k=payload.top_k,
            started_at=started_at,
            status="failed",
            error=str(exc),
            scope=scope,
        )
        return error_response(message="深度调研失败", code=500)


def handle_doc_qa_health(*, probe_llm: bool = False, request: Request | None = None, internal: bool = False):
    try:
        result = doc_qa_service.diagnose(probe_llm=probe_llm)
        status_value = _docqa_health_status(result)
        result["status"] = status_value
        result["checks"] = {
            "neo4j": bool((result.get("neo4j") or {}).get("ok")),
            "retrieval": bool((result.get("retrieval") or {}).get("ok")),
            "llm": bool((result.get("llm") or {}).get("ok")),
        }
        logger.info(
            "内部文档问答健康检查" if internal else "文档问答健康检查",
            context={
                "probe_llm": probe_llm,
                "status": status_value,
                "neo4j_ok": result.get("neo4j", {}).get("ok"),
                "llm_ok": result.get("llm", {}).get("ok"),
            },
        )
        return success_response(data=result, message=status_value)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "内部文档问答健康检查失败" if internal else "文档问答健康检查失败",
            context={"error": str(exc)},
        )
        return success_response(
            data={
                "status": "unhealthy",
                "probe_llm": probe_llm,
                "checks": {"neo4j": False, "retrieval": False, "llm": False},
                "error": str(exc),
            },
            message="unhealthy",
        )
