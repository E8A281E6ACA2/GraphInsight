#!/usr/bin/env python3
"""归档非空 disposable 轮次证据并**机器复验**（fail-closed）。

合同（本轮修订）：
  1. **每步布尔 ok**：所有校验步骤逐条记 `ok=true/false` 进 `machine_verify.json["steps"]`，
     汇总 `machine_failed`；不允许"没检查就当通过"。
  2. **身份与统计三方一致**：主日志 / steps JSON / meta 的 RUN_ID 必须一致且与期望相同；
     `CHECKS_TOTAL / CHECKS_FAILED / ROUND_EXIT` 与 steps 实际状态自洽
     （`len(steps)==CHECKS_TOTAL`、`count(ok=false)==CHECKS_FAILED`、`ROUND_EXIT==2 iff failed>0`）。
  3. **重复关键字段拒绝**：meta 里 `RUN_ID/EXEC_TIME/CHECKS_TOTAL/CHECKS_FAILED/ROUND_EXIT`
     任一出现多次 → 判失败（防止歧义/覆盖式伪造）。
  4. **跳过 Docker 不得记 residue_ok=true**：只能标
     `residue_state=UNVERIFIED`（"文件校验通过、残留未取证"）；`residue_ok` 恒为 false。
  5. **归档后读回最终文件**：写盘后必须读回 dst 的 steps/meta/machine_verify 复核，
     复核结果写入独立 `recheck.json`（不能只写不读回）。
  6. **明确清单关联本轮 manifests**：用 `--manifest-list` 逐个文件名精确关联（不按 kb_id 模糊筛选）；
     清单内文件缺失即失败，清单外本轮文件需被 `manifest_orphans` 记录以便人工核对。
  7. **保留原证据**：只读源目录，不删改；归档目录新增复核记录。

任一项失败 → exit 2，不留"看起来绿"的归档。
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
# meta 关键字段：任一重复即拒绝（身份/统计不允许二义）
META_KEY_FIELDS = ("RUN_ID", "EXEC_TIME", "TARGET_KIND", "CHECKS_TOTAL", "CHECKS_FAILED", "ROUND_EXIT")


def say(line: str = "") -> None:
    print(line, flush=True)


def parse_meta_values(text: str) -> Dict[str, str]:
    """解析 meta 的 `KEY=VALUE` 行（后出现的覆盖先出现的，重复由 meta_duplicate_keys 判失败）。"""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def meta_duplicate_keys(text: str, keys: Tuple[str, ...] = META_KEY_FIELDS) -> List[str]:
    """返回 meta 中**重复出现**的关键字段（防歧义/覆盖式伪造）。"""
    counts: Dict[str, int] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        counts[key] = counts.get(key, 0) + 1
    return sorted(k for k in keys if counts.get(k, 0) > 1)


def _read_text_any(path: Path) -> str:
    """读文本并自动识别 BOM（PowerShell `>` 重定向默认是 UTF-16LE，按 utf-8 读会全乱码）。"""
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
    except Exception as exc:  # noqa: BLE001 - docker 不可用 = 无法取证
        return [], [], [], f"docker unavailable: {type(exc).__name__}: {exc}"
    if cps.returncode or nets.returncode or vols.returncode:
        return [], [], [], f"docker rc={cps.returncode}/{nets.returncode}/{vols.returncode}"
    pick = lambda out: [x.strip() for x in out.splitlines() if x.strip().startswith(RESIDUE_PREFIX)]
    return pick(cps.stdout), pick(nets.stdout), pick(vols.stdout), None


class CheckLog:
    """逐条记录布尔 ok 的校验步骤。"""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        say(("  [PASS] " if ok else "  [FAIL] ") + name + (f" ({detail})" if detail and not ok else ""))
        return bool(ok)

    @property
    def failed(self) -> List[str]:
        return [s["name"] for s in self.steps if not s["ok"]]


def main() -> int:
    ap = argparse.ArgumentParser(description="归档非空 disposable 证据并机器复验")
    ap.add_argument("--src", required=True, help="源轮次证据目录（只读，保留原证据）")
    ap.add_argument("--dst", required=True, help="固定归档目录")
    ap.add_argument("--console-log", required=True, help="本轮主日志文件")
    ap.add_argument("--manifest-list", default="",
                    help="本轮 manifests 的**明确文件名清单**（逗号分隔），不用 kb 模糊筛选")
    ap.add_argument("--expect-run-id", default=None)
    ap.add_argument("--expect-total", type=int, default=None)
    ap.add_argument("--skip-docker", action="store_true",
                    help="跳过 docker 残留复查（只能标残留未取证，不得记 residue_ok=true）")
    args = ap.parse_args()

    src, dst, console = Path(args.src), Path(args.dst), Path(args.console_log)
    log = CheckLog()

    steps_src = src / "nonempty_steps.json"
    meta_src = src / "nonempty_meta.txt"
    mf_src = src / "manifests"
    for name, path, is_dir in (("steps JSON", steps_src, False), ("meta", meta_src, False),
                               ("manifests 目录", mf_src, True)):
        log.add(f"源：{name} 存在", path.is_dir() if is_dir else path.is_file(), str(path))

    meta_text = meta_src.read_text(encoding="utf-8") if meta_src.is_file() else ""
    console_text = _read_text_any(console) if console.is_file() else ""
    try:
        doc = json.loads(steps_src.read_text(encoding="utf-8")) if steps_src.is_file() else {}
        log.add("JSON 可解析（`json.load` 直过）", True)
    except Exception as exc:  # noqa: BLE001
        doc = {}
        log.add("JSON 可解析（`json.load` 直过）", False, f"{type(exc).__name__}: {exc}")

    # ---- 身份三方一致 ----
    meta_vals = parse_meta_values(meta_text)
    meta_run, json_run = meta_vals.get("RUN_ID"), doc.get("run_id")
    log.add("身份：meta.RUN_ID == steps.run_id", bool(meta_run) and meta_run == json_run,
            f"meta={meta_run} json={json_run}")
    if args.expect_run_id:
        log.add("身份：与期望 RUN_ID 一致", meta_run == args.expect_run_id,
                f"meta={meta_run} expect={args.expect_run_id}")
    log.add("身份：主日志含同一 RUN_ID（三方一致）",
            bool(meta_run) and meta_run in console_text, f"run={meta_run}")

    # ---- 重复关键字段拒绝 ----
    dups = meta_duplicate_keys(meta_text)
    log.add("meta 关键字段无重复（重复即拒绝）", not dups, f"duplicates={dups}")

    # ---- 统计与步骤状态自洽 ----
    def _int(key: str) -> Optional[int]:
        raw = meta_vals.get(key)
        if raw is None:
            return None
        m = re.search(r"-?\d+", raw)
        return int(m.group(0)) if m else None

    total, failed, round_exit = _int("CHECKS_TOTAL"), _int("CHECKS_FAILED"), _int("ROUND_EXIT")
    if args.expect_total is not None:
        log.add(f"统计：CHECKS_TOTAL == {args.expect_total}", total == args.expect_total, f"total={total}")
    log.add("统计：CHECKS_FAILED == 0", failed == 0, f"failed={failed}")
    log.add("统计：ROUND_EXIT == 0", round_exit == 0, f"exit={round_exit}")
    steps = doc.get("steps")
    failed_count = (sum(1 for s in steps if isinstance(s, dict) and s.get("ok") is False)
                    if isinstance(steps, list) else -1)
    log.add("步骤状态：每个 step 都有布尔 ok",
            isinstance(steps, list) and all(isinstance(s, dict) and isinstance(s.get("ok"), bool) for s in steps),
            f"steps={len(steps) if isinstance(steps, list) else 'NA'}")
    log.add("步骤状态：len(steps) == CHECKS_TOTAL",
            isinstance(steps, list) and total is not None and len(steps) == total,
            f"len={len(steps) if isinstance(steps, list) else 'NA'} total={total}")
    log.add("步骤状态：count(ok=false) == CHECKS_FAILED", failed_count == failed,
            f"count={failed_count} failed={failed}")
    log.add("步骤状态：ROUND_EXIT == 2 iff failed>0",
            round_exit == (2 if (failed or 0) > 0 else 0), f"exit={round_exit} failed={failed}")

    # ---- 零残留（跳过时只能标未取证） ----
    if args.skip_docker:
        say("  [SKIP] docker 残留复查（--skip-docker）：仅文件校验通过，残留未取证")
        residue = {"containers": [], "networks": [], "volumes": []}
        residue_ok, residue_state = False, "UNVERIFIED"
        residue_detail = "文件校验通过、残留未取证（--skip-docker）"
    else:
        cons, nets, vols, err = docker_residue()
        residue = {"containers": cons, "networks": nets, "volumes": vols}
        residue_ok = err is None and not (cons or nets or vols)
        residue_state = "VERIFIED" if err is None else "UNVERIFIED"
        residue_detail = err or f"containers={cons} nets={nets} vols={vols}"
        log.add("零残留：gi-nonempty-* 容器/网络/卷均为空", residue_ok, residue_detail)

    # ---- 归档（只读源，保留原证据） ----
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "manifests").mkdir(parents=True, exist_ok=True)
    shutil.copy2(steps_src, dst / "nonempty_steps.json")
    shutil.copy2(meta_src, dst / "nonempty_meta.txt")
    if console.is_file():
        (dst / "nonempty-console.log").write_text(console_text, encoding="utf-8")

    # 明确清单关联本轮 manifests（不按 kb_id 模糊筛选）
    wanted = [x.strip() for x in args.manifest_list.split(",") if x.strip()]
    log.add("清单：--manifest-list 非空（精确关联本轮 manifests）", bool(wanted), f"list={wanted}")
    copied, missing, kb_ids = [], [], set()
    for name in wanted:
        f = mf_src / name
        if not f.is_file():
            missing.append(name)
            continue
        try:
            kb_ids.add(str(json.loads(f.read_text(encoding="utf-8")).get("kb_id") or ""))
        except Exception:  # noqa: BLE001
            pass
        shutil.copy2(f, dst / "manifests" / name)
        copied.append(name)
    log.add("清单：清单内文件全部存在并已复制", not missing and bool(copied),
            f"missing={missing} copied={len(copied)}")
    log.add("清单：本轮 manifests 的 kb_id 唯一（交叉校验）",
            len(kb_ids - {""}) == 1, f"kb_ids={sorted(kb_ids)}")
    # 清单外同 kb 的 manifest（人工核对用，不算失败）
    orphans = []
    if len(kb_ids - {""}) == 1:
        kb = next(iter(kb_ids - {""}))
        for f in sorted(mf_src.glob("*.json")) if mf_src.is_dir() else []:
            if f.name in wanted:
                continue
            try:
                if str(json.loads(f.read_text(encoding="utf-8")).get("kb_id") or "") == kb:
                    orphans.append(f.name)
            except Exception:  # noqa: BLE001
                continue
    log.add("清单：无清单外同 kb 的 manifest 遗漏", not orphans, f"orphans={orphans}")

    failure_lines = [ln for ln in console_text.splitlines()
                     if any(k in ln for k in ("失败重放", "幂等", "回滚", "预检", "零新增"))]
    (dst / "failure_injection.log").write_text(
        "\n".join(failure_lines) + ("\n" if failure_lines else ""), encoding="utf-8")
    log.add("归档：失败注入日志非空", bool(failure_lines), f"lines={len(failure_lines)}")

    (dst / "zero_residue_check.txt").write_text(
        f"RESIDUE_PREFIX={RESIDUE_PREFIX}\n"
        f"containers={residue['containers']}\n"
        f"networks={residue['networks']}\n"
        f"volumes={residue['volumes']}\n"
        f"residue_ok={residue_ok}\n"
        f"residue_state={residue_state}\n"
        f"detail={residue_detail}\n",
        encoding="utf-8",
    )

    verify: Dict[str, Any] = {
        "schema": "gate0-nonempty-archive-verify-v2",
        "run_id": meta_run,
        "kb_ids": sorted(kb_ids - {""}),
        "checks_total": total,
        "checks_failed": failed,
        "round_exit": round_exit,
        "json_parsable": bool(doc),
        "steps_len": len(steps) if isinstance(steps, list) else None,
        "steps_failed_count": failed_count,
        "manifest_list": wanted,
        "manifests_copied": copied,
        "manifest_missing": missing,
        "manifest_orphans": orphans,
        "residue_ok": residue_ok,
        "residue_state": residue_state,
        "residue_detail": residue_detail,
        "machine_checks_total": len(log.steps),
        "machine_failed": len(log.failed),
        "machine_failed_names": log.failed,
        "steps": log.steps,
        "result": "PASS" if not log.failed else "FAIL",
    }
    (dst / "machine_verify.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # ---- 归档后读回最终文件（复核，不能只写不读回） ----
    recheck: Dict[str, Any] = {"schema": "gate0-nonempty-archive-recheck-v1", "checks": []}

    def rk(name: str, ok: bool, detail: str = "") -> None:
        recheck["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
        say(("  [PASS] 读回 " if ok else "  [FAIL] 读回 ") + name +
            (f" ({detail})" if detail and not ok else ""))

    try:
        back_steps = json.loads((dst / "nonempty_steps.json").read_text(encoding="utf-8"))
        back_meta = (dst / "nonempty_meta.txt").read_text(encoding="utf-8")
        back_verify = json.loads((dst / "machine_verify.json").read_text(encoding="utf-8"))
        rk("归档 steps JSON 可解析", True)
        rk("归档 steps.run_id 与源一致", back_steps.get("run_id") == json_run,
           f"archived={back_steps.get('run_id')} src={json_run}")
        rk("归档 steps 条数与源一致",
           isinstance(back_steps.get("steps"), list) and len(back_steps["steps"]) == (
               len(steps) if isinstance(steps, list) else -1))
        rk("归档 meta 含源 RUN_ID", bool(meta_run) and f"RUN_ID={meta_run}" in back_meta)
        rk("归档 meta 关键字段无重复", not meta_duplicate_keys(back_meta))
        rk("读回 machine_verify 的 result 与步骤失败数自洽",
           back_verify.get("result") == ("PASS" if not back_verify.get("machine_failed") else "FAIL"))
        rk("读回 machine_verify.steps 每步都有布尔 ok",
           all(isinstance(s, dict) and isinstance(s.get("ok"), bool)
               for s in back_verify.get("steps", [])))
        rk("读回：residue_ok=true 必须有 VERIFIED 状态（跳过时不得为 true）",
           not (back_verify.get("residue_ok") is True and back_verify.get("residue_state") != "VERIFIED"),
           f"ok={back_verify.get('residue_ok')} state={back_verify.get('residue_state')}")
        rk("清单内 manifests 全部落盘",
           all((dst / "manifests" / n).is_file() for n in wanted), f"list={wanted}")
    except Exception as exc:  # noqa: BLE001
        rk("归档后读回最终文件", False, f"{type(exc).__name__}: {exc}")

    recheck_failed = [c["name"] for c in recheck["checks"] if not c["ok"]]
    recheck["failed"] = len(recheck_failed)
    recheck["result"] = "PASS" if not recheck_failed else "FAIL"
    (dst / "recheck.json").write_text(
        json.dumps(recheck, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    say()
    say(f"归档目录：{dst}（源证据保留，未删改）")
    say(f"RESULT: {len(log.steps) - len(log.failed)} passed, {len(log.failed)} failed")
    for name in log.failed:
        say("  FAILED: " + name)
    if log.failed or recheck_failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
