# M5-B0 Dry-run Contract v1

This contract covers the M5-A/B0 administration entry points:

- `backend/admin/migrate_chunk_revisions.py`
- `backend/admin/migrate_jobs_targets_hash.py`
- `backend/admin/backfill_chunk_revisions.py`

When invoked with `--dry-run`, each entry point must emit exactly one
machine-readable line prefixed with `DRY_RUN_RESULT `. The suffix is JSON with
these required fields:

```json
{
  "contract_version": 1,
  "operation": "...",
  "mode": "dry-run",
  "writes": 0,
  "status": "ready|OPEN|CLOSED|CLOSED_DEGRADED|rejected|error",
  "exit_code": 0,
  "plan": []
}
```

`writes` must remain `0`; a dry-run must not create or update PostgreSQL rows,
Neo4j nodes, Milvus entities, or `admin_jobs`. Existing human-readable output
remains supported for operators.

Exit-code semantics are unchanged:

- `0`: preview completed; for backfill, the gate may still be `OPEN`.
- `1`: runtime or schema-check error.
- `2`: fail-closed refusal such as invalid scope, missing migration, or an
  unrecoverable/scope mismatch.
- `3`: execute mode completed with an open convergence gate.

The contract is additive and is validated by the SQLite acceptance suite. It is
not evidence of shared-production migration or a `CLOSED` shared gate.

## 2026-10-03 Read-only C3 probe

The read-only report found one shared KB. Its five C3 lists were empty, but the
resolved collection was `graphinsight_chunks_v2` without an explicit
`content_revision` field. The report therefore records
`shared_production_gate=OPEN`; the inventory's empty gate is not a production
acceptance decision.
