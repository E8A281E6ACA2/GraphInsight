#!/usr/bin/env python3
"""P6 一次性 Postgres 驱动（在 gi-p6-py 容器内执行，由 check_m5_p6_disposable_pg.py 编排）。

为什么必须有这一轮：§16.3 的复用分支在 Postgres 下会加 `FOR UPDATE` 行锁
（`services/reindex_queue.py:_lock_existing_job`），而既有套件全部跑在 SQLite 上，
那条分支从未被执行过；缺列 → 42703 也只在真 PG 上才造得出形态。

阶段（每个阶段一次独立进程，真实迁移脚本由编排器作为独立子进程跑）：
  bootstrap        钉连接 + 建表 + 种 KB
  assert-old-shape 断言 targets_hash 列/索引确实不在（旧形态成立），并种一条旧行
  assert-migrated  断言真实迁移脚本把列 + 部分唯一索引补齐
  submit           真实 `JobService.create_job` 提交路径：判据 2（能建）、
                   判据 6（同 hash 不新增且回读到既有 child ID）、判据 7（failed/cancelled 原地 retry/reset）
  dump             收尾状态，供编排器复核

隔离铁律：只认 `GI_P6_PG_DSN`；连接落点必须是一次性集群（同集群内不得存在共享开发库名），
方言不是 postgresql 就在任何 DDL 之前退出 9。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

KB = "kb-p6"
TENANT = "t1"
PROJECT = "p1"
DOC = "doc-p6"
CHUNKS = ("c-p6-1", "c-p6-2")
TARGET_REVISION = 1
TRACE = "p6-disposable-trace"
# Go 写侧认证结果里的操作员 id（与 httpserver 假授权保持一致，由编排器注入回 Go 阶段核对）。
OPERATOR_ID = 1

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILURES: List[str] = []
CAPTURED_SQL: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def marker(name: str, payload: Dict[str, Any]) -> None:
    print(f"__{name}__ {json.dumps(payload, ensure_ascii=False, sort_keys=True)}")


def fatal(message: str) -> None:
    print(f"FATAL: {message}")
    raise SystemExit(9)


def pin_env() -> str:
    dsn = os.environ.get("GI_P6_PG_DSN", "").strip()
    if not dsn:
        fatal("未注入 GI_P6_PG_DSN（P6 只允许一次性 Postgres，不给默认配置留回落口）")
    env_path = Path(os.environ.get("GI_P6_ENV_FILE", "/tmp/p6_admin.env"))
    env_path.write_text(f"ADMIN_DATABASE_URL={dsn}\n", encoding="utf-8")
    os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(env_path)
    return dsn


def assert_disposable(dsn: str):
    from admin.database import engine
    from sqlalchemy import text

    print("DIALECT", engine.dialect.name)
    if engine.dialect.name != "postgresql":
        fatal(f"方言是 {engine.dialect.name}，P6 必须是 postgresql（SQLite 造不出缺列 42703 与 FOR UPDATE）")

    expected_host = os.environ.get("GI_P6_PG_HOST", "gi-p6-pg").strip()
    expected_db = os.environ.get("GI_P6_PG_DATABASE", "p6_admin").strip()
    if expected_host not in dsn:
        fatal(f"钉的连接串里没有一次性容器主机名 {expected_host}: {dsn}")

    with engine.connect() as conn:
        current_db = conn.execute(text("SELECT current_database()")).scalar()
        shared_dev = conn.execute(
            text("SELECT count(*) FROM pg_database WHERE datname IN ('graphinsight_admin', 'graphinsight')")
        ).scalar()
        server_addr = str(conn.execute(text("SELECT COALESCE(inet_server_addr()::text, '')")).scalar())
    if str(current_db) != expected_db:
        fatal(f"当前库是 {current_db}，期望一次性库 {expected_db}")
    if int(shared_dev or 0) > 0:
        fatal("该集群里存在共享开发库名，判定为连错对象，拒绝执行任何 DDL")
    return engine, {
        "current_database": str(current_db),
        "shared_dev_databases_in_cluster": int(shared_dev or 0),
        "server_addr": server_addr,
    }


def job_shape(engine) -> Dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as conn:
        columns = [
            str(row[0])
            for row in conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'admin_jobs' ORDER BY ordinal_position")
            )
        ]
        indexes = [
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'admin_jobs' ORDER BY indexname"
                )
            )
        ]
    partial = [defn for name, defn in indexes if name == "uq_admin_jobs_targets_hash"]
    return {
        "column_count": len(columns),
        "has_targets_hash": "targets_hash" in columns,
        "index_names": [name for name, _ in indexes],
        "partial_unique_indexdef": partial[0] if partial else "",
    }


def seed_kb(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:kb, :tenant, :project, :name, 'active', :prefix) ON CONFLICT (id) DO NOTHING"
            ),
            {"kb": KB, "tenant": TENANT, "project": PROJECT, "name": "kb p6", "prefix": f"kb/{KB}"},
        )


def seed_operator(engine) -> int:
    """种 Go 写侧需要的操作员行：admin_jobs.requested_by 与 admin_logs.operator_id 两条外键
    都指向 admin_users(id)。Python 内部提交路径传 requested_by=None，所以 P6 前几轮不种也绿；
    Wave 8 的 Go 提交链会把认证结果里的 UserID 真写进这两列，缺行就是外键违例（实跑抓到过）。
    id 必须与 Go 测试假授权（newSoftKBGuardForTest 的 authz.UserID）一致，由编排器注入给
    Go 阶段核对，不靠两边各写一个魔数。
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO admin_users (id, username, password_hash, email, is_active) "
                "VALUES (:id, :username, 'p6-not-a-real-hash', :email, TRUE) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": OPERATOR_ID, "username": f"p6-operator-{OPERATOR_ID}", "email": f"p6-operator-{OPERATOR_ID}@p6.local"},
        )
        conn.execute(text("SELECT setval('admin_users_id_seq', GREATEST(:id, 1), TRUE)"), {"id": OPERATOR_ID})
        present = conn.execute(
            text("SELECT count(*) FROM admin_users WHERE id = :id"), {"id": OPERATOR_ID}
        ).scalar()
    check(f"操作员行已就位（id={OPERATOR_ID}，供 Go 写侧两条外键引用）", int(present or 0) == 1, f"count={present}")
    return OPERATOR_ID


