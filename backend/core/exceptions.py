"""
自定义异常类和错误码定义
"""
from typing import Optional, Any


class ErrorCode:
    """错误码常量"""

    # 通用错误 (1xxx)
    UNKNOWN_ERROR = "1000"
    INVALID_REQUEST = "1001"
    NOT_FOUND = "1002"
    METHOD_NOT_ALLOWED = "1003"

    # 认证/授权错误 (2xxx)
    UNAUTHORIZED = "2001"
    TOKEN_EXPIRED = "2002"
    INVALID_TOKEN = "2003"
    INVALID_CREDENTIALS = "2004"
    USER_DISABLED = "2005"
    FORBIDDEN = "2006"

    # 业务逻辑错误 (3xxx)
    BUSINESS_ERROR = "3000"
    RESOURCE_NOT_FOUND = "3001"
    RESOURCE_ALREADY_EXISTS = "3002"
    OPERATION_FAILED = "3003"
    OPERATION_NOT_ALLOWED = "3004"

    # 验证错误 (4xxx)
    VALIDATION_ERROR = "4001"
    MISSING_PARAMETER = "4002"
    INVALID_PARAMETER = "4003"
    PARAMETER_OUT_OF_RANGE = "4004"

    # 系统错误 (5xxx)
    INTERNAL_ERROR = "5000"
    DATABASE_ERROR = "5001"
    NETWORK_ERROR = "5002"
    SERVICE_UNAVAILABLE = "5003"
    CONFIGURATION_ERROR = "5004"

    # 限流错误 (6xxx)
    RATE_LIMIT_EXCEEDED = "6001"

    # 知识库作用域错误（字符串码，与 Go 侧统一，见 docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2.9）
    KB_SCOPE_REQUIRED = "KB_SCOPE_REQUIRED"
    KB_NOT_FOUND = "KB_NOT_FOUND"
    KB_ACCESS_DENIED = "KB_ACCESS_DENIED"
    KB_ARCHIVED = "KB_ARCHIVED"
    KB_CROSS_SCOPE = "KB_CROSS_SCOPE"
    KB_DUPLICATE_NAME = "KB_DUPLICATE_NAME"
    KB_STORAGE_PATH_INVALID = "KB_STORAGE_PATH_INVALID"
    SCOPE_INVALID = "SCOPE_INVALID"
    CHUNK_REVISION_CONFLICT = "CHUNK_REVISION_CONFLICT"


class AppException(Exception):
    """应用基础异常"""

    def __init__(
        self,
        message: str,
        error_code: str = ErrorCode.UNKNOWN_ERROR,
        status_code: int = 500,
        details: Optional[dict] = None
    ):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "message": self.message,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "details": self.details
        }


class ValidationException(AppException):
    """验证异常 (4xxx)"""

    def __init__(
        self,
        message: str = "参数验证失败",
        error_code: str = ErrorCode.VALIDATION_ERROR,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=400,
            details=details
        )


class AuthenticationException(AppException):
    """认证异常 (2xxx)"""

    def __init__(
        self,
        message: str = "认证失败",
        error_code: str = ErrorCode.UNAUTHORIZED,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=401,
            details=details
        )


class AuthorizationException(AppException):
    """授权异常"""

    def __init__(
        self,
        message: str = "权限不足",
        error_code: str = ErrorCode.FORBIDDEN,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=403,
            details=details
        )


class BusinessException(AppException):
    """业务异常 (3xxx)"""

    def __init__(
        self,
        message: str = "业务处理失败",
        error_code: str = ErrorCode.BUSINESS_ERROR,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=400,
            details=details
        )


class NotFoundException(AppException):
    """资源不存在异常"""

    def __init__(
        self,
        message: str = "资源不存在",
        error_code: str = ErrorCode.RESOURCE_NOT_FOUND,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=404,
            details=details
        )


class SystemException(AppException):
    """系统异常 (5xxx)"""

    def __init__(
        self,
        message: str = "系统错误",
        error_code: str = ErrorCode.INTERNAL_ERROR,
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=500,
            details=details
        )


class DatabaseException(SystemException):
    """数据库异常"""

    def __init__(
        self,
        message: str = "数据库操作失败",
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=ErrorCode.DATABASE_ERROR,
            details=details
        )


