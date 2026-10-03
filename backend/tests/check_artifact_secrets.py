#!/usr/bin/env python3
"""发布产物与日志的敏感信息扫描门禁。

用途：在任何文件被 `actions/upload-artifact` 带走、或任何日志被写进 Actions 输出之前，
用「本次运行真实注入的凭据字面值」+「通用凭据形状」两层规则扫一遍，命中即失败。

两层规则的差别是刻意的：
  * 字面值（--secret-env-var 指定的环境变量值）永远不可放行，因为它们是本次运行的一次性
    凭据或 GitHub Secret，出现在产物里就是泄露；
  * 形状（JWT / 带口令的 DSN / password= 赋值 / bcrypt 哈希）会误报仓库里公开声明过的
    fixture 口令，所以允许显式 --allow-fixture 放行，但放行清单会打进日志，便于复核。

形状层内部分两档（任务 #31）：
  * 高置信形状 jwt / dsn_with_credentials / bcrypt_hash 在任何文件里都扫；
  * 赋值形状 credential_assignment 依赖值的字符集，是唯一的误报来源，因此对打包产物
    （playwright-report/、test-results/、node_modules/、dist/、build/、*.min.js、
    *.min.css、*.map）默认关闭；--exclude 追加范围，--no-default-excludes 恢复全量扫描。
    取舍是「误报靠扫描范围控制，漏报靠规则修复」，不再靠放宽全局值字符集换安静——
    那一次字符集调整把 password=ab;cdefghij、"password": "..." 这类真凭据一起放过了。
    被排除的文件仍然扫字面值与高置信形状，注入凭据不会因为排除而隐身。

字面值层再分可选与受保护（CI 凭据扫描契约，任务 #207）：
  * --secret-env-var 是可选凭据，未设只打 NOTE 继续扫（如多腿登录里可能为空的 ADMIN_TOKEN）；
  * --require-secret-env-var 是受保护凭据，必须已设且非空——未设即在读取任何文件前
    fail-closed（exit 2 + artifacts_withheld）。否则"该 job 必定注入却因配置静默失败而
    扫 0 字面量"会伪装成永久绿灯，正是扫描器最危险的失效形态。

扫描结果只打印匹配位置的哈希与脱敏片段，绝不回显命中的凭据本身。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

ASSIGNMENT_KIND = "credential_assignment"

# 打包/压缩产物：赋值形状在此类路径上的误报率远高于收益，默认只跑字面值与高置信形状。
DEFAULT_SHAPE_EXCLUDES: tuple[str, ...] = (
    "*/playwright-report/*",
    "*/test-results/*",
    "*/node_modules/*",
    "*/dist/*",
    "*/build/*",
    "*.min.js",
    "*.min.css",
    "*.map",
)

# 键锚点不能用 \b：`_` 属于词字符，ADMIN_PASSWORD / SECRET_KEY 会在 \b 处失配。
# 允许前缀段（GRAPHINSIGHT_ADMIN_PASSWORD=）；后缀只放行确实承载凭据的形态
# （_key/_hash/_token/_value/_secret），password_hint / passwordValidator 仍不误报。
# 冒号前允许可选引号，使 JSON 的 "password": "..." 也能命中。
# 值字符集放开 ; { }（真凭据里会出现），结构性误报交给下面的字面量判别。
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])*"
    r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization)"
    r"(?:[_-](?:key|hash|token|value|secret))?['\"]?"
    r"\s*[:=]\s*['\"]?(?P<value>[^\s'\",]{6,})"
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("dsn_with_credentials", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]{1,64}:[^\s/:@]{3,128}@[0-9a-zA-Z.-]+")),
    (ASSIGNMENT_KIND, CREDENTIAL_ASSIGNMENT),
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

# 值首段就是这些字面量/接收者时，赋值右侧是程序结构而不是凭据
# （minified 与松散日志里的 `password=null;const t=1;`，源码里的 `password=self.password`）。
# 判别只看开头连续字母段，`null;const` 判为结构，`ab;cdefghij` 与 `nullpass123` 不放过。
STRUCTURAL_VALUE_HEADS = {
    "null",
    "undefined",
    "true",
    "false",
    "nan",
    "void",
    "this",
    "self",
    "cls",
}

# 值为「点分标识符 + 左括号」开头时是调用表达式（os.getenv(...) / get_password_hash(x)），
# 不是凭据。已知代价：真凭据若长成 `Pa(ss)word` 这种"标识符紧跟左括号"的形态会被放过，
# 自检里有一条用例把这个缺口钉在明面上，不允许它悄悄扩大。
CODE_CALL_VALUE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*\(")

# 首段命中结构字面量后，紧随其后的字符必须真的是代码边界，否则右侧仍承载真实凭据。
# `null;const` / `self.password` 里首段之后是 `;` 或 `.`（语句分隔符 / 属性访问）——是骨架；
# 而 `undefined-SECRET123` / `null-SECRET123` / `self-SECRET123` 里首段之后是 `-`，
# 那是把真凭据拼在结构前缀上（漏报回归 2026-10-03），必须照常报。
STRUCTURAL_VALUE_TAIL_BOUNDARY = frozenset(".;,)]}")


def _walk(paths: Iterable[Path], excludes: Sequence[str]) -> Iterable[tuple[Path, bool]]:
    """产出 (文件, 是否扫描赋值形状)。排除只作用于赋值形状，不作用于字面值与高置信形状。"""
    for root in paths:
        if root.is_file():
            yield root, not _is_shape_excluded(root, excludes)
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                yield path, not _is_shape_excluded(path, excludes)


def _is_shape_excluded(path: Path, excludes: Sequence[str]) -> bool:
    text = path.as_posix()
    # 相对路径可能没有前导目录段，补一份带前导 / 的形态让 `*/dir/*` 能命中根目录。
    for candidate in (text, f"/{text}"):
        for pattern in excludes:
            if fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _redact(match: str) -> str:
    if len(match) <= 12:
        return f"{match[:2]}***"
    return f"{match[:6]}...{match[-3:]}({len(match)} chars)"


def _is_structural_value(value: str) -> bool:
    """True only when the WHOLE value is code structure, not just a structural prefix.

    A leading run of letters matching STRUCTURAL_VALUE_HEADS is necessary but not
    sufficient: the character right after that run must be a genuine code boundary
    (`;` statement separator, `.` property access, `,` `)` `}` `]`) or end of the
    token. `undefined-SECRET123` / `null-SECRET123` / `self-SECRET123` glue a real
    credential onto the structural prefix with `-`, so they are NOT structural.
    """
    head = re.match(r"[^A-Za-z]*([A-Za-z]+)", value)
    if not head or head.group(1).lower() not in STRUCTURAL_VALUE_HEADS:
        return False
    rest = value[head.end():]
    return rest == "" or rest[0] in STRUCTURAL_VALUE_TAIL_BOUNDARY


def _is_code_value(value: str) -> bool:
    """结构字面量或调用表达式——两者都是代码骨架，不是凭据。"""
    return _is_structural_value(value) or bool(CODE_CALL_VALUE.match(value))


def scan(
    targets: Iterable[tuple[Path, bool]],
    literals: list[str],
    allowed: set[str],
) -> tuple[int, int, int, list[str]]:
    findings: list[str] = []
    scanned_files = 0
    shape_files = 0
    scanned_bytes = 0
    for path, assignment_scanned in targets:
        scanned_files += 1
        if assignment_scanned:
            shape_files += 1
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
            if kind == ASSIGNMENT_KIND and not assignment_scanned:
                continue
            for match in pattern.finditer(text):
                token = match.group(0)
                captured = match.group("value") if kind == ASSIGNMENT_KIND else token
                if not captured:
                    continue
                if captured.strip() in allowed or captured.strip() in PLACEHOLDER_VALUES:
                    continue
                if kind == ASSIGNMENT_KIND and _is_code_value(captured):
                    continue
                if kind == "dsn_with_credentials" and any(fixture in token for fixture in allowed):
                    continue
                findings.append(
                    f"SECRET_FINDING kind={kind} file={path} match_sha256={_sha(token)} value={_redact(captured)}"
                )
    return scanned_files, shape_files, scanned_bytes, findings


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
        "--require-secret-env-var",
        action="append",
        default=[],
        help=(
            "Like --secret-env-var but the variable is protected: it MUST be set and "
            "non-empty. An unset protected variable fails closed instead of silently "
            "scanning zero literals. Repeatable."
        ),
    )
    parser.add_argument(
        "--allow-fixture",
        action="append",
        default=[],
        help="Declared non-secret fixture value that shape patterns may match. Repeatable.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob whose files skip the credential_assignment shape only (defaults still apply). Repeatable.",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Scan bundled/minified artifacts for assignment shapes too (stricter, may raise false positives).",
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

    excludes: list[str] = list(args.exclude)
    if not args.no_default_excludes:
        excludes.extend(DEFAULT_SHAPE_EXCLUDES)

    literals: list[str] = []
    provided: list[str] = []
    for name in args.secret_env_var:
        value = (os.getenv(name) or "").strip()
        if value:
            literals.append(value)
            provided.append(name)
        else:
            print(f"SECRET_SCAN_NOTE env_var_unset name={name}")
    # Protected credentials must be present or the run is misconfigured: scanning zero
    # literals and returning "pass" would be a false green light (an unset ADMIN_PASSWORD
    # on a stack that always provisions one means provisioning silently failed). Fail
    # closed before reading any file so artifacts stay withheld.
    protected: list[str] = []
    unset_required: list[str] = []
    for name in args.require_secret_env_var:
        # Strip BEFORE testing presence: a whitespace-only value ("   ") is a misconfigured
        # provision, not a set credential. Without this, os.getenv returns a truthy "   ",
        # .strip() yields "", and the run would count it as protected and scan zero literals.
        value = (os.getenv(name) or "").strip()
        if value:
            literals.append(value)
            provided.append(name)
            protected.append(name)
        else:
            unset_required.append(name)
    if unset_required:
        for name in unset_required:
            print(f"SECRET_SCAN_PREREQ_MISSING required_secret_env_var_unset name={name}")
        print(
            "SECRET_SCAN_SUMMARY result=fail reason=protected_credential_unset "
            f"unset_required={len(unset_required)} artifacts_withheld=true"
        )
        return 2
    allowed = {item.strip() for item in args.allow_fixture if item.strip()}

    files, shape_files, total_bytes, findings = scan(_walk(existing, excludes), literals, allowed)
    excluded_files = files - shape_files
    for line in findings[:40]:
        print(line)
    if len(findings) > 40:
        print(f"SECRET_FINDINGS_TRUNCATED hidden={len(findings) - 40}")
    if excluded_files:
        print(
            f"SECRET_SCAN_NOTE shape_assignment_skipped_files={excluded_files} "
            "scope=bundled/minified_artifacts layers_still_scanned=run_credential,jwt,dsn,bcrypt"
        )
    print(
        "SECRET_SCAN_SUMMARY "
        f"paths={len(existing)} files={files} bytes={total_bytes} "
        f"shape_scanned_files={shape_files} shape_excluded_files={excluded_files} "
        f"exclude_rules={len(excludes)} "
        f"credential_env_vars={len(provided)} protected_env_vars={len(protected)} "
        f"allowed_fixtures={len(allowed)} "
        f"findings={len(findings)} result={'pass' if not findings else 'fail'}"
    )
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
