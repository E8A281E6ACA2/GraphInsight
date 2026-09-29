#!/usr/bin/env python3
"""Lock the current Python internal capability surface inventory."""
from __future__ import annotations

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from api.route_registry import INTERNAL_CAPABILITY_ROUTERS  # noqa: E402
from admin.api.route_registry import INTERNAL_ADMIN_CAPABILITY_ROUTERS  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _paths_from_specs(router_specs) -> set[str]:
    paths: set[str] = set()
    for router, prefix, _tags in router_specs:
        for route in router.routes:
            paths.add(f"{prefix}{route.path}")
    return paths


EXPECTED_INTERNAL_PATHS = {
    "/api/internal/docqa",
    "/api/internal/docqa/deep-research",
    "/api/internal/docqa/health",
    "/api/internal/docqa/retrieval-diagnostics",
    "/api/internal/nl2cypher",
    "/api/internal/vector/delete-doc",
    "/api/internal/jobs/wake",
}


def main() -> int:
    business_paths = _paths_from_specs(INTERNAL_CAPABILITY_ROUTERS)
    admin_paths = _paths_from_specs(INTERNAL_ADMIN_CAPABILITY_ROUTERS)
    all_paths = business_paths | admin_paths
    _assert(
        all_paths == EXPECTED_INTERNAL_PATHS,
        f"unexpected internal capability inventory\nexpected={sorted(EXPECTED_INTERNAL_PATHS)}\nactual={sorted(all_paths)}",
    )
    print("PYTHON_INTERNAL_ROUTE_INVENTORY_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
