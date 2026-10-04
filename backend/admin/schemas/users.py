"""
用户管理相关 Pydantic 模型 - 简化版(无角色权限)
"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


# ============ 用户相关 ============

class UserBase(BaseModel):
    """用户基础信息"""
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    email: str = Field(..., max_length=100, description="邮箱")
    full_name: Optional[str] = Field(None, max_length=100, description="姓名")
    phone: Optional[str] = Field(None, max_length=20, description="电话")
    department: Optional[str] = Field(None, max_length=100, description="部门")


class UserCreateRequest(UserBase):
    """创建用户请求"""
    password: str = Field(..., min_length=8, max_length=100, description="密码")
    
    @field_validator('username')
    @classmethod
    def username_alphanumeric(cls, v: str) -> str:
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError('用户名只能包含字母、数字、下划线和连字符')
        return v
    
    @field_validator('email')
    @classmethod
    def email_format(cls, v: str) -> str:
        if '@' not in v or '.' not in v.split('@')[1]:
            raise ValueError('请输入有效的邮箱地址')
        return v.lower()
    
    @field_validator('password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


class UserUpdateRequest(BaseModel):
    """更新用户请求"""
    email: Optional[str] = Field(None, max_length=100, description="邮箱")
    full_name: Optional[str] = Field(None, max_length=100, description="姓名")
    phone: Optional[str] = Field(None, max_length=20, description="电话")
    department: Optional[str] = Field(None, max_length=100, description="部门")
    avatar: Optional[str] = Field(None, max_length=255, description="头像URL")
    is_active: Optional[bool] = Field(None, description="是否激活")
    
    @field_validator('email')
    @classmethod
    def email_format(cls, v: Optional[str]) -> Optional[str]:
        if v and ('@' not in v or '.' not in v.split('@')[1]):
            raise ValueError('请输入有效的邮箱地址')
        return v.lower() if v else v


class UserPasswordUpdate(BaseModel):
    """修改密码请求"""
    old_password: str = Field(..., description="旧密码")
    new_password: str = Field(..., min_length=8, max_length=100, description="新密码")
    
    @field_validator('new_password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


class UserPasswordResetRequest(BaseModel):
    """管理员重置密码请求"""
    new_password: str = Field(..., min_length=8, max_length=100, description="新密码")

    @field_validator('new_password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


class UserResponse(BaseModel):
    """用户响应"""
    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    avatar: Optional[str] = None
    is_active: bool
    last_login: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    login_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """用户列表响应"""
    total: int = Field(..., description="总数")
    items: List[UserResponse] = Field(..., description="用户列表")
    page: int = Field(..., description="当前页")
    page_size: int = Field(..., description="每页数量")


class UserQueryParams(BaseModel):
    """用户查询参数"""
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")
    search: Optional[str] = Field(None, description="搜索关键词")
    is_active: Optional[bool] = Field(None, description="是否激活")
    department: Optional[str] = Field(None, description="部门")
    order_by: str = Field(default="created_at", description="排序字段")
    order_desc: bool = Field(default=True, description="是否降序")


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    user_ids: List[int] = Field(..., min_items=1, description="用户ID列表")
    soft_delete: bool = Field(default=True, description="是否软删除")


class BatchStatusUpdateRequest(BaseModel):
    """批量更新用户状态请求"""
    user_ids: List[int] = Field(..., min_items=1, description="用户ID列表")
    is_active: bool = Field(..., description="目标状态")


class BatchResetPasswordRequest(BaseModel):
    """批量重置密码请求"""
    user_ids: List[int] = Field(..., min_items=1, description="用户ID列表")
    new_password: str = Field(..., min_length=8, max_length=100, description="新密码")

    @field_validator('new_password')
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError('密码长度至少为 8 位')
        if not any(c.isdigit() for c in v):
            raise ValueError('密码必须包含数字')
        if not any(c.isalpha() for c in v):
            raise ValueError('密码必须包含字母')
        return v


# ============ 个人设置相关 ============

class ProfileUpdateRequest(BaseModel):
    """个人信息更新请求"""
    email: Optional[str] = Field(None, max_length=100, description="邮箱")
    full_name: Optional[str] = Field(None, max_length=100, description="姓名")
    phone: Optional[str] = Field(None, max_length=20, description="电话")
    avatar: Optional[str] = Field(None, max_length=255, description="头像URL")
    
    @field_validator('email')
    @classmethod
    def email_format(cls, v: Optional[str]) -> Optional[str]:
        if v and ('@' not in v or '.' not in v.split('@')[1]):
            raise ValueError('请输入有效的邮箱地址')
        return v.lower() if v else v


class ProfileResponse(BaseModel):
    """个人信息响应"""
    id: int
    username: str
    email: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    avatar: Optional[str] = None
    is_active: bool
    last_login: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    login_count: int
    created_at: datetime
    
    class Config:
        from_attributes = True


# ============ 统计相关 ============

class UserStatsResponse(BaseModel):
    """用户统计响应"""
    total_users: int = Field(..., description="总用户数")
    active_users: int = Field(..., description="活跃用户数")
    inactive_users: int = Field(..., description="非活跃用户数")
    departments: List[str] = Field(..., description="部门列表")
