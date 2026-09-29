"""
CRUD 操作层
提供数据库操作的封装
"""
from .user import user_crud
from .config import config_crud
from .log import log_crud

__all__ = [
    "user_crud",
    "config_crud",
    "log_crud",
]
