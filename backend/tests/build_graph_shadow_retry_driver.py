#!/usr/bin/env python3
"""
普通 build_graph 影子失败 → 作业边界 fail-closed → 真实 job_service 退避重试 驱动
（由 check_build_graph_shadow_retry.py 以子进程调用）

覆盖方向B 审计三个 P1 与 P2 的"作业系统真的会重试、而非只证明可重放异常"：
  - P1#1 relay：真实 `retrieval_orchestrator.index_chunks` 把 dual_write 影子写失败
    (DualWriteShadowError) 吸收进 failures 列表、不上抛（这是被静默掩盖的根因）；
  - P1#3 修复：真实 `job_runtime.execute_build_graph` 见 stats["vector_failures"] 非空
    不再报 completed，改抛 RuntimeError；
  - P2：真实 `admin.services.job_service.run_job` 捕获该 RuntimeError 后按
    max_retries 递增 retry_count、指数退避排重试，耗尽后落终态 failed——全程走真实
    作业状态机，绝不被判成功。

只有"文档解析 / 实体抽取 / Neo4j / embedding / Milvus 网络写"这些与向量失败无关的重活
换成假实现；作业边界、index_chunks 吞异常、vector_store dual_write 扇出、job_service
重试决策都保持真实代码路径。引擎方言非 sqlite 立即退出（9），不碰共享 dev 活栈。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# 先导入 admin.database：它按 GRAPHINSIGHT_BACKEND_ENV_FILE 加载 dotenv（含
# ADMIN_DATABASE_URL / JOB_* ），必须在 job_service 读取模块级 JOB_* 常量之前完成。
from admin.database import Base, SessionLocal, engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

KB = "kb-bgsr"
TENANT = "t1"
PROJECT = "p1"
DOC = "doc-1"


def _guard_sqlite() -> None:
    if engine.dialect.name != "sqlite":
        print("__ABORT__" + json.dumps({"reason": "engine_dialect_not_sqlite", "dialect": engine.dialect.name}))
        sys.exit(9)


def _reset_schema() -> None:
    from admin.models import AdminJob, AdminLog

    Base.metadata.drop_all(
        bind=engine, tables=[AdminJob.__table__, AdminLog.__table__], checkfirst=True
    )
    Base.metadata.create_all(bind=engine, tables=[AdminJob.__table__, AdminLog.__table__])


class ShadowFailingClient:
    """v2 主库 upsert 成功、v3 影子 upsert 抛错；记录每库落库情况（与 E2 同型）。"""

    def __init__(self, primary: str, shadow: str) -> None:
        self.primary = primary
        self.shadow = shadow
        self.upserts: Dict[str, list] = {}
        self.shadow_attempted = False

    def has_collection(self, name):
        return True

    def upsert(self, *, collection_name, data):  # noqa: A002 - 匹配 pymilvus 关键字签名
        if collection_name == self.shadow:
            self.shadow_attempted = True
            raise RuntimeError("simulated v3 shadow upsert failure")
        self.upserts.setdefault(collection_name, []).append(data)
        return {"upsert_count": len(data)}


_ACTIVE_CLIENT: Dict[str, Any] = {}


def _install_shadow_vector_store(primary: str, shadow: str) -> ShadowFailingClient:
    """把真实 vector_store 单例配成 dual_write 生效、影子 client 抛错。"""
    from services.embedding_service import embedding_service
    from services.vector_store import vector_store

    client = ShadowFailingClient(primary, shadow)
    _ACTIVE_CLIENT["client"] = client
    vector_store._get_client = lambda: client
    vector_store._get_client = lambda: client
    vector_store._revision_field = {primary: True, shadow: True}
    vector_store.is_enabled = lambda: True
    vector_store.config = lambda: {
        "enabled": True,
        "provider": "milvus",
        "collection": primary,
        "dual_write": True,
        "shadow_collection": shadow,
    }
    vector_store.ensure_collection = lambda **k: None
    vector_store.has_content_revision_field = lambda *a, **k: True

    embedding_service.is_enabled = lambda: True
    embedding_service.config = lambda: {"model": "m-test", "batch_size": 32}
    embedding_service.embed_texts = lambda texts: [[0.1, 0.2] for _ in texts]
    embedding_service.content_hash = lambda value: "h-test"
    return client


# ---------------------------------------------------------------------------
# 场景 1：P1#1 —— 真实 index_chunks 把影子写失败吸收成 failures（不上抛）
# ---------------------------------------------------------------------------


def scenario_relay_absorbs_failure() -> None:
    primary, shadow = "graphinsight_chunks_v2", "graphinsight_chunks_v3"
    _install_shadow_vector_store(primary, shadow)

    from services.retrieval_orchestrator import retrieval_orchestrator

    chunk_payload = [
        {
            "chunk_id": "c-1",
            "doc_id": DOC,
            "text": "正文",
            "title": "t",
            "location": "p1",
            "entities": ["Alice"],
        }
    ]

    raised = None
    result: Dict[str, Any] = {}
    try:
        result = retrieval_orchestrator.index_chunks(
            chunk_payload, kb_id=KB, tenant_id=TENANT, project_id=PROJECT
        )
    except Exception as exc:  # noqa: BLE001 - 场景核心：验证是否被吸收而非上抛
        raised = type(exc).__name__

    client = _ACTIVE_CLIENT["client"]
    from services.vector_store import vector_store

    print(
        "__RELAY__"
        + json.dumps(
            {
                "raised": raised,
                "failures_count": len(result.get("failures") or []),
                "indexed": int(result.get("indexed") or 0),
                "primary_written": primary in client.upserts,
                "shadow_attempted": client.shadow_attempted,
                "shadow_written": shadow in client.upserts,
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 2：P1#3 + P2 —— 真实 execute_build_graph + 真实 job_service.run_job 重试到耗尽
# ---------------------------------------------------------------------------


def scenario_job_retries_then_exhausts() -> None:
    _reset_schema()

    exec_state = {"n": 0, "kwargs": None}

    from services.document_graph_service import DocumentGraphService

    def fake_build_graph(self, **kwargs):  # noqa: ANN001, ARG001 - 替身签名对齐真实调用
        exec_state["n"] += 1
        exec_state["kwargs"] = {k: kwargs.get(k) for k in ("kb_id", "doc_ids", "tenant_id", "project_id")}
        # 精确复刻场景 1 真实 relay 交给 document_graph_service 的 stats 形态：
        # documents>0（否则会报 completed），但 vector_failures 非空（影子脏写未收敛）。
        return {
            "documents": 1,
            "total_documents": 1,
            "skipped_documents": 0,
            "chunks": 1,
            "entities": 1,
            "relations": 0,
            "vector_indexed": 0,
            "vector_failures": ["simulated v3 shadow upsert failure"],
            "failures": [],
            "skipped_cross_scope": [],
        }

    DocumentGraphService.build_graph = fake_build_graph

    from admin.models import AdminJob
    from admin.services.job_service import (
        job_service as svc,
        RUNNABLE_JOB_TYPES,
        JOB_AUTO_RETRY_ENABLED,
    )

    # 屏蔽真实重试线程唤醒：本场景同步驱动"排定的重试"（run_job 本身即重试执行单元），
    # 但保留并断言真实 run_job 的重试决策与退避计算。
    sched_calls: List[Dict[str, Any]] = []
    svc._schedule_retry = lambda job_id, attempt, delay: sched_calls.append(
        {"job_id": job_id, "attempt": attempt, "delay": delay}
    )

    payload = {
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "doc_ids": [DOC],
        "source": "documents",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries) VALUES ('build_graph', 'pending', :t, :p, :kb, :payload, 0, 2)"
            ),
            {"t": TENANT, "p": PROJECT, "kb": KB, "payload": json.dumps(payload, ensure_ascii=False)},
        )
        job_id = int(conn.execute(text("SELECT id FROM admin_jobs ORDER BY id LIMIT 1")).scalar_one())

    def row_state() -> Dict[str, Any]:
        db = SessionLocal()
        try:
            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            return {
                "status": job.status,
                "retry_count": job.retry_count,
                "max_retries": job.max_retries,
                "error_message": job.error_message or "",
            }
        finally:
            db.close()

    def set_pending() -> None:
        # 模拟 _schedule_retry 后台唤醒把 failed 复位成 pending 交回 worker（同步驱动，去掉线程时序）
        db = SessionLocal()
        try:
            job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
            job.status = "pending"
            db.commit()
        finally:
            db.close()

    timeline: List[Dict[str, Any]] = []
    succeeded_seen = False
    prev_sched = 0
    for _round in range(3):  # max_retries=2 → 初始 1 次 + 重试 2 次 = 3 次执行
        svc.run_job(job_id)
        state = row_state()
        timeline.append({**state, "exec_count": exec_state["n"]})
        if state["status"] == "succeeded":
            succeeded_seen = True
            break
        # 真实 _schedule_retry 是 daemon 线程 sleep(delay) 后把 failed 复位 pending 交 worker；
        # 本场景用 sched_calls 增长作为"这一轮确实排了新重试"的判据，同步模拟唤醒。
        if len(sched_calls) > prev_sched:
            prev_sched = len(sched_calls)
            set_pending()

    print(
        "__RETRY__"
        + json.dumps(
            {
                "exec_count": exec_state["n"],
                "succeeded_seen": succeeded_seen,
                "timeline": timeline,
                "sched_calls": sched_calls,
                "final": timeline[-1] if timeline else {},
                "build_graph_kwargs": exec_state["kwargs"],
                "runnable_has_build_graph": "build_graph" in RUNNABLE_JOB_TYPES,
                "auto_retry_enabled": bool(JOB_AUTO_RETRY_ENABLED),
            },
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# 场景 3：对照组 —— 无 vector_failures 的干净 build_graph 必须判 completed/succeeded
# ---------------------------------------------------------------------------


def scenario_clean_build_graph_succeeds() -> None:
    _reset_schema()

    from services.document_graph_service import DocumentGraphService

    def fake_build_graph(self, **kwargs):  # noqa: ANN001, ARG001
        return {
            "documents": 1,
            "total_documents": 1,
            "skipped_documents": 0,
            "chunks": 3,
            "entities": 2,
            "relations": 1,
            "vector_indexed": 3,
            "vector_failures": [],
            "failures": [],
            "skipped_cross_scope": [],
        }

    DocumentGraphService.build_graph = fake_build_graph

    from admin.models import AdminJob
    from admin.services.job_service import job_service as svc

    svc._schedule_retry = lambda *a, **k: None

    payload = {"kb_id": KB, "tenant_id": TENANT, "project_id": PROJECT, "doc_ids": [DOC], "source": "documents"}
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, "
                "retry_count, max_retries) VALUES ('build_graph', 'pending', :t, :p, :kb, :payload, 0, 2)"
            ),
            {"t": TENANT, "p": PROJECT, "kb": KB, "payload": json.dumps(payload, ensure_ascii=False)},
        )
        job_id = int(conn.execute(text("SELECT id FROM admin_jobs ORDER BY id LIMIT 1")).scalar_one())
    svc.run_job(job_id)
    db = SessionLocal()
    try:
        job = db.query(AdminJob).filter(AdminJob.id == job_id).first()
        result_obj = json.loads(job.result) if job.result else {}
        print(
            "__CLEAN__"
            + json.dumps(
                {
                    "status": job.status,
                    "retry_count": job.retry_count,
                    "execution_status": (result_obj.get("result") or result_obj).get("execution_status"),
                    "error_message": job.error_message or "",
                },
                ensure_ascii=False,
            )
        )
    finally:
        db.close()


SCENARIOS = {
    "relay_absorbs_failure": scenario_relay_absorbs_failure,
    "job_retries_then_exhausts": scenario_job_retries_then_exhausts,
    "clean_build_graph_succeeds": scenario_clean_build_graph_succeeds,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="build_graph shadow-failure retry driver")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args()
    _guard_sqlite()
    SCENARIOS[args.scenario]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
