"""
Jobs API smoke check.

Usage:
    python backend/tests/check_jobs_api.py
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from kb_scope import require_active_kb


def _request(
    method: str,
    url: str,
    payload: dict | None = None,
    token: str | None = None,
    kb_id: str | None = None,
) -> tuple[int, dict | str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if kb_id:
        headers["x-kb-id"] = kb_id
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _data(body: dict | str) -> dict | list | None:
    if isinstance(body, dict):
        return body.get("data")
    return None


def _get_job(base: str, token: str, kb_id: str, job_id: int) -> tuple[int, dict | str]:
    return _request("GET", f"{base}/api/v1/admin/jobs/{job_id}", token=token, kb_id=kb_id)


def _wait_terminal(
    base: str, token: str, kb_id: str, job_id: int, *, timeout_seconds: float = 30.0
) -> dict | None:
    deadline = time.time() + timeout_seconds
    last: dict | None = None
    while time.time() < deadline:
        status, body = _get_job(base, token, kb_id, job_id)
        if status != 200:
            print(f"GET_JOB_FAIL status={status} body={body}")
            return None
        data = _data(body)
        if not isinstance(data, dict):
            print(f"GET_JOB_INVALID body={body}")
            return None
        last = data
        state = str(data.get("status") or "")
        if state in {"succeeded", "failed", "cancelled"}:
            return data
        time.sleep(0.5)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="Jobs API smoke check")
    parser.add_argument("--base-url", default=os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN"))
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL", "yh@qs.al"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD"))
    args = parser.parse_args()

    base = str(args.base_url).rstrip("/")
    token = args.admin_token.strip() if args.admin_token else ""
    if not token:
        if not args.admin_password:
            print("MISSING_ADMIN_PASSWORD")
            return 1
        status, body = _request(
            "POST",
            f"{base}/api/v1/admin/auth/login",
            {"username": args.admin_email, "password": args.admin_password},
        )
        if status != 200:
            print(f"LOGIN_FAIL status={status} body={body}")
            return 1
        token = (_data(body) or {}).get("token") if isinstance(_data(body), dict) else None
        if not token:
            print(f"LOGIN_NO_TOKEN body={body}")
            return 1

    kb_id = require_active_kb("check_jobs_api.py", base, token)

    create_payload = {
        "tenant_id": "t-demo",
        "project_id": "p-demo",
        "kb_id": kb_id,
        "payload": {"source": "smoke"},
        "max_retries": 0,
    }
    c_status, c_body = _request(
        "POST", f"{base}/api/v1/admin/jobs/build-graph", create_payload, token, kb_id
    )
    print(f"CREATE_JOB status={c_status}")
    if c_status not in {200, 201}:
        print(c_body)
        return 1
    c_data = _data(c_body)
    if not isinstance(c_data, dict) or not c_data.get("id"):
        print(f"CREATE_JOB_INVALID body={c_body}")
        return 1
    job_id = int(c_data["id"])

    l_status, l_body = _request(
        "GET", f"{base}/api/v1/admin/jobs?page=1&page_size=10", token=token, kb_id=kb_id
    )
    print(f"LIST_JOB status={l_status}")
    if l_status != 200:
        print(l_body)
        return 1

    g_status, g_body = _request(
        "GET", f"{base}/api/v1/admin/jobs/{job_id}", token=token, kb_id=kb_id
    )
    print(f"GET_JOB status={g_status}")
    if g_status != 200:
        print(g_body)
        return 1

    x_status, x_body = _request(
        "POST", f"{base}/api/v1/admin/jobs/{job_id}:cancel", token=token, kb_id=kb_id
    )
    print(f"CANCEL_JOB status={x_status}")
    terminal = None
    if x_status != 200:
        current_status, current_body = _get_job(base, token, kb_id, job_id)
        current = _data(current_body)
        current_state = current.get("status") if isinstance(current, dict) else None
        if x_status == 400 and current_state in {"succeeded", "failed", "cancelled"}:
            print(f"CANCEL_SKIPPED_ALREADY_TERMINAL state={current_state}")
            terminal = current
        else:
            print(x_body)
            return 1
    else:
        terminal = _data(x_body) if isinstance(_data(x_body), dict) else None

    if not isinstance(terminal, dict) or str(terminal.get("status") or "") not in {"succeeded", "failed", "cancelled"}:
        terminal = _wait_terminal(base, token, kb_id, job_id)
    if not isinstance(terminal, dict):
        print("JOB_TERMINAL_MISSING")
        return 1
    # build_graph 按契约要求显式 doc_ids，本夹具不带文档，因此"到达终态即通过"，
    # 但把终态与错误原因打出来，避免把契约拒绝悄悄当成建图成功。
    print(f"JOB_TERMINAL state={terminal.get('status')} error={terminal.get('error_message')}")

    r_status, r_body = _request(
        "POST", f"{base}/api/v1/admin/jobs/{job_id}:retry", token=token, kb_id=kb_id
    )
    print(f"RETRY_JOB status={r_status}")
    terminal_state = str(terminal.get("status") or "")
    retry_count = int(terminal.get("retry_count") or 0)
    max_retries = int(terminal.get("max_retries") or 0)
    if terminal_state in {"failed", "cancelled"} and retry_count < max_retries:
        if r_status != 200:
            print(r_body)
            return 1
    elif r_status != 400:
        print(f"RETRY_EXPECTED_REJECT terminal_state={terminal_state} retry_count={retry_count} max_retries={max_retries}")
        print(r_body)
        return 1

    print("JOBS_API_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
