"""
任务中心相关 Pydantic 模型
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

# reindex_chunks（M5-B0 worker / §8.2）必须在此登记：JobItem.job_type 用它做校验，
# 缺字面量时任务中心 list/get 一读到 reindex_chunks 行就抛 ValidationError，整个列表页 500。
JobType = Literal["build_graph", "clear_kb", "reindex", "reindex_chunks"]
JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class JobCreateRequest(BaseModel):
    tenant_id: Optional[str] = Field(default=None, max_length=100)
    project_id: Optional[str] = Field(default=None, max_length=100)
    kb_id: Optional[str] = Field(default=None, max_length=100)
    payload: Dict[str, Any] = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0, le=20)


class JobItem(BaseModel):
    id: int
    job_type: JobType
    status: JobStatus
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    kb_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    requested_by: Optional[int] = None
    trace_id: Optional[str] = None
    # §16.3 去重键：运维要能看出两条 job 是"同一批 targets 复用"还是"另一次提交"。
    targets_hash: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class JobQuery(BaseModel):
    job_type: Optional[JobType] = None
    status: Optional[JobStatus] = None
    tenant_id: Optional[str] = None
    project_id: Optional[str] = None
    kb_id: Optional[str] = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=200)