def stage_bootstrap(engine) -> None:
    from admin.database import Base
    from admin.models import AdminJob, AdminLog, AdminUser, KnowledgeBase

    Base.metadata.create_all(
        bind=engine,
        tables=[AdminUser.__table__, AdminJob.__table__, AdminLog.__table__, KnowledgeBase.__table__],
    )
    seed_kb(engine)
    operator_id = seed_operator(engine)
    shape = job_shape(engine)
    check("建表后 admin_jobs 含 targets_hash（随后由真实迁移脚本回滚成旧形态）", shape["has_targets_hash"], str(shape))
    marker("BOOTSTRAP", {
        "shape": shape,
        "kb_id": KB,
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "operator_id": operator_id,
    })


def stage_assert_old_shape(engine) -> None:
    shape = job_shape(engine)
    check("旧形态：admin_jobs 无 targets_hash 列", not shape["has_targets_hash"], str(shape))
    check("旧形态：无 uq_admin_jobs_targets_hash 索引", not shape["partial_unique_indexdef"], str(shape))

    from sqlalchemy import text

    with engine.begin() as conn:
        legacy_id = conn.execute(
            text(
                "INSERT INTO admin_jobs (job_type, status, tenant_id, project_id, kb_id, payload, result, "
                "error_message, retry_count, max_retries, requested_by, trace_id, started_at, finished_at) "
                "VALUES ('build_graph', 'succeeded', :tenant, :project, :kb, :payload, NULL, NULL, 0, 3, NULL, "
                "'p6-legacy-row', NULL, NULL) RETURNING id"
            ),
            {
                "tenant": TENANT,
                "project": PROJECT,
                "kb": KB,
                "payload": json.dumps({"kb_id": KB, "doc_ids": ["doc-legacy"]}, sort_keys=True),
            },
        ).scalar()
    check("旧列清单（不含 targets_hash）INSERT 成功", legacy_id is not None, str(legacy_id))
    marker("LEGACY", {"legacy_job_id": int(legacy_id), "shape": shape})


def stage_assert_migrated(engine, before: Optional[Dict[str, Any]]) -> None:
    shape = job_shape(engine)
    check("迁移后：targets_hash 列已补齐", shape["has_targets_hash"], str(shape))
    check(
        "迁移后：列数比旧形态多 1",
        before is not None and shape["column_count"] == before["column_count"] + 1,
        f"before={before} after={shape}",
    )
    defn = shape["partial_unique_indexdef"]
    check(
        "迁移后：部分唯一索引是 UNIQUE 且带 WHERE targets_hash IS NOT NULL",
        "UNIQUE" in defn and "WHERE (targets_hash IS NOT NULL)" in defn,
        defn,
    )
    marker("MIGRATED", {"shape": shape, "old_shape": before or {}})


