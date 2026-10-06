#!/usr/bin/env python3
"""check_m5_nonempty_disposable.py 的离线故障注入契约测试（零真实容器/连接）。

覆盖（第八轮工单）：
一、证据合同 fail-closed：
  - JSON 解析失败 / RUN_ID 不一致 / meta 读取失败 / evidence 写入失败 → evidence 存在、
    ROUND_EXIT=2、CHECKS_FAILED>0、write_evidence 返回 2；
二、finally 保护结构：
  - 真实 main 流程下 archive/cleanup/write_evidence 失败分别注入，确认后续步骤仍被调用；
三、一致性：ROUND_EXIT=0 只在全部通过时、CHECKS_TOTAL 与最终步骤数一致。

只 import 模块、用临时目录与 mock.patch，不创建任何容器、不连接任何真实服务。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_m5_nonempty_disposable as m  # noqa: E402

FAILURES = []
PASSES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" ({detail})" if detail and not cond else ""))


def _reset() -> None:
    m.CHECKS.clear()
    m.STEPS.clear()
    m.FAILURES.clear()


def _read_meta(base: Path) -> str:
    return (base / "nonempty_meta.txt").read_text(encoding="utf-8")


def test_evidence_normal() -> None:
    _reset()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        m.add_step("正常步骤", True)
        exit_code = m.write_evidence(base, "RUN-ok", "2026-01-01T00:00:00")
        check("正常：write_evidence 返回 0", exit_code == 0, f"exit={exit_code}")
        meta = _read_meta(base)
        check("正常：meta ROUND_EXIT=0", "ROUND_EXIT=0" in meta, meta)
        steps = json.loads((base / "nonempty_steps.json").read_text(encoding="utf-8"))
        check("正常：CHECKS_TOTAL 与 JSON steps 数一致", f"CHECKS_TOTAL={len(steps['steps'])}" in meta,
              f"meta={meta!r} steps={len(steps['steps'])}")


def test_evidence_json_parse_failure() -> None:
    _reset()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        m.add_step("步骤", True)
        with mock.patch.object(m.json, "loads", side_effect=ValueError("bad json")):
            exit_code = m.write_evidence(base, "RUN-j", "2026-01-01T00:00:00")
        check("JSON 解析失败：write_evidence 返回 2", exit_code == 2, f"exit={exit_code}")
        meta = _read_meta(base)
        check("JSON 解析失败：meta ROUND_EXIT=2", "ROUND_EXIT=2" in meta, meta)
        check("JSON 解析失败：CHECKS_FAILED>0", "CHECKS_FAILED=0" not in meta, meta)


def test_evidence_run_id_mismatch() -> None:
    _reset()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        m.add_step("步骤", True)
        orig_dumps = m.json.dumps

        def tamper(obj, *args, **kwargs):
            if isinstance(obj, dict) and obj.get("run_id") == "RUN-m":
                obj = {**obj, "run_id": "RUN-OTHER"}
            return orig_dumps(obj, *args, **kwargs)

        with mock.patch.object(m.json, "dumps", side_effect=tamper):
            exit_code = m.write_evidence(base, "RUN-m", "2026-01-01T00:00:00")
        check("RUN_ID 不一致：write_evidence 返回 2", exit_code == 2, f"exit={exit_code}")
        meta = _read_meta(base)
        check("RUN_ID 不一致：meta ROUND_EXIT=2", "ROUND_EXIT=2" in meta, meta)


def test_evidence_meta_read_failure() -> None:
    _reset()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        m.add_step("步骤", True)
        orig_read = Path.read_text

        def flaky_read(self, *args, **kwargs):
            if self.name == "nonempty_meta.txt":
                raise OSError("meta read failed")
            return orig_read(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", flaky_read):
            exit_code = m.write_evidence(base, "RUN-r", "2026-01-01T00:00:00")
        check("meta 读取失败：write_evidence 返回 2", exit_code == 2, f"exit={exit_code}")


def test_evidence_write_failure() -> None:
    _reset()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        m.add_step("步骤", True)
        orig_write = Path.write_text
        call_count = [0]

        def flaky_write(self, data, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # 第一次写 steps JSON 失败
                raise OSError("disk full")
            return orig_write(self, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", flaky_write):
            exit_code = m.write_evidence(base, "RUN-w", "2026-01-01T00:00:00")
        check("写盘失败：write_evidence 返回 2", exit_code == 2, f"exit={exit_code}")
        check("写盘失败：emergency 证据存在", (base / "nonempty_emergency.txt").exists())


def test_main_finally_protection() -> None:
    """真实 main 流程：archive/cleanup/write_evidence 失败分别注入，后续步骤仍被调用。"""
    _reset()
    calls = []

    def fake_archive(base, tmp):
        calls.append("archive")
        raise RuntimeError("archive fail")

    def fake_cleanup(tmp, kb, keep):
        calls.append("cleanup")

    def fake_write_evidence(base, run_id, exec_time):
        calls.append("write_evidence")
        return 0

    with tempfile.TemporaryDirectory() as td:
        # mock 预检（无同名对象）→ ensure_containers 抛异常（主流程进 except）→ finally 三步。
        with mock.patch.object(sys, "argv", ["prog", "--authorize", m.AUTHORIZE_TOKEN,
                                             "--evidence-dir", td]), \
             mock.patch.object(m, "_docker_names", return_value=set()), \
             mock.patch.object(m, "_docker_networks", return_value=set()), \
             mock.patch.object(m, "_docker_volumes", return_value=set()), \
             mock.patch.object(m, "ensure_containers", side_effect=RuntimeError("ensure fail")), \
             mock.patch.object(m, "archive_manifests", side_effect=fake_archive), \
             mock.patch.object(m, "cleanup", side_effect=fake_cleanup), \
             mock.patch.object(m, "write_evidence", side_effect=fake_write_evidence):
            exit_code = m.main()
        check("main：archive 失败后 cleanup 仍被调用", "cleanup" in calls, str(calls))
        check("main：cleanup 后 write_evidence 仍被调用", "write_evidence" in calls, str(calls))
        check("main：三步都被调用", calls == ["archive", "cleanup", "write_evidence"], str(calls))
        check("main：返回非零（主流程异常 + archive 失败）", exit_code != 0, f"exit={exit_code}")


def test_no_container_on_unauthorized() -> None:
    """未授权 / dry-run / 预检失败路径都不创建真实容器（ensure_containers 不被调用）。"""
    _reset()
    with mock.patch.object(sys, "argv", ["prog"]), \
         mock.patch.object(m, "ensure_containers") as mock_ensure, \
         mock.patch.object(m, "print_plan"):
        exit_code = m.main()
    check("未授权：返回 3", exit_code == 3, f"exit={exit_code}")
    check("未授权：不创建容器（ensure_containers 未调用）", not mock_ensure.called)

    _reset()
    with mock.patch.object(sys, "argv", ["prog", "--dry-run"]), \
         mock.patch.object(m, "ensure_containers") as mock_ensure, \
         mock.patch.object(m, "print_plan"):
        exit_code = m.main()
    check("dry-run：返回 3", exit_code == 3, f"exit={exit_code}")
    check("dry-run：不创建容器（ensure_containers 未调用）", not mock_ensure.called)

    _reset()
    with mock.patch.object(sys, "argv", ["prog", "--authorize", m.AUTHORIZE_TOKEN, "--evidence-dir", "tmp"]), \
         mock.patch.object(m, "_docker_names", return_value={"gi-nonempty-pg"}), \
         mock.patch.object(m, "_docker_networks", return_value=set()), \
         mock.patch.object(m, "_docker_volumes", return_value=set()), \
         mock.patch.object(m, "ensure_containers") as mock_ensure, \
         mock.patch.object(m, "archive_manifests"), \
         mock.patch.object(m, "cleanup"), \
         mock.patch.object(m, "write_evidence", return_value=0):
        exit_code = m.main()
    check("预检失败（同名对象）：返回 2", exit_code == 2, f"exit={exit_code}")
    check("预检失败：不创建容器（ensure_containers 未调用）", not mock_ensure.called)


def main() -> int:
    test_evidence_normal()
    test_evidence_json_parse_failure()
    test_evidence_run_id_mismatch()
    test_evidence_meta_read_failure()
    test_evidence_write_failure()
    test_main_finally_protection()
    test_no_container_on_unauthorized()
    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
