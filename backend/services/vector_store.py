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


# ---------------------------------------------------------------------------
# 写入意图（统一写入契约）：同一入口，两种意图，语义分开定义
#   ONLINE   = 在线读写路径（S1 双写：primary 先写 + shadow 扇出，读仍 primary）
#   BACKFILL = 历史数据回填（只补"版本承载侧" shadow，**不写 primary**）
# 两者的共同不变量：绝不把 revision 写进 dynamic metadata（§8.5）、绝不半写（写前判定）、
# 失败可定位并保持未收敛。扇出/守卫/写后读回只有这一份实现（禁止复制第二份）。
# ---------------------------------------------------------------------------
WRITE_INTENT_ONLINE = "online_s1"
WRITE_INTENT_BACKFILL = "backfill_versioned"

# 契约拒绝原因（可机读；调用方据此决定成果状态，而不是靠字符串匹配）
REASON_SHADOW_NOT_ACTIVE = "SHADOW_NOT_ACTIVE"
REASON_SHADOW_FIELD_MISSING = "SHADOW_FIELD_MISSING"
REASON_PRIMARY_VECTOR_MISSING = "PRIMARY_VECTOR_MISSING"
REASON_PRIMARY_UNREADABLE = "PRIMARY_UNREADABLE"
REASON_VERIFY_MISMATCH = "VERIFY_MISMATCH"
REASON_VERIFY_UNAVAILABLE = "VERIFY_UNAVAILABLE"
REASON_KB_NOT_ALLOWED = "KB_NOT_ALLOWED"
REASON_SCOPE_MISMATCH = "SCOPE_MISMATCH"
REASON_COLLECTION_NOT_WRITABLE = "COLLECTION_NOT_WRITABLE"


