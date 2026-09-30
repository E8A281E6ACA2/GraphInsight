#!/usr/bin/env python3
"""Refuse GitHub Actions expressions whose context is illegal for its position.

Why this exists: push run #20 compiled to zero jobs because `${{ runner.temp }}`
sat in a *job-level* `env:` block. PyYAML accepts it and every structural check
passes; only Actions' own expression-context rules reject the file, and the
rejection surfaces as "workflow is invalid" with no log and no check-run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# Contexts GitHub makes available where a workflow-level `env:` value is evaluated.
WORKFLOW_ENV_ALLOWED = {"github", "vars", "secrets"}
# Contexts available for `jobs.<id>.env` and `jobs.<id>.if`.
JOB_LEVEL_ALLOWED = {"github", "needs", "strategy", "matrix", "inputs", "vars", "secrets"}

ALL_CONTEXTS = {
    "github",
    "needs",
    "strategy",
    "matrix",
    "job",
    "runner",
    "env",
    "vars",
    "secrets",
    "steps",
    "inputs",
}

EXPRESSION_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
QUOTED_RE = re.compile(r"'[^']*'")
ROOT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s*(?=\.|\[)")


def iter_expressions(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_expressions(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_expressions(item)
    elif isinstance(value, str):
        for match in EXPRESSION_RE.finditer(value):
            yield match.group(1).strip()


def illegal_contexts(expression, allowed):
    offenders = []
    for token in ROOT_RE.findall(QUOTED_RE.sub("''", expression)):
        if token in ALL_CONTEXTS and token not in allowed:
            offenders.append(token)
    return sorted(set(offenders))


def check_workflow(path: Path):
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return [f"{path}: yaml_parse_error {error}"], 0
    if not isinstance(document, dict):
        return [f"{path}: workflow root is not a mapping"], 0

    findings = []
    checked = 0

    def inspect(value, allowed, where):
        nonlocal checked
        for expression in iter_expressions(value):
            checked += 1
            for token in illegal_contexts(expression, allowed):
                findings.append(
                    f"{path}: {where} context '{token}' is not allowed here; "
                    f"expression={expression}"
                )

    inspect(document.get("env") or {}, WORKFLOW_ENV_ALLOWED, "key=env")

    jobs = document.get("jobs") or {}
    if not isinstance(jobs, dict):
        return findings + [f"{path}: jobs is not a mapping"], checked
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for key in ("env", "if", "concurrency", "container", "services"):
            inspect(job.get(key), JOB_LEVEL_ALLOWED, f"job={job_id} key={key}")
    return findings, checked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="workflow file or directory to check (default: .github/workflows)",
    )
    args = parser.parse_args(argv)

    targets = []
    for raw in args.path or [Path(".github/workflows")]:
        path = Path(raw)
        if path.is_dir():
            targets.extend(sorted(path.glob("*.yml")) + sorted(path.glob("*.yaml")))
        elif path.is_file():
            targets.append(path)
        else:
            print(f"WORKFLOW_PREREQ_INVALID missing path: {path}")
            return 2

    findings = []
    expressions = 0
    for target in targets:
        target_findings, target_count = check_workflow(target)
        findings.extend(target_findings)
        expressions += target_count

    for finding in findings:
        print(f"WORKFLOW_CONTEXT_INVALID {finding}")
    print(
        f"WORKFLOW_CONTEXT_SUMMARY files={len(targets)} expressions={expressions} "
        f"violations={len(findings)}"
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
