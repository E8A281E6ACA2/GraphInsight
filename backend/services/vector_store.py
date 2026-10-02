"""Milvus vector store adapter for document chunks (M3: kb-scoped)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core import get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.runtime_config import get_embedding_runtime_config, get_vector_store_runtime_config
from services.scope_contract import milvus_kb_filter, normalize_scope_id


logger = get_logger()


def require_scope_filter(filter_expr: Optional[str]) -> str:
    """空 filter 一律拒绝（契约 §3.4/§11.2：不允许无作用域的向量检索）。"""
    cleaned = str(filter_expr or "").strip()
    if not cleaned:
        raise KnowledgeScopeError(
            ErrorCode.KB_SCOPE_REQUIRED,
            message="Milvus 检索必须携带非空作用域过滤表达式",
        )
    return cleaned


@dataclass
class VectorChunk:
    chunk_id: str
    doc_id: str
    text: str
    title: str = ""
    location: str = ""
    entities: List[str] = field(default_factory=list)
    content_hash: str = ""
    embedding_model: str = ""
    kb_id: str = ""
    tenant_id: str = ""
    project_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    # §8.5：投影版本号必须是 collection 的显式 INT64 字段，不能只进 dynamic metadata。
    content_revision: Optional[int] = None


@dataclass
class VectorSearchHit:
    chunk_id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class MilvusVectorStore:
    def __init__(self) -> None:
        self._client = None
        self._client_key: Optional[tuple[str, str, str]] = None
        self._collection_ready = False
        self._revision_field: Dict[str, bool] = {}

    def config(self) -> Dict[str, Any]:
        cfg = get_vector_store_runtime_config()
        cfg["provider"] = str(cfg.get("provider") or "milvus").strip().lower()
        # 契约 §11.1 / 决策 D3：旧全局单库 collection（无 kb_id schema）不再复用，
        # 默认切换到带 kb_id/tenant_id/project_id 的新 collection。
        collection = str(cfg.get("collection") or "").strip()
        if not collection or collection == "graphinsight_chunks":
            if collection:
                logger.warning(
                    "检测到旧版全局 Milvus collection，已切换为 kb 隔离的新 collection",
                    context={"legacy": collection, "collection": "graphinsight_chunks_v2"},
                )
            collection = "graphinsight_chunks_v2"
        cfg["collection"] = collection
        cfg["metric_type"] = str(cfg.get("metric_type") or "COSINE").strip().upper()
        cfg["index_type"] = str(cfg.get("index_type") or "IVF_FLAT").strip().upper()
        cfg["search_nprobe"] = max(1, int(cfg.get("search_nprobe") or 16))
        return cfg

    def is_enabled(self) -> bool:
        cfg = self.config()
        return bool(cfg.get("enabled")) and cfg.get("provider") == "milvus"

    def health(self) -> Dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "enabled": False, "provider": "milvus"}
        try:
            client = self._get_client()
            collection = self.config()["collection"]
            collection_exists = bool(client.has_collection(collection))
            return {
                "ok": True,
                "enabled": True,
                "provider": "milvus",
                "collection": collection,
                "collection_exists": collection_exists,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "enabled": True, "provider": "milvus", "error": str(exc)}

    def ensure_collection(self, dimension: Optional[int] = None) -> None:
        if not self.is_enabled():
            return
        client = self._get_client()
        cfg = self.config()
        embedding_cfg = get_embedding_runtime_config()
        collection = cfg["collection"]
        vector_dimension = int(dimension or embedding_cfg.get("dimension") or 1536)

        if client.has_collection(collection):
            existing_dimension = self._collection_vector_dimension(client, collection)
            if existing_dimension and existing_dimension != vector_dimension:
                # 阻断修复（契约 §11.4）：向量是 projection，但 collection 里可能仍有唯一可用
                # 索引；维度/Schema 冲突绝不允许静默 drop 重建，必须人工迁移。
                raise RuntimeError(
                    f"Milvus collection {collection} 的向量维度 ({existing_dimension}) "
                    f"与当前 embedding 维度 ({vector_dimension}) 不一致。"
                    "为避免静默销毁已有向量数据，系统不会自动 drop/重建 collection。"
                    "请执行人工迁移：新建带正确维度的 collection（按 embedding generation 区分，"
                    "如 graphinsight_chunks_v2_<dim>），将 vector_store.collection 配置切换过去，"
                    "再按 kb 重建向量（reindex），确认计数后停用旧 collection。"
                )
            if not self._collection_has_kb_fields(client, collection):
                # 旧 schema 没有 kb_id 字段，无法做 KB 隔离：拒绝写入，要求换新 collection。
                raise RuntimeError(
                    f"Milvus collection {collection} 缺少 kb_id/tenant_id/project_id 字段，"
                    "无法执行知识库作用域隔离；请配置新的 collection（如 graphinsight_chunks_v2）"
                )

        if not client.has_collection(collection):
            try:
                from pymilvus import DataType
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("缺少 pymilvus 依赖，无法初始化 Milvus collection") from exc

            schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
            schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=128)
            schema.add_field(field_name="doc_id", datatype=DataType.VARCHAR, max_length=128)
            schema.add_field(field_name="kb_id", datatype=DataType.VARCHAR, max_length=100)
            schema.add_field(field_name="tenant_id", datatype=DataType.VARCHAR, max_length=100)
            schema.add_field(field_name="project_id", datatype=DataType.VARCHAR, max_length=100)
            schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
            schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=512)
            schema.add_field(field_name="location", datatype=DataType.VARCHAR, max_length=128)
            schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=80)
            schema.add_field(field_name="embedding_model", datatype=DataType.VARCHAR, max_length=128)
            schema.add_field(field_name="entities_json", datatype=DataType.VARCHAR, max_length=2048)
            schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=vector_dimension)
            client.create_collection(collection_name=collection, schema=schema)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type=cfg["index_type"],
            metric_type=cfg["metric_type"],
            params={"nlist": 128},
        )
        try:
            client.create_index(collection_name=collection, index_params=index_params)
        except Exception as exc:  # noqa: BLE001
            if "index" not in str(exc).lower():
                logger.warning("创建 Milvus 向量索引失败", context={"error": str(exc)})
        try:
            client.load_collection(collection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载 Milvus collection 失败", context={"collection": collection, "error": str(exc)})
        self._collection_ready = True

    def upsert_chunks(self, chunks: List[VectorChunk], vectors: List[List[float]]) -> int:
        if not self.is_enabled() or not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        vector_dimension = len(vectors[0]) if vectors and vectors[0] else None
        self.ensure_collection(dimension=vector_dimension)
        client = self._get_client()
        collection = self.config()["collection"]

        import json

        if any(chunk.content_revision is not None for chunk in chunks):
            if not self.has_content_revision_field(collection):
                # §8.5 冻结：没有显式字段就禁止写版本（写进去只会进 dynamic metadata，
                # 类型与查询都不可靠）。拒写而不是降级写入，投影由调用方保持未收敛状态。
                raise RuntimeError(
                    f"Milvus collection {collection} 缺少显式 content_revision 字段，"
                    "禁止把投影版本号写入 dynamic metadata；请按 §8.5/§15.4 迁移到 "
                    "graphinsight_chunks_v3 后重建向量"
                )

        rows = []
        for chunk, vector in zip(chunks, vectors):
            row = {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "text": (chunk.text or "")[:4096],
                "title": (chunk.title or "")[:512],
                "location": (chunk.location or "")[:128],
                "content_hash": (chunk.content_hash or "")[:80],
                "embedding_model": (chunk.embedding_model or "")[:128],
                "entities_json": json.dumps(chunk.entities or [], ensure_ascii=False)[:2048],
                "vector": vector,
                **(chunk.metadata or {}),
                # 作用域与版本号以 VectorChunk 显式字段为准，元数据不能覆盖
                "kb_id": chunk.kb_id,
                "tenant_id": chunk.tenant_id,
                "project_id": chunk.project_id,
            }
            if chunk.content_revision is not None:
                row["content_revision"] = int(chunk.content_revision)
            rows.append(row)
        client.upsert(collection_name=collection, data=rows)
        return len(rows)

    def has_content_revision_field(self, collection: Optional[str] = None) -> bool:
        """collection 是否有显式 `content_revision` 字段（§8.5 v3 判据）。

        结果按 collection 名缓存：schema 在运行期不会自变，重复 describe 只增加延迟。
        """
        target = str(collection or self.config().get("collection") or "").strip()
        if not target:
            return False
        cached = self._revision_field.get(target)
        if cached is not None:
            return cached
        supported = False
        try:
            client = self._get_client()
            if client.has_collection(target):
                description = client.describe_collection(target)
                fields = description.get("fields") if isinstance(description, dict) else None
                names = {
                    str(item.get("name") or "")
                    for item in (fields or [])
                    if isinstance(item, dict)
                }
                supported = "content_revision" in names
        except Exception as exc:  # noqa: BLE001 - 探测失败按“不支持”处理，宁可拒写不误写
            logger.warning("探测 Milvus content_revision 字段失败", context={"collection": target, "error": str(exc)})
            supported = False
        self._revision_field[target] = supported
        return supported

    def delete_doc(self, doc_id: str, kb_id: str) -> None:
        clean_kb = normalize_scope_id("kb_id", kb_id)
        if not clean_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="删除文档向量必须显式携带 kb_id",
            )
        if not self.is_enabled() or not doc_id:
            return
        client = self._get_client()
        collection = self.config()["collection"]
        if not client.has_collection(collection):
            return
        filter_expr = (
            f'{milvus_kb_filter([clean_kb])} '
            f'and doc_id == "{self._escape_filter_value(str(doc_id))}"'
        )
        client.delete(
            collection_name=collection,
            filter=filter_expr,
        )

    def clear(self, kb_ids: List[str]) -> None:
        """按 kb 作用域删除向量；禁止 drop collection / 全库清空（手册 §11.4）。"""
        filter_expr = milvus_kb_filter(kb_ids)
        if not self.is_enabled():
            return
        client = self._get_client()
        collection = self.config()["collection"]
        if client.has_collection(collection):
            client.delete(
                collection_name=collection,
                filter=filter_expr,
            )

    def search(self, vector: List[float], limit: int, filter_expr: str = "") -> List[VectorSearchHit]:
        if not self.is_enabled() or not vector:
            return []
        require_scope_filter(filter_expr)
        # 用本次查询向量的实际维度做一致性校验：配置维度可能滞后于 embedding
        # 生成切换（M4-R1），以真实请求维度为准，避免误报也避免漏报。
        self.ensure_collection(dimension=len(vector) or None)
        cfg = self.config()
        result = self._get_client().search(
            collection_name=cfg["collection"],
            data=[vector],
            anns_field="vector",
            limit=max(1, int(limit or 10)),
            filter=require_scope_filter(filter_expr),
            output_fields=[
                "chunk_id",
                "doc_id",
                "kb_id",
                "text",
                "title",
                "location",
                "content_hash",
                "embedding_model",
                "entities_json",
            ],
            search_params={
                "metric_type": cfg["metric_type"],
                "params": {"nprobe": cfg["search_nprobe"]},
            },
        )
        hits: List[VectorSearchHit] = []
        first = result[0] if result else []
        for item in first:
            if isinstance(item, dict):
                entity = item.get("entity") or {}
                raw_id = item.get("id")
                raw_score = item.get("score", item.get("distance", 0.0))
            else:
                entity = getattr(item, "entity", None) or {}
                raw_id = getattr(item, "id", "")
                raw_score = getattr(item, "score", None)
                if raw_score is None:
                    raw_score = getattr(item, "distance", 0.0)
            if not isinstance(entity, dict):
                try:
                    entity = dict(entity)
                except Exception:
                    entity = {}
            chunk_id = str(entity.get("chunk_id") or raw_id or "")
            if not chunk_id:
                continue
            try:
                normalized_score = float(raw_score or 0.0)
            except Exception:
                normalized_score = 0.0
            hits.append(VectorSearchHit(chunk_id=chunk_id, score=normalized_score, metadata=entity))
        return hits

    def _get_client(self):
        cfg = self.config()
        key = (str(cfg.get("uri") or ""), str(cfg.get("token") or ""), str(cfg.get("db_name") or "default"))
        if self._client is not None and self._client_key == key:
            return self._client
        try:
            from pymilvus import MilvusClient
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("缺少 pymilvus 依赖，请安装 backend/requirements.txt") from exc
        kwargs: Dict[str, Any] = {"uri": key[0], "db_name": key[2]}
        if key[1]:
            kwargs["token"] = key[1]
        self._client = MilvusClient(**kwargs)
        self._client_key = key
        self._collection_ready = False
        return self._client

    @staticmethod
    def _collection_vector_dimension(client, collection: str) -> Optional[int]:
        try:
            description = client.describe_collection(collection)
        except Exception:
            return None
        fields = description.get("fields") if isinstance(description, dict) else None
        if not isinstance(fields, list):
            return None
        for field in fields:
            if not isinstance(field, dict) or field.get("name") != "vector":
                continue
            params = field.get("params") or field.get("type_params") or {}
            try:
                return int(params.get("dim") or params.get("dimension") or 0) or None
            except Exception:
                return None
        return None

    @staticmethod
    def _collection_has_kb_fields(client, collection: str) -> bool:
        try:
            description = client.describe_collection(collection)
        except Exception:
            return False
        fields = description.get("fields") if isinstance(description, dict) else None
        if not isinstance(fields, list):
            return False
        names = {
            field.get("name")
            for field in fields
            if isinstance(field, dict)
        }
        return "kb_id" in names

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')


vector_store = MilvusVectorStore()


__all__ = ["MilvusVectorStore", "VectorChunk", "VectorSearchHit", "vector_store"]
