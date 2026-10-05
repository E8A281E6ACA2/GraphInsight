#!/usr/bin/env python3
"""
M5-A 迁移/backfill 验收测试（临时 SQLite 隔离 + UTF-8 子进程捕获）

覆盖验收矩阵（设计 §17.3 / 开工令 M5-A-4）：
1. chunk_revisions 迁移 dry-run/幂等/回滚/再迁移
2. current 部分唯一索引（同 chunk 双 current 拒绝；superseded 允许）
3. admin_jobs.targets_hash 迁移幂等/部分唯一（NULL 不参与）/回滚
4. backfill 新 chunk（revision 1 + 投影状态落库）与幂等重跑
5. 已有 revision 行不降级（索引侧不被触碰，§17.1）
6. needs_reindex_targets 前置门（入队 targets_hash、重跑复用、收敛后 CLOSED）
7. blocked（能力关闭 + pending）阻断；UNRECOVERABLE_MISMATCH 拒绝（exit 2）
8. DEGRADED_SKIPPED 降级门（CLOSED_DEGRADED，不得宣布完整索引验收通过）
9. 双 KB 隔离（同 chunk_id 跨 KB 不串写）
10. §8.5 MILVUS_REVISION_FIELD_ABSENT → vector 保持 pending 并转 needs_reindex
11. 表缺失/非法 kb_id 拒绝执行；全流程 Windows/Linux UTF-8 子进程运行

隔离铁律（本次实测根因）：脚本带 backend/ 下 __file__ 时 dotenv find_dotenv 会
向上找到 backend/.env 并以 override=True 覆盖注入的 ADMIN_DATABASE_URL，
cwd/置空 GRAPHINSIGHT_BACKEND_ENV_FILE 均无效。唯一可靠做法：
GRAPHINSIGHT_BACKEND_ENV_FILE 指向"含 ADMIN_DATABASE_URL=sqlite 的临时 env 文件"，
admin/database.py 会优先加载该文件且不再回退 find_dotenv。
每个子进程还打印引擎方言，非 sqlite 立即失败（防误连开发库）。

运行：python backend/tests/check_m5a_revision_backfill.py
（依赖 sqlalchemy/dotenv；无需 Neo4j/Milvus/PG）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
FAILURES: list = []

# Windows 控制台默认 cp936，本套件打印中文断言名与子进程中文输出，必须自锁 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_env(base: dict) -> dict:
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env_file = tmp / "m5a_test.env"
        self.current_db = ""

    def use_db(self, name: str) -> str:
        db_path = (self.tmp / name).as_posix()
        url = f"sqlite:///{db_path}"
        self.env_file.write_text(f"ADMIN_DATABASE_URL={url}\n", encoding="utf-8")
        self.current_db = url
        return url

    def run(self, script: str, args: list = None, extra_env: dict = None) -> tuple:
        env = self._base_env(extra_env)
        cmd = [sys.executable, str(backend_dir / script)] + (args or [])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=env,
            timeout=180,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def _base_env(self, extra_env: dict = None) -> dict:
        env = _utf8_env(os.environ)
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self.env_file)
        env["PYTHONPATH"] = str(backend_dir)
        if extra_env:
            env.update(extra_env)
        return env

    def guard_sqlite(self) -> None:
        code, out = self.run_python_code(
            "from admin.database import engine; print('DIALECT', engine.dialect.name)"
        )
        step("引擎隔离守卫（必须 sqlite）", code == 0 and "DIALECT sqlite" in out, out[-300:])

    def run_python_code(self, code: str) -> tuple:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=self._base_env(),
            timeout=180,
        )
        return proc.returncode, proc.stdout + proc.stderr


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def parse_marker(out: str, marker: str) -> list:
    for line in out.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    return []


def parse_dry_run_result(out: str) -> dict:
    value = parse_marker(out, "DRY_RUN_RESULT ")
    return value if isinstance(value, dict) else {}


def row_of(rows: list, kb: str, chunk: str) -> dict:
    cols = ["kb_id", "chunk_id", "doc_id", "content_revision", "revision_status",
            "graph_status", "graph_content_revision", "vector_status",
            "vector_content_revision", "content", "revision_source", "reason"]
    for row in rows:
        if row[0] == kb and row[1] == chunk:
            return dict(zip(cols, row))
    return {}


AUDIT_LOG_COLS = ["action", "resource", "resource_id", "kb_id", "trace_id", "details", "status"]


def audit_logs(out: str) -> list:
    """__AUDITLOGS__ 行 → dict（details 反序列化），§16.3 留痕的逐组口径只能在 details 里看。"""
    result = []
    for row in parse_marker(out, "__AUDITLOGS__"):
        item = dict(zip(AUDIT_LOG_COLS, row))
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except (TypeError, ValueError):
            item["details"] = {}
        result.append(item)
    return result


def bootstrap_admin_jobs(h: Harness) -> tuple:
    """建出**迁移前**形状的 admin_jobs（没有 targets_hash）。

    ORM 现在声明了 targets_hash（列结构与已迁移库对齐），`create_all` 于是会把列一并建出来，
    本节要验的却是"老库升级"这条路径——列已存在时 migrate 只会打印 already exists，
    "added column" 断言与 rollback（先删索引再删列）都失去取证对象。所以建表后把列剥掉，
    顺带删掉可能挂在列上的索引：SQLite 的 DROP COLUMN 在列仍被索引引用时会直接报错。
    """
    return h.run_python_code(
        "from admin.database import Base, engine\n"
        "from admin.models import AdminJob\n"
        "from sqlalchemy import text\n"
        "Base.metadata.create_all(bind=engine, tables=[AdminJob.__table__])\n"
        "with engine.begin() as conn:\n"
        "    conn.execute(text('DROP INDEX IF EXISTS ix_admin_jobs_targets_hash'))\n"
        "    conn.execute(text('DROP INDEX IF EXISTS uq_admin_jobs_targets_hash'))\n"
        "    cols = [r[1] for r in conn.execute(text('PRAGMA table_info(admin_jobs)'))]\n"
        "    if 'targets_hash' in cols:\n"
        "        conn.execute(text('ALTER TABLE admin_jobs DROP COLUMN targets_hash'))\n"
        "    left = [r[1] for r in conn.execute(text('PRAGMA table_info(admin_jobs)'))]\n"
        "engine.dispose()\n"
        "print('bootstrap ok legacy_no_column=' + str('targets_hash' not in left))\n"
    )


def bootstrap_admin_logs(h: Harness) -> tuple:
    """建 admin_logs：run() 的 §16.3 留痕落这张表，缺表时退出码必须是 4（不是 3）。"""
    return h.run_python_code(
        "from admin.database import Base, engine\n"
        "from admin.models import AdminLog\n"
        "Base.metadata.create_all(bind=engine, tables=[AdminLog.__table__])\n"
        "engine.dispose()\n"
        "print('bootstrap admin_logs ok')\n"
    )


def prep_db(h: Harness, name: str, *, with_admin_logs: bool = True) -> None:
    """切到独立 SQLite 库并完成建表：chunk_revisions / admin_jobs / targets_hash / admin_logs。

    每个场景组必须有独立库：backfill 的 universe 现在包含该 kb 全部 current 行，
    共库会让前一场景的行被后续场景判为孤儿 revision，污染断言。
    `with_admin_logs=False` 只在"留痕表缺失 → 退出码 4"那条守卫里用。
    """
    h.use_db(name)
    code, out = h.run(str(Path("admin") / "migrate_chunk_revisions.py"), ["--action", "migrate"])
    step(f"{name}：迁移 chunk_revisions", code == 0, f"exit={code} " + out[-300:])
    code, out = bootstrap_admin_jobs(h)
    step(f"{name}：引导 admin_jobs", code == 0 and "bootstrap ok legacy_no_column=True" in out, f"exit={code} " + out[-300:])
    code, out = h.run(str(Path("admin") / "migrate_jobs_targets_hash.py"), ["--action", "migrate"])
    step(f"{name}：迁移 targets_hash", code == 0, f"exit={code} " + out[-300:])
    if with_admin_logs:
        code, out = bootstrap_admin_logs(h)
        step(f"{name}：引导 admin_logs", code == 0 and "bootstrap admin_logs ok" in out, f"exit={code} " + out[-300:])


# ---------------------------------------------------------------------------
# A. chunk_revisions 迁移
# ---------------------------------------------------------------------------


def section_a(h: Harness) -> None:
    print("[A] migrate_chunk_revisions（幂等/回滚/部分唯一索引）")
    h.use_db("mig_a.db")
    script = str(Path("admin") / "migrate_chunk_revisions.py")

    code, out = h.run(script, ["--dry-run"])
    dry = parse_dry_run_result(out)
    step(
        "dry-run 退出 0 且写计划",
        code == 0
        and "dry-run completed" in out
        and "计划动作: migrate" in out
        and dry.get("contract_version") == 1
        and dry.get("operation") == "migrate_chunk_revisions"
        and dry.get("writes") == 0
        and dry.get("exit_code") == 0,
        out[-500:],
    )
    code, out = h.run_python_code(
        "from admin.database import engine;"
        "from sqlalchemy import text;"
        "c = engine.connect();"
        "print('TABLES', [r[0] for r in c.execute(text(\"SELECT name FROM sqlite_master WHERE type='table'\"))])"
    )
    step("dry-run 未建表", code == 0 and "chunk_revisions" not in out, out[-300:])

    code, out = h.run(script, ["--action", "migrate"])
    step("首次 migrate", code == 0 and "table is ready" in out, out[-300:])
    step(
        "迁移后自动结构校验：列/约束/索引全项通过",
        code == 0 and "[schema-check] chunk_revisions" in out and "✗" not in out.split("[schema-check]")[1],
        out[-400:],
    )
    step("结构校验覆盖 UNIQUE 约束与部分唯一索引谓词", "✓ UNIQUE 约束 uq_chunk_revisions_rev" in out and "✓ 索引 uq_chunk_revisions_current 部分谓词" in out, out[-400:])
    code, out = h.run(script, ["--action", "migrate"])
    step("重复 migrate 幂等", code == 0 and "already exists" in out and "indexes ensured" in out, out[-300:])

    # 负向自证：人为删除一个索引后，独立结构校验必须失败（否则校验等于没做）
    code, out = h.run_python_code(
        "from admin.database import engine\n"
        "from sqlalchemy import text\n"
        "with engine.begin() as c:\n"
        "    c.execute(text('DROP INDEX idx_chunk_rev_kb_doc_graph'))\n"
        "engine.dispose()\n"
        "print('dropped')\n"
    )
    step("负向铺垫：删除 idx_chunk_rev_kb_doc_graph", code == 0 and "dropped" in out, out[-300:])
    code, out = h.run(str(Path("admin") / "m5a_schema_check.py"), ["chunk_revisions"])
    step("结构校验能抓到缺失索引（exit 1）", code == 1 and "idx_chunk_rev_kb_doc_graph 存在" in out and "FAILED" in out, f"exit={code} " + out[-400:])
    code, out = h.run(script, ["--action", "migrate"])
    step(
        "补齐缺失索引后校验恢复全绿",
        code == 0 and "✗" not in out and "✓ 索引 idx_chunk_rev_kb_doc_graph 列顺序" in out,
        f"exit={code} " + out[-400:],
    )

    code, out = h.run(str(Path("tests") / "m5a_backfill_driver.py"), ["--scenario", "current_unique"])
    rows = parse_marker(out, "__ROWS__")
    step("current 部分唯一索引：双 current 拒绝", code == 0 and "__DUP_CURRENT_REJECTED__True" in out, out[-400:])
    step("superseded 历史行允许", "__SUPERSEDED_ALLOWED__True" in out and len(rows) == 2, out[-400:])

    code, out = h.run(script, ["--action", "rollback"])
    step("rollback 整表删除", code == 0 and "rollback completed" in out, out[-300:])
    code, out = h.run_python_code(
        "from admin.database import engine;"
        "from sqlalchemy import text;"
        "c = engine.connect();"
        "print('STILL', c.execute(text(\"SELECT name FROM sqlite_master WHERE name='chunk_revisions'\")).fetchall())"
    )
    step("rollback 后表不存在", code == 0 and "STILL []" in out, out[-300:])
    code, out = h.run(script, ["--action", "migrate"])
    step("rollback 后再 migrate", code == 0 and "table is ready" in out, out[-300:])


# ---------------------------------------------------------------------------
# B. admin_jobs.targets_hash 迁移
# ---------------------------------------------------------------------------


def section_b(h: Harness) -> None:
    print("[B] migrate_jobs_targets_hash（幂等/部分唯一/回滚）")
    h.use_db("mig_b.db")
    code, out = bootstrap_admin_jobs(h)
    step("admin_jobs 引导", code == 0 and "bootstrap ok legacy_no_column=True" in out, out[-300:])
    script = str(Path("admin") / "migrate_jobs_targets_hash.py")

    code, out = h.run(script, ["--dry-run"])
    dry = parse_dry_run_result(out)
    step(
        "dry-run",
        code == 0
        and "dry-run completed" in out
        and dry.get("contract_version") == 1
        and dry.get("operation") == "migrate_jobs_targets_hash"
        and dry.get("writes") == 0,
        out[-500:],
    )
    code, out = h.run(script, ["--action", "migrate"])
    step("首次 migrate 加列+索引", code == 0 and "added column" in out and "ensured index" in out, out[-300:])
    step(
        "targets_hash 结构校验：列类型/可空 + 部分唯一索引全绿",
        code == 0
        and "[schema-check] admin_jobs.targets_hash" in out
        and "✓ 列 targets_hash 类型 varchar" in out
        and "✓ 列 targets_hash 可空" in out
        and "✓ 索引 targets_hash 列顺序" in out
        and "✓ 索引 targets_hash 部分谓词" in out
        and "✗" not in out.split("[schema-check]")[1],
        out[-500:],
    )
    code, out = h.run(script, ["--action", "migrate"])
    step("重复 migrate 幂等", code == 0 and "targets_hash already exists" in out and "index uq_admin_jobs_targets_hash already exists" in out, out[-300:])

    dup_code = (
        "from admin.database import engine\n"
        "from sqlalchemy import text\n"
        "ins = 'INSERT INTO admin_jobs (job_type, status, kb_id, targets_hash, retry_count, max_retries) "
        "VALUES (:a, :b, :c, :d, 0, 3)'\n"
        "dup = 0\n"
        "with engine.begin() as conn:\n"
        "    conn.execute(text(ins), {'a': 'reindex_chunks', 'b': 'pending', 'c': 'kb1', 'd': 'h' * 64})\n"
        "with engine.begin() as conn:\n"
        "    try:\n"
        "        conn.execute(text(ins), {'a': 'reindex_chunks', 'b': 'failed', 'c': 'kb1', 'd': 'h' * 64})\n"
        "    except Exception:\n"
        "        dup = 1\n"
        "with engine.begin() as conn:\n"
        "    conn.execute(text(ins), {'a': 'reindex_chunks', 'b': 'pending', 'c': 'kb1', 'd': None})\n"
        "    conn.execute(text(ins), {'a': 'build_graph', 'b': 'pending', 'c': 'kb1', 'd': None})\n"
        "    print('NULL_ROWS', len(conn.execute(text('SELECT id FROM admin_jobs WHERE targets_hash IS NULL')).fetchall()))\n"
        "print('DUP_REJECTED', dup)\n"
    )
    code, out = h.run_python_code(dup_code)
    step("targets_hash 部分唯一：同 hash 拒绝（含 failed 状态）", code == 0 and "DUP_REJECTED 1" in out, out[-300:])
    step("历史 NULL 行不参与唯一性", "NULL_ROWS 2" in out, out[-300:])

    code, out = h.run(script, ["--action", "rollback"])
    step("rollback 先删索引再删列", code == 0 and "dropped index" in out and "dropped column" in out, out[-300:])
    code, out = h.run(script, ["--action", "migrate"])
    step("rollback 后再 migrate", code == 0 and "added column" in out, out[-300:])


# ---------------------------------------------------------------------------
# C. backfill 场景
# ---------------------------------------------------------------------------


def section_c(h: Harness) -> None:
    print("[C] backfill_chunk_revisions 场景矩阵")
    driver = str(Path("tests") / "m5a_backfill_driver.py")

    prep_db(h, "bf_new.db")
    code, out = h.run(driver, ["--scenario", "new_and_degraded"])
    rows = parse_marker(out, "__ROWS__")
    r = row_of(rows, "kb-a", "c1")
    step("新 chunk：revision 1 current 落库", code == 0 and r.get("content_revision") == 1 and r.get("revision_status") == "current", f"exit={code} row={r}")
    step("新 chunk：graph indexed/1，vector skipped/NULL", r.get("graph_status") == "indexed" and r.get("graph_content_revision") == 1 and r.get("vector_status") == "skipped" and r.get("vector_content_revision") is None, str(r))
    step("新 chunk：system_initial + backfill_m5a + 中文内容 UTF-8", r.get("revision_source") == "system_initial" and r.get("reason") == "backfill_m5a" and r.get("content") == "内容一", str(r))
    step("降级门：DEGRADED_SKIPPED + CLOSED_DEGRADED + 不得宣布完整验收", "DEGRADED_SKIPPED" in out and "CLOSED_DEGRADED" in out and "不得宣布完整索引验收通过" in out, out[-400:])
    step("rows_skipped_existing 输出存在（新库首轮为 0）", "rows_skipped_existing=0" in out and "insert_conflicts_skipped=0" in out, out[-300:])

    code, out = h.run(driver, ["--scenario", "new_and_degraded_rerun"])
    step("幂等重跑：rows_new=0 且门状态不变", code == 0 and "rows_new=0" in out and "CLOSED_DEGRADED" in out, f"exit={code} " + out[-300:])
    step("幂等重跑：rows_skipped_existing=1（决策时已有行）且不重复插入（insert_conflicts_skipped=0）",
         "rows_skipped_existing=1" in out and "insert_conflicts_skipped=0" in out, out[-300:])

    prep_db(h, "bf_nd.db")
    code, out = h.run(driver, ["--scenario", "no_downgrade"])
    rows = parse_marker(out, "__ROWS__")
    calls = parse_marker(out, "__CALLS__")
    e1 = row_of(rows, "kb-a", "e1")
    n1 = row_of(rows, "kb-a", "n1")
    step("已有 revision 不降级：索引侧仅触碰新 chunk", code == 0 and calls == ["n1"], f"exit={code} calls={calls}")
    step("已有行原样保留（indexed/1、内容不变）", e1.get("graph_status") == "indexed" and e1.get("graph_content_revision") == 1 and e1.get("vector_content_revision") == 1 and e1.get("content") == "E-keep" and e1.get("doc_id") == "d9", str(e1))
    step("新行 n1 正常落库", n1.get("content") == "N-new" and n1.get("graph_status") == "indexed", str(n1))
    step("已有 revision 行计入 rows_skipped_existing=1", "rows_skipped_existing=1" in out, out[-300:])

    prep_db(h, "bf_nr.db")
    code, out = h.run(driver, ["--scenario", "nr_setup"])
    step("needs_reindex 场景播种", code == 0, out[-300:])
    code, out = h.run(driver, ["--scenario", "nr_dry_preview"])
    jobs = parse_marker(out, "__JOBS__")
    dry = parse_dry_run_result(out)
    step(
        "dry-run：targets 预览输出且不写库（exit 0、jobs=0）",
        code == 0
        and "dry-run preview" in out
        and "chunk_id=x1" in out
        and jobs == []
        and dry.get("contract_version") == 1
        and dry.get("operation") == "backfill_chunk_revisions"
        and dry.get("writes") == 0
        and dry.get("status") == "OPEN"
        and audit_logs(out) == [],
        f"exit={code} jobs={len(jobs)} dry={dry}",
    )
    code, out = h.run(driver, ["--scenario", "nr_run"])
    jobs = parse_marker(out, "__JOBS__")
    step("needs_reindex 前置门 OPEN（exit 3）", code == 3 and "[gate] OPEN" in out, f"exit={code}")
    step("reindex job 入队（targets_hash 64 位、payload 含 current targets）", len(jobs) == 1 and jobs[0][3] and len(str(jobs[0][3])) == 64 and '"chunk_id": "x1"' in str(jobs[0][4]) and '"target_revision": 1' in str(jobs[0][4]), str(jobs))
    first_logs = audit_logs(out)
    step(
        "缺口 1：run() 把逐组 §16.3 结果写进 admin_logs（1 行 job_created，不是只有 stdout 计数）",
        len(first_logs) == 1
        and first_logs[0]["action"] == "job_created"
        and first_logs[0]["resource"] == "job"
        and first_logs[0]["status"] == "success"
        and first_logs[0]["kb_id"] == "kb-a"
        and str(first_logs[0]["trace_id"] or "").startswith("backfill-kb-a-"),
        f"logs={first_logs}",
    )
    fd = first_logs[0]["details"] if first_logs else {}
    step(
        "缺口 2：details 用 created/child_job_id/targets_hash 规范键名（旧名 enqueued/job_id 不得出现）",
        fd.get("outcome") == "created"
        and fd.get("child_job_id") is not None
        and fd.get("targets_hash") == jobs[0][3]
        and fd.get("job_type") == "reindex_chunks"
        and fd.get("source") == "backfill_chunk_revisions"
        and fd.get("created") == 1
        and fd.get("reused") == 0
        and first_logs[0]["resource_id"] == str(fd.get("child_job_id"))
        and "enqueued" not in fd
        and "job_id" not in fd,
        f"details={fd} jobs={jobs}",
    )
    code, out = h.run(driver, ["--scenario", "nr_run_again"])
    jobs = parse_marker(out, "__JOBS__")
    step("重跑 targets_hash 复用不新建", code == 3 and "jobs_reused=1" in out and len(jobs) == 1, f"exit={code} jobs={len(jobs)}")
    again_logs = audit_logs(out)
    step(
        "缺口 1/2：复用轮追加 job_reused 行并回读同一 child_job_id（留痕到实例，不只聚合数）",
        len(again_logs) == 2
        and again_logs[1]["action"] == "job_reused"
        and again_logs[1]["details"].get("outcome") == "reused"
        and again_logs[1]["details"].get("child_job_id") == fd.get("child_job_id")
        and again_logs[1]["details"].get("reused") == 1
        and again_logs[1]["details"].get("created") == 0,
        f"logs={again_logs}",
    )
    code, _ = h.run(driver, ["--scenario", "nr_finish"])
    step("模拟 reindex 完成", code == 0, f"exit={code}")
    code, out = h.run(driver, ["--scenario", "nr_converged"])
    step("收敛后前置门 CLOSED（exit 0）", code == 0 and "gate] CLOSED" in out, f"exit={code}")

    # 留痕表缺失必须让退出码变 4：admin_logs 不存在时若仍返回 3，"留痕已落盘"就又被
    # 降级成一句 stdout，§16.3 实例级取证重新不可复查（P5 缺口 1 的反面守卫）。
    prep_db(h, "bf_audit_missing.db", with_admin_logs=False)
    code, out = h.run(driver, ["--scenario", "nr_setup"])
    step("留痕缺失场景：needs_reindex 播种", code == 0, out[-300:])
    code, out = h.run(driver, ["--scenario", "nr_run"])
    step(
        "守卫：admin_logs 表缺失 → 退出码 4 且显式报 REINDEX_AUDIT_WRITE_FAILED（不静默、不返 3）",
        code == 4 and "REINDEX_AUDIT_WRITE_FAILED" in out and "admin_logs 表不存在" in out,
        f"exit={code} " + out[-400:],
    )

    prep_db(h, "bf_blocked.db")
    code, out = h.run(driver, ["--scenario", "blocked_gate"])
    step("blocked：能力关闭+pending 阻断（exit 3，blocked=1）", code == 3 and "blocked=1" in out and "needs_reindex=0" in out, f"exit={code} " + out[-300:])

    prep_db(h, "bf_unrec.db")
    code, out = h.run(driver, ["--scenario", "unrecoverable_dry"])
    dry = parse_dry_run_result(out)
    step(
        "UNRECOVERABLE_MISMATCH：dry-run 拒绝（exit 2）",
        code == 2
        and "UNRECOVERABLE_MISMATCH" in out
        and dry.get("status") == "rejected"
        and dry.get("exit_code") == 2
        and dry.get("writes") == 0,
        f"exit={code} dry={dry} " + out[-300:],
    )

    prep_db(h, "bf_dual.db")
    code, out = h.run(driver, ["--scenario", "dual_kb"])
    rows = parse_marker(out, "__ROWS__")
    b = row_of(rows, "kb-b", "z9")
    a = row_of(rows, "kb-a", "z9")
    step("双 KB 隔离：kb-a 新行落库", code == 0 and a.get("content") == "A-new", str(a))
    step("双 KB 隔离：同 chunk_id 的 kb-b 行不被改写", b.get("content") == "B-keep" and b.get("graph_status") == "indexed" and b.get("doc_id") == "db1", str(b))
    jobs = parse_marker(out, "__JOBS__")
    step("双 KB 隔离：无 kb-b 任务写入", all(str(j[1]) == "kb-a" for j in jobs), str(jobs))

    prep_db(h, "bf_rfa.db")
    code, out = h.run(driver, ["--scenario", "rfa_run"])
    rows = parse_marker(out, "__ROWS__")
    rfa = row_of(rows, "kb-a", "f1")
    step("§8.5 REVISION_FIELD_ABSENT：vector 保持 pending 不伪标", "MILVUS_REVISION_FIELD_ABSENT" in out and rfa.get("vector_status") == "pending" and rfa.get("vector_content_revision") is None, f"exit={code} {rfa}")
    step("§8.5 pending 转 needs_reindex 阻断门（exit 3）", code == 3 and "needs_reindex=1" in out, f"exit={code}")
    rfa_jobs = parse_marker(out, "__JOBS__")
    step(
        "同一轮必须为未收敛的新 chunk 排队（禁止报告 needs_reindex 却零 job）",
        "jobs_created=1" in out and "targets_total=1" in out and len(rfa_jobs) == 1 and "f1" in str(rfa_jobs),
        f"jobs={rfa_jobs}",
    )
    code, out = h.run(driver, ["--scenario", "rfa_rerun"])
    rfa_jobs = parse_marker(out, "__JOBS__")
    step(
        "pending 目标重跑复用 targets_hash 不新建 job",
        code == 3 and "jobs_reused=1" in out and len(rfa_jobs) == 1,
        f"exit={code} jobs={len(rfa_jobs)}",
    )

    prep_db(h, "bf_orphan.db")
    code, out = h.run(driver, ["--scenario", "orphan_gate"])
    rows = parse_marker(out, "__ROWS__")
    o1 = row_of(rows, "kb-a", "o1")
    step("修复#1 孤儿 revision 纳入 inventory：exit 3 且 orphan_revisions=1", code == 3 and "orphan_revisions=1" in out, f"exit={code} " + out[-400:])
    step("修复#1 孤儿 revision 显式列名且计入 blocked（非静默跳过）", "ORPHAN_REVISION chunk_ids" in out and "o1" in out and "blocked=1" in out, out[-400:])
    step("修复#1 孤儿行仍计入 rows_skipped_existing=1", "rows_skipped_existing=1" in out and o1.get("chunk_id") == "o1", out[-300:])
    code, out = h.run(driver, ["--scenario", "orphan_converged"])
    step("修复#1 补回索引证据后 orphan 归零、门可关闭（exit 0）", code == 0 and "orphan_revisions=0" in out and "gate] CLOSED" in out, f"exit={code} " + out[-400:])

    prep_db(h, "bf_uno.db")
    code, out = h.run(driver, ["--scenario", "unrecoverable_neo_only"])
    rows = parse_marker(out, "__ROWS__")
    step("修复#2 仅 Neo4j 有文本判 UNRECOVERABLE（exit 2）", code == 2 and "UNRECOVERABLE_MISMATCH" in out and "n9" in out, f"exit={code} " + out[-400:])
    step("修复#2 拒绝路径零写入", rows == [], str(rows))

    prep_db(h, "bf_scope1.db")
    code, out = h.run(driver, ["--scenario", "scope_conflict_new"])
    rows = parse_marker(out, "__ROWS__")
    jobs = parse_marker(out, "__JOBS__")
    calls = parse_marker(out, "__CALLS__")
    step("修复#4 新 chunk tenant 与 KB 登记冲突 → fail-closed（exit 2）", code == 2 and "SCOPE_MISMATCH" in out, f"exit={code} " + out[-400:])
    step("修复#4 冲突明细含 expected/actual", "chunk_id=g1" in out and "field=tenant_id" in out and "expected=t1" in out and "actual=t9" in out, out[-400:])
    step("修复#4 冲突时零写入（PG 行、job、索引调用全空）", rows == [] and jobs == [] and calls == [], f"rows={rows} jobs={jobs} calls={calls}")

    prep_db(h, "bf_scope2.db")
    code, out = h.run(driver, ["--scenario", "scope_conflict_row"])
    rows = parse_marker(out, "__ROWS__")
    calls = parse_marker(out, "__CALLS__")
    step("修复#4 已有行 project 与 KB 登记冲突 → fail-closed（exit 2）", code == 2 and "SCOPE_MISMATCH" in out and "field=revision.project_id" in out and "expected=p1" in out and "actual=pX" in out, f"exit={code} " + out[-400:])
    step("修复#4 已有行冲突同样零新增写入", all(r[1] == "g2" for r in rows) and len(rows) == 1 and calls == [], f"rows={rows} calls={calls}")

    prep_db(h, "bf_scope3.db")
    code, out = h.run(driver, ["--scenario", "new_and_degraded"])
    step("无 KB 登记时不误判冲突（SCOPE_WARNING 降级但正常执行）", code == 0 and "SCOPE_WARNING" in out, f"exit={code} " + out[-300:])

    # 活栈纠偏 1：作用域三件套不全必须独立成 SCOPE_UNRESOLVED，不混进 UNRECOVERABLE_MISMATCH
    prep_db(h, "bf_scope4")
    code, out = h.run(driver, ["--scenario", "scope_unresolved"])
    rows = parse_marker(out, "__ROWS__")
    step("活栈纠偏：三件套不全判 SCOPE_UNRESOLVED 且 fail-closed（exit 2）",
         code == 2 and "SCOPE_UNRESOLVED" in out and "scope_unresolved=1" in out, f"exit={code} " + out[-300:])
    step("活栈纠偏：内容可恢复的 chunk 不再误标 UNRECOVERABLE_MISMATCH",
         "unrecoverable=0" in out and "UNRECOVERABLE_MISMATCH chunk_ids" not in out, out[-300:])
    step("活栈纠偏：SCOPE_UNRESOLVED 同样零写入", rows == [], str(rows))

    # 活栈纠偏 2：collection 名必须共用 services.vector_store 的归一化（配置历史名指向不存在的库）
    code, out = h.run(driver, ["--scenario", "collection_resolution"])
    step("backfill 归一化历史 collection 名 graphinsight_chunks → _v2",
         code == 0 and "__COLL_LEGACY__graphinsight_chunks_v2" in out, f"exit={code} " + out[-200:])
    step("显式非历史 collection 名保持原样（不擅自改写）",
         "__COLL_EXPLICIT__kb_scope_coll_1536" in out, out[-200:])


# ---------------------------------------------------------------------------
# D. CLI 拒绝路径（真实入口 argparse/main）
# ---------------------------------------------------------------------------


def section_e2(h: Harness) -> None:
    driver = str(Path("tests") / "m5a_backfill_driver.py")
    prep_db(h, "bf_direct_agg.db")
    code, out = h.run(driver, ["--scenario", "direct_document_aggregation"])
    docs = parse_marker(out, "__DOCS__")
    doc = next((row for row in docs if row[1] == "d-direct"), None)
    step("backfill 直接路径写入后聚合文档状态", code == 0 and doc is not None and doc[2] == "indexed" and doc[3] == "stale", f"exit={code} docs={docs}")

    prep_db(h, "bf_direct_cas.db")
    code, out = h.run(driver, ["--scenario", "direct_cas_failure"])
    rows = parse_marker(out, "__ROWS__")
    docs = parse_marker(out, "__DOCS__")
    doc = next((row for row in docs if row[1] == "d-cas"), None)
    step("backfill 检查 CAS rowcount=1，失败时保持 OPEN", code == 3 and "state_write_failed=cas-1" in out, f"exit={code} out={out[-500:]}")
    step("backfill CAS 失败不伪装 indexed", rows and rows[0][1] == "cas-1" and rows[0][5] == "pending", f"rows={rows}")
    step("backfill CAS 失败仍聚合文档为 pending", doc is not None and doc[2] == "pending" and doc[3] == "pending", f"docs={docs}")

    prep_db(h, "bf_agg_error.db")
    code, out = h.run(driver, ["--scenario", "backfill_document_aggregation_failure"])
    aggregation_error = parse_marker(out, "__AGGREGATION_ERROR__")
    step(
        "backfill 文档聚合异常向上抛出且不报告 CLOSED",
        code == 1
        and aggregation_error.get("type") == "RuntimeError"
        and "document aggregation database unavailable" in str(aggregation_error.get("message"))
        and "✓ backfill 完成，前置门 CLOSED" not in out,
        f"exit={code} error={aggregation_error} out={out[-500:]}",
    )


def section_d(h: Harness) -> None:
    print("[D] backfill CLI 拒绝路径")
    script = str(Path("admin") / "backfill_chunk_revisions.py")

    h.use_db("cli_missing.db")
    code, out = h.run(script, ["--kb", "kb-a", "--dry-run"])
    dry = parse_dry_run_result(out)
    step(
        "表缺失拒绝执行（exit 2）",
        code == 2
        and "chunk_revisions 表不存在" in out
        and dry.get("status") == "rejected"
        and dry.get("details", {}).get("reason") == "MIGRATION_REQUIRED"
        and dry.get("writes") == 0,
        f"exit={code} dry={dry} " + out[-300:],
    )

    h.use_db("mig_a.db")  # 已有 chunk_revisions 表（引擎隔离已在 guard 验证）
    code, out = h.run(script, ["--kb", "BAD KB!!"])
    step("非法 kb_id 拒绝（exit 2，SCOPE_INVALID）", code == 2 and "kb_id 非法" in out, f"exit={code} " + out[-300:])
    code, out = h.run(script, ["--dry-run"])
    step("--kb 必填（argparse 拒绝）", code != 0 and "required" in out.lower(), f"exit={code} " + out[-200:])


def main() -> int:
    print("=" * 60)
    print("GraphInsight M5-A migration/backfill acceptance tests (sqlite)")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        h.use_db("guard.db")
        h.guard_sqlite()
        section_a(h)
        section_b(h)
        section_c(h)
        section_e2(h)
        section_d(h)
    print("-" * 60)
    if FAILURES:
        for item in FAILURES:
            print(f"  FAILED: {item}")
        print(f"✗ {len(FAILURES)} checks failed")
        return 1
    print("✓ all M5-A acceptance checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
