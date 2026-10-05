"""
任务中心服务
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from services.chunk_projection_state import aggregate_document_states, write_back_job_failure
from services.job_runtime import execute_job
from services.reindex_queue import (
    JOB_TYPE as REINDEX_CHUNKS_JOB_TYPE,
    OUTCOME_ENQUEUED,
    OUTCOME_REJECTED,
    REJECT_REASON_RETRY_EXHAUSTED,
    enqueue_on_connection,
    targets_from_payload,
)
from services.scope_contract import normalize_scope_id
from ..crud import log_crud
from ..database import SessionLocal
from ..models import AdminJob, AdminLog, KnowledgeBase
from ..schemas.jobs import JobCreateRequest, JobItem, JobQuery
from ..schemas.logs import LogCreate
from core import BusinessException, NotFoundException, ValidationException, get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError

logger = get_logger()

JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

ALLOWED_RETRY_FROM = {JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
ALLOWED_CANCEL_FROM = {JOB_STATUS_PENDING, JOB_STATUS_RUNNING}
SUPPORTED_JOB_TYPES = {"build_graph", "clear_kb", "reindex", "reindex_chunks"}
RUNNABLE_JOB_TYPES = {"build_graph", "clear_kb", "reindex", "reindex_chunks"}
# 知识数据类任务：创建时必须携带 kb_id 且 KB 必须存在且为 active；
# reindex 只重建 Neo4j 全文索引（基础设施操作），kb_id 可选。
# reindex_chunks 是 chunk 投影重建（§8.2），targets 必须限定在单个 kb 内 → 归知识数据类。
KB_SCOPED_JOB_TYPES = {"build_graph", "clear_kb", "reindex_chunks"}


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(value, minimum)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


JOB_EXECUTION_TIMEOUT_SECONDS = _env_int("JOB_EXECUTION_TIMEOUT_SECONDS", 600, 30)
JOB_HEARTBEAT_INTERVAL_SECONDS = _env_int("JOB_HEARTBEAT_INTERVAL_SECONDS", 10, 3)
JOB_AUTO_RETRY_ENABLED = _env_bool("JOB_AUTO_RETRY_ENABLED", True)
JOB_AUTO_RETRY_BASE_DELAY_SECONDS = _env_int("JOB_AUTO_RETRY_BASE_DELAY_SECONDS", 10, 1)
JOB_AUTO_RETRY_MAX_DELAY_SECONDS = _env_int("JOB_AUTO_RETRY_MAX_DELAY_SECONDS", 300, 5)
JOB_WORKER_ENABLED = _env_bool("JOB_WORKER_ENABLED", True)
JOB_WORKER_POLL_INTERVAL_SECONDS = _env_int("JOB_WORKER_POLL_INTERVAL_SECONDS", 2, 1)
JOB_WORKER_STOP_TIMEOUT_SECONDS = _env_int("JOB_WORKER_STOP_TIMEOUT_SECONDS", 5, 1)
JOB_WORKER_LEASE_SECONDS = _env_int("JOB_WORKER_LEASE_SECONDS", 30, 5)


class JobExecutionTimeoutError(TimeoutError):
    """任务执行超时"""


def _to_json_text(payload: Optional[dict]) -> str:
    if payload is None:
        return "{}"
    return json.dumps(payload, ensure_ascii=False)


def _parse_json_text(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    try:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {"raw": loaded}
    except Exception:
        return {"raw": value}


def _to_item(job: AdminJob) -> JobItem:
    return JobItem(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        tenant_id=job.tenant_id,
        project_id=job.project_id,
        kb_id=job.kb_id,
        payload=_parse_json_text(job.payload) or {},
        result=_parse_json_text(job.result),
        error_message=job.error_message,
        retry_count=job.retry_count or 0,
        max_retries=job.max_retries or 0,
        requested_by=job.requested_by,
        trace_id=job.trace_id,
        targets_hash=job.targets_hash,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class JobService:
    def __init__(self) -> None:
        self._worker_lock = threading.Lock()
        self._worker_stop_event = threading.Event()
        self._worker_wake_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_id = self._build_worker_id()

    def _build_worker_id(self) -> str:
        host = socket.gethostname().strip() or "localhost"
        pid = os.getpid()
        suffix = uuid.uuid4().hex[:8]
        return f"py-job-worker:{host}:{pid}:{suffix}"

    def _validate_kb_usable(self, db: Session, kb_id: str) -> None:
        """任务创建时的 KB 校验：必须存在（KB_NOT_FOUND/404）且为 active（KB_ARCHIVED/409）。"""
        row = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        if row is None:
            raise KnowledgeScopeError(
                ErrorCode.KB_NOT_FOUND,
                message=f"知识库不存在: {kb_id}",
                details={"kb_id": kb_id},
            )
        status = str(row.status or "").strip().lower()
        if status != "active":
            raise KnowledgeScopeError(
                ErrorCode.KB_ARCHIVED,
                message=f"知识库当前状态禁止创建该任务: {status}",
                details={"kb_id": kb_id, "status": status},
            )

    def _resolve_job_scope(
        self,
        db: Session,
        *,
        job_type: str,
        request: JobCreateRequest,
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        """解析并冻结任务作用域：payload 优先，缺失时回填请求字段。

        返回 (normalized_scope, frozen_payload)。知识数据类任务缺 kb_id → KB_SCOPE_REQUIRED；
        kb_id 存在时校验 KB 存在且 active。
        """
        payload = dict(request.payload or {})
        scope: Dict[str, str] = {}
        for field in ("kb_id", "tenant_id", "project_id"):
            raw = payload.get(field) or getattr(request, field)
            scope[field] = normalize_scope_id(field, raw) or ""

        if job_type in KB_SCOPED_JOB_TYPES and not scope["kb_id"]:
            raise ValidationException(
                "任务缺少 kb_id 作用域",
                error_code=ErrorCode.KB_SCOPE_REQUIRED,
                details={"job_type": job_type},
            )
        if scope["kb_id"]:
            self._validate_kb_usable(db, scope["kb_id"])

        # 冻结作用域进 payload：worker 只消费 payload 中固化的 scope（手册 §13.2）
        for field, value in scope.items():
            if value:
                payload[field] = value
        return scope, payload

    def create_job(
        self,
        db: Session,
        *,
        job_type: str,
        request: JobCreateRequest,
        requested_by: Optional[int],
        trace_id: Optional[str] = None,
    ) -> JobItem:
        if job_type not in SUPPORTED_JOB_TYPES:
            raise ValidationException(f"不支持的任务类型: {job_type}")
        # reindex_chunks 走 §16.3 共享去重入队，且必须留在兜底 except 之外——
        # 落进 `except Exception: raise BusinessException("创建任务失败")` 会把
        # 超限拒绝的结构化 details（job_id/targets_hash/reason）压成一句空话。
        if job_type == REINDEX_CHUNKS_JOB_TYPE:
            return self._create_reindex_chunks_job(
                db, request=request, requested_by=requested_by, trace_id=trace_id
            )
        try:
            scope, payload = self._resolve_job_scope(db, job_type=job_type, request=request)
            job = AdminJob(
                job_type=job_type,
                status=JOB_STATUS_PENDING,
                tenant_id=scope["tenant_id"] or None,
                project_id=scope["project_id"] or None,
                kb_id=scope["kb_id"] or None,
                payload=_to_json_text(payload),
                retry_count=0,
                max_retries=request.max_retries,
                requested_by=requested_by,
                trace_id=trace_id,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            self._write_job_log(
                db,
                job=job,
                action="job_created",
                details={
                    "job_type": job.job_type,
                    "status": job.status,
                    "max_retries": job.max_retries,
                    "kb_id": job.kb_id,
                    "tenant_id": job.tenant_id,
                    "project_id": job.project_id,
                },
            )
            return _to_item(job)
        except (ValidationException, KnowledgeScopeError):
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error(f"创建任务失败: {exc}", exc_info=True)
            raise BusinessException("创建任务失败")

    def _create_reindex_chunks_job(
        self,
        db: Session,
        *,
        request: JobCreateRequest,
        requested_by: Optional[int],
        trace_id: Optional[str],
    ) -> JobItem:
        """§16.3 提交路径：reindex_chunks 走共享去重入队，不走裸 INSERT。

        裸 INSERT 会让同 hash 的重复提交各建一行 pending，worker 于是把同一批 targets
        重建多次；去重/复用/原地 retry/超限拒绝只在 services.reindex_queue 实现一次。
        入队必须落在 API session 的同一事务里（`db.connection()`），否则提交失败时
        `db.rollback()` 只能回滚一半，留下孤零零的 pending job。

        超限拒绝用 3xxx `OPERATION_NOT_ALLOWED` + 结构化 details 表达。§16.3 文里写的
        HTTP 409 映射在 Go 控制面并不存在（NOT-IMPLEMENTED）：reindex_chunks 的 POST 在
        **路由分派**处就拿到 404 `NOT_FOUND`——`admin_jobs_native.go:228-229` 只登记
        build-graph/clear-kb/reindex，未登记路径经 `adminJobTypeFromPath`
        （`admin_jobs_native.go:686-697`）返回空串并落到 `admin_jobs_native.go:278` 的
        default 分支，`CreateJob` 不被调用。store 层白名单 `supportedJobTypes`
        （`jobs.go:26-30`）+ `validateJobCreateRequest`（`jobs.go:592-605`）才是第二道线，
        命中时由 `admin_jobs_native.go:758-761` 映射为 400 `INVALID_BODY`。
        Python 侧不新增错误码。
        """
        scope, payload = self._resolve_job_scope(db, job_type=REINDEX_CHUNKS_JOB_TYPE, request=request)
        try:
            targets = targets_from_payload(payload)
        except ValueError as exc:
            db.rollback()
            raise ValidationException(
                str(exc),
                error_code=ErrorCode.REINDEX_SCOPE_REQUIRED,
                details={"job_type": REINDEX_CHUNKS_JOB_TYPE, "kb_id": scope["kb_id"]},
            ) from exc

        report = enqueue_on_connection(
            db.connection(),
            targets,
            source="admin_api",
            trace_id=trace_id or "",
            max_retries=request.max_retries,
        )
        entry = (report.get("jobs") or [{}])[0]
        job_id = entry.get("job_id")
        outcome = entry.get("outcome")
        detail = (report.get("rejected_detail") or [{}])[0]

        if outcome == OUTCOME_REJECTED:
            db.rollback()
            self._audit_reindex_rejected(db, job_id=job_id, requested_by=requested_by, detail=detail)
            raise BusinessException(
                "reindex_chunks 重试额度已用尽，需人工介入后再提交",
                error_code=ErrorCode.OPERATION_NOT_ALLOWED,
                details={
                    "job_id": job_id,
                    "kb_id": scope["kb_id"],
                    "targets_hash": detail.get("targets_hash"),
                    "reason": REJECT_REASON_RETRY_EXHAUSTED,
                    "retry_count": detail.get("retry_count"),
                    "max_retries": detail.get("max_retries"),
                },
            )
        if job_id is None:
            # 唯一索引拦住新增却读不到既有行（§16.3 分支表外的异常）：不猜、不补建。
            db.rollback()
            raise BusinessException(
                "reindex_chunks 入队异常：命中去重索引但读不到既有任务行",
                error_code=ErrorCode.OPERATION_FAILED,
                details={"kb_id": scope["kb_id"], "targets_hash": entry.get("targets_hash")},
            )

        db.commit()
        job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
        if job is None:
            raise BusinessException(
                "reindex_chunks 入队后任务行读取失败",
                error_code=ErrorCode.OPERATION_FAILED,
                details={"job_id": job_id, "kb_id": scope["kb_id"]},
            )
        if requested_by is not None and job.requested_by is None:
            # 共享入队的裸 INSERT 不含 requested_by，这里补登记（审计要能追到人）。
            job.requested_by = requested_by
            db.commit()
            db.refresh(job)
        self._write_job_log(
            db,
            job=job,
            action="job_created" if outcome == OUTCOME_ENQUEUED else "job_reused",
            details={
                "job_type": job.job_type,
                "status": job.status,
                "outcome": outcome,
                "targets_hash": entry.get("targets_hash"),
                "target_count": entry.get("target_count"),
                "kb_id": job.kb_id,
                "enqueued": report.get("enqueued"),
                "reused": report.get("reused"),
                "retried": report.get("retried"),
                "reset": report.get("reset"),
            },
        )
        return _to_item(job)

    def _audit_reindex_rejected(
        self, db: Session, *, job_id: Optional[int], requested_by: Optional[int], detail: Dict[str, Any]
    ) -> None:
        """超限拒绝写审计 `kb_chunk_reindex_failed`（§16.3），取既有行做上下文。"""
        job = db.query(AdminJob).filter(AdminJob.id == job_id).first() if job_id else None
        if job is None:
            logger.warning("reindex 超限拒绝审计跳过：任务行不存在", context={"job_id": job_id})
            return
        if requested_by is not None and job.requested_by is None:
            job.requested_by = requested_by
            db.commit()
            db.refresh(job)
        self._write_job_log(
            db,
            job=job,
            action="kb_chunk_reindex_failed",
            status_value="failed",
            details={
                "job_type": job.job_type,
                "kb_id": job.kb_id,
                "reason": REJECT_REASON_RETRY_EXHAUSTED,
                "targets_hash": detail.get("targets_hash"),
                "retry_count": detail.get("retry_count"),
                "max_retries": detail.get("max_retries"),
                "chunk_ids": detail.get("chunk_ids"),
                "submit_source": "admin_api",
            },
            error_message="reindex_chunks 重试额度已用尽，拒绝再次入队（§16.3）",
        )

    def _write_back_terminal_failure(
        self, db: Session, *, job: AdminJob, error_code: Optional[str]
    ) -> None:
        """作业（父）终态 failed 且不再自动重试 → 投影（子）落 failed + 文档级聚合（§15.5）。

        只对 reindex_chunks 生效：worker 正常收敛路径自己已逐 chunk 落过 failed 并聚合过
        文档态，这里补的是 worker 没机会写完的场景（超时、崩溃、重试额度用尽的终态失败）。
        build_graph 的失败转交在 GI-9c 已落成 per-chunk failed + 自动入队，不在这里重复降级。

        §8.5：`INDEX_UNAVAILABLE`（collection 缺显式 content_revision 字段）属"没能力写"
        而不是"写失败"，vector 侧保持 pending 等 v3 迁移，因此整列不动。
        """
        if job.job_type != REINDEX_CHUNKS_JOB_TYPE:
            return
        payload = _parse_json_text(job.payload) or {}
        kb_id = str(payload.get("kb_id") or job.kb_id or "").strip()
        if not kb_id:
            logger.warning("作业终态父子回写缺少 kb_id，跳过", context={"job_id": job.id})
            return
        try:
            targets = targets_from_payload(payload)
        except ValueError as exc:
            logger.warning(
                "作业终态父子回写 payload 非法，跳过",
                context={"job_id": job.id, "error": str(exc)},
            )
            return
        keep_vector_untouched = error_code == ErrorCode.INDEX_UNAVAILABLE
        try:
            report = write_back_job_failure(
                kb_id, targets, keep_vector_untouched=keep_vector_untouched
            )
            document_states = (
                aggregate_document_states(kb_id, report["doc_ids"]) if report["doc_ids"] else []
            )
        except Exception as wb_exc:  # noqa: BLE001
            logger.error(
                "作业终态父子回写失败",
                context={"job_id": job.id, "kb_id": kb_id, "error": str(wb_exc)},
                exc_info=True,
            )
            return
        self._write_job_log(
            db,
            job=job,
            action="kb_chunk_reindex_failed",
            status_value="failed",
            details={
                "job_type": job.job_type,
                "kb_id": kb_id,
                "targets_hash": job.targets_hash,
                "targets": report["targets"],
                "updated": report["updated"],
                "current_moved": report["current_moved"],
                "keep_vector_untouched": keep_vector_untouched,
                "document_states": document_states[:20],
            },
            error_message=str(job.error_message or "")[:1000],
        )

    def should_auto_run(self, item: JobItem) -> bool:
        return item.job_type in RUNNABLE_JOB_TYPES and item.status == JOB_STATUS_PENDING

    def start_background_worker(self) -> None:
        if not JOB_WORKER_ENABLED:
            logger.info("后台任务 worker 已禁用", context={"enabled": False})
            return

        with self._worker_lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._worker_stop_event = threading.Event()
            self._worker_wake_event = threading.Event()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                args=(self._worker_stop_event, self._worker_wake_event),
                daemon=True,
                name="admin-job-worker",
            )
            self._worker_thread.start()
        logger.info(
            "后台任务 worker 已启动",
            context={
                "enabled": True,
                "poll_interval_seconds": JOB_WORKER_POLL_INTERVAL_SECONDS,
            },
        )

    def stop_background_worker(self) -> None:
        with self._worker_lock:
            thread = self._worker_thread
            stop_event = self._worker_stop_event
            wake_event = self._worker_wake_event
            self._worker_thread = None
        if not thread:
            return
        stop_event.set()
        wake_event.set()
        thread.join(timeout=JOB_WORKER_STOP_TIMEOUT_SECONDS)
        logger.info("后台任务 worker 已停止")

    def wake_worker(self) -> bool:
        if JOB_WORKER_ENABLED:
            self.start_background_worker()
            with self._worker_lock:
                thread = self._worker_thread
                wake_event = self._worker_wake_event
            if thread and thread.is_alive():
                wake_event.set()
                return True

        thread = threading.Thread(
            target=self.run_next_pending_job_once,
            daemon=True,
            name="admin-job-worker-wake-once",
        )
        thread.start()
        return True

    def _worker_loop(self, stop_event: threading.Event, wake_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                recovered = self.recover_stale_running_jobs_once()
                processed = self.run_next_pending_job_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("后台任务 worker 轮询失败", context={"error": str(exc)}, exc_info=True)
                recovered = 0
                processed = False
            if recovered > 0 or processed:
                continue
            wake_event.wait(JOB_WORKER_POLL_INTERVAL_SECONDS)
            wake_event.clear()

    def recover_stale_running_jobs_once(self) -> int:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            rows = (
                db.query(AdminJob)
                .filter(
                    AdminJob.status == JOB_STATUS_RUNNING,
                    AdminJob.claim_expires_at.is_not(None),
                    AdminJob.claim_expires_at < now,
                )
                .order_by(AdminJob.id.asc())
                .limit(20)
                .all()
            )
            if not rows:
                return 0

            recovered = 0
            for job in rows:
                previous_claimed_by = job.claimed_by
                job.status = JOB_STATUS_PENDING
                job.started_at = None
                job.finished_at = None
                job.claimed_by = None
                job.claim_expires_at = None
                job.last_heartbeat_at = None
                job.error_message = (
                    f"worker lease expired and job was re-queued at {now.isoformat()}Z"
                )
                db.flush()
                self._write_job_log(
                    db,
                    job=job,
                    action="job_requeued_stale_lease",
                    status_value="success",
                    details={
                        "job_type": job.job_type,
                        "previous_claimed_by": previous_claimed_by,
                        "recovered_at": now.isoformat() + "Z",
                    },
                )
                recovered += 1

            db.commit()
            if recovered > 0:
                logger.warning("检测到过期运行任务并已重新入队", context={"recovered_jobs": recovered})
            return recovered
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def run_next_pending_job_once(self) -> bool:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            row = (
                db.query(AdminJob.id)
                .filter(
                    AdminJob.status == JOB_STATUS_PENDING,
                    AdminJob.job_type.in_(tuple(RUNNABLE_JOB_TYPES)),
                    ((AdminJob.claim_expires_at.is_(None)) | (AdminJob.claim_expires_at < now)),
                )
                .order_by(AdminJob.created_at.asc(), AdminJob.id.asc())
                .first()
            )
            if not row:
                return False
            job_id = int(row[0])
        finally:
            db.close()

        self.run_job(job_id)
        return True

    def _write_job_log(
        self,
        db: Session,
        *,
        job: AdminJob,
        action: str,
        status_value: str = "success",
        details: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        try:
            log_crud.create(
                db,
                LogCreate(
                    user_id=job.requested_by,
                    operator_id=job.requested_by,
                    tenant_id=job.tenant_id,
                    trace_id=job.trace_id,
                    action=action,
                    resource="job",
                    resource_id=str(job.id),
                    details=details or {},
                    status=status_value,
                    error_message=error_message,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "写入任务审计日志失败",
                context={"job_id": job.id, "action": action, "error": str(exc)},
            )

    def _compute_backoff_delay(self, retry_attempt: int) -> int:
        raw = JOB_AUTO_RETRY_BASE_DELAY_SECONDS * (2 ** max(retry_attempt - 1, 0))
        return min(raw, JOB_AUTO_RETRY_MAX_DELAY_SECONDS)

    def _schedule_retry(self, job_id: int, retry_attempt: int, delay_seconds: int) -> None:
        def _runner() -> None:
            if delay_seconds > 0:
                time.sleep(delay_seconds)

            db = SessionLocal()
            try:
                row = db.query(AdminJob).filter(AdminJob.id == job_id).first()
                if not row:
                    return
                if row.status != JOB_STATUS_FAILED:
                    logger.info(
                        "自动重试触发时任务状态已变化，跳过",
                        context={"job_id": job_id, "status": row.status},
                    )
                    return
                if (row.retry_count or 0) != retry_attempt:
                    logger.info(
                        "自动重试触发时任务重试次数已变化，跳过",
                        context={"job_id": job_id, "retry_count": row.retry_count, "expected": retry_attempt},
                    )
                    return
                row.status = JOB_STATUS_PENDING
                row.started_at = None
                row.finished_at = None
                row.error_message = None
                row.result = None
                row.claimed_by = None
                row.claim_expires_at = None
                row.last_heartbeat_at = None
                db.commit()
                self._write_job_log(
                    db,
                    job=row,
                    action="job_retry_queued",
                    details={
                        "job_type": row.job_type,
                        "retry_attempt": retry_attempt,
                        "delay_seconds": delay_seconds,
                    },
                )
            finally:
                db.close()

            if JOB_WORKER_ENABLED:
                self.wake_worker()
                return
            self.run_job(job_id)

        thread = threading.Thread(target=_runner, daemon=True, name=f"admin-job-retry-{job_id}")
        thread.start()

    def run_job(self, job_id: int) -> None:
        db = SessionLocal()
        started_monotonic: float | None = None
        try:
            now = datetime.utcnow()
            lease_expires_at = now + timedelta(seconds=JOB_WORKER_LEASE_SECONDS)
            claimed = (
                db.query(AdminJob)
                .filter(
                    AdminJob.id == job_id,
                    AdminJob.status == JOB_STATUS_PENDING,
                    ((AdminJob.claim_expires_at.is_(None)) | (AdminJob.claim_expires_at < now)),
                )
                .update(
                    {
                        AdminJob.status: JOB_STATUS_RUNNING,
                        AdminJob.claimed_by: self._worker_id,
                        AdminJob.claim_expires_at: lease_expires_at,
                        AdminJob.last_heartbeat_at: now,
                        AdminJob.started_at: datetime.utcnow(),
                        AdminJob.finished_at: None,
                        AdminJob.error_message: None,
                    },
                    synchronize_session=False,
                )
            )
            if claimed != 1:
                db.rollback()
                job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
                if not job:
                    logger.warning("任务不存在，忽略执行", context={"job_id": job_id})
                    return
                logger.info("任务状态非待执行，忽略调度", context={"job_id": job_id, "status": job.status})
                return
            db.commit()

            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            if not job:
                logger.warning("任务不存在，忽略执行", context={"job_id": job_id})
                return
            self._write_job_log(
                db,
                job=job,
                action="job_started",
                details={"job_type": job.job_type, "status": job.status, "claimed_by": self._worker_id},
            )
            started_monotonic = time.monotonic()

            result = self._execute_job_logic_with_guardrails(
                job_id=job.id,
                job_type=job.job_type,
                payload_text=job.payload,
            )
            duration_seconds = round(time.monotonic() - started_monotonic, 3)
            result.setdefault("runtime", {})
            result["runtime"].update(
                {
                    "duration_seconds": duration_seconds,
                    "timeout_seconds": JOB_EXECUTION_TIMEOUT_SECONDS,
                    "heartbeat_interval_seconds": JOB_HEARTBEAT_INTERVAL_SECONDS,
                }
            )

            db.expire_all()
            latest = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            if not latest:
                logger.warning("任务执行完成但记录已丢失", context={"job_id": job_id})
                return
            if latest.status == JOB_STATUS_CANCELLED:
                logger.info("任务已被取消，跳过成功状态回写", context={"job_id": job_id})
                return

            latest.status = JOB_STATUS_SUCCEEDED
            latest.result = _to_json_text(result)
            latest.error_message = None
            latest.claimed_by = None
            latest.claim_expires_at = None
            latest.last_heartbeat_at = datetime.utcnow()
            latest.finished_at = datetime.utcnow()
            db.commit()
            self._write_job_log(
                db,
                job=latest,
                action="job_succeeded",
                details={
                    "job_type": latest.job_type,
                    "status": latest.status,
                    "runtime": result.get("runtime"),
                },
            )
        except Exception as exc:
            db.rollback()
            duration_seconds: float | None = None
            if started_monotonic is not None:
                duration_seconds = round(time.monotonic() - started_monotonic, 3)
            error_type = type(exc).__name__
            error_message = f"{error_type}: {str(exc)}"
            error_details = getattr(exc, "details", None)
            logger.error("后台任务执行失败", context={"job_id": job_id, "error": error_message}, exc_info=True)
            try:
                failed = db.query(AdminJob).filter(AdminJob.id == job_id).first()
                if failed and failed.status != JOB_STATUS_CANCELLED:
                    failed.status = JOB_STATUS_FAILED
                    failed.error_message = error_message[:2000]
                    failed.claimed_by = None
                    failed.claim_expires_at = None
                    failed.last_heartbeat_at = datetime.utcnow()
                    failed.result = _to_json_text(
                        {
                            "job_id": job_id,
                            "error_type": error_type,
                            "error": str(exc),
                            "details": error_details,
                            "runtime": {
                                "duration_seconds": duration_seconds,
                                "timeout_seconds": JOB_EXECUTION_TIMEOUT_SECONDS,
                                "heartbeat_interval_seconds": JOB_HEARTBEAT_INTERVAL_SECONDS,
                            },
                        }
                    )
                    failed.finished_at = datetime.utcnow()
                    db.commit()
                    self._write_job_log(
                        db,
                        job=failed,
                        action="job_failed",
                        status_value="failed",
                        details={
                            "job_type": failed.job_type,
                            "status": failed.status,
                            "error_type": error_type,
                            "runtime_seconds": duration_seconds,
                            "error_details": error_details,
                        },
                        error_message=error_message[:1000],
                    )

                    retry_scheduled = False
                    if (
                        JOB_AUTO_RETRY_ENABLED
                        and failed.job_type in RUNNABLE_JOB_TYPES
                        and error_type != "ValidationException"
                        and (failed.retry_count or 0) < (failed.max_retries or 0)
                    ):
                        retry_attempt = (failed.retry_count or 0) + 1
                        delay_seconds = self._compute_backoff_delay(retry_attempt)
                        failed.retry_count = retry_attempt
                        failed.error_message = (
                            f"{error_message[:800]} | 已计划自动重试({retry_attempt}/{failed.max_retries})，"
                            f"{delay_seconds}s 后执行"
                        )
                        db.commit()
                        self._write_job_log(
                            db,
                            job=failed,
                            action="job_retry_scheduled",
                            details={
                                "job_type": failed.job_type,
                                "retry_attempt": retry_attempt,
                                "max_retries": failed.max_retries,
                                "delay_seconds": delay_seconds,
                                "reason": error_type,
                            },
                        )
                        self._schedule_retry(job_id, retry_attempt, delay_seconds)
                        retry_scheduled = True
                    if not retry_scheduled:
                        self._write_back_terminal_failure(
                            db, job=failed, error_code=getattr(exc, "error_code", None)
                        )
            except Exception as update_exc:  # noqa: BLE001
                db.rollback()
                logger.error(
                    "回写任务失败状态异常",
                    context={"job_id": job_id, "error": str(update_exc)},
                    exc_info=True,
                )
        finally:
            db.close()

    def _heartbeat_loop(self, job_id: int, stop_event: threading.Event) -> None:
        while not stop_event.wait(JOB_HEARTBEAT_INTERVAL_SECONDS):
            hb_db = SessionLocal()
            try:
                row = hb_db.query(AdminJob).filter(AdminJob.id == job_id).first()
                if not row or row.status != JOB_STATUS_RUNNING:
                    return
                row.updated_at = datetime.utcnow()
                row.last_heartbeat_at = datetime.utcnow()
                row.claim_expires_at = datetime.utcnow() + timedelta(seconds=JOB_WORKER_LEASE_SECONDS)
                hb_db.commit()
            except Exception as exc:  # noqa: BLE001
                hb_db.rollback()
                logger.warning("任务心跳更新失败", context={"job_id": job_id, "error": str(exc)})
            finally:
                hb_db.close()

    def _execute_job_logic_with_guardrails(
        self,
        *,
        job_id: int,
        job_type: str,
        payload_text: Optional[str],
    ) -> Dict[str, Any]:
        payload = _parse_json_text(payload_text) or {}
        stop_event = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, stop_event),
            daemon=True,
            name=f"admin-job-heartbeat-{job_id}",
        )
        heartbeat_thread.start()

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"admin-job-{job_id}")
        future = executor.submit(
            self._execute_job_logic,
            job_id=job_id,
            job_type=job_type,
            payload=payload,
        )
        try:
            return future.result(timeout=JOB_EXECUTION_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            raise JobExecutionTimeoutError(f"任务执行超时（>{JOB_EXECUTION_TIMEOUT_SECONDS}s）") from exc
        finally:
            stop_event.set()
            heartbeat_thread.join(timeout=1)
            executor.shutdown(wait=False, cancel_futures=True)

    def _execute_job_logic(self, *, job_id: int, job_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return execute_job(job_id=job_id, job_type=job_type, payload=payload)

    def get_job_logs(
        self,
        db: Session,
        job_id: int,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        try:
            exists = db.query(AdminJob.id).filter(AdminJob.id == job_id).first()
            if not exists:
                raise NotFoundException("任务不存在")

            base_query = (
                db.query(AdminLog)
                .filter(AdminLog.resource == "job", AdminLog.resource_id == str(job_id))
                .order_by(AdminLog.created_at.desc())
            )
            total = base_query.count()
            offset = (page - 1) * page_size
            rows = base_query.offset(offset).limit(page_size).all()

            items: List[Dict[str, Any]] = []
            for row in rows:
                parsed_details = _parse_json_text(row.details)
                items.append(
                    {
                        "id": row.id,
                        "action": row.action,
                        "status": row.status,
                        "error_message": row.error_message,
                        "trace_id": row.trace_id,
                        "operator_id": row.operator_id,
                        "created_at": row.created_at,
                        "details": parsed_details,
                    }
                )
            return items, total
        except NotFoundException:
            raise
        except Exception as exc:
            logger.error(f"查询任务日志失败: {exc}", exc_info=True)
            raise BusinessException("查询任务日志失败")

    def list_jobs(self, db: Session, query: JobQuery) -> Tuple[List[JobItem], int]:
        try:
            db_query = db.query(AdminJob)
            if query.job_type:
                db_query = db_query.filter(AdminJob.job_type == query.job_type)
            if query.status:
                db_query = db_query.filter(AdminJob.status == query.status)
            if query.tenant_id:
                db_query = db_query.filter(AdminJob.tenant_id == query.tenant_id)
            if query.project_id:
                db_query = db_query.filter(AdminJob.project_id == query.project_id)
            if query.kb_id:
                db_query = db_query.filter(AdminJob.kb_id == query.kb_id)

            total = db_query.count()
            offset = (query.page - 1) * query.page_size
            rows = (
                db_query.order_by(AdminJob.created_at.desc())
                .offset(offset)
                .limit(query.page_size)
                .all()
            )
            return [_to_item(row) for row in rows], total
        except Exception as exc:
            logger.error(f"查询任务列表失败: {exc}", exc_info=True)
            raise BusinessException("查询任务列表失败")

    def get_job(self, db: Session, job_id: int) -> JobItem:
        try:
            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            if not job:
                raise NotFoundException("任务不存在")
            return _to_item(job)
        except NotFoundException:
            raise
        except Exception as exc:
            logger.error(f"查询任务详情失败: {exc}", exc_info=True)
            raise BusinessException("查询任务详情失败")

    def retry_job(self, db: Session, job_id: int, *, operator_id: Optional[int], trace_id: Optional[str]) -> JobItem:
        try:
            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            if not job:
                raise NotFoundException("任务不存在")
            if job.status not in ALLOWED_RETRY_FROM:
                raise ValidationException("仅失败/已取消任务可重试")
            if (job.retry_count or 0) >= (job.max_retries or 0):
                raise ValidationException("任务已达到最大重试次数")

            job.retry_count = (job.retry_count or 0) + 1
            job.status = JOB_STATUS_PENDING
            job.error_message = None
            job.result = None
            job.started_at = None
            job.finished_at = None
            job.claimed_by = None
            job.claim_expires_at = None
            job.last_heartbeat_at = None
            job.requested_by = operator_id or job.requested_by
            job.trace_id = trace_id or job.trace_id
            db.commit()
            db.refresh(job)
            self._write_job_log(
                db,
                job=job,
                action="job_retry_submitted",
                details={
                    "job_type": job.job_type,
                    "retry_count": job.retry_count,
                    "max_retries": job.max_retries,
                    "operator_id": operator_id,
                },
            )
            return _to_item(job)
        except (NotFoundException, ValidationException):
            raise
        except Exception as exc:
            db.rollback()
            logger.error(f"重试任务失败: {exc}", exc_info=True)
            raise BusinessException("重试任务失败")

    def cancel_job(self, db: Session, job_id: int, *, trace_id: Optional[str]) -> JobItem:
        try:
            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            if not job:
                raise NotFoundException("任务不存在")
            if job.status not in ALLOWED_CANCEL_FROM:
                raise ValidationException("当前任务状态不支持取消")

            job.status = JOB_STATUS_CANCELLED
            job.finished_at = datetime.utcnow()
            job.claimed_by = None
            job.claim_expires_at = None
            job.last_heartbeat_at = datetime.utcnow()
            job.trace_id = trace_id or job.trace_id
            db.commit()
            db.refresh(job)
            self._write_job_log(
                db,
                job=job,
                action="job_cancelled",
                details={"job_type": job.job_type, "status": job.status},
            )
            return _to_item(job)
        except (NotFoundException, ValidationException):
            raise
        except Exception as exc:
            db.rollback()
            logger.error(f"取消任务失败: {exc}", exc_info=True)
            raise BusinessException("取消任务失败")


job_service = JobService()
