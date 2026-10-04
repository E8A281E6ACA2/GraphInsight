"""
统一响应格式模块
提供标准化的 API 响应格式
"""
from typing import Any, Optional, List, TypeVar, Generic
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict


T = TypeVar('T')


class ResponseModel(BaseModel):
    """统一响应模型"""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    code: int = Field(description="HTTP 状态码")
    message: str = Field(description="响应消息")
    data: Optional[Any] = Field(default=None, description="业务数据")
    timestamp: str = Field(description="响应时间戳")
    trace_id: Optional[str] = Field(default=None, description="请求追踪ID")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 200,
                "message": "success",
                "data": {"key": "value"},
                "timestamp": "2025-11-26T10:00:00Z",
                "trace_id": "5f88e7b0-2f38-4c8e-bd9f-7ab4cf8f2e40"
            }
        }
    )


class PaginatedData(BaseModel):
    """分页数据模型"""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    items: List[Any] = Field(description="数据列表")
    total: int = Field(description="总数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页大小")
    total_pages: int = Field(description="总页数")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [{"id": 1}, {"id": 2}],
                "total": 100,
                "page": 1,
                "page_size": 10,
                "total_pages": 10
            }
        }
    )


class ErrorDetail(BaseModel):
    """错误详情模型"""
    error_code: str = Field(description="错误码")
    error_type: str = Field(description="错误类型")
    details: Optional[dict] = Field(default=None, description="详细信息")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error_code": "4001",
                "error_type": "ValidationError",
                "details": {"field": "username", "message": "用户名不能为空"}
            }
        }
    )


class ErrorResponse(BaseModel):
    """错误响应模型"""
    code: int = Field(description="HTTP 状态码")
    message: str = Field(description="错误消息")
    error: ErrorDetail = Field(description="错误详情")
    timestamp: str = Field(description="响应时间戳")
    trace_id: Optional[str] = Field(default=None, description="请求追踪ID")
    
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 400,
                "message": "参数验证失败",
                "error": {
                    "error_code": "4001",
                    "error_type": "ValidationError",
                    "details": {"field": "username", "message": "用户名不能为空"}
                },
                "timestamp": "2025-11-26T10:00:00Z",
                "trace_id": "5f88e7b0-2f38-4c8e-bd9f-7ab4cf8f2e40"
            }
        }
    )


class ResponseBuilder:
    """响应构造器"""
    
    @staticmethod
    def _get_timestamp() -> str:
        """获取当前时间戳"""
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    @staticmethod
    def success(
        data: Any = None,
        message: str = "success",
        code: int = 200
    ) -> dict:
        """
        构造成功响应
        
        Args:
            data: 业务数据
            message: 响应消息
            code: HTTP 状态码
            
        Returns:
            标准响应字典
        """
        return {
            "code": code,
            "message": message,
            "data": data,
            "timestamp": ResponseBuilder._get_timestamp()
        }
    
    @staticmethod
    def error(
        message: str,
        code: int = 400,
        error_code: Optional[str] = None,
        error_type: str = "Error",
        details: Optional[dict] = None
    ) -> dict:
        """
        构造错误响应
        
        Args:
            message: 错误消息
            code: HTTP 状态码
            error_code: 错误码
            error_type: 错误类型
            details: 详细信息
            
        Returns:
            标准错误响应字典
        """
        return {
            "code": code,
            "message": message,
            "error": {
                "error_code": error_code or str(code),
                "error_type": error_type,
                "details": details
            },
            "timestamp": ResponseBuilder._get_timestamp()
        }
    
    @staticmethod
    def paginated(
        items: List[Any],
        total: int,
        page: int,
        page_size: int,
        message: str = "success"
    ) -> dict:
        """
        构造分页响应
        
        Args:
            items: 数据列表
            total: 总数
            page: 当前页码
            page_size: 每页大小
            message: 响应消息
            
        Returns:
            标准分页响应字典
        """
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        
        return ResponseBuilder.success(
            data={
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages
            },
            message=message
        )


# 便捷函数
def success_response(data: Any = None, message: str = "success", code: int = 200) -> dict:
    """成功响应快捷函数"""
    return ResponseBuilder.success(data, message, code)


def error_response(
    message: str,
    code: int = 400,
    error_code: Optional[str] = None,
    error_type: str = "Error",
    details: Optional[dict] = None
) -> dict:
    """错误响应快捷函数"""
    return ResponseBuilder.error(message, code, error_code, error_type, details)


def paginated_response(
    items: List[Any],
    total: int,
    page: int,
    page_size: int,
    message: str = "success"
) -> dict:
    """分页响应快捷函数"""
    return ResponseBuilder.paginated(items, total, page, page_size, message)
