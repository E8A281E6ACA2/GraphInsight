"""Runtime config helpers for Python capability and worker layers."""
from __future__ import annotations

import os
from typing import Any, Dict

from config import get_settings


settings = get_settings()


def _load_category(category: str) -> Dict[str, str]:
    try:
        from admin.database import SessionLocal
        from admin.crud.config import config_crud

        db = SessionLocal()
        try:
            rows = config_crud.get_by_category(db, category)
            result: Dict[str, str] = {}
            for row in rows:
                key = str(getattr(row, "key", "") or "").strip()
                if key:
                    result[key] = str(getattr(row, "value", "") or "")
            return result
        finally:
            db.close()
    except Exception:
        return {}


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def get_ai_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("ai_service")
    api_key = _first_non_empty(loaded.get("api_key"), settings.llm_api_key, settings.openai_api_key)
    return {
        "provider": _first_non_empty(loaded.get("provider"), os.getenv("AI_SERVICE_PROVIDER"), "openai"),
        "enabled": _to_bool(loaded.get("enabled"), True),
        "base_url": _first_non_empty(loaded.get("base_url"), settings.llm_base_url, os.getenv("OPENAI_BASE_URL")),
        "api_key": api_key,
        "model": _first_non_empty(loaded.get("model"), settings.llm_qa_model, settings.llm_model, settings.openai_model),
        "temperature": _to_float(loaded.get("temperature"), settings.openai_temperature),
        "max_tokens": _to_int(loaded.get("max_tokens"), settings.openai_max_tokens),
        "docqa_reasoning_profile": loaded.get("docqa_reasoning_profile", "balanced"),
        "deep_research_reasoning_profile": loaded.get("deep_research_reasoning_profile", "deep"),
        "model_probe_reasoning_profile": loaded.get("model_probe_reasoning_profile", "fast"),
        "graph_extract_reasoning_profile": loaded.get("graph_extract_reasoning_profile", "fast"),
        "graph_extract_complex_reasoning_profile": loaded.get("graph_extract_complex_reasoning_profile", "balanced"),
    }


def get_retrieval_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("retrieval")
    ai_config = get_ai_runtime_config()
    return {
        "mode": str(loaded.get("mode", settings.docqa_retrieval_mode) or "keyword").strip().lower(),
        "rrf_k": _to_int(loaded.get("rrf_k"), settings.docqa_retrieval_rrf_k),
        "candidate_multiplier": _to_int(
            loaded.get("candidate_multiplier"),
            settings.docqa_retrieval_candidate_multiplier,
        ),
        "graph_enabled": _to_bool(loaded.get("graph_enabled"), settings.docqa_retrieval_graph_enabled),
        "rerank_enabled": _to_bool(loaded.get("rerank_enabled"), False),
        "rerank_model": _first_non_empty(loaded.get("rerank_model"), os.getenv("DOCQA_RERANK_MODEL")),
        "rerank_base_url": _first_non_empty(
            loaded.get("rerank_base_url"),
            os.getenv("DOCQA_RERANK_BASE_URL"),
            ai_config.get("base_url"),
            settings.llm_base_url,
        ),
        "rerank_api_key": _first_non_empty(
            loaded.get("rerank_api_key"),
            os.getenv("DOCQA_RERANK_API_KEY"),
            ai_config.get("api_key"),
            settings.llm_api_key,
        ),
        "rerank_endpoint_path": _first_non_empty(
            loaded.get("rerank_endpoint_path"),
            os.getenv("DOCQA_RERANK_ENDPOINT_PATH"),
            "/rerank",
        ),
        "rerank_top_n": _to_int(loaded.get("rerank_top_n"), _to_int(os.getenv("DOCQA_RERANK_TOP_N"), 20)),
        "rerank_timeout_seconds": _to_float(
            loaded.get("rerank_timeout_seconds"),
            _to_float(os.getenv("DOCQA_RERANK_TIMEOUT_SECONDS"), 15.0),
        ),
    }


def get_embedding_runtime_config() -> Dict[str, Any]:
    ai_config = get_ai_runtime_config()
    loaded = _load_category("embedding")
    # 嵌入模型可以和问答模型不是同一家供应商，所以优先级为：
    # 配置中心 DB > 嵌入专属 env（EMBEDDING_PROVIDER/BASE_URL/API_KEY）> 复用 ai_service/LLM。
    return {
        "enabled": _to_bool(loaded.get("enabled"), settings.embedding_enabled),
        "provider": _first_non_empty(
            loaded.get("provider"),
            os.getenv("EMBEDDING_PROVIDER"),
            ai_config.get("provider"),
            "openai",
        ),
        "base_url": _first_non_empty(
            loaded.get("base_url"),
            os.getenv("EMBEDDING_BASE_URL"),
            ai_config.get("base_url"),
            settings.llm_base_url,
        ),
        "api_key": _first_non_empty(
            loaded.get("api_key"),
            os.getenv("EMBEDDING_API_KEY"),
            ai_config.get("api_key"),
            settings.llm_api_key,
        ),
        "model": _first_non_empty(loaded.get("model"), settings.embedding_model),
        "dimension": _to_int(loaded.get("dimension"), settings.embedding_dimension),
        "batch_size": _to_int(loaded.get("batch_size"), settings.embedding_batch_size),
    }


