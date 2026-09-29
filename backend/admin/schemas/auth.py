"""
认证相关 Pydantic 模型
"""
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    """登录请求 - 使用邮箱登录"""
    username: str = Field(..., description="邮箱")  # 保持字段名为 username 以兼容现有代码
    password: str = Field(..., min_length=6, max_length=100, description="密码")
    
    @field_validator('username')
    @classmethod
    def validate_email(cls, v: str) -> str:
        """验证邮箱格式"""
        if '@' not in v or '.' not in v.split('@')[1]:
            raise ValueError('请输入有效的邮箱地址')
        return v.lower()  # 转换为小写


class TokenData(BaseModel):
    """Token 数据"""
    username: str


class UserInfo(BaseModel):
    """用户信息"""
    id: int
    username: str
    email: Optional[str] = None
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None
    login_count: int = 0
    
    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    """登录响应"""
    token: str = Field(..., description="JWT Token")
    expires_in: int = Field(..., description="过期时间（秒）")
    user: UserInfo = Field(..., description="用户信息")


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., min_length=6, description="旧密码")
    new_password: str = Field(..., min_length=6, max_length=100, description="新密码")
    
    @field_validator('new_password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        """验证密码强度"""
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


class UserCreate(BaseModel):
    """创建用户请求"""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)
    email: Optional[str] = Field(None, max_length=100)
    
    @field_validator('username')
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.replace('_', '').isalnum():
            raise ValueError('用户名只能包含字母、数字和下划线')
        return v


class UserUpdate(BaseModel):
    """更新用户请求"""
    email: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None


class RegisterRequest(BaseModel):
    """注册请求 - 使用邮箱注册"""
    email: str = Field(..., max_length=100, description="邮箱")
    password: str = Field(..., min_length=8, max_length=100, description="密码")
    
    @field_validator('email')
    @classmethod
    def email_format(cls, v: str) -> str:
        """验证邮箱格式"""
        if '@' not in v or '.' not in v.split('@')[1]:
            raise ValueError('请输入有效的邮箱地址')
        return v.lower()  # 转换为小写
    
    @field_validator('password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        """验证密码强度"""
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


class RegisterResponse(BaseModel):
    """注册响应"""
    user: UserInfo = Field(..., description="用户信息")
    message: str = Field(default="注册成功，请登录", description="提示信息")
