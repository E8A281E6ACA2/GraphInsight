"""reindex_chunks 投影重建 worker（M5-B0，设计 §8.2/§8.3/§15.2/§15.5/§15.9 冻结语义）。

与旧 `reindex`（Neo4j 全文索引重建，基础设施操作）**不是同一件事**，禁止互相复用：
本任务只重建 chunk 的 Neo4j/Milvus 投影版本，不产生新 revision。

口径：
1. 范围只来自 `payload.targets`（§8.1 无隐式扩范围）：targets 缺失/为空/元素非法
   → `ValidationException(REINDEX_SCOPE_REQUIRED)`，不写任何索引。
2. 作用域权威 = `knowledge_bases` 登记表；payload 与登记值冲突 → `KB_CROSS_SCOPE` 零写入；
   current 行的 tenant/project/doc 与 payload 冲突 → 该 chunk 零写入并计入 `scope_mismatches`。
3. 拾取规则（§8.2 步骤 1）：无 current 行、或 `target_revision != current.content_revision`
   → `OUTDATED_SKIPPED`，不写索引（旧任务保护 §8.3）。
4. 四道复核（§8.3）：读清单 → 写索引前 → 回写状态前 → 回写状态后各复核一次 current；
   current 已移动即放弃剩余写入，投影由新任务收敛。
5. 内容唯一来源 = current 行的 `content`（§10：reindex 只重建投影，以 current content 为源）；
   content 为空 → 两侧投影 `failed` 并让任务失败，绝不写空文本投影。
6. graph 投影写 Neo4j `Chunk.text + content_revision(= target)`；LLM 未配置 → 不写索引、
   状态置 `skipped` 且 `graph_content_revision IS NULL`（§8.4，不伪装 indexed）。
   实体/关系抽取不在本切片：委派 `build_graph` 会创建新 revision，违反 §10 reindex 语义，
   因此本任务只重建 Chunk 文本投影，抽取腿需要 M5-B 独立入口（结果 `notes` 如实标注）。
7. vector 投影要求 collection 有显式 `content_revision` 字段（§8.5）；embedding 未配置 →
   不写索引、状态置 `skipped`；字段缺失 → 拒写、投影保持 pending，并以 `INDEX_UNAVAILABLE`
   失败（ValidationException 不自动重试，等 v3 迁移后重新入队）。
8. 状态回写一律 CAS（`WHERE content_revision = target AND revision_status='current'`），
   rowcount=0 视为 current 已移动。
9. 结束按 §6.2 聚合重算受影响文档的 `knowledge_base_documents.graph_status/vector_status`。
10. 失败语义：targets/作用域非法或 collection 缺字段 → `ValidationException`（不重试，§15.5）；
    投影写入或状态回写失败 → 抛 `RuntimeError`（按 admin_jobs max_retries 指数退避重试）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from config import get_settings
from core import ValidationException, get_logger
from core.exceptions import ErrorCode
from services.chunk_projection_state import aggregate_document_states, update_projection_state
from services.runtime_config import get_projection_capabilities
from services.scope_contract import milvus_kb_filter

logger = get_logger()
settings = get_settings()

JOB_TYPE = "reindex_chunks"
NEO4J_BATCH_SIZE = 50
MILVUS_BATCH_SIZE = 10
MILVUS_QUERY_BATCH_SIZE = 200

OUTCOME_INDEXED = "indexed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_ALREADY = "already_indexed"
OUTCOME_NO_CONTENT = "no_content"
OUTCOME_WRITE_FAILED = "write_failed"
OUTCOME_CURRENT_MOVED = "current_moved"
OUTCOME_STATE_WRITE_FAILED = "state_write_failed"
OUTCOME_REVISION_FIELD_ABSENT = "revision_field_absent"

NOTES = (
    "graph 投影只重建 Chunk.text/content_revision；实体与关系抽取不在 reindex_chunks 范围"
    "（委派 build_graph 会产生新 revision，违反设计 §10 reindex 语义）"
)


def _engine():
    from admin.database import engine

    return engine


def _in_clause(values: List[str]) -> str:
    return ", ".join(f":v_{index}" for index, _ in enumerate(values))


def _in_params(values: List[str]) -> Dict[str, str]:
    return {f"v_{index}": value for index, value in enumerate(values)}


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _load_current_rows(kb_id: str, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """读取本 KB 的 current revision 行（含 content，投影重建的唯一内容源）。"""
    if not chunk_ids:
        return {}
    with _engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT chunk_id, doc_id, tenant_id, project_id, content_revision, content, "
                "graph_status, vector_status, graph_content_revision, vector_content_revision "
                f"FROM chunk_revisions WHERE kb_id = :kb_id AND revision_status = 'current' "
                f"AND chunk_id IN ({_in_clause(chunk_ids)})"
            ),
            {"kb_id": kb_id, **_in_params(chunk_ids)},
        ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        result[str(row[0])] = {
            "chunk_id": str(row[0]),
            "doc_id": str(row[1] or ""),
            "tenant_id": str(row[2] or ""),
            "project_id": str(row[3] or ""),
            "content_revision": int(row[4]),
            "content": str(row[5] or ""),
            "graph_status": str(row[6] or ""),
            "vector_status": str(row[7] or ""),
            "graph_content_revision": None if row[8] is None else int(row[8]),
            "vector_content_revision": None if row[9] is None else int(row[9]),
        }
    return result


def _load_kb_registry_scope(kb_id: str) -> Dict[str, str]:
    """knowledge_bases 登记作用域（权威源）；未登记返回空 dict。"""
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT tenant_id, project_id FROM knowledge_bases WHERE id = :kb_id"),
            {"kb_id": kb_id},
        ).fetchone()
    if not row:
        return {}
    return {"tenant_id": str(row[0] or ""), "project_id": str(row[1] or "")}


def _normalize_targets(raw: Any) -> List[Dict[str, Any]]:
    """targets 必须是非空 `[{chunk_id, target_revision}]`（§15.2）；非法即拒绝执行。"""
    items = raw if isinstance(raw, list) else []
    targets: List[Dict[str, Any]] = []
    invalid: List[str] = []
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            invalid.append(str(item))
            continue
        chunk_id = str(item.get("chunk_id") or "").strip()
        try:
            target_revision = int(item.get("target_revision"))
        except (TypeError, ValueError):
            invalid.append(f"{chunk_id or '<empty>'}:target_revision")
            continue
        if not chunk_id or target_revision < 1:
            invalid.append(f"{chunk_id or '<empty>'}:{item.get('target_revision')}")
            continue
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        targets.append({"chunk_id": chunk_id, "target_revision": target_revision})
    if invalid or not targets:
        raise ValidationException(
            "reindex_chunks 需要非空且合法的 targets 列表（chunk_id + target_revision）",
            error_code=ErrorCode.REINDEX_SCOPE_REQUIRED,
            details={"invalid": invalid[:10], "targets_received": len(items)},
        )
    return targets


def _require_registry_scope_matches(kb_id: str, scope: Dict[str, str]) -> None:
    """payload 作用域 vs knowledge_bases 登记：冲突 → KB_CROSS_SCOPE，零写入 fail-closed。"""
    registry = _load_kb_registry_scope(kb_id)
    conflicts: List[Dict[str, str]] = []
    for field in ("tenant_id", "project_id"):
        expected = str(registry.get(field) or "")
        actual = str(scope.get(field) or "")
        if expected and actual and expected != actual:
            conflicts.append({"field": field, "expected": expected, "actual": actual})
    if conflicts:
        raise ValidationException(
            "reindex_chunks payload 作用域与知识库登记不一致",
            error_code=ErrorCode.KB_CROSS_SCOPE,
            details={"kb_id": kb_id, "conflicts": conflicts},
        )


def _row_scope_conflicts(row: Dict[str, Any], scope: Dict[str, str], payload_doc_id: str) -> List[Dict[str, str]]:
    conflicts: List[Dict[str, str]] = []
    for field in ("tenant_id", "project_id"):
        expected = str(scope.get(field) or "")
        actual = str(row.get(field) or "")
        if expected and actual and expected != actual:
            conflicts.append({"field": f"revision.{field}", "expected": expected, "actual": actual})
    row_doc_id = str(row.get("doc_id") or "")
    if payload_doc_id and row_doc_id and payload_doc_id != row_doc_id:
        conflicts.append({"field": "revision.doc_id", "expected": payload_doc_id, "actual": row_doc_id})
    return conflicts


def _batched(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _write_neo4j_projection(kb_id: str, items: List[Dict[str, Any]]) -> Dict[str, str]:
    """写 Neo4j Chunk 文本投影 + content_revision（= target，参数化不硬编码）。

    返回 {chunk_id: indexed|write_failed}；单批异常或数量不匹配整批判 failed，不虚报 indexed。
    """
    from neo4j import GraphDatabase

    outcome: Dict[str, str] = {}
    if not items:
        return outcome
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            for batch in _batched(items, NEO4J_BATCH_SIZE):
                chunk_ids = [item["chunk_id"] for item in batch]
                rows = [
                    {
                        "chunk_id": item["chunk_id"],
                        "doc_id": item["doc_id"],
                        "tenant_id": item["tenant_id"],
                        "project_id": item["project_id"],
                        "text": item["content"],
                        "content_revision": item["target_revision"],
                    }
                    for item in batch
                ]
                try:
                    result = session.run(
                        """
                        UNWIND $rows AS r
                        MERGE (c:Chunk {chunk_id: r.chunk_id, kb_id: $kb_id})
                        ON CREATE SET c.source = 'document_ingest'
                        SET c.text = r.text,
                            c.doc_id = r.doc_id,
                            c.tenant_id = r.tenant_id,
                            c.project_id = r.project_id,
                            c.content_revision = r.content_revision
                        RETURN count(c) AS updated
                        """,
                        {"kb_id": kb_id, "rows": rows},
                    ).single()
                    updated = int(result["updated"]) if result else 0
                except Exception as exc:  # noqa: BLE001 - 单批失败逐 chunk 记 failed，不伪装 indexed
                    logger.warning(
                        "reindex_chunks Neo4j 投影写入失败",
                        context={"kb_id": kb_id, "batch_size": len(rows), "error": str(exc)},
                    )
                    for chunk_id in chunk_ids:
                        outcome[chunk_id] = OUTCOME_WRITE_FAILED
                    continue
                if updated != len(rows):
                    logger.warning(
                        "reindex_chunks Neo4j 投影写入数量不匹配，保守判 failed",
                        context={"kb_id": kb_id, "expected": len(rows), "updated": updated},
                    )
                state = OUTCOME_INDEXED if updated == len(rows) else OUTCOME_WRITE_FAILED
                for chunk_id in chunk_ids:
                    outcome[chunk_id] = state
        return outcome
    finally:
        driver.close()


MILVUS_MERGE_FIELDS = ["doc_id", "title", "location", "entities_json", "embedding_model"]


def _existing_milvus_fields(kb_id: str, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """读取已有 Milvus 记录的可复用字段。

    Milvus upsert 是整行替换，不带回这些字段就会把已有 title/location/entities 洗掉，
    所以重建向量前先读回再合并。output_fields 按 collection 实际 schema 过滤，
    缺字段的老 collection 不会因为请求不存在的字段而报错（审计修复 #3 同源口径）。
    """
    from pymilvus import MilvusClient
    from services.vector_store import vector_store

    if not chunk_ids:
        return {}
    cfg = vector_store.config()
    collection = str(cfg.get("collection") or "")
    if not collection:
        return {}
    client = MilvusClient(
        uri=str(cfg.get("uri") or ""),
        token=str(cfg.get("token") or ""),
        db_name=str(cfg.get("db_name") or ""),
    )
    if not client.has_collection(collection):
        return {}
    try:
        description = client.describe_collection(collection)
        fields = description.get("fields") if isinstance(description, dict) else None
        names = {str(item.get("name") or "") for item in (fields or []) if isinstance(item, dict)}
    except Exception as exc:  # noqa: BLE001 - 读不到 schema 时禁止整行替换，避免洗掉旧元数据
        raise RuntimeError(
            f"Milvus collection schema probe failed for reindex_chunks: kb_id={kb_id}"
        ) from exc
    output_fields = [field for field in MILVUS_MERGE_FIELDS if field in names]
    if "chunk_id" not in output_fields:
        output_fields = ["chunk_id"] + output_fields

    merged: Dict[str, Dict[str, Any]] = {}
    for batch in _batched(chunk_ids, MILVUS_QUERY_BATCH_SIZE):
        expr = " && ".join(
            [
                milvus_kb_filter([kb_id]),
                "chunk_id in [" + ", ".join(f'"{_escape(item)}"' for item in batch) + "]",
            ]
        )
        try:
            page = list(
                client.query(
                    collection_name=collection,
                    filter=expr,
                    output_fields=output_fields,
                    limit=len(batch),
                )
                or []
            )
        except Exception as exc:  # noqa: BLE001 - upsert 是整行替换，读回失败必须 fail closed
            raise RuntimeError(
                f"reindex_chunks Milvus existing-field read failed: kb_id={kb_id}"
            ) from exc
        for row in page:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            entities: List[str] = []
            raw_entities = row.get("entities_json")
            if isinstance(raw_entities, str) and raw_entities.strip():
                try:
                    loaded = json.loads(raw_entities)
                except json.JSONDecodeError:
                    loaded = None
                if isinstance(loaded, list):
                    entities = [str(e) for e in loaded if str(e).strip()]
            merged[chunk_id] = {
                "title": str(row.get("title") or ""),
                "location": str(row.get("location") or ""),
                "entities": entities,
                "embedding_model": str(row.get("embedding_model") or ""),
            }
    return merged


def _write_milvus_projection(
    kb_id: str,
    items: List[Dict[str, Any]],
    *,
    tenant_id: str,
    project_id: str,
) -> Dict[str, str]:
    """embedding 后按显式 `content_revision` upsert。

    前置条件是 collection 有显式 content_revision 字段（§8.5）；`upsert_chunks` 内还有
    同一判据的边界守卫，任一失败都记 write_failed，不写不可靠版本。
    """
    from services.embedding_service import embedding_service
    from services.vector_store import VectorChunk, vector_store

    outcome: Dict[str, str] = {}
    if not items:
        return outcome
    existing = _existing_milvus_fields(kb_id, [item["chunk_id"] for item in items])
    cfg = embedding_service.config()
    for batch in _batched(items, MILVUS_BATCH_SIZE):
        try:
            vectors = embedding_service.embed_texts([item["content"] for item in batch])
        except Exception as exc:  # noqa: BLE001
            logger.warning("reindex_chunks embedding 失败", context={"kb_id": kb_id, "error": str(exc)})
            for item in batch:
                outcome[item["chunk_id"]] = OUTCOME_WRITE_FAILED
            continue
        if len(vectors) != len(batch):
            logger.warning(
                "reindex_chunks embedding 返回数量不一致",
                context={"kb_id": kb_id, "expected": len(batch), "returned": len(vectors)},
            )
            for item in batch:
                outcome[item["chunk_id"]] = OUTCOME_WRITE_FAILED
            continue
        chunks = []
        for item, vector in zip(batch, vectors):
            merged = existing.get(item["chunk_id"]) or {}
            chunks.append(
                VectorChunk(
                    chunk_id=item["chunk_id"],
                    doc_id=item["doc_id"],
                    text=item["content"],
                    title=merged.get("title", ""),
                    location=merged.get("location", ""),
                    entities=merged.get("entities", []),
                    content_hash=embedding_service.content_hash(item["content"]),
                    embedding_model=merged.get("embedding_model") or str(cfg.get("model") or ""),
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    content_revision=item["target_revision"],
                )
            )
        try:
            written = vector_store.upsert_chunks(chunks, vectors)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reindex_chunks Milvus upsert 失败", context={"kb_id": kb_id, "error": str(exc)})
            for item in batch:
                outcome[item["chunk_id"]] = OUTCOME_WRITE_FAILED
            continue
        state = OUTCOME_INDEXED if written == len(chunks) else OUTCOME_WRITE_FAILED
        for item in batch:
            outcome[item["chunk_id"]] = state
    return outcome


def _side_needed(row: Dict[str, Any], side: str, target_revision: int) -> bool:
    """幂等跳过：该侧已 indexed 且版本等于 target 就不必重建（§6.1 worker 重试 no-op）。"""
    return not (
        row.get(f"{side}_status") == OUTCOME_INDEXED
        and row.get(f"{side}_content_revision") == target_revision
    )


def _status_kwargs(item: Dict[str, Any], graph_state: str, vector_state: str) -> Dict[str, Any]:
    """只把 indexed / skipped / failed 落成投影状态；其余取值不改 DB（保守）。"""
    target = item["target_revision"]
    kwargs: Dict[str, Any] = {}
    mapping = {"graph": graph_state, "vector": vector_state}
    for side, state in mapping.items():
        if state == OUTCOME_INDEXED:
            kwargs[f"{side}_status"] = OUTCOME_INDEXED
            kwargs[f"{side}_content_revision"] = target
        elif state == OUTCOME_SKIPPED:
            kwargs[f"{side}_status"] = OUTCOME_SKIPPED
            kwargs[f"{side}_content_revision"] = None
        elif state in (OUTCOME_WRITE_FAILED, OUTCOME_NO_CONTENT):
            kwargs[f"{side}_status"] = OUTCOME_FAILED
            kwargs[f"{side}_content_revision"] = None
    return kwargs


def reindex_chunks(*, job_id: int, payload: Dict[str, Any], scope: Dict[str, str]) -> Dict[str, Any]:
    """消费一个 reindex_chunks job：按 targets 重建 chunk 投影并回写状态。"""
    kb_id = scope["kb_id"]
    targets = _normalize_targets(payload.get("targets"))
    payload_doc_id = str(payload.get("doc_id") or "").strip()
    _require_registry_scope_matches(kb_id, scope)
    capabilities = get_projection_capabilities()

    # 复核 1：targets 对应的 current 行清单
    rows = _load_current_rows(kb_id, [item["chunk_id"] for item in targets])

    outdated: List[Dict[str, Any]] = []
    scope_mismatches: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    for target in targets:
        row = rows.get(target["chunk_id"])
        if row is None:
            outdated.append({**target, "reason": "no_current_revision"})
            continue
        if row["content_revision"] != target["target_revision"]:
            outdated.append(
                {**target, "reason": "revision_moved", "current_revision": row["content_revision"]}
            )
            continue
        conflicts = _row_scope_conflicts(row, scope, payload_doc_id)
        if conflicts:
            scope_mismatches.append({"chunk_id": target["chunk_id"], "conflicts": conflicts})
            continue
        candidates.append({**target, "row": row})

    report: Dict[str, Any] = {
        "job_id": job_id,
        "job_type": JOB_TYPE,
        "kb_id": kb_id,
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "doc_id": payload_doc_id or None,
        "source": str(payload.get("source") or ""),
        "capabilities": dict(capabilities),
        "targets_total": len(targets),
        "outdated_skipped": outdated,
        "scope_mismatches": scope_mismatches,
        "outcomes": {},
        "notes": [NOTES],
    }
    if not candidates:
        report["execution_status"] = "no_write"
        report["message"] = "没有 target 命中当前 current revision，未写入任何投影"
        report["counts"] = {"targets": len(targets), "outdated": len(outdated), "scope_mismatch": len(scope_mismatches)}
        return report

    # 复核 2：写索引前逐目标确认 current 仍等于 target
    fresh = _load_current_rows(kb_id, [item["chunk_id"] for item in candidates])
    items: List[Dict[str, Any]] = []
    outcomes: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        chunk_id = candidate["chunk_id"]
        row = fresh.get(chunk_id) or candidate["row"]
        if row["content_revision"] != candidate["target_revision"]:
            outdated.append(
                {
                    "chunk_id": chunk_id,
                    "target_revision": candidate["target_revision"],
                    "reason": "revision_moved_pre_write",
                    "current_revision": row["content_revision"],
                }
            )
            continue
        if not row["content"].strip():
            outcomes[chunk_id] = {
                "graph": OUTCOME_NO_CONTENT,
                "vector": OUTCOME_NO_CONTENT,
                "doc_id": row["doc_id"],
            }
            continue
        items.append(
            {
                "chunk_id": chunk_id,
                "doc_id": row["doc_id"],
                "tenant_id": row["tenant_id"] or scope["tenant_id"],
                "project_id": row["project_id"] or scope["project_id"],
                "content": row["content"],
                "target_revision": candidate["target_revision"],
                "needs_graph": _side_needed(row, "graph", candidate["target_revision"]),
                "needs_vector": _side_needed(row, "vector", candidate["target_revision"]),
            }
        )

    graph_items = [item for item in items if item["needs_graph"]]
    vector_items = [item for item in items if item["needs_vector"]]

    # 能力未配置时不写索引，状态由 _resolve_state/_resolve_vector_state 落 skipped（§8.4）
    graph_write = [item for item in graph_items if capabilities["graph"]]
    vector_write = [item for item in vector_items if capabilities["vector"]]
    graph_outcome = _write_neo4j_projection(kb_id, graph_write) if graph_write else {}

    vector_blocked = ""
    vector_outcome: Dict[str, str] = {}
    if vector_write:
        from services.vector_store import vector_store

        if not vector_store.has_content_revision_field():
            # §8.5：collection 无显式字段就不写不可靠版本，投影保持 pending 等 v3 迁移
            vector_blocked = OUTCOME_REVISION_FIELD_ABSENT
        else:
            vector_outcome = _write_milvus_projection(
                kb_id,
                vector_write,
                tenant_id=scope["tenant_id"],
                project_id=scope["project_id"],
            )

    # 复核 3：回写状态前确认 current 未被并发编辑移动
    post = _load_current_rows(kb_id, [item["chunk_id"] for item in items])
    state_written: List[str] = []
    targets_by_chunk = {item["chunk_id"]: item["target_revision"] for item in items}
    for item in items:
        chunk_id = item["chunk_id"]
        row = post.get(chunk_id)
        if row is None or row["content_revision"] != item["target_revision"]:
            outcomes[chunk_id] = {
                "graph": OUTCOME_CURRENT_MOVED,
                "vector": OUTCOME_CURRENT_MOVED,
                "doc_id": item["doc_id"],
            }
            continue
        graph_state = _resolve_state(item["needs_graph"], capabilities["graph"], graph_outcome.get(chunk_id))
        vector_state = _resolve_vector_state(
            item["needs_vector"], capabilities["vector"], vector_blocked, vector_outcome.get(chunk_id)
        )
        kwargs = _status_kwargs(item, graph_state, vector_state)
        if kwargs and update_projection_state(
            kb_id, chunk_id, expected_revision=item["target_revision"], **kwargs
        ) == 0:
            outcomes[chunk_id] = {
                "graph": OUTCOME_CURRENT_MOVED,
                "vector": OUTCOME_CURRENT_MOVED,
                "doc_id": item["doc_id"],
            }
            continue
        outcomes[chunk_id] = {"graph": graph_state, "vector": vector_state, "doc_id": item["doc_id"]}
        if kwargs:
            state_written.append(chunk_id)

    # 复核 4：回写后确认落库版本，没落到 indexed 就不宣布 indexed
    verify = _load_current_rows(kb_id, state_written) if state_written else {}
    for chunk_id in state_written:
        row = verify.get(chunk_id)
        claimed = outcomes[chunk_id]
        target = targets_by_chunk.get(chunk_id)
        if row is None or row["content_revision"] != target:
            claimed["graph"] = OUTCOME_STATE_WRITE_FAILED
            claimed["vector"] = OUTCOME_STATE_WRITE_FAILED
            continue
        for side in ("graph", "vector"):
            if claimed[side] == OUTCOME_INDEXED and (
                row[f"{side}_status"] != OUTCOME_INDEXED
                or row[f"{side}_content_revision"] != row["content_revision"]
            ):
                claimed[side] = OUTCOME_STATE_WRITE_FAILED

    doc_ids = sorted({item["doc_id"] for item in items if item["doc_id"]})
    document_states = aggregate_document_states(kb_id, doc_ids)

    def _count(side: str, state: str) -> int:
        return len([detail for detail in outcomes.values() if detail[side] == state])

    failed_chunks = sorted(
        chunk_id
        for chunk_id, detail in outcomes.items()
        if OUTCOME_WRITE_FAILED in (detail["graph"], detail["vector"])
        or OUTCOME_STATE_WRITE_FAILED in (detail["graph"], detail["vector"])
        or OUTCOME_NO_CONTENT in (detail["graph"], detail["vector"])
    )
    counts = {
        "targets": len(targets),
        "candidates": len(candidates),
        "written": len(items),
        "outdated": len(outdated),
        "scope_mismatch": len(scope_mismatches),
        "graph_indexed": _count("graph", OUTCOME_INDEXED),
        "graph_skipped": _count("graph", OUTCOME_SKIPPED),
        "graph_failed": _count("graph", OUTCOME_WRITE_FAILED) + _count("graph", OUTCOME_STATE_WRITE_FAILED)
        + _count("graph", OUTCOME_NO_CONTENT),
        "vector_indexed": _count("vector", OUTCOME_INDEXED),
        "vector_skipped": _count("vector", OUTCOME_SKIPPED),
        "vector_failed": _count("vector", OUTCOME_WRITE_FAILED) + _count("vector", OUTCOME_STATE_WRITE_FAILED)
        + _count("vector", OUTCOME_NO_CONTENT),
        "vector_blocked": _count("vector", OUTCOME_REVISION_FIELD_ABSENT),
        "current_moved": _count("graph", OUTCOME_CURRENT_MOVED),
        "failed_chunks": len(failed_chunks),
    }
    report.update(
        {
            "outcomes": outcomes,
            "counts": counts,
            "document_states": document_states,
            "vector_blocked": vector_blocked or None,
            "execution_status": "completed",
            "message": (
                "投影重建完成"
                if not failed_chunks
                and not vector_blocked
                and not outdated
                and not scope_mismatches
                and counts["current_moved"] == 0
                else "本轮未完全收敛（存在过期/作用域冲突/失败目标），见 counts/outcomes 明细"
            ),
        }
    )

    if vector_blocked:
        raise ValidationException(
            "Milvus collection 缺少显式 content_revision 字段，reindex_chunks 拒写向量投影"
            "（设计 §8.5/§15.4：迁移 graphinsight_chunks_v3 后重新入队）",
            error_code=ErrorCode.INDEX_UNAVAILABLE,
            details={"kb_id": kb_id, "blocked": vector_blocked, "counts": counts},
        )
    if failed_chunks:
        error = RuntimeError(
            f"reindex_chunks 投影写入未收敛: kb_id={kb_id} failed={len(failed_chunks)} "
            f"chunks={failed_chunks[:10]}"
        )
        error.details = {
            "job_id": job_id,
            "kb_id": kb_id,
            "counts": counts,
            "outcomes": outcomes,
            "failed_chunks": failed_chunks[:50],
        }
        raise error
    return report


def _resolve_state(needed: bool, capability_enabled: bool, state: Optional[str]) -> str:
    if not needed:
        return OUTCOME_ALREADY
    if not capability_enabled:
        return OUTCOME_SKIPPED
    return state or OUTCOME_WRITE_FAILED


def _resolve_vector_state(needed: bool, capability_enabled: bool, blocked: str, state: Optional[str]) -> str:
    if not needed:
        return OUTCOME_ALREADY
    if not capability_enabled:
        return OUTCOME_SKIPPED
    if blocked:
        return blocked
    return state or OUTCOME_WRITE_FAILED
