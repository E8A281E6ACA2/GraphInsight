"""Milvus vector store adapter for document chunks (M3: kb-scoped)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core import get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.runtime_config import get_embedding_runtime_config, get_vector_store_runtime_config
from services.scope_contract import milvus_kb_filter, normalize_scope_id


logger = get_logger()


class VectorStoreSchemaError(RuntimeError):
    """Milvus collection schema cannot satisfy the projection contract."""


class VectorStoreUpsertError(RuntimeError):
    """Milvus did not acknowledge the expected number of upserts."""


class DualWriteShadowError(VectorStoreUpsertError):
    """§16.1 S1 双写：主库（读源）已写成功，影子（v3）侧未收敛。

    主库是读源，其写入不因影子失败而回滚；但影子缺口必须**可定位**且让调用方判"投影未收敛"
    （与 §8.5 拒写语义一致），从而重试收敛（Milvus 按主键幂等 upsert），绝不静默吸收（§6）。
    """

    def __init__(self, message: str, *, primary_collection: str, shadow_collection: str) -> None:
        super().__init__(message)
        self.primary_collection = primary_collection
        self.shadow_collection = shadow_collection


class DualWriteConfigError(RuntimeError):
    """§16.1 S1 双写：`dual_write=true` 已请求但配置非法，在任何 mutation（upsert/delete/clear）前 fail-closed。

    触发条件（requested=true 且 active=false 的所有原因）：vector_store 未启用、主 collection 为空、
    shadow_collection 未配置、shadow 与主 collection 同名。以前两种"退化成单写"曾导致 §6 红线
    "影子失败绝不静默吸收"被绕过——请求了双写却配置非法 = 用户以为 v3 会同步、实际只会写 v2，
    这类静默退化必须在入口拒绝，而不是让主库写完再返回。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _is_int64_field(field: Dict[str, Any]) -> bool:
    raw_type = field.get("data_type", field.get("type"))
    if hasattr(raw_type, "name"):
        raw_type = raw_type.name
    if isinstance(raw_type, int):
        return raw_type == 5  # pymilvus DataType.INT64
    normalized = str(raw_type or "").strip().upper()
    return normalized in {"INT64", "5"}


def content_revision_field_is_int64(client: Any, collection: str) -> bool:
    """Shared §8.5 check: field must be explicit and INT64, never dynamic metadata."""
    if not client.has_collection(collection):
        return False
    try:
        description = client.describe_collection(collection)
    except Exception:
        return False
    fields = description.get("fields") if isinstance(description, dict) else None
    return any(
        isinstance(field, dict)
        and str(field.get("name") or "") == "content_revision"
        and _is_int64_field(field)
        for field in (fields or [])
    )