class RateLimitException(AppException):
    """限流异常 (6xxx)"""

    def __init__(
        self,
        message: str = "请求过于频繁，请稍后再试",
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=ErrorCode.RATE_LIMIT_EXCEEDED,
            status_code=429,
            details=details
        )


class ConfigurationException(SystemException):
    """配置异常"""

    def __init__(
        self,
        message: str = "配置错误",
        details: Optional[dict] = None
    ):
        super().__init__(
            message=message,
            error_code=ErrorCode.CONFIGURATION_ERROR,
            details=details
        )


# 知识库作用域错误的默认 HTTP 状态映射（契约 §2.9）
KB_ERROR_STATUS = {
    ErrorCode.KB_SCOPE_REQUIRED: 400,
    ErrorCode.KB_NOT_FOUND: 404,
    ErrorCode.KB_ACCESS_DENIED: 403,
    ErrorCode.KB_ARCHIVED: 409,
    ErrorCode.KB_CROSS_SCOPE: 400,
    ErrorCode.KB_DUPLICATE_NAME: 409,
    ErrorCode.KB_STORAGE_PATH_INVALID: 400,
    ErrorCode.SCOPE_INVALID: 400,
    ErrorCode.CHUNK_REVISION_CONFLICT: 409,
}


class KnowledgeScopeError(AppException):
    """知识库作用域错误（契约 §2.9 统一字符串码）"""

    def __init__(
        self,
        error_code: str,
        message: str,
        details: Optional[dict] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(
            message=message,
            error_code=error_code,
            status_code=status_code or KB_ERROR_STATUS.get(error_code, 400),
            details=details
        )


# 错误消息映射
ERROR_MESSAGES = {
    ErrorCode.UNKNOWN_ERROR: "未知错误",
    ErrorCode.INVALID_REQUEST: "无效的请求",
    ErrorCode.NOT_FOUND: "资源不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "方法不允许",

    ErrorCode.UNAUTHORIZED: "未授权",
    ErrorCode.TOKEN_EXPIRED: "Token 已过期",
    ErrorCode.INVALID_TOKEN: "无效的 Token",
    ErrorCode.INVALID_CREDENTIALS: "用户名或密码错误",
    ErrorCode.USER_DISABLED: "用户已被禁用",
    ErrorCode.FORBIDDEN: "权限不足",

    ErrorCode.BUSINESS_ERROR: "业务处理失败",
    ErrorCode.RESOURCE_NOT_FOUND: "资源不存在",
    ErrorCode.RESOURCE_ALREADY_EXISTS: "资源已存在",
    ErrorCode.OPERATION_FAILED: "操作失败",
    ErrorCode.OPERATION_NOT_ALLOWED: "操作不被允许",

    ErrorCode.VALIDATION_ERROR: "参数验证失败",
    ErrorCode.MISSING_PARAMETER: "缺少必需参数",
    ErrorCode.INVALID_PARAMETER: "参数格式错误",
    ErrorCode.PARAMETER_OUT_OF_RANGE: "参数超出范围",

    ErrorCode.INTERNAL_ERROR: "系统内部错误",
    ErrorCode.DATABASE_ERROR: "数据库操作失败",
    ErrorCode.NETWORK_ERROR: "网络错误",
    ErrorCode.SERVICE_UNAVAILABLE: "服务不可用",
    ErrorCode.CONFIGURATION_ERROR: "配置错误",

    ErrorCode.RATE_LIMIT_EXCEEDED: "请求过于频繁",

    ErrorCode.KB_SCOPE_REQUIRED: "缺少知识库作用域",
    ErrorCode.KB_NOT_FOUND: "知识库不存在",
    ErrorCode.KB_ACCESS_DENIED: "无权访问该知识库",
    ErrorCode.KB_ARCHIVED: "知识库已归档，禁止该操作",
    ErrorCode.KB_CROSS_SCOPE: "请求作用域不一致",
    ErrorCode.KB_DUPLICATE_NAME: "同项目内知识库名称已存在",
    ErrorCode.KB_STORAGE_PATH_INVALID: "知识库存储路径非法",
    ErrorCode.SCOPE_INVALID: "作用域标识格式非法",
    ErrorCode.CHUNK_REVISION_CONFLICT: "Chunk 版本冲突",
}


def get_error_message(error_code: str) -> str:
    """获取错误消息"""
    return ERROR_MESSAGES.get(error_code, "未知错误")
