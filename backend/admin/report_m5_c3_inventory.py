#!/usr/bin/env python3
"""Read-only per-KB C3 inventory for the M5 shared-production gate.

This command only loads PostgreSQL, Neo4j, Milvus, and parsed evidence through
the existing inventory readers. It never calls backfill, enqueues jobs, or
writes projection state. Shared production must remain migration-frozen while
this report is collected.

Gate 0 coverage evidence contract (audit/m5-gate0-completion):
1. Coverage counters: registered_kb_total / active_kb_total / eligible_kb_total
   / probed_kb_total / omitted_kb_total / failed_kb_total.
2. omitted_kbs / failed_kbs entries each carry kb_id / status / reason.
3. Inclusion policy: only status='active' KBs are eligible for probing;
   archived / deleting / soft-deleted (any non-active status) are omitted with
   STATUS_NOT_ELIGIBLE:<status>; explicit --kb ids absent from
   knowledge_bases are omitted with UNREGISTERED_KB.
4. coverage_verdict: FULL (all eligible probed) / PARTIAL (some probed) /
   FAILED (none probed, or any probed KB read failed -- fail-closed, never
   reported as full coverage).
5. Milvus collections are enumerated read-only (list_collections only) and
   classified current / target / historical / unknown; nothing is created,
   modified, or deleted.
6. PG / Neo4j / Milvus / admin_jobs counters are snapshotted before and after
   the probe; any drift fails closed (writes must be 0).
7. Output carries timestamp, commit_sha, and a non-sensitive environment_id;
   URIs, tokens, and passwords are never emitted.
8. --kb slice probes are marked scope=SCOPE_SLICE and capped at PARTIAL.
Isolated contract tests only prove this formatter/state machine; they do not
constitute shared-production Gate 0 closure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine  # noqa: E402
from admin import backfill_chunk_revisions as backfill  # noqa: E402


MILVUS_TARGET_COLLECTION = "graphinsight_chunks_v3"
MILVUS_HISTORICAL_COLLECTIONS = frozenset({"graphinsight_chunks"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_commit_sha() -> str:
    """Resolve the running source tree commit; never fatal, never spoofed."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(backend_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else "UNKNOWN"


def _count_rows(conn: Any, table: str) -> int:
    try:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)  # noqa: S608
    except Exception:  # noqa: BLE001 - 缺表/权限不足时按 0 计并保留 reason
        return 0