def get_vector_store_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("vector_store")
    return {
        "enabled": _to_bool(loaded.get("enabled"), settings.vector_store_enabled),
        "provider": str(loaded.get("provider", settings.vector_store_provider) or "milvus").strip().lower(),
        "uri": _first_non_empty(loaded.get("uri"), settings.milvus_uri),
        "token": _first_non_empty(loaded.get("token"), settings.milvus_token),
        "db_name": _first_non_empty(loaded.get("db_name"), settings.milvus_db_name),
        "collection": _first_non_empty(loaded.get("collection"), settings.milvus_collection),
        "metric_type": _first_non_empty(loaded.get("metric_type"), settings.milvus_metric_type),
        "index_type": _first_non_empty(loaded.get("index_type"), settings.milvus_index_type),
        "search_nprobe": _to_int(loaded.get("search_nprobe"), settings.milvus_search_nprobe),
    }


def get_projection_capabilities() -> Dict[str, bool]:
    """投影重建能力判定（§15.3：graph = LLM_ENABLED；vector = embedding 已配置且 vector_store 开启）。

    backfill（M5-A 前置门）与 reindex worker（M5-B0 消费端）必须共用这一份判定：
    两边口径不一致时，门会按"能力已配置"要求投影收敛，而 worker 按另一套口径写
    `skipped`（或反之），造成前置门永不 CLOSED 或假性 CLOSED_DEGRADED。
    """
    embedding = get_embedding_runtime_config()
    store = get_vector_store_runtime_config()
    return {
        "graph": bool(getattr(settings, "llm_enabled", False)),
        "vector": bool(embedding.get("enabled"))
        and bool(str(embedding.get("api_key") or "").strip())
        and bool(store.get("enabled")),
    }


def get_document_parser_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("document_parser")
    return {
        "provider": str(loaded.get("provider", settings.document_parser_provider) or "native").strip().lower(),
        "fallback_provider": str(
            loaded.get("fallback_provider", settings.document_parser_fallback_provider) or "native"
        ).strip().lower(),
        "base_url": _first_non_empty(loaded.get("base_url"), settings.mineru_base_url),
        "endpoint_path": _first_non_empty(loaded.get("endpoint_path"), settings.mineru_endpoint_path),
        "file_field": _first_non_empty(loaded.get("file_field"), settings.mineru_file_field),
        "parse_mode": _first_non_empty(loaded.get("parse_mode"), settings.mineru_parse_mode),
        "output_format": _first_non_empty(loaded.get("output_format"), settings.mineru_output_format),
        "timeout_seconds": _to_float(loaded.get("timeout_seconds"), settings.mineru_timeout_seconds),
        "parser_version": _first_non_empty(loaded.get("parser_version"), settings.mineru_parser_version),
    }


def get_ai_cost_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("ai_cost")
    return {
        "model_pricing_json": loaded.get("model_pricing_json", ""),
        "currency": loaded.get("currency", ""),
    }


def get_nl2cypher_runtime_config() -> Dict[str, Any]:
    loaded = _load_category("nl2cypher")
    return {
        "enabled": _to_bool(loaded.get("enabled"), settings.nl2cypher_enabled),
        "cache_size": _to_int(loaded.get("cache_size"), 100),
        "max_limit": _to_int(loaded.get("max_limit"), settings.nl2cypher_max_limit),
    }


def get_graph_build_runtime_defaults(*, complex_extraction: bool) -> str:
    ai_config = get_ai_runtime_config()
    key = "graph_extract_complex_reasoning_profile" if complex_extraction else "graph_extract_reasoning_profile"
    fallback = "balanced" if complex_extraction else "fast"
    value = str(ai_config.get(key, fallback) or "").strip().lower()
    if value in {"fast", "balanced", "deep"}:
        return value
    return fallback


def get_neo4j_runtime_config() -> Dict[str, str]:
    loaded = _load_category("neo4j")
    user = _first_non_empty(loaded.get("user"), loaded.get("username"), settings.neo4j_user)
    return {
        "uri": _first_non_empty(loaded.get("uri"), settings.neo4j_uri),
        "user": user,
        "password": _first_non_empty(loaded.get("password"), settings.neo4j_password),
        "database": _first_non_empty(loaded.get("database"), getattr(settings, "neo4j_database", "neo4j")),
    }
