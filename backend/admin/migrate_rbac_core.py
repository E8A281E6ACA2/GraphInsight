"""
RBAC 核心迁移脚本

用途：
1. 创建 AUTH-001 所需 RBAC 表
2. 初始化系统角色和权限
3. 为首个管理员授予 super_admin@global（若未绑定）
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import inspect, text
from dotenv import find_dotenv, load_dotenv

# 添加项目根目录到 Python 路径
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import Base, SessionLocal, engine
from admin.models import (  # noqa: F401 - 确保模型被导入注册到 metadata
    AdminPermission,
    AdminRole,
    AdminRolePermission,
    AdminUser,
    AdminUserRoleBinding,
)
from admin.services.authz_service import authz_service

load_dotenv(find_dotenv(), override=True)

RBAC_TABLES = (
    AdminRole.__table__,
    AdminPermission.__table__,
    AdminRolePermission.__table__,
    AdminUserRoleBinding.__table__,
)


def _safe_db_url(raw: str) -> str:
    if "@" not in raw:
        return raw
    left, right = raw.split("@", 1)
    if "://" in left and ":" in left.split("://", 1)[1]:
        prefix, account = left.split("://", 1)
        username = account.split(":", 1)[0]
        return f"{prefix}://{username}:****@{right}"
    return raw


def _table_exists(table_name: str) -> bool:
    inspector = inspect(engine)
    return inspector.has_table(table_name)


def _print_plan(action: str) -> None:
    print("-" * 60)
    print(f"计划动作: {action}")
    if action == "migrate":
        print("- ensure RBAC tables: admin_roles, admin_permissions, admin_role_permissions, admin_user_role_bindings")
        print("- seed system roles and permissions")
        print("- grant first admin user super_admin@global if needed")
    else:
        print("- drop table admin_user_role_bindings")
        print("- drop table admin_role_permissions")
        print("- drop table admin_permissions")
        print("- drop table admin_roles")
    print("-" * 60)


def create_rbac_tables() -> None:
    print("创建 RBAC 表...")
    Base.metadata.create_all(bind=engine, tables=list(RBAC_TABLES))
    print("✓ RBAC 表创建完成（若已存在则跳过）")


def seed_roles_and_permissions() -> None:
    db = SessionLocal()
    try:
        authz_service.ensure_seed_data(db)
        print("✓ 系统角色和权限种子数据初始化完成")
    finally:
        db.close()


def grant_super_admin_to_first_user() -> None:
    if not _table_exists("admin_users"):
        print("! admin_users 表不存在，跳过默认角色绑定")
        return

    db = SessionLocal()
    try:
        # 只投影绑定角色所需的列：legacy admin_users 可能缺后续迁移新增的列
        # （如 preferred_home_path），整行加载会让本迁移在老库上因无关列缺失而失败。
        first_user = (
            db.query(AdminUser.id, AdminUser.username)
            .order_by(AdminUser.id.asc())
            .first()
        )
        if not first_user:
            print("! 未找到管理员用户，跳过默认角色绑定")
            return

        exists = (
            db.query(AdminUserRoleBinding)
            .join(AdminRole, AdminRole.id == AdminUserRoleBinding.role_id)
            .filter(
                AdminUserRoleBinding.user_id == first_user.id,
                AdminRole.name == "super_admin",
                AdminUserRoleBinding.scope_type == "global",
            )
            .first()
        )
        if exists:
            print(f"✓ 用户 {first_user.username} 已存在 super_admin@global 绑定")
            return

        authz_service.assign_role_binding(
            db=db,
            user_id=first_user.id,
            role_name="super_admin",
            scope_type="global",
            created_by=first_user.id,
        )
        print(f"✓ 已授予 {first_user.username} super_admin@global")
    finally:
        db.close()


def drop_rbac_tables() -> None:
    print("回滚 RBAC 表...")
    with engine.begin() as conn:
        for table in reversed(RBAC_TABLES):
            if _table_exists(table.name):
                table.drop(bind=conn, checkfirst=True)
                print(f"✓ 已删除表: {table.name}")
            else:
                print(f"✓ 表已不存在: {table.name}")


def verify_summary(action: str) -> None:
    if action == "rollback":
        print("-" * 60)
        for table in RBAC_TABLES:
            state = "present" if _table_exists(table.name) else "absent"
            print(f"{table.name}: {state}")
        print("-" * 60)
        return

    with engine.connect() as conn:
        role_count = conn.execute(text("SELECT COUNT(*) FROM admin_roles")).scalar() or 0
        perm_count = conn.execute(text("SELECT COUNT(*) FROM admin_permissions")).scalar() or 0
        role_perm_count = conn.execute(text("SELECT COUNT(*) FROM admin_role_permissions")).scalar() or 0
        binding_count = conn.execute(text("SELECT COUNT(*) FROM admin_user_role_bindings")).scalar() or 0
        print("-" * 60)
        print(f"roles: {role_count}")
        print(f"permissions: {perm_count}")
        print(f"role_permissions: {role_perm_count}")
        print(f"user_role_bindings: {binding_count}")
        print("-" * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate or rollback GraphInsight RBAC core tables")
    parser.add_argument("--action", choices=("migrate", "rollback"), default="migrate")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_url = os.getenv("ADMIN_DATABASE_URL", "未配置")
    print("=" * 60)
    print("GraphInsight RBAC 核心迁移")
    print("=" * 60)
    print(f"数据库: {_safe_db_url(db_url)}")
    print(f"方言: {engine.dialect.name}")
    _print_plan(args.action)

    if args.dry_run:
        print("✓ dry-run completed, database not modified")
        return 0

    try:
        if args.action == "migrate":
            create_rbac_tables()
            seed_roles_and_permissions()
            grant_super_admin_to_first_user()
        else:
            drop_rbac_tables()
        verify_summary(args.action)
        print(f"✓ RBAC {args.action} 完成")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"✗ RBAC {args.action} 失败: {str(exc)}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