def _load_kbs_with_fallback() -> Tuple[Dict[str, str], Optional[str]]:
    """Load registered KBs; on query failure degrade to an empty map with a reason.

    Failing closed here keeps the run auditable: an unknown registry state must
    not silently look like "zero KBs registered".
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, status FROM knowledge_bases ORDER BY id")
            ).fetchall()
        return {
            str(row[0]): str(row[1] or "active")
            for row in rows
            if str(row[0] or "").strip()
        }, None
    except Exception as exc:  # noqa: BLE001 - 只保留非敏感错误类别
        return {}, f"KB_REGISTRY_QUERY_FAILED: {type(exc).__name__}"


def _load_kbs() -> Dict[str, str]:
    """Backward-compatible wrapper returning just the KB status map."""
    return _load_kbs_with_fallback()[0]


def classify_collections(
    configured: str,
    existing: List[str],
) -> Dict[str, Any]:
    """Read-only classification of Milvus collections for gate evidence.

    - current: the collection the live read/write path resolves to (must be a
      normalized v2 name per §11.1/D3).
    - target: the §8.5 v3 migration target (must NOT exist while migration is
      gated; its existence is reported as a fail-closed signal).
    - historical: legacy pre-isolation names kept only for reference.
    - unknown: any other collection discovered on the instance.

    This function never creates, modifies, or deletes anything.
    """
    configured_norm = str(configured or "").strip()
    existing_sorted = sorted({str(name or "").strip() for name in existing if str(name or "").strip()})
    categories = {
        "current": [],
        "target": [],
        "historical": [],
        "unknown": [],
    }
    for name in existing_sorted:
        if name == configured_norm:
            categories["current"].append(name)
        elif name == MILVUS_TARGET_COLLECTION:
            categories["target"].append(name)
        elif name in MILVUS_HISTORICAL_COLLECTIONS:
            categories["historical"].append(name)
        else:
            categories["unknown"].append(name)
    return {
        "configured": configured_norm,
        "resolved": configured_norm,
        "existing": existing_sorted,
        "categories": categories,
        "existing_total": len(existing_sorted),
        "read_only": True,
        "target_exists": bool(categories["target"]),
        "classification_note": (
            "legacy graphinsight_chunks normalizes to graphinsight_chunks_v2 (§11.1/D3); "
            "target existence is reported but never modified"
        ),
    }


def coverage_verdict(
    probed: int,
    eligible: int,
    failed: int,
    scope: str,
) -> str:
    """FULL / PARTIAL / FAILED verdict; fail-closed on any probe failure.

    A single failed KB read forces FAILED even when others succeeded: the run
    must never claim full coverage while any eligible target is unreadable.
    SCOPE_SLICE runs are additionally capped at PARTIAL in summarize().
    """
    if failed > 0:
        return "FAILED"
    if eligible <= 0 or probed <= 0:
        return "FAILED"
    if probed >= eligible:
        return "FULL"
    return "PARTIAL"


def _resolve_targets(kbs: Dict[str, str], requested: Iterable[str]) -> List[Tuple[str, str]]:
    """Resolve requested KBs while keeping unregistered explicit ids auditable."""
    requested_ids = list(requested)
    if requested_ids:
        return [(kb_id, kbs.get(kb_id, "unregistered")) for kb_id in requested_ids]
    return list(kbs.items())


def _reason_items(chunk_ids: Iterable[str], reason: str) -> List[Dict[str, str]]:
    return [{"chunk_id": str(chunk_id), "reason": reason} for chunk_id in sorted(set(chunk_ids))]


def c3_report(inventory: Any, gate: Dict[str, Any], kb_status: str) -> Dict[str, Any]:
    """Convert an Inventory into a stable, secret-free C3 report payload."""

    return {
        "kb_id": inventory.kb_id,
        "kb_status": kb_status,
        "inventory_gate": gate,
        "shared_production_gate": "OPEN",
        "shared_production_gate_reason": "shared v3 migration and authorization are still pending",
        "capabilities": {
            "graph_enabled": bool(inventory.graph_enabled),
            "vector_enabled": bool(inventory.vector_enabled),
            "milvus_collection": inventory.milvus_collection,
            "milvus_revision_field": bool(inventory.milvus_revision_field),
        },
        "counts": {
            "blocked": inventory.blocked,
            "orphan_revisions": len(inventory.orphan_revisions),
            "unrecoverable": len(inventory.unrecoverable),
            "scope_unresolved": len(inventory.scope_unresolved),
            "scope_mismatches": len(inventory.scope_mismatches),
            "needs_reindex": len(inventory.needs_reindex_targets),
        },
        "c3": {
            "blocked": list(inventory.blocked_targets),
            "orphan_revisions": _reason_items(inventory.orphan_revisions, "ORPHAN_REVISION"),
            "unrecoverable": _reason_items(inventory.unrecoverable, "UNRECOVERABLE_MISMATCH"),
            "scope_unresolved": _reason_items(inventory.scope_unresolved, "SCOPE_UNRESOLVED"),
            "scope_mismatches": list(inventory.scope_mismatches),
        },
        "needs_reindex_targets": [
            {
                "chunk_id": str(item["chunk_id"]),
                "doc_id": str(item.get("doc_id") or ""),
                "target_revision": int(item["target_revision"]),
                "graph_state": str(item.get("graph_state") or ""),
                "vector_state": str(item.get("vector_state") or ""),
            }
            for item in inventory.needs_reindex_targets
        ],
    }


C3_COUNT_KEYS = (
    "blocked",
    "orphan_revisions",
    "unrecoverable",
    "scope_unresolved",
    "scope_mismatches",
    "needs_reindex",
)


def summarize(
    reports: List[Dict[str, Any]],
    coverage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aggregate C3 reports into a stable, secret-free full-scope rollup.

    Returns the Gate 0 coverage counters, per-status counts (active/archived/
    deleting and any unregistered explicit ids), the six C3 category totals,
    and needs_reindex. SCOPE_SLICE runs are capped at PARTIAL regardless of
    probe success, and must never be presented as full coverage.
    """
    by_status: Dict[str, int] = {}
    c3_totals = {key: 0 for key in C3_COUNT_KEYS}
    needs_reindex_total = 0
    for report in reports:
        status = report["kb_status"]
        by_status[status] = by_status.get(status, 0) + 1
        counts = report["counts"]
        for key in C3_COUNT_KEYS:
            c3_totals[key] += int(counts[key])
        needs_reindex_total += len(report["needs_reindex_targets"])
    cov = dict(coverage or {})
    probed = int(cov.get("probed_kb_total", len(reports)))
    eligible = int(cov.get("eligible_kb_total", probed))
    failed = int(cov.get("failed_kb_total", 0))
    scope = str(cov.get("scope", "FULL"))
    verdict = coverage_verdict(probed, eligible, failed, scope)
    if scope == "SCOPE_SLICE" and verdict == "FULL":
        verdict = "PARTIAL"
    # fail-closed 信号（计数漂移/注册表或 Milvus 证据降级/受限目标已存在）压过一切成功判定：
    # 证据链不完整时不得宣称全量覆盖成功。
    if bool(cov.get("coverage_fail_closed")):
        verdict = "FAILED"
    return {
        "kb_count": len(reports),
        "by_status": by_status,
        "c3_totals": c3_totals,
        "needs_reindex_total": needs_reindex_total,
        "read_only": True,
        "scope": scope,
        "registered_kb_total": int(cov.get("registered_kb_total", probed)),
        "active_kb_total": int(cov.get("active_kb_total", probed)),
        "eligible_kb_total": eligible,
        "probed_kb_total": probed,
        "omitted_kb_total": int(cov.get("omitted_kb_total", 0)),
        "failed_kb_total": failed,
        "coverage_verdict": verdict,
        "coverage_fail_closed": bool(cov.get("coverage_fail_closed", failed > 0)),
        "omitted_kbs": list(cov.get("omitted_kbs", [])),
        "failed_kbs": list(cov.get("failed_kbs", [])),
        "writes": cov.get("writes", "unknown"),
        "counter_drift": list(cov.get("counter_drift", [])),
        "milvus_collections": cov.get("milvus_collections", {}),
        "timestamp": cov.get("timestamp", ""),
        "commit_sha": cov.get("commit_sha", "UNKNOWN"),
        "environment_id": cov.get("environment_id", ""),
    }


