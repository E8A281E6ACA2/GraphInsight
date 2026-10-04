"""
核心模块
提供统一响应格式、异常处理、安全认证等基础功能
"""
from .response import (
    ResponseModel,
    PaginatedData,
    ErrorDetail,
    ErrorResponse,
    ResponseBuilder,
    success_response,
    error_response,
    paginated_response,
)
from .exceptions import (
    ErrorCode,
    AppException,
    ValidationException,
    AuthenticationException,
    AuthorizationException,
    BusinessException,
    NotFoundException,
    SystemException,
    DatabaseException,
    RateLimitException,
    ConfigurationException,
    get_error_message,
)
from .logger import (
    LogConfig,
    StructuredLogger,
    get_logger,
    init_logger,
    logger,
)
from .middleware import (
    RequestLoggingMiddleware,
    RateLimitMiddleware,
    ErrorHandlingMiddleware,
    DEFAULT_RATE_LIMITS,
    DEFAULT_RATE_LIMIT_EXEMPT_PATHS,
)
from .observability import get_api_observability, get_qa_observability
from .constants import (
    HTTPStatus,
    LogLevel,
    Environment,
    ConfigCategory,
    ActionType,
    ResourceType,
)
from .security import (
    verify_password,
    get_password_hash,
)

__all__ = [
    # Response
    "ResponseModel",
    "PaginatedData",
    "ErrorDetail",
    "ErrorResponse",
    "ResponseBuilder",
    "success_response",
    "error_response",
    "paginated_response",
    # Exceptions
    "ErrorCode",
    "AppException",
    "ValidationException",
    "AuthenticationException",
    "AuthorizationException",
    "BusinessException",
    "NotFoundException",
    "SystemException",
    "DatabaseException",
    "RateLimitException",
    "ConfigurationException",
    "get_error_message",
    # Logger
    "LogConfig",
    "StructuredLogger",
    "get_logger",
    "init_logger",
    "logger",
    # Middleware
    "RequestLoggingMiddleware",
    "RateLimitMiddleware",
    "ErrorHandlingMiddleware",
    "DEFAULT_RATE_LIMITS",
    "DEFAULT_RATE_LIMIT_EXEMPT_PATHS",
    "get_api_observability",
    "get_qa_observability",
    # Constants
    "HTTPStatus",
    "LogLevel",
    "Environment",
    "ConfigCategory",
    "ActionType",
    "ResourceType",
    # Security
    "verify_password",
    "get_password_hash",
]
