"""
管理系统数据库连接
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv, find_dotenv

# Linux 统一启动脚本会通过 GRAPHINSIGHT_BACKEND_ENV_FILE 指向临时开发配置。
# 这里必须与 backend/config.py 使用同一来源，避免 admin 导入把运行态重新覆盖回 backend/.env。
_env_override = os.getenv("GRAPHINSIGHT_BACKEND_ENV_FILE", "").strip()
if _env_override:
    _env_path = Path(_env_override).expanduser().resolve()
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
    else:
        load_dotenv(find_dotenv(), override=True)
else:
    load_dotenv(find_dotenv(), override=True)

# PostgreSQL 连接配置
# 从环境变量读取，如果没有则使用默认值
ADMIN_DATABASE_URL = os.getenv(
    "ADMIN_DATABASE_URL",
    "postgresql://graphinsight:graphinsight-dev-password@127.0.0.1:5434/graphinsight_admin"
)

# 创建数据库引擎
engine = create_engine(ADMIN_DATABASE_URL, pool_pre_ping=True)

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建基类
Base = declarative_base()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """初始化数据库表"""
    Base.metadata.create_all(bind=engine)
    _ensure_admin_config_unique_key()


def _ensure_admin_config_unique_key():
    """Ensure config keys are unique per category, including upgraded databases."""
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("""
                DELETE FROM admin_configs old
                USING admin_configs newer
                WHERE old.category = newer.category
                  AND old.key = newer.key
                  AND old.id < newer.id
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_config_category_key
                ON admin_configs (category, key)
            """))
            return

        if dialect == "sqlite":
            conn.execute(text("""
                DELETE FROM admin_configs
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM admin_configs
                    GROUP BY category, key
                )
            """))
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_config_category_key
                ON admin_configs (category, key)
            """))
