"""
配置服务
处理配置管理、缓存等业务逻辑
"""
import os
import time
from typing import Optional, Dict, List, Tuple
from functools import lru_cache
from datetime import datetime, timedelta
import httpx
from sqlalchemy.orm import Session
from neo4j import GraphDatabase
from config import get_settings
from services.openai_client_factory import build_httpx_client

from ..models import AdminUser
from ..crud import config_crud, log_crud
from ..schemas.config import (
    ConfigItem,
    ConfigCreate,
    ConfigUpdate,
    ConfigQuery,
    ConfigBatchUpdate,
)
from ..schemas.logs import LogCreate
from core import (
    BusinessException,
    NotFoundException,
    ValidationException,
    get_logger,
)

logger = get_logger()
settings = get_settings()


class ConfigService:
    """配置服务类"""

    def __init__(self):
        self._cache: Dict[str, Tuple[str, datetime]] = {}
        self._cache_ttl = timedelta(minutes=5)  # 缓存5分钟
        self._last_model_connection_test: Optional[Dict[str, any]] = None

    def _get_cache_key(self, category: str, key: str) -> str:
        """生成缓存键"""
        return f"{category}:{key}"

    def _get_from_cache(self, category: str, key: str) -> Optional[str]:
        """从缓存获取配置"""
        cache_key = self._get_cache_key(category, key)
        if cache_key in self._cache:
            value, timestamp = self._cache[cache_key]
            if datetime.utcnow() - timestamp < self._cache_ttl:
                logger.debug(f"从缓存获取配置: {cache_key}")
                return value
            else:
                # 缓存过期，删除
                del self._cache[cache_key]
        return None

    def _set_to_cache(self, category: str, key: str, value: str):
        """设置缓存"""
        cache_key = self._get_cache_key(category, key)
        self._cache[cache_key] = (value, datetime.utcnow())
        logger.debug(f"设置缓存: {cache_key}")

    def _clear_cache(self, category: Optional[str] = None, key: Optional[str] = None):
        """清除缓存"""
        if category and key:
            cache_key = self._get_cache_key(category, key)
            if cache_key in self._cache:
                del self._cache[cache_key]
                logger.debug(f"清除缓存: {cache_key}")
        elif category:
            # 清除整个分类的缓存
            keys_to_delete = [
                k for k in self._cache.keys()
                if k.startswith(f"{category}:")
            ]
            for k in keys_to_delete:
                del self._cache[k]
            logger.debug(f"清除分类缓存: {category}")
        else:
            # 清除所有缓存
            self._cache.clear()
            logger.debug("清除所有缓存")

    def get_config(
        self,
        db: Session,
        category: str,
        key: str,
        default: Optional[str] = None,
        use_cache: bool = True
    ) -> Optional[str]:
        """
        获取配置值

        优先级: 缓存 > 数据库 > 环境变量 > 默认值
        """
        try:
            # 1. 尝试从缓存获取
            if use_cache:
                cached_value = self._get_from_cache(category, key)
                if cached_value is not None:
                    return cached_value

            # 2. 从数据库获取
            config = config_crud.get_by_key(db, category, key)
            if config:
                value = config.value
                if use_cache:
                    self._set_to_cache(category, key, value)
                return value

            # 3. 从环境变量获取
            env_key = f"{category.upper()}_{key.upper()}"
            env_value = os.getenv(env_key)
            if env_value:
                logger.debug(f"从环境变量获取配置: {env_key}")
                return env_value

            # 4. 返回默认值
            return default

        except Exception as e:
            logger.error(f"获取配置失败: {category}.{key}, {str(e)}")
            return default

    def set_config(
        self,
        db: Session,
        category: str,
        key: str,
        value: str,
        *,
        user_id: int = 0,
        description: Optional[str] = None,
        is_sensitive: Optional[bool] = None,
    ) -> ConfigItem:
        """
        设置配置（upsert）

        - 已存在：更新 value（可选更新 description）
        - 不存在：自动创建
        """
        try:
            value_str = "" if value is None else str(value)
            sensitive = is_sensitive
            if sensitive is None:
                lower_key = key.lower()
                sensitive = any(flag in lower_key for flag in ["password", "secret", "token", "key"])

            existed = config_crud.get_by_key(db, category, key)
            if existed:
                updated = config_crud.update(
                    db=db,
                    category=category,
                    key=key,
                    config_update=ConfigUpdate(value=value_str, description=description),
                    user_id=user_id,
                )
                if not updated:
                    raise BusinessException(f"配置更新失败: {category}.{key}")
                self._clear_cache(category, key)
                return ConfigItem.model_validate(updated)

            created = config_crud.create(
                db=db,
                config_create=ConfigCreate(
                    category=category,
                    key=key,
                    value=value_str,
                    description=description,
                    is_sensitive=sensitive,
                ),
                user_id=user_id,
            )
            self._clear_cache(category, key)
            return ConfigItem.model_validate(created)
        except Exception as e:
            logger.error(f"设置配置失败: {category}.{key}, {str(e)}", exc_info=True)
            raise

    def get_config_item(
        self,
        db: Session,
        category: str,
        key: str
    ) -> ConfigItem:
        """获取配置项（完整信息）"""
        try:
            config = config_crud.get_by_key(db, category, key)
            if not config:
                default_item = self._resolve_default_config(category, key)
                if default_item:
                    try:
                        created = config_crud.create(
                            db,
                            config_create=ConfigCreate(
                                category=category,
                                key=key,
                                value=default_item["value"],
                                description=default_item["description"],
                                is_sensitive=default_item["is_sensitive"],
                            ),
                            user_id=None,
                        )
                        self._clear_cache(category, key)
                        return ConfigItem.model_validate(created)
                    except Exception as exc:
                        logger.warning(
                            f"默认配置写入失败: {category}.{key}",
                            context={"error": str(exc)},
                        )
                        return ConfigItem(
                            id=0,
                            category=category,
                            key=key,
                            value=default_item["value"],
                            description=default_item["description"],
                            is_sensitive=default_item["is_sensitive"],
                            is_encrypted=False,
                            updated_by=None,
                            updated_at=None,
                            version=1,
                        )
                raise NotFoundException(f"配置不存在: {category}.{key}")

            return ConfigItem.model_validate(config)
        except NotFoundException:
            raise
        except Exception as e:
            logger.error(f"获取配置项失败: {str(e)}", exc_info=True)
            raise BusinessException("获取配置失败")

    @staticmethod
    def _resolve_default_config(category: str, key: str) -> Optional[Dict[str, str]]:
        category = (category or "").strip()
        key = (key or "").strip()
        if not category or not key:
            return None
        defaults: Dict[str, Dict[str, Dict[str, str]]] = {
            "neo4j": {
                "uri": {
                    "value": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                    "description": "Neo4j 连接地址",
                    "is_sensitive": "false",
                },
                "user": {
                    "value": os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j")),
                    "description": "Neo4j 用户名",
                    "is_sensitive": "false",
                },
                "password": {
                    "value": os.getenv("NEO4J_PASSWORD", "password"),
                    "description": "Neo4j 密码",
                    "is_sensitive": "true",
                },
                "database": {
                    "value": os.getenv("NEO4J_DATABASE", "neo4j"),
                    "description": "Neo4j 数据库名称",
                    "is_sensitive": "false",
                },
            },
            "ai_service": {
                "provider": {
                    "value": os.getenv("AI_SERVICE_PROVIDER", os.getenv("OPENAI_PROVIDER", "openai")),
                    "description": "AI 服务提供商",
                    "is_sensitive": "false",
                },
                "enabled": {
                    "value": os.getenv("AI_SERVICE_ENABLED", "true"),
                    "description": "是否启用 AI 服务",
                    "is_sensitive": "false",
                },
                "api_key": {
                    "value": os.getenv("AI_SERVICE_API_KEY", os.getenv("OPENAI_API_KEY", "")),
                    "description": "AI 服务 API Key",
                    "is_sensitive": "true",
                },
                "base_url": {
                    "value": os.getenv("AI_SERVICE_BASE_URL", os.getenv("OPENAI_BASE_URL", "")),
                    "description": "AI 服务 API 地址",
                    "is_sensitive": "false",
                },
                "model": {
                    "value": os.getenv("AI_SERVICE_MODEL", os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")),
                    "description": "AI 模型名称",
                    "is_sensitive": "false",
                },
                "docqa_reasoning_profile": {
                    "value": os.getenv("AI_SERVICE_DOCQA_REASONING_PROFILE", "balanced"),
                    "description": "文档问答默认推理档位",
                    "is_sensitive": "false",
                },
                "deep_research_reasoning_profile": {
                    "value": os.getenv("AI_SERVICE_DEEP_RESEARCH_REASONING_PROFILE", "deep"),
                    "description": "深度调研默认推理档位",
                    "is_sensitive": "false",
                },
                "model_probe_reasoning_profile": {
                    "value": os.getenv("AI_SERVICE_MODEL_PROBE_REASONING_PROFILE", "fast"),
                    "description": "模型连通性测试默认推理档位",
                    "is_sensitive": "false",
                },
                "graph_extract_reasoning_profile": {
                    "value": os.getenv("AI_SERVICE_GRAPH_EXTRACT_REASONING_PROFILE", "fast"),
                    "description": "图谱抽取默认推理档位",
                    "is_sensitive": "false",
                },
                "graph_extract_complex_reasoning_profile": {
                    "value": os.getenv("AI_SERVICE_GRAPH_EXTRACT_COMPLEX_REASONING_PROFILE", "balanced"),
                    "description": "复杂图谱抽取默认推理档位",
                    "is_sensitive": "false",
                },
                "max_tokens": {
                    "value": os.getenv("AI_SERVICE_MAX_TOKENS", os.getenv("OPENAI_MAX_TOKENS", "2000")),
                    "description": "最大 Token 数",
                    "is_sensitive": "false",
                },
                "temperature": {
                    "value": os.getenv("AI_SERVICE_TEMPERATURE", os.getenv("OPENAI_TEMPERATURE", "0.7")),
                    "description": "温度参数",
                    "is_sensitive": "false",
                },
            },
            "retrieval": {
                "mode": {
                    "value": os.getenv("DOCQA_RETRIEVAL_MODE", "keyword"),
                    "description": "文档问答检索模式(keyword/vector/hybrid/graph_hybrid)",
                    "is_sensitive": "false",
                },
                "rrf_k": {
                    "value": os.getenv("DOCQA_RETRIEVAL_RRF_K", "60"),
                    "description": "RRF 融合参数",
                    "is_sensitive": "false",
                },
                "candidate_multiplier": {
                    "value": os.getenv("DOCQA_RETRIEVAL_CANDIDATE_MULTIPLIER", "6"),
                    "description": "候选召回倍数",
                    "is_sensitive": "false",
                },
                "graph_enabled": {
                    "value": os.getenv("DOCQA_RETRIEVAL_GRAPH_ENABLED", "true"),
                    "description": "是否启用图谱扩展召回",
                    "is_sensitive": "false",
                },
                "rerank_enabled": {
                    "value": os.getenv("DOCQA_RERANK_ENABLED", "false"),
                    "description": "是否启用重排器",
                    "is_sensitive": "false",
                },
                "rerank_model": {
                    "value": os.getenv("DOCQA_RERANK_MODEL", ""),
                    "description": "Reranker 模型名称，留空则不执行二阶段重排",
                    "is_sensitive": "false",
                },
                "rerank_base_url": {
                    "value": os.getenv("DOCQA_RERANK_BASE_URL", ""),
                    "description": "Reranker API 地址，留空时复用 AI 服务地址",
                    "is_sensitive": "false",
                },
                "rerank_endpoint_path": {
                    "value": os.getenv("DOCQA_RERANK_ENDPOINT_PATH", "/rerank"),
                    "description": "Reranker 接口路径",
                    "is_sensitive": "false",
                },
                "rerank_top_n": {
                    "value": os.getenv("DOCQA_RERANK_TOP_N", "20"),
                    "description": "进入 Reranker 的融合候选上限",
                    "is_sensitive": "false",
                },
                "rerank_timeout_seconds": {
                    "value": os.getenv("DOCQA_RERANK_TIMEOUT_SECONDS", "15"),
                    "description": "Reranker 请求超时时间",
                    "is_sensitive": "false",
                },
            },
            "embedding": {
                "enabled": {
                    "value": os.getenv("EMBEDDING_ENABLED", "true"),
                    "description": "是否启用 Embedding",
                    "is_sensitive": "false",
                },
                "provider": {
                    "value": os.getenv("EMBEDDING_PROVIDER", os.getenv("AI_SERVICE_PROVIDER", os.getenv("OPENAI_PROVIDER", "openai"))),
                    "description": "Embedding 服务提供商",
                    "is_sensitive": "false",
                },
                "base_url": {
                    "value": os.getenv("EMBEDDING_BASE_URL", ""),
                    "description": "Embedding OpenAI-compatible API 地址；留空时复用 AI 服务地址",
                    "is_sensitive": "false",
                },
                "api_key": {
                    "value": os.getenv("EMBEDDING_API_KEY", os.getenv("AI_SERVICE_API_KEY", os.getenv("OPENAI_API_KEY", ""))),
                    "description": "Embedding API Key",
                    "is_sensitive": "true",
                },
                "model": {
                    "value": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                    "description": "Embedding 模型",
                    "is_sensitive": "false",
                },
                "dimension": {
                    "value": os.getenv("EMBEDDING_DIMENSION", "1536"),
                    "description": "Embedding 维度",
                    "is_sensitive": "false",
                },
                "batch_size": {
                    "value": os.getenv("EMBEDDING_BATCH_SIZE", "32"),
                    "description": "Embedding 批大小",
                    "is_sensitive": "false",
                },
            },
            "vector_store": {
                "enabled": {
                    "value": os.getenv("VECTOR_STORE_ENABLED", "false"),
                    "description": "是否启用向量库",
                    "is_sensitive": "false",
                },
                "provider": {
                    "value": os.getenv("VECTOR_STORE_PROVIDER", "milvus"),
                    "description": "向量库提供商",
                    "is_sensitive": "false",
                },
                "uri": {
                    "value": os.getenv("MILVUS_URI", "http://127.0.0.1:19530"),
                    "description": "Milvus 连接地址",
                    "is_sensitive": "false",
                },
                "db_name": {
                    "value": os.getenv("MILVUS_DB_NAME", "default"),
                    "description": "Milvus 数据库名称",
                    "is_sensitive": "false",
                },
                "collection": {
                    "value": os.getenv("MILVUS_COLLECTION", "graphinsight_chunks"),
                    "description": "Milvus Collection 名称",
                    "is_sensitive": "false",
                },
                "token": {
                    "value": os.getenv("MILVUS_TOKEN", ""),
                    "description": "Milvus Token",
                    "is_sensitive": "true",
                },
            },
            "document_parser": {
                "provider": {
                    "value": os.getenv("DOCUMENT_PARSER_PROVIDER", "native"),
                    "description": "文档解析器提供商(native/mineru)",
                    "is_sensitive": "false",
                },
                "fallback_provider": {
                    "value": os.getenv("DOCUMENT_PARSER_FALLBACK_PROVIDER", "native"),
                    "description": "解析失败时的回退解析器(native/none)",
                    "is_sensitive": "false",
                },
                "base_url": {
                    "value": os.getenv("MINERU_BASE_URL", ""),
                    "description": "MinerU API 地址",
                    "is_sensitive": "false",
                },
                "endpoint_path": {
                    "value": os.getenv("MINERU_ENDPOINT_PATH", "/file_parse"),
                    "description": "MinerU 解析接口路径",
                    "is_sensitive": "false",
                },
                "file_field": {
                    "value": os.getenv("MINERU_FILE_FIELD", "files"),
                    "description": "MinerU multipart 文件字段名",
                    "is_sensitive": "false",
                },
                "parse_mode": {
                    "value": os.getenv("MINERU_PARSE_MODE", "auto"),
                    "description": "MinerU parse_method(auto/ocr/txt)",
                    "is_sensitive": "false",
                },
                "output_format": {
                    "value": os.getenv("MINERU_OUTPUT_FORMAT", "markdown,json"),
                    "description": "MinerU 输出格式",
                    "is_sensitive": "false",
                },
                "timeout_seconds": {
                    "value": os.getenv("MINERU_TIMEOUT_SECONDS", "300"),
                    "description": "MinerU 请求超时时间",
                    "is_sensitive": "false",
                },
            },
            "nl2cypher": {
                "enabled": {
                    "value": os.getenv("NL2CYPHER_ENABLED", "true"),
                    "description": "是否启用 NL2Cypher",
                    "is_sensitive": "false",
                },
                "cache_size": {
                    "value": os.getenv("NL2CYPHER_CACHE_SIZE", "100"),
                    "description": "缓存大小",
                    "is_sensitive": "false",
                },
                "max_limit": {
                    "value": os.getenv("NL2CYPHER_MAX_LIMIT", "100"),
                    "description": "最大查询限制",
                    "is_sensitive": "false",
                },
            },
        }
        payload = defaults.get(category, {}).get(key)
        if not payload:
            return None
        return {
            "value": str(payload["value"]),
            "description": payload["description"],
            "is_sensitive": payload["is_sensitive"] == "true",
        }

    def get_config_list(
        self,
        db: Session,
        query: ConfigQuery
    ) -> Tuple[List[ConfigItem], int]:
        """获取配置列表"""
        try:
            items, total = config_crud.get_list(db, query)

            # 转换为 Pydantic 模型
            config_items = [ConfigItem.model_validate(item) for item in items]

            return config_items, total
        except Exception as e:
            logger.error(f"获取配置列表失败: {str(e)}", exc_info=True)
            raise BusinessException("获取配置列表失败")

    def create_config(
        self,
        db: Session,
        config_create: ConfigCreate,
        user: AdminUser,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None
    ) -> ConfigItem:
        """创建配置"""
        try:
            # 创建配置
            config = config_crud.create(db, config_create, user.id)

            # 记录日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="create",
                resource="config",
                resource_id=str(config.id),
                details={
                    "category": config.category,
                    "key": config.key,
                    "is_sensitive": config.is_sensitive
                },
                ip_address=ip_address,
                status="success"
            ))

            logger.info(
                f"创建配置: {config.category}.{config.key}",
                context={"user_id": user.id, "config_id": config.id}
            )

            return ConfigItem.model_validate(config)
        except Exception as e:
            logger.error(f"创建配置失败: {str(e)}", exc_info=True)
            raise BusinessException("创建配置失败")

    def update_config(
        self,
        db: Session,
        category: str,
        key: str,
        config_update: ConfigUpdate,
        user: AdminUser,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None
    ) -> ConfigItem:
        """更新配置（不存在时自动创建）"""
        try:
            # 更新配置
            config = config_crud.update(db, category, key, config_update, user.id)
            if config:
                # 清除缓存
                self._clear_cache(category, key)

                # 记录日志
                log_crud.create(db, LogCreate(
                    user_id=user.id,
                    operator_id=user.id,
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    action="update",
                    resource="config",
                    resource_id=str(config.id),
                    details={
                        "category": category,
                        "key": key,
                        "old_version": config.version - 1,
                        "new_version": config.version
                    },
                    ip_address=ip_address,
                    status="success"
                ))

                logger.info(
                    f"更新配置: {category}.{key}",
                    context={"user_id": user.id, "config_id": config.id}
                )
                return ConfigItem.model_validate(config)

            # 不存在则自动创建，避免前端首次配置失败
            is_sensitive = any(flag in key.lower() for flag in ["password", "secret", "token", "key"])
            created = config_crud.create(
                db,
                ConfigCreate(
                    category=category,
                    key=key,
                    value=config_update.value,
                    description=config_update.description,
                    is_sensitive=is_sensitive,
                ),
                user.id,
            )
            self._clear_cache(category, key)

            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="create",
                resource="config",
                resource_id=str(created.id),
                details={
                    "category": category,
                    "key": key,
                    "is_sensitive": is_sensitive,
                    "auto_created": True,
                },
                ip_address=ip_address,
                status="success"
            ))

            logger.info(
                f"自动创建配置: {category}.{key}",
                context={"user_id": user.id, "config_id": created.id}
            )
            return ConfigItem.model_validate(created)
        except Exception as e:
            logger.error(f"更新配置失败: {str(e)}", exc_info=True)
            raise BusinessException("更新配置失败")

    def batch_update_configs(
        self,
        db: Session,
        batch_update: ConfigBatchUpdate,
        user: AdminUser,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None
    ) -> int:
        """批量更新配置"""
        try:
            # 批量更新
            updated_count = config_crud.batch_update(
                db,
                batch_update.configs,
                user.id
            )

            # 清除所有缓存
            self._clear_cache()

            # 记录日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="batch_update",
                resource="config",
                details={
                    "count": updated_count,
                    "total": len(batch_update.configs)
                },
                ip_address=ip_address,
                status="success"
            ))

            logger.info(
                f"批量更新配置: {updated_count}/{len(batch_update.configs)}",
                context={"user_id": user.id}
            )

            return updated_count
        except Exception as e:
            logger.error(f"批量更新配置失败: {str(e)}", exc_info=True)
            raise BusinessException("批量更新配置失败")

    def delete_config(
        self,
        db: Session,
        category: str,
        key: str,
        user: AdminUser,
        ip_address: Optional[str] = None,
        tenant_id: Optional[str] = None,
        trace_id: Optional[str] = None
    ) -> bool:
        """删除配置"""
        try:
            # 删除配置
            success = config_crud.delete(db, category, key)
            if not success:
                raise NotFoundException(f"配置不存在: {category}.{key}")

            # 清除缓存
            self._clear_cache(category, key)

            # 记录日志
            log_crud.create(db, LogCreate(
                user_id=user.id,
                operator_id=user.id,
                tenant_id=tenant_id,
                trace_id=trace_id,
                action="delete",
                resource="config",
                details={"category": category, "key": key},
                ip_address=ip_address,
                status="success"
            ))

            logger.info(
                f"删除配置: {category}.{key}",
                context={"user_id": user.id}
            )

            return True
        except NotFoundException:
            raise
        except Exception as e:
            logger.error(f"删除配置失败: {str(e)}", exc_info=True)
            raise BusinessException("删除配置失败")

    # 便捷方法：获取特定分类的配置

    @staticmethod
    def _safe_sensitive_value(value: Optional[str], include_sensitive: bool) -> str:
        if include_sensitive:
            return value or ""
        return ""

    def get_ai_service_config(self, db: Session, include_sensitive: bool = True) -> Dict[str, any]:
        """获取 AI 服务配置"""
        api_key = self.get_config(db, "ai_service", "api_key", "")
        return {
            "provider": self.get_config(db, "ai_service", "provider", "openai"),
            "enabled": self.get_config(db, "ai_service", "enabled", "true").lower() == "true",
            "base_url": self.get_config(db, "ai_service", "base_url", ""),
            "api_key": self._safe_sensitive_value(api_key, include_sensitive),
            "api_key_configured": bool(api_key and api_key.strip() and api_key != "your-api-key-here"),
            "model": self.get_config(db, "ai_service", "model", "gpt-3.5-turbo"),
            "docqa_reasoning_profile": self.get_config(db, "ai_service", "docqa_reasoning_profile", "balanced"),
            "deep_research_reasoning_profile": self.get_config(db, "ai_service", "deep_research_reasoning_profile", "deep"),
            "model_probe_reasoning_profile": self.get_config(db, "ai_service", "model_probe_reasoning_profile", "fast"),
            "graph_extract_reasoning_profile": self.get_config(db, "ai_service", "graph_extract_reasoning_profile", "fast"),
            "graph_extract_complex_reasoning_profile": self.get_config(db, "ai_service", "graph_extract_complex_reasoning_profile", "balanced"),
            "max_tokens": int(self.get_config(db, "ai_service", "max_tokens", "2000")),
            "temperature": float(self.get_config(db, "ai_service", "temperature", "0.7")),
        }

    def get_nl2cypher_config(self, db: Session) -> Dict[str, any]:
        """获取 NL2Cypher 配置"""
        return {
            "enabled": self.get_config(db, "nl2cypher", "enabled", "true").lower() == "true",
            "cache_size": int(self.get_config(db, "nl2cypher", "cache_size", "100")),
            "max_limit": int(self.get_config(db, "nl2cypher", "max_limit", "100")),
        }

    def get_neo4j_config(self, db: Session, include_sensitive: bool = True) -> Dict[str, str]:
        """获取 Neo4j 配置"""
        mode = str(getattr(settings, "neo4j_config_source", "env") or "env").strip().lower()
        if mode == "env":
            env_user = os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j"))
            env_password = os.getenv("NEO4J_PASSWORD", "password")
            return {
                "uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                "user": env_user,
                "username": env_user,
                "password": self._safe_sensitive_value(env_password, include_sensitive),
                "password_configured": bool(env_password and env_password.strip()),
                "database": os.getenv("NEO4J_DATABASE", "neo4j"),
                "source": "env",
                "mode": "env",
            }

        db_user = config_crud.get_by_key(db, "neo4j", "user")
        db_username = config_crud.get_by_key(db, "neo4j", "username")
        db_uri = config_crud.get_by_key(db, "neo4j", "uri")
        db_pwd = config_crud.get_by_key(db, "neo4j", "password")
        db_database = config_crud.get_by_key(db, "neo4j", "database")
        has_admin_value = any(
            item and str(item.value or "").strip()
            for item in [db_uri, db_user, db_username, db_pwd, db_database]
        )

        user_value = (
            self.get_config(db, "neo4j", "user", None)
            or self.get_config(db, "neo4j", "username", None)
            or "neo4j"
        )
        password_value = self.get_config(db, "neo4j", "password", "password")
        return {
            "uri": self.get_config(db, "neo4j", "uri", "bolt://localhost:7687"),
            "user": user_value,
            # 仅用于兼容历史读取方，严格模式仍推荐使用 user
            "username": user_value,
            "password": self._safe_sensitive_value(password_value, include_sensitive),
            "password_configured": bool(password_value and password_value.strip()),
            "database": self.get_config(db, "neo4j", "database", "neo4j"),
            "source": "admin_config" if has_admin_value else "env_fallback",
            "mode": mode,
        }

    def test_neo4j_connection(self, db: Session) -> Dict[str, any]:
        """测试 Neo4j 连接"""
        try:
            config = self.get_neo4j_config(db)
            uri = config.get("uri") or "bolt://localhost:7687"
            user = config.get("user") or config.get("username") or "neo4j"
            password = config.get("password") or ""
            database = config.get("database") or "neo4j"

            driver = GraphDatabase.driver(
                uri,
                auth=(user, password),
                max_connection_pool_size=10,
                connection_timeout=8,
            )
            try:
                driver.verify_connectivity()
                with driver.session(database=database) as session:
                    record = session.run("RETURN 1 as test").single()
                    if record and record.get("test") == 1:
                        return {
                            "success": True,
                            "message": f"Neo4j 连接成功 ({uri})"
                        }
                return {
                    "success": False,
                    "message": f"Neo4j 连接失败: 查询无结果 ({uri})"
                }
            finally:
                driver.close()

        except Exception as e:
            logger.error(f"测试 Neo4j 连接失败: {str(e)}")
            return {
                "success": False,
                "message": f"Neo4j 连接失败: {str(e)}"
            }

    def test_ai_service_connection(self, db: Session) -> Dict[str, any]:
        """测试 AI 服务连接"""
        try:
            # 获取配置
            config = self.get_ai_service_config(db)
            provider = config.get("provider", "openai")
            enabled = config.get("enabled", True)
            api_key = config.get("api_key", "")

            if not enabled:
                return {
                    "success": False,
                    "message": "AI服务未启用"
                }

            if not api_key or not api_key.strip() or api_key == "your-api-key-here":
                return {
                    "success": False,
                    "message": f"{provider.upper()} API Key 未配置"
                }

            # API Key 是服务商定义的不透明凭据，不按前缀推断有效性。
            # 实际鉴权与连通性由模型连接测试发起真实请求验证。
            if provider == "openai":
                return {
                    "success": True,
                    "message": "OpenAI API 配置完整；请使用“测试当前模型”验证实际鉴权与连通性"
                }
            elif provider == "claude":
                return {
                    "success": True,
                    "message": "Claude API 配置完整；请使用“测试当前模型”验证实际鉴权与连通性"
                }
            elif provider == "openai_compatible":
                # OpenAI 兼容接口（智谱、通义千问等），只检查是否配置
                base_url = config.get("base_url", "")
                if not base_url:
                    return {
                        "success": False,
                        "message": "OpenAI 兼容接口需要配置 API 地址"
                    }
                return {
                    "success": True,
                    "message": f"OpenAI 兼容接口已配置 (API: {base_url})"
                }
            else:
                # 其他 provider，只检查是否配置
                return {
                    "success": True,
                    "message": f"{provider.upper()} API Key 已配置"
                }

        except Exception as e:
            logger.error(f"测试 AI 服务连接失败: {str(e)}")
            return {
                "success": False,
                "message": f"测试失败: {str(e)}"
            }

    @staticmethod
    def _build_chat_completion_url(base_url: str) -> str:
        normalized = (base_url or "").strip().rstrip("/")
        if not normalized:
            return "https://api.openai.com/v1/chat/completions"
        if normalized.endswith("/chat/completions"):
            return normalized
        if normalized.endswith("/v1") or "/v1/" in normalized:
            return f"{normalized}/chat/completions"
        return f"{normalized}/v1/chat/completions"

    @staticmethod
    def _build_claude_messages_url(base_url: str) -> str:
        normalized = (base_url or "").strip().rstrip("/")
        if not normalized:
            return "https://api.anthropic.com/v1/messages"
        if normalized.endswith("/messages"):
            return normalized
        if normalized.endswith("/v1") or "/v1/" in normalized:
            return f"{normalized}/messages"
        return f"{normalized}/v1/messages"

    def test_model_connection(self, db: Session) -> Dict[str, any]:
        """真实探测模型网关与当前模型。"""
        checked_at = datetime.utcnow().isoformat() + "Z"
        started_at = time.perf_counter()
        checks: List[Dict[str, any]] = []

        def finish(result: Dict[str, any]) -> Dict[str, any]:
            result["checked_at"] = checked_at
            result["latency_ms"] = round((time.perf_counter() - started_at) * 1000, 3)
            result["checks"] = checks
            self._last_model_connection_test = result
            return result

        try:
            import httpx

            config = self.get_ai_service_config(db)
            provider = str(config.get("provider") or "openai").strip()
            enabled = bool(config.get("enabled"))
            api_key = str(config.get("api_key") or "").strip()
            base_url = str(config.get("base_url") or "").strip()
            model = str(config.get("model") or "").strip()

            if not enabled:
                checks.append({"name": "enabled", "success": False, "message": "AI 服务未启用"})
                return finish(
                    {
                        "success": False,
                        "message": "AI 服务未启用",
                        "provider": provider,
                        "model": model,
                        "base_url": base_url,
                        "endpoint": None,
                    }
                )

            checks.append({"name": "enabled", "success": True, "message": "AI 服务已启用"})

            if not api_key or api_key == "your-api-key-here":
                checks.append({"name": "api_key", "success": False, "message": "API Key 未配置"})
                return finish(
                    {
                        "success": False,
                        "message": "API Key 未配置",
                        "provider": provider,
                        "model": model,
                        "base_url": base_url,
                        "endpoint": None,
                    }
                )

            checks.append({"name": "api_key", "success": True, "message": "API Key 已配置"})

            if not model:
                checks.append({"name": "model", "success": False, "message": "模型未配置"})
                return finish(
                    {
                        "success": False,
                        "message": "模型未配置",
                        "provider": provider,
                        "model": model,
                        "base_url": base_url,
                        "endpoint": None,
                    }
                )

            checks.append({"name": "model", "success": True, "message": f"当前模型: {model}"})

            if provider == "claude":
                endpoint = self._build_claude_messages_url(base_url)
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "Reply with ok."}],
                }
                request_started = time.perf_counter()
                with build_httpx_client(timeout=20.0) as client:
                    response = client.post(endpoint, headers=headers, json=payload)
                request_ms = round((time.perf_counter() - request_started) * 1000, 3)
                if response.status_code < 400:
                    checks.append(
                        {
                            "name": "probe",
                            "success": True,
                            "message": f"模型探测成功，HTTP {response.status_code}",
                            "latency_ms": request_ms,
                        }
                    )
                    return finish(
                        {
                            "success": True,
                            "message": "模型连通性测试成功",
                            "provider": provider,
                            "model": model,
                            "base_url": base_url or "https://api.anthropic.com/v1",
                            "endpoint": endpoint,
                        }
                    )

                body = response.text[:240]
                checks.append(
                    {
                        "name": "probe",
                        "success": False,
                        "message": f"模型探测失败，HTTP {response.status_code}: {body}",
                        "latency_ms": request_ms,
                    }
                )
                return finish(
                    {
                        "success": False,
                        "message": f"模型探测失败，HTTP {response.status_code}",
                        "provider": provider,
                        "model": model,
                        "base_url": base_url or "https://api.anthropic.com/v1",
                        "endpoint": endpoint,
                    }
                )

            endpoint = self._build_chat_completion_url(base_url)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with ok."}],
                "max_tokens": 8,
                "temperature": 0,
            }
            request_started = time.perf_counter()
            with build_httpx_client(timeout=20.0) as client:
                response = client.post(endpoint, headers=headers, json=payload)
            request_ms = round((time.perf_counter() - request_started) * 1000, 3)

            if response.status_code < 400:
                checks.append(
                    {
                        "name": "probe",
                        "success": True,
                        "message": f"模型探测成功，HTTP {response.status_code}",
                        "latency_ms": request_ms,
                    }
                )
                return finish(
                    {
                        "success": True,
                        "message": "模型连通性测试成功",
                        "provider": provider,
                        "model": model,
                        "base_url": base_url or "https://api.openai.com/v1",
                        "endpoint": endpoint,
                    }
                )

            body = response.text[:240]
            checks.append(
                {
                    "name": "probe",
                    "success": False,
                    "message": f"模型探测失败，HTTP {response.status_code}: {body}",
                    "latency_ms": request_ms,
                }
            )
            return finish(
                {
                    "success": False,
                    "message": f"模型探测失败，HTTP {response.status_code}",
                    "provider": provider,
                    "model": model,
                    "base_url": base_url or "https://api.openai.com/v1",
                    "endpoint": endpoint,
                }
            )
        except Exception as e:
            logger.warning("模型连通性测试异常", context={"error": str(e)})
            checks.append({"name": "probe", "success": False, "message": str(e)})
            return finish(
                {
                    "success": False,
                    "message": f"模型连通性测试失败: {str(e)}",
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "endpoint": None,
                }
            )

    def get_last_model_connection_test(self) -> Optional[Dict[str, any]]:
        """获取最近一次模型连通性测试结果（进程内留存）。"""
        return self._last_model_connection_test

    def _build_models_urls(self, base_url: str) -> List[str]:
        """把 base_url 规整为候选 models 接口地址（兼容 /v1 与非 /v1）。"""
        normalized = (base_url or "").strip().rstrip("/")
        if not normalized:
            return ["https://api.openai.com/v1/models"]
        if normalized.endswith("/models"):
            return [normalized]

        candidates: List[str] = []
        # 常见 OpenAI-compatible：base_url = https://host 或 https://host/v1
        if normalized.endswith("/v1") or "/v1/" in normalized:
            candidates.append(f"{normalized}/models")
        else:
            candidates.append(f"{normalized}/v1/models")
            candidates.append(f"{normalized}/models")

        # 去重并保持顺序
        unique: List[str] = []
        for url in candidates:
            if url not in unique:
                unique.append(url)
        return unique

    @staticmethod
    def _parse_model_ids(payload: Dict) -> List[str]:
        """兼容多种 OpenAI-compatible 返回结构。"""
        candidates = payload.get("data")
        if isinstance(candidates, list):
            models = [
                item.get("id")
                for item in candidates
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id").strip()
            ]
            return sorted(list(dict.fromkeys(models)))[:30]
        model_list = payload.get("models")
        if isinstance(model_list, list):
            models = [item for item in model_list if isinstance(item, str) and item.strip()]
            return sorted(list(dict.fromkeys(models)))[:30]
        return []

    @staticmethod
    def _guess_compatible_vendor(base_url: str, current_model: str) -> str:
        hint = f"{(base_url or '').lower()} {(current_model or '').lower()}"
        if "bigmodel" in hint or "zhipu" in hint or "glm-" in hint:
            return "zhipu"
        if "dashscope" in hint or "qwen" in hint or "aliyuncs" in hint:
            return "qwen"
        if "deepseek" in hint:
            return "deepseek"
        return "generic"

    def _fallback_models(self, provider: str, base_url: str, current_model: str) -> List[str]:
        openai_models = [
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o",
            "gpt-4o-mini",
        ]
        claude_models = [
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ]
        zhipu_models = [
            "glm-4-plus",
            "glm-4-air",
            "glm-4-long",
            "glm-4-flash",
        ]
        qwen_models = [
            "qwen-max",
            "qwen-plus",
            "qwen-turbo",
            "qwen-long",
        ]
        deepseek_models = [
            "deepseek-chat",
            "deepseek-reasoner",
        ]
        compatible_generic = [
            "gpt-4o-mini",
            "deepseek-chat",
            "qwen-plus",
            "glm-4-flash",
        ]

        if provider == "claude":
            return claude_models
        if provider == "openai":
            return openai_models
        if provider == "openai_compatible":
            vendor = self._guess_compatible_vendor(base_url, current_model)
            if vendor == "zhipu":
                return zhipu_models
            if vendor == "qwen":
                return qwen_models
            if vendor == "deepseek":
                return deepseek_models
            return compatible_generic
        return openai_models

    def get_available_models(self, db: Session, overrides: Optional[Dict[str, str]] = None) -> List[str]:
        """获取可用的 AI 模型列表。优先实时请求，其次回退静态列表。"""
        try:
            config = self.get_ai_service_config(db)
            merged = {
                **config,
                **(overrides or {}),
            }

            provider = str(merged.get("provider", "openai") or "openai").strip()
            api_key = str(merged.get("api_key", "") or "").strip()
            base_url = str(merged.get("base_url", "") or "").strip()
            current_model = str(merged.get("model", "") or "").strip()

            logger.info(
                "获取模型列表",
                context={
                    "provider": provider,
                    "base_url": base_url,
                    "has_api_key": bool(api_key),
                    "is_override": bool(overrides),
                },
            )

            should_probe = bool(api_key) and (provider in ["openai", "openai_compatible"] or bool(base_url))
            if should_probe:
                try:
                    urls = self._build_models_urls(base_url)
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    with build_httpx_client(timeout=15.0) as client:
                        for url in urls:
                            response = client.get(url, headers=headers)
                            if response.status_code == 200:
                                payload = response.json()
                                models = self._parse_model_ids(payload if isinstance(payload, dict) else {})
                                if models:
                                    logger.info(f"动态获取模型成功: {len(models)} 个, url={url}")
                                    return models
                                logger.warning(f"模型接口返回成功但未发现可解析字段: url={url}")
                                continue
                            logger.warning(
                                f"动态获取模型失败: status={response.status_code}, url={url}, body={response.text[:180]}"
                            )
                except Exception as e:
                    logger.warning(f"动态获取模型异常: {str(e)}")

            fallback = self._fallback_models(provider, base_url, current_model)
            logger.info(f"返回静态模型列表: provider={provider}, count={len(fallback)}")
            return fallback
        except Exception as e:
            logger.error(f"获取模型列表异常: {str(e)}", exc_info=True)
            return self._fallback_models("openai", "", "")

# 创建全局实例
config_service = ConfigService()
