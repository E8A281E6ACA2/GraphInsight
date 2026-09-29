#!/usr/bin/env python3
"""Unit-style checks for Python background job worker behavior."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


class _FakeColumn:
    class _Expr:
        def __or__(self, _other):
            return self

    def __eq__(self, _other):
        return self._Expr()

    def __lt__(self, _other):
        return self._Expr()

    def in_(self, _values):
        return self._Expr()

    def asc(self):
        return self._Expr()

    def is_(self, _other):
        return self._Expr()

    def is_not(self, _other):
        return self._Expr()


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _value):
        return self

    def all(self):
        if self.row is None:
            return []
        if isinstance(self.row, list):
            return self.row
        return [self.row]

    def first(self):
        return self.row


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.closed = False

    def query(self, _model):
        return _FakeQuery(self.row)

    def flush(self):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_run_next_pending_job_once() -> None:
    from admin.services.job_service import JobService

    fake_session = _FakeSession((123,))
    executed: list[int] = []

    with patch("admin.services.job_service.SessionLocal", return_value=fake_session), patch(
        "admin.services.job_service.AdminJob",
        SimpleNamespace(
            id=_FakeColumn(),
            status=_FakeColumn(),
            job_type=_FakeColumn(),
            claim_expires_at=_FakeColumn(),
            created_at=_FakeColumn(),
        ),
    ):
        service = JobService()
        service.run_job = lambda job_id: executed.append(job_id)  # type: ignore[method-assign]
        processed = service.run_next_pending_job_once()

    _assert(processed is True, "expected one pending job to be processed")
    _assert(executed == [123], f"unexpected executed jobs: {executed}")
    _assert(fake_session.closed, "session should be closed after polling")


def _check_run_next_pending_job_once_when_empty() -> None:
    from admin.services.job_service import JobService

    fake_session = _FakeSession(None)
    executed: list[int] = []

    with patch("admin.services.job_service.SessionLocal", return_value=fake_session), patch(
        "admin.services.job_service.AdminJob",
        SimpleNamespace(
            id=_FakeColumn(),
            status=_FakeColumn(),
            job_type=_FakeColumn(),
            claim_expires_at=_FakeColumn(),
            created_at=_FakeColumn(),
        ),
    ):
        service = JobService()
        service.run_job = lambda job_id: executed.append(job_id)  # type: ignore[method-assign]
        processed = service.run_next_pending_job_once()

    _assert(processed is False, "expected no pending job to be processed")
    _assert(executed == [], f"unexpected executed jobs: {executed}")
    _assert(fake_session.closed, "session should be closed after polling")


def _check_start_and_stop_background_worker() -> None:
    from admin.services.job_service import JobService

    starts: list[threading.Event] = []

    def _fake_worker_loop(stop_event: threading.Event, _wake_event: threading.Event) -> None:
        starts.append(stop_event)
        stop_event.wait(0.1)

    service = JobService()
    service._worker_loop = _fake_worker_loop  # type: ignore[method-assign]
    service.start_background_worker()
    thread = service._worker_thread
    _assert(thread is not None, "worker thread should be created")
    _assert(thread.is_alive(), "worker thread should be alive after start")
    _assert(len(starts) == 1, f"expected exactly one worker start, got {len(starts)}")

    service.start_background_worker()
    _assert(service._worker_thread is thread, "duplicate start should reuse existing worker thread")

    service.stop_background_worker()
    _assert(service._worker_thread is None, "worker thread should be cleared after stop")
    _assert(starts[0].is_set(), "stop event should be signaled on stop")


def _check_wake_worker_signals_live_thread() -> None:
    from admin.services.job_service import JobService

    service = JobService()
    service._worker_thread = SimpleNamespace(is_alive=lambda: True)
    service._worker_wake_event.clear()

    woke = service.wake_worker()

    _assert(woke is True, "wake_worker should report accepted")
    _assert(service._worker_wake_event.is_set(), "wake_worker should signal existing thread")


def _check_recover_stale_running_jobs_once() -> None:
    from admin.services.job_service import JOB_STATUS_PENDING, JOB_STATUS_RUNNING, JobService

    job = SimpleNamespace(
        id=17,
        job_type="build_graph",
        status=JOB_STATUS_RUNNING,
        claimed_by="py-job-worker:stale",
        claim_expires_at="expired",
        last_heartbeat_at="expired",
        started_at="started",
        finished_at=None,
        error_message=None,
        tenant_id=None,
        requested_by=None,
        trace_id=None,
    )
    fake_session = _FakeSession([job])
    log_actions: list[str] = []

    with patch("admin.services.job_service.SessionLocal", return_value=fake_session), patch(
        "admin.services.job_service.AdminJob",
        SimpleNamespace(
            id=_FakeColumn(),
            status=_FakeColumn(),
            job_type=_FakeColumn(),
            claim_expires_at=_FakeColumn(),
        ),
    ):
        service = JobService()
        service._write_job_log = lambda db, **kwargs: log_actions.append(kwargs["action"])  # type: ignore[method-assign]
        recovered = service.recover_stale_running_jobs_once()

    _assert(recovered == 1, f"expected one recovered job, got {recovered}")
    _assert(job.status == JOB_STATUS_PENDING, f"expected pending after recovery, got {job.status}")
    _assert(job.claimed_by is None, f"expected claimed_by cleared, got {job.claimed_by}")
    _assert(job.claim_expires_at is None, f"expected claim_expires_at cleared, got {job.claim_expires_at}")
    _assert(job.last_heartbeat_at is None, f"expected last_heartbeat_at cleared, got {job.last_heartbeat_at}")
    _assert(job.started_at is None, f"expected started_at cleared, got {job.started_at}")
    _assert(log_actions == ["job_requeued_stale_lease"], f"unexpected log actions: {log_actions}")
    _assert(fake_session.closed, "session should be closed after stale recovery")


def _check_build_graph_job_passes_reasoning_profile() -> None:
    from services.job_runtime import execute_build_graph

    captured: dict[str, object] = {}

    class _FakeGraphService:
        def build_graph(self, **kwargs):
            captured.update(kwargs)
            return {
                "documents": 1,
                "chunks": 2,
                "entities": 3,
                "relations": 4,
                "total_documents": 1,
                "skipped_documents": 0,
                "failures": [],
            }

    with patch("services.job_runtime.DocumentGraphService", return_value=_FakeGraphService()):
        result = execute_build_graph(
            job_id=101,
            payload={
                "source": "documents",
                "force": False,
                "kb_id": "kb-1",
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "doc_ids": ["doc-1"],
                "reasoning_profile": "balanced",
                "complex_extraction": True,
                "parser_provider": "mineru",
            },
        )

    _assert(captured.get("reasoning_profile") == "balanced", f"unexpected reasoning profile: {captured}")
    _assert(captured.get("complex_extraction") is True, f"unexpected complex_extraction: {captured}")
    _assert(captured.get("parser_provider") == "mineru", f"unexpected parser_provider: {captured}")
    _assert(result["reasoning_profile"] == "balanced", f"unexpected result reasoning profile: {result}")
    _assert(result["complex_extraction"] is True, f"unexpected result complex flag: {result}")
    _assert(result["parser_provider"] == "mineru", f"unexpected result parser provider: {result}")


def main() -> int:
    _check_run_next_pending_job_once()
    _check_run_next_pending_job_once_when_empty()
    _check_start_and_stop_background_worker()
    _check_wake_worker_signals_live_thread()
    _check_recover_stale_running_jobs_once()
    _check_build_graph_job_passes_reasoning_profile()
    print("JOB_WORKER_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