def _latest_log(engine, job_id: int) -> Dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT action, details FROM admin_logs WHERE resource = 'job' AND resource_id = :rid "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"rid": str(job_id)},
        ).first()
    if row is None:
        return {}
    try:
        details = json.loads(str(row[1] or "{}"))
    except json.JSONDecodeError:
        details = {}
    return {"action": str(row[0]), "details": details}


def _reindex_row_count(engine) -> int:
    from sqlalchemy import text

    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM admin_jobs WHERE job_type = 'reindex_chunks'")).scalar())


def _read_row(engine, job_id: int) -> Dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, job_type, status, retry_count, max_retries, targets_hash, kb_id, tenant_id, project_id "
                "FROM admin_jobs WHERE id = :id"
            ),
            {"id": job_id},
        ).first()
    if row is None:
        return {}
    return {
        "id": int(row[0]),
        "job_type": str(row[1]),
        "status": str(row[2]),
        "retry_count": int(row[3]),
        "max_retries": int(row[4]),
        "targets_hash": str(row[5]) if row[5] is not None else None,
        "kb_id": str(row[6]) if row[6] is not None else None,
        "tenant_id": str(row[7]) if row[7] is not None else None,
        "project_id": str(row[8]) if row[8] is not None else None,
    }


def stage_submit(engine) -> None:
    from sqlalchemy import event, text

    from admin.schemas.jobs import JobCreateRequest
    from admin.services.job_service import JobService, REINDEX_CHUNKS_JOB_TYPE
    from admin.database import SessionLocal
    from services.reindex_queue import canonical_targets_hash

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        CAPTURED_SQL.append(str(statement))

    service = JobService()
    payload_targets = [{"chunk_id": chunk, "target_revision": TARGET_REVISION} for chunk in CHUNKS]
    expected_hash = canonical_targets_hash(payload_targets)

    def submit(tag: str) -> Dict[str, Any]:
        request = JobCreateRequest(
            tenant_id=TENANT,
            project_id=PROJECT,
            kb_id=KB,
            max_retries=3,
            payload={
                "kb_id": KB,
                "tenant_id": TENANT,
                "project_id": PROJECT,
                "doc_id": DOC,
                "targets": payload_targets,
            },
        )
        db = SessionLocal()
        try:
            item = service.create_job(
                db, job_type=REINDEX_CHUNKS_JOB_TYPE, request=request, requested_by=None, trace_id=f"{TRACE}-{tag}"
            )
            db.commit()
        finally:
            db.close()
        row = _read_row(engine, int(item.id))
        log = _latest_log(engine, int(item.id))
        return {"tag": tag, "row": row, "log": log, "count": _reindex_row_count(engine)}

    # 判据 2：Python 内部提交路径真的建出 pending 的 reindex_chunks 行
    first = submit("first")
    row1 = first["row"]
    check("判据2 首次提交落库 reindex_chunks", row1.get("job_type") == "reindex_chunks", str(row1))
    check("判据2 新行 status=pending", row1.get("status") == "pending", str(row1))
    check("判据2 targets_hash 落列且等于 canonical 复算值", row1.get("targets_hash") == expected_hash, str(row1))
    check("判据2 作用域冻结为 t1/p1/kb-p6", (row1.get("tenant_id"), row1.get("project_id"), row1.get("kb_id")) == (TENANT, PROJECT, KB), str(row1))
    check("判据2 留痕 outcome=created 且带同一 targets_hash", first["log"].get("details", {}).get("outcome") == "created" and first["log"].get("details", {}).get("targets_hash") == expected_hash, str(first["log"]))
    check("判据2 留痕 child_job_id 就是新建行 id（键名用 child_job_id，不用 job_id）", first["log"].get("details", {}).get("child_job_id") == row1.get("id") and "job_id" not in (first["log"].get("details") or {}), str(first["log"]))

    # 判据 6：同 hash 二次提交不新增，且回读到既有 child ID
    second = submit("second")
    check("判据6 二次提交后 reindex_chunks 仍只有 1 行", second["count"] == 1, str(second))
    check("判据6 回读到的 child_job_id 就是既有行 id（不是新建 id）", second["row"].get("id") == row1.get("id") and second["log"].get("details", {}).get("child_job_id") == row1.get("id"), f"first={row1} second={second['row']} details={second['log'].get('details')}")
    check("判据6 复用留痕 outcome=reused + action=job_reused", second["log"].get("action") == "job_reused" and second["log"].get("details", {}).get("outcome") == "reused", str(second["log"]))
    for_update = [s for s in CAPTURED_SQL if "FOR UPDATE" in s and "admin_jobs" in s]
    check("Postgres 复用分支确实走了 FOR UPDATE 行锁（SQLite 套件到不了这条分支）", bool(for_update), f"captured={len(CAPTURED_SQL)}")

    # 判据 7：failed 原地 retry（不新建行，retry_count+1）
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE admin_jobs SET status = 'failed', retry_count = 1, error_message = 'simulated' WHERE id = :id"),
            {"id": row1["id"]},
        )
    third = submit("after-failed")
    check("判据7 failed 后重提交仍复用同一行", third["count"] == 1 and third["row"].get("id") == row1.get("id"), str(third))
    check("判据7 failed → pending 且 retry_count 由 1 变 2", third["row"].get("status") == "pending" and third["row"].get("retry_count") == 2, str(third["row"]))
    check("判据7 留痕 outcome=retried", third["log"].get("details", {}).get("outcome") == "retried", str(third["log"]))

    # 判据 7：cancelled 原地 reset（retry_count 归零）
    with engine.begin() as conn:
        conn.execute(text("UPDATE admin_jobs SET status = 'cancelled', retry_count = 2 WHERE id = :id"), {"id": row1["id"]})
    fourth = submit("after-cancelled")
    check("判据7 cancelled 后重提交仍复用同一行", fourth["count"] == 1 and fourth["row"].get("id") == row1.get("id"), str(fourth))
    check("判据7 cancelled → pending 且 retry_count 归零", fourth["row"].get("status") == "pending" and fourth["row"].get("retry_count") == 0, str(fourth["row"]))
    check("判据7 留痕 outcome=reset", fourth["log"].get("details", {}).get("outcome") == "reset", str(fourth["log"]))

    marker(
        "SUBMIT",
        {
            "child_job_id": row1.get("id"),
            "targets_hash": expected_hash,
            "doc_id": DOC,
            # Wave 8：Go 写侧用例要用**完全相同**的一批 targets 重提交，才能把
            # "Go 端算出的 targets_hash 与 Python 逐字相同"从推断变成观测。原文只在这里
            # 出口一次，编排器原样注入给 golang 容器，不重新拼字符串（重拼就可能不同形）。
            "payload_targets": payload_targets,
            "reindex_chunks_count": fourth["count"],
            "outcomes": [first["log"].get("details", {}).get("outcome"), second["log"].get("details", {}).get("outcome"), third["log"].get("details", {}).get("outcome"), fourth["log"].get("details", {}).get("outcome")],
            "detail_child_job_ids": [
                (x["log"].get("details") or {}).get("child_job_id")
                for x in (first, second, third, fourth)
            ],
            "for_update_sql_count": len(for_update),
            "log_actions": [first["log"].get("action"), second["log"].get("action")],
        },
    )


