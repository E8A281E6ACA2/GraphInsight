#!/usr/bin/env python3
"""P6 一次性 Postgres 集成套件编排器（宿主侧，D3 已批口径）。

为什么必须有这一轮：§16.3 复用分支在 Postgres 下加 `FOR UPDATE` 行锁，既有 20 项门禁
全跑 SQLite，那条分支从未被执行；缺列 → 42703 也只在真 PG 上造得出。本套件因此需要
真 Postgres，但只能在一次性集群里跑。

隔离口径（逐条对应 D3）：
  - 一次性 network `gi-p6-net`，一次性容器 `gi-p6-pg` / `gi-p6-py`，全部不发布宿主端口；
  - Go 用例用 `docker run --rm --network gi-p6-net` 临时进程，不常驻；
  - 收尾 `rm -f` + `network rm`，再按名字复查容器/网络零残留；
  - 预检发现任何 `gi-p6*` 同名对象即拒绝执行，绝不覆盖别人的东西；
  - 共享栈（`graphinsight_default`、`gi-phase3-*`）只记录前后状态以证明未被动过。

编排顺序（每步都是独立子进程，断言真实退出码）：
  bootstrap → 真实迁移脚本 --action rollback → assert-old-shape → Go pre_migrate
  → 真实迁移脚本 --action migrate → assert-migrated → submit → Go post_migrate → dump

本脚本不进 20 项统一门禁：门禁必须能在无 Docker 的 CI 里跑；本轮作为独立的
disposable 取证入口（`python backend/tests/check_m5_p6_disposable_pg.py`）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_MOUNT = str(REPO_ROOT).replace("\\", "/")
GO_MOUNT_ROOT = str(Path.home()).replace("\\", "/") + "/go"

NET = "gi-p6-net"
PG = "gi-p6-pg"
PY = "gi-p6-py"
PG_IMAGE = "postgres:16-alpine"
PY_IMAGE = "gi-p6-py:tmp"
GO_IMAGE = "golang:1.27"
DSN = "postgresql://p6@gi-p6-pg:5432/p6_admin?sslmode=disable"
ENV_FILE = "/tmp/p6_admin.env"
DRIVER = "/src/backend/tests/p6_disposable_pg_driver.py"
MIGRATE = "/src/backend/admin/migrate_jobs_targets_hash.py"
KB_ID = "kb-p6"
TENANT_ID = "t1"
PROJECT_ID = "p1"

MARKER_RE = re.compile(r"^__([A-Z_]+)__ (.*)$", re.MULTILINE)
GO_TEST_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.MULTILINE)

FAILURES: List[str] = []
CRITERIA: Dict[str, Tuple[bool, str]] = {}
LOG: List[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    LOG.append(line)


def check(name: str, ok: bool, detail: str = "") -> bool:
    say(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def criterion(idx: int, title: str, ok: bool, evidence: str) -> None:
    CRITERIA[f"C{idx}"] = (ok, f"{title} | {evidence}")


def run(args: List[str], *, timeout: int = 300, label: str = "") -> Tuple[int, str]:
    """执行子进程并回读真实退出码；输出合并 stdout+stderr。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        say(f"  ✗ {label or ' '.join(args)} 超时（>{timeout}s）")
        return 124, ""
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def docker(*args: str, timeout: int = 300, label: str = "") -> Tuple[int, str]:
    return run(["docker", *args], timeout=timeout, label=label or " ".join(args[:2]))


def markers(out: str) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for name, raw in MARKER_RE.findall(out):
        try:
            found[name] = json.loads(raw)
        except json.JSONDecodeError:
            found[name] = {"_unparsed": raw}
    return found


def show_tail(out: str, *, limit: int = 40) -> None:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in lines[-limit:]:
        say(f"    | {ln}")


def dump_out(out: str, rc: int, ok_limit: int = 14) -> None:
    show_tail(out, limit=ok_limit if rc == 0 else 400)


# ── 预检 ─────────────────────────────────────────────────────────────

