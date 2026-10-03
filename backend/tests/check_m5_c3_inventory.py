"""Isolated contract checks for the read-only M5 C3 inventory formatter."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.report_m5_c3_inventory import (
    C3_COUNT_KEYS,
    _resolve_targets,
    c3_report,
    summarize,
    summary_line,
)


def main() -> int:
    inventory = SimpleNamespace(
        kb_id="kb-a",
        blocked=1,
        blocked_targets=[
            {
                "chunk_id": "blocked-1",
                "doc_id": "doc-1",
                "reason": "PROJECTION_BLOCKED",
                "graph_state": "blocked",
                "vector_state": "blocked",
            }
        ],
        orphan_revisions=["orphan-1"],
        unrecoverable=["unrecoverable-1"],
        scope_unresolved=["scope-1"],
        scope_mismatches=[
            {"chunk_id": "scope-mismatch-1", "field": "tenant_id", "expected": "t1", "actual": "t2"}
        ],
        needs_reindex_targets=[
            {
                "chunk_id": "pending-1",
                "doc_id": "doc-2",
                "target_revision": 3,
                "graph_state": "needs_reindex",
                "vector_state": "needs_reindex",
            }
        ],
        graph_enabled=True,
        vector_enabled=True,
        milvus_collection="graphinsight_chunks_v2",
        milvus_revision_field=False,
    )
    report = c3_report(
        inventory,
        {"closed": False, "needs_reindex": 1, "blocked": 1},
        "archived",
    )
    assert report["kb_id"] == "kb-a"
    assert report["kb_status"] == "archived"
    assert report["inventory_gate"]["closed"] is False
    assert report["shared_production_gate"] == "OPEN"
    assert report["counts"] == {
        "blocked": 1,
        "orphan_revisions": 1,
        "unrecoverable": 1,
        "scope_unresolved": 1,
        "scope_mismatches": 1,
        "needs_reindex": 1,
    }
    assert report["c3"]["blocked"][0]["chunk_id"] == "blocked-1"
    assert report["c3"]["orphan_revisions"] == [{"chunk_id": "orphan-1", "reason": "ORPHAN_REVISION"}]
    assert report["needs_reindex_targets"][0]["target_revision"] == 3
    assert _resolve_targets(
        {"kb-active": "active", "kb-archived": "archived", "kb-deleting": "deleting"},
        [],
    ) == [
        ("kb-active", "active"),
        ("kb-archived", "archived"),
        ("kb-deleting", "deleting"),
    ]
    assert _resolve_targets({"kb-active": "active"}, ["kb-active", "kb-missing"]) == [
        ("kb-active", "active"),
        ("kb-missing", "unregistered"),
    ]

    # --- summarize + C3_SUMMARY 契约（全状态 rollup）---
    def _mk_report(status, counts, n_reindex):
        return {
            "kb_status": status,
            "counts": {key: counts.get(key, 0) for key in C3_COUNT_KEYS},
            "needs_reindex_targets": [{"chunk_id": f"c{i}"} for i in range(n_reindex)],
        }

    reports = [
        _mk_report("active", {"blocked": 2, "needs_reindex": 3, "orphan_revisions": 1}, 3),
        _mk_report("active", {"scope_unresolved": 1, "scope_mismatches": 2}, 0),
        _mk_report("archived", {"unrecoverable": 4}, 0),
        _mk_report("deleting", {"blocked": 1}, 0),
        _mk_report("unregistered", {}, 0),
    ]
    summary = summarize(reports)
    assert summary["kb_count"] == 5
    # by_status 必须显式覆盖四种状态（含 unregistered），不再只列 active。
    assert summary["by_status"] == {
        "active": 2,
        "archived": 1,
        "deleting": 1,
        "unregistered": 1,
    }
    assert set(summary["by_status"]) == {"active", "archived", "deleting", "unregistered"}
    # c3_totals 六项逐项合计正确。
    assert summary["c3_totals"] == {
        "blocked": 3,
        "orphan_revisions": 1,
        "unrecoverable": 4,
        "scope_unresolved": 1,
        "scope_mismatches": 2,
        "needs_reindex": 3,
    }
    assert set(summary["c3_totals"]) == set(C3_COUNT_KEYS)
    assert summary["needs_reindex_total"] == 3
    assert summary["read_only"] is True

    # CLI 的 C3_SUMMARY 行必须单行、可解析、字段自洽。
    line = summary_line(summary)
    assert line.startswith("C3_SUMMARY ")
    payload = json.loads(line[len("C3_SUMMARY "):])
    assert payload == summary
    assert payload["kb_count"] == 5 and payload["needs_reindex_total"] == 3
    assert "\n" not in line

    print("C3_INVENTORY_CONTRACT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
