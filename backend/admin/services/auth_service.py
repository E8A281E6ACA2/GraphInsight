"""
认证服务
处理用户认证、Token 管理等业务逻辑
"""
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, ProgrammingError

from ..models import AdminUser
from ..crud import user_crud, log_crud
from ..schemas.auth import (
    LoginRequest,
    LoginResponse,
    UserInfo,
    TokenData,
    ChangePasswordRequest,
    RegisterRequest,
    RegisterResponse,
)
from ..schemas.logs import LogCreate
from core import (
    AuthenticationException,
    ValidationException,
    ErrorCode,
    get_logger,
)

logger = get_logger()

# JWT 配置
SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24


class AuthService:
    """认证服务类"""
    
    def __init__(self):
        self.secret_key = SECRET_KEY
        self.algorithm = ALGORITHM
        self.token_expire_hours = ACCESS_TOKEN_EXPIRE_HOURS
    
    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """验证密码"""
        try:
            return bcrypt.checkpw(
                plain_password.encode('utf-8'),
                hashed_password.encode('utf-8')
            )
        except Exception as e:
            logger.error(f"密码验证失败: {str(e)}")
            return False
    
    def hash_password(self, password: str) -> str:
        """加密密码"""
        try:
            salt = bcrypt.gensalt()
            hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
            return hashed.decode('utf-8')
        except Exception as e:
            logger.error(f"密码加密失败: {str(e)}")
            raise ValidationException("密码加密失败")
    
    def create_access_token(
        self,
        data: dict,
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """创建 JWT Token"""
        try:
            to_encode = data.copy()
            if expires_delta:
                expire = datetime.utcnow() + expires_delta
            else:
                expire = datetime.utcnow() + timedelta(hours=self.token_expire_hours)
            
            to_encode.update({"exp": expire})
            encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
            
            logger.debug(
                "创建 Token 成功",
                context={"username": data.get("sub"), "expire": expire.isoformat()}
            )
            
            return encoded_jwt
        except Exception as e:
            logger.error(f"创建 Token 失败: {str(e)}")
            raise AuthenticationException("Token 创建失败")
    
    def verify_token(self, token: str) -> TokenData:
        """验证 Token"""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            username: str = payload.get("sub")
            
            if username is None:
                raise AuthenticationException(
                    message="无效的认证凭证",
                    error_code=ErrorCode.INVALID_TOKEN
                )
            
            return TokenData(username=username)
        except JWTError as e:
            logger.warning(f"Token 验证失败: {str(e)}")
            raise AuthenticationException(
                message="Token 已过期或无效",
                error_code=ErrorCode.TOKEN_EXPIRED
            )
    
    def authenticate_user(
        self,
        db: Session,
        email: str,
        password: str
    ) -> Optional[AdminUser]:
        """认证用户 - 使用邮箱登录"""
        try:
            # 使用邮箱查询用户
            user = user_crud.get_by_email(db, email)
            if not user:
                logger.warning(f"用户不存在: {email}")
                return None
            
            # 验证密码
            if not self.verify_password(password, user.password_hash):
                logger.warning(f"密码错误: {email}")
                return None
            
            # 检查用户状态
            if not user.is_active:
                logger.warning(f"用户已被禁用: {email}")
                raise AuthenticationException(
                    message="用户已被禁用",
                    error_code=ErrorCode.USER_DISABLED
                )
            
            return user
        except AuthenticationException:
            raise
        except Exception as e:
            logger.error(f"用户认证失败: {str(e)}", exc_info=True)
            raise AuthenticationException("认证失败")
    
    def login(
        self,
        db: Session,
        login_request: LoginRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Tuple[LoginResponse, bool]:
        """
        用户登录 - 使用邮箱登录
        
        Returns:
            (LoginResponse, success: bool)
        """
        try:
            # 认证用户 - 使用邮箱
            user = self.authenticate_user(
                db,
                login_request.username,  # 这里的 username 实际上是邮箱
                login_request.password
            )
            
            if not user:
                # 记录失败日志
                log_crud.create(db, LogCreate(
                    action="login",
                    resource="user",
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    details={"email": login_request.username},
                    ip_address=ip_address,
                    user_agent=user_agent,
                    status="failed",
                    error_message="邮箱或密码错误"
                ))
                
                raise AuthenticationException(
                    message="邮箱或密码错误",
                    error_code=ErrorCode.INVALID_CREDENTIALS
                )
            
            # 创建 Token - 使用邮箱作为标识
            token = self.create_access_token(
                data={"sub": user.email}
            )
            
            # 更新登录信息
            user_crud.update_last_login(db, user.id)
            
            # 记录成功日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="login",
                resource="user",
                resource_id=str(user.id),
                details={"username": user.username},
                ip_address=ip_address,
                user_agent=user_agent,
                status="success"
            ))
            
            # 构造响应
            user_info = UserInfo.model_validate(user)
            response = LoginResponse(
                token=token,
                expires_in=self.token_expire_hours * 3600,
                user=user_info
            )
            
            logger.info(
                f"用户登录成功: {user.username}",
                context={
                    "user_id": user.id,
                    "ip_address": ip_address
                }
            )
            
            return response, True
            
        except AuthenticationException:
            raise
        except Exception as e:
            logger.error(f"登录失败: {str(e)}", exc_info=True)
            raise AuthenticationException("登录失败")
    
    def logout(
        self,
        db: Session,
        user: AdminUser,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        """用户登出"""
        try:
            # 记录登出日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="logout",
                resource="user",
                resource_id=str(user.id),
                ip_address=ip_address,
                user_agent=user_agent,
                status="success"
            ))
            
            logger.info(
                f"用户登出: {user.username}",
                context={"user_id": user.id}
            )
        except Exception as e:
            logger.error(f"登出失败: {str(e)}", exc_info=True)
    
    def change_password(
        self,
        db: Session,
        user: AdminUser,
        change_request: ChangePasswordRequest,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> bool:
        """修改密码"""
        try:
            # 验证旧密码
            if not self.verify_password(change_request.old_password, user.password_hash):
                raise AuthenticationException(
                    message="旧密码错误",
                    error_code=ErrorCode.INVALID_CREDENTIALS
                )
            
            # 加密新密码
            new_password_hash = self.hash_password(change_request.new_password)
            
            # 更新密码
            success = user_crud.update_password(db, user.id, new_password_hash)
            
            if success:
                # 记录日志
                log_crud.create(db, LogCreate(
                    user_id=user.id,
                    operator_id=user.id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    action="change_password",
                    resource="user",
                    resource_id=str(user.id),
                    ip_address=ip_address,
                    status="success"
                ))
                
                logger.info(
                    f"密码修改成功: {user.username}",
                    context={"user_id": user.id}
                )
            
            return success
        except AuthenticationException:
            raise
        except Exception as e:
            logger.error(f"修改密码失败: {str(e)}", exc_info=True)
            raise AuthenticationException("修改密码失败")
    
    def get_user_info(self, db: Session, user: AdminUser) -> UserInfo:
        """获取用户信息"""
        try:
            # 刷新用户数据
            fresh_user = user_crud.get_by_id(db, user.id)
            if not fresh_user:
                raise AuthenticationException("用户不存在")
            
            return UserInfo.model_validate(fresh_user)
        except Exception as e:
            logger.error(f"获取用户信息失败: {str(e)}", exc_info=True)
            raise AuthenticationException("获取用户信息失败")
    
    def register(
        self,
        db: Session,
        register_request: RegisterRequest,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> RegisterResponse:
        """
        用户注册 - 使用邮箱注册
        
        注册后自动成为管理员
        警告: 单用户系统：只允许注册一个管理员账户
        """
        try:
            # 检查是否已有用户（单用户限制）
            user_count = user_crud.get_count(db)
            if user_count > 0:
                logger.warning(
                    f"注册被拒绝：系统已有管理员账户",
                    context={"ip_address": ip_address}
                )
                raise ValidationException(
                    message="系统已有管理员账户，不允许再次注册",
                    error_code=ErrorCode.OPERATION_NOT_ALLOWED
                )
            
            # 检查邮箱是否已存在
            existing_email = user_crud.get_by_email(db, register_request.email)
            if existing_email:
                raise ValidationException(
                    message="该邮箱已被注册",
                    error_code=ErrorCode.RESOURCE_ALREADY_EXISTS
                )
            
            # 加密密码
            password_hash = self.hash_password(register_request.password)
            
            # 使用邮箱作为用户名
            username = register_request.email.split('@')[0]  # 从邮箱提取用户名
            
            # 如果用户名已存在,添加随机后缀
            existing_user = user_crud.get_by_username(db, username)
            if existing_user:
                import random
                username = f"{username}_{random.randint(1000, 9999)}"
            
            # 创建用户（自动成为管理员）
            user = user_crud.create(
                db=db,
                username=username,
                password_hash=password_hash,
                email=register_request.email,
                is_active=True  # 注册后直接激活
            )

            # 为首个管理员授予全局 super_admin（RBAC）
            try:
                from .authz_service import authz_service

                authz_service.assign_role_binding(
                    db=db,
                    user_id=user.id,
                    role_name="super_admin",
                    scope_type="global",
                    created_by=user.id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "授予默认 RBAC 角色失败，已进入兼容模式",
                    context={"user_id": user.id, "error": str(exc)},
                )
            
            # 记录注册日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="register",
                resource="user",
                resource_id=str(user.id),
                details={
                    "username": user.username,
                    "email": user.email
                },
                ip_address=ip_address,
                user_agent=user_agent,
                status="success"
            ))
            
            logger.info(
                f"用户注册成功: {user.username}",
                context={
                    "user_id": user.id,
                    "ip_address": ip_address
                }
            )
            
            # 构造响应
            user_info = UserInfo.model_validate(user)
            return RegisterResponse(
                user=user_info,
                message="注册成功，请登录"
            )
            
        except (ValidationException, AuthenticationException):
            raise
        except OperationalError as e:
            logger.error(f"注册失败（数据库不可用）: {str(e)}", exc_info=True)
            raise ValidationException("管理员数据库不可用，请检查 ADMIN_DATABASE_URL")
        except ProgrammingError as e:
            msg = str(e).lower()
            if "admin_users" in msg and "does not exist" in msg:
                raise ValidationException("管理员数据库未初始化，请先执行数据库迁移")
            logger.error(f"注册失败（数据库结构异常）: {str(e)}", exc_info=True)
            raise ValidationException("管理员数据库结构异常，请先执行迁移脚本")
        except Exception as e:
            logger.error(f"注册失败: {str(e)}", exc_info=True)
            raise ValidationException("注册失败，请稍后重试")


# 创建全局实例
auth_service = AuthService()