def require_upsert_count(result: Any, expected: int) -> int:
    """Extract Milvus' acknowledged mutation count and reject partial/unknown writes."""
    count = None
    if isinstance(result, dict):
        for key in ("upsert_count", "insert_count", "mutation_count"):
            if key in result:
                count = result[key]
                break
        status = result.get("status")
        if isinstance(status, dict) and status.get("code") not in (None, 0, "0"):
            raise VectorStoreUpsertError(f"Milvus upsert status={status}")
    else:
        for key in ("upsert_count", "insert_count", "mutation_count"):
            value = getattr(result, key, None)
            if value is not None:
                count = value
                break
        status = getattr(result, "status", None)
        code = getattr(status, "code", None) if status is not None else None
        if code not in (None, 0, "0"):
            raise VectorStoreUpsertError(f"Milvus upsert status code={code}")
    try:
        acknowledged = int(count)
    except (TypeError, ValueError) as exc:
        raise VectorStoreUpsertError(
            f"Milvus upsert returned no acknowledged count (expected={expected})"
        ) from exc
    if acknowledged != int(expected):
        raise VectorStoreUpsertError(
            f"Milvus upsert count mismatch: expected={expected} actual={acknowledged}"
        )
    return acknowledged


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

    def resolve_dual_write(self) -> Dict[str, Any]:
        """§16.1 S1 双写生效判据（单次配置快照，不重复读 config）。

        active 需同时满足：配置请求开启、vector_store 已启用、shadow_collection 非空、
        且 shadow ≠ 主 collection（同名会让"双写"退化成对同一集合重复写，掩盖真实缺口）。
        返回里始终带 enabled/primary/shadow 名与不生效原因，供健康与迁移门禁可读，绝不静默。
        调用方应使用本方法一次读，不再自行 is_enabled()，避免配置漂移（Wave 1 收口）。
        """
        cfg = self.config()
        enabled = bool(cfg.get("enabled")) and cfg.get("provider") == "milvus"
        primary = str(cfg.get("collection") or "").strip()
        shadow = str(cfg.get("shadow_collection") or "").strip()
        requested = bool(cfg.get("dual_write"))
        if not requested:
            active, reason = False, "dual_write 未开启"
        elif not enabled:
            active, reason = False, "vector_store 未启用"
        elif not primary:
            active, reason = False, "主 collection 为空"
        elif not shadow:
            active, reason = False, "shadow_collection 未配置"
        elif shadow == primary:
            active, reason = False, "shadow_collection 与主 collection 同名，拒绝双写"
        else:
            active, reason = True, ""
        return {
            "active": active,
            "requested": requested,
            "enabled": enabled,
            "primary": primary,
            "shadow": shadow,
            "reason": reason,
        }

    @staticmethod
    def _guard_dual_write_config(dw: Dict[str, Any], operation: str) -> None:
        """store 已启用 + requested=true + active=false → 任何 mutation 前 fail-closed。

        Wave 1 收口（P1#3）：以前 upsert_chunks/delete_doc/clear 会在 shadow 为空或与主库
        同名时**静默退化成单写**，违反 §6 "影子失败绝不静默吸收"红线——运维以为 v3 在同步，
        实际只写 v2。现在只要请求了 dual_write 且配置非法就抛 DualWriteConfigError，
        调用方必须修 shadow_collection 或显式关掉 dual_write 才能继续。

        store 本身未启用（enabled=false）时保持旧的"零写"语义：不属于 dual_write 配置错，
        属于整库关停；此时 mutation 直接短路返回，不进入本 guard。
        """
        if dw.get("enabled") and dw.get("requested") and not dw.get("active"):
            raise DualWriteConfigError(
                f"dual_write 已请求但配置非法（{dw.get('reason') or '未知原因'}），"
                f"拒绝执行 {operation}；请修正 shadow_collection 配置或显式关闭 dual_write",
                reason=str(dw.get("reason") or "未知原因"),
            )

    def ensure_collection(self, dimension: Optional[int] = None, collection: Optional[str] = None) -> None:
        if not self.is_enabled():
            return
        client = self._get_client()
        cfg = self.config()
        embedding_cfg = get_embedding_runtime_config()
        collection = str(collection or cfg["collection"]).strip()
        vector_dimension = int(dimension or embedding_cfg.get("dimension") or 1536)

        if client.has_collection(collection):
            existing_dimension = self._collection_vector_dimension(client, collection)
            if existing_dimension and existing_dimension != vector_dimension:
                # 阻断修复（契约 §11.4）：向量是 projection，但 collection 里可能仍有唯一可用
                # 索引；维度/Schema 冲突绝不允许静默 drop 重建，必须人工迁移。
                raise VectorStoreSchemaError(
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
            schema.add_field(field_name="content_revision", datatype=DataType.INT64)
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

    @staticmethod
    def _build_rows(chunks: List[VectorChunk], vectors: List[List[float]]) -> List[Dict[str, Any]]:
        import json

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
        return rows

    def _upsert_to(self, collection: str, chunks: List[VectorChunk], vectors: List[List[float]], dimension: Optional[int]) -> int:
        """向单个 collection 写一批 rows：先保证 schema，再过 §8.5 显式字段门，最后要求确认数相等。"""
        self.ensure_collection(dimension=dimension, collection=collection)
        client = self._get_client()
        if any(chunk.content_revision is not None for chunk in chunks):
            if not self.has_content_revision_field(collection):
                # §8.5 冻结：没有显式字段就禁止写版本（写进去只会进 dynamic metadata，
                # 类型与查询都不可靠）。拒写而不是降级写入，投影由调用方保持未收敛状态。
                raise VectorStoreSchemaError(
                    f"Milvus collection {collection} 缺少显式 content_revision 字段，"
                    "禁止把投影版本号写入 dynamic metadata；请按 §8.5/§15.4 迁移到 "
                    "graphinsight_chunks_v3 后重建向量"
                )
        rows = self._build_rows(chunks, vectors)
        result = client.upsert(collection_name=collection, data=rows)
        return require_upsert_count(result, len(rows))

    def upsert_chunks(self, chunks: List[VectorChunk], vectors: List[List[float]]) -> int:
        dw = self.resolve_dual_write()
        self._guard_dual_write_config(dw, "upsert_chunks")
        if not dw["enabled"] or not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        vector_dimension = len(vectors[0]) if vectors and vectors[0] else None

        # 主库（读源）先写。主库失败直接抛出：读源保持未收敛、调用方重试，
        # 绝不允许出现"影子已写、主库未写"，也不触发任何影子写。
        primary_written = self._upsert_to(dw["primary"], chunks, vectors, vector_dimension)

        if not dw["active"]:
            return primary_written

        # 影子（v3）侧：主库成功不等于可以吞掉影子失败。任何异常都记 ERROR（可定位）并抛
        # DualWriteShadowError 让投影判为未收敛，调用方重放即收敛（按主键幂等）。
        try:
            shadow_written = self._upsert_to(dw["shadow"], chunks, vectors, vector_dimension)
        except DualWriteShadowError:
            raise
        except Exception as exc:  # noqa: BLE001 - 影子侧任何失败都必须上抛，绝不静默吸收
            logger.error(
                "双写影子侧失败：主库已写、影子未收敛，投影必须保持未收敛并重试",
                context={
                    "primary_collection": dw["primary"],
                    "shadow_collection": dw["shadow"],
                    "primary_count": primary_written,
                    "error": str(exc)[:200],
                },
            )
            raise DualWriteShadowError(
                f"双写影子 collection {dw['shadow']} 写入失败"
                f"（主库 {dw['primary']} 已成功 {primary_written} 条）：{exc}",
                primary_collection=dw["primary"],
                shadow_collection=dw["shadow"],
            ) from exc
        if shadow_written != primary_written:
            raise DualWriteShadowError(
                f"双写影子确认数与主库不一致：primary={primary_written} shadow={shadow_written} "
                f"(primary={dw['primary']} shadow={dw['shadow']})",
                primary_collection=dw["primary"],
                shadow_collection=dw["shadow"],
            )
        return primary_written

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
                supported = content_revision_field_is_int64(client, target)
        except Exception as exc:  # noqa: BLE001 - 探测失败按“不支持”处理，宁可拒写不误写
            logger.warning("探测 Milvus content_revision 字段失败", context={"collection": target, "error": str(exc)})
            supported = False
        self._revision_field[target] = supported
        return supported

    def _delete_from(self, collection: str, filter_expr: str) -> None:
        client = self._get_client()
        if client.has_collection(collection):
            client.delete(collection_name=collection, filter=filter_expr)

    def delete_doc(self, doc_id: str, kb_id: str) -> None:
        clean_kb = normalize_scope_id("kb_id", kb_id)
        if not clean_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="删除文档向量必须显式携带 kb_id",
            )
        dw = self.resolve_dual_write()
        self._guard_dual_write_config(dw, "delete_doc")
        if not dw["enabled"] or not doc_id:
            return
        collection = dw["primary"]
        client = self._get_client()
        if not client.has_collection(collection):
            return
        filter_expr = (
            f'{milvus_kb_filter([clean_kb])} '
            f'and doc_id == "{self._escape_filter_value(str(doc_id))}"'
        )
        self._delete_from(collection, filter_expr)
        # 影子侧同步删除：v2 删了而 v3 没删会让 §6 的 chunk_id 集合"v3 多"，对账必红。
        if dw["active"]:
            try:
                self._delete_from(dw["shadow"], filter_expr)
            except Exception as exc:  # noqa: BLE001 - 影子删除失败必须可定位并保持未收敛
                logger.error(
                    "双写影子删除失败：主库已删、影子未收敛",
                    context={
                        "primary_collection": dw["primary"],
                        "shadow_collection": dw["shadow"],
                        "doc_id": doc_id,
                        "kb_id": clean_kb,
                        "error": str(exc)[:200],
                    },
                )
                raise DualWriteShadowError(
                    f"双写影子 collection {dw['shadow']} 删除失败（主库 {dw['primary']} 已删）：{exc}",
                    primary_collection=dw["primary"],
                    shadow_collection=dw["shadow"],
                ) from exc

    def clear(self, kb_ids: List[str]) -> None:
        """按 kb 作用域删除向量；禁止 drop collection / 全库清空（手册 §11.4）。"""
        filter_expr = milvus_kb_filter(kb_ids)
        dw = self.resolve_dual_write()
        self._guard_dual_write_config(dw, "clear")
        if not dw["enabled"]:
            return
        self._delete_from(dw["primary"], filter_expr)
        if dw["active"]:
            try:
                self._delete_from(dw["shadow"], filter_expr)
            except Exception as exc:  # noqa: BLE001 - 影子删除失败必须可定位并保持未收敛
                logger.error(
                    "双写影子 clear 失败：主库已删、影子未收敛",
                    context={
                        "primary_collection": dw["primary"],
                        "shadow_collection": dw["shadow"],
                        "kb_ids": list(kb_ids),
                        "error": str(exc)[:200],
                    },
                )
                raise DualWriteShadowError(
                    f"双写影子 collection {dw['shadow']} clear 删除失败（主库 {dw['primary']} 已删）：{exc}",
                    primary_collection=dw["primary"],
                    shadow_collection=dw["shadow"],
                ) from exc

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


__all__ = [
    "MilvusVectorStore",
    "VectorChunk",
    "VectorSearchHit",
    "DualWriteShadowError",
    "vector_store",
]
