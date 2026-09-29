#!/usr/bin/env python3
"""Unit-style checks for internal DocQA routes."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from main import app  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    client = TestClient(app)

    denied = client.get("/api/internal/docqa/health?probe_llm=false")
    _assert(denied.status_code == 403, f"expected 403 without Go orchestrator header, got {denied.status_code}")

    missing_trace = client.get(
        "/api/internal/docqa/health?probe_llm=false",
        headers={"X-Go-Orchestrator": "graphinsight-go"},
    )
    _assert(missing_trace.status_code == 400, f"expected 400 without trace header, got {missing_trace.status_code}")

    proxy_only = client.get(
        "/api/internal/docqa/health?probe_llm=false",
        headers={"X-Go-Proxy": "graphinsight-go", "X-Trace-Id": "trace-proxy-only"},
    )
    _assert(proxy_only.status_code == 403, f"expected 403 for proxy-only access, got {proxy_only.status_code}")

    allowed = client.get(
        "/api/internal/docqa/health?probe_llm=false",
        headers={"X-Go-Orchestrator": "graphinsight-go", "X-Trace-Id": "trace-internal-docqa"},
    )
    _assert(allowed.status_code == 200, f"expected 200 with Go orchestrator header, got {allowed.status_code}")
    body = allowed.json()
    _assert(isinstance(body, dict), f"expected dict body, got {type(body)}")
    _assert(body.get("trace_id") == "trace-internal-docqa", f"expected forwarded trace_id, got {body.get('trace_id')}")
    data = body.get("data") or {}
    _assert(isinstance(data, dict), f"expected dict data, got {type(data)}")
    _assert("status" in data, f"expected status in data, got {data}")

    diag_denied = client.post(
        "/api/internal/docqa/retrieval-diagnostics",
        json={"question": "hybrid search"},
        headers={"X-Go-Orchestrator": "graphinsight-go", "X-Trace-Id": "trace-orchestrator-only"},
    )
    _assert(diag_denied.status_code == 403, f"expected 403 for orchestrator-only diagnostics, got {diag_denied.status_code}")

    # 缺少 kb 作用域 -> KB_SCOPE_REQUIRED(400)：契约 §2.4 作用域强制点，
    # 即使带合法控制面头，Python 数据面也要求显式 kb 作用域。
    diag_missing_scope = client.post(
        "/api/internal/docqa/retrieval-diagnostics",
        json={"question": "hybrid search", "top_k": 3, "modes": ["keyword"]},
        headers={"X-Go-Proxy": "graphinsight-go"},
    )
    _assert(diag_missing_scope.status_code == 400, f"expected 400 for scopeless diagnostics, got {diag_missing_scope.status_code}")

    with patch(
        "api.routes.doc_qa_internal.retrieval_orchestrator.diagnose",
        return_value={"query": "hybrid search", "runs": {"keyword": {"items": []}}},
    ) as diagnose:
        diag_allowed = client.post(
            "/api/internal/docqa/retrieval-diagnostics",
            json={"question": "hybrid search", "top_k": 3, "modes": ["keyword"], "kb_id": "kb-1"},
            headers={"X-Go-Proxy": "graphinsight-go"},
        )
    _assert(diag_allowed.status_code == 200, f"expected 200 for control-plane diagnostics, got {diag_allowed.status_code}")
    diagnose.assert_called_once()
    diag_body = diag_allowed.json()
    _assert((diag_body.get("data") or {}).get("query") == "hybrid search", f"unexpected diagnostics body: {diag_body}")
    print("DOCQA_INTERNAL_ROUTE_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
