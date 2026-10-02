"""
知识库表迁移幂等/回滚 smoke（M1 验收门）

对临时 SQLite 库执行 migrate -> migrate（幂等）-> rollback -> migrate，
验证两张新表与审计 scope 列可重复执行且可回滚。
不触碰开发/生产数据库；PostgreSQL 上的正式执行由部署流程完成。

隔离铁律（v2 整改，任务 #55）：本套件会真实执行 rollback（DROP TABLE / DROP COLUMN），
一旦隔离失效就是删开发库的表。历史上用 `GRAPHINSIGHT_BACKEND_ENV_FILE=""` + 注入
`ADMIN_DATABASE_URL=sqlite` 的写法是伪隔离：backend/admin/database.py 只在该变量
指向"存在的文件"时才走隔离分支，空串会落到 else 分支执行
`load_dotenv(find_dotenv(), override=True)`，沿脚本 __file__ 向上找到 backend/.env，
用其中的 PostgreSQL 地址覆盖注入值。因此本套件只允许下面这一种手法：

1. GRAPHINSIGHT_BACKEND_ENV_FILE 指向临时目录里真实存在、且内容为
   ADMIN_DATABASE_URL=sqlite:///... 的 env 文件（所有子进程与父进程共用）。
2. 任何破坏性动作之前先跑引擎方言守卫；父进程 inspector 也再做一次方言断言。
   任一处解析到非 sqlite 立即中止，一条 DDL 都不下发。

运行：python backend/tests/check_kb_migrations_smoke.py
（依赖 sqlalchemy/dotenv；无需连接外部服务）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Windows 默认码（cp936）下父进程打印 ✓/✗ 与中文会 UnicodeEncodeError 直接崩掉整个取证，
# 所以脚本自身必须强制 UTF-8——不允许靠命令行 `-X utf8` 当前提。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILURES: list = []

# 由 main() 在临时目录里创建；所有子进程与父进程都必须走这一份 env 文件
ENV_FILE: Path = None  # type: ignore[assignment]
DB_URL: str = ""


def _utf8_env(base: dict) -> dict:
    """强制子进程以 UTF-8 读写管道，避免 Windows 默认码（cp936）截获中文时乱码。"""
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _base_env() -> dict:
    """唯一合法的隔离环境：env 文件 + UTF-8 + PYTHONPATH。"""
    env = _utf8_env(os.environ)
    env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(ENV_FILE)
    env["ADMIN_DATABASE_URL"] = DB_URL
    env["PYTHONPATH"] = str(backend_dir)
    return env


def _dump_failure(label: str, code: int, stdout: str, stderr: str) -> None:
    """子进程非 0 退出时给出完整 exit code 与 stderr，不做尾部截断。"""
    if code == 0:
        return
    print(f"    !! {label} exit={code}")
    for line in (stdout.splitlines() or ["<stdout 为空>"]):
        print(f"    [stdout] {line}")
    for line in (stderr.splitlines() or ["<stderr 为空>"]):
        print(f"    [stderr] {line}")


def run_script(script: str, action: str, extra_args: list = None) -> tuple:
    label = f"{script} --action {action}"
    proc = subprocess.run(
        [sys.executable, str(backend_dir / "admin" / script), "--action", action, *(extra_args or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ENV_FILE.parent),
        env=_base_env(),
        timeout=120,
    )
    _dump_failure(label, proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode, proc.stdout + proc.stderr


def run_python_code(code: str) -> tuple:
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ENV_FILE.parent),
        env=_base_env(),
        timeout=120,
    )
    _dump_failure("python -c", proc.returncode, proc.stdout, proc.stderr)
    return proc.returncode, proc.stdout + proc.stderr


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def abort_if_not_isolated(stage: str) -> int:
    """前置守卫失败即中止：本套件会真跑 DROP TABLE / DROP COLUMN，宁可一步都不下发。"""
    if not FAILURES:
        return 0
    print("-" * 60)
    for item in FAILURES:
        print(f"  FAILED: {item}")
    print(f"✗ 前置守卫未通过（{stage}），已中止，未执行任何 migrate/rollback 动作")
    return 1


def guard_isolation() -> None:
    """所有破坏性动作之前的唯一准入条件：子进程与父进程都必须是 sqlite。"""
    code, out = run_python_code(
        "from admin.database import engine;"
        "print('DIALECT', engine.dialect.name);"
        "print('DBNAME', str(engine.url).split('@')[-1])"
    )
    step("子进程引擎守卫（必须 sqlite 且 exit 0）", code == 0 and "DIALECT sqlite" in out, f"exit={code} {out[-400:]}")

    from admin.database import engine as parent_engine

    step(
        "父进程引擎守卫（必须 sqlite）",
        parent_engine.dialect.name == "sqlite",
        f"dialect={parent_engine.dialect.name}",
    )
    parent_engine.dispose()


def dialect_of(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("方言:"):
            return line.split(":", 1)[1].strip()
    return "<未打印方言>"


def run_migration_cycle(script: str) -> None:
    print(f"[{script}]")
    code, out = run_script(script, "migrate", ["--dry-run"])
    step(
        "dry-run 解析到 sqlite 且 exit 0（真实动作前的最后一道闸）",
        code == 0 and dialect_of(out) == "sqlite",
        f"exit={code} dialect={dialect_of(out)} {out[-300:]}",
    )
    if FAILURES:
        print(f"  !! {script} 隔离守卫未过，跳过后续 migrate/rollback")
        return

    for action, name in (
        ("migrate", "首次 migrate"),
        ("migrate", "重复 migrate（幂等）"),
        ("rollback", "rollback"),
        ("migrate", "rollback 后再 migrate"),
    ):
        code, out = run_script(script, action)
        step(
            f"{name}（exit 0 且仍为 sqlite）",
            code == 0 and dialect_of(out) == "sqlite",
            f"exit={code} dialect={dialect_of(out)} {out[-300:]}",
        )


def main() -> int:
    global ENV_FILE, DB_URL

    print("=" * 60)
    print("GraphInsight KB migrations idempotency/rollback smoke (sqlite)")
    print("=" * 60)
    with tempfile.TemporaryDirectory(prefix="graphinsight-kb-mig-smoke-") as tmp:
        tmp_dir = Path(tmp)
        db_path = (tmp_dir / "kb_mig_smoke.db").as_posix()
        DB_URL = f"sqlite:///{db_path}"
        ENV_FILE = tmp_dir / "kb_mig_smoke.env"
        ENV_FILE.write_text(f"ADMIN_DATABASE_URL={DB_URL}\n", encoding="utf-8")

        # 父进程后续会 import admin.database，必须同样指向 env 文件，否则回退 backend/.env
        os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(ENV_FILE)
        os.environ["ADMIN_DATABASE_URL"] = DB_URL

        print(f"隔离方式: GRAPHINSIGHT_BACKEND_ENV_FILE -> {ENV_FILE.name}")
        print("目标库:   sqlite:///<临时目录>/kb_mig_smoke.db")

        guard_isolation()
        rc = abort_if_not_isolated("引擎守卫")
        if rc:
            return rc

        # 引导基础表：audit 列迁移依赖 admin_logs / admin_qa_traces 已存在
        bootstrap = (
            "from admin.database import Base, engine;"
            "from admin.models import AdminUser, AdminLog, AdminQATrace;"
            "Base.metadata.create_all(bind=engine, tables=["
            "AdminUser.__table__, AdminLog.__table__, AdminQATrace.__table__]);"
            "engine.dispose(); print('bootstrap ok')"
        )
        code, out = run_python_code(bootstrap)
        step("基础表引导（exit 0）", code == 0 and "bootstrap ok" in out, f"exit={code} {out[-300:]}")
        rc = abort_if_not_isolated("基础表引导")
        if rc:
            return rc

        for script in ("migrate_knowledge_base_tables.py", "migrate_audit_scope_columns.py"):
            run_migration_cycle(script)
            if FAILURES:
                return abort_if_not_isolated(script)

        # 验证表结构：rollback 后重新迁移，用 inspector 检查列
        from sqlalchemy import inspect

        from admin.database import engine

        step(
            "结构校验连接的仍是 sqlite（非误连开发库）",
            engine.dialect.name == "sqlite",
            f"dialect={engine.dialect.name}",
        )
        if FAILURES:
            engine.dispose()
            return abort_if_not_isolated("结构校验")

        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        step("knowledge_bases 存在", "knowledge_bases" in tables, str(sorted(tables)))
        step("knowledge_base_documents 存在", "knowledge_base_documents" in tables)
        log_cols = {c["name"] for c in inspector.get_columns("admin_logs")} if "admin_logs" in tables else set()
        qa_cols = {c["name"] for c in inspector.get_columns("admin_qa_traces")} if "admin_qa_traces" in tables else set()
        step("admin_logs 含 project_id/kb_id", {"project_id", "kb_id"} <= log_cols, str(sorted(log_cols)))
        step("admin_qa_traces 含 tenant/project/kb", {"tenant_id", "project_id", "kb_id"} <= qa_cols, str(sorted(qa_cols)))

        # 守卫有效性自证：把 env 文件换成指向 PostgreSQL 的内容，引擎必须随之改变，
        # 说明"env 文件"确实是解析入口、上面的方言守卫不是摆设。create_engine 是惰性的，
        # 这里只读取 dialect.name，不会建立任何连接，更不会下发 DDL。
        leak_env_file = tmp_dir / "leak_probe.env"
        leak_env_file.write_text(
            "ADMIN_DATABASE_URL=postgresql://probe:probe@127.0.0.1:1/graphinsight_probe_only\n",
            encoding="utf-8",
        )
        probe_env = _base_env()
        probe_env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(leak_env_file)
        proc = subprocess.run(
            [sys.executable, "-c", "from admin.database import engine; print('DIALECT', engine.dialect.name)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ENV_FILE.parent), env=probe_env, timeout=120,
        )
        step(
            "守卫有效性自证（env 文件改指 PG 时方言必须变化，否则守卫是摆设）",
            proc.returncode == 0 and "DIALECT postgresql" in (proc.stdout + proc.stderr),
            f"exit={proc.returncode} {(proc.stdout + proc.stderr)[-300:]}",
        )
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
