"""
授权服务（RBAC + Scope）
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import (
    AdminPermission,
    AdminRole,
    AdminRolePermission,
    AdminUserRoleBinding,
)
from core import get_logger

logger = get_logger()

SCOPE_GLOBAL = "global"
SCOPE_TENANT = "tenant"
SCOPE_PROJECT = "project"
SCOPE_KB = "kb"

SYSTEM_ROLES = {
    "super_admin": "系统超级管理员",
    "project_admin": "项目管理员",
    "operator": "运营操作员",
    "viewer": "只读用户",
}

PERMISSION_DEFS = [
    {"code": "graph:read", "resource_type": "graph", "action": "read", "description": "图谱查询与查看"},
    {"code": "graph:build", "resource_type": "graph", "action": "build", "description": "图谱构建与重建"},
    # 与 go-backend/internal/adminstore/rbac_seed.go 保持一致：/api/query 强制 graph:admin。
    {"code": "graph:admin", "resource_type": "graph", "action": "admin", "description": "原始 Cypher 诊断查询（受限）"},
    {"code": "kb:read", "resource_type": "kb", "action": "read", "description": "知识库读取"},
    {"code": "kb:write", "resource_type": "kb", "action": "write", "description": "知识库写入"},
    {"code": "kb:delete", "resource_type": "kb", "action": "delete", "description": "知识库删除"},
    # 预留权限码：仅注册进目录，不参与任何强制校验，也不授予任何角色。
    {"code": "kb:review", "resource_type": "kb", "action": "review", "description": "知识库审核（预留）"},
    {"code": "kb:manage", "resource_type": "kb", "action": "manage", "description": "知识库治理（预留）"},
    {"code": "kb:publish", "resource_type": "kb", "action": "publish", "description": "知识库发布（预留）"},
    {"code": "qa:ask", "resource_type": "qa", "action": "ask", "description": "文档问答"},
    {"code": "nl2cypher:use", "resource_type": "nl2cypher", "action": "use", "description": "自然语言转 Cypher"},
    {"code": "config:read", "resource_type": "config", "action": "read", "description": "配置读取"},
    {"code": "config:write", "resource_type": "config", "action": "write", "description": "配置写入"},
    {"code": "logs:read", "resource_type": "logs", "action": "read", "description": "日志读取"},
    {"code": "logs:clean", "resource_type": "logs", "action": "clean", "description": "日志清理"},
    {"code": "monitor:read", "resource_type": "monitor", "action": "read", "description": "监控读取"},
    {"code": "user:manage", "resource_type": "user", "action": "manage", "description": "用户和权限管理"},
    {"code": "job:read", "resource_type": "job", "action": "read", "description": "任务读取"},
    {"code": "job:manage", "resource_type": "job", "action": "manage", "description": "任务管理"},
]

ROLE_PERMISSION_CODES = {
    # super_admin 显式列权限码而不是"目录全集"，否则预留码会被隐式授予，
    # 与 Go 侧 rbac_seed.go 的授予口径产生分歧。
    "super_admin": [
        "graph:read",
        "graph:build",
        "graph:admin",
        "kb:read",
        "kb:write",
        "kb:delete",
        "qa:ask",
        "nl2cypher:use",
        "config:read",
        "config:write",
        "logs:read",
        "logs:clean",
        "monitor:read",
        "user:manage",
        "job:read",
        "job:manage",
    ],
    "project_admin": [
        "graph:read",
        "graph:build",
        "kb:read",
        "kb:write",
        "kb:delete",
        "qa:ask",
        "nl2cypher:use",
        "config:read",
        "logs:read",
        "monitor:read",
        "job:read",
        "job:manage",
    ],
    "operator": [
        "graph:read",
        "graph:build",
        "kb:read",
        "kb:write",
        "qa:ask",
        "nl2cypher:use",
        "job:read",
    ],
    "viewer": [
        "graph:read",
        "kb:read",
        "qa:ask",
        "nl2cypher:use",
        "job:read",
    ],
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AuthzService:
    """RBAC 授权服务"""

    def __init__(self) -> None:
        self.cache_ttl_seconds = int(os.getenv("RBAC_CACHE_TTL_SECONDS", "60"))
        self.rbac_enabled = _env_bool("RBAC_ENABLE", True)
        self.fail_open_when_unbound = _env_bool("RBAC_FAIL_OPEN_WHEN_UNBOUND", False)
        self._cache: Dict[int, Tuple[float, List[dict]]] = {}
        self._seed_checked = False

    def invalidate_user_cache(self, user_id: int) -> None:
        self._cache.pop(user_id, None)

    def invalidate_all_cache(self) -> None:
        self._cache.clear()

    def ensure_seed_data(self, db: Session) -> None:
        """确保系统角色和权限已初始化。"""
        if self._seed_checked:
            return

        try:
            role_map: Dict[str, AdminRole] = {}
            for role_name, role_desc in SYSTEM_ROLES.items():
                role = db.query(AdminRole).filter(AdminRole.name == role_name).first()
                if not role:
                    role = AdminRole(
                        name=role_name,
                        description=role_desc,
                        is_system=True,
                    )
                    db.add(role)
                    db.flush()
                role_map[role_name] = role

            permission_map: Dict[str, AdminPermission] = {}
            for item in PERMISSION_DEFS:
                perm = db.query(AdminPermission).filter(AdminPermission.code == item["code"]).first()
                if not perm:
                    perm = AdminPermission(
                        code=item["code"],
                        resource_type=item["resource_type"],
                        action=item["action"],
                        description=item["description"],
                    )
                    db.add(perm)
                    db.flush()
                permission_map[item["code"]] = perm

            for role_name, permission_codes in ROLE_PERMISSION_CODES.items():
                role = role_map.get(role_name)
                if not role:
                    continue
                for code in permission_codes:
                    perm = permission_map.get(code)
                    if not perm:
                        continue
                    exists = (
                        db.query(AdminRolePermission)
                        .filter(
                            AdminRolePermission.role_id == role.id,
                            AdminRolePermission.permission_id == perm.id,
                        )
                        .first()
                    )
                    if not exists:
                        db.add(
                            AdminRolePermission(
                                role_id=role.id,
                                permission_id=perm.id,
                            )
                        )

            db.commit()
            self._seed_checked = True
        except Exception:
            db.rollback()
            logger.error("初始化 RBAC 种子数据失败", exc_info=True)
            raise

    def assign_role_binding(
        self,
        db: Session,
        *,
        user_id: int,
        role_name: str,
        scope_type: str = SCOPE_GLOBAL,
        tenant_id: Optional[str] = None,
        project_id: Optional[str] = None,
        kb_id: Optional[str] = None,
        created_by: Optional[int] = None,
    ) -> AdminUserRoleBinding:
        self.ensure_seed_data(db)
        role = db.query(AdminRole).filter(AdminRole.name == role_name).first()
        if role is None:
            raise ValueError(f"角色不存在: {role_name}")

        exists = (
            db.query(AdminUserRoleBinding)
            .filter(
                AdminUserRoleBinding.user_id == user_id,
                AdminUserRoleBinding.role_id == role.id,
                AdminUserRoleBinding.scope_type == scope_type,
                AdminUserRoleBinding.tenant_id == tenant_id,
                AdminUserRoleBinding.project_id == project_id,
                AdminUserRoleBinding.kb_id == kb_id,
            )
            .first()
        )
        if exists:
            return exists

        binding = AdminUserRoleBinding(
            user_id=user_id,
            role_id=role.id,
            scope_type=scope_type,
            tenant_id=tenant_id,
            project_id=project_id,
            kb_id=kb_id,
            created_by=created_by,
        )
        db.add(binding)
        db.commit()
        db.refresh(binding)
        self.invalidate_user_cache(user_id)
        return binding

    def get_user_permission_bindings(self, db: Session, user_id: int) -> List[dict]:
        now = time.time()
        cached = self._cache.get(user_id)
        if cached and cached[0] > now:
            return cached[1]

        self.ensure_seed_data(db)
        rows = (
            db.query(
                AdminPermission.code.label("permission_code"),
                AdminRole.name.label("role_name"),
                AdminUserRoleBinding.scope_type.label("scope_type"),
                AdminUserRoleBinding.tenant_id.label("tenant_id"),
                AdminUserRoleBinding.project_id.label("project_id"),
                AdminUserRoleBinding.kb_id.label("kb_id"),
            )
            .join(AdminRolePermission, AdminRolePermission.permission_id == AdminPermission.id)
            .join(AdminRole, AdminRole.id == AdminRolePermission.role_id)
            .join(AdminUserRoleBinding, AdminUserRoleBinding.role_id == AdminRole.id)
            .filter(AdminUserRoleBinding.user_id == user_id)
            .filter(
                or_(
                    AdminUserRoleBinding.expires_at.is_(None),
                    AdminUserRoleBinding.expires_at > datetime.utcnow(),
                )
            )
            .all()
        )
        bindings = [
            {
                "permission_code": row.permission_code,
                "role_name": row.role_name,
                "scope_type": row.scope_type,
                "tenant_id": row.tenant_id,
                "project_id": row.project_id,
                "kb_id": row.kb_id,
            }
            for row in rows
        ]

        self._cache[user_id] = (now + self.cache_ttl_seconds, bindings)
        return bindings

    @staticmethod
    def _scope_match(binding: dict, request_scope: dict) -> bool:
        scope_type = (binding.get("scope_type") or SCOPE_GLOBAL).lower()
        if scope_type == SCOPE_GLOBAL:
            return True
        if scope_type == SCOPE_TENANT:
            return bool(request_scope.get("tenant_id")) and binding.get("tenant_id") == request_scope.get("tenant_id")
        if scope_type == SCOPE_PROJECT:
            return bool(request_scope.get("project_id")) and binding.get("project_id") == request_scope.get("project_id")
        if scope_type == SCOPE_KB:
            return bool(request_scope.get("kb_id")) and binding.get("kb_id") == request_scope.get("kb_id")
        return False

    def check_permission(
        self,
        db: Session,
        *,
        user_id: int,
        permission_code: str,
        request_scope: Optional[dict] = None,
    ) -> Tuple[bool, str, Optional[dict]]:
        """
        返回 (allowed, reason, matched_binding)
        """
        if not self.rbac_enabled:
            return True, "rbac_disabled", None

        scope = request_scope or {}
        bindings = self.get_user_permission_bindings(db, user_id)
        if not bindings:
            if self.fail_open_when_unbound:
                return True, "legacy_allow_no_binding", None
            return False, "no_binding", None

        permission_bindings = [item for item in bindings if item["permission_code"] == permission_code]
        if not permission_bindings:
            return False, "permission_missing", None

        # 没有提供资源作用域时，命中任意作用域即可（兼容当前接口）
        if not any(scope.values()):
            return True, "allowed_without_scope", permission_bindings[0]

        for item in permission_bindings:
            if self._scope_match(item, scope):
                return True, "allowed", item

        return False, "scope_mismatch", None


authz_service = AuthzService()
