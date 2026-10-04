"""
日志 CRUD 操作
"""
from typing import Optional, List, Tuple, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func, and_

from ..models import AdminLog, AdminUser
from ..schemas.logs import LogCreate, LogQuery
from core import DatabaseException


def classify_log_severity(*, status: Optional[str], action: Optional[str], error_message: Optional[str]) -> str:
    """统一日志分级策略。"""
    normalized_status = (status or "").strip().lower()
    normalized_action = (action or "").strip().lower()
    normalized_error = (error_message or "").strip().lower()

    if normalized_status == "failed" or normalized_error:
        return "error"
    if any(keyword in normalized_action for keyword in ("retry", "cancel", "cleanup", "clean", "delete")):
        return "warn"
    return "info"


class LogCRUD:
    """日志 CRUD 操作类"""
    
    def get_by_id(self, db: Session, log_id: int) -> Optional[AdminLog]:
        """根据 ID 获取日志"""
        try:
            return db.query(AdminLog).filter(AdminLog.id == log_id).first()
        except Exception as e:
            raise DatabaseException(f"查询日志失败: {str(e)}")
    
    def get_list(
        self,
        db: Session,
        query: LogQuery
    ) -> Tuple[List[dict], int]:
        """获取日志列表（分页，包含用户名）"""
        try:
            # 构建查询（关联用户表）
            db_query = db.query(
                AdminLog,
                AdminUser.username
            ).outerjoin(
                AdminUser,
                AdminLog.user_id == AdminUser.id
            )
            
            # 过滤条件
            filters = []
            if query.user_id:
                filters.append(AdminLog.user_id == query.user_id)
            if query.action:
                filters.append(AdminLog.action == query.action)
            if query.resource:
                filters.append(AdminLog.resource == query.resource)
            if query.status:
                filters.append(AdminLog.status == query.status)
            if query.trace_id:
                filters.append(AdminLog.trace_id == query.trace_id)
            if query.start_date:
                filters.append(AdminLog.created_at >= query.start_date)
            if query.end_date:
                filters.append(AdminLog.created_at <= query.end_date)
            if query.ip_address:
                filters.append(AdminLog.ip_address == query.ip_address)
            
            if filters:
                db_query = db_query.filter(and_(*filters))
            
            # 总数
            total = db_query.count()
            
            # 分页
            offset = (query.page - 1) * query.page_size
            results = db_query.order_by(
                AdminLog.created_at.desc()
            ).offset(offset).limit(query.page_size).all()
            
            # 转换为字典列表
            items = []
            for log, username in results:
                log_dict = {
                    "id": log.id,
                    "user_id": log.user_id,
                    "operator_id": log.operator_id,
                    "tenant_id": log.tenant_id,
                    "trace_id": log.trace_id,
                    "username": username,
                    "action": log.action,
                    "resource": log.resource,
                    "resource_id": log.resource_id,
                    "details": log.details,
                    "ip_address": log.ip_address,
                    "user_agent": log.user_agent,
                    "status": log.status,
                    "severity": classify_log_severity(
                        status=log.status,
                        action=log.action,
                        error_message=log.error_message,
                    ),
                    "error_message": log.error_message,
                    "created_at": log.created_at
                }
                items.append(log_dict)
            
            return items, total
        except Exception as e:
            raise DatabaseException(f"查询日志列表失败: {str(e)}")
    
    def create(self, db: Session, log_create: LogCreate) -> AdminLog:
        """创建日志"""
        try:
            # 将 details 转换为 JSON 字符串
            import json
            details_str = None
            if log_create.details:
                details_str = json.dumps(log_create.details, ensure_ascii=False)
            
            db_log = AdminLog(
                user_id=log_create.user_id,
                operator_id=log_create.operator_id or log_create.user_id,
                tenant_id=log_create.tenant_id,
                trace_id=log_create.trace_id,
                action=log_create.action,
                resource=log_create.resource,
                resource_id=log_create.resource_id,
                details=details_str,
                ip_address=log_create.ip_address,
                user_agent=log_create.user_agent,
                status=log_create.status,
                error_message=log_create.error_message
            )
            db.add(db_log)
            db.commit()
            db.refresh(db_log)
            return db_log
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"创建日志失败: {str(e)}")
    
    def get_stats(
        self,
        db: Session,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> Dict:
        """获取日志统计"""
        try:
            # 默认统计最近 7 天
            if not start_date:
                start_date = datetime.utcnow() - timedelta(days=7)
            if not end_date:
                end_date = datetime.utcnow()
            
            # 基础查询
            base_query = db.query(AdminLog).filter(
                AdminLog.created_at >= start_date,
                AdminLog.created_at <= end_date
            )
            
            # 总数
            total_logs = base_query.count()
            
            # 成功/失败数
            success_count = base_query.filter(AdminLog.status == "success").count()
            failed_count = base_query.filter(AdminLog.status == "failed").count()
            severity_stats = {
                "info": 0,
                "warn": 0,
                "error": 0,
            }

            severity_rows = base_query.with_entities(
                AdminLog.status,
                AdminLog.action,
                AdminLog.error_message,
            ).all()
            for status_value, action_value, error_message in severity_rows:
                severity = classify_log_severity(
                    status=status_value,
                    action=action_value,
                    error_message=error_message,
                )
                severity_stats[severity] = severity_stats.get(severity, 0) + 1
            
            # 成功率
            success_rate = success_count / total_logs if total_logs > 0 else 0
            
            # 操作统计
            action_stats = {}
            action_results = db.query(
                AdminLog.action,
                func.count(AdminLog.id)
            ).filter(
                AdminLog.created_at >= start_date,
                AdminLog.created_at <= end_date
            ).group_by(AdminLog.action).all()
            
            for action, count in action_results:
                action_stats[action] = count
            
            # 用户统计
            user_stats = {}
            user_results = db.query(
                AdminUser.username,
                func.count(AdminLog.id)
            ).join(
                AdminLog,
                AdminUser.id == AdminLog.user_id
            ).filter(
                AdminLog.created_at >= start_date,
                AdminLog.created_at <= end_date
            ).group_by(AdminUser.username).all()
            
            for username, count in user_results:
                user_stats[username] = count
            
            # 小时统计
            hourly_stats = {}
            for hour in range(24):
                hour_str = f"{hour:02d}"
                count = base_query.filter(
                    func.extract('hour', AdminLog.created_at) == hour
                ).count()
                hourly_stats[hour_str] = count
            
            return {
                "total_logs": total_logs,
                "success_count": success_count,
                "failed_count": failed_count,
                "success_rate": round(success_rate, 4),
                "severity_stats": severity_stats,
                "action_stats": action_stats,
                "user_stats": user_stats,
                "hourly_stats": hourly_stats
            }
        except Exception as e:
            raise DatabaseException(f"查询日志统计失败: {str(e)}")
    
    def delete_old_logs(self, db: Session, days: int = 90) -> int:
        """删除旧日志"""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            deleted = db.query(AdminLog).filter(
                AdminLog.created_at < cutoff_date
            ).delete()
            db.commit()
            return deleted
        except Exception as e:
            db.rollback()
            raise DatabaseException(f"删除旧日志失败: {str(e)}")


# 创建全局实例
log_crud = LogCRUD()
