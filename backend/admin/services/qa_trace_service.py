"""
问答链路追踪服务
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import AdminQATrace
from ..schemas.qa_traces import (
    QACostModelBreakdown,
    QACostSummary,
    QACostSummaryQuery,
    QATraceCreate,
    QATraceDetail,
    QATraceItem,
    QATraceQuery,
)
from core import get_logger
from services.runtime_config import get_ai_cost_runtime_config

logger = get_logger()


def _to_json_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps({"raw": str(value)}, ensure_ascii=False)


def _from_json_text(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return default


class QATraceService:
    def create_trace(self, db: Session, payload: QATraceCreate) -> Optional[QATraceItem]:
        try:
            row = AdminQATrace(
                trace_id=payload.trace_id,
                qa_type=payload.qa_type,
                status=payload.status,
                question=payload.question[:4000],
                operator_id=payload.operator_id,
                tenant_id=payload.tenant_id,
                project_id=payload.project_id,
                kb_id=payload.kb_id,
                model=payload.model,
                top_k=payload.top_k,
                latency_ms=payload.latency_ms,
                retrieval_count=payload.retrieval_count,
                citation_count=payload.citation_count,
                answer_preview=(payload.answer_preview or "")[:2000] if payload.answer_preview else None,
                retrieval_snapshot=_to_json_text(payload.retrieval_snapshot),
                generation_snapshot=_to_json_text(payload.generation_snapshot),
                response_snapshot=_to_json_text(payload.response_snapshot),
                error_message=(payload.error_message or "")[:2000] if payload.error_message else None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return QATraceItem.model_validate(row)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("问答链路追踪写入失败", context={"error": str(exc)})
            return None

    def list_traces(self, db: Session, query: QATraceQuery) -> tuple[list[QATraceItem], int]:
        filters = []
        if query.qa_type:
            filters.append(AdminQATrace.qa_type == query.qa_type)
        if query.status:
            filters.append(AdminQATrace.status == query.status)
        if query.trace_id:
            filters.append(AdminQATrace.trace_id == query.trace_id)
        if query.operator_id:
            filters.append(AdminQATrace.operator_id == query.operator_id)
        if query.keyword:
            pattern = f"%{query.keyword}%"
            filters.append(or_(AdminQATrace.question.ilike(pattern), AdminQATrace.answer_preview.ilike(pattern)))

        base = db.query(AdminQATrace)
        if filters:
            base = base.filter(*filters)

        total = base.count()
        rows = (
            base.order_by(AdminQATrace.created_at.desc())
            .offset((query.page - 1) * query.page_size)
            .limit(query.page_size)
            .all()
        )
        return [QATraceItem.model_validate(row) for row in rows], total

    def get_trace(self, db: Session, trace_id_or_pk: str) -> Optional[QATraceDetail]:
        row = None
        if str(trace_id_or_pk).isdigit():
            row = db.query(AdminQATrace).filter(AdminQATrace.id == int(trace_id_or_pk)).first()
        if row is None:
            row = db.query(AdminQATrace).filter(AdminQATrace.trace_id == trace_id_or_pk).first()
        if row is None:
            return None

        item = QATraceDetail.model_validate(row)
        item.retrieval_snapshot = _from_json_text(row.retrieval_snapshot)
        item.generation_snapshot = _from_json_text(row.generation_snapshot)
        item.response_snapshot = _from_json_text(row.response_snapshot)
        return item

    def get_cost_summary(self, db: Session, query: QACostSummaryQuery) -> QACostSummary:
        since = datetime.utcnow() - timedelta(hours=max(1, int(query.window_hours or 24)))
        filters = [AdminQATrace.created_at >= since]
        if query.qa_type:
            filters.append(AdminQATrace.qa_type == query.qa_type)
        if query.status:
            filters.append(AdminQATrace.status == query.status)

        rows = db.query(AdminQATrace).filter(*filters).all()
        pricing = self._load_pricing(db)
        currency = pricing.get("currency", "USD")
        pricing_source = pricing.get("source", "env_or_admin_config")
        model_pricing = pricing.get("models", {})

        total_calls = len(rows)
        success_calls = sum(1 for row in rows if row.status == "success")
        failed_calls = total_calls - success_calls
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        total_cost = 0.0
        buckets: dict[tuple[str, str], dict[str, Any]] = {}

        for row in rows:
            model = str(row.model or "unknown")
            qa_type = str(row.qa_type or "unknown")
            usage = self._extract_usage_from_trace(row)
            prompt_tokens = usage["prompt_tokens"]
            completion_tokens = usage["completion_tokens"]
            tokens = usage["total_tokens"]
            cost = self._estimate_cost(model_pricing, model, prompt_tokens, completion_tokens)

            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            total_tokens += tokens
            total_cost += cost

            key = (model, qa_type)
            bucket = buckets.setdefault(
                key,
                {
                    "model": model,
                    "qa_type": qa_type,
                    "calls": 0,
                    "success_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost": 0.0,
                    "latency_ms": [],
                },
            )
            bucket["calls"] += 1
            if row.status == "success":
                bucket["success_calls"] += 1
            bucket["prompt_tokens"] += prompt_tokens
            bucket["completion_tokens"] += completion_tokens
            bucket["total_tokens"] += tokens
            bucket["estimated_cost"] += cost
            if row.latency_ms is not None:
                bucket["latency_ms"].append(max(0, int(row.latency_ms)))

        models = []
        for bucket in buckets.values():
            latencies = bucket["latency_ms"]
            calls = max(1, int(bucket["calls"]))
            models.append(
                QACostModelBreakdown(
                    model=bucket["model"],
                    qa_type=bucket["qa_type"],
                    calls=bucket["calls"],
                    prompt_tokens=bucket["prompt_tokens"],
                    completion_tokens=bucket["completion_tokens"],
                    total_tokens=bucket["total_tokens"],
                    estimated_cost=round(bucket["estimated_cost"], 6),
                    avg_latency_ms=round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
                    success_rate=round(bucket["success_calls"] / calls, 6),
                )
            )

        models.sort(key=lambda item: (item.estimated_cost, item.total_tokens, item.calls), reverse=True)
        return QACostSummary(
            window_hours=query.window_hours,
            total_calls=total_calls,
            success_calls=success_calls,
            failed_calls=failed_calls,
            success_rate=round(success_calls / total_calls, 6) if total_calls else 0.0,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            total_tokens=total_tokens,
            estimated_cost=round(total_cost, 6),
            currency=currency,
            pricing_source=pricing_source,
            models=models,
        )

    def _load_pricing(self, db: Session) -> dict[str, Any]:
        pricing_config = get_ai_cost_runtime_config()
        raw = pricing_config.get("model_pricing_json") or os.getenv("AI_COST_MODEL_PRICING_JSON", "")
        currency = pricing_config.get("currency") or os.getenv("AI_COST_CURRENCY", "USD")
        if not raw:
            return {"currency": currency, "source": "not_configured", "models": {}}
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("AI cost pricing config is not valid JSON")
            return {"currency": currency, "source": "invalid_config", "models": {}}

        if not isinstance(parsed, dict):
            return {"currency": currency, "source": "invalid_config", "models": {}}
        models = parsed.get("models") if isinstance(parsed.get("models"), dict) else parsed
        return {
            "currency": str(parsed.get("currency") or currency),
            "source": "admin_config_or_env",
            "models": models if isinstance(models, dict) else {},
        }

    @staticmethod
    def _extract_usage_from_trace(row: AdminQATrace) -> dict[str, int]:
        generation = _from_json_text(row.generation_snapshot)
        usage = generation.get("usage") if isinstance(generation, dict) else None
        if not isinstance(usage, dict):
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _estimate_cost(
        model_pricing: dict[str, Any],
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        price = model_pricing.get(model) or model_pricing.get("*") or {}
        if not isinstance(price, dict):
            return 0.0
        prompt_per_1k = _safe_float(price.get("prompt_per_1k") or price.get("input_per_1k"))
        completion_per_1k = _safe_float(price.get("completion_per_1k") or price.get("output_per_1k"))
        return (prompt_tokens / 1000.0 * prompt_per_1k) + (completion_tokens / 1000.0 * completion_per_1k)


qa_trace_service = QATraceService()
