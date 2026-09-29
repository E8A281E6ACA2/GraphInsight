"""
日志记录工具
"""
from typing import Optional
from admin.database import SessionLocal
from admin.models import AdminLog


def log_action(
    action: str,
    resource: Optional[str] = None,
    details: Optional[str] = None,
    user_id: Optional[int] = None,
    ip_address: Optional[str] = None
):
    """
    记录操作日志
    
    Args:
        action: 操作类型（如 "query", "nl2cypher"）
        resource: 资源标识
        details: 详细信息
        user_id: 用户ID（可选）
        ip_address: IP地址（可选）
    """
    try:
        db = SessionLocal()
        log = AdminLog(
            user_id=user_id,
            action=action,
            resource=resource,
            details=details,
            ip_address=ip_address
        )
        db.add(log)
        db.commit()
        db.close()
    except Exception as e:
        print(f"[WARNING] Failed to log action: {e}")
        # 不抛出异常，避免影响主业务