def summary_line(summary: Dict[str, Any]) -> str:
    """Serialize the rollup as the single parseable C3_SUMMARY line the CLI emits."""
    return "C3_SUMMARY " + json.dumps(summary, ensure_ascii=True, sort_keys=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only M5 C3 inventory by knowledge base")
    parser.add_argument("--kb", action="append", default=[], help="restrict to one or more KB ids")
    parser.add_argument("--json", action="store_true", help="emit C3_REPORT JSON lines")
    return parser.parse_args()


def _milvus_snapshot() -> Tuple[int, Optional[str]]:
    """Count Milvus entities on the current collection; degrade with a reason."""
    try:
        from pymilvus import MilvusClient

        collection = backfill._milvus_collection_name()
        cfg = {}
        try:
            from services.runtime_config import get_vector_store_runtime_config

            cfg = get_vector_store_runtime_config()
        except Exception:  # noqa: BLE001
            cfg = {}
        client = MilvusClient(
            uri=str(cfg.get("uri") or getattr(backfill.settings, "milvus_uri", "")),
            token=str(cfg.get("token") or getattr(backfill.settings, "milvus_token", "") or ""),
            db_name=str(cfg.get("db_name") or getattr(backfill.settings, "milvus_db_name", "default")),
        )
        if not client.has_collection(collection):
            return 0, None
        stats = client.get_collection_stats(collection)
        if isinstance(stats, dict):
            for key in ("row_count", "rowNum", "num_entities"):
                if stats.get(key) is not None:
                    return int(stats[key]), None
        return 0, "MILVUS_STATS_UNREADABLE"
    except Exception as exc:  # noqa: BLE001 - 只保留非敏感错误类别
        return 0, f"MILVUS_COUNT_FAILED: {type(exc).__name__}"


def _neo4j_snapshot() -> Tuple[int, Optional[str]]:
    """Count Neo4j Chunk nodes; degrade with a reason instead of crashing."""
    try:
        from neo4j import GraphDatabase

        settings = backfill.settings
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
        )
        try:
            with driver.session(
                database=getattr(settings, "neo4j_database", None) or None
            ) as session:
                record = session.run("MATCH (c:Chunk) RETURN count(c) AS n").single()
                return int(record["n"] if record else 0), None
        finally:
            driver.close()
    except Exception as exc:  # noqa: BLE001 - 只保留非敏感错误类别
        return 0, f"NEO4J_COUNT_FAILED: {type(exc).__name__}"


