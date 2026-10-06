#!/usr/bin/env python3
r"""
Wave 9 密钥扫描固定基线对照（可复算证据，不是门禁）

问题：`check_artifact_secrets.py` 命中即非零退出，而 `go-backend`/`frontend/src` 这类目录
本来就带着 CI 扫描范围外的历史命中（§11.4）。"本轮零新增"不能靠"扫过没报错"来证，
必须把同一批文件在**固定提交基线**与当前工作树上的命中集合做差。

做法（每个改动文件两条命，逐字对照）：
  1. 基线内容 = `git show <BASELINE>:<path>`（新增文件没有基线，基线命中集为空）；
  2. 工作树内容 = 磁盘上的当前字节；
  3. 两份都写进临时目录（保留相对路径，让扫描器的路径排除规则两边一致），分别跑扫描器；
  4. 解析 `SECRET_FINDING kind=… match_sha256=… value=…` 四元组集合（去掉 file 前缀差异），
     断言 工作树命中 − 基线命中 = ∅；
  5. 同时断言两次调用的真实 returncode（扫描器 pass=0 / fail=1 / 前置缺失=2），
     命中数超过 40 会被截断，截断即判失败——否则"零新增"是看不全的假绿。

基线取固定 SHA（Wave 8 前驱 `97645ac`）而不是移动的 HEAD：HEAD 会被本轮提交本身推移，
"对照 HEAD"会退化成"自己跟自己比"。

运行：python backend/tests/_w9_secret_baseline.py 97645ac
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent.parent
SCANNER = REPO / "backend" / "tests" / "check_artifact_secrets.py"
SOURCE_PREFIXES = ("backend/", "go-backend/", "frontend/", "docs/", "scripts/")


def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", env=env)


def git(*args: str) -> subprocess.CompletedProcess:
    return run(["git", *args], REPO)


def changed_files(baseline: str) -> list[str]:
    tracked = git("diff", "--name-only", baseline)
    if tracked.returncode != 0:
        raise SystemExit(f"git diff --name-only {baseline} EXIT={tracked.returncode}: {tracked.stderr.strip()}")
    untracked = git("ls-files", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        raise SystemExit(f"git ls-files --others EXIT={untracked.returncode}: {untracked.stderr.strip()}")
    names = [line.strip() for line in (tracked.stdout + untracked.stdout).splitlines() if line.strip()]
    return sorted({n for n in names if n.startswith(SOURCE_PREFIXES) and (REPO / n).exists()})


def scan_one(sample: Path) -> tuple[Counter, int, int]:
    """返回 (命中四元组计数, 扫描器 returncode, 声明的 findings 数)。

    同一个字面量在一文件里出现多次是常态（例如 fixture 口令反复声明），所以用计数而不是集合：
    集合会把"新增一处重复"洗成零新增。
    """
    proc = run([sys.executable, str(SCANNER), "--path", str(sample)], REPO)
    findings: Counter = Counter()
    declared = -1
    truncated = False
    for line in proc.stdout.splitlines():
        if line.startswith("SECRET_FINDING "):
            parts = dict(
                kv.split("=", 1) for kv in line.split(" ")[1:] if "=" in kv and not kv.startswith("file=")
            )
            findings[f"{parts.get('kind')}|{parts.get('match_sha256')}|{parts.get('value')}"] += 1
        elif line.startswith("SECRET_SCAN_SUMMARY "):
            for token in line.split(" "):
                if token.startswith("findings="):
                    declared = int(token.split("=", 1)[1])
        elif line.startswith("SECRET_FINDINGS_TRUNCATED"):
            truncated = True
    if truncated:
        raise SystemExit(f"命中超过 40 条被截断，无法逐字比对: {sample}")
    return findings, proc.returncode, declared


def materialize(root: Path, rel: str, content: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def main() -> int:
    if len(sys.argv) != 2 or len(sys.argv[1]) < 7:
        raise SystemExit("用法：_w9_secret_baseline.py <BASELINE_SHA>")
    baseline = sys.argv[1].strip()
    resolved = git("rev-parse", baseline)
    if resolved.returncode != 0:
        raise SystemExit(f"基线 SHA 解析失败: {baseline}")
    baseline_sha = resolved.stdout.strip()
    print(f"BASELINE_SHA={baseline_sha}")

    files = changed_files(baseline_sha)
    if not files:
        raise SystemExit("基线对照文件清单为空，等于没扫")
    print(f"SCAN_SCOPE files={len(files)}")

    failures: list[str] = []
    total_new = 0
    with tempfile.TemporaryDirectory(prefix="gi_w9_base_") as tmp_cur, tempfile.TemporaryDirectory(
        prefix="gi_w9_base_"
    ) as tmp_base:
        cur_root, base_root = Path(tmp_cur), Path(tmp_base)
        for rel in files:
            current = (REPO / rel).read_bytes()
            shown = git("show", f"{baseline_sha}:{rel}")
            if shown.returncode == 0:
                base_bytes = shown.stdout.encode("utf-8")
                base_state = "baseline"
            elif "not in" in (shown.stderr or ""):
                base_bytes = b""
                base_state = "added"
            else:
                failures.append(f"{rel}: 基线内容取不到 EXIT={shown.returncode} stderr={shown.stderr.strip()}")
                continue

            cur_findings, cur_rc, cur_declared = scan_one(materialize(cur_root, rel, current))
            base_findings, base_rc, base_declared = scan_one(materialize(base_root, rel, base_bytes))
            # 扫描器必须真的跑完：pass=0 或 fail=1；2 是前置缺失（配置错），不能当"无命中"
            for label, rc in (("worktree", cur_rc), ("baseline", base_rc)):
                if rc not in (0, 1):
                    failures.append(f"{rel}: 扫描器 {label} EXIT={rc}")
            for label, declared, got in (("worktree", cur_declared, cur_findings), ("baseline", base_declared, base_findings)):
                if declared != sum(got.values()):
                    failures.append(f"{rel}: {label} 声明 findings={declared} 实解析 {sum(got.values())}")

            new = cur_findings - base_findings
            total_new += sum(new.values())
            status = "OK" if not new else "CHECK"
            print(
                f"{status} {rel} [{base_state}] baseline={sum(base_findings.values())} worktree={sum(cur_findings.values())} "
                f"new={sum(new.values())} rc_wt={cur_rc} rc_base={base_rc}"
            )
            for item in sorted(new):
                print(f"  NEW_FINDING {rel} {item}")

    print(f"SECRET_BASELINE_SUMMARY files={len(files)} new_findings={total_new} failed={len(failures)}")
    for line in failures:
        print(f"FAIL {line}")
    return 0 if not failures and total_new == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