def preflight() -> bool:
    say("── 预检：Docker 与镜像 ──")
    rc, out = docker("version", timeout=60, label="docker version")
    if not check("docker 可用", rc == 0, out.strip()[:200]):
        return False
    for image in (PG_IMAGE, PY_IMAGE, GO_IMAGE):
        rc, out = docker(
            "image", "inspect", image, "--format", "{{.Id}}", timeout=60, label=f"inspect {image}"
        )
        check(f"镜像已存在 {image}（不临时拉取）", rc == 0, out.strip()[:200])
    if FAILURES:
        return False

    say("── 预检：一次性命名零冲突 ──")
    rc, out = docker("ps", "-a", "--filter", "name=gi-p6", "--format", "{{.Names}}", timeout=60)
    check("无同名 gi-p6* 容器（不会覆盖他人对象）", rc == 0 and not out.strip(), out.strip())
    rc, out = docker("network", "ls", "--filter", "name=gi-p6", "--format", "{{.Name}}", timeout=60)
    check("无同名 gi-p6* 网络", rc == 0 and not out.strip(), out.strip())

    say("── 预检：共享栈基线（本轮不碰，收尾比对）──")
    rc, out = docker("network", "inspect", "graphinsight_default", "--format", "{{.Name}}", timeout=60)
    SHARED["graphinsight_default"] = rc == 0
    say(f"    graphinsight_default 存在={rc == 0}")
    rc, out = docker("ps", "--filter", "name=gi-phase3-pg", "--format", "{{.Names}}", timeout=60)
    SHARED["gi-phase3-pg"] = bool(out.strip())
    say(f"    gi-phase3-pg 运行中={bool(out.strip())}")
    return not FAILURES


SHARED: Dict[str, bool] = {}


# ── 容器与网络 ───────────────────────────────────────────────────────

def bring_up() -> bool:
    say("── 启动一次性网络与容器（全部不发布宿主端口）──")
    rc, out = docker("network", "create", "--driver", "bridge", NET, timeout=60, label="network create")
    if not check(f"创建 network {NET}", rc == 0, out.strip()):
        return False

    rc, out = docker(
        "run", "-d", "--name", PG, "--network", NET,
        "-e", "POSTGRES_USER=p6",
        "-e", "POSTGRES_DB=p6_admin",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
        PG_IMAGE,
        timeout=180, label="run postgres",
    )
    if not check(f"启动 {PG}（trust 认证 → DSN 无凭据，不发布端口）", rc == 0, out.strip()):
        return False

    rc, out = docker(
        "run", "-d", "--name", PY, "--network", NET,
        "-v", f"{SRC_MOUNT}:/src:ro",
        "-w", "/tmp",
        PY_IMAGE, "sleep", "7200",
        timeout=180, label="run python",
    )
    if not check(f"启动 {PY}（挂载只读，工作目录 /tmp 以远离 backend/.env）", rc == 0, out.strip()):
        return False

    # 不发布宿主端口的直接证据：docker port 为空
    for name in (PG, PY):
        rc, out = docker("port", name, timeout=60)
        check(f"{name} 未发布任何宿主端口", rc == 0 and not out.strip(), out.strip())

    # 钉连接：先把 env 文件写进容器，再回读校验内容唯一
    body = f"ADMIN_DATABASE_URL={DSN}\n"
    rc, out = docker(
        "exec", PY, "sh", "-c", f"printf '%s' {json.dumps(body)} > {ENV_FILE}",
        timeout=60, label="write env file",
    )
    if not check(f"写入容器内 {ENV_FILE}", rc == 0, out.strip()):
        return False
    rc, out = docker("exec", PY, "cat", ENV_FILE, timeout=60)
    text_ok = out.count("ADMIN_DATABASE_URL=") == 1 and DSN in out
    check("回读 env 文件：单条 ADMIN_DATABASE_URL 且指向一次性集群", text_ok, out.strip())

    say("── 等待 Postgres 就绪 ──")
    ready = False
    for _ in range(60):
        rc, _ = docker("exec", PG, "pg_isready", "-U", "p6", "-d", "p6_admin", timeout=30)
        if rc == 0:
            ready = True
            break
        time.sleep(1)
    if not check("pg_isready 就绪（60s 内）", ready):
        return False
    return not FAILURES


