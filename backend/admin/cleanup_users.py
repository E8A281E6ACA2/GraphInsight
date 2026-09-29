#!/usr/bin/env python3
"""
清理多余用户 - 只保留 yh@qs.al
"""
import sys
import os
import logging

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.database import get_db
from admin.models import AdminUser

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def cleanup_users():
    """清理用户,只保留 yh@qs.al"""
    logger.info("=" * 60)
    logger.info("开始清理多余用户")
    logger.info("=" * 60)
    
    db = next(get_db())
    try:
        # 查询所有用户
        all_users = db.query(AdminUser).all()
        logger.info(f"当前用户总数: {len(all_users)}")
        
        # 显示所有用户
        logger.info("\n当前用户列表:")
        for user in all_users:
            logger.info(f"  - ID: {user.id}, Email: {user.email}, Username: {user.username}")
        
        # 找到要保留的用户
        keep_user = db.query(AdminUser).filter(AdminUser.email == "yh@qs.al").first()
        
        if not keep_user:
            logger.error("未找到 yh@qs.al 用户!")
            return False
        
        logger.info(f"\n找到要保留的用户: {keep_user.email} (ID: {keep_user.id})")
        
        # 先更新外键引用,将其他用户的操作记录转移到保留的用户
        from admin.models import AdminConfig, AdminLog
        
        logger.info("\n更新外键引用...")
        for user in all_users:
            if user.id != keep_user.id:
                # 更新配置表的 updated_by
                db.query(AdminConfig).filter(
                    AdminConfig.updated_by == user.id
                ).update({"updated_by": keep_user.id})
                
                # 更新日志表的 user_id
                db.query(AdminLog).filter(
                    AdminLog.user_id == user.id
                ).update({"user_id": keep_user.id})
        
        db.commit()
        logger.info("外键引用更新完成")
        
        # 删除其他用户
        deleted_count = 0
        for user in all_users:
            if user.id != keep_user.id:
                logger.info(f"删除用户: {user.email} (ID: {user.id})")
                db.delete(user)
                deleted_count += 1
        
        db.commit()
        
        logger.info("=" * 60)
        logger.info(f"清理完成!")
        logger.info(f"   - 删除用户数: {deleted_count}")
        logger.info(f"   - 保留用户: {keep_user.email}")
        logger.info("=" * 60)
        
        # 验证结果
        remaining_users = db.query(AdminUser).all()
        logger.info(f"\n清理后用户总数: {len(remaining_users)}")
        for user in remaining_users:
            logger.info(f"  - ID: {user.id}, Email: {user.email}, Username: {user.username}")
        
        return True
        
    except Exception as e:
        logger.error(f"清理失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


if __name__ == "__main__":
    success = cleanup_users()
    sys.exit(0 if success else 1)
