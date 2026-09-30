"""
临时本地种子脚本（统一活栈 E2E 前置数据）：
  1. 创建/更新 admin 用户（email 登录，bcrypt 哈希）
  2. 绑定 super_admin@global（使 business guard 第一阶段与 AuthorizedKBIDs 全量放行）
  3. 确保存在一个 active 知识库（业务面 KB 目录 / E2E resolveTestKbId 依赖）
  4. 播种后自证：重新读取哈希做 bcrypt.checkpw、确认 super_admin 绑定与 active KB
     仍然成立；给定 VERIFY_BASE_URL 时再走真实 HTTP 登录确认账号可用。
     自证失败一律非零退出，禁止靠人工 SQL 兜底。全程不输出密码、token 或哈希内容。

用法（PowerShell）:
  $env:ADMIN_DATABASE_URL="postgresql://..."
  $env:SEED_ADMIN_PASSWORD="***"
  python -X utf8 backend/scripts/seed_e2e_local_stack.py

可选自证登录（统一活栈已起时推荐）:
  $env:SEED_VERIFY_BASE_URL="http://127.0.0.1:18082"
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt
from sqlalchemy import text

from admin.database import SessionLocal

ADMIN_EMAIL = os.getenv("SEED_ADMIN_EMAIL", "e2e-admin@local.test")
ADMIN_USERNAME = os.getenv("SEED_ADMIN_USERNAME", "e2e-admin")
ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "")
KB_NAME = os.getenv("SEED_KB_NAME", "E2E Test KB")
KB_TENANT = os.getenv("SEED_KB_TENANT", "default")
KB_PROJECT = os.getenv("SEED_KB_PROJECT", "default")
VERIFY_BASE_URL = os.getenv("SEED_VERIFY_BASE_URL", "").strip()


def _request_json(req: urllib.request.Request) -> tuple[int, dict]:
    """返回 (HTTP 状态, 响应体)；不可达返回 (0, {})。

    4xx/5xx 也带统一响应信封，必须把响应体读出来才能区分 INVALID_CREDENTIALS 与
    KB_ACCESS_DENIED，否则种子自证只能报告"请求失败"而无法定位是网关不可达还是权限不足。
    """
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, {}


def _login_and_list_kbs(kb_id: str) -> bool:
    """真实 HTTP 登录 + 读取业务 KB 目录，确认账号确实能看到目标 active KB。

    token 只在内存里用于一次请求，绝不打印；失败细节只输出状态码与错误码。
    """
    body = json.dumps(
        {"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    ).encode("utf-8")
    login_req = urllib.request.Request(
        VERIFY_BASE_URL.rstrip("/") + "/api/v1/admin/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    status, payload = _request_json(login_req)
    if status == 0:
        print("! login verification did not reach the gateway")
        return False
    data = payload.get("data") or {}
    token = (data.get("token") or "").strip()
    if status != 200 or payload.get("code") != 200 or not token:
        print(
            f"! login verification rejected: http={status} code={payload.get('code')} "
            f"error_code={data.get('error_code', 'n/a')}"
        )
        return False

    kb_req = urllib.request.Request(
        VERIFY_BASE_URL.rstrip("/") + "/api/knowledge-bases",
        headers={"Authorization": f"Bearer {token}"},
    )
    kb_status, kb_payload = _request_json(kb_req)
    if kb_status != 200 or kb_payload.get("code") != 200:
        print(
            f"! kb directory rejected: http={kb_status} code={kb_payload.get('code')} "
            f"error_code={(kb_payload.get('data') or {}).get('error_code', 'n/a')}"
        )
        return False

    items = ((kb_payload.get("data") or {}).get("items")) or []
    visible = {
        item.get("kb_id")
        for item in items
        if item.get("status") == "active"
    }
    if kb_id not in visible:
        print(
            f"! seeded KB not visible to the test account: active_count={len(visible)}"
        )
        return False
    print(
        f"✓ login + KB scope verified over HTTP: active_kb_visible=1 "
        f"account_active_kbs={len(visible)}"
    )
    return True


def main() -> int:
    password_bytes = len(ADMIN_PASSWORD.encode("utf-8"))
    # 网关登录校验要求密码 6-100 字节，bcrypt 只在 72 字节内可靠还原；
    # 不在此拦截会播种出一个"能写库但登录返回 400 INVALID_BODY"的账号。
    if password_bytes < 6 or password_bytes > 72:
        print("! SEED_ADMIN_PASSWORD must be 6-72 bytes (gateway login rejects <6)")
        return 1

    db = SessionLocal()
    try:
        # 1. admin user
        user_id = db.execute(
            text("SELECT id FROM admin_users WHERE email = :email"),
            {"email": ADMIN_EMAIL},
        ).scalar()
        password_hash = bcrypt.hashpw(
            ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")
        if user_id:
            db.execute(
                text(
                    "UPDATE admin_users SET password_hash = :h, is_active = TRUE "
                    "WHERE id = :id"
                ),
                {"h": password_hash, "id": user_id},
            )
            print(f"~ admin user updated: id={user_id} ({ADMIN_EMAIL})")
        else:
            user_id = db.execute(
                text(
                    "INSERT INTO admin_users (username, email, password_hash, "
                    "is_active, preferred_home_path, created_at, updated_at) "
                    "VALUES (:u, :e, :h, TRUE, '/admin/dashboard', now(), now()) "
                    "RETURNING id"
                ),
                {"u": ADMIN_USERNAME, "e": ADMIN_EMAIL, "h": password_hash},
            ).scalar()
            print(f"+ admin user created: id={user_id} ({ADMIN_EMAIL})")

        # 2. super_admin@global binding
        role_id = db.execute(
            text("SELECT id FROM admin_roles WHERE name = 'super_admin'")
        ).scalar()
        if not role_id:
            print("! super_admin role missing; run migrate_rbac_core.py first")
            return 1
        bound = db.execute(
            text(
                "SELECT 1 FROM admin_user_role_bindings WHERE user_id = :u "
                "AND role_id = :r AND scope_type = 'global'"
            ),
            {"u": user_id, "r": role_id},
        ).scalar()
        if not bound:
            db.execute(
                text(
                    "INSERT INTO admin_user_role_bindings (user_id, role_id, "
                    "scope_type, created_at) VALUES (:u, :r, 'global', now())"
                ),
                {"u": user_id, "r": role_id},
            )
            print(f"+ super_admin@global bound to user {user_id}")
        else:
            print("~ super_admin@global binding already exists")

        # 3. active knowledge base
        kb_count = db.execute(
            text("SELECT COUNT(*) FROM knowledge_bases WHERE status = 'active'")
        ).scalar()
        if kb_count and kb_count > 0:
            kb_id = db.execute(
                text(
                    "SELECT id FROM knowledge_bases WHERE status = 'active' "
                    "ORDER BY created_at LIMIT 1"
                )
            ).scalar()
            print(f"~ {kb_count} active KB already exists, reuse id={kb_id}")
        else:
            kb_id = str(uuid.uuid4())
            db.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, "
                    "slug, status, storage_prefix, created_by, created_at, updated_at) "
                    "VALUES (:id, :t, :p, :n, :s, 'active', :sp, :c, now(), now())"
                ),
                {
                    "id": kb_id,
                    "t": KB_TENANT,
                    "p": KB_PROJECT,
                    "n": KB_NAME,
                    "s": "e2e-test-kb",
                    "sp": f"kb/{kb_id}",
                    "c": user_id,
                },
            )
            print(f"+ active KB seeded: id={kb_id} name={KB_NAME}")

        db.commit()

        # 4. 自证：用全新会话重读，确认提交真的落库（不复用同一 session 的 identity map）
        verify_db = SessionLocal()
        try:
            stored_hash = verify_db.execute(
                text("SELECT password_hash FROM admin_users WHERE id = :id"),
                {"id": user_id},
            ).scalar()
            binding_ok = bool(verify_db.execute(
                text(
                    "SELECT 1 FROM admin_user_role_bindings WHERE user_id = :u "
                    "AND role_id = :r AND scope_type = 'global'"
                ),
                {"u": user_id, "r": role_id},
            ).scalar())
            kb_status = verify_db.execute(
                text("SELECT status FROM knowledge_bases WHERE id = :id"),
                {"id": kb_id},
            ).scalar()
        finally:
            verify_db.close()

        if not stored_hash or not bcrypt.checkpw(
            ADMIN_PASSWORD.encode("utf-8"), stored_hash.encode("utf-8")
        ):
            print("! seed verification failed: password checkpw mismatch after commit")
            return 1
        print("✓ password checkpw verified after commit")

        if not binding_ok:
            print("! seed verification failed: super_admin@global binding missing")
            return 1
        if kb_status != "active":
            print(f"! seed verification failed: KB status={kb_status!r}")
            return 1
        print(f"✓ binding and active KB verified: kb_id={kb_id}")

        if VERIFY_BASE_URL:
            if not _login_and_list_kbs(kb_id):
                return 1
        else:
            print("~ SEED_VERIFY_BASE_URL not set: HTTP login check skipped")

        print("✓ seed done")
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"! seed failed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
