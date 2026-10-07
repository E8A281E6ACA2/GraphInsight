#!/usr/bin/env python3
"""`archive_nonempty_evidence.py` 归档合同的**负向回归**（CLI 级，零真实服务连接）。

覆盖本轮修订的合同点：
  - 每步布尔 ok；
  - 身份三方一致（主日志 / JSON / meta）；
  - **重复关键字段拒绝**；
  - **跳过 Docker 只标"文件校验通过、残留未取证"，不得记 residue_ok=true**；
  - 归档后读回最终文件（recheck.json）；
  - **明确清单关联 manifests**（清单缺失 / 清单外同 kb 遗漏均失败）；
  - 保留原证据（归档后源目录不变）。

用临时目录构造证据、以子进程跑真实 CLI，不连任何服务。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "archive_nonempty_evidence.py"
PASSES: List[str] = []
FAILURES: List[str] = []

KB = "kb-nonempty-abc12345"
RUN = "RUN-20261007T000406-nonempty-disposable"
MF1 = f"backfill-{KB}-20261006160434-6286ec0b.json"
MF2 = f"backfill-{KB}-20261006160453-9554a731.json"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILURES).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f" ({detail})" if detail and not cond else ""))


def _steps_doc(n: int = 2, *, run_id: str = RUN, bad_ok: bool = False) -> Dict[str, Any]:
    ok_val: Any = "yes" if bad_ok else True
    return {
        "schema": "gate0-nonempty-disposable-steps-v1",
        "run_id": run_id,
        "steps": [{"name": f"s{i}", "ok": ok_val, "detail": ""} for i in range(n)],
    }


def _meta_text(*, total: int = 2, failed: int = 0, exit_code: int = 0,
               run_id: str = RUN, dup_key: bool = False) -> str:
    lines = [
        "GATE NONEMPTY-DISPOSABLE ROUND METADATA (single round, overwritten each round)",
        f"RUN_ID={run_id}",
        "EXEC_TIME=2026-10-07T00:04:06",
        "TARGET_KIND=disposable-docker",
        f"CHECKS_TOTAL={total}",
        f"CHECKS_FAILED={failed}",
        f"ROUND_EXIT={exit_code}",
    ]
    if dup_key:
        lines.append(f"RUN_ID={run_id}")  # 重复关键字段
    return "\n".join(lines) + "\n"


def build_evidence(base: Path, *, n_steps: int = 2, bad_step_ok: bool = False,
                   meta_run_id: str = RUN, dup_meta: bool = False,
                   total: Optional[int] = None, manifests: List[str] = None,
                   console_has_run: bool = True) -> None:
    (base / "manifests").mkdir(parents=True, exist_ok=True)
    total = n_steps if total is None else total
    (base / "nonempty_steps.json").write_text(
        json.dumps(_steps_doc(n_steps, run_id=meta_run_id, bad_ok=bad_step_ok),
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (base / "nonempty_meta.txt").write_text(
        _meta_text(total=total, run_id=meta_run_id, dup_key=dup_meta), encoding="utf-8")
    for name in (manifests if manifests is not None else [MF1, MF2]):
        (base / "manifests" / name).write_text(
            json.dumps({"schema": "backfill-manifest-v1", "trace_id": name[:-5],
                        "kb_id": KB, "exit_code": 3, "outcome": "gate_open"},
                       ensure_ascii=False), encoding="utf-8")
    console = f"ROUND_MARK {RUN}\n" if console_has_run else "unrelated console\n"
    console += "  [PASS] 预检：目标端口空闲\n  [PASS] 失败重放：零新增写入\n  [PASS] 回滚 chunk_revisions 退出 0\n"
    (base / "nonempty-console.log").write_text(console, encoding="utf-8")


def run_archive(src: Path, dst: Path, console: Path, manifest_list: str,
                *, skip_docker: bool = True, expect_total: Optional[int] = None) -> tuple[int, str, Dict[str, Any]]:
    cmd = [sys.executable, str(SCRIPT), "--src", str(src), "--dst", str(dst),
           "--console-log", str(console), "--manifest-list", manifest_list]
    if expect_total is not None:
        cmd += ["--expect-total", str(expect_total)]
    if skip_docker:
        cmd.append("--skip-docker")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    verify: Dict[str, Any] = {}
    vf = dst / "machine_verify.json"
    if vf.is_file():
        verify = json.loads(vf.read_text(encoding="utf-8"))
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), verify


def scenario_ok() -> None:
    """正常：清单精确关联 → exit 0、每步布尔 ok、读回 PASS、源证据保留。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src)
        before = (src / "nonempty_meta.txt").read_text(encoding="utf-8")
        code, out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                        expect_total=2)
        check("正常：exit 0", code == 0, f"code={code} out={out[-300:]}")
        check("正常：result=PASS", verify.get("result") == "PASS", str(verify.get("machine_failed_names")))
        check("正常：每步都有布尔 ok",
              all(isinstance(s.get("ok"), bool) for s in verify.get("steps", [])))
        check("正常：manifests 按清单复制", verify.get("manifests_copied") == [MF1, MF2],
              str(verify.get("manifests_copied")))
        check("正常：源证据保留（未被删改）",
              (src / "nonempty_meta.txt").read_text(encoding="utf-8") == before)
        recheck = json.loads((dst / "recheck.json").read_text(encoding="utf-8")) if (dst / "recheck.json").is_file() else {}
        check("正常：归档后读回 recheck=PASS", recheck.get("result") == "PASS", str(recheck.get("checks")))
        check("正常：跳过 docker → residue_state=UNVERIFIED（残留未取证）",
              verify.get("residue_state") == "UNVERIFIED", str(verify.get("residue_state")))


