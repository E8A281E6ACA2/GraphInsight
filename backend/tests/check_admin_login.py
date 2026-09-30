"""
Check admin login API quickly.

The password is never taken from argv (it would show up in `ps` and CI command
logs) and the response body is never printed (it carries the admin JWT).

Usage:
    ADMIN_BASE_URL=http://127.0.0.1:18082 python backend/tests/check_admin_login.py \
        --username <admin-email> --password-stdin < /path/to/password.file
    ADMIN_PASSWORD=*** python backend/tests/check_admin_login.py --username <admin-email>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=os.getenv("ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of argv or ADMIN_PASSWORD",
    )
    args = parser.parse_args()

    password = sys.stdin.readline().strip() if args.password_stdin else args.password
    if not password:
        print("missing password: use --password-stdin or ADMIN_PASSWORD")
        return 2

    body = json.dumps({"username": args.username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/api/v1/admin/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            payload = {}
        print(
            "STATUS=%s code=%s error_code=%s"
            % (
                exc.code,
                payload.get("code"),
                ((payload.get("data") or {}).get("error_code") or "n/a"),
            )
        )
        return 1
    except urllib.error.URLError as exc:
        print(f"STATUS=unreachable error={exc.reason}")
        return 1

    print(f"STATUS=200 code={payload.get('code')} token_present={bool((payload.get('data') or {}).get('token'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
