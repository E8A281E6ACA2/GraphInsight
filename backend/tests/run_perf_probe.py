#!/usr/bin/env python3
"""Lightweight performance probe for GraphInsight runtime endpoints.

The default case set is read-only and safe for release checks. Expensive cases
such as DocQA generation and graph build must be selected explicitly.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ["health", "docqa-health", "nl2cypher-status"]
RELEASE_CASES = ["health", "query", "docqa-health", "nl2cypher-status", "docqa", "graph-build"]


@dataclass(frozen=True)
class ProbeCase:
    name: str
    method: str
    path: str
    payload: Optional[dict[str, Any]]
    expected_owner: str
    # 作用域强制（M4）：docqa / graph-build 必须携带 kb 作用域，否则网关返回
    # 400 KB_SCOPE_REQUIRED，探针会把「契约要求」误报成「接口失败」。
    scoped: bool = False


@dataclass(frozen=True)
class ProbeResult:
    case: str
    ok: bool
    status: int
    latency_ms: float
    route_owner: str
    error: str = ""


def _json_or_raw(text: str) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


def _request(
    *,
    base_url: str,
    token: str,
    timeout: float,
    case: ProbeCase,
    check_route_owner: bool,
    kb_id: str = "",
) -> ProbeResult:
    url = base_url.rstrip("/") + case.path
    body = None
    headers = {
        "Accept": "application/json",
        "X-Trace-Id": "perf-probe-" + uuid.uuid4().hex,
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    if case.scoped and kb_id:
        headers["x-kb-id"] = kb_id
    if case.payload is not None:
        body = json.dumps(case.payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    started = time.perf_counter()
    status = 0
    route_owner = ""
    error = ""

    try:
        req = urllib.request.Request(url=url, data=body, method=case.method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            route_owner = resp.headers.get("X-GraphInsight-Route-Owner", "")
            response_body = _json_or_raw(resp.read().decode("utf-8", errors="replace"))
            _cleanup_probe_side_effect(base_url=base_url, token=token, timeout=timeout, case=case, body=response_body)
    except urllib.error.HTTPError as exc:
        status = exc.code
        route_owner = exc.headers.get("X-GraphInsight-Route-Owner", "")
        error = f"http_error:{exc.code}"
        _json_or_raw(exc.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        error = exc.__class__.__name__

    latency_ms = (time.perf_counter() - started) * 1000.0
    ok_status = 200 <= status < 300
    ok_owner = (not check_route_owner) or route_owner == case.expected_owner
    if ok_status and not ok_owner:
        error = f"route_owner:{route_owner or '<missing>'}"
    return ProbeResult(
        case=case.name,
        ok=ok_status and ok_owner,
        status=status,
        latency_ms=latency_ms,
        route_owner=route_owner,
        error=error,
    )


def _cleanup_probe_side_effect(
    *,
    base_url: str,
    token: str,
    timeout: float,
    case: ProbeCase,
    body: Any,
) -> None:
    if case.name != "graph-build" or not token.strip() or not isinstance(body, dict):
        return

    data = body.get("data")
    if not isinstance(data, dict):
        return
    job_id = data.get("job_id")
    if not isinstance(job_id, int):
        return

    url = base_url.rstrip("/") + f"/api/v1/admin/jobs/{job_id}:cancel"
    req = urllib.request.Request(
        url=url,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token.strip()}",
            "Content-Type": "application/json",
            "X-Trace-Id": "perf-probe-cleanup-" + uuid.uuid4().hex,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, 5.0)) as resp:
            resp.read()
    except Exception:
        return


def _login(base_url: str, username: str, password: str, timeout: float) -> str:
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url=base_url.rstrip("/") + "/api/v1/admin/auth/login",
        data=payload,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = _json_or_raw(resp.read().decode("utf-8", errors="replace"))
    data = body.get("data") if isinstance(body, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("login response did not include data.token")
    return str(token)


def _discover_active_kb(base_url: str, token: str, timeout: float) -> str:
    """取调用账号真实可见的第一个 active KB，避免探针依赖人工指定作用域。"""
    req = urllib.request.Request(
        url=base_url.rstrip("/") + "/api/knowledge-bases",
        method="GET",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token.strip()}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = _json_or_raw(resp.read().decode("utf-8", errors="replace"))
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    for item in items or []:
        if isinstance(item, dict) and str(item.get("status")) == "active" and item.get("kb_id"):
            return str(item["kb_id"])
    return ""


def _issue_local_token(email: str) -> str:
    sys.path.insert(0, str(ROOT))
    from admin.database import SessionLocal
    from admin.models import AdminUser
    from admin.services.auth_service import auth_service

    db = SessionLocal()
    try:
        user = db.query(AdminUser).filter(AdminUser.email == email).first()
        if user is None:
            raise RuntimeError(f"admin user not found: {email}")
        return auth_service.create_access_token({"sub": user.email or user.username})
    finally:
        db.close()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil((percentile / 100.0) * len(ordered)) - 1))
    return ordered[rank]


def _summarize(case: ProbeCase, results: list[ProbeResult]) -> dict[str, Any]:
    latencies = [item.latency_ms for item in results]
    success = sum(1 for item in results if item.ok)
    total = len(results)
    status_counts: dict[str, int] = {}
    owner_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for item in results:
        status_counts[str(item.status)] = status_counts.get(str(item.status), 0) + 1
        owner = item.route_owner or "<missing>"
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        if item.error:
            error_counts[item.error] = error_counts.get(item.error, 0) + 1

    return {
        "name": case.name,
        "method": case.method,
        "path": case.path,
        "expected_owner": case.expected_owner,
        "total": total,
        "success": success,
        "failed": total - success,
        "error_rate": round((total - success) / total, 4) if total else 0.0,
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else 0.0,
            "avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "p50": round(_percentile(latencies, 50), 3),
            "p95": round(_percentile(latencies, 95), 3),
            "p99": round(_percentile(latencies, 99), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "status_counts": status_counts,
        "route_owner_counts": owner_counts,
        "error_counts": error_counts,
    }


def _build_cases(args: argparse.Namespace) -> list[ProbeCase]:
    payload_query = {"cypher": args.cypher, "parameters": {}}
    payload_docqa = {"question": args.question, "top_k": args.top_k}
    payload_build = {"source": "documents", "force": args.build_force, "note": "perf-probe"}

    catalog = {
        "health": ProbeCase("health", "GET", "/health", None, "go-native"),
        "query": ProbeCase("query", "POST", "/api/query", payload_query, "go-native"),
        "docqa-health": ProbeCase("docqa-health", "GET", "/api/docqa/health?probe_llm=false", None, "go-orchestrator"),
        "nl2cypher-status": ProbeCase("nl2cypher-status", "GET", "/api/nl2cypher/status", None, "go-native"),
        "docqa": ProbeCase("docqa", "POST", "/api/docqa", payload_docqa, "go-orchestrator", scoped=True),
        "graph-build": ProbeCase("graph-build", "POST", "/api/graph/build", payload_build, "go-native", scoped=True),
    }
    if args.case:
        selected = args.case
    elif args.preset == "release":
        selected = RELEASE_CASES
    else:
        selected = DEFAULT_CASES
    return [catalog[name] for name in selected]


def _write_json_report(path: str, report: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown_report(path: str, report: dict[str, Any]) -> None:
    lines = [
        "# GraphInsight Performance Probe",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- base_url: `{report['base_url']}`",
        f"- requests_per_case: `{report['requests_per_case']}`",
        f"- concurrency: `{report['concurrency']}`",
        f"- preset: `{report['preset']}`",
        f"- kb_id: `{report.get('kb_id') or '<none>'}`",
        f"- max_error_rate: `{report['thresholds']['max_error_rate']}`",
        f"- max_p95_ms: `{report['thresholds']['max_p95_ms']}`",
        "",
        "| case | total | success | failed | error_rate | p50_ms | p95_ms | p99_ms | max_ms | owners |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report["cases"]:
        latency = item["latency_ms"]
        owners = ", ".join(f"{k}:{v}" for k, v in sorted(item["route_owner_counts"].items()))
        lines.append(
            "| {name} | {total} | {success} | {failed} | {error_rate:.2%} | {p50:.1f} | {p95:.1f} | {p99:.1f} | {max_latency:.1f} | {owners} |".format(
                name=item["name"],
                total=item["total"],
                success=item["success"],
                failed=item["failed"],
                error_rate=item["error_rate"],
                p50=latency["p50"],
                p95=latency["p95"],
                p99=latency["p99"],
                max_latency=latency["max"],
                owners=owners,
            )
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a lightweight GraphInsight performance probe")
    parser.add_argument("--base-url", default=os.getenv("GO_BASE_URL", os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081")))
    parser.add_argument("--token", default=os.getenv("ADMIN_TOKEN", ""))
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL", "yh@qs.al"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--kb-id",
        default=os.getenv("PERF_PROBE_KB_ID", ""),
        help="KB scope for scoped cases; empty means discover from the caller's visible active KBs",
    )
    parser.add_argument(
        "--preset",
        choices=["readonly", "release"],
        default=os.getenv("PERF_PROBE_PRESET", "readonly"),
        help="readonly runs safe health/status checks; release also runs query/docqa/graph-build cases",
    )
    parser.add_argument("--case", action="append", choices=["health", "query", "docqa-health", "nl2cypher-status", "docqa", "graph-build"])
    parser.add_argument("--requests", type=int, default=20, help="Request count per case")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--question", default="Give a short summary from the current knowledge base.")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--cypher", default="MATCH (n) RETURN count(n) AS count LIMIT 1")
    parser.add_argument("--build-force", action="store_true")
    parser.add_argument("--skip-route-owner-check", action="store_true")
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--max-p95-ms", type=float, default=0.0, help="0 disables latency threshold")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-markdown", default="")
    args = parser.parse_args()

    if args.requests < 1:
        raise SystemExit("--requests must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")

    token = args.token.strip()
    if not token and args.admin_password.strip():
        try:
            token = _login(args.base_url, args.admin_email, args.admin_password, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"LOGIN_INIT_FAIL {exc}")
            return 1
    if not token:
        try:
            token = _issue_local_token(args.admin_email)
            print("ADMIN_TOKEN_READY source=local")
        except Exception as exc:  # noqa: BLE001
            print(f"LOCAL_TOKEN_INIT_SKIP {exc}")

    cases = _build_cases(args)
    kb_id = args.kb_id.strip()
    kb_source = "env"
    if any(case.scoped for case in cases):
        if not kb_id and token:
            try:
                kb_id = _discover_active_kb(args.base_url, token, args.timeout)
                kb_source = "discover"
            except Exception as exc:  # noqa: BLE001
                print(f"KB_DISCOVERY_FAIL {exc.__class__.__name__}")
        if not kb_id:
            print("PERF_PROBE_BLOCKED reason=no_visible_active_kb scoped_cases=1")
            return 1
        print(f"PERF_PROBE_KB kb_id={kb_id} source={kb_source}")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url.rstrip("/"),
        "preset": args.preset,
        "kb_id": kb_id,
        "requests_per_case": args.requests,
        "concurrency": args.concurrency,
        "route_owner_check": not args.skip_route_owner_check,
        "thresholds": {
            "max_error_rate": args.max_error_rate,
            "max_p95_ms": args.max_p95_ms,
        },
        "cases": [],
    }

    print(
        "PERF_PROBE begin "
        f"base_url={report['base_url']} requests_per_case={args.requests} concurrency={args.concurrency}"
    )
    failed = False
    for case in cases:
        results: list[ProbeResult] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    _request,
                    base_url=report["base_url"],
                    token=token,
                    timeout=args.timeout,
                    case=case,
                    check_route_owner=not args.skip_route_owner_check,
                    kb_id=kb_id,
                )
                for _ in range(args.requests)
            ]
            for future in as_completed(futures):
                results.append(future.result())

        summary = _summarize(case, results)
        report["cases"].append(summary)

        latency = summary["latency_ms"]
        status = "OK"
        if summary["error_rate"] > args.max_error_rate:
            status = "FAIL"
            failed = True
        if args.max_p95_ms > 0 and latency["p95"] > args.max_p95_ms:
            status = "FAIL"
            failed = True

        print(
            f"- [{status}] {summary['name']}: total={summary['total']} "
            f"success={summary['success']} failed={summary['failed']} "
            f"error_rate={summary['error_rate']:.2%} "
            f"p50={latency['p50']:.1f}ms p95={latency['p95']:.1f}ms "
            f"p99={latency['p99']:.1f}ms max={latency['max']:.1f}ms "
            f"owners={summary['route_owner_counts']} statuses={summary['status_counts']}"
        )

    if args.output_json:
        _write_json_report(args.output_json, report)
        print(f"PERF_PROBE_JSON {args.output_json}")
    if args.output_markdown:
        _write_markdown_report(args.output_markdown, report)
        print(f"PERF_PROBE_MARKDOWN {args.output_markdown}")

    print("PERF_PROBE end")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
