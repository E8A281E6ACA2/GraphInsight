#!/usr/bin/env python3
"""Isolated Gate 0 coverage contract tests for the M5 C3 inventory CLI.

Fully isolated: SQLite in-memory registry, fake pymilvus/neo4j modules, and a
stubbed build_inventory. Nothing here touches shared dev Milvus/Neo4j/PG, and
no report produced by this test may be cited as shared-production evidence.

Positive / negative matrix (task contract + audit revision 2):
1. 正常全覆盖       — FULL verdict, writes=0, exit 0
2. 状态排除         — archived/deleting/soft_deleted omitted with reason;
                      non-active KBs must never be probed
3. 缺 scope         — unregistered --kb id omitted with UNREGISTERED_KB;
                      --kb runs are SCOPE_SLICE and capped at PARTIAL
4. 单 KB 读取异常    — one failed probe forces FAILED + exit 2 (fail-closed)
5. 多 collection    — multi-collection instance classified read-only;
                      §8.5 v3 target existing fails the run closed
6. before/after 漂移 — counter drift -> writes != 0 -> FAILED + exit 2
7. 注册表不可读      — KB registry failure degrades auditable, never
                      masquerades as "zero KBs registered"
8. PG chunk_revisions 读取失败（前后值等价 0）— value/ok/reason 显式记录，
   绝不折算 0；writes=UNVERIFIED_EVIDENCE、FAILED、exit 2
9. admin_jobs 读取失败（前后值等价 0）— 同上 fail-closed
10. Neo4j 计数失败（前后值等价 0）— 同上 fail-closed
11. Milvus stats 不可读（前后值等价 0）— 同上 fail-closed
12. active KB 带 scope_unresolved — 仍被探测并进入 C3_REPORT
13. environment_id 非法值 — 整体拒绝为 INVALID，绝不清洗拼接输出；FAILED + exit 2
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

import admin.report_m5_c3_inventory as report  # noqa: E402
from admin import backfill_chunk_revisions as backfill  # noqa: E402

ENV_ID = "admin_db:sqlite"
COMMIT_SHA = "testsha0"


# ---------------------------------------------------------------------------
# 隔离夹具：SQLite 注册表 + 假 pymilvus/neo4j + build_inventory 桩
# ---------------------------------------------------------------------------


def _make_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE knowledge_bases (id VARCHAR PRIMARY KEY, status VARCHAR)")
        )
        for kb_id, status in [
            ("kb-active-a", "active"),
            ("kb-active-b", "active"),
            ("kb-archived", "archived"),
            ("kb-deleting", "deleting"),
            ("kb-softdel", "soft_deleted"),
        ]:
            conn.execute(
                text("INSERT INTO knowledge_bases (id, status) VALUES (:id, :status)"),
                {"id": kb_id, "status": status},
            )
        # 审计修订 2：证据源必须真实可读；夹具不再依赖"缺表折算 0"的旧行为，
        # chunk_revisions / admin_jobs 建表并灌入已知计数供 before/after 对账。
        conn.execute(text("CREATE TABLE chunk_revisions (id INTEGER PRIMARY KEY)"))
        for _ in range(3):
            conn.execute(text("INSERT INTO chunk_revisions (id) VALUES (NULL)"))
        conn.execute(text("CREATE TABLE admin_jobs (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO admin_jobs (id) VALUES (NULL)"))
    return engine


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query, **kwargs):
        return SimpleNamespace(single=lambda: {"n": 5})


class _FakeDriver:
    def __init__(self, *args, **kwargs):
        pass

    def session(self, database=None):
        return _FakeSession()

    def close(self):
        pass


class _FakeGraphDatabase:
    @staticmethod
    def driver(uri, auth=None, connection_timeout=None):
        return _FakeDriver()


class _FakeMilvusClient:
    """Fake Milvus client: list_collections / stats only. No mutations exist."""

    instance_collections: List[str] = [
        "graphinsight_chunks_v2",
        "graphinsight_chunks",
        "team_shared_collection",
    ]
    stats_payload: Any = {"row_count": 7}
    stats_error: bool = False

    def __init__(self, uri=None, token=None, db_name=None):
        self.uri = uri

    def list_collections(self):
        return list(_FakeMilvusClient.instance_collections)

    def has_collection(self, name):
        return name in _FakeMilvusClient.instance_collections

    def get_collection_stats(self, name):
        if _FakeMilvusClient.stats_error:
            raise RuntimeError("simulated stats failure")
        return dict(_FakeMilvusClient.stats_payload)


class _FailingGraphDatabase:
    """Fake neo4j driver whose count query always fails (audit revision 2)."""

    @staticmethod
    def driver(uri, auth=None, connection_timeout=None):
        class _BrokenDriver:
            def session(self, database=None):
                raise RuntimeError("simulated neo4j failure")

            def close(self):
                pass

        return _BrokenDriver()


def _install_fake_drivers(restore: Dict[str, Any]) -> None:
    restore["pymilvus_module"] = sys.modules.get("pymilvus")
    restore["neo4j_module"] = sys.modules.get("neo4j")
    milvus_mod = types.ModuleType("pymilvus")
    milvus_mod.MilvusClient = _FakeMilvusClient
    neo4j_mod = types.ModuleType("neo4j")
    neo4j_mod.GraphDatabase = _FakeGraphDatabase
    sys.modules["pymilvus"] = milvus_mod
    sys.modules["neo4j"] = neo4j_mod


def _stub_inventory(kb_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        kb_id=kb_id,
        new_chunks=[],
        needs_reindex_targets=[],
        converged=0,
        blocked=0,
        blocked_targets=[],
        orphan_revisions=[],
        rows_skipped_existing=0,
        scope_mismatches=[],
        kb_scope_missing=False,
        degraded_skipped={"graph": 0, "vector": 0},
        content_mismatch=0,
        unrecoverable=[],
        scope_unresolved=[],
        neo4j_count=0,
        milvus_count=0,
        parsed_count=0,
        overlap_count=0,
        revision_row_count=0,
        graph_enabled=False,
        vector_enabled=False,
        milvus_revision_field=False,
        milvus_collection="graphinsight_chunks_v2",
    )


class _Sandbox:
    def __init__(self, engine: Any, probes: List[str]):
        self.engine = engine
        self.probes = probes


def _patch_sandbox(fail_kb_ids: List[str]) -> Tuple[_Sandbox, Dict[str, Any]]:
    """Patch the report module into a fully isolated state; return restore plan."""
    engine = _make_engine()
    probes: List[str] = []
    fail_set = set(fail_kb_ids)

    def stub_build(kb_id: str):
        probes.append(kb_id)
        if kb_id in fail_set:
            raise RuntimeError("simulated read failure")
        return _stub_inventory(kb_id)

    restore: Dict[str, Any] = {
        "engine": report.engine,
        "take_counter_snapshot": report.take_counter_snapshot,
        "resolve_commit_sha": report._resolve_commit_sha,
        "build_inventory": backfill.build_inventory,
        "milvus_collection_name": backfill._milvus_collection_name,
        "milvus_raw_collection_name": report._milvus_raw_collection_name,
        "allow_non_pg": os.environ.get("C3_INVENTORY_ALLOW_NON_PG"),
        "environment_id": os.environ.get("GRAPHINSIGHT_ENVIRONMENT_ID"),
    }
    _install_fake_drivers(restore)
    report.engine = engine
    report._resolve_commit_sha = lambda: COMMIT_SHA
    backfill.build_inventory = stub_build
    # raw(legacy) != resolved(v2)：确定性演示 §11.1/D3 归一化分离（audit 修订 4）。
    backfill._milvus_collection_name = lambda: "graphinsight_chunks_v2"
    report._milvus_raw_collection_name = lambda: "graphinsight_chunks"
    os.environ["C3_INVENTORY_ALLOW_NON_PG"] = "1"
    # 隔离夹具声明显式环境标识，避免 SQLite 运行依赖派生标识。
    os.environ["GRAPHINSIGHT_ENVIRONMENT_ID"] = ENV_ID
    return _Sandbox(engine, probes), restore


def _restore(restore: Dict[str, Any]) -> None:
    report.engine = restore["engine"]
    report.take_counter_snapshot = restore["take_counter_snapshot"]
    report._resolve_commit_sha = restore["resolve_commit_sha"]
    backfill.build_inventory = restore["build_inventory"]
    backfill._milvus_collection_name = restore["milvus_collection_name"]
    report._milvus_raw_collection_name = restore["milvus_raw_collection_name"]
    if restore.get("pymilvus_module") is not None:
        sys.modules["pymilvus"] = restore["pymilvus_module"]
    else:
        sys.modules.pop("pymilvus", None)
    if restore.get("neo4j_module") is not None:
        sys.modules["neo4j"] = restore["neo4j_module"]
    else:
        sys.modules.pop("neo4j", None)
    if restore["allow_non_pg"] is None:
        os.environ.pop("C3_INVENTORY_ALLOW_NON_PG", None)
    else:
        os.environ["C3_INVENTORY_ALLOW_NON_PG"] = restore["allow_non_pg"]
    if restore.get("environment_id") is None:
        os.environ.pop("GRAPHINSIGHT_ENVIRONMENT_ID", None)
    else:
        os.environ["GRAPHINSIGHT_ENVIRONMENT_ID"] = restore["environment_id"]
    _FakeMilvusClient.stats_payload = {"row_count": 7}
    _FakeMilvusClient.stats_error = False


def _run_cli_capture(argv: List[str]) -> Tuple[int, str]:
    buffer = io.StringIO()
    original_argv = sys.argv
    sys.argv = ["report_m5_c3_inventory.py", *argv]
    try:
        with redirect_stdout(buffer):
            code = report.main()
    finally:
        sys.argv = original_argv
    return code, buffer.getvalue()


def _parse_kv_line(line: str, prefix: str) -> Dict[str, str]:
    assert line.startswith(prefix + " "), f"missing {prefix} line: {line!r}"
    pairs = re.findall(r"(\w+)=([^\s]+)", line[len(prefix) + 1:])
    return dict(pairs)


def _parse_json_line(line: str, prefix: str) -> Dict[str, Any]:
    assert line.startswith(prefix + " "), f"missing {prefix} line: {line!r}"
    return json.loads(line[len(prefix) + 1:])


def _summary_of(stdout: str) -> Dict[str, Any]:
    for line in stdout.splitlines():
        if line.startswith("C3_SUMMARY "):
            return json.loads(line[len("C3_SUMMARY "):])
    raise AssertionError("C3_SUMMARY line missing from output")


# ---------------------------------------------------------------------------
# 场景 1：正常全覆盖 + 状态排除 + 多 collection 正腿 + 安全输出
# ---------------------------------------------------------------------------


def scenario_full_coverage_positive() -> None:
    box, restore = _patch_sandbox([])
    try:
        code, stdout = _run_cli_capture([])
        summary = _summary_of(stdout)
        cov = _parse_kv_line(
            next(l for l in stdout.splitlines() if l.startswith("GATE0_COVERAGE ")),
            "GATE0_COVERAGE",
        )
        assert code == 0, f"expected exit 0, got {code}"
        assert cov["scope"] == "FULL"
        assert cov["registered"] == "5"
        assert cov["active"] == "2"
        assert cov["eligible"] == "2"
        assert cov["probed"] == "2"
        assert cov["omitted"] == "3"
        assert cov["failed"] == "0"
        assert cov["coverage_verdict"] == "FULL"
        assert cov["writes"] == "0"
        assert cov["shared_production_gate"] == "OPEN"
        assert summary["coverage_verdict"] == "FULL"
        assert summary["writes"] == 0
        assert summary["timestamp"].endswith("Z")
        assert summary["commit_sha"] == COMMIT_SHA
        assert summary["environment_id"] == ENV_ID
        # 证据源计数：全部显式 value/ok/reason 且真实读出（audit 修订 1）。
        assert summary["degraded_reasons"] == []
        before = summary["counters_before"]
        after = summary["counters_after"]
        assert before["pg"]["chunk_revisions"] == {"value": 3, "ok": True, "reason": None}
        assert before["pg"]["knowledge_bases"] == {"value": 5, "ok": True, "reason": None}
        assert before["admin_jobs"]["rows"] == {"value": 1, "ok": True, "reason": None}
        assert before["neo4j"]["chunk_nodes"] == {"value": 5, "ok": True, "reason": None}
        assert before["milvus"]["entities"] == {"value": 7, "ok": True, "reason": None}
        assert after == before
        # 状态排除：三个非 active KB 全部带逐项原因，且从未被探测。
        reasons = {(o["kb_id"], o["reason"]) for o in summary["omitted_kbs"]}
        assert ("kb-archived", "STATUS_NOT_ELIGIBLE:archived") in reasons
        assert ("kb-deleting", "STATUS_NOT_ELIGIBLE:deleting") in reasons
        assert ("kb-softdel", "STATUS_NOT_ELIGIBLE:soft_deleted") in reasons
        assert sorted(box.probes) == ["kb-active-a", "kb-active-b"]
        for item in summary["omitted_kbs"]:
            assert set(item) == {"kb_id", "status", "reason"}
        # 多 collection 只读分类（正腿：无 v3；raw/resolved 分离 + current 存在）。
        collections = _parse_json_line(
            next(l for l in stdout.splitlines() if l.startswith("MILVUS_COLLECTIONS ")),
            "MILVUS_COLLECTIONS",
        )
        assert collections["configured_raw"] == "graphinsight_chunks"
        assert collections["resolved"] == "graphinsight_chunks_v2"
        assert collections["current"] == ["graphinsight_chunks_v2"]
        assert collections["current_missing"] is False
        assert collections["historical"] == ["graphinsight_chunks"]
        assert collections["unknown"] == ["team_shared_collection"]
        assert collections["target"] == []
        assert collections["read_only"] is True
        # 输出必须无敏感信息。
        for secret in ("postgres://", "bolt://", "19530", "password", "token"):
            assert secret not in stdout, f"leak detected: {secret}"
    finally:
        _restore(restore)


# ---------------------------------------------------------------------------
# 场景 2：--kb 局部探测 SCOPE_SLICE + unregistered omitted
# ---------------------------------------------------------------------------


def scenario_scope_slice_and_unregistered() -> None:
    box, restore = _patch_sandbox([])
    try:
        code, stdout = _run_cli_capture(["--kb", "kb-active-a", "--kb", "kb-ghost"])
        summary = _summary_of(stdout)
        assert code == 0, f"slice run should exit 0, got {code}"
        assert summary["scope"] == "SCOPE_SLICE"
        assert summary["probed_kb_total"] == 1
        assert summary["eligible_kb_total"] == 1
        assert summary["omitted_kb_total"] == 1
        # SCOPE_SLICE 即使探测齐全也必须封顶 PARTIAL，不得宣称 FULL。
        assert summary["coverage_verdict"] == "PARTIAL"
        omitted = summary["omitted_kbs"][0]
        assert omitted == {
            "kb_id": "kb-ghost",
            "status": "unregistered",
            "reason": "UNREGISTERED_KB",
        }
        assert box.probes == ["kb-active-a"]
    finally:
        _restore(restore)


# ---------------------------------------------------------------------------
# 场景 3：--kb 全部不可探测（非 active）→ FAILED
# ---------------------------------------------------------------------------


def scenario_slice_all_ineligible() -> None:
    _box, restore = _patch_sandbox([])
    try:
        code, stdout = _run_cli_capture(["--kb", "kb-archived"])
        summary = _summary_of(stdout)
        assert code == 2, f"zero-probe slice must fail closed, got {code}"
        assert summary["scope"] == "SCOPE_SLICE"
        assert summary["eligible_kb_total"] == 0
        assert summary["probed_kb_total"] == 0
        assert summary["coverage_verdict"] == "FAILED"
        assert summary["omitted_kbs"] == [
            {"kb_id": "kb-archived", "status": "archived", "reason": "STATUS_NOT_ELIGIBLE:archived"}
        ]
    finally:
        _restore(restore)


# ---------------------------------------------------------------------------
# 场景 4：单 KB 读取异常 → fail-closed FAILED
# ---------------------------------------------------------------------------


def scenario_single_kb_probe_failure() -> None:
    _box, restore = _patch_sandbox(["kb-active-b"])
    try:
        code, stdout = _run_cli_capture([])
        summary = _summary_of(stdout)
        assert code == 2, f"failed probe must exit 2, got {code}"
        assert summary["probed_kb_total"] == 1
        assert summary["eligible_kb_total"] == 2
        assert summary["failed_kb_total"] == 1
        # 单 KB 失败必须压过其它成功探测：不得宣称全量覆盖成功。
        assert summary["coverage_verdict"] == "FAILED"
        assert summary["coverage_fail_closed"] is True
        failed = summary["failed_kbs"][0]
        assert failed["kb_id"] == "kb-active-b"
        assert failed["status"] == "active"
        assert failed["reason"] == "KB_PROBE_FAILED: RuntimeError"
        assert "COVERAGE_FAIL_CLOSED" in stdout
    finally:
        _restore(restore)


# ---------------------------------------------------------------------------
# 场景 5：多 collection — §8.5 v3 target 已存在必须 fail-closed
# ---------------------------------------------------------------------------


def scenario_target_collection_exists() -> None:
    _box, restore = _patch_sandbox([])
    original_collections = list(_FakeMilvusClient.instance_collections)
    _FakeMilvusClient.instance_collections = [
        "graphinsight_chunks_v2",
        "graphinsight_chunks_v3",
    ]
    try:
        code, stdout = _run_cli_capture([])
        summary = _summary_of(stdout)
        assert code == 2, f"gated target existing must fail closed, got {code}"
        assert summary["coverage_verdict"] == "FAILED"
        assert summary["milvus_collections"]["target_exists"] is True
        collections = _parse_json_line(
            next(l for l in stdout.splitlines() if l.startswith("MILVUS_COLLECTIONS ")),
            "MILVUS_COLLECTIONS",
        )
        assert collections["target"] == ["graphinsight_chunks_v3"]
        assert "COVERAGE_FAIL_CLOSED" in stdout
    finally:
        _FakeMilvusClient.instance_collections = original_collections
        _restore(restore)


# ---------------------------------------------------------------------------
# 场景 6：before/after 计数漂移 → writes != 0 → FAILED
# ---------------------------------------------------------------------------


def scenario_counter_drift() -> None:
    _box, restore = _patch_sandbox([])
    calls = {"n": 0}
    real_snapshot = report.take_counter_snapshot

    def drifting_snapshot():
        calls["n"] += 1
        snap = real_snapshot()
        if calls["n"] >= 2:
            snap = copy.deepcopy(snap)
            snap["pg"]["chunk_revisions"]["value"] = int(snap["pg"]["chunk_revisions"]["value"]) + 1
        return snap

    report.take_counter_snapshot = drifting_snapshot
    try:
        code, stdout = _run_cli_capture([])
    finally:
        report.take_counter_snapshot = real_snapshot
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"counter drift must exit 2, got {code}"
    assert summary["writes"] == "UNVERIFIED_COUNTER_DRIFT"
    assert summary["coverage_verdict"] == "FAILED"
    assert summary["counter_drift"] == ["pg.chunk_revisions: 3 -> 4"]
    assert "COVERAGE_COUNTER_DRIFT" in stdout


# ---------------------------------------------------------------------------
# 场景 7：KB 注册表不可读 → 降级可审计，绝不伪装成"零 KB"
# ---------------------------------------------------------------------------


def scenario_registry_degraded_fail_closed() -> None:
    _box, restore = _patch_sandbox([])

    class _BrokenEngine:
        dialect = SimpleNamespace(name="sqlite")

        def connect(self):
            raise RuntimeError("registry unavailable")

    original_engine = report.engine
    report.engine = _BrokenEngine()
    try:
        code, stdout = _run_cli_capture([])
    finally:
        report.engine = original_engine
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"registry failure must exit 2, got {code}"
    assert summary["coverage_verdict"] == "FAILED"
    registry_items = [
        o for o in summary["omitted_kbs"] if o["reason"].startswith("KB_REGISTRY_QUERY_FAILED")
    ]
    assert registry_items, "registry degradation must be auditable in omitted_kbs"
    assert registry_items[0]["reason"] == "KB_REGISTRY_QUERY_FAILED: RuntimeError"
    assert "COVERAGE_FAIL_CLOSED" in stdout


# ---------------------------------------------------------------------------
# 场景 8-11（audit 修订 2/6）：单个证据源读取失败且前后值等价 0 —— 旧实现会把
# 错误折算成 0 从而 before==after 全部通过；新合同必须 value/ok/reason 显式记录，
# writes=UNVERIFIED_EVIDENCE、FAILED、exit 2。
# ---------------------------------------------------------------------------


def _assert_evidence_failure(summary: Dict[str, Any], expected_reason_prefix: str) -> None:
    assert summary["coverage_verdict"] == "FAILED"
    assert summary["writes"] == "UNVERIFIED_EVIDENCE"
    assert summary["coverage_fail_closed"] is True
    reasons = summary["degraded_reasons"]
    assert reasons and all(r.startswith(expected_reason_prefix) for r in reasons), reasons
    # 前后快照中失败源 value 必须为 None，绝不折算为 0。
    for snapshot in (summary["counters_before"], summary["counters_after"]):
        for reason in reasons:
            section, rest = reason.split(".", 1)
            key, _ = rest.split(": ", 1)
            entry = snapshot[section][key]
            assert entry["ok"] is False and entry["value"] is None


def scenario_pg_chunk_revisions_read_failure() -> None:
    box, restore = _patch_sandbox([])
    real_reader = report._read_pg_count_entry

    def failing_reader(table):
        if table == "chunk_revisions":
            return report._failed_entry("PG_COUNT_FAILED[chunk_revisions]: OperationalError")
        return real_reader(table)

    report._read_pg_count_entry = failing_reader
    try:
        code, stdout = _run_cli_capture([])
    finally:
        report._read_pg_count_entry = real_reader
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"PG count failure must exit 2, got {code}"
    _assert_evidence_failure(summary, "pg.chunk_revisions: PG_COUNT_FAILED[chunk_revisions]")
    assert "COVERAGE_DEGRADED reason=pg.chunk_revisions" in stdout
    # 探测本身仍执行（证据源失败独立于探测），但结果必须 fail-closed。
    assert sorted(box.probes) == ["kb-active-a", "kb-active-b"]


def scenario_admin_jobs_read_failure() -> None:
    _box, restore = _patch_sandbox([])
    real_reader = report._read_pg_count_entry

    def failing_reader(table):
        if table == "admin_jobs":
            return report._failed_entry("PG_COUNT_FAILED[admin_jobs]: OperationalError")
        return real_reader(table)

    report._read_pg_count_entry = failing_reader
    try:
        code, stdout = _run_cli_capture([])
    finally:
        report._read_pg_count_entry = real_reader
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"admin_jobs failure must exit 2, got {code}"
    _assert_evidence_failure(summary, "admin_jobs.rows: PG_COUNT_FAILED[admin_jobs]")


def scenario_neo4j_count_failure() -> None:
    _box, restore = _patch_sandbox([])
    neo4j_mod = sys.modules["neo4j"]
    broken_mod = types.ModuleType("neo4j")
    broken_mod.GraphDatabase = _FailingGraphDatabase
    sys.modules["neo4j"] = broken_mod
    try:
        code, stdout = _run_cli_capture([])
    finally:
        sys.modules["neo4j"] = neo4j_mod
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"Neo4j count failure must exit 2, got {code}"
    _assert_evidence_failure(summary, "neo4j.chunk_nodes: NEO4J_COUNT_FAILED")


def scenario_milvus_stats_failure() -> None:
    _box, restore = _patch_sandbox([])
    _FakeMilvusClient.stats_error = True
    try:
        code, stdout = _run_cli_capture([])
    finally:
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"Milvus stats failure must exit 2, got {code}"
    _assert_evidence_failure(summary, "milvus.entities: MILVUS_COUNT_FAILED")


# ---------------------------------------------------------------------------
# 场景 12（audit 修订 7）：active KB 带 scope_unresolved —— 仍必须被探测并进入
# C3 报告（scope_unresolved 是 inventory 内容，不是探测资格问题）。
# ---------------------------------------------------------------------------


def scenario_active_scope_unresolved_still_probed() -> None:
    box, restore = _patch_sandbox([])
    real_build = backfill.build_inventory

    def build_with_unresolved(kb_id: str):
        inventory = real_build(kb_id)
        if kb_id == "kb-active-a":
            inventory.scope_unresolved = ["chunk-scope-x"]
        return inventory

    backfill.build_inventory = build_with_unresolved
    try:
        code, stdout = _run_cli_capture([])
    finally:
        backfill.build_inventory = restore["build_inventory"]
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 0, f"scope_unresolved must not fail the run, got {code}"
    # 仍被探测：两个 active KB 都进入探测序列。
    assert sorted(box.probes) == ["kb-active-a", "kb-active-b"]
    assert summary["probed_kb_total"] == 2 and summary["coverage_verdict"] == "FULL"
    # C3_REPORT 中 scope_unresolved 逐 chunk 记录，带原因 SCOPE_UNRESOLVED。
    c3_reports = [
        json.loads(line[len("C3_REPORT "):])
        for line in stdout.splitlines()
        if line.startswith("C3_REPORT ")
    ]
    target = next(r for r in c3_reports if r["kb_id"] == "kb-active-a")
    assert target["counts"]["scope_unresolved"] == 1
    assert target["c3"]["scope_unresolved"] == [{"chunk_id": "chunk-scope-x", "reason": "SCOPE_UNRESOLVED"}]
    # 汇总 c3_totals 同步计入。
    assert summary["c3_totals"]["scope_unresolved"] == 1


# ---------------------------------------------------------------------------
# 场景 13（audit 修订 3）：environment_id 非法值 —— 整体拒绝为 INVALID 并
# fail-closed，绝不清洗拼接后输出（URI 凭据片段不得经部分剥离泄露）。
# ---------------------------------------------------------------------------


def scenario_environment_id_invalid_fail_closed() -> None:
    box, restore = _patch_sandbox([])
    os.environ["GRAPHINSIGHT_ENVIRONMENT_ID"] = "postgres://user:pass@host"
    try:
        code, stdout = _run_cli_capture([])
    finally:
        _restore(restore)
    summary = _summary_of(stdout)
    assert code == 2, f"invalid environment id must exit 2, got {code}"
    assert summary["coverage_verdict"] == "FAILED"
    assert summary["writes"] == "UNVERIFIED_EVIDENCE"
    assert summary["environment_id_invalid"] is True
    # 原值与其凭据片段绝不出现在任何输出行。
    for fragment in ("postgres://", "user:pass", "pass@host"):
        assert fragment not in stdout, f"leak detected: {fragment}"
    assert "ENVIRONMENT_ID_INVALID" in stdout
    kv = _parse_kv_line(
        next(l for l in stdout.splitlines() if l.startswith("GATE0_COVERAGE ")),
        "GATE0_COVERAGE",
    )
    assert kv["environment_id"] == "INVALID"
    # 探测仍执行（身份校验独立于探测），但结果必须 fail-closed。
    assert sorted(box.probes) == ["kb-active-a", "kb-active-b"]


SCENARIOS = (
    scenario_full_coverage_positive,
    scenario_scope_slice_and_unregistered,
    scenario_slice_all_ineligible,
    scenario_single_kb_probe_failure,
    scenario_target_collection_exists,
    scenario_counter_drift,
    scenario_registry_degraded_fail_closed,
    scenario_pg_chunk_revisions_read_failure,
    scenario_admin_jobs_read_failure,
    scenario_neo4j_count_failure,
    scenario_milvus_stats_failure,
    scenario_active_scope_unresolved_still_probed,
    scenario_environment_id_invalid_fail_closed,
)


def main() -> int:
    for scenario in SCENARIOS:
        scenario()
        print(f"OK {scenario.__name__}")
    print(f"GATE0_COVERAGE_CONTRACT_OK scenarios={len(SCENARIOS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