def _admin_jobs_snapshot() -> int:
    try:
        with engine.connect() as conn:
            return _count_rows(conn, "admin_jobs")
    except Exception:  # noqa: BLE001
        return 0


def take_counter_snapshot() -> Dict[str, Any]:
    """Read-only before/after counters for PG / Neo4j / Milvus / admin_jobs."""
    pg_counts: Dict[str, int] = {}
    reasons: List[str] = []
    try:
        with engine.connect() as conn:
            pg_counts["chunk_revisions"] = _count_rows(conn, "chunk_revisions")
            pg_counts["knowledge_bases"] = _count_rows(conn, "knowledge_bases")
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"PG_COUNT_FAILED: {type(exc).__name__}")
    neo4j_count, neo4j_reason = _neo4j_snapshot()
    milvus_count, milvus_reason = _milvus_snapshot()
    for reason in (neo4j_reason, milvus_reason, *reasons):
        if reason:
            reasons.append(reason)
    return {
        "pg": {
            "chunk_revisions": pg_counts.get("chunk_revisions", 0),
            "knowledge_bases": pg_counts.get("knowledge_bases", 0),
        },
        "neo4j": {"chunk_nodes": neo4j_count},
        "milvus": {"entities": milvus_count},
        "admin_jobs": {"rows": _admin_jobs_snapshot()},
        "degraded_reasons": sorted(set(reasons)),
    }


def snapshots_match(before: Dict[str, Any], after: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Fail-closed comparison of two snapshots; any drift means writes happened."""
    diffs: List[str] = []
    for section in ("pg", "neo4j", "milvus", "admin_jobs"):
        before_section = before.get(section) or {}
        after_section = after.get(section) or {}
        for key in sorted(set(before_section) | set(after_section)):
            if before_section.get(key) != after_section.get(key):
                diffs.append(f"{section}.{key}: {before_section.get(key)} -> {after_section.get(key)}")
    return not diffs, diffs


def _probe_kb(kb_id: str, kb_status: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Probe one KB; return (report, failure). Exactly one side is non-None."""
    try:
        inventory = backfill.build_inventory(kb_id)
    except Exception as exc:  # noqa: BLE001 - fail-closed：单 KB 读取失败不得静默吞掉
        return None, {
            "kb_id": kb_id,
            "status": kb_status,
            "reason": f"KB_PROBE_FAILED: {type(exc).__name__}",
        }
    report = c3_report(inventory, backfill.evaluate_gate(inventory), kb_status)
    return report, None


def _resolve_milvus_evidence() -> Dict[str, Any]:
    """Enumerate existing collections read-only and classify them."""
    configured = ""
    existing: List[str] = []
    degraded: List[str] = []
    try:
        configured = backfill._milvus_collection_name()
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"MILVUS_CONFIG_RESOLVE_FAILED: {type(exc).__name__}")
    try:
        from pymilvus import MilvusClient

        cfg = {}
        try:
            from services.runtime_config import get_vector_store_runtime_config

            cfg = get_vector_store_runtime_config()
        except Exception:  # noqa: BLE001
            pass
        client = MilvusClient(
            uri=str(cfg.get("uri") or getattr(backfill.settings, "milvus_uri", "")),
            token=str(cfg.get("token") or getattr(backfill.settings, "milvus_token", "") or ""),
            db_name=str(cfg.get("db_name") or getattr(backfill.settings, "milvus_db_name", "default")),
        )
        listed = client.list_collections()
        existing = [str(name) for name in (listed or [])]
    except Exception as exc:  # noqa: BLE001
        degraded.append(f"MILVUS_LIST_COLLECTIONS_FAILED: {type(exc).__name__}")
    classification = classify_collections(configured, existing)
    if degraded:
        classification["degraded_reasons"] = sorted(set(degraded))
        classification["coverage_degraded"] = True
    return classification


