"""
任务中心 + 可观测性联调检查

验证项：
1. reindex 任务可自动执行到终态
2. /api/v1/admin/jobs/{job_id}/logs 可返回任务链路日志
3. /api/v1/admin/monitor/performance|slo|alerts/check 可用
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from kb_scope import require_active_kb


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    kb_id: str | None = None,
    payload: dict | None = None,
) -> tuple[int, dict | str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if kb_id:
        headers["x-kb-id"] = kb_id
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _extract_data(body: dict | str) -> dict | list | None:
    if isinstance(body, dict):
        return body.get("data")
    return None


def main() -> int:
    base = os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
    username = os.getenv("ADMIN_EMAIL", "yh@qs.al")
    password = os.getenv("ADMIN_PASSWORD")
    token = os.getenv("ADMIN_TOKEN")

    if not token:
        if not password:
            print("MISSING_ADMIN_PASSWORD")
            return 1
        s, b = _request(
            "POST",
            f"{base}/api/v1/admin/auth/login",
            payload={"username": username, "password": password},
        )
        if s != 200:
            print(f"LOGIN_FAIL status={s} body={b}")
            return 1
        token = (_extract_data(b) or {}).get("token") if isinstance(_extract_data(b), dict) else None
        if not token:
            print(f"LOGIN_NO_TOKEN body={b}")
            return 1

    kb_id = require_active_kb("check_job_reindex_and_observability.py", base, token)

    # reindex 创建本身允许 kb 可选，但任务的读取（detail/logs）强制 kb 作用域且要求任务归属
    # 该 kb（jobBelongsToKB），所以创建时显式带 kb_id，否则作用域读取会 404。
    create_payload = {
        "tenant_id": "t-slo",
        "project_id": "p-slo",
        "kb_id": kb_id,
        "payload": {"index_name": "chunkText"},
    }
    s, b = _request("POST", f"{base}/api/v1/admin/jobs/reindex", token=token, kb_id=kb_id, payload=create_payload)
    if s not in {200, 201}:
        print(f"REINDEX_CREATE_FAIL status={s} body={b}")
        return 1
    data = _extract_data(b)
    if not isinstance(data, dict) or not data.get("id"):
        print(f"REINDEX_CREATE_INVALID body={b}")
        return 1
    job_id = int(data["id"])
    print(f"REINDEX_JOB_CREATED id={job_id}")

    deadline = time.time() + 30
    final_status = ""
    while time.time() < deadline:
        s, b = _request("GET", f"{base}/api/v1/admin/jobs/{job_id}", token=token, kb_id=kb_id)
        if s != 200:
            print(f"REINDEX_GET_FAIL status={s} body={b}")
            return 1
        data = _extract_data(b)
        final_status = str(data.get("status")) if isinstance(data, dict) else ""
        if final_status in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(1.0)
    print(f"REINDEX_JOB_STATUS status={final_status}")
    if final_status not in {"succeeded", "failed", "cancelled"}:
        print("REINDEX_JOB_TIMEOUT")
        return 1

    qs = urllib.parse.urlencode({"page": 1, "page_size": 100})
    s, b = _request("GET", f"{base}/api/v1/admin/jobs/{job_id}/logs?{qs}", token=token, kb_id=kb_id)
    if s != 200:
        print(f"JOB_LOGS_FAIL status={s} body={b}")
        return 1
    logs_data = _extract_data(b)
    items = logs_data.get("items", []) if isinstance(logs_data, dict) else []
    print(f"JOB_LOGS_COUNT count={len(items)}")
    if len(items) == 0:
        print("JOB_LOGS_EMPTY")
        return 1

    s, b = _request("GET", f"{base}/api/v1/admin/monitor/performance?window_seconds=900", token=token)
    if s != 200:
        print(f"PERF_FAIL status={s} body={b}")
        return 1
    perf = _extract_data(b)
    print(f"PERF_OK total_requests={perf.get('total_requests') if isinstance(perf, dict) else None}")

    s, b = _request("GET", f"{base}/api/v1/admin/monitor/slo?api_window_seconds=900&job_window_minutes=60", token=token)
    if s != 200:
        print(f"SLO_FAIL status={s} body={b}")
        return 1
    slo = _extract_data(b)
    print(f"SLO_OK keys={list(slo.keys()) if isinstance(slo, dict) else None}")

    s, b = _request(
        "POST",
        f"{base}/api/v1/admin/monitor/alerts/check?send_webhook=false&api_window_seconds=900&job_window_minutes=60",
        token=token,
    )
    if s != 200:
        print(f"ALERT_FAIL status={s} body={b}")
        return 1
    alerts = _extract_data(b)
    print(f"ALERT_OK count={alerts.get('alert_count') if isinstance(alerts, dict) else None}")

    print("JOB_REINDEX_OBSERVABILITY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