def stage_dump(engine) -> None:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = [
            {
                "id": int(r[0]),
                "job_type": str(r[1]),
                "status": str(r[2]),
                "retry_count": int(r[3]),
                "targets_hash": (str(r[4]) if r[4] is not None else None),
            }
            for r in conn.execute(
                text("SELECT id, job_type, status, retry_count, targets_hash FROM admin_jobs ORDER BY id")
            )
        ]
        logs = [
            {"action": str(r[0]), "resource_id": str(r[1])}
            for r in conn.execute(text("SELECT action, resource_id FROM admin_logs WHERE resource = 'job' ORDER BY id"))
        ]
    marker("DUMP", {"jobs": rows, "job_logs": logs, "failures": FAILURES})


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 disposable Postgres driver")
    parser.add_argument(
        "--stage",
        required=True,
        choices=("bootstrap", "assert-old-shape", "assert-migrated", "submit", "dump"),
    )
    parser.add_argument("--old-shape-json", default="", help="assert-migrated 用来对账的旧形态 JSON")
    args = parser.parse_args()

    dsn = pin_env()
    engine, pinned = assert_disposable(dsn)
    marker("PIN", pinned)

    if args.stage == "bootstrap":
        stage_bootstrap(engine)
    elif args.stage == "assert-old-shape":
        stage_assert_old_shape(engine)
    elif args.stage == "assert-migrated":
        before = json.loads(args.old_shape_json) if args.old_shape_json else None
        stage_assert_migrated(engine, before)
    elif args.stage == "submit":
        stage_submit(engine)
    else:
        stage_dump(engine)

    engine.dispose()
    if args.stage != "dump" and FAILURES:
        print(f"STAGE_FAILURES stage={args.stage} count={len(FAILURES)}")
        return 1
    print(f"STAGE_OK stage={args.stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
