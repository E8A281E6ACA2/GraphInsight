#!/usr/bin/env python3
"""归档非空 disposable 轮次证据并**机器复验**（fail-closed）。

把某一轮 `--authorize NONEMPTY-DISPOSABLE-AUTHORIZED` 执行的证据从轮次目录（会被下一轮覆盖）
归档到**固定目录**，并机器复验：

  1. 单 RUN_ID：meta 的 RUN_ID == steps JSON 的 run_id == 期望 RUN_ID；
  2. CHECKS_TOTAL == 期望总步数、CHECKS_FAILED == 0；
  3. ROUND_EXIT == 0；
  4. JSON 可解析（`json.load` 直过）+ 步骤状态与 meta 统计一致
     （`len(steps) == CHECKS_TOTAL`、`count(ok=false) == CHECKS_FAILED`、`ROUND_EXIT==2 iff failed>0`）；
  5. 零残留：`gi-nonempty-*` 容器/网络/卷三者复查均为空（`--skip-docker` 可跳过，仅跑文件复验）。

归档内容：主日志、steps JSON、meta、本轮 backfill manifests、失败注入日志、零残留复查、
机器复验结果（`machine_verify.json`）。任一项复验失败 → exit 2，不留"看起来绿"的归档。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RESIDUE_PREFIX = "gi-nonempty-"


def say(line: str = "") -> None:
    print(line, flush=True)


def _read_meta_int(text: str, key: str) -> Optional[int]:
    m = re.search(rf"{re.escape(key)}=(-?\d+)", text)
    return int(m.group(1)) if m else None


def _read_meta_str(text: str, key: str) -> Optional[str]:
    m = re.search(rf"{re.escape(key)}=([^\r\n]*)", text)
    return m.group(1).strip() if m else None


def _read_text_any(path: Path) -> str:
    """读文本并自动识别 BOM（PowerShell `>` 重定向默认是 UTF-16LE，直接按 utf-8 读会全乱码）。"""
    raw = path.read_bytes()
    for bom, enc in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8-sig")):
        if raw.startswith(bom):
            return raw.decode(enc, errors="replace")
    return raw.decode("utf-8", errors="replace")


def docker_residue() -> Tuple[List[str], List[str], List[str], Optional[str]]:
    """复查 gi-nonempty-* 残留（容器/网络/卷）。docker 不可用时返回 error。"""
    try:
        cps = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=60)
        nets = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"],
                              capture_output=True, text=True, timeout=60)
        vols = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                              capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 - docker 不可用 = 无法复查
        return [], [], [], f"docker unavailable: {type(exc).__name__}: {exc}"
    if cps.returncode or nets.returncode or vols.returncode:
        return [], [], [], f"docker rc={cps.returncode}/{nets.returncode}/{vols.returncode}"
    pick = lambda out: [x.strip() for x in out.splitlines()
                        if x.strip().startswith(RESIDUE_PREFIX)]
    return pick(cps.stdout), pick(nets.stdout), pick(vols.stdout), None


def main() -> int:
    ap = argparse.ArgumentParser(description="归档非空 disposable 证据并机器复验")
    ap.add_argument("--src", required=True, help="源轮次证据目录（会被下一轮覆盖）")
    ap.add_argument("--dst", required=True, help="固定归档目录")
    ap.add_argument("--console-log", required=True, help="本轮主日志文件")
    ap.add_argument("--kb-id", required=True, help="本轮 KB id（用于挑选本轮 manifests）")
    ap.add_argument("--expect-run-id", default=None, help="期望 RUN_ID（不传则只校验自洽）")
    ap.add_argument("--expect-total", type=int, default=None, help="期望 CHECKS_TOTAL")
    ap.add_argument("--skip-docker", action="store_true", help="跳过 docker 残留复查")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    console = Path(args.console_log)
    problems: List[str] = []
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        say(("  [PASS] " if ok else "  [FAIL] ") + name + (f" ({detail})" if detail and not ok else ""))

    # ---- 1. 源文件存在 ----
    steps_src = src / "nonempty_steps.json"
    meta_src = src / "nonempty_meta.txt"
    mf_src = src / "manifests"
    for name, path, must_be_dir in (("steps JSON", steps_src, False),
                                    ("meta", meta_src, False),
                                    ("manifests 目录", mf_src, True)):
        ok = path.is_dir() if must_be_dir else path.is_file()
        check(f"源：{name} 存在", ok, str(path))

    meta_text = meta_src.read_text(encoding="utf-8") if meta_src.is_file() else ""
    try:
        doc = json.loads(steps_src.read_text(encoding="utf-8")) if steps_src.is_file() else {}
        json_ok = True
    except Exception as exc:  # noqa: BLE001
        doc = {}
        json_ok = False
        check("JSON 可解析（`json.load` 直过）", False, f"{type(exc).__name__}: {exc}")
    if json_ok:
        check("JSON 可解析（`json.load` 直过）", True)

    # ---- 2. 单 RUN_ID ----
    meta_run = _read_meta_str(meta_text, "RUN_ID")
    json_run = doc.get("run_id")
    check("单 RUN_ID：meta.RUN_ID == steps.run_id",
          bool(meta_run) and meta_run == json_run, f"meta={meta_run} json={json_run}")
    if args.expect_run_id:
        check("单 RUN_ID：与期望 RUN_ID 一致",
              meta_run == args.expect_run_id, f"meta={meta_run} expect={args.expect_run_id}")

    # ---- 3. meta 统计 ----
    total = _read_meta_int(meta_text, "CHECKS_TOTAL")
    failed = _read_meta_int(meta_text, "CHECKS_FAILED")
    round_exit = _read_meta_int(meta_text, "ROUND_EXIT")
    if args.expect_total is not None:
        check(f"CHECKS_TOTAL == {args.expect_total}", total == args.expect_total, f"total={total}")
    check("CHECKS_FAILED == 0", failed == 0, f"failed={failed}")
    check("ROUND_EXIT == 0", round_exit == 0, f"exit={round_exit}")

    # ---- 4. 步骤状态与 meta 统计一致（第十轮判据） ----
    steps = doc.get("steps")
    failed_count = (sum(1 for s in steps if isinstance(s, dict) and s.get("ok") is False)
                    if isinstance(steps, list) else -1)
    check("步骤状态一致：len(steps) == CHECKS_TOTAL",
          isinstance(steps, list) and total is not None and len(steps) == total,
          f"len={len(steps) if isinstance(steps, list) else 'NA'} total={total}")
    check("步骤状态一致：count(ok=false) == CHECKS_FAILED",
          failed_count == failed, f"count={failed_count} failed={failed}")
    check("步骤状态一致：ROUND_EXIT == 2 iff failed>0",
          round_exit == (2 if (failed or 0) > 0 else 0), f"exit={round_exit} failed={failed}")

    # ---- 5. 零残留 ----
    if args.skip_docker:
        say("  [SKIP] 零残留复查（--skip-docker）")
        residue_ok, residue_detail = True, "skipped"
        residue: Dict[str, List[str]] = {"containers": [], "networks": [], "volumes": []}
    else:
        cons, nets, vols, err = docker_residue()
        residue = {"containers": cons, "networks": nets, "volumes": vols}
        residue_ok = err is None and not (cons or nets or vols)
        residue_detail = err or f"containers={cons} nets={nets} vols={vols}"
        check("零残留：gi-nonempty-* 容器/网络/卷均为空", residue_ok, residue_detail)

    # ---- 归档 ----
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "manifests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(steps_src, dst / "nonempty_steps.json")
    shutil.copy2(meta_src, dst / "nonempty_meta.txt")
    if console.is_file():
        # 主日志可能由 PowerShell 重定向成 UTF-16LE：归档统一转码为 UTF-8，保证可读可比对。
        (dst / "nonempty-console.log").write_text(_read_text_any(console), encoding="utf-8")

    copied = 0
    for mf in sorted((mf_src).glob("*.json")) if mf_src.is_dir() else []:
        try:
            mdoc = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if str(mdoc.get("kb_id") or "") == args.kb_id:
            shutil.copy2(mf, dst / "manifests" / mf.name)
            copied += 1
    check(f"归档：本轮 manifests（kb_id={args.kb_id}）已复制 ≥1", copied >= 1, f"copied={copied}")

    # 失败注入日志：从主日志提取"失败重放/幂等/回滚"段落（证据子集，不重跑）
    failure_lines: List[str] = []
    if console.is_file():
        for line in _read_text_any(console).splitlines():
            if any(k in line for k in ("失败重放", "幂等", "回滚", "预检", "零新增")):
                failure_lines.append(line)
    (dst / "failure_injection.log").write_text(
        "\n".join(failure_lines) + ("\n" if failure_lines else ""), encoding="utf-8")
    check("归档：失败注入日志非空", bool(failure_lines), f"lines={len(failure_lines)}")

    (dst / "zero_residue_check.txt").write_text(
        f"RESIDUE_PREFIX={RESIDUE_PREFIX}\n"
        f"containers={residue.get('containers')}\n"
        f"networks={residue.get('networks')}\n"
        f"volumes={residue.get('volumes')}\n"
        f"ok={residue_ok}\n"
        f"detail={residue_detail}\n",
        encoding="utf-8",
    )

    # ---- 机器复验结果 ----
    failed_names = [n for n, ok, _ in checks if not ok]
    verify: Dict[str, Any] = {
        "schema": "gate0-nonempty-archive-verify-v1",
        "run_id": meta_run,
        "kb_id": args.kb_id,
        "checks_total": total,
        "checks_failed": failed,
        "round_exit": round_exit,
        "json_parsable": json_ok,
        "steps_len": len(steps) if isinstance(steps, list) else None,
        "steps_failed_count": failed_count,
        "residue_ok": residue_ok,
        "residue_detail": residue_detail,
        "manifests_copied": copied,
        "machine_checks_total": len(checks),
        "machine_failed": len(failed_names),
        "machine_failed_names": failed_names,
        "result": "PASS" if not failed_names else "FAIL",
    }
    (dst / "machine_verify.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    say()
    say(f"归档目录：{dst}")
    say(f"RESULT: {len(checks) - len(failed_names)} passed, {len(failed_names)} failed")
    for name in failed_names:
        say("  FAILED: " + name)
    if failed_names:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
