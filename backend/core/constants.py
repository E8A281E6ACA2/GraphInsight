"""
常量定义
"""

# HTTP 状态码
class HTTPStatus:
    OK = 200
    CREATED = 201
    NO_CONTENT = 204
    BAD_REQUEST = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    CONFLICT = 409
    TOO_MANY_REQUESTS = 429
    INTERNAL_SERVER_ERROR = 500
    SERVICE_UNAVAILABLE = 503


# 日志级别
class LogLevel:
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# 环境类型
class Environment:
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TESTING = "testing"


# 配置分类
class ConfigCategory:
    SYSTEM = "system"
    NEO4J = "neo4j"
    AI_SERVICE = "ai_service"
    EMBEDDING = "embedding"
    NL2CYPHER = "nl2cypher"
    MEDIA = "media"


# 操作类型
class ActionType:
    LOGIN = "login"
    LOGOUT = "logout"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    QUERY = "query"
    EXPORT = "export"


# 资源类型
class ResourceType:
    USER = "user"
    CONFIG = "config"
    LOG = "log"
    GRAPH = "graph"
    NODE = "node"
    RELATIONSHIP = "relationship"
