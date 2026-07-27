"""
监控服务
处理系统监控、健康检查等业务逻辑
"""
import json
import os
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from ..database import engine
from ..models import AdminJob, AdminLog
from ..crud.log import classify_log_severity
from ..schemas.monitor import (
    SystemStats,
    HealthStatus,
    DatabaseStatus,
    Neo4jStatus,
    AIServiceStatus,
)
from core import get_logger, SystemException, get_api_observability, get_qa_observability
from services.runtime_config import get_ai_runtime_config, get_neo4j_runtime_config

logger = get_logger()

# 应用启动时间
_start_time = time.time()


class MonitorService:
    """监控服务类"""

    def __init__(self):
        self.start_time = _start_time
        self.api_metrics = get_api_observability()
        self.qa_metrics = get_qa_observability()
        try:
            self.cache_ttl_seconds = max(0.0, float(os.getenv("MONITOR_CACHE_TTL_SECONDS", "5")))
        except ValueError:
            self.cache_ttl_seconds = 5.0
        self._cache: Dict[str, tuple[float, Any]] = {}

    def _get_cached(self, key: str) -> Optional[Any]:
        if self.cache_ttl_seconds <= 0:
            return None
        cached = self._cache.get(key)
        if not cached:
            return None
        expires_at, value = cached
        if expires_at <= time.time():
            self._cache.pop(key, None)
            return None
        return value

    def _set_cached(self, key: str, value: Any) -> Any:
        if self.cache_ttl_seconds > 0:
            self._cache[key] = (time.time() + self.cache_ttl_seconds, value)
        return value

    def get_system_stats(self) -> SystemStats:
        """获取系统统计信息"""
        cached = self._get_cached("system_stats")
        if cached is not None:
            return cached

        if not PSUTIL_AVAILABLE:
            logger.warning("psutil 未安装，返回模拟数据")
            return self._set_cached("system_stats", SystemStats(
                cpu_percent=0.0,
                memory_percent=0.0,
                memory_used_mb=0.0,
                memory_total_mb=0.0,
                disk_percent=0.0,
                disk_used_gb=0.0,
                disk_total_gb=0.0,
                uptime_seconds=round(time.time() - self.start_time, 2),
                timestamp=datetime.utcnow()
            ))

        try:
            # CPU 使用率
            cpu_percent = psutil.cpu_percent(interval=None)

            # 内存信息
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
            memory_used_mb = memory.used / (1024 * 1024)
            memory_total_mb = memory.total / (1024 * 1024)

            # 磁盘信息
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent
            disk_used_gb = disk.used / (1024 * 1024 * 1024)
            disk_total_gb = disk.total / (1024 * 1024 * 1024)

            # 运行时间
            uptime_seconds = time.time() - self.start_time

            return self._set_cached("system_stats", SystemStats(
                cpu_percent=round(cpu_percent, 2),
                memory_percent=round(memory_percent, 2),
                memory_used_mb=round(memory_used_mb, 2),
                memory_total_mb=round(memory_total_mb, 2),
                disk_percent=round(disk_percent, 2),
                disk_used_gb=round(disk_used_gb, 2),
                disk_total_gb=round(disk_total_gb, 2),
                uptime_seconds=round(uptime_seconds, 2),
                timestamp=datetime.utcnow()
            ))
        except Exception as e:
            logger.error(f"获取系统统计失败: {str(e)}", exc_info=True)
            raise SystemException("获取系统统计失败")

    def check_database_status(self, db: Session) -> DatabaseStatus:
        """检查数据库状态"""
        try:
            # 尝试执行简单查询
            result = db.execute(text("SELECT 1")).scalar()

            if result == 1:
                # 获取表数量
                tables_result = db.execute(text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )).scalar()

                return DatabaseStatus(
                    connected=True,
                    database=engine.url.database or "unknown",
                    tables_count=tables_result,
                    error=None
                )
            else:
                return DatabaseStatus(
                    connected=False,
                    database=engine.url.database or "unknown",
                    tables_count=None,
                    error="查询返回异常结果"
                )
        except Exception as e:
            logger.error(f"数据库检查失败: {str(e)}")
            return DatabaseStatus(
                connected=False,
                database=engine.url.database or "unknown",
                tables_count=None,
                error=str(e)
            )

    def check_neo4j_status(self, db: Session) -> Neo4jStatus:
        """检查 Neo4j 状态"""
        cached = self._get_cached("neo4j_status")
        if cached is not None:
            return cached

        try:
            from services.neo4j_service import get_neo4j_service

            # 获取 Neo4j 配置
            neo4j_config = get_neo4j_runtime_config()
            uri = neo4j_config.get("uri", "bolt://localhost:7687")
            database = neo4j_config.get("database", "neo4j")

            # 尝试连接并获取统计
            try:
                neo4j_service = get_neo4j_service()

                neo4j_service.ensure_connected()
                with neo4j_service.session() as session:
                    session.run("RETURN 1 AS ok").single()

                return self._set_cached("neo4j_status", Neo4jStatus(
                    connected=True,
                    uri=uri,
                    database=database,
                    nodes_count=None,
                    relationships_count=None,
                    error=None
                ))
            except Exception as e:
                logger.error(f"Neo4j 查询失败: {str(e)}")
                return self._set_cached("neo4j_status", Neo4jStatus(
                    connected=False,
                    uri=uri,
                    database=database,
                    nodes_count=None,
                    relationships_count=None,
                    error=str(e)
                ))
        except Exception as e:
            logger.error(f"Neo4j 检查失败: {str(e)}")
            return self._set_cached("neo4j_status", Neo4jStatus(
                connected=False,
                uri="unknown",
                database="unknown",
                nodes_count=None,
                relationships_count=None,
                error=str(e)
            ))

    def check_ai_service_status(self, db: Session) -> AIServiceStatus:
        """检查AI服务状态"""
        try:
            # 获取AI服务配置
            ai_config = get_ai_runtime_config()
            provider = str(ai_config.get("provider") or "openai")
            enabled = bool(ai_config.get("enabled", True))
            api_key = str(ai_config.get("api_key") or "")
            model = str(ai_config.get("model") or "gpt-3.5-turbo")

            # 检查是否启用
            if not enabled:
                return AIServiceStatus(
                    connected=False,
                    service_name=provider.upper(),
                    model=model,
                    api_key_configured=False,
                    error="AI服务未启用"
                )

            # 检查API Key是否配置
            api_key_configured = bool(api_key and api_key.strip() and api_key != "your-api-key-here")

            if not api_key_configured:
                return AIServiceStatus(
                    connected=False,
                    service_name=provider.upper(),
                    model=model,
                    api_key_configured=False,
                    error="API Key未配置"
                )

            # API Key 是不透明凭据；监控只报告是否配置，不能通过前缀推断鉴权结果。
            if provider == "openai":
                return AIServiceStatus(
                    connected=True,
                    service_name="OpenAI",
                    model=model,
                    api_key_configured=True,
                    error=None
                )
            elif provider == "claude":
                return AIServiceStatus(
                    connected=True,
                    service_name="Claude",
                    model=model,
                    api_key_configured=True,
                    error=None
                )
            else:
                # 其他 provider，只检查是否配置
                return AIServiceStatus(
                    connected=True,
                    service_name=provider.upper(),
                    model=model,
                    api_key_configured=True,
                    error=None
                )
        except Exception as e:
            logger.error(f"AI服务检查失败: {str(e)}")
            return AIServiceStatus(
                connected=False,
                service_name="Unknown",
                model=None,
                api_key_configured=False,
                error=str(e)
            )

    def get_health_status(self, db: Session) -> HealthStatus:
        """获取健康状态"""
        cached = self._get_cached("health_status")
        if cached is not None:
            return cached

        try:
            # 检查数据库
            database_status = self.check_database_status(db)

            # 检查 Neo4j
            neo4j_status = self.check_neo4j_status(db)

            # 检查AI服务
            ai_service_status = self.check_ai_service_status(db)

            # 获取系统统计
            try:
                system_stats = self.get_system_stats()
            except:
                system_stats = None

            # 检查项
            checks = {
                "database": database_status.connected,
                "neo4j": neo4j_status.connected,
                "ai_service": ai_service_status.connected,
                "disk_space": system_stats.disk_percent < 90 if system_stats else True,
                "memory": system_stats.memory_percent < 90 if system_stats else True,
            }

            # 确定整体状态
            if all(checks.values()):
                status = "healthy"
            elif any(checks.values()):
                status = "degraded"
            else:
                status = "unhealthy"

            return self._set_cached("health_status", HealthStatus(
                status=status,
                timestamp=datetime.utcnow(),
                database=database_status,
                neo4j=neo4j_status,
                ai_service=ai_service_status,
                system=system_stats,
                checks=checks
            ))
        except Exception as e:
            logger.error(f"获取健康状态失败: {str(e)}", exc_info=True)
            raise SystemException("获取健康状态失败")

    def get_performance_metrics(self, window_seconds: int = 900) -> Dict:
        """获取 API 性能指标"""
        metrics = self.api_metrics.snapshot(window_seconds=window_seconds)
        return {
            "avg_response_time_ms": metrics["avg_response_time_ms"],
            "p50_response_time_ms": metrics["p50_response_time_ms"],
            "p95_response_time_ms": metrics["p95_response_time_ms"],
            "p99_response_time_ms": metrics["p99_response_time_ms"],
            "requests_per_second": metrics["requests_per_second"],
            "error_rate": metrics["error_rate"],
            "total_requests": metrics["total_requests"],
            "failed_requests": metrics["failed_requests"],
            "window_seconds": metrics["window_seconds"],
            "top_paths": metrics["top_paths"],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def get_qa_quality_metrics(self, window_seconds: int = 900) -> Dict:
        """获取问答质量指标"""
        metrics = self.qa_metrics.snapshot(window_seconds=window_seconds)
        return {
            "window_seconds": metrics["window_seconds"],
            "total_requests": metrics["total_requests"],
            "failed_requests": metrics["failed_requests"],
            "success_rate": metrics["success_rate"],
            "failure_rate": metrics["failure_rate"],
            "citation_rate": metrics["citation_rate"],
            "avg_citations": metrics["avg_citations"],
            "avg_latency_ms": metrics["avg_latency_ms"],
            "p50_latency_ms": metrics["p50_latency_ms"],
            "p95_latency_ms": metrics["p95_latency_ms"],
            "p99_latency_ms": metrics["p99_latency_ms"],
            "by_type": metrics["by_type"],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def get_job_slo_metrics(self, db: Session, window_minutes: int = 60) -> Dict:
        """获取任务中心 SLO 指标"""
        cache_key = f"job_slo:{window_minutes}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        start_time = datetime.utcnow().timestamp() - max(window_minutes, 1) * 60
        rows = (
            db.query(AdminJob)
            .filter(AdminJob.created_at >= datetime.utcfromtimestamp(start_time))
            .order_by(AdminJob.created_at.desc())
            .all()
        )
        total = len(rows)
        succeeded = sum(1 for item in rows if item.status == "succeeded")
        failed = sum(1 for item in rows if item.status == "failed")
        cancelled = sum(1 for item in rows if item.status == "cancelled")
        running = sum(1 for item in rows if item.status == "running")
        pending = sum(1 for item in rows if item.status == "pending")
        timeout_failed = sum(
            1
            for item in rows
            if item.status == "failed"
            and isinstance(item.error_message, str)
            and item.error_message.startswith("JobExecutionTimeoutError")
        )

        durations: list[float] = []
        for item in rows:
            if item.status != "succeeded":
                continue
            if not item.started_at or not item.finished_at:
                continue
            try:
                sec = (item.finished_at - item.started_at).total_seconds()
                if sec >= 0:
                    durations.append(sec * 1000)
            except Exception:
                continue
        durations.sort()

        def percentile(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            pos = min(max(int(round((len(values) - 1) * p)), 0), len(values) - 1)
            return round(values[pos], 3)

        success_rate = round((succeeded / total), 6) if total > 0 else 0.0
        timeout_rate = round((timeout_failed / total), 6) if total > 0 else 0.0
        return self._set_cached(cache_key, {
            "window_minutes": window_minutes,
            "total_jobs": total,
            "succeeded_jobs": succeeded,
            "failed_jobs": failed,
            "cancelled_jobs": cancelled,
            "running_jobs": running,
            "pending_jobs": pending,
            "timeout_failed_jobs": timeout_failed,
            "success_rate": success_rate,
            "timeout_rate": timeout_rate,
            "p95_duration_ms": percentile(durations, 0.95),
            "p99_duration_ms": percentile(durations, 0.99),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })

    def get_log_severity_metrics(self, db: Session, window_minutes: int = 60) -> Dict:
        """获取日志分级与告警路由指标。"""
        cache_key = f"log_severity:{window_minutes}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        start_time = datetime.utcnow().timestamp() - max(window_minutes, 1) * 60
        rows = (
            db.query(AdminLog)
            .filter(AdminLog.created_at >= datetime.utcfromtimestamp(start_time))
            .order_by(AdminLog.created_at.desc())
            .all()
        )
        total = len(rows)
        severity_counts = {"info": 0, "warn": 0, "error": 0}
        status_counts: Dict[str, int] = {}
        action_counts: Dict[str, int] = {}
        resource_counts: Dict[str, int] = {}
        routed_alerts: list[Dict[str, Any]] = []

        for row in rows:
            severity = classify_log_severity(
                status=row.status,
                action=row.action,
                error_message=row.error_message,
            )
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            status_key = str(row.status or "unknown")
            action_key = str(row.action or "unknown")
            resource_key = str(row.resource or "unknown")
            status_counts[status_key] = status_counts.get(status_key, 0) + 1
            action_counts[action_key] = action_counts.get(action_key, 0) + 1
            resource_counts[resource_key] = resource_counts.get(resource_key, 0) + 1
            if severity in {"warn", "error"}:
                routed_alerts.append(
                    {
                        "id": row.id,
                        "severity": severity,
                        "action": row.action,
                        "resource": row.resource,
                        "resource_id": row.resource_id,
                        "status": row.status,
                        "trace_id": row.trace_id,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "message": row.error_message,
                    }
                )

        def top_items(values: Dict[str, int], limit: int = 8) -> list[Dict[str, Any]]:
            return [
                {"name": name, "count": count}
                for name, count in sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]
            ]

        error_count = int(severity_counts.get("error", 0))
        warn_count = int(severity_counts.get("warn", 0))
        failed_count = int(status_counts.get("failed", 0))
        return self._set_cached(cache_key, {
            "window_minutes": window_minutes,
            "total_logs": total,
            "failed_logs": failed_count,
            "severity_counts": severity_counts,
            "status_counts": status_counts,
            "error_rate": round(error_count / total, 6) if total else 0.0,
            "warn_rate": round(warn_count / total, 6) if total else 0.0,
            "failed_rate": round(failed_count / total, 6) if total else 0.0,
            "top_actions": top_items(action_counts),
            "top_resources": top_items(resource_counts),
            "alert_routes": {
                "error": {
                    "policy": "page_or_webhook",
                    "count": error_count,
                    "threshold_env": "ALERT_LOG_ERROR_RATE_THRESHOLD",
                },
                "warn": {
                    "policy": "webhook_or_digest",
                    "count": warn_count,
                    "threshold_env": "ALERT_LOG_WARN_RATE_THRESHOLD",
                },
            },
            "recent_alerts": routed_alerts[:20],
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })

    def get_unified_metrics_snapshot(
        self,
        db: Session,
        *,
        api_window_seconds: int = 900,
        qa_window_seconds: int = 900,
        job_window_minutes: int = 60,
    ) -> Dict:
        """获取统一指标快照，聚合 API / QA / Jobs 的核心运营指标。"""
        api_metrics = self.get_performance_metrics(window_seconds=api_window_seconds)
        qa_metrics = self.get_qa_quality_metrics(window_seconds=qa_window_seconds)
        job_metrics = self.get_job_slo_metrics(db, window_minutes=job_window_minutes)
        log_metrics = self.get_log_severity_metrics(db, window_minutes=job_window_minutes)

        summary = {
            "api_error_rate": api_metrics["error_rate"],
            "api_requests_per_second": api_metrics["requests_per_second"],
            "qa_success_rate": qa_metrics["success_rate"],
            "qa_citation_rate": qa_metrics["citation_rate"],
            "job_success_rate": job_metrics["success_rate"],
            "job_timeout_rate": job_metrics["timeout_rate"],
            "log_error_rate": log_metrics["error_rate"],
            "log_warn_rate": log_metrics["warn_rate"],
        }

        return {
            "summary": summary,
            "api": api_metrics,
            "qa": qa_metrics,
            "jobs": job_metrics,
            "logs": log_metrics,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def get_slo_snapshot(self, db: Session, *, api_window_seconds: int = 900, job_window_minutes: int = 60) -> Dict:
        api_metrics = self.get_performance_metrics(window_seconds=api_window_seconds)
        job_metrics = self.get_job_slo_metrics(db, window_minutes=job_window_minutes)
        log_metrics = self.get_log_severity_metrics(db, window_minutes=job_window_minutes)

        return {
            "api": api_metrics,
            "jobs": job_metrics,
            "logs": log_metrics,
            "slo": {
                "api_error_rate": {
                    "value": api_metrics["error_rate"],
                    "target": "<=0.01",
                },
                "job_success_rate": {
                    "value": job_metrics["success_rate"],
                    "target": ">=0.99",
                },
                "job_timeout_rate": {
                    "value": job_metrics["timeout_rate"],
                    "target": "<=0.10",
                },
                "job_p95_duration_ms": {
                    "value": job_metrics["p95_duration_ms"],
                    "target": "track",
                },
                "log_error_rate": {
                    "value": log_metrics["error_rate"],
                    "target": "<=0.02",
                },
                "log_warn_rate": {
                    "value": log_metrics["warn_rate"],
                    "target": "<=0.10",
                },
            },
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    def check_and_send_alerts(
        self,
        db: Session,
        *,
        send_webhook: bool = True,
        api_window_seconds: int = 900,
        job_window_minutes: int = 60,
    ) -> Dict:
        snapshot = self.get_slo_snapshot(
            db,
            api_window_seconds=api_window_seconds,
            job_window_minutes=job_window_minutes,
        )
        api_error_threshold = float(os.getenv("ALERT_API_ERROR_RATE_THRESHOLD", "0.05"))
        timeout_threshold = float(os.getenv("ALERT_JOB_TIMEOUT_RATE_THRESHOLD", "0.10"))
        log_error_threshold = float(os.getenv("ALERT_LOG_ERROR_RATE_THRESHOLD", "0.02"))
        log_warn_threshold = float(os.getenv("ALERT_LOG_WARN_RATE_THRESHOLD", "0.10"))

        alerts = []
        api_error_rate = float(snapshot["api"]["error_rate"])
        job_timeout_rate = float(snapshot["jobs"]["timeout_rate"])
        log_error_rate = float(snapshot["logs"]["error_rate"])
        log_warn_rate = float(snapshot["logs"]["warn_rate"])

        if api_error_rate > api_error_threshold:
            alerts.append(
                {
                    "type": "api_error_rate_high",
                    "severity": "warning",
                    "message": f"API 错误率过高: {api_error_rate:.4f} > {api_error_threshold:.4f}",
                }
            )
        if job_timeout_rate > timeout_threshold:
            alerts.append(
                {
                    "type": "job_timeout_rate_high",
                    "severity": "warning",
                    "message": f"任务超时率过高: {job_timeout_rate:.4f} > {timeout_threshold:.4f}",
                }
            )
        if log_error_rate > log_error_threshold:
            alerts.append(
                {
                    "type": "log_error_rate_high",
                    "severity": "error",
                    "message": f"错误日志占比过高: {log_error_rate:.4f} > {log_error_threshold:.4f}",
                    "route": "page_or_webhook",
                }
            )
        if log_warn_rate > log_warn_threshold:
            alerts.append(
                {
                    "type": "log_warn_rate_high",
                    "severity": "warning",
                    "message": f"警告日志占比过高: {log_warn_rate:.4f} > {log_warn_threshold:.4f}",
                    "route": "webhook_or_digest",
                }
            )

        webhook_url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
        delivered = False
        delivery_error = None

        if alerts and send_webhook and webhook_url:
            payload = {
                "source": "GraphInsight",
                "type": "slo_alert",
                "alerts": alerts,
                "snapshot": snapshot,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=8):  # noqa: S310
                    delivered = True
            except Exception as exc:  # noqa: BLE001
                delivery_error = str(exc)
                logger.warning("告警 webhook 发送失败", context={"error": delivery_error})

        return {
            "alerts": alerts,
            "alert_count": len(alerts),
            "sent": delivered,
            "delivery_error": delivery_error,
            "webhook_configured": bool(webhook_url),
            "snapshot": snapshot,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }


# 创建全局实例
monitor_service = MonitorService()
