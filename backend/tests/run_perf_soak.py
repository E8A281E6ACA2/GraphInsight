#!/usr/bin/env python3
"""Batch soak/capacity runner built on top of run_perf_probe.py."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "run_perf_probe.py"
PYTHON_EXE = ROOT / ".venv" / "bin" / "python"


def _resolve_python(explicit: str) -> str:
    if explicit:
        return explicit
    if PYTHON_EXE.exists():
        return str(PYTHON_EXE)
    # Windows 开发机没有 backend/.venv（Linux 口径），退回当前解释器，
    # 否则 soak 只能在单一平台跑，容量趋势无法在提交前复核。
    return sys.executable


def _build_probe_cmd(args: argparse.Namespace, output_json: Path, output_md: Path) -> list[str]:
    cmd = [
        _resolve_python(args.python),
        str(PROBE),
        "--base-url",
        args.base_url.rstrip("/"),
        "--preset",
        args.preset,
        "--requests",
        str(args.requests),
        "--concurrency",
        str(args.concurrency),
        "--max-error-rate",
        str(args.max_error_rate),
        "--max-p95-ms",
        str(args.max_p95_ms),
        "--output-json",
        str(output_json),
        "--output-markdown",
        str(output_md),
    ]
    if args.admin_email:
        cmd.extend(["--admin-email", args.admin_email])
    if args.admin_password:
        cmd.extend(["--admin-password", args.admin_password])
    if args.token:
        cmd.extend(["--token", args.token])
    if args.question:
        cmd.extend(["--question", args.question])
    if args.cypher:
        cmd.extend(["--cypher", args.cypher])
    if args.top_k:
        cmd.extend(["--top-k", str(args.top_k)])
    if args.build_force:
        cmd.append("--build-force")
    if args.skip_route_owner_check:
        cmd.append("--skip-route-owner-check")
    for case in args.case:
        cmd.extend(["--case", case])
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repeated GraphInsight perf probes for soak/capacity validation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--preset", choices=["readonly", "release"], default="readonly")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rounds", type=int, default=3, help="Number of probe rounds to execute")
    parser.add_argument("--sleep-seconds", type=float, default=5.0, help="Pause between rounds")
    parser.add_argument("--timeout-seconds", type=float, default=0.0, help="Global timeout; 0 disables")
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--max-p95-ms", type=float, default=0.0)
    parser.add_argument("--admin-email", default="")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--question", default="")
    parser.add_argument("--cypher", default="")
    parser.add_argument("--top-k", type=int, default=0)
    parser.add_argument("--build-force", action="store_true")
    parser.add_argument("--skip-route-owner-check", action="store_true")
    parser.add_argument("--python", default=os.getenv("SOAK_PYTHON", ""), help="Interpreter used to run each probe round")
    parser.add_argument("--output-dir", default="artifacts/perf-soak")
    args = parser.parse_args()

    if args.rounds < 1:
        raise SystemExit("--rounds must be >= 1")
    if args.requests < 1:
        raise SystemExit("--requests must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    summary: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url.rstrip("/"),
        "preset": args.preset,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "rounds": args.rounds,
        "sleep_seconds": args.sleep_seconds,
        "round_results": [],
    }

    failures = 0
    for index in range(1, args.rounds + 1):
        if args.timeout_seconds > 0 and (time.time() - started_at) > args.timeout_seconds:
            print(f"SOAK_TIMEOUT elapsed_seconds={round(time.time() - started_at, 3)}")
            failures += 1
            break

        round_json = output_dir / f"round-{index}.json"
        round_md = output_dir / f"round-{index}.md"
        cmd = _build_probe_cmd(args, round_json, round_md)
        print(
            f"SOAK_ROUND_BEGIN round={index}/{args.rounds} preset={args.preset} "
            f"requests={args.requests} concurrency={args.concurrency}"
        )
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=str(ROOT.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            print(stdout)
        if stderr:
            print(f"[stderr]\n{stderr}")

        round_entry: dict[str, object] = {
            "round": index,
            "exit_code": proc.returncode,
            "json": str(round_json),
            "markdown": str(round_md),
        }
        if round_json.exists():
            round_entry["report"] = json.loads(round_json.read_text(encoding="utf-8"))
        summary["round_results"].append(round_entry)  # type: ignore[index]

        if proc.returncode != 0:
            failures += 1
            print(f"SOAK_ROUND_FAIL round={index} exit_code={proc.returncode}")
        else:
            print(f"SOAK_ROUND_OK round={index}")

        if index < args.rounds and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    summary["failed_rounds"] = failures
    summary["duration_seconds"] = round(time.time() - started_at, 3)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"SOAK_SUMMARY rounds={args.rounds} failed_rounds={failures} "
        f"duration_seconds={summary['duration_seconds']} summary={summary_path}"
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
