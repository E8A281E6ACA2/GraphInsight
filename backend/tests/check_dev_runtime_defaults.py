#!/usr/bin/env python3
"""Validate the unified dev runtime defaults produced by scripts/dev-backend.sh."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from runtime_env import RUNTIME_ENV_FILE, resolve_base_url


REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_BACKEND_ENV_FILE = REPO_ROOT / "logs" / "dev" / "backend.env"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _dsn_target(url: str) -> str:
    """丢掉 userinfo，只留 host:port/db，供失败信息使用。"""
    if not url:
        return "<empty>"
    return re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@]*@", "", url)


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise AssertionError(f"expected env file to exist: {path}")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            data[key] = value
    return data


def _request_json(url: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def main() -> int:
    runtime_env = _read_env_file(RUNTIME_ENV_FILE)
    backend_env = _read_env_file(DEV_BACKEND_ENV_FILE)

    # runtime.env 会被 CI 步骤打印、也会被人贴进排障记录：只允许承载 *_BASE_URL，
    # 任何口令、DSN、token 落进这个文件都算回归。放在最前面，避免后续断言先打印其内容。
    non_url_keys = sorted(key for key in runtime_env if not key.endswith("_BASE_URL"))
    _assert(not non_url_keys, f"runtime.env must only carry *_BASE_URL keys, found: {non_url_keys}")

    python_base_url = resolve_base_url("PYTHON_BASE_URL", "http://127.0.0.1:8001")
    go_base_url = resolve_base_url("GO_BASE_URL", "http://127.0.0.1:8081")
    admin_base_url = resolve_base_url("ADMIN_BASE_URL", go_base_url)

    _assert(runtime_env.get("PYTHON_BASE_URL") == python_base_url, f"unexpected PYTHON_BASE_URL: {runtime_env}")
    _assert(runtime_env.get("GO_BASE_URL") == go_base_url, f"unexpected GO_BASE_URL: {runtime_env}")
    _assert(runtime_env.get("ADMIN_BASE_URL") == admin_base_url, f"unexpected ADMIN_BASE_URL: {runtime_env}")

    expected_backend_env = {
        "RBAC_AUTHZ_MODE": "go_db",
        "NEO4J_CONFIG_SOURCE": "auto",
    }
    for key, expected in expected_backend_env.items():
        actual = backend_env.get(key)
        _assert(actual == expected, f"unexpected backend env {key}: expected={expected} actual={actual}")
    _assert(
        "PUBLIC_BUSINESS_ROUTES_ENABLED" not in backend_env,
        f"PUBLIC_BUSINESS_ROUTES_ENABLED should not be written after removing Python business public compat: keys={sorted(backend_env)}",
    )
    _assert(
        "PUBLIC_ADMIN_ROUTES_ENABLED" not in backend_env,
        f"PUBLIC_ADMIN_ROUTES_ENABLED should not be written after removing Python admin public compat: keys={sorted(backend_env)}",
    )
    admin_database_url = backend_env.get("ADMIN_DATABASE_URL", "").strip()
    _assert(admin_database_url, "expected ADMIN_DATABASE_URL to be written into unified backend env")
    _assert(
        admin_database_url == "postgresql://graphinsight:graphinsight-dev-password@127.0.0.1:5434/graphinsight_admin",
        f"unified backend env should use local docker admin database URL, got target: {_dsn_target(admin_database_url)}",
    )

    python_port = python_base_url.rsplit(":", 1)[-1]
    _assert(backend_env.get("API_PORT") == python_port, f"backend env API_PORT mismatch: {backend_env.get('API_PORT')} != {python_port}")

    python_status, python_body = _request_json(f"{python_base_url}/health")
    _assert(python_status == 200, f"expected Python health status=200, got status={python_status}, body={python_body}")
    _assert(isinstance(python_body, dict), f"expected Python health JSON, got {type(python_body)}")
    python_data = python_body.get("data")
    _assert(isinstance(python_data, dict), f"expected Python health data object, got {python_data}")
    _assert("build_tag" in python_data, f"expected Python health build_tag, got {python_data}")

    go_status, go_body = _request_json(f"{go_base_url}/health")
    _assert(go_status == 200, f"expected Go health status=200, got status={go_status}, body={go_body}")
    _assert(isinstance(go_body, dict), f"expected Go health JSON, got {type(go_body)}")
    go_data = go_body.get("data")
    _assert(isinstance(go_data, dict), f"expected Go health data object, got {go_data}")
    python_backend = go_data.get("python_backend")
    _assert(isinstance(python_backend, dict), f"expected Go python_backend object, got {python_backend}")
    _assert(python_backend.get("base_url") == python_base_url, f"unexpected Go python backend base url: {python_backend}")
    authz = go_data.get("authz")
    _assert(isinstance(authz, dict), f"expected Go authz object, got {authz}")
    _assert(authz.get("mode") == "go_db", f"expected go_db authz mode, got {authz}")
    _assert(authz.get("permission_check_via_upstream") is False, f"expected no Python upstream authz in unified mode, got {authz}")
    orchestrator = go_data.get("orchestrator")
    _assert(isinstance(orchestrator, dict), f"expected Go orchestrator object, got {orchestrator}")
    _assert(orchestrator.get("base_url") == python_base_url, f"unexpected orchestrator base url: {orchestrator}")

    print("DEV_RUNTIME_DEFAULTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
