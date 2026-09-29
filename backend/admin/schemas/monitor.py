"""
监控相关 Pydantic 模型
"""
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field


class DatabaseStatus(BaseModel):
    """数据库状态"""
    connected: bool = Field(..., description="是否连接")
    database: str = Field(..., description="数据库名称")
    tables_count: Optional[int] = Field(None, description="表数量")
    error: Optional[str] = Field(None, description="错误信息")


class Neo4jStatus(BaseModel):
    """Neo4j 状态"""
    connected: bool = Field(..., description="是否连接")
    uri: str = Field(..., description="连接地址")
    database: str = Field(..., description="数据库名称")
    nodes_count: Optional[int] = Field(None, description="节点数量")
    relationships_count: Optional[int] = Field(None, description="关系数量")
    error: Optional[str] = Field(None, description="错误信息")


class AIServiceStatus(BaseModel):
    """AI服务状态"""
    connected: bool = Field(..., description="是否连接")
    service_name: str = Field(..., description="服务名称")
    model: Optional[str] = Field(None, description="模型名称")
    api_key_configured: bool = Field(..., description="API Key是否配置")
    error: Optional[str] = Field(None, description="错误信息")


class SystemStats(BaseModel):
    """系统统计"""
    cpu_percent: float = Field(..., description="CPU 使用率")
    memory_percent: float = Field(..., description="内存使用率")
    memory_used_mb: float = Field(..., description="已使用内存(MB)")
    memory_total_mb: float = Field(..., description="总内存(MB)")
    disk_percent: float = Field(..., description="磁盘使用率")
    disk_used_gb: float = Field(..., description="已使用磁盘(GB)")
    disk_total_gb: float = Field(..., description="总磁盘(GB)")
    uptime_seconds: float = Field(..., description="运行时间(秒)")
    timestamp: datetime = Field(..., description="统计时间")


class HealthStatus(BaseModel):
    """健康状态"""
    status: str = Field(..., description="状态: healthy, degraded, unhealthy")
    timestamp: datetime = Field(..., description="检查时间")
    database: DatabaseStatus = Field(..., description="数据库状态")
    neo4j: Neo4jStatus = Field(..., description="Neo4j 状态")
    ai_service: Optional[AIServiceStatus] = Field(None, description="AI服务状态")
    system: Optional[SystemStats] = Field(None, description="系统统计")
    checks: Dict[str, bool] = Field(..., description="检查项")
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
                "timestamp": "2025-11-26T10:00:00Z",
                "database": {
                    "connected": True,
                    "database": "graphinsight_admin",
                    "tables_count": 3
                },
                "neo4j": {
                    "connected": True,
                    "uri": "bolt://localhost:7687",
                    "database": "neo4j",
                    "nodes_count": 1000,
                    "relationships_count": 2000
                },
                "checks": {
                    "database": True,
                    "neo4j": True,
                    "disk_space": True
                }
            }
        }


class PerformanceMetrics(BaseModel):
    """性能指标"""
    avg_response_time_ms: float = Field(..., description="平均响应时间(ms)")
    p95_response_time_ms: float = Field(..., description="P95响应时间(ms)")
    p99_response_time_ms: float = Field(..., description="P99响应时间(ms)")
    requests_per_second: float = Field(..., description="每秒请求数")
    error_rate: float = Field(..., description="错误率")
    total_requests: int = Field(..., description="总请求数")
    failed_requests: int = Field(..., description="失败请求数")
    timestamp: datetime = Field(..., description="统计时间")
