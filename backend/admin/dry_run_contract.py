"""Shared machine-readable contract for destructive-operation previews.

Every M5 migration/backfill dry-run emits one ``DRY_RUN_RESULT`` JSON line.
The line is intentionally additive to the existing human-readable output so
operators and existing acceptance checks keep their current interface.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


DRY_RUN_CONTRACT_VERSION = 1
DRY_RUN_RESULT_MARKER = "DRY_RUN_RESULT "


def emit_dry_run_result(
    *,
    operation: str,
    status: str,
    exit_code: int,
    plan: list[str] | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Print a stable, secret-free preview result with an explicit zero-write claim."""

    result: dict[str, Any] = {
        "contract_version": DRY_RUN_CONTRACT_VERSION,
        "operation": operation,
        "mode": "dry-run",
        "writes": 0,
        "status": status,
        "exit_code": int(exit_code),
        "plan": list(plan or []),
    }
    if details:
        result["details"] = dict(details)
    print(DRY_RUN_RESULT_MARKER + json.dumps(result, ensure_ascii=True, sort_keys=True))
