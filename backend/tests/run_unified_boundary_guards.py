#!/usr/bin/env python3
"""Run the Python-side unified boundary guard suite."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
# 首选 Linux 后端虚拟环境；允许用 GUARD_PYTHON 显式覆盖，便于在容器 / 非默认布局中运行。
PYTHON_EXE = ROOT / ".venv" / "bin" / "python"

# 本文件会把子进程输出原样回显，子进程带 ✓/✗ 与中文；父进程不强制 UTF-8 时，
# Windows 默认码（cp936）会在 print 处 UnicodeEncodeError 崩掉整条守卫链（2026-10-02 实测）。
# 这是脚本自身契约，不靠命令行 `-X utf8`。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_env(base: dict[str, str], *, status_file: Path, violations_file: Path) -> dict[str, str]:
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 网络护栏：把 tests/netguard 注入 PYTHONPATH，Python 启动时会自动 import sitecustomize，
    # 从而禁止外网与真实服务端口连接。见 netguard/sitecustomize.py。
    netguard = str(TESTS_DIR / "netguard")
    existing = env.get("PYTHONPATH") or ""
    parts = [netguard] + [p for p in existing.split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    # 加载留痕 + 违规留痕（独立文件）：**不依赖子进程打印什么**，捕获异常静默也不能假绿。
    env["GUARD_NET_STATUS"] = str(status_file)
    env["GUARD_NET_VIOLATIONS"] = str(violations_file)
    return env


NETGUARD_MARKER = "NEUTRALIZED_NETWORK"


def _read_netguard_records(path: Path) -> list[dict]:
    """读取护栏 JSONL 留痕；不存在/不可解析一律返回空列表（缺失由调用方判失败）。"""
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    except FileNotFoundError:
        return []
    return records


@dataclass(frozen=True)
class GuardCase:
    name: str
    script: str
    timeout_seconds: int
    description: str


CASES: tuple[GuardCase, ...] = (
    GuardCase("migration_cleanup", "check_migration_cleanup_guards.py", 60, "迁移清理静态守卫"),
    GuardCase("route_mounts", "check_unified_route_mounts_unit.py", 60, "统一运行态路由挂载边界"),
    GuardCase("internal_inventory", "check_python_internal_route_inventory_unit.py", 60, "Python internal capability inventory"),
    GuardCase("business_public_removed", "check_business_public_routes_removed_unit.py", 60, "Python 公开业务路由移除守卫"),
    GuardCase("admin_public_removed", "check_admin_public_routes_removed_unit.py", 60, "Python 公开管理路由移除守卫"),
    GuardCase("docqa_internal", "check_docqa_internal_route_unit.py", 60, "DocQA internal header contract"),
    GuardCase("nl2cypher_internal", "check_nl2cypher_internal_route_unit.py", 60, "NL2Cypher internal header contract"),
    GuardCase("runtime_config_boundary", "check_runtime_config_boundary_unit.py", 60, "Python runtime config boundary"),
    GuardCase("document_parser", "check_document_parser_unit.py", 60, "Document parser adapters and fallback"),
    GuardCase("retrieval_orchestrator", "check_retrieval_orchestrator_unit.py", 60, "DocQA hybrid retrieval orchestrator"),
    GuardCase("job_worker", "check_job_worker_unit.py", 60, "Python worker lease and wake behavior"),
    GuardCase("qa_cost", "check_qa_cost_summary_unit.py", 60, "QA cost aggregation unit check"),
    GuardCase("admin_env_override", "check_admin_env_override_unit.py", 60, "unified runtime env override guard"),
    GuardCase("rate_limit_exempt", "check_rate_limit_exempt_unit.py", 60, "internal health probe rate limit exemption"),
    GuardCase("netguard_enforcement", "check_netguard_enforcement.py", 180,
              "网络护栏自检（含静默吞异常仍留痕的负向用例）"),
    GuardCase("backfill_manifest", "check_backfill_manifest.py", 180, "backfill 运行清单（隔离 SQLite，零真实连接）"),
    GuardCase("vector_write_contract", "check_vector_write_contract.py", 60, "向量写入契约矩阵（两种意图 + KB 范围守卫，进程内假体）"),
    GuardCase("rbac_catalog_parity", "check_rbac_catalog_parity.py", 120, "Python/Go RBAC 权限目录精确对账"),
    GuardCase("m5_dual_write", "check_m5_dual_write.py", 60, "M5 §16.1 S1 双写扇出与失败语义守卫"),
    GuardCase(
        "build_graph_shadow_retry",
        "check_build_graph_shadow_retry.py",
        90,
        "M5 §16.1 S1 build_graph→dual_write 影子失败→作业重试调用链守卫",
    ),
    GuardCase(
        "m5_build_graph_revision",
        "check_m5_build_graph_revision.py",
        120,
        "M5 §16.1 S1 Wave 2 revision 生命周期（CAS + 先于投影写入 + VectorChunk content_revision）守卫",
    ),
    GuardCase(
        "m5_wave3_handoff",
        "check_m5_wave3_handoff.py",
        300,
        "M5 Wave 3 §16.3 连续场景（影子失败持久转交 / targets_hash 复用 / 终态父子回写 / §8.5 拒写）守卫",
    ),
    GuardCase(
        "secret_scanner_selftest",
        "check_artifact_secrets_selftest.py",
        180,
        "敏感信息扫描器正样本自检与路径排除守卫",
    ),
)


def _resolve_python() -> Path:
    # 解析顺序：GUARD_PYTHON 环境变量 -> backend/.venv/bin/python（Linux）-> 当前解释器。
    # 当前解释器兜底是为了让本套件在没有 Linux venv 布局的环境（容器 / Windows）也能执行；
    # 前提是已安装 requirements.txt 依赖。缺失依赖导致的用例失败属于环境未就绪，不是代码缺陷。
    override = os.environ.get("GUARD_PYTHON", "").strip()
    if override:
        override_path = Path(override)
        if override_path.exists():
            return override_path
        raise RuntimeError(f"GUARD_PYTHON 指向的解释器不存在: {override_path}")
    if PYTHON_EXE.exists():
        return PYTHON_EXE
    fallback = Path(sys.executable)
    print(
        f"[warn] 未找到 Linux 后端虚拟环境 Python: {PYTHON_EXE}；"
        f"回退使用当前解释器 {fallback}（需已安装 backend/requirements.txt 依赖）",
        file=sys.stderr,
    )
    return fallback


def _run_case(python_bin: Path, case: GuardCase) -> tuple[bool, float, str, int]:
    started = time.perf_counter()
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"netguard-{case.name}-") as guard_dir_name:
        guard_dir = Path(guard_dir_name)
        status_file = guard_dir / "load_status.jsonl"
        violations_file = guard_dir / "violations.jsonl"
        proc = subprocess.run(  # noqa: S603
            [str(python_bin), str(TESTS_DIR / case.script)],
            cwd=str(ROOT.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_utf8_env(os.environ, status_file=status_file, violations_file=violations_file),
            timeout=case.timeout_seconds,
            check=False,
        )
        status_records = _read_netguard_records(status_file)
        violation_records = _read_netguard_records(violations_file)
    duration = time.perf_counter() - started
    output = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = output
    if err:
        combined = f"{combined}\n[stderr]\n{err}".strip()

    # 判红依据（三条独立信号，任一命中即失败；不因为退出码为 0 就放行）：
    #   1) 护栏**确实加载**（加载留痕存在）——否则"隔离"根本没生效；
    #   2) 违规留痕为空——**即使子项把拦截异常静默 try/except 掉、stdout 无任何标记**；
    #   3) stdout 标记（次要信号，便于定位）。
    if not status_records:
        problems.append("[NETGUARD] 未发现护栏加载留痕：隔离未生效（PYTHONPATH/sitecustomize 注入失败）")
    if violation_records:
        detail = "; ".join(
            f"{item.get('host')}:{item.get('port')}" for item in violation_records[:5]
        )
        problems.append(
            f"[NETGUARD] 检测到 {len(violation_records)} 次真实网络连接尝试（持久留痕，"
            f"不依赖子进程输出）：{detail}"
        )
    if NETGUARD_MARKER in combined and not violation_records:
        problems.append("[NETGUARD] 输出现拦截标记但无留痕（护栏状态异常）")
    if problems:
        combined = f"{combined}\n" + "\n".join(problems)
        return False, duration, combined, proc.returncode
    return proc.returncode == 0, duration, combined, proc.returncode


def main() -> int:
    python_bin = _resolve_python()
    failed = 0
    for case in CASES:
        print("=" * 72)
        print(f"CASE {case.name}: {case.description}")
        try:
            success, duration, output, code = _run_case(python_bin, case)
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"[FAIL] {case.name} timeout>{case.timeout_seconds}s")
            continue

        # 失败腿不截断，完整给出子进程输出（含 stderr）；通过腿仍截到 12000 字符
        print(output if not success else (output[:12000] if output else "(no output)"))
        if success:
            print(f"[OK] {case.name} duration={duration:.1f}s")
        else:
            print(f"[FAIL] {case.name} exit={code} duration={duration:.1f}s")
            failed += 1

    print("=" * 72)
    print(f"SUMMARY total={len(CASES)} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
