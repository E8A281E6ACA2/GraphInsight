"""
临时本地种子脚本（统一活栈 E2E 前置数据）：
  1. 创建/更新 admin 用户（email 登录，bcrypt 哈希）
  2. 绑定 super_admin@global（使 business guard 第一阶段与 AuthorizedKBIDs 全量放行）
  3. 确保存在一个 active 知识库（业务面 KB 目录 / E2E resolveTestKbId 依赖）

用法（PowerShell）:
  $env:ADMIN_DATABASE_URL="postgresql://..."
  $env:SEED_ADMIN_PASSWORD="***"
  python -X utf8 backend/scripts/seed_e2e_local_stack.py
"""
import os
import sys
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


def main() -> int:
    if len(ADMIN_PASSWORD.encode("utf-8")) > 72 or not ADMIN_PASSWORD:
        print("! SEED_ADMIN_PASSWORD missing or too long")
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
            print(f"~ {kb_count} active KB already exists, skip seeding")
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
