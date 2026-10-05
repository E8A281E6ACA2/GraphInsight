"""
配置管理
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from functools import lru_cache

# 先加载用户 backend/.env 中的密钥与模型配置，再用开发运行时 env 覆盖端口、
# 存储目录和数据库连接等本地启动参数。这样 Go 网关与 Python worker 使用
# 同一份 AI/Embedding 凭据，同时不需要把密钥复制进 logs/dev/backend.env。
_env_override = os.getenv("GRAPHINSIGHT_BACKEND_ENV_FILE", "").strip()
_default_env_path = Path(__file__).resolve().parent / ".env"
if _default_env_path.exists():
    load_dotenv(dotenv_path=_default_env_path, override=True)
else:
    load_dotenv(override=True)
if _env_override:
    _env_path = Path(_env_override).expanduser().resolve()
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)


_BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(value: str, base_dir: Path) -> str:
    if not value:
        return str(base_dir)
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


class Settings:
    """应用配置"""
    
    def __init__(self):
        # Neo4j 配置
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", os.getenv("NEO4J_USERNAME", "neo4j"))
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.neo4j_connection_timeout_seconds = float(os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS", "5"))
        self.neo4j_connection_acquisition_timeout_seconds = float(
            os.getenv("NEO4J_CONNECTION_ACQUISITION_TIMEOUT_SECONDS", "5")
        )
        # env(默认): 仅使用 .env；admin: 仅使用配置中心；auto: 优先配置中心，缺失时回退 .env
        self.neo4j_config_source = os.getenv("NEO4J_CONFIG_SOURCE", "env").strip().lower()
        
        # API 配置
        self.api_host = os.getenv("API_HOST", "0.0.0.0")
        self.api_port = int(os.getenv("API_PORT", "8000"))
        
        # 媒体存储路径（统一解析为绝对路径）
        self.media_storage_path = _resolve_path(os.getenv("MEDIA_STORAGE_PATH", "./media"), _BASE_DIR)

        # 文档存储路径（用于建图与问答，统一解析为绝对路径）
        self.document_storage_path = _resolve_path(os.getenv("DOCUMENT_STORAGE_PATH", "./documents"), _BASE_DIR)
        # 文档解析中间产物路径（Markdown/raw JSON/blocks/chunks），用于排障与验收。
        self.parsed_document_storage_path = _resolve_path(
            os.getenv("PARSED_DOCUMENT_STORAGE_PATH", "./parsed_documents"),
            _BASE_DIR,
        )

        # 文档解析配置。MinerU 作为独立 HTTP 侧车接入，默认继续使用内置解析器。
        self.document_parser_provider = os.getenv("DOCUMENT_PARSER_PROVIDER", "native").strip().lower()
        self.document_parser_fallback_provider = os.getenv("DOCUMENT_PARSER_FALLBACK_PROVIDER", "native").strip().lower()
        self.mineru_base_url = os.getenv("MINERU_BASE_URL", "").strip()
        self.mineru_endpoint_path = os.getenv("MINERU_ENDPOINT_PATH", "/file_parse").strip() or "/file_parse"
        self.mineru_file_field = os.getenv("MINERU_FILE_FIELD", "files").strip() or "files"
        self.mineru_parse_mode = os.getenv("MINERU_PARSE_MODE", "auto").strip() or "auto"
        self.mineru_output_format = os.getenv("MINERU_OUTPUT_FORMAT", "markdown,json").strip() or "markdown,json"
        self.mineru_timeout_seconds = float(os.getenv("MINERU_TIMEOUT_SECONDS", "300"))
        self.mineru_parser_version = os.getenv("MINERU_PARSER_VERSION", "").strip()
        
        # OpenAI 配置
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
        self.openai_max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "500"))
        self.openai_temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))

        # LLM 实体抽取配置（OpenAI 兼容）
        self.llm_enabled = os.getenv("LLM_ENABLED", "true").lower() == "true"
        self.llm_api_key = os.getenv("LLM_API_KEY") or self.openai_api_key
        self.llm_base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")
        self.llm_model = os.getenv("LLM_MODEL") or self.openai_model
        self.llm_max_entities = int(os.getenv("LLM_MAX_ENTITIES", "12"))
        self.llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.1"))

        # LLM 关系抽取配置（OpenAI 兼容）
        self.llm_relation_enabled = os.getenv("LLM_RELATION_ENABLED", "true").lower() == "true"
        self.llm_relation_model = os.getenv("LLM_RELATION_MODEL") or self.llm_model
        self.llm_relation_temperature = float(os.getenv("LLM_RELATION_TEMPERATURE", "0.1"))
        self.llm_max_relations = int(os.getenv("LLM_MAX_RELATIONS", "8"))
        self.llm_relation_dynamic_type = os.getenv("LLM_RELATION_DYNAMIC_TYPE", "true").lower() == "true"
        self.llm_relation_text_budget = int(os.getenv("LLM_RELATION_TEXT_BUDGET", "1000"))
        self.llm_relation_max_prompt_entities = int(os.getenv("LLM_RELATION_MAX_PROMPT_ENTITIES", "18"))
        self.llm_graph_extract_max_llm_chunks = int(os.getenv("LLM_GRAPH_EXTRACT_MAX_LLM_CHUNKS", "2"))
        self.llm_graph_extract_timeout_seconds = float(os.getenv("LLM_GRAPH_EXTRACT_TIMEOUT_SECONDS", "12"))

        # LLM 问答配置（默认复用 LLM_MODEL）
        self.llm_qa_model = os.getenv("LLM_QA_MODEL") or self.llm_model
        self.llm_qa_temperature = float(os.getenv("LLM_QA_TEMPERATURE", "0.2"))
        self.llm_qa_max_tokens = int(os.getenv("LLM_QA_MAX_TOKENS", "600"))
        self.llm_qa_max_context = int(os.getenv("LLM_QA_MAX_CONTEXT", "4"))

        # 文档问答检索配置
        # keyword: Neo4j 全文检索；vector: Milvus 向量检索；
        # hybrid: 全文 + 向量；graph_hybrid: 全文 + 向量 + 图谱扩展。
        self.docqa_retrieval_mode = os.getenv("DOCQA_RETRIEVAL_MODE", "keyword").strip().lower()
        self.docqa_retrieval_rrf_k = int(os.getenv("DOCQA_RETRIEVAL_RRF_K", "60"))
        self.docqa_retrieval_candidate_multiplier = int(os.getenv("DOCQA_RETRIEVAL_CANDIDATE_MULTIPLIER", "6"))
        self.docqa_retrieval_graph_enabled = os.getenv("DOCQA_RETRIEVAL_GRAPH_ENABLED", "true").lower() == "true"

        # OpenAI-compatible embedding 配置，默认复用 ai_service 的 base_url/api_key。
        self.embedding_enabled = os.getenv("EMBEDDING_ENABLED", "true").lower() == "true"
        self.embedding_model = os.getenv(
            "EMBEDDING_MODEL",
            os.getenv("AI_SERVICE_EMBEDDING_MODEL", "text-embedding-3-small"),
        )
        self.embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1536"))
        self.embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "32"))

        # 向量库配置。默认不开启，避免没有 Milvus 或 embedding key 时影响现有全文问答。
        self.vector_store_enabled = os.getenv("VECTOR_STORE_ENABLED", "false").lower() == "true"
        self.vector_store_provider = os.getenv("VECTOR_STORE_PROVIDER", "milvus").strip().lower()
        self.milvus_uri = os.getenv("MILVUS_URI", "http://127.0.0.1:19530")
        self.milvus_token = os.getenv("MILVUS_TOKEN", "")
        self.milvus_db_name = os.getenv("MILVUS_DB_NAME", "default")
        self.milvus_collection = os.getenv("MILVUS_COLLECTION", "graphinsight_chunks")
        self.milvus_metric_type = os.getenv("MILVUS_METRIC_TYPE", "COSINE").strip().upper()
        self.milvus_index_type = os.getenv("MILVUS_INDEX_TYPE", "IVF_FLAT").strip().upper()
        self.milvus_search_nprobe = int(os.getenv("MILVUS_SEARCH_NPROBE", "16"))
        # M5 §16.1 S1 双写（dual_write）：一次 upsert 同时扇出主库(v2)与影子(v3)，读源仍主库。
        # 默认关闭 = S0 现网安全态；开启是需单独授权的迁移动作，且 shadow_collection 必须异于主库。
        self.milvus_dual_write = os.getenv("MILVUS_DUAL_WRITE", "false").strip().lower() == "true"
        self.milvus_shadow_collection = os.getenv("MILVUS_SHADOW_COLLECTION", "").strip()

        # HTTP client 配置
        self.http_client_trust_env = os.getenv("HTTP_CLIENT_TRUST_ENV", "false").lower() == "true"

        # Go 兼容鉴权模式。默认 unified mode 使用 go_db，不再依赖 Python authorize。
        self.rbac_authz_mode = os.getenv("RBAC_AUTHZ_MODE", "go_db").strip().lower() or "go_db"
        
        # NL2Cypher 配置
        self.nl2cypher_enabled = os.getenv("NL2CYPHER_ENABLED", "true").lower() == "true"
        self.nl2cypher_cache_size = int(os.getenv("NL2CYPHER_CACHE_SIZE", "100"))
        self.nl2cypher_max_limit = int(os.getenv("NL2CYPHER_MAX_LIMIT", "100"))


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()