def _build_coverage(
    registered: Dict[str, str],
    registry_reason: Optional[str],
    targets: List[Tuple[str, str]],
    requested: List[str],
) -> Dict[str, Any]:
    """Assemble Gate 0 coverage counters from the inclusion policy."""
    omitted: List[Dict[str, str]] = []
    probe_plan: List[Tuple[str, str]] = []
    if requested:
        scope = "SCOPE_SLICE"
        for kb_id, kb_status in targets:
            if kb_status == "unregistered":
                omitted.append({"kb_id": kb_id, "status": kb_status, "reason": "UNREGISTERED_KB"})
            elif kb_status != "active":
                omitted.append(
                    {
                        "kb_id": kb_id,
                        "status": kb_status,
                        "reason": f"STATUS_NOT_ELIGIBLE:{kb_status}",
                    }
                )
            else:
                probe_plan.append((kb_id, kb_status))
        eligible_total = len(probe_plan)
        # --kb 覆盖度只在显式请求集合内判定；SCOPE_SLICE 永远不能宣称 FULL。
    else:
        scope = "FULL"
        for kb_id, kb_status in sorted(registered.items()):
            if kb_status == "active":
                probe_plan.append((kb_id, kb_status))
            else:
                omitted.append(
                    {
                        "kb_id": kb_id,
                        "status": kb_status,
                        "reason": f"STATUS_NOT_ELIGIBLE:{kb_status}",
                    }
                )
        eligible_total = len(probe_plan)
    if registry_reason:
        omitted.append({"kb_id": "*", "status": "registry", "reason": registry_reason})
    return {
        "scope": scope,
        "registered_kb_total": len(registered),
        "active_kb_total": sum(1 for status in registered.values() if status == "active"),
        "eligible_kb_total": eligible_total,
        "omitted_kbs": omitted,
        "planned": probe_plan,
        "registry_degraded": bool(registry_reason),
    }


