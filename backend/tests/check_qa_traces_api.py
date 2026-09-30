"""
问答链路追踪 API 烟测脚本

用法:
    # 方式 1：直接提供管理员 token
    ADMIN_BASE_URL=http://127.0.0.1:8081 \
    ADMIN_TOKEN=... \
    python backend/tests/check_qa_traces_api.py

    # 方式 2：提供管理员邮箱和密码（脚本自动登录）
    ADMIN_BASE_URL=http://127.0.0.1:8081 \
    ADMIN_EMAIL=yh@qs.al \
    ADMIN_PASSWORD=Admin@123456 \
    python backend/tests/check_qa_traces_api.py
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Optional

from kb_scope import require_active_kb


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    payload: Optional[dict] = None,
    kb_id: Optional[str] = None,
) -> tuple[int, dict | str]:
    body = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if kb_id:
        headers["x-kb-id"] = kb_id
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _extract_data(body: dict | str) -> dict | list | None:
    if isinstance(body, dict) and "data" in body:
        return body.get("data")
    return None


def _extract_trace_id(body: dict | str) -> Optional[str]:
    if isinstance(body, dict):
        trace_id = body.get("trace_id")
        if trace_id:
            return str(trace_id)
    return None


def _login(base_url: str, username: str, password: str) -> str:
    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/auth/login",
        payload={"username": username, "password": password},
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"登录失败: status={status}, body={body}")
    data = _extract_data(body)
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"登录响应缺少 token: body={body}")
    return str(token)


def _verify_trace(
    *,
    base_url: str,
    token: str,
    kb_id: str,
    trace_id: str,
    qa_type: str,
    expected_status: str,
) -> None:
    list_status, list_body = _request(
        "GET",
        f"{base_url}/api/v1/admin/qa-traces?trace_id={trace_id}&page=1&page_size=5",
        token=token,
        kb_id=kb_id,
    )
    if list_status != 200 or not isinstance(list_body, dict):
        raise RuntimeError(f"查询追踪列表失败: status={list_status}, body={list_body}")

    data = _extract_data(list_body)
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        raise RuntimeError(f"未找到 trace_id={trace_id} 的追踪记录")

    item = items[0]
    if item.get("qa_type") != qa_type:
        raise RuntimeError(f"qa_type 不匹配: expected={qa_type}, actual={item.get('qa_type')}")
    if item.get("status") != expected_status:
        raise RuntimeError(f"status 不匹配: expected={expected_status}, actual={item.get('status')}")

    detail_status, detail_body = _request(
        "GET",
        f"{base_url}/api/v1/admin/qa-traces/{trace_id}",
        token=token,
        kb_id=kb_id,
    )
    if detail_status != 200 or not isinstance(detail_body, dict):
        raise RuntimeError(f"查询追踪详情失败: status={detail_status}, body={detail_body}")

    detail = _extract_data(detail_body)
    if not isinstance(detail, dict):
        raise RuntimeError(f"追踪详情结构非法: body={detail_body}")
    if detail.get("trace_id") != trace_id:
        raise RuntimeError(f"详情 trace_id 不匹配: expected={trace_id}, actual={detail.get('trace_id')}")
    if detail.get("qa_type") != qa_type:
        raise RuntimeError(f"详情 qa_type 不匹配: expected={qa_type}, actual={detail.get('qa_type')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="问答链路追踪 API 烟测")
    parser.add_argument("--base-url", default=_env("ADMIN_BASE_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--admin-token", default=_env("ADMIN_TOKEN"))
    parser.add_argument("--admin-email", default=_env("ADMIN_EMAIL"))
    parser.add_argument("--admin-password", default=_env("ADMIN_PASSWORD"))
    args = parser.parse_args()

    base_url = str(args.base_url).rstrip("/")
    token = args.admin_token.strip() if args.admin_token else None
    if not token:
        if not args.admin_email or not args.admin_password:
            print("缺少管理员凭证：请提供 ADMIN_TOKEN 或 ADMIN_EMAIL + ADMIN_PASSWORD")
            return 1
        try:
            token = _login(base_url, args.admin_email, args.admin_password)
            print("admin_token 获取成功")
        except Exception as exc:  # noqa: BLE001
            print(f"获取 admin_token 失败: {exc}")
            return 1

    kb_id = require_active_kb("check_qa_traces_api.py", base_url, token)

    cases = [
        (
            "docqa",
            "/api/docqa",
            {
                "question": "请概述当前知识库的核心主题",
                "top_k": 2,
                "require_citation": True,
                "kb_id": kb_id,
            },
        ),
        (
            "deep_research",
            "/api/docqa/deep-research",
            {
                "question": "请总结当前知识库文档的核心主题，并给出风险与下一步建议",
                "top_k": 8,
                "max_sub_questions": 4,
                "kb_id": kb_id,
            },
        ),
    ]

    failed = 0
    for qa_type, path, payload in cases:
        status, body = _request(
            "POST", f"{base_url}{path}", token=token, payload=payload, kb_id=kb_id
        )
        trace_id = _extract_trace_id(body)
        expected_trace_status = "success" if status == 200 else "failed"

        if status not in {200, 500}:
            print(f"[FAIL] {qa_type} unexpected status={status} body={body}")
            failed += 1
            continue
        if not trace_id:
            print(f"[FAIL] {qa_type} 响应缺少 trace_id body={body}")
            failed += 1
            continue

        try:
            _verify_trace(
                base_url=base_url,
                token=token,
                kb_id=kb_id,
                trace_id=trace_id,
                qa_type=qa_type,
                expected_status=expected_trace_status,
            )
            print(f"[OK] {qa_type} status={status} trace_id={trace_id}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {qa_type} trace verify failed: {exc}")

    print("-" * 60)
    print(f"failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