def py_stage(stage: str, extra: List[str] = (), timeout: int = 240) -> Tuple[int, str]:
    return docker(
        "exec", "-w", "/tmp",
        "-e", f"GI_P6_PG_DSN={DSN}",
        "-e", f"GI_P6_ENV_FILE={ENV_FILE}",
        "-e", f"GRAPHINSIGHT_BACKEND_ENV_FILE={ENV_FILE}",
        "-e", "PYTHONIOENCODING=utf-8",
        PY, "python", DRIVER, "--stage", stage, *extra,
        timeout=timeout, label=f"driver {stage}",
    )


def migrate(action: str) -> Tuple[int, str]:
    return docker(
        "exec", "-w", "/tmp",
        "-e", f"GRAPHINSIGHT_BACKEND_ENV_FILE={ENV_FILE}",
        "-e", "PYTHONIOENCODING=utf-8",
        PY, "python", MIGRATE, "--action", action,
        timeout=240, label=f"migrate {action}",
    )


def go_phase(phase: str, env: Dict[str, str]) -> Tuple[int, str, Dict[str, str]]:
    args = [
        "run", "--rm", "--network", NET,
        "-v", f"{SRC_MOUNT}:/src",
        "-v", f"{GO_MOUNT_ROOT}:/go",
        "-w", "/src/go-backend",
        "-e", "GOPROXY=off",
        "-e", "GOFLAGS=-mod=mod",
        "-e", f"GI_P6_PG_DSN={DSN}",
        "-e", f"GI_P6_PHASE={phase}",
        "-e", f"GI_P6_KB_ID={KB_ID}",
        "-e", f"GI_P6_TENANT_ID={TENANT_ID}",
        "-e", f"GI_P6_PROJECT_ID={PROJECT_ID}",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [GO_IMAGE, "go", "test", "./internal/httpserver/", "-run", "TestP6", "-count=1", "-v"]
    rc, out = docker(*args, timeout=900, label=f"go test {phase}")
    verdicts = {name: status for status, name in GO_TEST_RE.findall(out)}
    return rc, out, verdicts


# ── 阶段执行 ─────────────────────────────────────────────────────────

def main() -> int:
    if not preflight():
        say("预检未通过，拒绝开始（不创建任何对象）")
        return finish()

    try:
        if not bring_up():
            say("容器未就绪，跳过后续阶段")
            return finish()

        say("── 阶段 1：bootstrap（建 admin_jobs 新形态 + 种 KB）──")
        rc, out = py_stage("bootstrap")
        m1 = markers(out)
        dump_out(out, rc, 12)
        boot = m1.get("BOOTSTRAP", {}).get("shape", {}) if "BOOTSTRAP" in m1 else {}
        check("bootstrap 退出码 0", rc == 0, f"rc={rc}")
        check("bootstrap 新形态含 targets_hash（随后由真实脚本回滚）", bool(boot.get("has_targets_hash")), str(boot))

        say("── 阶段 2：真实迁移脚本 --action rollback（D2：不手写 DDL）──")
        rc, out = migrate("rollback")
        show_tail(out, limit=14)
        check("rollback 退出码 0", rc == 0, f"rc={rc}")
        check("rollback 报告完成", "rollback completed" in out, out[-200:])

        say("── 阶段 3：断言迁移前形态（真实模型回滚后，无 targets_hash）成立并种旧行 ──")
        rc, out = py_stage("assert-old-shape")
        m2 = markers(out)
        dump_out(out, rc, 12)
        legacy = m2.get("LEGACY", {})
        legacy_shape = legacy.get("shape", {})
        legacy_id = legacy.get("legacy_job_id")
        check("assert-old-shape 退出码 0", rc == 0, f"rc={rc}")
        check("旧形态无 targets_hash 列/索引", legacy_shape and not legacy_shape.get("has_targets_hash")
              and not legacy_shape.get("partial_unique_indexdef"), str(legacy_shape))
        old_columns = int(legacy_shape.get("column_count", -1)) if legacy_shape else -1

        say("── 阶段 4：Go 读侧 pre_migrate（缺列必须结构化 503 + 路由 404）──")
        rc, out, v1 = go_phase("pre_migrate", {"GI_P6_LEGACY_JOB_ID": str(legacy_id or 0)})
        show_tail(out, limit=28)
        check("Go pre_migrate 阶段整体退出码 0", rc == 0, f"rc={rc}")
        criterion(1, "路由未放行返回 404", v1.get("TestP6UnlistedJobTypeIsRejectedAtRouteDispatch") == "PASS",
                  f"pre_migrate verdict={v1.get('TestP6UnlistedJobTypeIsRejectedAtRouteDispatch')}")
        criterion(3, "缺 targets_hash 读侧结构化 503", v1.get("TestP6ReadPathIsStructuredUnavailableWithoutColumn") == "PASS",
                  f"pre_migrate verdict={v1.get('TestP6ReadPathIsStructuredUnavailableWithoutColumn')}")

        say("── 阶段 5：真实迁移脚本 --action migrate（补齐列 + 部分唯一索引）──")
        rc, out = migrate("migrate")
        show_tail(out, limit=14)
        check("migrate 退出码 0", rc == 0, f"rc={rc}")
        check("migrate 报告完成", "migrate completed" in out, out[-200:])

        say("── 阶段 6：断言迁移后结构与旧形态对账 ──")
        rc, out = py_stage("assert-migrated", ["--old-shape-json", json.dumps(legacy_shape)])
        m3 = markers(out)
        dump_out(out, rc, 12)
        new_shape = m3.get("MIGRATED", {}).get("shape", {})
        check("assert-migrated 退出码 0", rc == 0, f"rc={rc}")
        check(f"列数 {old_columns} → {old_columns + 1}", bool(new_shape) and new_shape.get("column_count") == old_columns + 1,
              str(new_shape))

        say("── 阶段 7：Python 内部提交路径（判据 2/6/7 + FOR UPDATE）──")
        rc, out = py_stage("submit", timeout=360)
        m4 = markers(out)
        dump_out(out, rc, 26)
        sub = m4.get("SUBMIT", {})
        check("submit 退出码 0", rc == 0, f"rc={rc}")
        job_id = sub.get("job_id")
        targets_hash = sub.get("targets_hash")
        outcomes = sub.get("outcomes") or []
        criterion(2, "Python 内部路径创建 reindex_chunks",
                  rc == 0 and isinstance(job_id, int) and sub.get("reindex_chunks_count") == 1 and bool(targets_hash),
                  f"marker={sub}")
        criterion(6, "同 targets_hash 不新增且回读既有 child ID",
                  rc == 0 and sub.get("reindex_chunks_count") == 1 and "reused" in outcomes
                  and int(sub.get("for_update_sql_count") or 0) >= 1,
                  f"count={sub.get('reindex_chunks_count')} outcomes={outcomes} for_update={sub.get('for_update_sql_count')}")
        criterion(7, "failed/cancelled 原地 retry/reset",
                  rc == 0 and "retried" in outcomes and "reset" in outcomes,
                  f"outcomes={outcomes}")

        say("── 阶段 8：Go 读侧 post_migrate（判据 1/4/5 + 留痕读侧）──")
        rc, out, v2 = go_phase(
            "post_migrate",
            {
                "GI_P6_LEGACY_JOB_ID": str(legacy_id or 0),
                "GI_P6_JOB_ID": str(job_id or 0),
                "GI_P6_TARGETS_HASH": str(targets_hash or ""),
            },
        )
        show_tail(out, limit=30)
        check("Go post_migrate 阶段整体退出码 0", rc == 0, f"rc={rc}")
        if CRITERIA.get("C1", (False, ""))[0]:
            criterion(1, "路由未放行返回 404", v2.get("TestP6UnlistedJobTypeIsRejectedAtRouteDispatch") == "PASS",
                      f"post_migrate verdict={v2.get('TestP6UnlistedJobTypeIsRejectedAtRouteDispatch')}")
        criterion(4, "加列后读侧成功回带 targets_hash",
                  v2.get("TestP6ReadPathSucceedsWithTargetsHashAfterMigration") == "PASS",
                  f"post_migrate verdict={v2.get('TestP6ReadPathSucceedsWithTargetsHashAfterMigration')}")
        criterion(5, "迁移前旧行（targets_hash 为 NULL）仍可读，不参与唯一性",
                  v2.get("TestP6LegacyRowStaysReadableAfterMigration") == "PASS",
                  f"post_migrate verdict={v2.get('TestP6LegacyRowStaysReadableAfterMigration')}")
        check("Go 读侧回读到 Python 留痕（P5 缺口 3 闭环）",
              v2.get("TestP6JobLogsExposePythonAuditDetails") == "PASS",
              f"post_migrate verdict={v2.get('TestP6JobLogsExposePythonAuditDetails')}")

        say("── 阶段 9：收尾状态 dump（供人工复查）──")
        rc, out = py_stage("dump")
        m5 = markers(out)
        dump = m5.get("DUMP", {})
        check("dump 退出码 0", rc == 0, f"rc={rc}")
        say(f"    admin_jobs 最终 {len(dump.get('jobs', []))} 行；job 留痕 {len(dump.get('job_logs', []))} 条")
        for row in dump.get("jobs", []):
            say(f"    · job#{row.get('id')} {row.get('job_type')} status={row.get('status')} "
                f"retry={row.get('retry_count')} targets_hash={'有' if row.get('targets_hash') else 'NULL'}")
    finally:
        teardown()

    return finish()


# ── 收尾与残留复查 ───────────────────────────────────────────────────

def teardown() -> None:
    say("── 收尾：删除一次性容器与网络并复查零残留 ──")
    for name in (PY, PG):
        rc, out = docker("rm", "-f", name, timeout=120, label=f"rm {name}")
        check(f"{name} 已删除", rc == 0, out.strip())
    rc, out = docker("network", "rm", NET, timeout=120, label="network rm")
    check(f"{NET} 已删除", rc == 0, out.strip())

    rc, out = docker("ps", "-a", "--filter", "name=gi-p6", "--format", "{{.Names}}", timeout=60)
    check("容器零残留（gi-p6*）", rc == 0 and not out.strip(), out.strip())
    rc, out = docker("network", "ls", "--filter", "name=gi-p6", "--format", "{{.Name}}", timeout=60)
    check("网络零残留（gi-p6*）", rc == 0 and not out.strip(), out.strip())

    say("── 共享栈未受影响复查 ──")
    rc, out = docker("network", "inspect", "graphinsight_default", "--format", "{{.Name}}", timeout=60)
    check("graphinsight_default 状态与基线一致", (rc == 0) == SHARED.get("graphinsight_default"))
    rc, out = docker("ps", "--filter", "name=gi-phase3-pg", "--format", "{{.Names}}", timeout=60)
    check("gi-phase3-pg 状态与基线一致", bool(out.strip()) == SHARED.get("gi-phase3-pg"))


def finish() -> int:
    say("")
    say("── 七项通过标准逐条对账 ──")
    for idx in range(1, 8):
        key = f"C{idx}"
        ok, evidence = CRITERIA.get(key, (False, "未执行到该判据"))
        say(f"  {'✓' if ok else '✗'} 判据{idx} {evidence}")
    failed_criteria = sum(1 for i in range(1, 8) if not CRITERIA.get(f"C{i}", (False, ""))[0])
    failed_steps = len(FAILURES)
    ok = failed_criteria == 0 and failed_steps == 0
    say(f"P6_DISPOSABLE_SUMMARY criteria=7 failed_criteria={failed_criteria} failed_steps={failed_steps}")
    say("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
