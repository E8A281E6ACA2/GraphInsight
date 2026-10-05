"""Lightweight retrieval orchestrator for DocQA.

It keeps the public QA contract stable while allowing keyword, vector, and
graph expansion retrieval to evolve independently.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core import get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.embedding_service import embedding_service
from services.rerank_service import rerank_service
from services.runtime_config import get_retrieval_runtime_config
from services.vector_store import DualWriteShadowError, VectorSearchHit, vector_store


logger = get_logger()

try:  # neo4j 驱动缺失时（单元测试环境）仍可加载本模块；调用检索方法时才需要真实驱动
    from neo4j import Query
except Exception:  # pragma: no cover - 环境相关
    Query = None  # type: ignore[assignment]


def _neo4j_service():
    """懒加载 Neo4j 服务，避免模块加载即导入 neo4j 驱动。"""
    from services.neo4j_service import get_neo4j_service

    return get_neo4j_service()


RETRIEVAL_QUERY_TIMEOUT_SECONDS = 4.0


@dataclass
class Candidate:
    chunk_id: str
    item: Dict[str, Any]
    ranks: Dict[str, int] = field(default_factory=dict)
    source_scores: Dict[str, float] = field(default_factory=dict)
    fused_score: float = 0.0


class RetrievalOrchestrator:
    def retrieve(
        self,
        question: str,
        top_k: int,
        *,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """检索入口（M4）：kb_ids 必填，缺省/为空一律 KB_SCOPE_REQUIRED。

        keyword / vector / hybrid / graph_hybrid 四条路径与最终融合结果全部按
        kb_ids（以及可选 document_ids）硬过滤（契约 §12.2）。
        """
        from services.scope_contract import require_kb_scope

        normalized_kb_ids = require_kb_scope(kb_ids)
        clean_doc_ids = self._clean_document_ids(document_ids)
        return self._retrieve_with_mode(
            question,
            top_k,
            mode_override=None,
            kb_ids=normalized_kb_ids,
            document_ids=clean_doc_ids,
        )

    def diagnose(
        self,
        question: str,
        top_k: int,
        modes: Optional[List[str]] = None,
        *,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        from services.scope_contract import require_kb_scope

        normalized_kb_ids = require_kb_scope(kb_ids)
        clean_doc_ids = self._clean_document_ids(document_ids)
        normalized_question = (question or "").strip()
        requested_modes = modes or ["keyword", "vector", "hybrid", "graph_hybrid"]
        normalized_modes: List[str] = []
        for mode in requested_modes:
            normalized = str(mode or "").strip().lower()
            if normalized not in {"keyword", "vector", "hybrid", "graph_hybrid"}:
                continue
            if normalized not in normalized_modes:
                normalized_modes.append(normalized)
        if not normalized_modes:
            normalized_modes = ["keyword", "vector", "hybrid", "graph_hybrid"]

        runs: Dict[str, Any] = {}
        for mode in normalized_modes:
            result = self._retrieve_with_mode(
                normalized_question,
                top_k,
                mode_override=mode,
                kb_ids=normalized_kb_ids,
                document_ids=clean_doc_ids,
            )
            runs[mode] = {
                "items": [self._diagnostic_item(item) for item in result.get("items", [])],
                "trace": result.get("trace", {}),
            }
        summary = self._diagnostic_summary(runs)

        return {
            "query": normalized_question,
            "top_k": max(1, int(top_k or 1)),
            "modes": normalized_modes,
            "scope": {"kb_ids": list(normalized_kb_ids), "document_ids": list(clean_doc_ids)},
            "runs": runs,
            "summary": summary,
            "health": self.health(),
        }

    def _retrieve_with_mode(
        self,
        question: str,
        top_k: int,
        mode_override: Optional[str],
        *,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        normalized_question = (question or "").strip()
        top_k = max(1, int(top_k or 1))
        clean_doc_ids = [str(item or "").strip() for item in (document_ids or []) if str(item or "").strip()]
        cfg = get_retrieval_runtime_config()
        mode = self._normalize_mode(mode_override or str(cfg.get("mode") or "keyword"))
        candidate_limit = max(top_k, top_k * max(1, int(cfg.get("candidate_multiplier") or 6)))

        trace: Dict[str, Any] = {
            "mode": mode,
            "top_k": top_k,
            "candidate_limit": candidate_limit,
            "scope": {"kb_ids": list(kb_ids), "document_ids": list(clean_doc_ids)},
            "sources": {},
            "fusion": {"method": "rrf", "rrf_k": int(cfg.get("rrf_k") or 60)},
            "rerank": {"enabled": bool(cfg.get("rerank_enabled")), "applied": False},
        }

        if not normalized_question:
            trace["duration_ms"] = 0
            return {"items": [], "trace": trace}

        source_hits: Dict[str, List[Dict[str, Any]]] = {}

        if mode in {"keyword", "hybrid", "graph_hybrid"}:
            source_hits["keyword"] = self._keyword_search(normalized_question, candidate_limit, kb_ids, clean_doc_ids)
            trace["sources"]["keyword"] = self._source_trace(source_hits["keyword"])

        if mode in {"vector", "hybrid", "graph_hybrid"}:
            vector_result = self._vector_search(normalized_question, candidate_limit, kb_ids, clean_doc_ids)
            source_hits["vector"] = vector_result["items"]
            trace["sources"]["vector"] = {
                **self._source_trace(source_hits["vector"]),
                **vector_result.get("trace", {}),
            }

        if mode == "vector" and not source_hits.get("vector"):
            source_hits["keyword_fallback"] = self._keyword_search(normalized_question, candidate_limit, kb_ids, clean_doc_ids)
            trace["sources"]["keyword_fallback"] = self._source_trace(source_hits["keyword_fallback"])
        elif mode in {"hybrid", "graph_hybrid"} and not source_hits.get("vector"):
            trace["sources"]["keyword_fallback"] = {
                "count": 0,
                "chunk_ids": [],
                "skip_reason": "keyword_already_queried",
            }

        if mode == "graph_hybrid" and bool(cfg.get("graph_enabled", True)):
            seed_ids = self._seed_ids(source_hits)
            graph_hits = self._graph_expand_search(normalized_question, seed_ids, candidate_limit, kb_ids, clean_doc_ids)
            source_hits["graph"] = graph_hits
            trace["sources"]["graph"] = {
                **self._source_trace(graph_hits),
                "seed_count": len(seed_ids),
            }

        fused_items = self._fuse(source_hits, top_k=candidate_limit, rrf_k=int(cfg.get("rrf_k") or 60))
        # 纵深防御：融合结果再按 kb_ids / document_ids 后置过滤一次
        fused_items = self._apply_scope_post_filter(fused_items, kb_ids, clean_doc_ids)
        trace["fusion"]["candidate_count"] = len(fused_items)
        if bool(cfg.get("rerank_enabled")):
            # rerank 窗口先过滤再送 rerank，保证重排不引入越权证据
            reranked = rerank_service.rerank(normalized_question, fused_items, top_k=top_k)
            items = self._apply_scope_post_filter(reranked.get("items", fused_items[:top_k]), kb_ids, clean_doc_ids)
            trace["rerank"] = {**trace["rerank"], **(reranked.get("trace") or {})}
        else:
            items = fused_items[:top_k]
        trace["fusion"]["result_count"] = len(items)
        trace["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 3)
        return {"items": items, "trace": trace}

    @staticmethod
    def _clean_document_ids(document_ids: Optional[List[str]]) -> List[str]:
        cleaned: List[str] = []
        for item in document_ids or []:
            value = str(item or "").strip()
            if value and value not in cleaned:
                cleaned.append(value)
        return cleaned

    @staticmethod
    def _apply_scope_post_filter(
        items: List[Dict[str, Any]],
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """fail-closed 后置过滤（M4-R1 FIX #4，契约 §12.2）。

        旧逻辑是 fail-open：结果缺少 kb_id（或文档过滤时缺 doc_id）会被当作
        安全结果保留。现在改为：无法证明作用域归属的证据一律丢弃，绝不送进
        QA 上下文与 citation。上游 require_kb_scope 保证 allowed_kb 非空。
        """
        allowed_kb = {str(item) for item in kb_ids if str(item).strip()}
        allowed_docs = {str(item).strip() for item in (document_ids or []) if str(item or "").strip()}
        kept: List[Dict[str, Any]] = []
        for item in items:
            item_kb = str(item.get("kb_id") or "").strip()
            # 缺少 kb_id 或不在授权 KB 集合内 -> 丢弃（不把无作用域结果当作安全结果）。
            if not item_kb or item_kb not in allowed_kb:
                continue
            if allowed_docs:
                item_doc = str(item.get("doc_id") or "").strip()
                if not item_doc or item_doc not in allowed_docs:
                    continue
            kept.append(item)
        return kept

    def index_chunks(
        self,
        chunks: List[Dict[str, Any]],
        *,
        kb_id: str,
        tenant_id: str = "",
        project_id: str = "",
        content_revisions: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """写入 chunk 向量；kb_id 必填（无作用域禁止任何 Milvus 写入）。

        `content_revisions`（Wave 2 / P1#2）：chunk_id → 权威 content_revision 映射，
        来自 services.chunk_revision_lifecycle.write_revisions_for_build_graph。
        §8.5 要求 shadow collection 显式 INT64 `content_revision` 字段——upsert 缺该字段
        时 vector_store 会包成 DualWriteShadowError 抛出。build_graph 调用点必须先建
        revision、再传 map 进来，不允许默默写无版本向量。不传 map 时逐 chunk 保持 None
        （reindex_chunks / backfill 不走本方法，它们直接 `vector_store.upsert_chunks`
        并自带 target_revision；这里只剩测试旁路，保持原行为不炸）。

        失败口径（Wave 3 / 用户 2026-10-04 定案"结构化：逐 chunk 失败清单 + 错误类别"）：
        - `failure_details`：每个失败批次一条 `{chunk_ids, error, error_class, is_shadow}`。
          `chunk_ids` 必须完整——调用方要按 chunk 回写 `vector_status='failed'` 并把这批
          chunk 转交 reindex job，这里丢一个 id 就等于永久虚标 indexed。
        - `failed_chunk_ids`：跨批次去重排序后的完整清单（同上，供调用方直接消费）。
        - `failures`：仍保留有界的错误文本样例，只给日志/作业报错用，不再是收敛判据。
        `error_class` 取异常类名（DualWriteShadowError / VectorStoreUpsertError /
        VectorStoreSchemaError / 嵌入侧异常），`is_shadow` 单独成列：§16.1 S1 的影子脏写
        必须与普通写失败区分，否则运维无法判断要不要迁 v3 collection。
        """
        # 作用域强制点：先于任何 enabled 检查，缺失即拒绝
        from services.scope_contract import require_kb_scope

        normalized_kb_ids = require_kb_scope([kb_id])
        normalized_kb = normalized_kb_ids[0]
        if not vector_store.is_enabled():
            return {"enabled": False, "indexed": 0, "reason": "vector_store_disabled"}
        if not embedding_service.is_enabled():
            return {"enabled": True, "indexed": 0, "reason": "embedding_disabled_or_missing_key"}
        if not chunks:
            return {"enabled": True, "indexed": 0, "reason": "empty_chunks"}

        from services.vector_store import VectorChunk

        cfg = embedding_service.config()
        batch_size = int(cfg.get("batch_size") or 32)
        indexed = 0
        failures: List[str] = []
        failure_details: List[Dict[str, Any]] = []
        for offset in range(0, len(chunks), batch_size):
            window = chunks[offset : offset + batch_size]
            # 空 text 的 chunk 不进批次也不会生成向量，但它确有 chunk_id/revision：
            # 记成失败而不是静默过滤，否则调用方会把它当"无失败"回写 indexed。
            empty_ids = sorted(
                {
                    str(item.get("chunk_id") or "").strip()
                    for item in window
                    if str(item.get("chunk_id") or "").strip() and not str(item.get("text") or "").strip()
                }
            )
            if empty_ids:
                failure_details.append(
                    {
                        "chunk_ids": empty_ids,
                        "error": "chunk text 为空，未生成向量",
                        "error_class": "CHUNK_TEXT_EMPTY",
                        "is_shadow": False,
                    }
                )
            batch = [
                item
                for item in window
                if str(item.get("chunk_id") or "").strip() and str(item.get("text") or "").strip()
            ]
            if not batch:
                continue
            batch_chunk_ids = sorted({str(item.get("chunk_id") or "") for item in batch})
            try:
                vectors = embedding_service.embed_texts([str(item.get("text") or "") for item in batch])
                vector_chunks = [
                    VectorChunk(
                        chunk_id=str(item.get("chunk_id") or ""),
                        doc_id=str(item.get("doc_id") or ""),
                        text=str(item.get("text") or ""),
                        title=str(item.get("title") or ""),
                        location=str(item.get("location") or ""),
                        entities=[str(e) for e in (item.get("entities") or []) if str(e).strip()],
                        content_hash=embedding_service.content_hash(str(item.get("text") or "")),
                        embedding_model=str(cfg.get("model") or ""),
                        kb_id=normalized_kb,
                        tenant_id=str(tenant_id or ""),
                        project_id=str(project_id or ""),
                        metadata=self._chunk_vector_metadata(item),
                        content_revision=(
                            content_revisions.get(str(item.get("chunk_id") or ""))
                            if content_revisions is not None
                            else None
                        ),
                    )
                    for item in batch
                ]
                indexed += vector_store.upsert_chunks(vector_chunks, vectors)
            except Exception as exc:  # noqa: BLE001
                failures.append(str(exc))
                failure_details.append(
                    {
                        "chunk_ids": batch_chunk_ids,
                        "error": str(exc)[:300],
                        "error_class": type(exc).__name__,
                        "is_shadow": isinstance(exc, DualWriteShadowError),
                    }
                )
                logger.warning(
                    "Milvus chunk 索引写入失败",
                    context={"error": str(exc), "offset": offset, "chunk_count": len(batch_chunk_ids)},
                )
        return {
            "enabled": True,
            "indexed": indexed,
            "kb_id": normalized_kb,
            "failures": failures[:5],
            "failure_details": failure_details,
            "failed_chunk_ids": sorted({cid for item in failure_details for cid in item["chunk_ids"]}),
            "embedding_model": str(cfg.get("model") or ""),
        }

    @staticmethod
    def _chunk_vector_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}
        for key in (
            "parser_provider",
            "parser_version",
            "parse_mode",
            "block_type",
            "heading_path",
            "page_start",
            "page_end",
            "source_location",
            "document_type",
            "domain",
            "profile_version",
            "extraction_planner_version",
            "extraction_strategy",
            "extraction_use_llm",
            "extraction_priority",
            "extraction_reasons",
            "caption",
            "neighbor_before",
            "neighbor_after",
            "table_columns",
        ):
            if key in item and item.get(key) is not None:
                metadata[key] = item.get(key)
        return metadata

    def delete_doc(self, doc_id: str, kb_id: str) -> bool:
        """删除单个文档的向量（kb 必填）；返回是否删除成功。"""
        clean_doc_id = str(doc_id or "").strip()
        if not clean_doc_id:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="删除文档向量必须显式携带 doc_id",
            )
        try:
            vector_store.delete_doc(clean_doc_id, kb_id)
            return True
        except KnowledgeScopeError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "删除 Milvus 文档向量失败",
                context={"doc_id": clean_doc_id, "kb_id": kb_id, "error": str(exc)},
            )
            return False

    def clear(self, kb_ids: List[str]) -> None:
        try:
            vector_store.clear(kb_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("按知识库清理 Milvus 向量失败", context={"kb_ids": kb_ids, "error": str(exc)})

    def health(self) -> Dict[str, Any]:
        retrieval_config = self._public_retrieval_config(get_retrieval_runtime_config())
        return {
            "retrieval": retrieval_config,
            "embedding": {
                "enabled": embedding_service.is_enabled(),
                "model": embedding_service.config().get("model"),
                "dimension": embedding_service.config().get("dimension"),
            },
            "vector_store": vector_store.health(),
        }

    def _keyword_search(self, question: str, limit: int, kb_ids: List[str], document_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """全文检索：chunk 匹配强制 kb_id IN $kb_ids（M4，契约 §12.2）。"""
        service = _neo4j_service()
        items: List[Dict[str, Any]] = []
        params = {"q": self._lucene_safe_query(question), "limit": limit, "kb_ids": list(kb_ids), "doc_ids": list(document_ids or [])}
        with service.driver.session() as session:
            try:
                result = session.run(
                    Query(
                        """
                        CALL db.index.fulltext.queryNodes('chunkText', $q) YIELD node, score
                        WHERE node.kb_id IN $kb_ids AND (size($doc_ids) = 0 OR node.doc_id IN $doc_ids)
                        OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(node)
                        OPTIONAL MATCH (node)-[:MENTIONS]->(e:Entity)
                        WITH node, d, score, collect(DISTINCT e.name) AS entity_names
                        RETURN node AS c, d, score, entity_names
                        ORDER BY score DESC
                        LIMIT $limit
                        """,
                        timeout=RETRIEVAL_QUERY_TIMEOUT_SECONDS,
                    ),
                    params,
                )
                for record in result:
                    item = self._record_to_item(record)
                    if item:
                        items.append(item)
            except Exception:
                items = []
                result = session.run(
                    Query(
                        """
                        MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
                        WHERE c.kb_id IN $kb_ids
                          AND (size($doc_ids) = 0 OR c.doc_id IN $doc_ids)
                          AND c.text CONTAINS $q
                        OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
                        WITH c, d, collect(DISTINCT e.name) AS entity_names
                        RETURN c, d, 0.0 AS score, entity_names
                        LIMIT $limit
                        """,
                        timeout=RETRIEVAL_QUERY_TIMEOUT_SECONDS,
                    ),
                    params,
                )
                for record in result:
                    item = self._record_to_item(record)
                    if item:
                        items.append(item)
        return items

    def _vector_search(self, question: str, limit: int, kb_ids: List[str], document_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        trace: Dict[str, Any] = {
            "enabled": vector_store.is_enabled(),
            "embedding_enabled": embedding_service.is_enabled(),
        }
        if not vector_store.is_enabled():
            trace["skip_reason"] = "vector_store_disabled"
            return {"items": [], "trace": trace}
        if not embedding_service.is_enabled():
            trace["skip_reason"] = "embedding_disabled_or_missing_key"
            return {"items": [], "trace": trace}

        try:
            vector = embedding_service.embed_query(question)
            if not vector:
                trace["skip_reason"] = "empty_embedding"
                return {"items": [], "trace": trace}
            # 硬过滤：kb_id IN authorized_kb_ids (+ 可选 doc_id)，filter 为空即拒绝（契约 §11.2）
            from services.scope_contract import milvus_kb_filter

            filter_expr = milvus_kb_filter(kb_ids)
            clean_doc_ids = [str(item or "").strip() for item in (document_ids or []) if str(item or "").strip()]
            if clean_doc_ids:
                filter_expr += " and doc_id in [" + ", ".join(
                    f'"{vector_store._escape_filter_value(item)}"' for item in clean_doc_ids
                ) + "]"
            hits = vector_store.search(vector, limit=limit, filter_expr=filter_expr)
            trace["raw_count"] = len(hits)
            items = self._items_from_vector_hits(hits, kb_ids, clean_doc_ids)
            return {"items": items, "trace": trace}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus 向量检索失败，已回退其他检索源", context={"error": str(exc)})
            trace["error"] = str(exc)
            return {"items": [], "trace": trace}

    def _items_from_vector_hits(
        self,
        hits: List[VectorSearchHit],
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        ids = [hit.chunk_id for hit in hits if hit.chunk_id]
        neo4j_items = self._fetch_chunks_by_ids(ids, kb_ids, document_ids)
        items: List[Dict[str, Any]] = []
        for hit in hits:
            base = neo4j_items.get(hit.chunk_id) or self._item_from_vector_metadata(hit)
            if not base:
                continue
            base = {**base}
            if not base.get("kb_id"):
                base["kb_id"] = (hit.metadata or {}).get("kb_id")
            base["retrieval_score"] = self._safe_score(hit.score)
            items.append(base)
        return items

    def _graph_expand_search(
        self,
        question: str,
        seed_ids: List[str],
        limit: int,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """图谱扩展：种子 chunk / 实体 / 扩展 chunk 全部限制在 kb 作用域内。"""
        service = _neo4j_service()
        items: List[Dict[str, Any]] = []
        doc_filter = "AND (size($doc_ids) = 0 OR {var}.doc_id IN $doc_ids)"
        with service.driver.session() as session:
            if seed_ids:
                result = session.run(
                    Query(
                        f"""
                        MATCH (seed:Chunk)
                        WHERE seed.chunk_id IN $seed_ids
                          AND seed.kb_id IN $kb_ids
                          {doc_filter.format(var="seed")}
                        MATCH (seed)-[:MENTIONS]->(e:Entity)
                        WHERE e.kb_id IN $kb_ids
                        MATCH (e)<-[:MENTIONS]-(related:Chunk)
                        WHERE related.chunk_id IS NOT NULL
                          AND related.kb_id IN $kb_ids
                          AND NOT related.chunk_id IN $seed_ids
                          {doc_filter.format(var="related")}
                        OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(related)
                        WITH related, d, count(DISTINCT e) AS score, collect(DISTINCT e.name) AS entity_names
                        RETURN related AS c, d, score, entity_names
                        ORDER BY score DESC
                        LIMIT $limit
                        """,
                        timeout=RETRIEVAL_QUERY_TIMEOUT_SECONDS,
                    ),
                    {"seed_ids": seed_ids, "limit": limit, "kb_ids": list(kb_ids), "doc_ids": list(document_ids or [])},
                )
                for record in result:
                    item = self._record_to_item(record)
                    if item:
                        items.append(item)

            entity_result = session.run(
                Query(
                    f"""
                    MATCH (e:Entity)
                    WHERE e.kb_id IN $kb_ids AND size(e.name) >= 2 AND toLower($q) CONTAINS toLower(e.name)
                    MATCH (e)<-[:MENTIONS]-(c:Chunk)
                    WHERE c.kb_id IN $kb_ids {doc_filter.format(var="c")}
                    OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(c)
                    WITH c, d, count(DISTINCT e) AS score, collect(DISTINCT e.name) AS entity_names
                    RETURN c, d, score, entity_names
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    timeout=RETRIEVAL_QUERY_TIMEOUT_SECONDS,
                ),
                {"q": question, "limit": limit, "kb_ids": list(kb_ids), "doc_ids": list(document_ids or [])},
            )
            seen = {str(item.get("id") or "") for item in items}
            for record in entity_result:
                item = self._record_to_item(record)
                if item and str(item.get("id") or "") not in seen:
                    seen.add(str(item.get("id") or ""))
                    items.append(item)
        return items[:limit]

    def _fetch_chunks_by_ids(
        self,
        chunk_ids: List[str],
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        clean_ids = [str(item) for item in chunk_ids if str(item).strip()]
        if not clean_ids:
            return {}
        service = _neo4j_service()
        result_map: Dict[str, Dict[str, Any]] = {}
        with service.driver.session() as session:
            result = session.run(
                Query(
                    """
                    MATCH (c:Chunk)
                    WHERE c.chunk_id IN $ids
                      AND c.kb_id IN $kb_ids
                      AND (size($doc_ids) = 0 OR c.doc_id IN $doc_ids)
                    OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(c)
                    OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
                    WITH c, d, collect(DISTINCT e.name) AS entity_names
                    RETURN c, d, 0.0 AS score, entity_names
                    """,
                    timeout=RETRIEVAL_QUERY_TIMEOUT_SECONDS,
                ),
                {"ids": clean_ids, "kb_ids": list(kb_ids), "doc_ids": list(document_ids or [])},
            )
            for record in result:
                item = self._record_to_item(record)
                if item:
                    result_map[str(item["id"])] = item
        return result_map

    def _record_to_item(self, record: Any) -> Optional[Dict[str, Any]]:
        chunk = record.get("c")
        doc = record.get("d")
        if not chunk:
            return None
        chunk_props = dict(chunk)
        doc_props = dict(doc) if doc else {}
        text = chunk_props.get("text", "") or ""
        index = chunk_props.get("index")
        raw_entities = record.get("entity_names") or []
        entity_names = sorted(
            {
                str(name).strip()
                for name in raw_entities
                if isinstance(name, str) and str(name).strip()
            }
        )
        return {
            "id": str(chunk_props.get("chunk_id") or chunk.id),
            "chunk_id": str(chunk_props.get("chunk_id") or chunk.id),
            "kb_id": str(chunk_props.get("kb_id") or "") or None,
            "doc_id": chunk_props.get("doc_id") or doc_props.get("doc_id"),
            "content_revision": chunk_props.get("content_revision"),
            "title": doc_props.get("name") or "文档片段",
            "location": f"Chunk {index}" if index is not None else None,
            "text": text,
            "snippet": text[:160].strip() if text else "",
            "entity_names": entity_names,
            "retrieval_score": self._safe_score(record.get("score")),
        }

    def _item_from_vector_metadata(self, hit: VectorSearchHit) -> Optional[Dict[str, Any]]:
        metadata = hit.metadata or {}
        text = str(metadata.get("text") or "")
        entities: List[str] = []
        raw_entities = metadata.get("entities_json")
        if isinstance(raw_entities, str) and raw_entities.strip():
            try:
                parsed = json.loads(raw_entities)
                if isinstance(parsed, list):
                    entities = [str(item) for item in parsed if str(item).strip()]
            except Exception:
                entities = []
        return {
            "id": hit.chunk_id,
            "chunk_id": hit.chunk_id,
            "kb_id": str(metadata.get("kb_id") or "") or None,
            "doc_id": metadata.get("doc_id"),
            "content_revision": metadata.get("content_revision"),
            "title": str(metadata.get("title") or "文档片段"),
            "location": str(metadata.get("location") or "") or None,
            "text": text,
            "snippet": text[:160].strip() if text else "",
            "entity_names": entities,
            "retrieval_score": self._safe_score(hit.score),
        }

    def _fuse(self, source_hits: Dict[str, List[Dict[str, Any]]], *, top_k: int, rrf_k: int) -> List[Dict[str, Any]]:
        weights = {
            "keyword": 1.0,
            "keyword_fallback": 0.8,
            "vector": 1.0,
            "graph": 0.7,
        }
        candidates: Dict[str, Candidate] = {}
        for source, hits in source_hits.items():
            weight = weights.get(source, 1.0)
            seen_in_source = set()
            for rank, item in enumerate(hits, start=1):
                chunk_id = str(item.get("id") or "").strip()
                if not chunk_id or chunk_id in seen_in_source:
                    continue
                seen_in_source.add(chunk_id)
                candidate = candidates.get(chunk_id)
                if not candidate:
                    candidate = Candidate(chunk_id=chunk_id, item={**item})
                    candidates[chunk_id] = candidate
                candidate.ranks[source] = rank
                source_score = item.get("retrieval_score")
                if source_score is not None:
                    candidate.source_scores[source] = float(source_score)
                candidate.fused_score += weight / (rrf_k + rank)

        if not candidates:
            return []

        ranked = sorted(candidates.values(), key=lambda item: item.fused_score, reverse=True)
        max_score = ranked[0].fused_score if ranked else 1.0
        items: List[Dict[str, Any]] = []
        for idx, candidate in enumerate(ranked[:top_k], start=1):
            item = {**candidate.item}
            item["retrieval_score"] = round(candidate.fused_score / max_score, 3) if max_score else 0.0
            item["retrieval_sources"] = sorted(candidate.ranks.keys())
            item["retrieval_ranks"] = candidate.ranks
            item["source_scores"] = candidate.source_scores
            item["retrieval_rank"] = idx
            items.append(item)
        return items

    @staticmethod
    def _source_trace(items: List[Dict[str, Any]], limit: int = 10) -> Dict[str, Any]:
        return {
            "count": len(items),
            "chunk_ids": [str(item.get("id") or "") for item in items[:limit]],
        }

    @staticmethod
    def _diagnostic_item(item: Dict[str, Any]) -> Dict[str, Any]:
        text = str(item.get("text") or item.get("snippet") or "")
        return {
            "id": str(item.get("id") or ""),
            "kb_id": item.get("kb_id"),
            "doc_id": item.get("doc_id"),
            "content_revision": item.get("content_revision"),
            "title": str(item.get("title") or ""),
            "location": item.get("location"),
            "retrieval_score": item.get("retrieval_score"),
            "retrieval_rank": item.get("retrieval_rank"),
            "retrieval_sources": item.get("retrieval_sources") or [],
            "retrieval_ranks": item.get("retrieval_ranks") or {},
            "source_scores": item.get("source_scores") or {},
            "entity_names": item.get("entity_names") or [],
            "snippet": text[:240],
        }

    @staticmethod
    def _diagnostic_summary(runs: Dict[str, Any]) -> Dict[str, Any]:
        modes: Dict[str, Any] = {}
        best_mode: Optional[str] = None
        best_hit_count = -1
        slowest_mode: Optional[str] = None
        slowest_duration = -1.0
        skipped_sources: Dict[str, List[str]] = {}

        for mode, run in runs.items():
            items = run.get("items") or []
            trace = run.get("trace") or {}
            sources = trace.get("sources") or {}
            hit_count = len(items)
            duration_ms = RetrievalOrchestrator._safe_float(trace.get("duration_ms"))
            mode_skips: List[str] = []
            source_counts: Dict[str, int] = {}
            for source, source_trace in sources.items():
                if not isinstance(source_trace, dict):
                    continue
                source_counts[source] = int(source_trace.get("count") or 0)
                skip_reason = str(source_trace.get("skip_reason") or "").strip()
                if skip_reason:
                    mode_skips.append(f"{source}:{skip_reason}")
            if hit_count > best_hit_count:
                best_mode = mode
                best_hit_count = hit_count
            if duration_ms > slowest_duration:
                slowest_mode = mode
                slowest_duration = duration_ms
            if mode_skips:
                skipped_sources[mode] = mode_skips
            modes[mode] = {
                "hit_count": hit_count,
                "duration_ms": duration_ms,
                "source_counts": source_counts,
                "skipped_sources": mode_skips,
            }

        return {
            "modes": modes,
            "best_mode": best_mode,
            "slowest_mode": slowest_mode,
            "skipped_sources": skipped_sources,
            "recommendations": RetrievalOrchestrator._diagnostic_recommendations(modes, skipped_sources),
        }

    @staticmethod
    def _diagnostic_recommendations(modes: Dict[str, Any], skipped_sources: Dict[str, List[str]]) -> List[str]:
        recommendations: List[str] = []
        flattened_skips = {reason for reasons in skipped_sources.values() for reason in reasons}
        if any("vector:vector_store_disabled" in reason for reason in flattened_skips):
            recommendations.append("enable_vector_store")
        if any("vector:embedding_disabled_or_missing_key" in reason for reason in flattened_skips):
            recommendations.append("configure_embedding")
        if modes and all(int(item.get("hit_count") or 0) == 0 for item in modes.values()):
            recommendations.append("reindex_or_import_documents")
        if "graph_hybrid" in modes and int((modes["graph_hybrid"].get("source_counts") or {}).get("graph") or 0) == 0:
            recommendations.append("verify_graph_mentions")
        return recommendations

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return round(float(value), 3)
        except Exception:
            return 0.0

    @staticmethod
    def _seed_ids(source_hits: Dict[str, List[Dict[str, Any]]]) -> List[str]:
        ids: List[str] = []
        seen = set()
        for source in ("keyword", "vector"):
            for item in source_hits.get(source, [])[:10]:
                chunk_id = str(item.get("id") or "").strip()
                if chunk_id and chunk_id not in seen:
                    seen.add(chunk_id)
                    ids.append(chunk_id)
        return ids

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        normalized = (mode or "").strip().lower()
        if normalized in {"keyword", "vector", "hybrid", "graph_hybrid"}:
            return normalized
        return "keyword"

    @staticmethod
    def _public_retrieval_config(config: Dict[str, Any]) -> Dict[str, Any]:
        public = {key: value for key, value in config.items() if key != "rerank_api_key"}
        public["rerank_api_key_configured"] = bool(str(config.get("rerank_api_key") or "").strip())
        return public

    @staticmethod
    def _lucene_safe_query(question: str) -> str:
        text = str(question or "").strip()
        if not text:
            return ""
        text = re.sub(r"&&|\|\|", " ", text)
        text = re.sub(r"[+\\\-!(){}\[\]^\"~*?:/]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:500]

    @staticmethod
    def _safe_score(value: Any) -> Optional[float]:
        try:
            score = float(value)
        except Exception:
            return None
        if score < 0:
            score = 0.0
        if score > 1:
            score = 1.0
        return round(score, 3)


retrieval_orchestrator = RetrievalOrchestrator()


__all__ = ["RetrievalOrchestrator", "retrieval_orchestrator"]
