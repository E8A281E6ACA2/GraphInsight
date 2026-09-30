"""
文档上传 -> 建图 -> 问答 -> 追踪 -> 删除 全链路烟测

用法:
    # 方式 1：直接提供管理员 token
    ADMIN_BASE_URL=http://127.0.0.1:8081 \
    ADMIN_TOKEN=... \
    python backend/tests/check_docqa_full_chain.py

    # 方式 2：提供管理员邮箱和密码（脚本自动登录）
    ADMIN_BASE_URL=http://127.0.0.1:8081 \
    ADMIN_EMAIL=yh@qs.al \
    ADMIN_PASSWORD=*** \
    python backend/tests/check_docqa_full_chain.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from kb_scope import require_active_kb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from admin.database import SessionLocal
from admin.models import AdminUser
from admin.services.auth_service import auth_service


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    payload: Optional[dict] = None,
    headers: Optional[dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 180,
) -> tuple[int, dict | str]:
    body = data
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    if isinstance(body, dict):
        return body.get("data")
    return None


def _extract_trace_id(body: dict | str) -> Optional[str]:
    if isinstance(body, dict) and body.get("trace_id"):
        return str(body["trace_id"])
    return None


def _login(base_url: str, username: str, password: str) -> str:
    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/auth/login",
        payload={"username": username, "password": password},
        timeout=30,
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"LOGIN_FAIL status={status} body={body}")
    data = _extract_data(body)
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"LOGIN_NO_TOKEN body={body}")
    return str(token)


def _issue_local_token(email: str) -> Optional[str]:
    db = SessionLocal()
    try:
        user = db.query(AdminUser).filter(AdminUser.email == email).first()
        if user is None:
            return None
        return auth_service.create_access_token({"sub": user.email or user.username})
    finally:
        db.close()


def _build_multipart(file_path: Path, field_name: str = "files") -> tuple[str, bytes]:
    boundary = f"----GraphInsightSmoke{uuid.uuid4().hex}"
    payload = []
    payload.append(f"--{boundary}\r\n".encode("utf-8"))
    payload.append(
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8")
    )
    payload.append(b"Content-Type: text/plain\r\n\r\n")
    payload.append(file_path.read_bytes())
    payload.append(b"\r\n")
    payload.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(payload)


def _list_documents(base_url: str, token: str, kb_id: str) -> list[dict]:
    status, body = _request(
        "GET", f"{base_url}/api/documents", token=token, headers={"x-kb-id": kb_id}, timeout=30
    )
    if status != 200:
        raise RuntimeError(f"LIST_DOCS_FAIL status={status} body={body}")
    data = _extract_data(body)
    items = data.get("items", []) if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _find_doc(items: list[dict], file_name: str) -> Optional[dict]:
    for item in items:
        if str(item.get("name")) == file_name:
            return item
    return None


def _delete_document(base_url: str, token: str, kb_id: str, doc_id: str) -> dict:
    query = urllib.parse.urlencode(
        {
            "purge_graph": "true",
            "soft_delete": "false",
            "dry_run": "false",
            "verify_after": "true",
        }
    )
    status, body = _request(
        "DELETE",
        f"{base_url}/api/documents/{doc_id}?{query}",
        token=token,
        headers={"x-kb-id": kb_id},
        timeout=120,
    )
    if status != 200:
        raise RuntimeError(f"DELETE_DOC_FAIL status={status} body={body}")
    data = _extract_data(body)
    if not isinstance(data, dict):
        raise RuntimeError(f"DELETE_DOC_INVALID body={body}")
    return data


def _verify_trace(base_url: str, token: str, kb_id: str, trace_id: str) -> dict:
    list_url = f"{base_url}/api/v1/admin/qa-traces?trace_id={trace_id}&page=1&page_size=5"
    status, body = _request(
        "GET", list_url, token=token, headers={"x-kb-id": kb_id}, timeout=30
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"TRACE_LIST_FAIL status={status} body={body}")
    data = _extract_data(body)
    items = data.get("items", []) if isinstance(data, dict) else []
    if not items:
        raise RuntimeError(f"TRACE_NOT_FOUND trace_id={trace_id}")

    detail_status, detail_body = _request(
        "GET",
        f"{base_url}/api/v1/admin/qa-traces/{trace_id}",
        token=token,
        headers={"x-kb-id": kb_id},
        timeout=30,
    )
    if detail_status != 200 or not isinstance(detail_body, dict):
        raise RuntimeError(f"TRACE_DETAIL_FAIL status={detail_status} body={detail_body}")
    detail = _extract_data(detail_body)
    if not isinstance(detail, dict):
        raise RuntimeError(f"TRACE_DETAIL_INVALID body={detail_body}")
    return detail


def main() -> int:
    parser = argparse.ArgumentParser(description="DocQA full-chain smoke check")
    parser.add_argument("--base-url", default=os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--admin-token", default=os.getenv("ADMIN_TOKEN"))
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL", "yh@qs.al"))
    parser.add_argument("--admin-password", default=os.getenv("ADMIN_PASSWORD"))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("DOCQA_FULL_CHAIN_TIMEOUT_SECONDS", "240")),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("DOCQA_FULL_CHAIN_POLL_SECONDS", "2")),
    )
    args = parser.parse_args()

    base_url = str(args.base_url).rstrip("/")
    admin_email = args.admin_email
    admin_password = args.admin_password
    token = (args.admin_token or "").strip()
    timeout_seconds = args.timeout_seconds
    poll_interval = args.poll_seconds

    if not token:
        if not admin_password:
            token = _issue_local_token(admin_email) or ""
            if token:
                print("ADMIN_TOKEN_READY source=local")
            else:
                print("MISSING_ADMIN_CREDENTIALS")
                return 1
        else:
            try:
                token = _login(base_url, admin_email, admin_password)
            except Exception as exc:  # noqa: BLE001
                print(str(exc))
                return 1

    unique_suffix = uuid.uuid4().hex[:10]
    temp_doc = ROOT / "tmp" / f"codex_docqa_chain_{int(time.time())}_{unique_suffix}.txt"
    temp_doc.parent.mkdir(parents=True, exist_ok=True)
    temp_doc.write_text(
        "\n".join(
            [
                "GraphInsight smoke chain document.",
                "Purpose: verify upload, build graph, docqa trace, and hard delete flow.",
                "Current default QA model: qwen-flash.",
                "This file should be safe to delete after the smoke test finishes.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    created_doc_id: Optional[str] = None
    created_doc_name: Optional[str] = None
    try:
        kb_id = require_active_kb("check_docqa_full_chain.py", base_url, token)
        print(f"STEP upload file={temp_doc.name}")
        boundary, form_body = _build_multipart(temp_doc)
        upload_status, upload_body = _request(
            "POST",
            f"{base_url}/api/documents/upload",
            token=token,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "x-kb-id": kb_id,
            },
            data=form_body,
            timeout=120,
        )
        print(f"UPLOAD_STATUS {upload_status}")
        if upload_status != 200:
            print(upload_body)
            return 1
        upload_data = _extract_data(upload_body)
        uploaded = upload_data.get("uploaded", []) if isinstance(upload_data, dict) else []
        if not uploaded:
            print(f"UPLOAD_EMPTY body={upload_body}")
            return 1
        first_uploaded = uploaded[0] if isinstance(uploaded[0], dict) else {}
        created_doc_id = str(first_uploaded.get("doc_id") or first_uploaded.get("id") or "")
        created_doc_name = str(first_uploaded.get("name") or temp_doc.name)
        if not created_doc_id:
            print(f"UPLOAD_DOC_ID_MISSING body={upload_body}")
            return 1

        print("STEP list documents")
        docs = _list_documents(base_url, token, kb_id)
        print(f"LIST_STATUS 200 count={len(docs)}")
        target = None
        for item in docs:
            if str(item.get("id")) == created_doc_id or str(item.get("doc_id")) == created_doc_id:
                target = item
                break
        if not target:
            print(f"DOC_NOT_FOUND_AFTER_UPLOAD doc_id={created_doc_id} uploaded_name={created_doc_name}")
            return 1
        print(f"DOC_ID {created_doc_id}")

        print("STEP create build job")
        create_payload = {
            "tenant_id": "t-docqa-chain",
            "project_id": "p-docqa-chain",
            "kb_id": kb_id,
            # build_graph 契约要求显式 doc_ids（禁止目录扫描），且必须归属当前 kb。
            "payload": {"doc_ids": [created_doc_id], "force": True},
            "max_retries": 0,
        }
        create_status, create_body = _request(
            "POST",
            f"{base_url}/api/v1/admin/jobs/build-graph",
            token=token,
            headers={"x-kb-id": kb_id},
            payload=create_payload,
            timeout=30,
        )
        print(f"JOB_CREATE_STATUS {create_status}")
        if create_status not in {200, 201}:
            print(create_body)
            return 1
        job_data = _extract_data(create_body)
        if not isinstance(job_data, dict) or not job_data.get("id"):
            print(f"JOB_CREATE_INVALID body={create_body}")
            return 1
        job_id = int(job_data["id"])
        print(f"JOB_ID {job_id}")

        print("STEP poll build job")
        deadline = time.time() + timeout_seconds
        last_job: dict | None = None
        while time.time() < deadline:
            get_status, get_body = _request(
                "GET",
                f"{base_url}/api/v1/admin/jobs/{job_id}",
                token=token,
                headers={"x-kb-id": kb_id},
                timeout=30,
            )
            if get_status != 200:
                print(f"JOB_GET_FAIL status={get_status} body={get_body}")
                return 1
            last_job = _extract_data(get_body)
            if not isinstance(last_job, dict):
                print(f"JOB_GET_INVALID body={get_body}")
                return 1
            state = str(last_job.get("status") or "")
            print(f"JOB_STATE {state}")
            if state in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(poll_interval)

        if not isinstance(last_job, dict):
            print("JOB_RESULT_MISSING")
            return 1
        final_state = str(last_job.get("status") or "")
        print(f"JOB_FINAL {final_state}")
        if final_state != "succeeded":
            print(f"JOB_NOT_SUCCEEDED body={last_job}")
            return 1

        print("STEP docqa")
        qa_payload = {
            "question": "这份文档主要是用来验证什么？",
            "top_k": 2,
            "require_citation": True,
            "kb_id": kb_id,
        }
        qa_status, qa_body = _request(
            "POST",
            f"{base_url}/api/docqa",
            token=token,
            headers={"x-kb-id": kb_id},
            payload=qa_payload,
            timeout=180,
        )
        print(f"DOCQA_STATUS {qa_status}")
        if qa_status != 200 or not isinstance(qa_body, dict):
            print(qa_body)
            return 1
        qa_data = _extract_data(qa_body)
        trace_id = _extract_trace_id(qa_body)
        if not isinstance(qa_data, dict) or not trace_id:
            print(f"DOCQA_INVALID body={qa_body}")
            return 1
        answer = str(qa_data.get("answer") or "")
        print(f"DOCQA_TRACE_ID {trace_id}")
        print(f"DOCQA_ANSWER_PREVIEW {answer[:120]}")

        print("STEP verify qa trace")
        detail = _verify_trace(base_url, token, kb_id, trace_id)
        print(f"TRACE_QA_TYPE {detail.get('qa_type')}")
        print(f"TRACE_STATUS {detail.get('status')}")
        if detail.get("qa_type") != "docqa":
            print(f"TRACE_QA_TYPE_INVALID detail={detail}")
            return 1
        if detail.get("status") != "success":
            print(f"TRACE_STATUS_INVALID detail={detail}")
            return 1

        print("STEP delete document")
        deleted = _delete_document(base_url, token, kb_id, created_doc_id)
        print("DELETE_STATUS 200")
        if str(deleted.get("file_action")) != "hard_deleted":
            print(f"DELETE_ACTION_INVALID data={deleted}")
            return 1

        print("STEP verify delete")
        docs_after = _list_documents(base_url, token, kb_id)
        if any(str(item.get("id")) == created_doc_id for item in docs_after):
            print("DELETE_VERIFY_FAIL_DOC_STILL_EXISTS")
            return 1

        print("DOCQA_FULL_CHAIN_OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED_ERROR {exc}")
        return 1
    finally:
        try:
            if created_doc_id:
                _delete_document(base_url, token, kb_id, created_doc_id)
        except Exception as exc:  # noqa: BLE001
            print(f"CLEANUP_SWEEP_NOOP detail={exc}")
        try:
            temp_doc.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
