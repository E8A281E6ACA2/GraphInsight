#!/usr/bin/env python3
"""
初始化默认配置
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.database import SessionLocal
from admin.models import AdminConfig
from sqlalchemy import text

def init_default_configs():
    """初始化默认配置"""
    db = SessionLocal()
    
    try:
        print("开始初始化默认配置...")
        
        # 默认配置列表
        default_configs = [
            # Neo4j 配置
            {
                "category": "neo4j",
                "key": "uri",
                "value": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                "description": "Neo4j 连接地址",
                "is_sensitive": False
            },
            {
                "category": "neo4j",
                "key": "user",
                "value": os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j")),
                "description": "Neo4j 用户名",
                "is_sensitive": False
            },
            {
                "category": "neo4j",
                "key": "password",
                "value": os.getenv("NEO4J_PASSWORD", "password"),
                "description": "Neo4j 密码",
                "is_sensitive": True
            },
            {
                "category": "neo4j",
                "key": "database",
                "value": os.getenv("NEO4J_DATABASE", "neo4j"),
                "description": "Neo4j 数据库名称",
                "is_sensitive": False
            },
            
            # AI 服务配置
            {
                "category": "ai_service",
                "key": "provider",
                "value": os.getenv("AI_PROVIDER", "openai"),
                "description": "AI 服务提供商 (openai/claude)",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "enabled",
                "value": os.getenv("AI_ENABLED", "true"),
                "description": "是否启用 AI 服务",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "api_key",
                "value": os.getenv("OPENAI_API_KEY", ""),
                "description": "AI 服务 API Key",
                "is_sensitive": True
            },
            {
                "category": "ai_service",
                "key": "base_url",
                "value": os.getenv("OPENAI_BASE_URL", ""),
                "description": "AI 服务 API 地址(可选)",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "model",
                "value": os.getenv("OPENAI_MODEL", "gpt-3.5-turbo"),
                "description": "AI 模型名称",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "docqa_reasoning_profile",
                "value": os.getenv("AI_SERVICE_DOCQA_REASONING_PROFILE", "balanced"),
                "description": "文档问答默认推理档位",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "deep_research_reasoning_profile",
                "value": os.getenv("AI_SERVICE_DEEP_RESEARCH_REASONING_PROFILE", "deep"),
                "description": "深度调研默认推理档位",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "model_probe_reasoning_profile",
                "value": os.getenv("AI_SERVICE_MODEL_PROBE_REASONING_PROFILE", "fast"),
                "description": "模型连通性测试默认推理档位",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "graph_extract_reasoning_profile",
                "value": os.getenv("AI_SERVICE_GRAPH_EXTRACT_REASONING_PROFILE", "fast"),
                "description": "图谱抽取默认推理档位",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "graph_extract_complex_reasoning_profile",
                "value": os.getenv("AI_SERVICE_GRAPH_EXTRACT_COMPLEX_REASONING_PROFILE", "balanced"),
                "description": "复杂图谱抽取默认推理档位",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "max_tokens",
                "value": os.getenv("OPENAI_MAX_TOKENS", "2000"),
                "description": "最大 Token 数",
                "is_sensitive": False
            },
            {
                "category": "ai_service",
                "key": "temperature",
                "value": os.getenv("OPENAI_TEMPERATURE", "0.7"),
                "description": "温度参数",
                "is_sensitive": False
            },

            # 检索配置
            {
                "category": "retrieval",
                "key": "mode",
                "value": os.getenv("DOCQA_RETRIEVAL_MODE", "keyword"),
                "description": "文档问答检索模式(keyword/vector/hybrid/graph_hybrid)",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rrf_k",
                "value": os.getenv("DOCQA_RETRIEVAL_RRF_K", "60"),
                "description": "RRF 融合参数",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "candidate_multiplier",
                "value": os.getenv("DOCQA_RETRIEVAL_CANDIDATE_MULTIPLIER", "6"),
                "description": "候选召回倍数",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "graph_enabled",
                "value": os.getenv("DOCQA_RETRIEVAL_GRAPH_ENABLED", "true"),
                "description": "是否启用图谱扩展召回",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_enabled",
                "value": os.getenv("DOCQA_RERANK_ENABLED", "false"),
                "description": "是否启用重排器",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_model",
                "value": os.getenv("DOCQA_RERANK_MODEL", ""),
                "description": "Reranker 模型名称，留空则不执行二阶段重排",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_base_url",
                "value": os.getenv("DOCQA_RERANK_BASE_URL", ""),
                "description": "Reranker API 地址，留空时复用 AI 服务地址",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_endpoint_path",
                "value": os.getenv("DOCQA_RERANK_ENDPOINT_PATH", "/rerank"),
                "description": "Reranker 接口路径",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_top_n",
                "value": os.getenv("DOCQA_RERANK_TOP_N", "20"),
                "description": "进入 Reranker 的融合候选上限",
                "is_sensitive": False
            },
            {
                "category": "retrieval",
                "key": "rerank_timeout_seconds",
                "value": os.getenv("DOCQA_RERANK_TIMEOUT_SECONDS", "15"),
                "description": "Reranker 请求超时时间",
                "is_sensitive": False
            },

            # Embedding 配置
            {
                "category": "embedding",
                "key": "enabled",
                "value": os.getenv("EMBEDDING_ENABLED", "true"),
                "description": "是否启用 Embedding",
                "is_sensitive": False
            },
            {
                "category": "embedding",
                "key": "provider",
                "value": os.getenv("EMBEDDING_PROVIDER", os.getenv("AI_SERVICE_PROVIDER", os.getenv("OPENAI_PROVIDER", "openai"))),
                "description": "Embedding 服务提供商",
                "is_sensitive": False
            },
            {
                "category": "embedding",
                "key": "base_url",
                "value": os.getenv("EMBEDDING_BASE_URL", ""),
                "description": "Embedding OpenAI-compatible API 地址；留空时复用 AI 服务地址",
                "is_sensitive": False
            },
            {
                "category": "embedding",
                "key": "api_key",
                "value": os.getenv("EMBEDDING_API_KEY", os.getenv("AI_SERVICE_API_KEY", os.getenv("OPENAI_API_KEY", ""))),
                "description": "Embedding API Key",
                "is_sensitive": True
            },
            {
                "category": "embedding",
                "key": "model",
                "value": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                "description": "Embedding 模型",
                "is_sensitive": False
            },
            {
                "category": "embedding",
                "key": "dimension",
                "value": os.getenv("EMBEDDING_DIMENSION", "1536"),
                "description": "Embedding 维度",
                "is_sensitive": False
            },
            {
                "category": "embedding",
                "key": "batch_size",
                "value": os.getenv("EMBEDDING_BATCH_SIZE", "32"),
                "description": "Embedding 批大小",
                "is_sensitive": False
            },

            # 向量库配置
            {
                "category": "vector_store",
                "key": "enabled",
                "value": os.getenv("VECTOR_STORE_ENABLED", "false"),
                "description": "是否启用向量库",
                "is_sensitive": False
            },
            {
                "category": "vector_store",
                "key": "provider",
                "value": os.getenv("VECTOR_STORE_PROVIDER", "milvus"),
                "description": "向量库提供商",
                "is_sensitive": False
            },
            {
                "category": "vector_store",
                "key": "uri",
                "value": os.getenv("MILVUS_URI", "http://127.0.0.1:19530"),
                "description": "Milvus 连接地址",
                "is_sensitive": False
            },
            {
                "category": "vector_store",
                "key": "db_name",
                "value": os.getenv("MILVUS_DB_NAME", "default"),
                "description": "Milvus 数据库名称",
                "is_sensitive": False
            },
            {
                "category": "vector_store",
                "key": "collection",
                "value": os.getenv("MILVUS_COLLECTION", "graphinsight_chunks"),
                "description": "Milvus Collection 名称",
                "is_sensitive": False
            },
            {
                "category": "vector_store",
                "key": "token",
                "value": os.getenv("MILVUS_TOKEN", ""),
                "description": "Milvus Token",
                "is_sensitive": True
            },

            # 文档解析配置
            {
                "category": "document_parser",
                "key": "provider",
                "value": os.getenv("DOCUMENT_PARSER_PROVIDER", "native"),
                "description": "文档解析器提供商(native/mineru)",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "fallback_provider",
                "value": os.getenv("DOCUMENT_PARSER_FALLBACK_PROVIDER", "native"),
                "description": "解析失败时的回退解析器(native/none)",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "base_url",
                "value": os.getenv("MINERU_BASE_URL", ""),
                "description": "MinerU API 地址",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "endpoint_path",
                "value": os.getenv("MINERU_ENDPOINT_PATH", "/file_parse"),
                "description": "MinerU 解析接口路径",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "file_field",
                "value": os.getenv("MINERU_FILE_FIELD", "files"),
                "description": "MinerU multipart 文件字段名",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "parse_mode",
                "value": os.getenv("MINERU_PARSE_MODE", "auto"),
                "description": "MinerU parse_method(auto/ocr/txt)",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "output_format",
                "value": os.getenv("MINERU_OUTPUT_FORMAT", "markdown,json"),
                "description": "MinerU 输出格式",
                "is_sensitive": False
            },
            {
                "category": "document_parser",
                "key": "timeout_seconds",
                "value": os.getenv("MINERU_TIMEOUT_SECONDS", "300"),
                "description": "MinerU 请求超时时间",
                "is_sensitive": False
            },

            # NL2Cypher 配置
            {
                "category": "nl2cypher",
                "key": "enabled",
                "value": "true",
                "description": "是否启用 NL2Cypher",
                "is_sensitive": False
            },
            {
                "category": "nl2cypher",
                "key": "cache_size",
                "value": "100",
                "description": "缓存大小",
                "is_sensitive": False
            },
            {
                "category": "nl2cypher",
                "key": "max_limit",
                "value": "1000",
                "description": "最大查询限制",
                "is_sensitive": False
            },
        ]
        
        count = 0
        for config_data in default_configs:
            # 检查是否已存在
            existing = db.query(AdminConfig).filter(
                AdminConfig.category == config_data["category"],
                AdminConfig.key == config_data["key"]
            ).first()
            
            if not existing:
                config = AdminConfig(**config_data)
                db.add(config)
                count += 1
                print(f"  添加配置: {config_data['category']}.{config_data['key']}")
            else:
                print(f"  跳过已存在: {config_data['category']}.{config_data['key']}")
        
        db.commit()
        print(f"\n初始化完成! 添加了 {count} 个配置项")
        
        # 显示当前配置
        print("\n当前配置:")
        configs = db.query(AdminConfig).all()
        for config in configs:
            value = config.value if not config.is_sensitive else "***"
            print(f"  {config.category}.{config.key} = {value}")
        
    except Exception as e:
        print(f"初始化失败: {e}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    init_default_configs()