class VectorWriteContractError(VectorStoreSchemaError):
    """写入契约不满足：**在任何写入之前**判定并拒绝（绝不半写），原因是机读的 reason。

    与 `VectorStoreSchemaError` 同族（都是"宁可不写也不写错"），但额外携带 `reason`，
    让调用方（backfill / worker）把拒绝映射成逐 chunk 的 pending/failed，而不必解析文案。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


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
        # 双写生效判据统一由 resolve_dual_write() 承担（审计线语义：requested-but-inactive
        # 在任何 mutation 前 fail-closed）。集成轮已退役"硬编码 shadow=v3 + warning"的旧口径，
        # 此处不再产生 dual_target_collection。
        return cfg

    def is_enabled(self) -> bool:
        cfg = self.config()
        return bool(cfg.get("enabled")) and cfg.get("provider") == "milvus"

    def health(self) -> Dict[str, Any]:
        if not self.is_enabled():
            return {"ok": False, "enabled": False, "provider": "milvus"}
        try:
            client = self._get_client()
            cfg = self.config()
            collection = cfg["collection"]
            collection_exists = bool(client.has_collection(collection))
            # §16.1：双写状态对后台可见（布尔/集合名非敏感，规范 9）。
            # 统一取自 resolve_dual_write()，与 mutation 守卫同一份判据，避免口径漂移。
            dw = self.resolve_dual_write()
            return {
                "ok": True,
                "enabled": True,
                "provider": "milvus",
                "collection": collection,
                "collection_exists": collection_exists,
                "dual_write": bool(dw.get("active")),
                "dual_write_requested": bool(dw.get("requested")),
                "dual_shadow_collection": str(dw.get("shadow") or ""),
                "dual_write_reason": str(dw.get("reason") or ""),
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

    def resolve_write_scope(self) -> Dict[str, Any]:
        """写入范围（canary 保护）+ 可写集合白名单。

        动机：S1 是**进程全局**配置，开启后所有 KB 都走扇出。授权通常只针对**单个目标 KB**，
        因此需要一份可执行的 KB 级限制，把"全局开关"收敛回"目标 KB"。

        配置（vector_store 类目 / 环境变量）：
          write_scope.kb_ids      ← MILVUS_WRITE_SCOPE_KB_IDS（逗号分隔；**留空 = 不限制**，保持现状）
          write_scope.tenant_id   ← MILVUS_WRITE_SCOPE_TENANT（可选，非空则必须逐 chunk 匹配）
          write_scope.project_id  ← MILVUS_WRITE_SCOPE_PROJECT（可选）

        返回统一结构；`kb_ids` 非空时，任何不在名单内的 KB 在**任何 mutation 之前**被拒。
        """
        loaded = get_vector_store_runtime_config()
        raw = loaded.get("write_scope")
        scope = raw if isinstance(raw, dict) else {}
        kb_ids = scope.get("kb_ids") or []
        if isinstance(kb_ids, str):
            kb_ids = [item.strip() for item in kb_ids.split(",") if item.strip()]
        return {
            "kb_ids": [str(item).strip() for item in kb_ids if str(item).strip()],
            "tenant_id": str(scope.get("tenant_id") or "").strip(),
            "project_id": str(scope.get("project_id") or "").strip(),
        }

    def _guard_write_scope(self, chunks: List[VectorChunk], collection: str, operation: str) -> None:
        """mutation 前校验：目标 KB / tenant / project / collection 均须在授权范围内。

        - 未配置 kb_ids（默认）→ 不限制，行为与历史一致；
        - 配置后：非名单 KB 一律拒（`KB_NOT_ALLOWED`），tenant/project 非空时必须逐 chunk 相等
          （`SCOPE_MISMATCH`）；collection 只能是本次解析出的 primary/shadow（`COLLECTION_NOT_WRITABLE`）。
        - 逐 chunk 判定，**任一不合规即整批拒**（不部分写）。
        """
        dw = self.resolve_dual_write()
        allowed_collections = {c for c in (dw.get("primary"), dw.get("shadow")) if c}
        if allowed_collections and collection not in allowed_collections:
            raise VectorWriteContractError(
                f"collection {collection} 不在本次可写集合 {sorted(allowed_collections)} 内，"
                f"拒绝执行 {operation}",
                reason=REASON_COLLECTION_NOT_WRITABLE,
            )
        scope = self.resolve_write_scope()
        kb_ids = scope["kb_ids"]
        if not kb_ids:
            return
        for chunk in chunks:
            kb_id = str(chunk.kb_id or "")
            if kb_id not in kb_ids:
                raise VectorWriteContractError(
                    f"KB {kb_id!r} 不在授权写入范围 {kb_ids} 内，拒绝执行 {operation}（零写入）",
                    reason=REASON_KB_NOT_ALLOWED,
                )
            if scope["tenant_id"] and str(chunk.tenant_id or "") != scope["tenant_id"]:
                raise VectorWriteContractError(
                    f"KB {kb_id!r} 的 tenant_id 与授权范围不一致，拒绝执行 {operation}",
                    reason=REASON_SCOPE_MISMATCH,
                )
            if scope["project_id"] and str(chunk.project_id or "") != scope["project_id"]:
                raise VectorWriteContractError(
                    f"KB {kb_id!r} 的 project_id 与授权范围不一致，拒绝执行 {operation}",
                    reason=REASON_SCOPE_MISMATCH,
                )

    def _guard_kb_write_scope(self, kb_ids: List[str], operation: str) -> None:
        """按 KB 名单校验的轻量版本（delete/clear 等没有 chunk 对象的路径）。"""
        scope = self.resolve_write_scope()
        allowed = scope["kb_ids"]
        if not allowed:
            return
        for kb_id in kb_ids:
            if str(kb_id or "") not in allowed:
                raise VectorWriteContractError(
                    f"KB {kb_id!r} 不在授权写入范围 {allowed} 内，拒绝执行 {operation}（零写入）",
                    reason=REASON_KB_NOT_ALLOWED,
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

    def verify_versioned_rows(self, collection: str, expected: Dict[str, Dict[str, Any]]) -> Optional[bool]:
        """写入后读回校验（统一实现，worker 与 backfill 共用）。

        expected: `{chunk_id: {"content_revision": int|None, "text": str|None}}`
        返回三态：`True` 全部一致 / `False` 明确不一致 / `None` **无法校验**（取不到 client、
        client 不支持 query、查询抛错）。**绝不把 None 当 True**——调用方必须显式处理 None
        （回填契约把 None 视为"不可标 indexed"，宁可 pending）。
        """
        if not expected:
            return True
        try:
            client = self._get_client()
        except Exception:  # noqa: BLE001 - 取不到 client 属于"无法校验"
            return None
        query = getattr(client, "query", None)
        if not callable(query):
            return None
        expr = "chunk_id in [" + ", ".join(
            f'"{self._escape_filter_value(str(cid))}"' for cid in sorted(expected)
        ) + "]"
        try:
            rows = list(query(collection_name=collection, filter=expr,
                              output_fields=["chunk_id", "text", "content_revision"],
                              limit=len(expected)) or [])
        except Exception:  # noqa: BLE001 - 读回失败 = 无法校验（不冒充已验证）
            return None
        got = {str(row.get("chunk_id") or ""): row for row in rows if isinstance(row, dict)}
        for chunk_id, want in expected.items():
            row = got.get(str(chunk_id))
            if row is None:
                return False
            rev = want.get("content_revision")
            if rev is not None:
                try:
                    if int(row.get("content_revision")) != int(rev):
                        return False
                except (TypeError, ValueError):
                    return False
            want_text = want.get("text")
            if want_text is not None and str(row.get("text") or "") != str(want_text):
                return False
        return True

    def _query_present_ids(self, collection: str, chunk_ids: List[str]) -> Optional[set]:
        """只读探测 collection 里已存在的 chunk_id 集合；无法读取返回 None（调用方须 fail-closed）。"""
        try:
            client = self._get_client()
        except Exception:  # noqa: BLE001
            return None
        if not client.has_collection(collection):
            return set()
        query = getattr(client, "query", None)
        if not callable(query):
            return None
        expr = "chunk_id in [" + ", ".join(
            f'"{self._escape_filter_value(str(cid))}"' for cid in sorted(set(chunk_ids))
        ) + "]"
        try:
            rows = list(query(collection_name=collection, filter=expr,
                              output_fields=["chunk_id"], limit=len(set(chunk_ids))) or [])
        except Exception:  # noqa: BLE001
            return None
        return {str(row.get("chunk_id") or "") for row in rows if isinstance(row, dict)}

    def _upsert_backfill_versioned(
        self, dw: Dict[str, Any], chunks: List[VectorChunk], vectors: List[List[float]], dimension: Optional[int]
    ) -> int:
        """历史回填意图：**只补版本承载侧（shadow/v3）**，不写 primary（v2 不改 schema、不覆写既有行）。

        契约前置（全部在**写之前**判定，任一不满足即整批拒、零写入）：
        1. shadow 必须生效（否则 revision 无处可靠承载 → 保持 pending，绝不下沉到 dynamic metadata）；
        2. shadow 必须有显式 INT64 `content_revision` 字段；
        3. **读源不变量（§16.1）**：primary 必须已持有这些 chunk——否则"只写 v3 + 读 v2"会让读源丢数据；
           读不到 primary（`PRIMARY_UNREADABLE`）同样拒绝（不能证明不丢数据）。
        写后必须读回一致才返回（不一致 `VERIFY_MISMATCH`、无法校验 `VERIFY_UNAVAILABLE` 均拒绝）。
        """
        shadow = str(dw.get("shadow") or "")
        if not dw.get("active") or not shadow:
            raise VectorWriteContractError(
                "历史回填需要生效的双写影子集合来承载版本号；当前 dual_write/shadow_collection 未生效。"
                "按 §8.5 禁止把 revision 写进 primary 的 dynamic metadata，本批保持 pending。",
                reason=REASON_SHADOW_NOT_ACTIVE,
            )
        if not self.has_content_revision_field(shadow):
            raise VectorWriteContractError(
                f"影子 collection {shadow} 缺少显式 content_revision 字段，禁止回填版本号"
                "（写进去只会进 dynamic metadata）。请先按 §8.5 建带显式 INT64 字段的 v3 集合。",
                reason=REASON_SHADOW_FIELD_MISSING,
            )
        present = self._query_present_ids(str(dw.get("primary") or ""), [c.chunk_id for c in chunks])
        if present is None:
            raise VectorWriteContractError(
                f"无法读取 primary {dw.get('primary')} 的现有 chunk，不能证明读源不丢数据（§16.1），"
                "拒绝只写 shadow。",
                reason=REASON_PRIMARY_UNREADABLE,
            )
        missing = [c.chunk_id for c in chunks if c.chunk_id not in present]
        if missing:
            raise VectorWriteContractError(
                f"primary {dw.get('primary')} 尚无 {len(missing)} 个 chunk（示例 {missing[:5]}）："
                "只写 shadow 会让读源丢数据（§16.1 禁止 v3 写 + v2 读），拒绝回填。",
                reason=REASON_PRIMARY_VECTOR_MISSING,
            )
        written = self._upsert_to(shadow, chunks, vectors, dimension)
        expected = {
            c.chunk_id: {"content_revision": c.content_revision, "text": c.text} for c in chunks
        }
        verified = self.verify_versioned_rows(shadow, expected)
        if verified is False:
            raise VectorWriteContractError(
                f"影子 {shadow} 写后读回与写入不一致，拒绝标记 indexed（投影保持 pending）",
                reason=REASON_VERIFY_MISMATCH,
            )
        if verified is None:
            raise VectorWriteContractError(
                f"影子 {shadow} 写后读回不可用（无法校验），拒绝标记 indexed（宁可 pending）",
                reason=REASON_VERIFY_UNAVAILABLE,
            )
        return written

    def upsert_chunks(
        self,
        chunks: List[VectorChunk],
        vectors: List[List[float]],
        *,
        intent: str = WRITE_INTENT_ONLINE,
    ) -> int:
        """**统一写入入口**（唯一扇出实现）。

        - `intent=online_s1`（默认）：primary 先写 + shadow 扇出，读仍 primary；影子失败 fail-closed。
        - `intent=backfill_versioned`：只补 shadow（版本承载侧），前置校验见 `_upsert_backfill_versioned`。
        两条意图共用同一份 config 守卫、范围守卫、`_upsert_to`（§8.5 显式字段门 + 确认数校验）与写后读回。
        """
        dw = self.resolve_dual_write()
        self._guard_dual_write_config(dw, f"upsert_chunks[{intent}]")
        if not dw["enabled"] or not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        vector_dimension = len(vectors[0]) if vectors and vectors[0] else None

        if intent == WRITE_INTENT_BACKFILL:
            self._guard_write_scope(chunks, str(dw.get("shadow") or dw.get("primary") or ""),
                                    "upsert_chunks[backfill_versioned]")
            return self._upsert_backfill_versioned(dw, chunks, vectors, vector_dimension)
        if intent != WRITE_INTENT_ONLINE:
            raise ValueError(f"未知写入意图: {intent!r}")

        self._guard_write_scope(chunks, str(dw.get("primary") or ""), "upsert_chunks[online_s1]")

        # §8.5 + S1 的 legacy v2 合同（工单 1：在线双写与历史回填的目标分开定义）：
        #   primary 有显式 INT64 字段 → 两侧都写显式版本（正常 S1）；
        #   primary 缺字段 + 影子生效   → primary **剥版本写**（不写 metadata、不改 v2 schema、
        #                                版本由影子完整承载），影子必须先具备显式字段，否则 fail-closed；
        #   primary 缺字段 + 无影子     → 拒写（版本无处可靠承载，绝不降级进 dynamic metadata）。
        versioned = any(chunk.content_revision is not None for chunk in chunks)
        primary_has_field = self.has_content_revision_field(dw["primary"])
        primary_chunks = chunks
        if versioned and not primary_has_field:
            if not dw["active"]:
                raise VectorStoreSchemaError(
                    f"Milvus collection {dw['primary']} 缺少显式 content_revision 字段且无生效影子集合，"
                    "禁止把投影版本号写入 dynamic metadata；请按 §8.5/§15.4 迁移到 "
                    "graphinsight_chunks_v3 后重建向量"
                )
            if not self.has_content_revision_field(dw["shadow"]):
                raise VectorStoreSchemaError(
                    f"影子 collection {dw['shadow']} 缺少显式 content_revision 字段：主库 "
                    f"{dw['primary']} 无法承载版本，若影子也不承载则会静默丢版本，拒绝写入"
                )
            from dataclasses import replace as _dc_replace

            primary_chunks = [
                _dc_replace(chunk, content_revision=None) if chunk.content_revision is not None else chunk
                for chunk in chunks
            ]

        # 主库（读源）先写。主库失败直接抛出：读源保持未收敛、调用方重试，
        # 绝不允许出现"影子已写、主库未写"，也不触发任何影子写。
        primary_written = self._upsert_to(dw["primary"], primary_chunks, vectors, vector_dimension)

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
        # 写入范围守卫（canary 保护）：非授权 KB 在**任何删除之前**拒绝，零写入。
        self._guard_kb_write_scope([clean_kb], "delete_doc")
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
        # 写入范围守卫（canary 保护）：非授权 KB 在**任何清空之前**拒绝，零写入。
        self._guard_kb_write_scope(list(kb_ids), "clear")
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
