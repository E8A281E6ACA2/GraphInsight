"""
日志相关 Pydantic 模型
"""
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


class LogItem(BaseModel):
    """日志项"""
    id: int
    user_id: Optional[int] = Field(None, description="用户ID")
    operator_id: Optional[int] = Field(None, description="操作人ID")
    tenant_id: Optional[str] = Field(None, description="租户ID")
    trace_id: Optional[str] = Field(None, description="请求追踪ID")
    username: Optional[str] = Field(None, description="用户名")
    action: str = Field(..., description="操作类型")
    resource: Optional[str] = Field(None, description="资源类型")
    resource_id: Optional[str] = Field(None, description="资源ID")
    details: Optional[str] = Field(None, description="详细信息")
    ip_address: Optional[str] = Field(None, description="IP地址")
    user_agent: Optional[str] = Field(None, description="User Agent")
    status: str = Field("success", description="状态: success, failed")
    severity: str = Field("info", description="日志级别: info, warn, error")
    error_message: Optional[str] = Field(None, description="错误信息")
    created_at: datetime = Field(..., description="创建时间")
    
    class Config:
        from_attributes = True


class LogDetail(BaseModel):
    """日志详情"""
    id: int
    user_id: Optional[int] = None
    operator_id: Optional[int] = None
    tenant_id: Optional[str] = None
    trace_id: Optional[str] = None
    username: Optional[str] = None
    action: str
    resource: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    status: str
    severity: str = "info"
    error_message: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class LogQuery(BaseModel):
    """日志查询"""
    user_id: Optional[int] = Field(None, description="用户ID")
    action: Optional[str] = Field(None, description="操作类型")
    resource: Optional[str] = Field(None, description="资源类型")
    status: Optional[str] = Field(None, description="状态")
    trace_id: Optional[str] = Field(None, description="追踪ID")
    start_date: Optional[datetime] = Field(None, description="开始时间")
    end_date: Optional[datetime] = Field(None, description="结束时间")
    ip_address: Optional[str] = Field(None, description="IP地址")
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(20, ge=1, le=100, description="每页大小")


class LogStats(BaseModel):
    """日志统计"""
    total_logs: int = Field(..., description="总日志数")
    success_count: int = Field(..., description="成功数")
    failed_count: int = Field(..., description="失败数")
    success_rate: float = Field(..., description="成功率")
    severity_stats: Dict[str, int] = Field(..., description="日志级别统计")
    action_stats: Dict[str, int] = Field(..., description="操作统计")
    user_stats: Dict[str, int] = Field(..., description="用户统计")
    hourly_stats: Dict[str, int] = Field(..., description="小时统计")
    
    class Config:
        json_schema_extra = {
            "example": {
                "total_logs": 1000,
                "success_count": 950,
                "failed_count": 50,
                "success_rate": 0.95,
                "severity_stats": {
                    "info": 880,
                    "warn": 70,
                    "error": 50
                },
                "action_stats": {
                    "login": 100,
                    "update": 200,
                    "query": 700
                },
                "user_stats": {
                    "admin": 800,
                    "user1": 200
                },
                "hourly_stats": {
                    "00": 50,
                    "01": 30,
                    "02": 20
                }
            }
        }


class LogCreate(BaseModel):
    """创建日志"""
    user_id: Optional[int] = None
    operator_id: Optional[int] = None
    tenant_id: Optional[str] = Field(None, max_length=100)
    trace_id: Optional[str] = Field(None, max_length=100)
    action: str = Field(..., min_length=1, max_length=100)
    resource: Optional[str] = Field(None, max_length=100)
    resource_id: Optional[str] = Field(None, max_length=100)
    details: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = Field(None, max_length=50)
    user_agent: Optional[str] = Field(None, max_length=500)
    status: str = Field("success", pattern="^(success|failed)$")
    error_message: Optional[str] = None
