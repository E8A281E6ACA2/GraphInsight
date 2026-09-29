#!/usr/bin/env python3
"""Verify admin.database does not clobber the unified runtime env override."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    # M4-R1 审计前置文件处理：不再依赖本机 logs/dev/backend.env（含真实运行态凭据），
    # 也不为通过守卫而提交敏感配置。改用自包含的临时 fixture 环境文件，
    # 本地/CI 无外部依赖即可验证“admin.database 导入不覆写统一运行态 env 覆盖”。
    fixture_neo4j_uri = "bolt://localhost:7687"
    with tempfile.TemporaryDirectory(prefix="gi-admin-env-override-") as tmpdir:
        env_file = Path(tmpdir) / "backend.env"
        env_file.write_text(
            f'NEO4J_URI="{fixture_neo4j_uri}"\n'
            'NEO4J_USER="neo4j"\n'
            'NEO4J_PASSWORD="fixture-not-a-secret"\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(env_file)
        script = (
            "import os, sys; "
            f"sys.path.insert(0, {str(BACKEND_ROOT)!r}); "
            "import config; "
            "before = os.getenv('NEO4J_URI'); "
            "import admin.database; "
            "after = os.getenv('NEO4J_URI'); "
            "print(before); "
            "print(after)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(BACKEND_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    _assert(len(lines) >= 2, f"unexpected output: {result.stdout!r}")
    before, after = lines[-2], lines[-1]
    _assert(before == fixture_neo4j_uri, f"unexpected pre-admin NEO4J_URI: {before}")
    _assert(after == before, f"admin.database should not clobber env override: before={before} after={after}")
    print("ADMIN_ENV_OVERRIDE_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
