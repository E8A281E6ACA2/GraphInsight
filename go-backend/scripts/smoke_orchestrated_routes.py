#!/usr/bin/env python3
"""Smoke test for Go orchestrated routes.

Default mode is read-only (no graph build, no file upload).

Examples:
  python scripts/smoke_orchestrated_routes.py \
    --go-base-url http://127.0.0.1:8081 \
    --admin-email yh@qs.al \
    --admin-password '***' \
    --require-orchestrator-connected

  python scripts/smoke_orchestrated_routes.py \
    --go-base-url http://127.0.0.1:8081 \
    --token <admin_jwt> \
    --with-build \
    --with-upload
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class StepResult:
    name: str
    ok: bool
    status: int
    detail: str


def _json_or_raw(text: str) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def _request_with_headers(
    *,
    base_url: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    token: str = "",
    timeout: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[int, Any, Dict[str, str]]:
    url = base_url.rstrip("/") + path
    body = None
    req_headers: Dict[str, str] = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if token.strip():
        req_headers["Authorization"] = f"Bearer {token.strip()}"
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url=url, data=body, method=method.upper(), headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, _json_or_raw(text), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, _json_or_raw(text), {k.lower(): v for k, v in exc.headers.items()}


def _request(
    *,
    base_url: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    token: str = "",
    timeout: float = 20.0,
    headers: Optional[Dict[str, str]] = None,
) -> tuple[int, Any]:
    status, body, _ = _request_with_headers(
        base_url=base_url,
        method=method,
        path=path,
        payload=payload,
        token=token,
        timeout=timeout,
        headers=headers,
    )
    return status, body


def _login(base_url: str, username: str, password: str, timeout: float) -> str:
    status, body = _request(
        base_url=base_url,
        method="POST",
        path="/api/v1/admin/auth/login",
        payload={"username": username, "password": password},
        timeout=timeout,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"LOGIN_FAIL status={status} body={_as_json(body)}")
    data = body.get("data")
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"LOGIN_NO_TOKEN body={_as_json(body)}")
    return str(token)


def _request_upload(
    *,
    base_url: str,
    token: str,
    timeout: float,
    filename: str,
    content: bytes,
    kb_id: str = "",
) -> tuple[int, Any, Dict[str, str]]:
    boundary = "----GraphInsightSmoke" + uuid.uuid4().hex
    url = base_url.rstrip("/") + "/api/documents/upload"

    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode())
    body.extend(b"Content-Type: text/plain\r\n\r\n")
    body.extend(content)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    headers = {
        "Accept": "application/json",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    if kb_id.strip():
        headers["x-kb-id"] = kb_id.strip()

    req = urllib.request.Request(url=url, data=bytes(body), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, _json_or_raw(text), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, _json_or_raw(text), {k.lower(): v for k, v in exc.headers.items()}


def _extract_doc_id_from_upload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    uploaded = data.get("uploaded")
    if not isinstance(uploaded, list) or not uploaded:
        return ""
    first = uploaded[0]
    if not isinstance(first, dict):
        return ""
    value = first.get("doc_id") or first.get("id")
    return str(value or "").strip()


def _envelope_code(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("code")
    return None


def _route_owner(headers: Dict[str, str]) -> str:
    return headers.get("x-graphinsight-route-owner", "")


def _as_json(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _discover_active_kb(base_url: str, token: str, timeout: float) -> str:
    """返回调用账号可见的第一个 active kb_id；与 backend/tests/kb_scope.py 口径一致。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/knowledge-bases",
        method="GET",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError):
        return ""
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    for item in items or []:
        if isinstance(item, dict) and str(item.get("status")) == "active" and item.get("kb_id"):
            return str(item["kb_id"])
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for Go orchestrated routes")
    parser.add_argument("--go-base-url", default=os.getenv("GO_BASE_URL", os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081")))
    parser.add_argument("--token", default=os.getenv("ADMIN_TOKEN", ""))
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL", "yh@qs.al"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD", ""))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--require-orchestrator-connected", action="store_true")
    parser.add_argument("--with-build", action="store_true")
    parser.add_argument("--with-upload", action="store_true")
    args = parser.parse_args()

    token = args.token.strip()
    if not token and args.admin_password.strip():
        try:
            token = _login(args.go_base_url, args.admin_email, args.admin_password, args.timeout)
        except Exception as exc:
            print(f"LOGIN_INIT_FAIL {exc}")
            return 1

    kb_id = _discover_active_kb(args.go_base_url, token, args.timeout) if token else ""
    if not kb_id:
        print(
            "KB_SCOPE_BLOCKED script=smoke_orchestrated_routes.py reason=no_visible_active_kb "
            "hint=run backend/seed_e2e_local_stack.py"
        )
        return 1
    print(f"KB_SCOPE_READY script=smoke_orchestrated_routes.py kb_id={kb_id} source=discover")

    results: list[StepResult] = []

    # 1) Health
    status, health, health_headers = _request_with_headers(
        base_url=args.go_base_url,
        method="GET",
        path="/health",
        token=token,
        timeout=args.timeout,
    )
    if status != 200:
        results.append(StepResult("health", False, status, f"unexpected status, payload={_as_json(health)}"))
    else:
        orch = (health or {}).get("data", {}).get("orchestrator", {}) if isinstance(health, dict) else {}
        connected = bool(orch.get("connected"))
        owner = _route_owner(health_headers)
        if owner != "go-native":
            results.append(StepResult("health", False, status, f"route_owner={owner or '<missing>'}, expected=go-native"))
        elif args.require_orchestrator_connected and not connected:
            results.append(StepResult("health", False, status, f"orchestrator not connected: {_as_json(orch)}"))
        else:
            results.append(StepResult("health", True, status, f"route_owner={owner}, orchestrator={_as_json(orch)}"))

    # 2) Documents list (go-native read path)
    status, docs, docs_headers = _request_with_headers(
        base_url=args.go_base_url,
        method="GET",
        path="/api/documents",
        token=token,
        timeout=args.timeout,
        headers={"x-kb-id": kb_id},
    )
    docs_owner = _route_owner(docs_headers)
    ok_docs = status == 200 and isinstance(docs, dict) and docs_owner == "go-native"
    detail_docs = f"route_owner={docs_owner or '<missing>'}, envelope_code={_envelope_code(docs)}"
    results.append(StepResult("documents.list", ok_docs, status, detail_docs))

    # 3) DocQA health (orchestrated)
    status, qa_health, qa_headers = _request_with_headers(
        base_url=args.go_base_url,
        method="GET",
        path="/api/docqa/health?probe_llm=false",
        token=token,
        timeout=args.timeout,
    )
    qa_owner = _route_owner(qa_headers)
    qa_data = qa_health.get("data") if isinstance(qa_health, dict) else None
    qa_status = qa_data.get("status") if isinstance(qa_data, dict) else None
    ok_qa = status == 200 and isinstance(qa_health, dict) and qa_owner == "go-orchestrator" and qa_status in {"healthy", "degraded", "unhealthy"}
    detail_qa = f"route_owner={qa_owner or '<missing>'}, envelope_code={_envelope_code(qa_health)}, status={qa_status or '<missing>'}"
    results.append(StepResult("docqa.health", ok_qa, status, detail_qa))

    # 4) NL2Cypher read-only contract checks (go-native)
    status, nl_examples, nl_examples_headers = _request_with_headers(
        base_url=args.go_base_url,
        method="GET",
        path="/api/nl2cypher/examples",
        token=token,
        timeout=args.timeout,
    )
    nl_examples_owner = _route_owner(nl_examples_headers)
    ok_nl_examples = status == 200 and isinstance(nl_examples, dict) and nl_examples_owner == "go-native"
    detail_nl_examples = f"route_owner={nl_examples_owner or '<missing>'}, success={nl_examples.get('success') if isinstance(nl_examples, dict) else None}"
    results.append(StepResult("nl2cypher.examples", ok_nl_examples, status, detail_nl_examples))

    status, nl_status, nl_status_headers = _request_with_headers(
        base_url=args.go_base_url,
        method="GET",
        path="/api/nl2cypher/status",
        token=token,
        timeout=args.timeout,
    )
    nl_status_owner = _route_owner(nl_status_headers)
    ok_nl_status = status == 200 and isinstance(nl_status, dict) and nl_status_owner == "go-native"
    detail_nl_status = f"route_owner={nl_status_owner or '<missing>'}, enabled={nl_status.get('enabled') if isinstance(nl_status, dict) else None}"
    results.append(StepResult("nl2cypher.status", ok_nl_status, status, detail_nl_status))

    # 5) Optional graph build trigger (go-native job submission)
    if args.with_build:
        status, build, build_headers = _request_with_headers(
            base_url=args.go_base_url,
            method="POST",
            path="/api/graph/build",
            payload={"source": "documents", "force": False, "note": "smoke"},
            token=token,
            timeout=max(args.timeout, 60.0),
        )
        build_owner = _route_owner(build_headers)
        ok_build = status == 200 and isinstance(build, dict) and build_owner == "go-native"
        detail_build = f"route_owner={build_owner or '<missing>'}, envelope_code={_envelope_code(build)}"
        results.append(StepResult("graph.build", ok_build, status, detail_build))

    # 6) Optional upload (go-native write path)
    if args.with_upload:
        status, upload, upload_headers = _request_upload(
            base_url=args.go_base_url,
            token=token,
            timeout=args.timeout,
            filename="smoke_upload.txt",
            content=b"GraphInsight smoke upload test\n",
            kb_id=kb_id,
        )
        upload_owner = _route_owner(upload_headers)
        ok_upload = status == 200 and isinstance(upload, dict) and upload_owner == "go-native"
        detail_upload = f"route_owner={upload_owner or '<missing>'}, envelope_code={_envelope_code(upload)}"
        results.append(StepResult("documents.upload", ok_upload, status, detail_upload))

        upload_doc_id = _extract_doc_id_from_upload(upload)
        if upload_doc_id:
            delete_status, delete_body, delete_headers = _request_with_headers(
                base_url=args.go_base_url,
                method="DELETE",
                path=f"/api/documents/{upload_doc_id}?purge_graph=false&soft_delete=false&dry_run=false&verify_after=false",
                token=token,
                timeout=args.timeout,
                headers={"x-kb-id": kb_id},
            )
            delete_owner = _route_owner(delete_headers)
            ok_delete = delete_status == 200 and isinstance(delete_body, dict) and delete_owner == "go-native"
            detail_delete = f"route_owner={delete_owner or '<missing>'}, envelope_code={_envelope_code(delete_body)}"
            results.append(StepResult("documents.delete", ok_delete, delete_status, detail_delete))

    print("Smoke results:")
    for item in results:
        icon = "PASS" if item.ok else "FAIL"
        print(f"- [{icon}] {item.name}: status={item.status} | {item.detail}")

    failed = [item for item in results if not item.ok]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
