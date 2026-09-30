#!/usr/bin/env python3
"""发布产物与日志的敏感信息扫描门禁。

用途：在任何文件被 `actions/upload-artifact` 带走、或任何日志被写进 Actions 输出之前，
用「本次运行真实注入的凭据字面值」+「通用凭据形状」两层规则扫一遍，命中即失败。

两层规则的差别是刻意的：
  * 字面值（--secret-env-var 指定的环境变量值）永远不可放行，因为它们是本次运行的一次性
    凭据或 GitHub Secret，出现在产物里就是泄露；
  * 形状（JWT / 带口令的 DSN / password= 赋值 / bcrypt 哈希）会误报仓库里公开声明过的
    fixture 口令，所以允许显式 --allow-fixture 放行，但放行清单会打进日志，便于复核。

扫描结果只打印匹配位置的哈希与脱敏片段，绝不回显命中的凭据本身。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Iterable

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("dsn_with_credentials", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]{1,64}:[^\s/:@]{3,128}@[0-9a-zA-Z.-]+")),
    (
        "credential_assignment",
        # 值字符集排除 ; { }：minified JS 里 o.password=null;const 之类的结构赋值
        # 会把 JS 语法片段当成凭据值误报；真实凭据通常带引号，仍能被捕获。
        re.compile(
            r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|authorization)\b\s*[:=]\s*[\"']?([^\s\"',;{}]{6,})"
        ),
    ),
    ("bcrypt_hash", re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")),
)

# 键名本身不足以判定泄露；只有赋值右侧是非占位值才算命中，占位值在此排除。
PLACEHOLDER_VALUES = {
    "changeme",
    "change-this-password",
    "redacted",
    "placeholder",
    "graphinsight-dev-password",
    "ci-internal-token",
    "***",
}


def _walk(paths: Iterable[Path]) -> Iterable[Path]:
    for root in paths:
        if root.is_file():
            yield root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                yield path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _redact(match: str) -> str:
    if len(match) <= 12:
        return f"{match[:2]}***"
    return f"{match[:6]}...{match[-3:]}({len(match)} chars)"


def scan(
    files: Iterable[Path],
    literals: list[str],
    allowed: set[str],
) -> tuple[int, int, list[str]]:
    findings: list[str] = []
    scanned_files = 0
    scanned_bytes = 0
    for path in files:
        scanned_files += 1
        try:
            raw = path.read_bytes()
        except OSError as exc:
            findings.append(f"SECRET_FINDING kind=unreadable file={path} detail={type(exc).__name__}")
            continue
        scanned_bytes += len(raw)
        text = raw.decode("utf-8", "replace")
        for value in literals:
            if value and value in text:
                findings.append(
                    f"SECRET_FINDING kind=run_credential file={path} match_sha256={_sha(value)} "
                    f"length={len(value)} occurrences={text.count(value)}"
                )
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(text):
                token = match.group(0)
                captured = match.group(2) if kind == "credential_assignment" and match.groups() and match.group(2) else token
                if captured.strip() in allowed or captured.strip() in PLACEHOLDER_VALUES:
                    continue
                if kind == "dsn_with_credentials" and any(fixture in token for fixture in allowed):
                    continue
                findings.append(
                    f"SECRET_FINDING kind={kind} file={path} match_sha256={_sha(token)} value={_redact(captured)}"
                )
    return scanned_files, scanned_bytes, findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan artifacts and logs before they are uploaded")
    parser.add_argument("--path", action="append", default=[], help="File or directory to scan. Repeatable.")
    parser.add_argument(
        "--secret-env-var",
        action="append",
        default=[],
        help="Name of an environment variable whose literal value must not appear. Repeatable.",
    )
    parser.add_argument(
        "--allow-fixture",
        action="append",
        default=[],
        help="Declared non-secret fixture value that shape patterns may match. Repeatable.",
    )
    parser.add_argument("--skip-missing", action="store_true", help="Do not fail when no path exists.")
    args = parser.parse_args()

    if not args.path:
        print("SECRET_SCAN_PREREQ_MISSING no --path given")
        return 2
    roots = [Path(item) for item in args.path]
    existing = [item for item in roots if item.exists()]
    if not existing:
        if args.skip_missing:
            print(f"SECRET_SCAN_SKIPPED reason=no_path_found paths={len(roots)}")
            return 0
        print(f"SECRET_SCAN_PREREQ_MISSING none of the {len(roots)} paths exist")
        return 2
    missing = [str(item) for item in roots if not item.exists()]
    if missing:
        print(f"SECRET_SCAN_NOTE missing_paths={missing}")

    literals: list[str] = []
    provided: list[str] = []
    for name in args.secret_env_var:
        value = os.getenv(name, "")
        if value:
            literals.append(value.strip())
            provided.append(name)
        else:
            print(f"SECRET_SCAN_NOTE env_var_unset name={name}")
    allowed = {item.strip() for item in args.allow_fixture if item.strip()}

    files, total_bytes, findings = scan(_walk(existing), literals, allowed)
    for line in findings[:40]:
        print(line)
    if len(findings) > 40:
        print(f"SECRET_FINDINGS_TRUNCATED hidden={len(findings) - 40}")
    print(
        "SECRET_SCAN_SUMMARY "
        f"paths={len(existing)} files={files} bytes={total_bytes} "
        f"credential_env_vars={len(provided)} allowed_fixtures={len(allowed)} "
        f"findings={len(findings)} result={'pass' if not findings else 'fail'}"
    )
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
