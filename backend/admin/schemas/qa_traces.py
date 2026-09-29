"""
问答链路追踪 schema
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class QATraceCreate(BaseModel):
    trace_id: Optional[str] = None
    qa_type: str = Field(..., max_length=50)
    status: str = Field(default="success", max_length=20)
    question: str
    operator_id: Optional[int] = None
    # M4：trace 记录作用域（列自 M1 迁移起存在）
    tenant_id: Optional[str] = Field(default=None, max_length=100)
    project_id: Optional[str] = Field(default=None, max_length=100)
    kb_id: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = None
    top_k: Optional[int] = None
    latency_ms: Optional[int] = None
    retrieval_count: int = 0
    citation_count: int = 0
    answer_preview: Optional[str] = None
    retrieval_snapshot: Optional[dict[str, Any] | list[Any]] = None
    generation_snapshot: Optional[dict[str, Any] | list[Any]] = None
    response_snapshot: Optional[dict[str, Any] | list[Any]] = None
    error_message: Optional[str] = None


class QATraceItem(BaseModel):
    id: int
    trace_id: Optional[str] = None
    qa_type: str
    status: str
    question: str
    operator_id: Optional[int] = None
    model: Optional[str] = None
    top_k: Optional[int] = None
    latency_ms: Optional[int] = None
    retrieval_count: int
    citation_count: int
    answer_preview: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class QATraceDetail(QATraceItem):
    retrieval_snapshot: Optional[Any] = None
    generation_snapshot: Optional[Any] = None
    response_snapshot: Optional[Any] = None


class QATraceQuery(BaseModel):
    qa_type: Optional[str] = None
    status: Optional[str] = None
    trace_id: Optional[str] = None
    operator_id: Optional[int] = None
    keyword: Optional[str] = None
    page: int = 1
    page_size: int = 20


class QACostSummaryQuery(BaseModel):
    qa_type: Optional[str] = None
    status: Optional[str] = None
    window_hours: int = Field(default=24, ge=1, le=24 * 90)


class QACostModelBreakdown(BaseModel):
    model: str
    qa_type: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    avg_latency_ms: float
    success_rate: float


class QACostSummary(BaseModel):
    window_hours: int
    total_calls: int
    success_calls: int
    failed_calls: int
    success_rate: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    currency: str
    pricing_source: str
    models: list[QACostModelBreakdown]
