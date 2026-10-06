#!/usr/bin/env python3
"""网络护栏（netguard）自身的验证——含"把拦截异常吞掉且不打印"的负向用例。

为什么必须验证护栏本身：2026-10-06 复核用"静默 try/except 拦截异常"复现了
**child exit=0 / 输出无标记 / runner 判通过** 的假绿。因此判红依据改为**独立持久留痕**
（拦截即写文件 + 导入即写加载记录），由 runner 校验留痕而不是只看 stdout。

本子项验证四件事（全部在**子进程**里做，父进程自身零外连）：
  N1 加载留痕：护栏注入后子进程启动即产出 `guard_loaded`；
  N2 拦截留痕：连接真实服务端口（7687）被拦，且**异常被吞、不打印任何输出**时，
     违规文件仍有 `blocked_connect` 记录（这正是堵死假绿的机制）；
  N3 外网（1.1.1.1:443）同样被拦并留痕；
  N4 普通回环（非服务端口）**不被误拦**（Windows asyncio 的 socketpair 依赖回环）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

TESTS_DIR = Path(__file__).resolve().parent
NETGUARD_DIR = TESTS_DIR / "netguard"
PYTHON_EXE = os.environ.get("GUARD_PYTHON") or sys.executable

FAILURES: List[str] = []
PASSES: List[str] = []

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def step(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILURES).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" ({detail})" if detail and not ok else ""))


# 子进程片段：**静默吞掉**拦截异常，且不打印任何内容（复现复核方的假绿手法）
SILENT_CONNECT = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('{host}', {port}), timeout=1)\n"
    "except Exception:\n"
    "    pass\n"
)

# 子进程片段：普通回环（非服务端口）连接，用于证明护栏不误伤
LOOPBACK_CONNECT = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
    "    print('CONNECTED')\n"
    "except Exception as exc:\n"
    "    print('ERR:' + type(exc).__name__)\n"
)


def _run_child(snippet: str) -> Dict[str, Any]:
    """在护栏下跑一个子进程（独立留痕文件），返回输出与留痕。"""
    with tempfile.TemporaryDirectory(prefix="netguard-selftest-") as tmp:
        status_file = Path(tmp) / "status.jsonl"
        violations_file = Path(tmp) / "violations.jsonl"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(NETGUARD_DIR) + os.pathsep + (env.get("PYTHONPATH") or "")
        env["GUARD_NET_STATUS"] = str(status_file)
        env["GUARD_NET_VIOLATIONS"] = str(violations_file)
        proc = subprocess.run(  # noqa: S603
            [PYTHON_EXE, "-c", snippet],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60, check=False,
        )
        return {
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "status": _read(status_file),
            "violations": _read(violations_file),
        }


def _read(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
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


def main() -> int:
    print("网络护栏自检（含'捕获异常且不打印'的负向用例）")

    # N2 静默吞掉拦截异常：无输出、无标记，但留痕必须在
    silent = _run_child(SILENT_CONNECT.format(host="127.0.0.1", port=7687))
    step("N1 子进程加载护栏并留痕 guard_loaded",
         any(r.get("event") == "guard_loaded" for r in silent["status"]),
         f"status={silent['status'][:2]}")
    step("N2 静默吞掉拦截异常时**输出为空**（复现假绿手法）",
         silent["stdout"] == "" and "NEUTRALIZED_NETWORK" not in silent["stderr"],
         f"stdout={silent['stdout']!r} rc={silent['returncode']}")
    blocked = [r for r in silent["violations"] if r.get("event") == "blocked_connect"]
    step("N2 违规留痕仍记录 blocked_connect（7687）——假绿被堵死",
         len(blocked) == 1 and str(blocked[0].get("port")) == "7687",
         f"violations={silent['violations']}")

    # N3 外网同样被拦
    external = _run_child(SILENT_CONNECT.format(host="1.1.1.1", port=443))
    ext_blocked = [r for r in external["violations"] if r.get("event") == "blocked_connect"]
    step("N3 外网连接被拦并留痕（1.1.1.1:443）",
         bool(ext_blocked) and str(ext_blocked[0].get("host")) == "1.1.1.1",
         f"violations={external['violations']}")

    # N4 普通回环不误拦（否则会打掉 Windows asyncio 的 socketpair）
    loopback = _run_child(LOOPBACK_CONNECT.format(port=59999))
    loop_violations = [r for r in loopback["violations"] if r.get("event") == "blocked_connect"]
    step("N4 普通回环连接不被护栏拦截（非服务端口）",
         not loop_violations and "NEUTRALIZED_NETWORK" not in loopback["stdout"],
         f"stdout={loopback['stdout']!r} violations={loopback['violations']}")

    # N5 runner 级验证：把"静默吞异常"的探针交给**真实 runner 的 _run_case**，必须被判红。
    # 只留痕不判红 = 依旧假绿；这一步证明 runner 的判据确实接上了留痕。
    sys.path.insert(0, str(TESTS_DIR))
    import run_unified_boundary_guards as runner  # noqa: E402

    with tempfile.TemporaryDirectory(prefix="netguard-probe-") as probe_dir:
        probe = Path(probe_dir) / "probe_silent_connect.py"
        probe.write_text(SILENT_CONNECT.format(host="127.0.0.1", port=7687), encoding="utf-8")
        case = runner.GuardCase("probe_silent_connect", str(probe), 60, "silent connect probe")
        probe_ok, _duration, probe_out, probe_rc = runner._run_case(Path(PYTHON_EXE), case)
    step("N5 真实 runner 判定：静默吞掉异常的子项被判红（留痕已接入判据）",
         probe_ok is False and "NETGUARD" in probe_out and "probe_silent_connect" in case.name,
         f"ok={probe_ok} rc={probe_rc} out={probe_out[-200:]}")

    # N6 损坏的违规留痕（坏 JSON 行）不得折为空后放行（复核反例：跳过坏行 → runner 判绿）
    with tempfile.TemporaryDirectory(prefix="netguard-corrupt-") as corrupt_dir:
        corrupt_file = Path(corrupt_dir) / "violations.jsonl"
        corrupt_file.write_text(
            '{"event": "blocked_connect", "host": "127.0.0.1", "port": 7687\n'  # 半截写入（截断）
            '{bad json line\n',
            encoding="utf-8",
        )
        read = runner._read_netguard_records(corrupt_file)
        problems = runner._netguard_problems(
            {"exists": True, "records": [{"event": "guard_loaded"}], "bad_lines": 0, "error": None},
            read, "",
        )
    step("N6 损坏的违规留痕 → 判红（坏行不折为空）",
         read.get("bad_lines", 0) >= 1 and any("损坏" in p for p in problems),
         f"read={read} problems={problems}")

    # N7 留痕读取失败（文件不可读）同样不得构成通过
    problems = runner._netguard_problems(
        {"exists": True, "records": [{"event": "guard_loaded"}], "bad_lines": 0, "error": None},
        {"exists": True, "records": [], "bad_lines": 0, "error": "PermissionError: denied"},
        "",
    )
    step("N7 留痕读取失败 → 判红（证据不可用）",
         any("读取失败" in p for p in problems), f"problems={problems}")

    # N8 留痕**写入失败**：违规文件指向不可写路径（目录），子进程静默吞异常 →
    # 护栏回退 stderr 标记，runner 必须判红（不得因为"没写进去"就当没有违规）。
    with tempfile.TemporaryDirectory(prefix="netguard-unwritable-") as unwritable_dir:
        # 用"目录"当违规文件路径：open() 写入必然失败
        probe = Path(unwritable_dir) / "probe_silent_connect.py"
        probe.write_text(SILENT_CONNECT.format(host="127.0.0.1", port=7687), encoding="utf-8")
        status_file = Path(unwritable_dir) / "status.jsonl"

        env = os.environ.copy()
        env["PYTHONPATH"] = str(NETGUARD_DIR) + os.pathsep + (env.get("PYTHONPATH") or "")
        env["GUARD_NET_STATUS"] = str(status_file)
        env["GUARD_NET_VIOLATIONS"] = str(Path(unwritable_dir))  # 目录：不可写
        child = subprocess.run(  # noqa: S603
            [PYTHON_EXE, str(probe)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=60, check=False,
        )
        child_problems = runner._netguard_problems(
            runner._read_netguard_records(status_file),
            {"exists": False, "records": [], "bad_lines": 0, "error": None},
            (child.stdout or "") + "\n" + (child.stderr or ""),
        )
    step("N8 违规留痕写入失败 → stderr 回退标记 + runner 判红",
         "NEUTRALIZED_NETWORK" in (child.stderr or "")
         and bool(child_problems) and child.returncode == 0,
         f"rc={child.returncode} stderr={child.stderr!r} problems={child_problems}")

    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
