"""
API 依赖项
提供通用的认证和授权依赖
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..crud import user_crud, log_crud
from ..database import get_db
from ..models import AdminUser
from ..schemas.logs import LogCreate
from ..services import auth_service, authz_service
from core import AuthenticationException, get_logger
from services.scope_contract import resolve_search_target  # noqa: F401 - 契约 §2.4 严格解析入口

logger = get_logger()

security = HTTPBearer(auto_error=False)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_user_from_credentials(
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session,
    *,
    required: bool,
) -> Optional[AdminUser]:
    if credentials is None:
        if required:
            raise AuthenticationException("缺少认证凭证")
        return None

    token = credentials.credentials
    token_data = auth_service.verify_token(token)

    # Token 中存储邮箱，兼容旧 token 中可能的用户名
    user = user_crud.get_by_email(db, token_data.username) or user_crud.get_by_username(db, token_data.username)
    if not user:
        raise AuthenticationException("用户不存在")
    if not user.is_active:
        raise AuthenticationException("用户已被禁用")
    return user


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> AdminUser:
    """
    严格认证依赖（后台接口默认使用）
    """
    try:
        cached_user = getattr(request.state, "current_user", None)
        if cached_user is not None:
            return cached_user

        user = _resolve_user_from_credentials(credentials, db, required=True)
        assert user is not None
        request.state.current_user = user
        return user
    except AuthenticationException as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.error(f"认证失败: {str(exc)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_optional_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[AdminUser]:
    """
    可选认证依赖（业务接口灰度阶段使用）
    """
    if credentials is None:
        return None
    try:
        cached_user = getattr(request.state, "current_user", None)
        if cached_user is not None:
            return cached_user

        user = _resolve_user_from_credentials(credentials, db, required=False)
        if user is not None:
            request.state.current_user = user
        return user
    except AuthenticationException as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_client_ip(request: Request) -> Optional[str]:
    """获取客户端 IP 地址"""
    if request.client:
        return request.client.host
    return None


def get_user_agent(request: Request) -> Optional[str]:
    """获取 User Agent"""
    return request.headers.get("user-agent")


def resolve_request_scope(request: Request) -> Dict[str, Optional[str]]:
    """
    解析请求作用域（优先级：Header > Query）
    """
    def pick(header_key: str, query_key: str) -> Optional[str]:
        return request.headers.get(header_key) or request.query_params.get(query_key)

    return {
        "tenant_id": pick("x-tenant-id", "tenant_id"),
        "project_id": pick("x-project-id", "project_id"),
        "kb_id": pick("x-kb-id", "kb_id"),
    }


def _split_scope_list(raw: Optional[str]) -> Optional[list]:
    """逗号分隔的多值 scope 头（如 x-kb-ids: kb-a,kb-b）。"""
    if raw is None:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or None


def resolve_strict_search_target(request: Request, body: Optional[dict] = None) -> "SearchTarget":
    """契约 §2.4 的严格作用域解析（M1 提供，M2-M4 知识库端点接入）。

    header/query/body 同时携带同一 scope 且归一后不一致 → KB_CROSS_SCOPE；
    kb 作用域完全缺失 → KB_SCOPE_REQUIRED（无 default 兜底）；
    格式非法 → SCOPE_INVALID。返回 services.scope_contract.SearchTarget。
    """
    header = {
        "tenant_id": request.headers.get("x-tenant-id"),
        "project_id": request.headers.get("x-project-id"),
        "kb_id": request.headers.get("x-kb-id"),
        "kb_ids": _split_scope_list(request.headers.get("x-kb-ids")),
    }
    query = {
        "tenant_id": request.query_params.get("tenant_id"),
        "project_id": request.query_params.get("project_id"),
        "kb_id": request.query_params.get("kb_id"),
        "kb_ids": _split_scope_list(request.query_params.get("kb_ids")),
    }
    return resolve_search_target(header=header, query=query, body=body)


def _write_authz_log(
    db: Session,
    *,
    user_id: Optional[int],
    request: Request,
    permission: str,
    resource: str,
    status_value: str,
    reason: str,
    scope: Dict[str, Optional[str]],
) -> None:
    try:
        log_crud.create(
            db,
            LogCreate(
                user_id=user_id,
                operator_id=user_id,
                tenant_id=scope.get("tenant_id"),
                trace_id=getattr(request.state, "trace_id", None),
                action="authz_check",
                resource=resource,
                details={
                    "permission": permission,
                    "reason": reason,
                    "scope": scope,
                    "path": str(request.url.path),
                    "method": request.method,
                },
                ip_address=get_client_ip(request),
                user_agent=get_user_agent(request),
                status=status_value,
                error_message=None if status_value == "success" else reason,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入授权审计日志失败", context={"error": str(exc)})


def require_permission(permission_code: str, resource: str = "business"):
    """
    权限守卫

    环境变量：
    - RBAC_ENFORCE_BUSINESS_API=true：强制要求登录并严格权限校验（默认）
    - RBAC_ENFORCE_BUSINESS_API=false：仅用于本地迁移诊断，允许匿名访问
    """

    async def dependency(
        request: Request,
        db: Session = Depends(get_db),
        current_user: Optional[AdminUser] = Depends(get_optional_current_user),
    ) -> Optional[AdminUser]:
        enforce = _env_bool("RBAC_ENFORCE_BUSINESS_API", True)

        if current_user is None:
            if enforce:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="缺少认证凭证",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.authz = {
                "permission": permission_code,
                "allowed": True,
                "reason": "legacy_allow_anonymous",
            }
            return None

        scope = resolve_request_scope(request)
        try:
            allowed, reason, matched = authz_service.check_permission(
                db,
                user_id=current_user.id,
                permission_code=permission_code,
                request_scope=scope,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("授权检查失败", context={"error": str(exc), "permission": permission_code})
            if enforce:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="授权服务不可用",
                )
            allowed, reason, matched = True, "legacy_allow_authz_error", None

        if not allowed:
            _write_authz_log(
                db,
                user_id=current_user.id,
                request=request,
                permission=permission_code,
                resource=resource,
                status_value="failed",
                reason=reason,
                scope=scope,
            )
            if enforce:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="权限不足",
                )

            # 灰度模式：仅记录，不拦截
            request.state.authz = {
                "permission": permission_code,
                "allowed": True,
                "reason": f"legacy_soft_allow:{reason}",
            }
            return current_user

        request.state.authz = {
            "permission": permission_code,
            "allowed": True,
            "reason": reason,
            "scope": scope,
            "binding": matched,
        }
        _write_authz_log(
            db,
            user_id=current_user.id,
            request=request,
            permission=permission_code,
            resource=resource,
            status_value="success",
            reason=reason,
            scope=scope,
        )
        return current_user

    return dependency


def require_admin_permission(permission_code: str, resource: str = "admin"):
    """
    后台权限守卫（严格模式，始终拦截）
    """

    async def dependency(
        request: Request,
        db: Session = Depends(get_db),
        current_user: AdminUser = Depends(get_current_user),
    ) -> AdminUser:
        scope = resolve_request_scope(request)
        try:
            allowed, reason, matched = authz_service.check_permission(
                db,
                user_id=current_user.id,
                permission_code=permission_code,
                request_scope=scope,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("后台授权检查失败", context={"error": str(exc), "permission": permission_code})
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="授权服务不可用",
            )

        if not allowed:
            _write_authz_log(
                db,
                user_id=current_user.id,
                request=request,
                permission=permission_code,
                resource=resource,
                status_value="failed",
                reason=reason,
                scope=scope,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足",
            )

        request.state.authz = {
            "permission": permission_code,
            "allowed": True,
            "reason": reason,
            "scope": scope,
            "binding": matched,
        }
        _write_authz_log(
            db,
            user_id=current_user.id,
            request=request,
            permission=permission_code,
            resource=resource,
            status_value="success",
            reason=reason,
            scope=scope,
        )
        return current_user

    return dependency
