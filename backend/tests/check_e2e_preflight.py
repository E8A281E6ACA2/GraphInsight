#!/usr/bin/env python
"""Release acceptance preflight: prove the test account really authenticates and
really sees at least one active knowledge base, before any acceptance step runs.

Credentials come from the environment only (never argv, so they stay out of `ps`
output and CI command logs). Nothing here prints a password, token or hash.

Usage:
    ADMIN_BASE_URL=http://127.0.0.1:18082 ADMIN_EMAIL=... ADMIN_PASSWORD=*** \
        python backend/tests/check_e2e_preflight.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(url: str, payload: dict | None, token: str = "") -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw_length": len(raw)}
    except urllib.error.URLError as exc:
        raise SystemExit(f"PREFLIGHT_FAIL reason=unreachable url={url} error={exc.reason}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("ADMIN_BASE_URL") or os.getenv("GO_BASE_URL") or "http://127.0.0.1:8081")
    parser.add_argument("--email", default=os.getenv("ADMIN_EMAIL", ""))
    parser.add_argument("--min-active-kbs", type=int, default=1)
    args = parser.parse_args()

    password = os.getenv("ADMIN_PASSWORD", "")
    token = os.getenv("ADMIN_TOKEN", "")
    base = args.base_url.rstrip("/")

    if not token and not (args.email and password):
        print("PREFLIGHT_FAIL reason=no_credentials hint=set ADMIN_TOKEN or ADMIN_EMAIL+ADMIN_PASSWORD")
        return 1

    source = "token_env"
    data = {}
    if not token:
        status, payload = _request(f"{base}/api/v1/admin/auth/login", {"username": args.email, "password": password})
        data = payload.get("data") or {}
        token = (data.get("token") or "").strip()
        if status != 200 or payload.get("code") != 200 or not token:
            print(
                "PREFLIGHT_FAIL reason=login_rejected http=%s code=%s error_code=%s"
                % (status, payload.get("code"), data.get("error_code") or "n/a")
            )
            return 1
        source = "password_login"

    status, payload = _request(f"{base}/api/knowledge-bases", None, token=token)
    if status != 200 or payload.get("code") != 200:
        print("PREFLIGHT_FAIL reason=kb_directory_rejected http=%s code=%s" % (status, payload.get("code")))
        return 1

    items = ((payload.get("data") or {}).get("items")) or []
    active = [item for item in items if item.get("status") == "active"]
    if len(active) < args.min_active_kbs:
        print(
            "PREFLIGHT_FAIL reason=no_visible_active_kb visible=%s required=%s"
            % (len(active), args.min_active_kbs)
        )
        return 1

    print("PREFLIGHT_OK auth_verified=1 login_source=%s active_kb_visible=%d sample_kb_id=%s" % (
        source, len(active), active[0].get("kb_id", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
