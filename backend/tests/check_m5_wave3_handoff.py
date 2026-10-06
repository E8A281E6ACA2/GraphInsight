#!/usr/bin/env python3
"""
M5 Wave 3 连续场景验收（临时 SQLite 隔离 + UTF-8 子进程捕获）

Wave 3 四条接线必须在**同一条调用链**上证成，单独一个单元测试各自绿不能说明接线成立：
1. continuity：真实 `build_graph` 的影子（v3）写失败 → 逐 chunk `vector_status='failed'`
   → 按 §16.3 自动转交 reindex_chunks（targets_hash 64 hex）→ 真实 `job_service.create_job`
   同 hash **复用**（不新增行、非法 payload 一律 REINDEX_SCOPE_REQUIRED）→ 真实 `run_job`
   消费同一份 handoff payload → 只重放未收敛的 vector 腿 → 双侧 indexed + §6.2 文档聚合
   → 作业 succeeded；
2. terminal_exhausted：重试额度用尽的终态失败 → 父子回写只降级未收敛的腿（已 indexed 的
   chunk 绝不回退）+ 审计 `kb_chunk_reindex_failed` + 再次提交被拒 3004（`retry_exhausted`）
   且不新增行；
2b. terminal_crash：worker 在投影状态回写之前抛异常（超时/崩溃形态，投影列还停在 pending）
   → 终态父子回写把未收敛的**两腿**都兜成 failed，非目标 chunk 一字不动；
3. retry_not_terminal：额度未用尽时只排退避重试，**不**做终态父子回写（该判据靠"重试路径
   零条 kb_chunk_reindex_failed、但 job_failed/job_retry_scheduled 有记录"证成——否则审计
   表缺表时 `_write_job_log` 会静默吞掉，0 条断言就变成假绿）；
4. index_unavailable：§8.5 collection 缺显式 content_revision 字段 → 一条向量都不写、
   vector 保持 pending（不是 failed），终态回写整列不动 vector 侧。

真实代码路径：作业状态机（run_job 的领取/失败/退避/终态分支）、§16.3 去重入队与冲突回读、
CAS 投影回写、§6.2 文档聚合、审计写入、reindex worker 的复核与失败语义。
替换的只有外部依赖：Neo4j 会话、文档注册表与解析路径、embedding、Milvus client、
worker 的两个索引写函数与能力判定。本轮禁止触碰共享 dev 活栈，也不开启 dual_write。

隔离与 UTF-8 铁律同 check_b0_reindex_chunks.py：
GRAPHINSIGHT_BACKEND_ENV_FILE 指向含 ADMIN_DATABASE_URL=sqlite 的临时 env 文件；
每场景独立库；子进程传 PYTHONUTF8/PYTHONIOENCODING 并断言真实 returncode；不使用 -X utf8。

运行：python backend/tests/check_m5_wave3_handoff.py
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
DRIVER = str(Path("tests") / "m5_wave3_handoff_driver.py")
KB = "kb-w3"
DOC = "doc-w3"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _utf8_env(base: dict) -> dict:
    env = base.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Harness:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.env_file = tmp / "w3_test.env"

    def use_db(self, name: str) -> str:
        url = f"sqlite:///{(self.tmp / name).as_posix()}"
        self.env_file.write_text(f"ADMIN_DATABASE_URL={url}\n", encoding="utf-8")
        return url

    def _base_env(self) -> dict:
        env = _utf8_env(os.environ)
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(self.env_file)
        env["PYTHONPATH"] = str(backend_dir)
        return env

    def _spawn(self, cmd: list) -> tuple:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.tmp),
            env=self._base_env(),
            timeout=600,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def run(self, script: str, args: list = None) -> tuple:
        return self._spawn([sys.executable, str(backend_dir / script)] + (args or []))

    def run_python_code(self, code: str) -> tuple:
        return self._spawn([sys.executable, "-c", code])

    def guard_sqlite(self) -> None:
        code, out = self.run_python_code(
            "from admin.database import engine; print('DIALECT', engine.dialect.name)"
        )
        step("引擎隔离守卫（必须 sqlite）", code == 0 and "DIALECT sqlite" in out, f"exit={code} " + out[-300:])


def step(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def parse_marker(out: str, marker: str, default=None):
    for line in out.splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):])
            except json.JSONDecodeError:
                return default
    return default


def obj_marker(out: str, marker: str) -> dict:
    """标记缺失/形状不对必须显式判红，不能让 `== {}` 类断言假绿。"""
    value = parse_marker(out, marker, None)
    if not isinstance(value, dict):
        step(f"标记 {marker} 缺失或非对象", False, f"got={str(value)[:200]}")
        return {}
    return value


def bootstrap_tables(h: Harness) -> tuple:
    """建齐 Wave 3 用到的四张表——admin_logs 缺表时 `_write_job_log` 会静默回滚，
    "零条审计"类断言就会假绿，所以这里必须显式建出来。"""
    return h.run_python_code(
        "from admin.database import Base, engine;"
        "from admin.models import AdminJob, AdminLog, KnowledgeBase, KnowledgeBaseDocument;"
        "Base.metadata.create_all(bind=engine, tables=["
        "KnowledgeBase.__table__, KnowledgeBaseDocument.__table__, AdminJob.__table__, AdminLog.__table__]);"
        "engine.dispose(); print('bootstrap ok')"
    )


def prep_db(h: Harness, name: str) -> None:
    """独立 SQLite 库 + 三件套（chunk_revisions / ORM 表含 admin_logs / targets_hash 索引）。

    `ON CONFLICT (job_type, kb_id, targets_hash) WHERE targets_hash IS NOT NULL` 在 SQLite
    下依赖 migrate_jobs_targets_hash 建出的部分唯一索引存在，缺索引则该迁移脚本必须先跑。
    """
    h.use_db(name)
    code, out = h.run(str(Path("admin") / "migrate_chunk_revisions.py"), ["--action", "migrate"])
    step(f"{name}：迁移 chunk_revisions", code == 0, f"exit={code} " + out[-300:])
    code, out = bootstrap_tables(h)
    step(f"{name}：建齐 ORM 表（含 admin_logs）", code == 0 and "bootstrap ok" in out, f"exit={code} " + out[-300:])
    code, out = h.run(str(Path("admin") / "migrate_jobs_targets_hash.py"), ["--action", "migrate"])
    step(f"{name}：迁移 targets_hash", code == 0, f"exit={code} " + out[-300:])


def scenario(h: Harness, db: str, name: str) -> str:
    prep_db(h, db)
    code, out = h.run(DRIVER, ["--scenario", name])
    step(f"{name}：driver 子进程 exit 0（UTF-8 捕获，无 -X utf8）", code == 0, f"exit={code}\n{out[-2500:]}")
    return out


def states(values: dict) -> set:
    return set(values.values())


# ---------------------------------------------------------------------------
# 1. continuity：影子失败 → 转交 → 复用 → worker 消费 → 闭环
# ---------------------------------------------------------------------------


def section_continuity(h: Harness) -> None:
    print("[1] continuity：build_graph 影子写失败 → §16.3 转交 → 同 hash 复用 → worker 闭环")
    out = scenario(h, "continuity.db", "continuity")
    handoff = obj_marker(out, "__HANDOFF__")
    submit = obj_marker(out, "__SUBMIT__")
    closed = obj_marker(out, "__CLOSED__")

    failed_ids = handoff.get("failed_ids") or []
    step("影子写失败被逐 chunk 记入 vector_failed_chunks", bool(failed_ids), f"failed_ids={failed_ids}")
    step(
        "§16.1 S1 影子脏写被识别（error_class=DualWriteShadowError / is_shadow）",
        handoff.get("is_shadow") is True and handoff.get("detail_count", 0) >= 1,
        f"is_shadow={handoff.get('is_shadow')} details={handoff.get('detail_count')}",
    )
    step(
        "未收敛 chunk 逐条落 vector_status=failed 且版本清空（不虚报 indexed）",
        failed_ids and states(handoff.get("rev_vector_state") or {}) == {"failed"}
        and all(v is None for v in (handoff.get("rev_vector_rev") or {}).values()),
        f"vector={handoff.get('rev_vector_state')} rev={handoff.get('rev_vector_rev')}",
    )
    step(
        "已收敛的 graph 腿不被 vector 失败拖回 pending",
        states(handoff.get("rev_graph_state") or {}) == {"indexed"},
        f"graph={handoff.get('rev_graph_state')}",
    )
    step(
        "§6.2 文档聚合：graph=indexed / vector=failed（两侧独立聚合）",
        (handoff.get("doc_state") or {}).get("graph") == "indexed"
        and (handoff.get("doc_state") or {}).get("vector") == "failed",
        f"doc={handoff.get('doc_state')}",
    )

    jobs = (handoff.get("handoff") or {}).get("jobs") or []
    step(
        "转交报表 created=1 / outcome=created / 来源 build_graph_m5_wave3",
        (handoff.get("handoff") or {}).get("created") == 1
        and bool(jobs)
        and jobs[0].get("outcome") == "created"
        and handoff.get("payload_source") == "build_graph_m5_wave3",
        f"handoff={handoff.get('handoff')}",
    )
    step("只建一条 reindex_chunks 作业（同文档合成一个 job，无隐式扩范围）", handoff.get("job_count") == 1, f"job_count={handoff.get('job_count')}")
    step(
        "targets_hash 是 64 位十六进制且等于 payload 的 canonical 复算值",
        handoff.get("hash_shape_ok") is True and handoff.get("hash_matches_payload") is True,
        f"hash={handoff.get('targets_hash')} ok={handoff.get('hash_shape_ok')}",
    )
    step(
        "转交 payload 的 targets 与失败清单逐条一致",
        bool(failed_ids) and handoff.get("payload_target_ids") == sorted(failed_ids)
        and handoff.get("payload_revisions") == [1],
        f"payload_ids={handoff.get('payload_target_ids')} failed={sorted(failed_ids)} revs={handoff.get('payload_revisions')}",
    )
    step("转交无未跟踪 chunk（每条失败都带权威 revision）", handoff.get("untracked") == [], f"untracked={handoff.get('untracked')}")

    targets_hash = handoff.get("targets_hash")
    step(
        "同 hash 提交复用既有 job：返回同一 job、库内仍只有一行",
        submit.get("item_targets_hash") == targets_hash
        and submit.get("item_status") == "pending"
        and submit.get("row_count_after") == 1,
        f"submit={ {k: submit.get(k) for k in ('item_id', 'item_status', 'item_targets_hash', 'row_count_after')} }",
    )
    step(
        "任务中心 list/get 回读到同一 targets_hash",
        submit.get("list_total") == 1 and submit.get("list_hashes") == [targets_hash] and submit.get("get_hash") == targets_hash,
        f"list_total={submit.get('list_total')} hashes={submit.get('list_hashes')}",
    )
    step("复用被审计（job_reused 落 admin_logs，证明审计面可写）", submit.get("job_reused_count", 0) >= 1, f"job_reused={submit.get('job_reused_count')}")
    errors = submit.get("errors") or {}
    step(
        "空/缺 chunk_id/非法 revision 三类 payload 一律 REINDEX_SCOPE_REQUIRED（不是 500）",
        set(errors) == {"empty", "no_chunk_id", "bad_revision"}
        and all(v == "ValidationException:REINDEX_SCOPE_REQUIRED" for v in errors.values()),
        f"errors={errors}",
    )

    step("worker 消费转交 payload 后作业 succeeded", closed.get("job_status") == "succeeded", f"status={closed.get('job_status')}")
    step(
        "vector 腿收敛为 indexed 且版本对齐 target_revision",
        states(closed.get("rev_vector") or {}) == {"indexed"}
        and set((closed.get("rev_vector_rev") or {}).values()) == {1},
        f"vector={closed.get('rev_vector')} rev={closed.get('rev_vector_rev')}",
    )
    step(
        "只重放未收敛的那条腿（graph 零重放，§6.1 worker 重试 no-op）",
        closed.get("mil_written") == sorted(failed_ids) and closed.get("neo_written") == [],
        f"mil={closed.get('mil_written')} neo={closed.get('neo_written')} failed={sorted(failed_ids)}",
    )
    step(
        "§6.2 文档态收口为双侧 indexed",
        (closed.get("doc_state") or {}).get("graph") == "indexed"
        and (closed.get("doc_state") or {}).get("vector") == "indexed",
        f"doc={closed.get('doc_state')}",
    )
    step("闭环未扩范围：reindex_chunks 作业总数仍为 1", closed.get("job_count_total") == 1, f"total={closed.get('job_count_total')}")


# ---------------------------------------------------------------------------
# 2. terminal_exhausted：额度用尽的终态父子回写 + 提交被拒
# ---------------------------------------------------------------------------


def section_terminal(h: Harness) -> None:
    print("[2] terminal_exhausted：重试额度用尽 → 父子回写只降级未收敛腿 + 审计 + 提交被拒 3004")
    out = scenario(h, "terminal.db", "terminal_exhausted")
    t = obj_marker(out, "__TERMINAL__")

    step(
        "作业终态 failed 且不再排自动重试（retry_count == max_retries）",
        t.get("job_status") == "failed" and t.get("retry_count") == 2 and t.get("max_retries") == 2
        and t.get("retry_planned") is False,
        f"status={t.get('job_status')} retry={t.get('retry_count')}/{t.get('max_retries')} planned={t.get('retry_planned')}",
    )
    c_idx = t.get("c_idx") or {}
    step(
        "已双侧 indexed 的 chunk 不被终态回写降级（保守到侧）",
        c_idx.get("graph") == "indexed" and c_idx.get("graph_rev") == 1
        and c_idx.get("vector") == "indexed" and c_idx.get("vector_rev") == 1,
        f"c_idx={c_idx}",
    )
    c_bad = t.get("c_bad") or {}
    step(
        "未收敛的 vector 腿落 failed 且版本清空；本轮已收敛的 graph 腿留在 indexed",
        c_bad.get("vector") == "failed" and c_bad.get("vector_rev") is None
        and c_bad.get("graph") == "indexed" and c_bad.get("graph_rev") == 1,
        f"c_bad={c_bad}",
    )
    step("worker 只重建目标 chunk（无隐式扩范围）", t.get("worker_vector_calls") == ["c-bad"], f"calls={t.get('worker_vector_calls')}")
    step(
        "§6.2 文档聚合：有一条腿 failed 就是 failed，另一条腿如实 indexed",
        (t.get("doc_state") or {}).get("graph") == "indexed" and (t.get("doc_state") or {}).get("vector") == "failed",
        f"doc={t.get('doc_state')}",
    )
    details = t.get("audit_details") or {}
    step(
        "父子回写审计 kb_chunk_reindex_failed（targets/updated/current_moved 齐）",
        t.get("audit_count", 0) >= 1 and details.get("targets") == 1
        and details.get("updated", 0) >= 1 and details.get("current_moved") == 0,
        f"count={t.get('audit_count')} details={details}",
    )
    step("写失败不是 §8.5 缺字段：keep_vector_untouched=False", details.get("keep_vector_untouched") is False, f"kv={details.get('keep_vector_untouched')}")
    step("审计面活着（同库有 job_failed 日志）", t.get("job_failed_count", 0) >= 1, f"job_failed={t.get('job_failed_count')}")

    rejected = t.get("rejected") or {}
    r_details = rejected.get("details") or {}
    step(
        "额度用尽后再提交被拒：BusinessException + 3004 OPERATION_NOT_ALLOWED",
        rejected.get("raised") == "BusinessException" and rejected.get("error_code") == "3004",
        f"rejected={ {k: rejected.get(k) for k in ('raised', 'error_code')} }",
    )
    step(
        "拒绝带结构化 details（reason=retry_exhausted + 额度 + targets_hash）",
        r_details.get("reason") == "retry_exhausted" and r_details.get("retry_count") == 2
        and r_details.get("max_retries") == 2 and bool(r_details.get("targets_hash")),
        f"details={r_details}",
    )
    step("拒绝不新增 job 行（§16.3 禁止无限重试）", t.get("job_count_total") == 1, f"total={t.get('job_count_total')}")
    step(
        "拒绝同样落审计 kb_chunk_reindex_failed",
        t.get("audit_count_after_reject") == t.get("audit_count", 0) + 1,
        f"before={t.get('audit_count')} after={t.get('audit_count_after_reject')}",
    )


# ---------------------------------------------------------------------------
# 2b. terminal_crash：worker 没机会写状态就炸 → 终态回写兜住未收敛的两腿
# ---------------------------------------------------------------------------


def section_terminal_crash(h: Harness) -> None:
    print("[2b] terminal_crash：worker 在状态回写前抛异常 → 两腿兜成 failed（终态回写的本职）")
    out = scenario(h, "crash.db", "terminal_crash")
    c = obj_marker(out, "__CRASH__")

    step(
        "作业终态 failed（RuntimeError 可重放，但额度已用尽）",
        c.get("job_status") == "failed" and c.get("retry_count") == 2 and "RuntimeError" in str(c.get("error_message")),
        f"status={c.get('job_status')} msg={str(c.get('error_message'))[:160]}",
    )
    step(
        "崩溃点确在状态回写之前（graph 腿被调用、vector 腿一次没跑）",
        c.get("graph_calls") == ["c-bad"] and c.get("vector_calls") == [],
        f"graph={c.get('graph_calls')} vector={c.get('vector_calls')}",
    )
    c_bad = c.get("c_bad") or {}
    step(
        "worker 没写成的两腿由终态回写兜成 failed 且版本清空",
        c_bad.get("graph") == "failed" and c_bad.get("graph_rev") is None
        and c_bad.get("vector") == "failed" and c_bad.get("vector_rev") is None,
        f"c_bad={c_bad}",
    )
    c_idx = c.get("c_idx") or {}
    step(
        "非目标 chunk 一字不动（回写只覆盖 payload.targets）",
        c_idx.get("graph") == "indexed" and c_idx.get("graph_rev") == 1
        and c_idx.get("vector") == "indexed" and c_idx.get("vector_rev") == 1,
        f"c_idx={c_idx}",
    )
    step(
        "§6.2 文档聚合双侧 failed",
        (c.get("doc_state") or {}).get("graph") == "failed" and (c.get("doc_state") or {}).get("vector") == "failed",
        f"doc={c.get('doc_state')}",
    )
    details = c.get("audit_details") or {}
    step(
        "兜底审计 kb_chunk_reindex_failed（targets=1/updated>=1/current_moved=0）",
        c.get("audit_count", 0) >= 1 and details.get("targets") == 1 and details.get("updated", 0) >= 1
        and details.get("current_moved") == 0 and details.get("keep_vector_untouched") is False,
        f"count={c.get('audit_count')} details={details}",
    )
    step("审计面活着（同库有 job_failed 日志）", c.get("job_failed_count", 0) >= 1, f"job_failed={c.get('job_failed_count')}")


# ---------------------------------------------------------------------------
# 3. retry_not_terminal：额度未用尽 → 只排重试，不做终态父子回写
# ---------------------------------------------------------------------------


def section_retry(h: Harness) -> None:
    print("[3] retry_not_terminal：额度未用尽 → 只排退避重试，终态父子回写不触发")
    out = scenario(h, "retry.db", "retry_not_terminal")
    r = obj_marker(out, "__RETRY_ONLY__")

    step(
        "失败后按 §15.5 排退避重试（retry_count+1、error_message 标注已计划）",
        r.get("job_status") == "failed" and r.get("retry_count") == 1 and r.get("retry_planned") is True,
        f"status={r.get('job_status')} retry={r.get('retry_count')} planned={r.get('retry_planned')}",
    )
    sched = r.get("sched") or []
    step(
        "_schedule_retry 被调用一次且 attempt/delay 正确",
        len(sched) == 1 and sched[0].get("attempt") == 1 and int(sched[0].get("delay") or 0) >= 1,
        f"sched={sched}",
    )
    step(
        "重试不是终态：零条 kb_chunk_reindex_failed",
        r.get("audit_count") == 0,
        f"audit_count={r.get('audit_count')}",
    )
    step(
        "同一库审计面确实在写（job_failed/job_retry_scheduled 有记录，排除 0 条假绿）",
        r.get("job_failed_count", 0) >= 1 and r.get("retry_scheduled_log", 0) >= 1,
        f"job_failed={r.get('job_failed_count')} retry_log={r.get('retry_scheduled_log')}",
    )
    c_bad = r.get("c_bad") or {}
    step(
        "worker 本轮仍如实落 failed（可重试态，非虚标）",
        c_bad.get("vector") == "failed" and c_bad.get("vector_rev") is None,
        f"c_bad={c_bad}",
    )


# ---------------------------------------------------------------------------
# 4. index_unavailable：§8.5 缺显式字段 → 拒写、vector 保持 pending
# ---------------------------------------------------------------------------


def section_blocked(h: Harness) -> None:
    print("[4] index_unavailable：§8.5 collection 缺显式 content_revision 字段 → 拒写 + 不谎报 failed")
    out = scenario(h, "blocked.db", "index_unavailable")
    b = obj_marker(out, "__BLOCKED__")

    step(
        "缺显式字段时一条向量都没写（拒写而非降级写 dynamic metadata）",
        b.get("vector_calls") == [],
        f"vector_calls={b.get('vector_calls')}",
    )
    step(
        "以 ValidationException(INDEX_UNAVAILABLE) 收口，不排自动重试",
        b.get("job_status") == "failed" and "ValidationException" in str(b.get("error_message"))
        and b.get("retry_count") == 0 and b.get("retry_scheduled_count") == 0,
        f"status={b.get('job_status')} msg={str(b.get('error_message'))[:160]}",
    )
    c_1 = b.get("c_1") or {}
    step(
        "vector 保持 pending 且版本为空（不是 failed，投影未收敛等 v3 迁移）",
        c_1.get("vector") == "pending" and c_1.get("vector_rev") is None,
        f"c_1={c_1}",
    )
    step(
        "graph 腿正常收敛 indexed@1（两侧独立）",
        c_1.get("graph") == "indexed" and c_1.get("graph_rev") == 1,
        f"c_1={c_1}",
    )
    step(
        "§6.2 文档态 graph=indexed / vector=pending",
        (b.get("doc_state") or {}).get("graph") == "indexed" and (b.get("doc_state") or {}).get("vector") == "pending",
        f"doc={b.get('doc_state')}",
    )
    details = b.get("audit_details") or {}
    step(
        "终态父子回写整列不动 vector 侧（keep_vector_untouched=True）",
        b.get("audit_count", 0) >= 1 and details.get("keep_vector_untouched") is True,
        f"count={b.get('audit_count')} kv={details.get('keep_vector_untouched')}",
    )
    step("审计面活着（job_failed 有记录）", b.get("job_failed_count", 0) >= 1, f"job_failed={b.get('job_failed_count')}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        h = Harness(tmp)
        print("=== M5 Wave 3 连续场景（临时 SQLite 隔离）===")
        h.use_db("guard.db")
        h.guard_sqlite()
        section_continuity(h)
        section_terminal(h)
        section_terminal_crash(h)
        section_retry(h)
        section_blocked(h)

    print()
    if FAILURES:
        print(f"RESULT: FAILED ({len(FAILURES)} 项)")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("RESULT: PASS — Wave 3 连续场景（影子失败转交 / 复用 / 终态父子回写 / §8.5 拒写）全部证成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
