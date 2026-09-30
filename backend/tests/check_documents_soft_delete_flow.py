"""
Documents soft-delete flow smoke check.

Validates (all requests carry an explicit KB scope, contract §2/§3):
1) upload a smoke document into the active knowledge base
2) delete dry-run preview
3) soft delete into trash
4) list deleted documents
5) restore from trash
6) hard delete cleanup

Usage:
    ADMIN_BASE_URL=http://127.0.0.1:8081 \
    ADMIN_EMAIL=yh@qs.al \
    ADMIN_PASSWORD=*** \
    python backend/tests/check_documents_soft_delete_flow.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from kb_scope import require_active_kb


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    kb_id: str | None = None,
    payload: dict | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, dict | str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if kb_id:
        headers["x-kb-id"] = kb_id
    body = raw_body
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _extract_data(body: dict | str) -> dict | list | None:
    if isinstance(body, dict):
        return body.get("data")
    return None


def _login(base: str, username: str, password: str) -> str:
    status, body = _request(
        "POST",
        f"{base}/api/v1/admin/auth/login",
        payload={"username": username, "password": password},
    )
    if status != 200:
        raise RuntimeError(f"LOGIN_FAIL status={status} body={body}")
    data = _extract_data(body)
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError(f"LOGIN_NO_TOKEN body={body}")
    return str(data["token"])


def _multipart_upload(filename: str, content: bytes) -> tuple[str, bytes]:
    boundary = f"----GraphInsightSmoke{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        ).encode("utf-8"),
        b"Content-Type: text/plain\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return boundary, b"".join(parts)


def _upload_document(base: str, token: str, kb_id: str, filename: str, content: bytes) -> str:
    boundary, body = _multipart_upload(filename, content)
    status, resp = _request(
        "POST",
        f"{base}/api/documents/upload",
        token=token,
        kb_id=kb_id,
        raw_body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if status != 200:
        raise RuntimeError(f"UPLOAD_FAIL status={status} body={resp}")
    data = _extract_data(resp)
    uploaded = data.get("uploaded", []) if isinstance(data, dict) else []
    if not uploaded or not isinstance(uploaded[0], dict):
        raise RuntimeError(f"UPLOAD_EMPTY body={resp}")
    doc_id = str(uploaded[0].get("doc_id") or uploaded[0].get("id") or "")
    if not doc_id:
        raise RuntimeError(f"UPLOAD_DOC_ID_MISSING body={resp}")
    return doc_id


def _list_documents(base: str, token: str, kb_id: str) -> list[dict]:
    status, body = _request("GET", f"{base}/api/documents", token=token, kb_id=kb_id)
    if status != 200:
        raise RuntimeError(f"LIST_DOCS_FAIL status={status} body={body}")
    data = _extract_data(body)
    items = data.get("items", []) if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _list_deleted_documents(base: str, token: str, kb_id: str) -> list[dict]:
    status, body = _request("GET", f"{base}/api/documents/deleted", token=token, kb_id=kb_id)
    if status != 200:
        raise RuntimeError(f"LIST_DELETED_FAIL status={status} body={body}")
    data = _extract_data(body)
    items = data.get("items", []) if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _delete_document(
    base: str,
    token: str,
    kb_id: str,
    doc_id: str,
    *,
    purge_graph: bool,
    soft_delete: bool,
    dry_run: bool,
    verify_after: bool,
) -> dict:
    query = urllib.parse.urlencode(
        {
            "purge_graph": str(bool(purge_graph)).lower(),
            "soft_delete": str(bool(soft_delete)).lower(),
            "dry_run": str(bool(dry_run)).lower(),
            "verify_after": str(bool(verify_after)).lower(),
        }
    )
    status, body = _request(
        "DELETE",
        f"{base}/api/documents/{doc_id}?{query}",
        token=token,
        kb_id=kb_id,
    )
    if status != 200:
        raise RuntimeError(f"DELETE_DOC_FAIL status={status} body={body}")
    data = _extract_data(body)
    if not isinstance(data, dict):
        raise RuntimeError(f"DELETE_DOC_INVALID body={body}")
    return data


def _restore_document(base: str, token: str, kb_id: str, doc_id: str) -> dict:
    status, body = _request(
        "POST",
        f"{base}/api/documents/{doc_id}/restore",
        token=token,
        kb_id=kb_id,
    )
    if status != 200:
        raise RuntimeError(f"RESTORE_FAIL status={status} body={body}")
    data = _extract_data(body)
    if not isinstance(data, dict):
        raise RuntimeError(f"RESTORE_INVALID body={body}")
    return data


def main() -> int:
    base = os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
    username = os.getenv("ADMIN_EMAIL", "yh@qs.al")
    password = os.getenv("ADMIN_PASSWORD")
    token = os.getenv("ADMIN_TOKEN")

    if not token:
        if not password:
            print("MISSING_ADMIN_PASSWORD")
            return 1
        token = _login(base, username, password)

    kb_id = require_active_kb("check_documents_soft_delete_flow.py", base, token)

    stamp = int(time.time() * 1000)
    test_name = f"gi_soft_delete_smoke_{stamp}_{uuid.uuid4().hex[:8]}.txt"
    doc_id = _upload_document(
        base,
        token,
        kb_id,
        test_name,
        f"GraphInsight soft delete smoke document {stamp}\n".encode("utf-8"),
    )
    print(f"UPLOAD_OK doc_id={doc_id} name={test_name}")

    try:
        docs = _list_documents(base, token, kb_id)
        print(f"DOCS_AFTER_UPLOAD count={len(docs)}")
        if not any(str(item.get("id")) == doc_id for item in docs):
            print(f"UPLOADED_DOC_NOT_IN_LIST doc_id={doc_id}")
            return 1

        preview = _delete_document(
            base,
            token,
            kb_id,
            doc_id,
            purge_graph=False,
            soft_delete=True,
            dry_run=True,
            verify_after=False,
        )
        if not bool(preview.get("dry_run")):
            print(f"DRY_RUN_FLAG_INVALID data={preview}")
            return 1
        print("DRY_RUN_OK")

        deleted = _delete_document(
            base,
            token,
            kb_id,
            doc_id,
            purge_graph=False,
            soft_delete=True,
            dry_run=False,
            verify_after=True,
        )
        if str(deleted.get("file_action")) != "soft_deleted":
            print(f"SOFT_DELETE_ACTION_INVALID data={deleted}")
            return 1
        print("SOFT_DELETE_OK")

        deleted_items = _list_deleted_documents(base, token, kb_id)
        if not any(str(item.get("doc_id")) == doc_id for item in deleted_items):
            print("DELETED_LIST_MISSING_DOC")
            return 1
        print(f"DELETED_LIST_OK count={len(deleted_items)}")

        restored = _restore_document(base, token, kb_id, doc_id)
        restored_doc_id = str(restored.get("doc_id") or "")
        if not restored_doc_id:
            print(f"RESTORE_DOC_ID_INVALID data={restored}")
            return 1
        print(f"RESTORE_OK new_doc_id={restored_doc_id}")

        cleanup = _delete_document(
            base,
            token,
            kb_id,
            restored_doc_id,
            purge_graph=False,
            soft_delete=False,
            dry_run=False,
            verify_after=False,
        )
        if str(cleanup.get("file_action")) != "hard_deleted":
            print(f"CLEANUP_ACTION_INVALID data={cleanup}")
            return 1
        print("CLEANUP_OK")

        print("DOCUMENTS_SOFT_DELETE_FLOW_OK")
        return 0
    finally:
        # 失败路径也不能把夹具留在活动知识库里；成功路径此处是幂等补刀，明确打印结果。
        try:
            _delete_document(
                base,
                token,
                kb_id,
                doc_id,
                purge_graph=False,
                soft_delete=False,
                dry_run=False,
                verify_after=False,
            )
            print("CLEANUP_SWEEP_DONE")
        except Exception as exc:  # noqa: BLE001
            print(f"CLEANUP_SWEEP_NOOP detail={exc}")


if __name__ == "__main__":
    raise SystemExit(main())
