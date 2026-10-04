"""
配置相关 Pydantic 模型
"""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class ConfigItem(BaseModel):
    """配置项"""
    id: int
    category: str = Field(..., description="配置分类")
    key: str = Field(..., description="配置键")
    value: str = Field(..., description="配置值")
    description: Optional[str] = Field(None, description="描述")
    is_sensitive: bool = Field(False, description="是否敏感")
    is_encrypted: bool = Field(False, description="是否加密")
    updated_by: Optional[int] = Field(None, description="更新人ID")
    updated_at: datetime = Field(..., description="更新时间")
    version: int = Field(1, description="版本号")
    
    class Config:
        from_attributes = True


class ConfigCreate(BaseModel):
    """创建配置"""
    category: str = Field(..., min_length=1, max_length=50, description="配置分类")
    key: str = Field(..., min_length=1, max_length=100, description="配置键")
    value: str = Field(..., description="配置值")
    description: Optional[str] = Field(None, max_length=500, description="描述")
    is_sensitive: bool = Field(False, description="是否敏感")


class ConfigUpdate(BaseModel):
    """更新配置"""
    value: str = Field(..., description="配置值")
    description: Optional[str] = Field(None, max_length=500, description="描述")


class ConfigQuery(BaseModel):
    """配置查询"""
    category: Optional[str] = Field(None, description="配置分类")
    key: Optional[str] = Field(None, description="配置键")
    is_sensitive: Optional[bool] = Field(None, description="是否敏感")
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(10, ge=1, le=100, description="每页大小")


class ConfigListResponse(BaseModel):
    """配置列表响应"""
    items: List[ConfigItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ConfigBatchUpdate(BaseModel):
    """批量更新配置"""
    configs: List[dict] = Field(..., description="配置列表")
    
    class Config:
        json_schema_extra = {
            "example": {
                "configs": [
                    {"category": "ai_service", "key": "api_key", "value": "sk-xxx"},
                    {"category": "ai_service", "key": "model", "value": "gpt-4"}
                ]
            }
        }
