"""
Release Gate R1 项3：版本级回滚演练探针（仅测试/本地隔离栈，禁止生产）

演练分两阶段，两条不变量口径在整个演练中固定不变，不因回滚版本行为而调整：

  setup  只在当前发布版本执行一次，用真实 HTTP API 建立回滚后仍可复用的夹具：
         第二个知识库 kb-b、低权限用户（仅 viewer@kb-b）。夹具凭据写入仓库外的
         状态文件（0600，落在 git 顶层目录之外会被硬拒绝），绝不打印密码。
  probe  任意版本都可执行：主链路（健康/登录/KB 目录/上传+检索链路）+ 三条安全不变量
         （无默认 KB 兜底、无缺 kb_id 透传、无跨 KB 泄漏）+ 伪造入站身份头拒绝。

--authz-mode 记录本腿网关的授权配置形态（go_db=enforce / local_jwt_soft=soft）。
断言集合不随模式变化：soft 是"RBAC 业务面放行"的部署形态，不是安全不变量的豁免；
一条版本只有 enforce 与 soft 两腿都过，才算完成回滚验收（见 run_rollback_matrix.sh）。

问答探针口径：无 embedding/LLM 配置的隔离栈里，docqa 只验证"作用域解析 + 检索/引用
链路可用"（HTTP 200 且返回 citations 结构），不声明问答语义质量。

退出码：0=全部通过；1=存在失败；2=前置缺失（夹具/凭据/服务不可达）。

用法：
    ADMIN_EMAIL=e2e-admin@local.test \
    ADMIN_PASSWORD_FILE=/path/to/password \
    python backend/tests/run_rollback_drill.py --base-url http://127.0.0.1:18090 \
        --phase setup --state-file /path/to/drill_state.json

    python backend/tests/run_rollback_drill.py --base-url http://127.0.0.1:18090 \
        --phase probe --version-label 66e1d18 --state-file /path/to/drill_state.json
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

SCOPE_REQUIRED = "KB_SCOPE_REQUIRED"
ACCESS_DENIED = "KB_ACCESS_DENIED"
UNAUTHORIZED = "UNAUTHORIZED"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _assert_outside_repo(path: str, label: str) -> Path:
    """夹具与口令文件必须落在仓库外。

    演练状态文件里含低权限用户密码，写进工作树就有被提交或被 upload-artifact 带走的
    风险；这里用 git 顶层目录判定，命中仓库内直接硬失败，而不是静默换路径。
    """
    resolved = Path(path).resolve()
    root = _repo_root()
    try:
        top = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        ).stdout.strip() or str(root))
        root = top.resolve()
    except (OSError, ValueError):
        pass
    if resolved == root or root in resolved.parents:
        raise SystemExit(f"DRILL_PREREQ_INVALID {label} must live outside the repository: {resolved} under {root}")
    return resolved


def _write_private_file(path: Path, content: str, label: str) -> None:
    resolved = _assert_outside_repo(str(path), label)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(resolved), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(str(resolved), 0o600)
    mode = resolved.stat().st_mode & 0o777
    # Windows 的 st_mode 是模拟值（恒为 0o666 一类），权限由 ACL 决定，位检查只在 POSIX 上有效。
    if os.name == "posix" and mode & 0o077:
        raise SystemExit(f"DRILL_PREREQ_INVALID {label} is group/other readable: mode={oct(mode)}")
    print(f"{label.upper()}_PROTECTED path={resolved} mode={oct(mode)} posix_enforced={int(os.name == 'posix')}")


def _password_from_args(args: argparse.Namespace) -> str:
    if args.admin_password_file:
        return Path(args.admin_password_file).read_text(encoding="utf-8").strip()
    value = os.getenv("ADMIN_PASSWORD", "").strip()
    if not value:
        raise SystemExit("DRILL_PREREQ_MISSING admin password (ADMIN_PASSWORD / --admin-password-file)")
    return value


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    payload: Optional[dict] = None,
    headers: Optional[dict[str, str]] = None,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
) -> tuple[int, dict]:
    header = dict(headers or {})
    if token:
        header["Authorization"] = f"Bearer {token}"
    data: Optional[bytes] = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        header["Content-Type"] = "application/json"
    elif body is not None:
        data = body
        header["Content-Type"] = content_type or "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=header, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _envelope(raw)
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"_transport_error": type(exc).__name__}
    return resp.status, _envelope(raw)


def _envelope(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"_non_json": raw[:200]}
    return parsed if isinstance(parsed, dict) else {"_list": parsed}


def _error_code(status: int, body: dict) -> str:
    data = body.get("data")
    if isinstance(data, dict) and data.get("error_code"):
        return str(data["error_code"])
    if body.get("_transport_error"):
        return str(body["_transport_error"])
    return f"HTTP_{status}"


def _login(base_url: str, username: str, password: str) -> tuple[int, str, str]:
    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/auth/login",
        payload={"username": username, "password": password},
    )
    data = body.get("data")
    token = data.get("token", "") if isinstance(data, dict) else ""
    return status, token, _error_code(status, body)


def _multipart(files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----gi-drill-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for field, name, content in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{field}"; filename="{name}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: text/markdown\r\n\r\n")
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Drill:
    def __init__(self, base_url: str, version_label: str, authz_mode: str = "unknown") -> None:
        self.base_url = base_url.rstrip("/")
        self.version_label = version_label
        self.authz_mode = authz_mode
        self.passed = 0
        self.failed = 0

    def check(self, probe_id: str, ok: bool, detail: str) -> None:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        print(f"PROBE id={probe_id} verdict={'PASS' if ok else 'FAIL'} {detail}")


def _active_kb_ids(base_url: str, token: str) -> tuple[int, str, list[str]]:
    status, body = _request("GET", f"{base_url}/api/knowledge-bases", token=token)
    data = body.get("data")
    items = data.get("items", []) if isinstance(data, dict) else []
    visible = [
        str(item.get("kb_id"))
        for item in items
        if isinstance(item, dict) and str(item.get("status")) == "active" and item.get("kb_id")
    ]
    return status, _error_code(status, body), visible


def run_setup(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    admin_password = _password_from_args(args)
    status, token, code = _login(base_url, args.admin_email, admin_password)
    if status != 200 or not token:
        print(f"SETUP_BLOCKED login status={status} error_code={code}")
        return 2
    print("SETUP_LOGIN ok=1")

    status, code, visible = _active_kb_ids(base_url, token)
    if status != 200 or not visible:
        print(f"SETUP_BLOCKED kb_directory status={status} error_code={code}")
        return 2
    kb_a = visible[0]

    slug = f"drill-kb-b-{int(time.time())}"
    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/knowledge-bases",
        token=token,
        payload={
            "name": slug,
            "slug": slug,
            "description": "rollback drill restricted kb",
            "tenant_id": args.tenant_id,
            "project_id": args.project_id,
        },
    )
    data = body.get("data")
    kb_b = data.get("id") or data.get("kb_id") if isinstance(data, dict) else None
    if status not in {200, 201} or not kb_b:
        print(f"SETUP_BLOCKED create_kb_b status={status} error_code={_error_code(status, body)}")
        return 2
    print(f"SETUP_KB_B ok=1 kb_b={kb_b}")

    alphabet = string.ascii_letters + string.digits
    low_password = "".join(secrets.choice(alphabet) for _ in range(20)) + "a1"
    low_username = f"drill_viewer_{uuid.uuid4().hex[:8]}"
    low_email = f"{low_username}@drill.local.test"
    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/users",
        token=token,
        payload={
            "username": low_username,
            "email": low_email,
            "password": low_password,
            "full_name": "Rollback drill restricted user",
        },
    )
    data = body.get("data")
    low_user_id = data.get("id") if isinstance(data, dict) else None
    if status not in {200, 201} or not low_user_id:
        print(f"SETUP_BLOCKED create_low_user status={status} error_code={_error_code(status, body)}")
        return 2
    print(f"SETUP_LOW_USER ok=1 user_id={low_user_id} password_length={len(low_password)}")

    status, body = _request(
        "POST",
        f"{base_url}/api/v1/admin/rbac/bindings",
        token=token,
        payload={
            "user_id": int(low_user_id),
            "role_name": "viewer",
            "scope_type": "kb",
            "kb_id": str(kb_b),
        },
    )
    if status not in {200, 201}:
        print(f"SETUP_BLOCKED bind_viewer_kb status={status} error_code={_error_code(status, body)}")
        return 2
    print("SETUP_BINDING ok=1 scope=kb")

    state = {
        "base_url": base_url,
        "kb_a": str(kb_a),
        "kb_b": str(kb_b),
        "low_username": low_username,
        "low_email": low_email,
        "low_password": low_password,
        "created_at": int(time.time()),
    }
    _write_private_file(Path(args.state_file), json.dumps(state, indent=2, ensure_ascii=False), "state_file")
    print("SETUP_DONE")
    return 0


def run_probe(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    drill = Drill(base_url, args.version_label, args.authz_mode)
    state_path = _assert_outside_repo(args.state_file, "state_file")
    if not state_path.exists():
        print(f"DRILL_PREREQ_MISSING state_file={state_path}")
        return 2
    mode = state_path.stat().st_mode & 0o777
    if os.name == "posix" and mode & 0o077:
        print(f"DRILL_PREREQ_INVALID state_file is group/other readable: mode={oct(mode)}")
        return 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    kb_a, kb_b = state["kb_a"], state["kb_b"]
    print(f"DRILL_MODE authz_mode={args.authz_mode} state_mode={oct(mode)}")

    # ---- 主链路（开工令项3：健康 / 登录 / KB 目录 / 上传+问答） ----
    status, body = _request("GET", f"{base_url}/health")
    data = body.get("data") or {}
    drill.check(
        "main_health",
        status == 200 and (data.get("status") == "healthy"),
        f"status={status} service_version={data.get('version')}",
    )

    admin_password = _password_from_args(args)
    status, admin_token, code = _login(base_url, args.admin_email, admin_password)
    drill.check(
        "main_login",
        status == 200 and bool(admin_token),
        f"status={status} error_code={code} token_present={int(bool(admin_token))}",
    )
    if not admin_token:
        return _finish(drill)

    status, code, visible = _active_kb_ids(base_url, admin_token)
    drill.check(
        "main_kb_directory",
        status == 200 and kb_a in visible,
        f"status={status} error_code={code} kb_a_visible={int(kb_a in visible)} visible={len(visible)}",
    )

    doc_name = f"drill-{uuid.uuid4().hex[:8]}.md"
    doc_bytes = f"# rollback drill\n\nquestion anchor {uuid.uuid4().hex}\n".encode("utf-8")
    payload, content_type = _multipart([("files", doc_name, doc_bytes)])
    status, body = _request(
        "POST",
        f"{base_url}/api/documents/upload",
        token=admin_token,
        body=payload,
        content_type=content_type,
        headers={"x-kb-id": kb_a},
    )
    data = body.get("data") or {}
    uploaded = data.get("uploaded") if isinstance(data, dict) else None
    drill.check(
        "main_upload",
        status == 200 and bool(uploaded),
        f"status={status} error_code={_error_code(status, body)} uploaded={len(uploaded) if isinstance(uploaded, list) else 0}",
    )

    status, body = _request(
        "GET",
        f"{base_url}/api/documents?page_size=100",
        token=admin_token,
        headers={"x-kb-id": kb_a},
    )
    data = body.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else None
    listed = [
        str(item.get("original_name") or item.get("name") or "")
        for item in (items or [])
        if isinstance(item, dict)
    ]
    drill.check(
        "main_document_registry",
        status == 200 and doc_name in listed,
        f"status={status} error_code={_error_code(status, body)} docs={len(listed)}",
    )

    status, body = _request(
        "POST",
        f"{base_url}/api/docqa",
        token=admin_token,
        payload={"question": "what is the drill anchor?", "kb_id": kb_a},
    )
    data = body.get("data") or {}
    answer = data.get("answer") if isinstance(data, dict) else None
    served_in_scope = status == 200 and isinstance(answer, str) and isinstance(data.get("citations"), list)
    drill.check(
        "main_docqa",
        served_in_scope,
        f"status={status} error_code={_error_code(status, body)} citations={len(data.get('citations') or [])}",
    )
    # 隔离演练库默认无 embedding/LLM 配置：本探针只判定"作用域解析 + 检索/引用链路可用"，
    # 检索为空不判失败，也不得对外表述为问答语义质量通过。
    print(
        f"NOTE main_docqa_scope=link_smoke_only llm_configured=0 "
        f"retrieval_empty={int(not (data.get('citations') or []))}"
    )

    # ---- 不变量 1：无默认 KB 兜底（缺 kb_id 必须拒绝，不得服务任意 KB） ----
    # 各路由先校验请求体再解析作用域，因此探针必须携带合法 body，只省略 kb 作用域。
    scopeless_payloads = {
        "/api/docqa": {"question": "scope-less probe"},
        "/api/nl2cypher": {"natural_language": "scope-less probe", "context": {}},
        "/api/graph/build": {},
    }
    for route, payload in scopeless_payloads.items():
        probe_id = "no_default_kb" + route.replace("/", "_")
        status, body = _request(
            "POST",
            f"{base_url}{route}",
            token=admin_token,
            payload=payload,
        )
        code = _error_code(status, body)
        drill.check(probe_id, status == 400 and code == SCOPE_REQUIRED, f"status={status} error_code={code}")

    # ---- 不变量 2：无缺 kb_id 透传（不得降级为下游 5xx / 200） ----
    status, body = _request(
        "POST",
        f"{base_url}/api/docqa",
        token=admin_token,
        payload={"question": "scope mismatch probe", "kb_id": kb_a},
        headers={"x-kb-id": kb_b},
    )
    code = _error_code(status, body)
    drill.check(
        "no_kb_passthrough_cross_scope",
        status == 400 and code == "KB_CROSS_SCOPE",
        f"status={status} error_code={code}",
    )

    # ---- 不变量 3：无跨 KB 泄漏（低权限用户只能看到并命中 kb-b） ----
    status, body = _request("GET", f"{base_url}/api/documents?page_size=100", token=admin_token)
    code = _error_code(status, body)
    drill.check(
        "no_default_kb_documents_list",
        status == 400 and code == SCOPE_REQUIRED,
        f"status={status} error_code={code}",
    )

    status, low_token, code = _login(base_url, state["low_email"], state["low_password"])
    if status != 200 or not low_token:
        drill.check("cross_kb_leak_low_login", False, f"status={status} error_code={code}")
    else:
        drill.check("cross_kb_leak_low_login", True, f"status={status} token_present=1")
        status, code, low_visible = _active_kb_ids(base_url, low_token)
        drill.check(
            "cross_kb_leak_directory",
            status == 200 and kb_a not in low_visible and kb_b in low_visible,
            f"status={status} error_code={code} kb_a_visible={int(kb_a in low_visible)} kb_b_visible={int(kb_b in low_visible)}",
        )
        status, body = _request(
            "POST",
            f"{base_url}/api/docqa",
            token=low_token,
            payload={"question": "cross kb probe", "kb_id": kb_a},
        )
        code = _error_code(status, body)
        drill.check(
            "cross_kb_leak_docqa_denied",
            status == 403 and code == ACCESS_DENIED,
            f"status={status} error_code={code}",
        )

    # ---- 伪造入站身份头不得获得 KB 读取（M4-R1 复审口径） ----
    status, body = _request(
        "POST",
        f"{base_url}/api/docqa",
        payload={"question": "forged identity probe", "kb_id": kb_a},
        headers={"x-auth-user-name": args.admin_email, "x-kb-id": kb_a},
    )
    code = _error_code(status, body)
    drill.check(
        "forged_identity_header_rejected",
        status == 401 and code == UNAUTHORIZED,
        f"status={status} error_code={code}",
    )

    return _finish(drill)


def _finish(drill: Drill) -> int:
    result = "pass" if drill.failed == 0 else "fail"
    print(
        f"ROLLBACK_DRILL_SUMMARY version={drill.version_label} authz_mode={drill.authz_mode} "
        f"result={result} passed={drill.passed} failed={drill.failed}"
    )
    return 0 if drill.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("ADMIN_BASE_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--phase", choices=("setup", "probe"), required=True)
    parser.add_argument("--version-label", default="unknown")
    # 声明本腿网关是以哪种授权模式运行的，只影响汇总行的可读性：三条安全不变量在
    # enforce 与 soft 下断言集合完全一致，模式不是放宽断言的理由。
    parser.add_argument(
        "--authz-mode",
        choices=("enforce", "soft", "unknown"),
        default=os.getenv("DRILL_AUTHZ_MODE", "unknown"),
    )
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--admin-email", default=os.getenv("ADMIN_EMAIL", "e2e-admin@local.test"))
    parser.add_argument("--admin-password-file", default=os.getenv("ADMIN_PASSWORD_FILE"))
    parser.add_argument("--tenant-id", default=os.getenv("DRILL_TENANT_ID", "default"))
    parser.add_argument("--project-id", default=os.getenv("DRILL_PROJECT_ID", "default"))
    args = parser.parse_args()
    return run_setup(args) if args.phase == "setup" else run_probe(args)


if __name__ == "__main__":
    sys.exit(main())
