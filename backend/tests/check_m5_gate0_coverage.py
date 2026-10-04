#!/usr/bin/env python3
"""Isolated Gate 0 coverage contract tests for the M5 C3 inventory CLI.

Fully isolated: SQLite in-memory registry, fake pymilvus/neo4j modules, and a
stubbed build_inventory. Nothing here touches shared dev Milvus/Neo4j/PG, and
no report produced by this test may be cited as shared-production evidence.

Positive / negative matrix (task contract):
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

    def __init__(self, uri=None, token=None, db_name=None):
        self.uri = uri

    def list_collections(self):
        return list(_FakeMilvusClient.instance_collections)

    def has_collection(self, name):
        return name in _FakeMilvusClient.instance_collections

    def get_collection_stats(self, name):
        return {"row_count": 7}


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
        "allow_non_pg": os.environ.get("C3_INVENTORY_ALLOW_NON_PG"),
    }
    _install_fake_drivers(restore)
    report.engine = engine
    report._resolve_commit_sha = lambda: COMMIT_SHA
    backfill.build_inventory = stub_build
    backfill._milvus_collection_name = lambda: "graphinsight_chunks_v2"
    os.environ["C3_INVENTORY_ALLOW_NON_PG"] = "1"
    return _Sandbox(engine, probes), restore


def _restore(restore: Dict[str, Any]) -> None:
    report.engine = restore["engine"]
    report.take_counter_snapshot = restore["take_counter_snapshot"]
    report._resolve_commit_sha = restore["resolve_commit_sha"]
    backfill.build_inventory = restore["build_inventory"]
    backfill._milvus_collection_name = restore["milvus_collection_name"]
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
        # 状态排除：三个非 active KB 全部带逐项原因，且从未被探测。
        reasons = {(o["kb_id"], o["reason"]) for o in summary["omitted_kbs"]}
        assert ("kb-archived", "STATUS_NOT_ELIGIBLE:archived") in reasons
        assert ("kb-deleting", "STATUS_NOT_ELIGIBLE:deleting") in reasons
        assert ("kb-softdel", "STATUS_NOT_ELIGIBLE:soft_deleted") in reasons
        assert sorted(box.probes) == ["kb-active-a", "kb-active-b"]
        for item in summary["omitted_kbs"]:
            assert set(item) == {"kb_id", "status", "reason"}
        # 多 collection 只读分类（正腿：无 v3）。
        collections = _parse_json_line(
            next(l for l in stdout.splitlines() if l.startswith("MILVUS_COLLECTIONS ")),
            "MILVUS_COLLECTIONS",
        )
        assert collections["configured"] == "graphinsight_chunks_v2"
        assert collections["current"] == ["graphinsight_chunks_v2"]
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
            snap["pg"]["chunk_revisions"] += 1
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
    assert summary["counter_drift"] == ["pg.chunk_revisions: 0 -> 1"]
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


SCENARIOS = (
    scenario_full_coverage_positive,
    scenario_scope_slice_and_unregistered,
    scenario_slice_all_ineligible,
    scenario_single_kb_probe_failure,
    scenario_target_collection_exists,
    scenario_counter_drift,
    scenario_registry_degraded_fail_closed,
)


def main() -> int:
    for scenario in SCENARIOS:
        scenario()
        print(f"OK {scenario.__name__}")
    print(f"GATE0_COVERAGE_CONTRACT_OK scenarios={len(SCENARIOS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
