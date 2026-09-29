"""
知识库表迁移幂等/回滚 smoke（M1 验收门）

对临时 SQLite 库执行 migrate -> migrate（幂等）-> rollback -> migrate，
验证两张新表与审计 scope 列可重复执行且可回滚。
不触碰开发/生产数据库；PostgreSQL 上的正式执行由部署流程完成。

运行：python backend/tests/check_kb_migrations_smoke.py
（依赖 sqlalchemy；无需连接外部服务）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

FAILURES: list = []


def _utf8_env(base: dict) -> dict:
    """强制子进程以 UTF-8 读写管道，避免 Windows 默认码（cp936）截获中文时乱码。"""
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_script(script: str, action: str, db_url: str) -> tuple:
    env = _utf8_env(os.environ)
    env["ADMIN_DATABASE_URL"] = db_url
    env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = ""  # 避免本地 .env 覆盖测试库地址
    proc = subprocess.run(
        [sys.executable, str(backend_dir / "admin" / script), "--action", action],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=tempfile.gettempdir(),
        env=env,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("=" * 60)
    print("GraphInsight KB migrations idempotency/rollback smoke (sqlite)")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'kb_mig_smoke.db'}"

        # 引导基础表：audit 列迁移依赖 admin_logs / admin_qa_traces 已存在
        bootstrap = (
            "import sys; sys.path.insert(0, r'{backend}');"
            "from admin.database import Base, engine;"
            "from admin.models import AdminUser, AdminLog, AdminQATrace;"
            "Base.metadata.create_all(bind=engine, tables=["
            "AdminUser.__table__, AdminLog.__table__, AdminQATrace.__table__]);"
            "engine.dispose(); print('bootstrap ok')"
        ).format(backend=backend_dir)
        env0 = _utf8_env(os.environ)
        env0["ADMIN_DATABASE_URL"] = db_url
        env0["GRAPHINSIGHT_BACKEND_ENV_FILE"] = ""
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tempfile.gettempdir(), env=env0, timeout=120,
        )
        step("基础表引导", proc.returncode == 0, (proc.stdout + proc.stderr)[-300:])

        for script in ("migrate_knowledge_base_tables.py", "migrate_audit_scope_columns.py"):
            print(f"[{script}]")
            code, out = run_script(script, "migrate", db_url)
            step("首次 migrate", code == 0, out[-300:])
            code, out = run_script(script, "migrate", db_url)
            step("重复 migrate（幂等）", code == 0, out[-300:])
            code, out = run_script(script, "rollback", db_url)
            step("rollback", code == 0, out[-300:])
            code, out = run_script(script, "migrate", db_url)
            step("rollback 后再 migrate", code == 0, out[-300:])

        # 验证表结构：rollback 后重新迁移，用 inspector 检查列
        os.environ["ADMIN_DATABASE_URL"] = db_url
        os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = ""
        from sqlalchemy import inspect

        from admin.database import engine

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        step("knowledge_bases 存在", "knowledge_bases" in tables, str(sorted(tables)))
        step("knowledge_base_documents 存在", "knowledge_base_documents" in tables)
        log_cols = {c["name"] for c in inspector.get_columns("admin_logs")} if "admin_logs" in tables else set()
        qa_cols = {c["name"] for c in inspector.get_columns("admin_qa_traces")} if "admin_qa_traces" in tables else set()
        step("admin_logs 含 project_id/kb_id", {"project_id", "kb_id"} <= log_cols, str(sorted(log_cols)))
        step("admin_qa_traces 含 tenant/project/kb", {"tenant_id", "project_id", "kb_id"} <= qa_cols, str(sorted(qa_cols)))
        engine.dispose()  # Windows 下必须先释放连接才能清理临时目录

    print("-" * 60)
    if FAILURES:
        for item in FAILURES:
            print(f"  FAILED: {item}")
        return 1
    print("✓ all migration smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
