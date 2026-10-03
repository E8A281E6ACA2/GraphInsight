#!/usr/bin/env python3
"""Read-only per-KB C3 inventory for the M5 shared-production gate.

This command only loads PostgreSQL, Neo4j, Milvus, and parsed evidence through
the existing inventory readers. It never calls backfill, enqueues jobs, or
writes projection state. Shared production must remain migration-frozen while
this report is collected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import text

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine  # noqa: E402
from admin import backfill_chunk_revisions as backfill  # noqa: E402


def _load_kbs() -> Dict[str, str]:
    """Load every registered KB and its status in stable id order."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, status FROM knowledge_bases ORDER BY id")).fetchall()
    return {
        str(row[0]): str(row[1] or "active")
        for row in rows
        if str(row[0] or "").strip()
    }


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


def summarize(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate C3 reports into a stable, secret-free full-scope rollup.

    Returns total KB count, per-status counts (active/archived/deleting and any
    unregistered explicit ids), the five C3 category totals, and needs_reindex.
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
    return {
        "kb_count": len(reports),
        "by_status": by_status,
        "c3_totals": c3_totals,
        "needs_reindex_total": needs_reindex_total,
        "read_only": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only M5 C3 inventory by knowledge base")
    parser.add_argument("--kb", action="append", default=[], help="restrict to one or more KB ids")
    parser.add_argument("--json", action="store_true", help="emit C3_REPORT JSON lines")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if engine.dialect.name != "postgresql":
        print(f"C3 inventory requires PostgreSQL; got dialect={engine.dialect.name}")
        return 2

    targets = _resolve_targets(_load_kbs(), args.kb)
    if not targets:
        print("C3 inventory found no knowledge_bases rows")
        return 2

    reports: List[Dict[str, Any]] = []
    for kb_id, kb_status in targets:
        inventory = backfill.build_inventory(kb_id)
        report = c3_report(inventory, backfill.evaluate_gate(inventory), kb_status)
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

    summary = summarize(reports)
    if not args.json:
        by_status = ", ".join(f"{status}={summary['by_status'][status]}" for status in sorted(summary["by_status"]))
        totals = summary["c3_totals"]
        print(
            f"C3 inventory complete: kb_count={summary['kb_count']} by_status=[{by_status}] "
            f"needs_reindex_total={summary['needs_reindex_total']} "
            f"blocked_total={totals['blocked']} read_only=true"
        )
    print("C3_SUMMARY " + json.dumps(summary, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
