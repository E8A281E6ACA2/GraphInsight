"""Isolated contract checks for the read-only M5 C3 inventory formatter."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.report_m5_c3_inventory import c3_report


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
    report = c3_report(inventory, {"closed": False, "needs_reindex": 1, "blocked": 1})
    assert report["kb_id"] == "kb-a"
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
    print("C3_INVENTORY_CONTRACT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