def scenario_skip_docker_not_residue_ok() -> None:
    """跳过 docker 时**不得**记 residue_ok=true（P1：跳过≠已验证）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src)
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                         skip_docker=True, expect_total=2)
        check("skip-docker：仍能 exit 0（文件校验通过）", code == 0, f"code={code}")
        check("skip-docker：residue_ok 不得为 true", verify.get("residue_ok") is not True,
              f"residue_ok={verify.get('residue_ok')}")
        check("skip-docker：residue_state=UNVERIFIED",
              verify.get("residue_state") == "UNVERIFIED", str(verify.get("residue_state")))
        zr = (dst / "zero_residue_check.txt").read_text(encoding="utf-8")
        check("skip-docker：零残留文件标注未取证",
              "residue_state=UNVERIFIED" in zr and "残留未取证" in zr, zr)


def scenario_duplicate_meta_key_rejected() -> None:
    """重复关键字段（RUN_ID 出现两次）→ 拒绝。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src, dup_meta=True)
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                         expect_total=2)
        check("重复关键字段：exit 2", code == 2, f"code={code}")
        check("重复关键字段：失败项含“重复”",
              any("重复" in n for n in verify.get("machine_failed_names", [])),
              str(verify.get("machine_failed_names")))


def scenario_non_bool_step_rejected() -> None:
    """step.ok 非布尔 → 拒绝（每步必须布尔 ok）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src, bad_step_ok=True)
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                         expect_total=2)
        check("step.ok 非布尔：exit 2", code == 2, f"code={code}")
        check("step.ok 非布尔：失败项含“布尔 ok”",
              any("布尔 ok" in n for n in verify.get("machine_failed_names", [])),
              str(verify.get("machine_failed_names")))


def scenario_manifest_missing() -> None:
    """清单里的文件不存在 → 拒绝（精确清单，不按 kb 模糊筛选）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src, manifests=[MF1])
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                         expect_total=2)
        check("清单文件缺失：exit 2", code == 2, f"code={code}")
        check("清单文件缺失：missing 记录该文件",
              MF2 in (verify.get("manifest_missing") or []), str(verify.get("manifest_missing")))


def scenario_manifest_orphan() -> None:
    """清单外还有同 kb 的 manifest（遗漏）→ 拒绝。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src, manifests=[MF1, MF2])
        # 只列 MF1 → MF2 成为清单外同 kb 的遗漏
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", MF1, expect_total=2)
        check("清单外同 kb 遗漏：exit 2", code == 2, f"code={code}")
        check("清单外同 kb 遗漏：orphans 含该文件",
              MF2 in (verify.get("manifest_orphans") or []), str(verify.get("manifest_orphans")))


def scenario_identity_mismatch() -> None:
    """主日志不含 RUN_ID（身份三方不一致）→ 拒绝。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst"
        build_evidence(src, console_has_run=False)
        code, _out, verify = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                         expect_total=2)
        check("主日志缺失 RUN_ID：exit 2", code == 2, f"code={code}")
        check("主日志缺失 RUN_ID：失败项含“主日志”",
              any("主日志" in n for n in verify.get("machine_failed_names", [])),
              str(verify.get("machine_failed_names")))


def scenario_readback_tamper() -> None:
    """归档后 dst 的最终文件被篡改 → 读回复核必须判失败（不能只写不读回）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dst = root / "src", root / "dst2"
        build_evidence(src)
        code, _out, _v = run_archive(src, dst, src / "nonempty-console.log", f"{MF1},{MF2}",
                                     expect_total=2)
        check("读回场景：先归档成功", code == 0, f"code={code}")
        # 篡改归档后的最终文件（模拟归档写盘后被改）
        (dst / "nonempty_meta.txt").write_text(
            _meta_text(total=2, run_id="RUN-TAMPERED"), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--src", str(dst), "--dst", str(root / "dst3"),
             "--console-log", str(dst / "nonempty-console.log"), "--manifest-list", MF1,
             "--expect-total", "2", "--skip-docker"],
            capture_output=True, text=True, timeout=180)
        # 用被篡改的目录当源：身份对不上主日志 → 必须失败
        check("篡改后作为源再归档：exit 2（身份不一致被抓到）", proc.returncode == 2,
              f"code={proc.returncode}")


def main() -> int:
    scenario_ok()
    scenario_skip_docker_not_residue_ok()
    scenario_duplicate_meta_key_rejected()
    scenario_non_bool_step_rejected()
    scenario_manifest_missing()
    scenario_manifest_orphan()
    scenario_identity_mismatch()
    scenario_readback_tamper()
    print()
    print(f"RESULT: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
