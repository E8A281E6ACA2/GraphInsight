"""
结构化日志系统
支持 JSON 格式日志、敏感信息脱敏、日志轮转
"""
import logging
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional, List
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LogConfig:
    """日志配置"""
    
    def __init__(
        self,
        level: str = "INFO",
        format_type: str = "json",  # json 或 text
        output: str = "both",  # file, console, both
        log_dir: str = "logs",
        log_file: str = "app.log",
        max_bytes: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5,
        sensitive_fields: Optional[List[str]] = None
    ):
        self.level = level
        self.format_type = format_type
        self.output = output
        self.log_dir = log_dir
        self.log_file = log_file
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.sensitive_fields = sensitive_fields or [
            "password", "token", "api_key", "secret", "authorization"
        ]


class SensitiveDataFilter:
    """敏感数据过滤器"""
    
    def __init__(self, sensitive_fields: List[str]):
        self.sensitive_fields = [field.lower() for field in sensitive_fields]
    
    def mask_value(self, value: str) -> str:
        """脱敏处理"""
        if not value:
            return value
        if len(value) <= 4:
            return "***"
        return f"{value[:2]}***{value[-2:]}"
    
    def filter_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """过滤字典中的敏感信息"""
        if not isinstance(data, dict):
            return data
        
        filtered = {}
        for key, value in data.items():
            key_lower = key.lower()
            
            # 检查是否是敏感字段
            is_sensitive = any(
                sensitive in key_lower
                for sensitive in self.sensitive_fields
            )
            
            if is_sensitive and isinstance(value, str):
                filtered[key] = self.mask_value(value)
            elif isinstance(value, dict):
                filtered[key] = self.filter_dict(value)
            elif isinstance(value, list):
                filtered[key] = [
                    self.filter_dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                filtered[key] = value
        
        return filtered


class JSONFormatter(logging.Formatter):
    """JSON 格式化器"""
    
    def __init__(self, sensitive_filter: SensitiveDataFilter):
        super().__init__()
        self.sensitive_filter = sensitive_filter
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        log_data = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        
        # 添加额外的上下文信息
        if hasattr(record, "context"):
            log_data["context"] = self.sensitive_filter.filter_dict(record.context)
        
        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # 添加 trace_id（如果存在）
        if hasattr(record, "trace_id"):
            log_data["trace_id"] = record.trace_id
        
        return json.dumps(log_data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """文本格式化器"""
    
    def __init__(self):
        super().__init__(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )


class StructuredLogger:
    """结构化日志记录器"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls, config: Optional[LogConfig] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, config: Optional[LogConfig] = None):
        if self._initialized:
            return
        
        self.config = config or LogConfig()
        self.sensitive_filter = SensitiveDataFilter(self.config.sensitive_fields)
        self.logger = logging.getLogger("graphinsight")
        self.logger.setLevel(getattr(logging, self.config.level.upper()))
        self.logger.handlers.clear()
        
        # 配置处理器
        self._setup_handlers()
        self._initialized = True
    
    def _setup_handlers(self):
        """设置日志处理器"""
        # 文件处理器
        if self.config.output in ["file", "both"]:
            self._add_file_handler()
        
        # 控制台处理器
        if self.config.output in ["console", "both"]:
            self._add_console_handler()
    
    def _add_file_handler(self):
        """添加文件处理器"""
        # 创建日志目录
        log_dir = Path(self.config.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        log_path = log_dir / self.config.log_file
        
        # 创建轮转文件处理器
        file_handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=self.config.max_bytes,
            backupCount=self.config.backup_count,
            encoding="utf-8"
        )
        
        # 设置格式化器
        if self.config.format_type == "json":
            formatter = JSONFormatter(self.sensitive_filter)
        else:
            formatter = TextFormatter()
        
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)
    
    def _add_console_handler(self):
        """添加控制台处理器"""
        console_handler = logging.StreamHandler()
        
        # 控制台使用文本格式
        formatter = TextFormatter()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
    
    def _log(
        self,
        level: int,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        exc_info: bool = False
    ):
        """内部日志方法"""
        extra = {}
        if context:
            extra["context"] = context
        if trace_id:
            extra["trace_id"] = trace_id
        
        self.logger.log(level, message, extra=extra, exc_info=exc_info)
    
    def debug(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None
    ):
        """调试日志"""
        self._log(logging.DEBUG, message, context, trace_id)
    
    def info(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None
    ):
        """信息日志"""
        self._log(logging.INFO, message, context, trace_id)
    
    def warning(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None
    ):
        """警告日志"""
        self._log(logging.WARNING, message, context, trace_id)
    
    def error(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        exc_info: bool = False
    ):
        """错误日志"""
        self._log(logging.ERROR, message, context, trace_id, exc_info)
    
    def critical(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        trace_id: Optional[str] = None,
        exc_info: bool = False
    ):
        """严重错误日志"""
        self._log(logging.CRITICAL, message, context, trace_id, exc_info)


# 全局日志实例
_logger_instance: Optional[StructuredLogger] = None


def get_logger(config: Optional[LogConfig] = None) -> StructuredLogger:
    """获取日志记录器实例"""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = StructuredLogger(config)
    return _logger_instance


def init_logger(config: Optional[LogConfig] = None):
    """初始化日志系统"""
    global _logger_instance
    _logger_instance = StructuredLogger(config)
    return _logger_instance


# 默认 logger 实例（用于向后兼容）
logger = get_logger()
