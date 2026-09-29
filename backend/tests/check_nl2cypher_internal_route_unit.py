#!/usr/bin/env python3
"""Unit-style checks for internal NL2Cypher capability route."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from main import app  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    client = TestClient(app)

    denied = client.post("/api/internal/nl2cypher", json={"natural_language": "MATCH"})
    _assert(denied.status_code == 403, f"expected 403 without Go orchestrator header, got {denied.status_code}")

    missing_trace = client.post(
        "/api/internal/nl2cypher",
        headers={"X-Go-Orchestrator": "graphinsight-go"},
        json={"natural_language": "查找所有文档"},
    )
    _assert(missing_trace.status_code == 400, f"expected 400 without trace header, got {missing_trace.status_code}")

    blank = client.post(
        "/api/internal/nl2cypher",
        headers={
            "X-Go-Orchestrator": "graphinsight-go",
            "X-Trace-Id": "trace-internal-nl2cypher-blank",
            "X-KB-Id": "kb-1",
        },
        json={"natural_language": "   "},
    )
    _assert(blank.status_code == 400, f"expected 400 for blank natural language, got {blank.status_code}")

    # 缺少 kb 作用域 -> KB_SCOPE_REQUIRED(400)：契约 §2.4 作用域强制点，
    # Go 已授权但 Python 数据面不把"来自 Go"当作授权证明。
    missing_scope = client.post(
        "/api/internal/nl2cypher",
        headers={
            "X-Go-Orchestrator": "graphinsight-go",
            "X-Trace-Id": "trace-internal-nl2cypher-no-scope",
        },
        json={"natural_language": "查找所有文档"},
    )
    _assert(missing_scope.status_code == 400, f"expected 400 without kb scope, got {missing_scope.status_code}")

    allowed = client.post(
        "/api/internal/nl2cypher",
        headers={
            "X-Go-Orchestrator": "graphinsight-go",
            "X-Trace-Id": "trace-internal-nl2cypher",
            "X-KB-Id": "kb-1",
        },
        json={"natural_language": "查找所有文档"},
    )
    _assert(allowed.status_code in {200, 500, 503}, f"unexpected status with Go orchestrator header: {allowed.status_code}")
    body = allowed.json()
    _assert(isinstance(body, dict), f"expected dict body, got {type(body)}")
    if allowed.status_code == 200:
        _assert(
            "success" in body,
            f"expected NL2Cypher capability response envelope, got {body}",
        )
        _assert(
            body.get("success") is True or "error" in body,
            f"expected success response or explicit capability error, got {body}",
        )
    print("NL2CYPHER_INTERNAL_ROUTE_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