def main() -> int:
    args = _parse_args()
    # 隔离契约测试可用 C3_INVENTORY_ALLOW_NON_PG=1 在 SQLite 上运行本 CLI 的格式/状态机；
    # 共享生产证据采集仍必须走 PostgreSQL，且不允许设置该豁免。
    allow_non_pg = os.environ.get("C3_INVENTORY_ALLOW_NON_PG", "").strip() == "1"
    if engine.dialect.name != "postgresql" and not allow_non_pg:
        print(f"C3 inventory requires PostgreSQL; got dialect={engine.dialect.name}")
        return 2

    registered, registry_reason = _load_kbs_with_fallback()
    targets = _resolve_targets(registered, args.kb)
    if not targets and not registry_reason:
        print("C3 inventory found no knowledge_bases rows")
        return 2

    coverage = _build_coverage(registered, registry_reason, targets, list(args.kb))
    probe_plan = coverage.pop("planned")

    before = take_counter_snapshot()
    reports: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for kb_id, kb_status in probe_plan:
        report, failure = _probe_kb(kb_id, kb_status)
        if failure is not None:
            failed.append(failure)
            print(
                f"[C3-FAILED] kb={kb_id} kb_status={kb_status} "
                f"reason={failure['reason']} (fail-closed, not counted as covered)"
            )
            continue
        reports.append(report)
        if not args.json:
            counts = report["counts"]
            print(
                f"[C3] kb={kb_id} kb_status={kb_status} blocked={counts['blocked']} "
                f"orphan_revisions={counts['orphan_revisions']} "
                f"unrecoverable={counts['unrecoverable']} "
                f"scope_unresolved={counts['scope_unresolved']} "
                f"scope_mismatches={counts['scope_mismatches']} "
                f"needs_reindex={counts['needs_reindex']} "
                f"inventory_gate={report['inventory_gate']['closed']} "
                "shared_production_gate=OPEN"
            )
        print("C3_REPORT " + json.dumps(report, ensure_ascii=True, sort_keys=True))

    after = take_counter_snapshot()
    counters_match, drift = snapshots_match(before, after)
    milvus_collections = _resolve_milvus_evidence()

    coverage.update(
        {
            "probed_kb_total": len(reports),
            "omitted_kb_total": len(coverage.get("omitted_kbs", [])),
            "failed_kb_total": len(failed),
            "failed_kbs": failed,
            "writes": 0 if counters_match else "UNVERIFIED_COUNTER_DRIFT",
            "counter_drift": drift,
            "counters_before": before,
            "counters_after": after,
            "milvus_collections": milvus_collections,
            "timestamp": _utc_now_iso(),
            "commit_sha": _resolve_commit_sha(),
            "environment_id": f"admin_db:{engine.dialect.name}",
        }
    )
    coverage["coverage_fail_closed"] = bool(
        coverage["failed_kb_total"] > 0
        or not counters_match
        or coverage.get("registry_degraded")
        or milvus_collections.get("coverage_degraded")
        or milvus_collections.get("target_exists")
    )

    summary = summarize(reports, coverage)
    if not args.json:
        by_status = ", ".join(f"{status}={summary['by_status'][status]}" for status in sorted(summary["by_status"]))
        totals = summary["c3_totals"]
        print(
            f"C3 inventory complete: kb_count={summary['kb_count']} by_status=[{by_status}] "
            f"needs_reindex_total={summary['needs_reindex_total']} "
            f"blocked_total={totals['blocked']} read_only=true"
        )
        print(
            f"GATE0_COVERAGE scope={summary['scope']} "
            f"registered={summary['registered_kb_total']} active={summary['active_kb_total']} "
            f"eligible={summary['eligible_kb_total']} probed={summary['probed_kb_total']} "
            f"omitted={summary['omitted_kb_total']} failed={summary['failed_kb_total']} "
            f"coverage_verdict={summary['coverage_verdict']} writes={summary['writes']} "
            f"shared_production_gate=OPEN"
        )
        for item in summary["omitted_kbs"]:
            print(f"COVERAGE_OMITTED kb={item['kb_id']} status={item['status']} reason={item['reason']}")
        for item in summary["failed_kbs"]:
            print(f"COVERAGE_FAILED kb={item['kb_id']} status={item['status']} reason={item['reason']}")
        if drift:
            print(f"COVERAGE_COUNTER_DRIFT drift={json.dumps(drift, ensure_ascii=True, sort_keys=True)}")
        cats = summary["milvus_collections"].get("categories") or {}
        print(
            "MILVUS_COLLECTIONS "
            + json.dumps(
                {
                    "configured": summary["milvus_collections"].get("configured", ""),
                    "existing_total": summary["milvus_collections"].get("existing_total", 0),
                    "current": cats.get("current", []),
                    "target": cats.get("target", []),
                    "historical": cats.get("historical", []),
                    "unknown": cats.get("unknown", []),
                    "read_only": True,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        if summary["coverage_verdict"] == "FAILED":
            print(
                "COVERAGE_FAIL_CLOSED：存在探测失败/计数漂移/注册表或 Milvus 证据降级，"
                "本轮不得宣称全量覆盖成功；shared_production_gate 保持 OPEN"
            )
    print(summary_line(summary))
    if summary["coverage_verdict"] == "FAILED":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
