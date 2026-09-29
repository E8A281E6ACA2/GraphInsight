#!/usr/bin/env python3
"""
用户管理模块数据库迁移脚本 - 简化版(无角色权限)
添加用户扩展字段
"""
import sys
import os
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text, inspect
from admin.database import get_db, engine
from admin.models import AdminUser

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def check_column_exists(table_name: str, column_name: str) -> bool:
    """检查列是否存在"""
    inspector = inspect(engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def migrate_admin_users():
    """迁移 admin_users 表"""
    logger.info("开始迁移 admin_users 表...")
    
    # 需要添加的新字段
    new_columns = [
        ("full_name", "VARCHAR(100)"),
        ("avatar", "VARCHAR(255)"),
        ("phone", "VARCHAR(20)"),
        ("department", "VARCHAR(100)"),
        ("last_login_ip", "VARCHAR(45)"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    
    with engine.connect() as conn:
        for column_name, column_def in new_columns:
            if not check_column_exists("admin_users", column_name):
                try:
                    sql = f"ALTER TABLE admin_users ADD COLUMN {column_name} {column_def}"
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info(f"添加字段: admin_users.{column_name}")
                except Exception as e:
                    logger.error(f"添加字段失败: admin_users.{column_name} - {e}")
            else:
                logger.info(f"字段已存在: admin_users.{column_name}")
        
        # 确保 email 字段有唯一约束
        try:
            # 检查是否已有唯一约束
            inspector = inspect(engine)
            indexes = inspector.get_indexes("admin_users")
            has_email_unique = any(
                idx.get('unique') and 'email' in idx.get('column_names', [])
                for idx in indexes
            )
            
            if not has_email_unique:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users(email)"))
                conn.commit()
                logger.info("为 email 字段添加唯一索引")
        except Exception as e:
            logger.warning(f"警告: 添加 email 唯一索引失败(可能已存在): {e}")


def main():
    """执行迁移"""
    logger.info("=" * 60)
    logger.info("开始用户管理模块数据库迁移(简化版)")
    logger.info("=" * 60)
    
    try:
        # 迁移 admin_users 表
        migrate_admin_users()
        
        logger.info("=" * 60)
        logger.info("用户管理模块数据库迁移完成!")
        logger.info("=" * 60)
        
        # 显示迁移结果
        db = next(get_db())
        try:
            user_count = db.query(AdminUser).count()
            logger.info(f"迁移统计:")
            logger.info(f"   - 用户数量: {user_count}")
            logger.info(f"   - 所有注册用户都是管理员")
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        return False
    
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
