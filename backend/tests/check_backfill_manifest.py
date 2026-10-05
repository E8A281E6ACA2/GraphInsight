#!/usr/bin/env python3
"""
M5-A 运行清单（backfill run manifest）验收测试（临时 SQLite 隔离 + UTF-8 子进程捕获）

覆盖（v3 backfill 路线图阶段 2：完整 manifest）：
T1  执行模式清单落盘：单 JSON 文档（json.load 直过）、schema/kb_id/mode/trace_id 齐备
T2  清单写入腿与 DB 实际一致：pg_rows_inserted / inserted_chunk_ids / projection_state_updated
T3  清单承载决策门与最终门（gate_decision / gate_final）与 outcome
T4  dry-run 清单：mode=dry-run、writes 全零、gate_final 为空（无最终门）
T5  幂等重跑：两轮各一份清单、trace_id 互不相同，第二轮 rows_new=0 且 rows_skipped_existing=1
T6  失败注入：写入腿抛异常也必须留下清单（exception + exit_code=1），供 disposable 对账
T7  未启用清单：三态 UNSET 且无环境变量 → 不落盘、不建目录、零副作用
T8  RUN_MANIFEST 行为：成功打印 path=<文件>，文件确实存在且文件名 = trace_id.json

隔离铁律（与 check_m5a_revision_backfill.py 同源）：子进程必须靠
GRAPHINSIGHT_BACKEND_ENV_FILE 指向"含 ADMIN_DATABASE_URL=sqlite 的临时 env 文件"，
否则 backend/.env 会覆盖注入值并回落共享开发 PG；驱动自身带 sqlite 方言守卫。
每个场景独立临时库 + 独立清单目录，互不污染。

运行：python backend/tests/check_backfill_manifest.py
（依赖 sqlalchemy/dotenv；无需 Neo4j/Milvus/PG）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).parent.parent
FAILURES: list = []

# 本套件打印中文断言名与子进程中文输出，Windows 控制台默认 cp936 会崩，必须自锁 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ZERO_WRITE_FIELDS = (
    "pg_rows_inserted",
    "pg_insert_conflicts_skipped",
    "neo4j_indexed",
    "neo4j_failed",
    "milvus_upserted",
    "milvus_failed",
    "milvus_skipped",
    "revision_field_absent",
    "projection_state_updated",
    "state_write_failed",
)


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env_file = tmp / "manifest_test.env"

    def use_db(self, name: str) -> None:
        url = f"sqlite:///{(self.tmp / name).as_posix()}"
        self.env_file.write_text(f"ADMIN_DATABASE_URL={url}\n", encoding="utf-8")

    def run(self, script: str, args: list = None, extra_env: dict = None) -> tuple:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self.env_file)
        env["PYTHONPATH"] = str(backend_dir)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, str(backend_dir / script)] + (args or []),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=env,
            timeout=180,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def run_python_code(self, code: str) -> tuple:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self.env_file)
        env["PYTHONPATH"] = str(backend_dir)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=env,
            timeout=180,
        )
        return proc.returncode, proc.stdout + proc.stderr


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def parse_marker(out: str, marker: str, json_value: bool = False) -> list:
    """取所有 marker 行；json_value=True 时解析为 JSON 对象列表（raw_decode，防多行 JSON）。"""
    values = []
    for line in out.splitlines():
        if not line.startswith(marker):
            continue
        payload = line[len(marker):]
        if json_value:
            try:
                obj, _ = json.JSONDecoder().raw_decode(payload)
                values.append(obj)
            except Exception:  # noqa: BLE001
                values.append({"__parse_error__": True})
        else:
            values.append(payload)
    return values


def prep_db(h: Harness, name: str) -> None:
    """独立 SQLite 库 + 三件套建表（chunk_revisions / admin_jobs / targets_hash）。"""
    h.use_db(name)
    code, out = h.run(str(Path("admin") / "migrate_chunk_revisions.py"), ["--action", "migrate"])
    step(f"{name}：迁移 chunk_revisions", code == 0, f"exit={code} " + out[-300:])
    code, out = h.run_python_code(
        "from admin.database import Base, engine;"
        "from admin.models import AdminJob;"
        "Base.metadata.create_all(bind=engine, tables=[AdminJob.__table__]);"
        "engine.dispose(); print('bootstrap ok')"
    )
    step(f"{name}：引导 admin_jobs", code == 0 and "bootstrap ok" in out, f"exit={code} " + out[-300:])
    code, out = h.run(str(Path("admin") / "migrate_jobs_targets_hash.py"), ["--action", "migrate"])
    step(f"{name}：迁移 targets_hash", code == 0, f"exit={code} " + out[-300:])


def manifest_files(mdir: Path) -> list:
    return sorted(mdir.glob("*.json")) if mdir.exists() else []


def run_scenario(h: Harness, scenario: str, mdir: Path) -> tuple:
    return h.run(
        str(Path("tests") / "m5a_backfill_driver.py"),
        ["--scenario", scenario],
        extra_env={"MANIFEST_TMP": str(mdir)},
    )


def main() -> int:
    driver = str(Path("tests") / "m5a_backfill_driver.py")
    with tempfile.TemporaryDirectory(prefix="m5a-manifest-") as raw:
        tmp = Path(raw)
        h = Harness(tmp)

        print("[T1/T2/T3] 执行模式清单")
        mdir = tmp / "mf_exec"
        prep_db(h, "mf_exec.db")
        code, out = run_scenario(h, "manifest_execute", mdir)
        files = manifest_files(mdir)
        mfs = parse_marker(out, "__MANIFEST__", json_value=True)
        mf = mfs[0] if mfs else {}
        step("执行退出 0", code == 0, f"exit={code} " + out[-400:])
        step("恰好一份清单文件", len(files) == 1, f"files={len(files)}")
        step("清单是单 JSON 文档且 json.load 可解析（证据合同）", len(mfs) == 1 and not mf.get("__parse_error__"), str(mf)[:200])
        step("schema/kb_id/mode/trace_id 齐备",
             mf.get("schema") == "m5a-backfill-manifest-v1" and mf.get("kb_id") == "kb-a"
             and mf.get("mode") == "execute" and str(mf.get("trace_id", "")).startswith("backfill-kb-a-"))
        caps = mf.get("capabilities") or {}
        step("能力快照落盘（graph 开、vector 关）", caps.get("graph_enabled") is True and caps.get("vector_enabled") is False, str(caps))
        w = mf.get("writes") or {}
        step("写入腿计数：PG 插 1 行、inserted_chunk_ids=[m1]",
             w.get("pg_rows_inserted") == 1 and w.get("inserted_chunk_ids") == ["m1"], str(w))
        step("投影状态回写 1 行且无 CAS 失败",
             w.get("projection_state_updated") == 1 and w.get("state_write_failed") == 0
             and w.get("state_write_failed_ids") == [], str(w))
        gf = mf.get("gate_final") or {}
        step("最终门与 outcome 落盘（CLOSED + DEGRADED vector skipped）",
             gf.get("closed") is True and gf.get("degraded_vector") == 1 and mf.get("outcome") == "completed", str(gf))
        step("决策门同步落盘", isinstance(mf.get("gate_decision"), dict) and mf.get("gate_decision"), str(mf.get("gate_decision")))
        step("时间/退出码/异常/落盘状态齐备",
             mf.get("finished_at") is not None and mf.get("duration_ms") is not None
             and mf.get("exit_code") == 0 and mf.get("exception") is None
             and mf.get("manifest_write_ok") is True, str({k: mf.get(k) for k in ("finished_at", "duration_ms", "exit_code", "exception", "manifest_write_ok")}))
        step("[T8] RUN_MANIFEST 打印的 path 确实存在且文件名 = trace_id.json",
             len(files) == 1 and files[0].name == f"{mf.get('trace_id')}.json"
             and all(Path(p).exists() for p in parse_marker(out, "RUN_MANIFEST path=")), out[-300:])

        print("[T4] dry-run 清单")
        mdir = tmp / "mf_dry"
        prep_db(h, "mf_dry.db")
        code, out = run_scenario(h, "manifest_dryrun", mdir)
        files = manifest_files(mdir)
        mfs = parse_marker(out, "__MANIFEST__", json_value=True)
        mf = mfs[0] if mfs else {}
        w = mf.get("writes") or {}
        step("dry-run 退出 0 且一份清单", code == 0 and len(files) == 1, f"exit={code} files={len(files)}")
        step("mode=dry-run 且 writes 全零（含空 id 列表）",
             mf.get("mode") == "dry-run" and all(w.get(k) == 0 for k in ZERO_WRITE_FIELDS)
             and w.get("inserted_chunk_ids") == [] and w.get("state_write_failed_ids") == [], str(w))
        step("outcome=dry_run_completed 且无最终门（gate_final 为空）",
             mf.get("outcome") == "dry_run_completed" and mf.get("gate_final") is None, str(mf.get("outcome")))

        print("[T5] 幂等重跑")
        mdir = tmp / "mf_rerun"
        prep_db(h, "mf_rerun.db")
        code, out = run_scenario(h, "manifest_rerun", mdir)
        files = manifest_files(mdir)
        mfs = parse_marker(out, "__MANIFEST__", json_value=True)
        step("两轮各一份清单（历史不覆盖不追加）", len(files) == 2 and len(mfs) == 2, f"files={len(files)} mfs={len(mfs)}")
        if len(mfs) == 2:
            # 文件名含随机后缀，字典序 ≠ 执行顺序；判据只能取清单自带的 started_at。
            first, second = sorted(mfs, key=lambda m: str(m.get("started_at") or ""))
            step("两轮 trace_id 互不相同",
                 first.get("trace_id") != second.get("trace_id")
                 and bool(first.get("trace_id")) and bool(second.get("trace_id")),
                 f"{first.get('trace_id')} vs {second.get('trace_id')}")
            step("首轮新增 1 行、第二轮零新增（幂等）",
                 (first.get("writes") or {}).get("pg_rows_inserted") == 1
                 and (second.get("writes") or {}).get("pg_rows_inserted") == 0,
                 f"first={first.get('writes')} second={second.get('writes')}")
            step("第二轮 inventory 标记已有行（rows_skipped_existing=1、new_chunks=0）",
                 (second.get("inventory") or {}).get("rows_skipped_existing") == 1
                 and (second.get("inventory") or {}).get("new_chunks") == 0, str(second.get("inventory")))
        step("重跑两轮均 completed", code == 0 and all(m.get("outcome") == "completed" for m in mfs), f"exit={code}")

        print("[T6] 失败注入（disposable 对账基础）")
        mdir = tmp / "mf_exc"
        prep_db(h, "mf_exc.db")
        code, out = run_scenario(h, "manifest_exception", mdir)
        files = manifest_files(mdir)
        mfs = parse_marker(out, "__MANIFEST__", json_value=True)
        mf = mfs[0] if mfs else {}
        exc_name = (parse_marker(out, "__EXCEPTION__") or [""])[0]
        step("注入的 RuntimeError 被原样上抛（未被吞）", exc_name == "RuntimeError", exc_name)
        step("异常轮仍留下清单（失败可定位，不静默吸收）", len(files) == 1 and len(mfs) == 1, f"files={len(files)}")
        step("清单记录 exception 文本与 exit_code=1、outcome=exception",
             "injected failure" in str(mf.get("exception")) and mf.get("exit_code") == 1
             and mf.get("outcome") == "exception", str(mf.get("exception")))
        step("异常轮清单仍是合法 JSON（证据合同）", bool(mfs) and not mf.get("__parse_error__"))

        print("[T7] 未启用清单：零副作用")
        mdir = tmp / "mf_off"
        prep_db(h, "mf_off.db")
        code, out = h.run(
            driver,
            ["--scenario", "manifest_disabled"],
            extra_env={"MANIFEST_TMP": str(mdir)},
        )
        step("未启用时目录不被创建", not mdir.exists(), str(mdir))
        step("无 RUN_MANIFEST 输出", "RUN_MANIFEST" not in out, out[-300:])
        step("清单计数为 0 且场景正常退出", (parse_marker(out, "__MANIFEST_COUNT__") or ["-1"])[0] == "0" and code == 0, f"exit={code}")

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} failed")
        for name in FAILURES:
            print("  FAILED: " + name)
        return 1
    print("✓ all backfill run-manifest checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
