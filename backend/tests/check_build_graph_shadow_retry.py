#!/usr/bin/env python3
"""
方向B P2 取证：普通 build_graph 影子失败调用链 + 真实 job_service 退避重试
（临时 SQLite 隔离 + UTF-8 子进程捕获，禁触碰共享 dev 活栈）

三段必证（对齐审计清单）：
  A. relay（P1#1）：真实 `retrieval_orchestrator.index_chunks` 把 dual_write 影子写失败
     吸收进 failures、不上抛，主库(v2)已写 / 影子(v3)尝试未落地。
  B. retry（P1#3 + P2）：真实 `execute_build_graph` 见 vector_failures 非空抛 RuntimeError（
     不再 completed），真实 `job_service.run_job` 捕获后按 max_retries 递增 retry_count、
     指数退避排重试，耗尽后落终态 failed——全程从未判成功。
  C. clean 对照组：无 vector_failures 的干净 build_graph 仍判 succeeded/completed，
     证明 fail-closed 修复没误伤正常路径。

隔离与 UTF-8 铁律同 check_b0：GRAPHINSIGHT_BACKEND_ENV_FILE 指向含 ADMIN_DATABASE_URL=sqlite
的临时 env 文件；子进程传 PYTHONUTF8/PYTHONIOENCODING 并断言真实 returncode；不用手工 -X utf8。

运行：python backend/tests/check_build_graph_shadow_retry.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
DRIVER = str(Path("tests") / "build_graph_shadow_retry_driver.py")
FAILURES: list = []

KB = "kb-bgsr"
DOC = "doc-1"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def obj_marker(out: str, marker: str) -> dict:
    for line in out.splitlines():
        if line.startswith(marker):
            try:
                value = json.loads(line[len(marker):])
            except json.JSONDecodeError:
                step(f"标记 {marker} 非法 JSON", False, line[:160])
                return {}
            if not isinstance(value, dict):
                step(f"标记 {marker} 非对象", False, str(value)[:160])
                return {}
            return value
    step(f"标记 {marker} 缺失", False, "")
    return {}


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp

    def _env_file(self, name: str) -> Path:
        env_file = self.tmp / f"{name}.env"
        url = f"sqlite:///{(self.tmp / f'{name}.db').as_posix()}"
        env_file.write_text(
            "\n".join(
                [
                    f"ADMIN_DATABASE_URL={url}",
                    "JOB_AUTO_RETRY_ENABLED=1",
                    "JOB_WORKER_ENABLED=0",
                    "JOB_AUTO_RETRY_BASE_DELAY_SECONDS=2",
                    "JOB_AUTO_RETRY_MAX_DELAY_SECONDS=100",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return env_file

    def run(self, name: str, scenario: str) -> tuple:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self._env_file(name))
        env["PYTHONPATH"] = str(backend_dir)
        proc = subprocess.run(
            [sys.executable, str(backend_dir / DRIVER), "--scenario", scenario],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=env,
            timeout=180,
        )
        return proc.returncode, proc.stdout + proc.stderr


def guard_sqlite(h: Harness) -> None:
    code, out = h.run("guard", "relay_absorbs_failure")
    step("引擎隔离守卫（driver 子进程 exit 0 / 非 sqlite 会退 9）", code == 0, f"exit={code} " + out[-300:])


def section_relay(h: Harness) -> None:
    print("[A] relay：真实 index_chunks 吸收 dual_write 影子失败（P1#1）")
    code, out = h.run("relay", "relay_absorbs_failure")
    step("driver 子进程 exit 0", code == 0, f"exit={code} " + out[-300:])
    r = obj_marker(out, "__RELAY__")
    step("影子失败被吸收、未从 index_chunks 上抛（P1#1 根因）", r.get("raised") is None, f"raised={r.get('raised')}")
    step("失败进入 failures 列表（数量=1）", r.get("failures_count") == 1, f"relay={r}")
    step("向量腿不虚报 indexed=0", r.get("indexed") == 0, f"relay={r}")
    step(
        "主库(v2)已写、影子(v3)尝试未落地（非危险反向）",
        r.get("primary_written") is True and r.get("shadow_attempted") is True and r.get("shadow_written") is False,
        f"relay={r}",
    )


def section_retry(h: Harness) -> None:
    print("[B] retry：execute_build_graph 抛 RuntimeError → 真实 job_service 退避重试到耗尽（P1#3+P2）")
    code, out = h.run("retry", "job_retries_then_exhausts")
    step("driver 子进程 exit 0", code == 0, f"exit={code} " + out[-300:])
    r = obj_marker(out, "__RETRY__")
    step("build_graph 属可重试作业类型", r.get("runnable_has_build_graph") is True, f"runnable={r.get('runnable_has_build_graph')}")
    step("作业系统自动重试开关生效", r.get("auto_retry_enabled") is True, f"enabled={r.get('auto_retry_enabled')}")
    step("全程从未判成功（succeeded_seen=False）", r.get("succeeded_seen") is False, f"succeeded_seen={r.get('succeeded_seen')}")

    timeline = r.get("timeline") or []
    step("max_retries=2 → 共执行 3 次（真实重试确实发生，非只证明可重放异常）", r.get("exec_count") == 3 and len(timeline) == 3, f"exec={r.get('exec_count')} timeline_len={len(timeline)}")

    if timeline:
        first = timeline[0]
        step("第1轮作业边界抛的是 RuntimeError（非 completed）", "RuntimeError" in str(first.get("error_message", "")), f"first={first}")
        step("错误信息含向量未收敛语义（P1#3 修复点）", "向量投影未收敛" in str(first.get("error_message", "")), f"first={first}")
        step("第1轮被判可重试：retry_count 0→1、状态 failed", first.get("retry_count") == 1 and first.get("status") == "failed", f"first={first}")
        step("第1轮排定自动重试（措辞含'已计划自动重试(1/2)'）", "已计划自动重试(1/2)" in str(first.get("error_message", "")), f"first={first}")
        final = r.get("final") or {}
        step("耗尽后落终态 failed、retry_count=2", final.get("status") == "failed" and final.get("retry_count") == 2, f"final={final}")
        step("耗尽终态不再排重试（无'已计划自动重试'）", "已计划自动重试" not in str(final.get("error_message", "")), f"final={final}")

    sched = r.get("sched_calls") or []
    attempts = [s.get("attempt") for s in sched]
    delays = [s.get("delay") for s in sched]
    step("排重试次数=2（attempt 1、2）", attempts == [1, 2], f"attempts={attempts}")
    step("退避为指数翻倍（base=2 → 2s, 4s）", delays == [2, 4], f"delays={delays}")

    kwargs = r.get("build_graph_kwargs") or {}
    step(
        "作业把 payload 的 kb/doc 作用域透传给 build_graph（调用接线正确）",
        kwargs.get("kb_id") == KB and list(kwargs.get("doc_ids") or []) == [DOC],
        f"kwargs={kwargs}",
    )


def section_clean(h: Harness) -> None:
    print("[C] clean 对照组：无 vector_failures 的 build_graph 仍判成功（修复没误伤）")
    code, out = h.run("clean", "clean_build_graph_succeeds")
    step("driver 子进程 exit 0", code == 0, f"exit={code} " + out[-300:])
    r = obj_marker(out, "__CLEAN__")
    step("干净作业判 succeeded", r.get("status") == "succeeded", f"clean={r}")
    step("结果 execution_status=completed", r.get("execution_status") == "completed", f"clean={r}")
    step("未触发重试（retry_count=0、无错误信息）", r.get("retry_count") == 0 and not r.get("error_message"), f"clean={r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td))
        guard_sqlite(h)
        section_relay(h)
        section_retry(h)
        section_clean(h)
    print("-" * 60)
    if FAILURES:
        print(f"BUILD_GRAPH_SHADOW_RETRY_SUMMARY failed={len(FAILURES)}")
        for name in FAILURES:
            print("  ✗ " + name)
        return 1
    print("BUILD_GRAPH_SHADOW_RETRY_SUMMARY passed result=pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
