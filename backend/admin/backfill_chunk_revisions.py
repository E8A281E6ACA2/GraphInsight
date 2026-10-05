#!/usr/bin/env python3
"""
chunk_revisions 存量 backfill（M5-A，设计 §9 / §15.3 / §16.2 / §17.1 v3.2.1 冻结契约）

口径（全文只保留一套规范，冲突以设计文档 v3.2.1 为准）：
1. `--kb` 必填，只处理该知识库，禁止全局扫描（§4 作用域纪律）。
2. inventory 三集合：new_chunks（无任何 revision 行）/ needs_reindex_targets
   （已有 revision 行且能力已配置但投影缺失或版本不一致）/ converged。
3. 仅 new_chunks 允许创建 revision 1 并补写索引侧 `content_revision=1`；
   已有 revision 行的 chunk 禁止降级或覆盖索引（§17.1）。
4. needs_reindex_targets 生成 current revision 的 reindex targets 入队
   admin_jobs（targets_hash ON CONFLICT 幂等），未收敛不关闭前置门（§15.3 步骤 7）；
   入队清单取写入后重新读取的 inventory，同一轮新写入但未收敛的 chunk 也必须排队，
   禁止按决策时清单入队造成"报告判为 needs_reindex 却无 job"的静默漏排。
5. v3.2.1 前置门：indexed 必须 `*_content_revision == current.content_revision`；
   skipped 必须"能力未配置且 *_content_revision IS NULL"，允许关闭技术迁移门
   但必须输出 DEGRADED_SKIPPED，不得宣布完整索引验收通过；
   failed/stale/pending 继续阻断前置门。
6. UNRECOVERABLE_MISMATCH 严格口径（审计修复 #2）：chunk 无解析产物（无
   chunks.jsonl 条目或条目无文本）且 Milvus 无记录 = 不可恢复，终止 backfill、
   零写入，走 §9 备选受控清库；仅 Neo4j 有文本不视为可恢复证据。
7. inventory 必须纳入该 kb 全部 current revision 行（审计修复 #1）：universe 取
   证据源与 PG current 行的并集；PG 有行但 Neo4j/Milvus 两侧索引均无该 chunk 的
   孤儿 revision 显式计为 blocked（orphan_revisions），禁止静默跳过或判为收敛。
8. 写入前作用域一致性校验（审计修复 #4，fail-closed）：KB 登记作用域
   （knowledge_bases）为权威，与各证据源/已有 revision 行的 tenant/project
   交叉比对；任一冲突输出 SCOPE_MISMATCH 明细、零写入、退出码 2。
9. SCOPE_UNRESOLVED 与 UNRECOVERABLE_MISMATCH 分列（活栈实测纠偏）：new chunk 的
   doc/tenant/project 三件套不全（KB 未登记且索引侧无证据）时内容仍可恢复，
   单独计数并零写入拒绝，处置是补登记或 reindex 重建，不得伪造作用域落库。
10. Milvus collection 名与线上读写路径共用一份解析（活栈实测纠偏）：runtime 配置
    可能仍是历史名 graphinsight_chunks，真实 collection 由 services.vector_store
    归一化为 graphinsight_chunks_v2；backfill 自行读原始配置会查不存在的库，
    把"有向量"错报成"字段缺失"。
11. 全流程幂等、可 dry-run、可中断重跑：PG ON CONFLICT DO NOTHING /
    Neo4j MERGE SET / Milvus upsert 均幂等。

用法：
    python backend/admin/backfill_chunk_revisions.py --kb <kb_id> --dry-run
    python backend/admin/backfill_chunk_revisions.py --kb <kb_id>

退出码：
    0 = 成功（dry-run，或执行且前置门 CLOSED / CLOSED_DEGRADED）
    1 = 执行错误
    2 = 拒绝执行（chunk_revisions/admin_jobs 表缺失、UNRECOVERABLE_MISMATCH、
        SCOPE_MISMATCH、SCOPE_UNRESOLVED、kb_id 非法）
    3 = 执行完成但前置门 OPEN（needs_reindex targets 待收敛 / blocked 投影）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import text

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from admin.database import engine  # noqa: E402
from admin.dry_run_contract import emit_dry_run_result  # noqa: E402
from config import get_settings  # noqa: E402
from services.scope_contract import milvus_kb_filter, normalize_scope_id  # noqa: E402

load_dotenv(find_dotenv(), override=True)

settings = get_settings()

JOB_TYPE = "reindex_chunks"
MILVUS_QUERY_LIMIT = 16384
MAX_MILVUS_QUERY_PASSES = 64


@dataclass
class ChunkPlan:
    """new_chunks 的 backfill 计划（无任何 revision 行的存量 chunk）。"""

    chunk_id: str
    doc_id: str
    tenant_id: str
    project_id: str
    source_content: str
    source_version: str = ""
    parser_version: str = ""
    in_neo4j: bool = False
    in_milvus: bool = False
    content_mismatch: bool = False


@dataclass
class Inventory:
    kb_id: str
    new_chunks: List[ChunkPlan] = field(default_factory=list)
    needs_reindex_targets: List[Dict[str, Any]] = field(default_factory=list)
    converged: int = 0
    blocked: int = 0
    blocked_targets: List[Dict[str, Any]] = field(default_factory=list)
    orphan_revisions: List[str] = field(default_factory=list)
    rows_skipped_existing: int = 0
    scope_mismatches: List[Dict[str, str]] = field(default_factory=list)
    kb_scope_missing: bool = False
    degraded_skipped: Dict[str, int] = field(default_factory=lambda: {"graph": 0, "vector": 0})
    content_mismatch: int = 0
    unrecoverable: List[str] = field(default_factory=list)
    scope_unresolved: List[str] = field(default_factory=list)
    neo4j_count: int = 0
    milvus_count: int = 0
    parsed_count: int = 0
    overlap_count: int = 0
    revision_row_count: int = 0
    graph_enabled: bool = False
    vector_enabled: bool = False
    milvus_revision_field: bool = False
    milvus_collection: str = ""


# ---------------------------------------------------------------------------
# 能力判定（§15.3：graph = LLM_ENABLED；vector = embedding 配置且 vector_store 开启）
# ---------------------------------------------------------------------------


def _graph_capability_enabled() -> bool:
    from services.runtime_config import get_projection_capabilities

    return bool(get_projection_capabilities()["graph"])


def _vector_capability_enabled() -> bool:
    from services.runtime_config import get_projection_capabilities

    return bool(get_projection_capabilities()["vector"])


def _sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 证据加载（外部依赖封装为模块级函数，测试以 patch 替换）
# ---------------------------------------------------------------------------


def _table_exists(conn, table: str) -> bool:
    if engine.dialect.name == "postgresql":
        return bool(
            conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = :name AND table_schema = CURRENT_SCHEMA()"
                ),
                {"name": table},
            ).scalar()
        )
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).fetchall()
    return bool(rows)


def _load_neo4j_chunks(kb_id: str) -> Dict[str, Dict[str, Any]]:
    """按 kb_id 限定读取存量 Chunk 节点；禁止全局扫描。"""
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            records = session.run(
                """
                MATCH (c:Chunk) WHERE c.kb_id = $kb_id
                RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text,
                       c.tenant_id AS tenant_id, c.project_id AS project_id,
                       c.parser_version AS parser_version, c.content_revision AS content_revision
                """,
                {"kb_id": kb_id},
            )
            chunks: Dict[str, Dict[str, Any]] = {}
            for record in records:
                chunk_id = str(record["chunk_id"] or "")
                if not chunk_id:
                    continue
                chunks[chunk_id] = {
                    "doc_id": str(record["doc_id"] or ""),
                    "text": str(record["text"] or ""),
                    "tenant_id": str(record["tenant_id"] or ""),
                    "project_id": str(record["project_id"] or ""),
                    "parser_version": str(record["parser_version"] or ""),
                    "content_revision": record["content_revision"],
                }
            return chunks
    finally:
        driver.close()


def _backfill_neo4j(kb_id: str, plans: List[ChunkPlan]) -> Dict[str, str]:
    """仅对本次新生成 revision 1 行的 chunk 补写 content_revision=1（§15.3 步骤 3，MERGE 幂等）。

    返回 {chunk_id: indexed|failed}，投影状态逐 chunk 判定，不用总数猜测。
    """
    outcome: Dict[str, str] = {}
    if not plans:
        return outcome
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            for chunk in _batched(plans, 50):
                chunk_ids = [item.chunk_id for item in chunk]
                rows = [
                    {
                        "chunk_id": item.chunk_id,
                        "doc_id": item.doc_id,
                        "tenant_id": item.tenant_id,
                        "project_id": item.project_id,
                        "text": item.source_content,
                    }
                    for item in chunk
                ]
                try:
                    result = session.run(
                        """
                        UNWIND $rows AS r
                        MERGE (c:Chunk {chunk_id: r.chunk_id, kb_id: $kb_id})
                        ON CREATE SET c.doc_id = r.doc_id, c.tenant_id = r.tenant_id,
                            c.project_id = r.project_id, c.source = 'document_ingest'
                        SET c.text = r.text, c.content_revision = 1
                        RETURN count(c) AS updated
                        """,
                        {"kb_id": kb_id, "rows": rows},
                    ).single()
                    updated = int(result["updated"]) if result else 0
                except Exception:  # noqa: BLE001 - 单批失败逐 chunk 记 failed，不伪装的 indexed
                    for chunk_id in chunk_ids:
                        outcome[chunk_id] = "failed"
                    continue
                if updated == len(rows):
                    for chunk_id in chunk_ids:
                        outcome[chunk_id] = "indexed"
                else:
                    # 数量不一致时保守判 failed，由 reindex 前置门收敛，不虚报
                    for chunk_id in chunk_ids:
                        outcome[chunk_id] = "failed"
        return outcome
    finally:
        driver.close()


def _milvus_collection_name() -> str:
    """collection 名必须与线上向量读写路径用同一份解析逻辑。

    活栈实测（dev Milvus）：runtime 配置里存的是历史名 graphinsight_chunks，真实
    collection 是 services.vector_store 按契约 §11.1/决策 D3 归一化出来的
    graphinsight_chunks_v2。backfill 若自己读原始配置就会去查一个不存在的
    collection，把"向量其实存在"误判成"字段缺失/无向量"，属于口径性错报。
    """
    try:
        from services.vector_store import vector_store

        name = str(vector_store.config().get("collection") or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001 - 服务不可导入时退回配置直读
        pass
    try:
        from services.runtime_config import get_vector_store_runtime_config

        name = str(get_vector_store_runtime_config().get("collection") or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    return name or str(getattr(settings, "milvus_collection", "graphinsight_chunks") or "graphinsight_chunks")


def _milvus_client():
    from pymilvus import MilvusClient

    client = MilvusClient(
        uri=settings.milvus_uri,
        token=settings.milvus_token or "",
        db_name=settings.milvus_db_name,
    )
    return client, _milvus_collection_name()


def _milvus_has_revision_field(client, collection: str) -> bool:
    from services.vector_store import content_revision_field_is_int64

    return content_revision_field_is_int64(client, collection)


MILVUS_BASE_OUTPUT_FIELDS = ["chunk_id", "doc_id", "text", "tenant_id", "project_id", "parser_version"]


def _milvus_query_output_fields(client, collection: str) -> Optional[List[str]]:
    """按 collection 实际 schema 动态构造 query output_fields（审计修复 #3）。

    返回 None 表示 schema 不可探测（保持全字段请求）；返回集合实际字段子集，
    避免 v2 collection 缺 content_revision/parser_version 时真实 query 直接报错。
    """
    try:
        description = client.describe_collection(collection)
    except Exception:
        return None
    fields = description.get("fields") if isinstance(description, dict) else None
    if not isinstance(fields, list):
        return None
    names = {f.get("name") for f in fields if isinstance(f, dict)}
    if not names:
        return None
    output = [f for f in MILVUS_BASE_OUTPUT_FIELDS if f in names]
    if "content_revision" in names:
        output.append("content_revision")
    return output


def _load_milvus_chunks(kb_id: str) -> Dict[str, Dict[str, Any]]:
    from services.runtime_config import get_vector_store_runtime_config

    store = get_vector_store_runtime_config()
    if not bool(store.get("enabled")):
        return {}
    client, cfg_collection = _milvus_client()
    if not client.has_collection(cfg_collection):
        return {}
    output_fields = _milvus_query_output_fields(client, cfg_collection)
    if output_fields is None:
        output_fields = MILVUS_BASE_OUTPUT_FIELDS + ["content_revision"]
    filter_expr = milvus_kb_filter([kb_id])
    rows: Dict[str, Dict[str, Any]] = {}
    offset = 0
    for _ in range(MAX_MILVUS_QUERY_PASSES):
        page = client.query(
            collection_name=cfg_collection,
            filter=filter_expr,
            output_fields=output_fields,
            limit=MILVUS_QUERY_LIMIT,
            offset=offset,
        )
        page = list(page or [])
        for row in page:
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            rows[chunk_id] = {
                "doc_id": str(row.get("doc_id") or ""),
                "text": str(row.get("text") or ""),
                "tenant_id": str(row.get("tenant_id") or ""),
                "project_id": str(row.get("project_id") or ""),
                "parser_version": str(row.get("parser_version") or ""),
                "content_revision": row.get("content_revision"),
            }
        if len(page) < MILVUS_QUERY_LIMIT:
            break
        offset += MILVUS_QUERY_LIMIT
    return rows


def _backfill_milvus(kb_id: str, plans: List[ChunkPlan]) -> Dict[str, str]:
    """仅对新 backfill 的 chunk embedding + upsert（显式 content_revision=1，§15.3 步骤 4）。

    返回 {chunk_id: indexed|skipped|revision_field_absent|failed}，逐 chunk 判定。
    """
    outcome: Dict[str, str] = {}
    if not plans:
        return outcome
    client, collection = _milvus_client()
    if not client.has_collection(collection):
        return {item.chunk_id: "skipped" for item in plans}
    if not _milvus_has_revision_field(client, collection):
        # §8.5：v2 无显式 content_revision 字段，禁止改 schema；不写不可靠版本，投影保持 pending。
        return {item.chunk_id: "revision_field_absent" for item in plans}
    from services.embedding_service import embedding_service

    for chunk in _batched(plans, 10):
        texts = [item.source_content for item in chunk]
        try:
            vectors = embedding_service.embed_texts(texts)
        except Exception:  # noqa: BLE001 - embedding 失败逐 chunk 记 failed，不伪装 indexed
            for item in chunk:
                outcome[item.chunk_id] = "failed"
            continue
        if len(vectors) != len(chunk):
            for item in chunk:
                outcome[item.chunk_id] = "failed"
            continue
        rows = [
            {
                "chunk_id": item.chunk_id,
                "doc_id": item.doc_id,
                "kb_id": kb_id,
                "tenant_id": item.tenant_id,
                "project_id": item.project_id,
                "text": (item.source_content or "")[:4096],
                "content_revision": 1,
                "vector": vector,
            }
            for item, vector in zip(chunk, vectors)
        ]
        try:
            from services.vector_store import require_upsert_count

            require_upsert_count(client.upsert(collection_name=collection, data=rows), len(rows))
        except Exception:  # noqa: BLE001 - upsert 失败逐 chunk 记 failed
            for item in chunk:
                outcome[item.chunk_id] = "failed"
            continue
        for item in chunk:
            outcome[item.chunk_id] = "indexed"
    return outcome


def _load_parsed_chunks(kb_id: str) -> Dict[str, Dict[str, Any]]:
    """解析产物是 backfill 的权威内容源（§16.2 内容不一致以 chunks.jsonl 为准）。"""
    root = Path(settings.parsed_document_storage_path) / kb_id
    chunks: Dict[str, Dict[str, Any]] = {}
    if not root.exists():
        return chunks
    for doc_dir in sorted(root.iterdir()):
        if not doc_dir.is_dir():
            continue
        doc_id = doc_dir.name
        manifest: Dict[str, Any] = {}
        manifest_path = doc_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                manifest = {}
        chunks_path = doc_dir / "chunks.jsonl"
        if not chunks_path.exists():
            continue
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            chunks[chunk_id] = {
                "doc_id": str(row.get("doc_id") or doc_id),
                "text": str(row.get("text") or ""),
                "parser_version": str(manifest.get("parser_version") or row.get("parser_version") or ""),
                "source_version": str(manifest.get("content_hash") or ""),
            }
    return chunks


def _load_kb_scope(kb_id: str) -> Dict[str, str]:
    """tenant/project 兜底：knowledge_bases 表（Neo4j/Milvus 属性缺失时使用）。"""
    with engine.begin() as conn:
        if not _table_exists(conn, "knowledge_bases"):
            return {}
        row = conn.execute(
            text("SELECT tenant_id, project_id FROM knowledge_bases WHERE id = :kb_id"),
            {"kb_id": kb_id},
        ).fetchone()
    if not row:
        return {}
    return {"tenant_id": str(row[0] or ""), "project_id": str(row[1] or "")}


def _load_current_revisions(kb_id: str) -> Dict[str, Dict[str, Any]]:
    with engine.begin() as conn:
        if not _table_exists(conn, "chunk_revisions"):
            raise RuntimeError("chunk_revisions 表不存在，请先执行 migrate_chunk_revisions.py")
        rows = conn.execute(
            text(
                "SELECT chunk_id, doc_id, tenant_id, project_id, content_revision, graph_status, vector_status, "
                "graph_content_revision, vector_content_revision "
                "FROM chunk_revisions WHERE kb_id = :kb_id AND revision_status = 'current'"
            ),
            {"kb_id": kb_id},
        ).fetchall()
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        result[str(row[0])] = {
            "chunk_id": str(row[0]),
            "doc_id": str(row[1] or ""),
            "tenant_id": str(row[2] or ""),
            "project_id": str(row[3] or ""),
            "content_revision": int(row[4]),
            "graph_status": str(row[5] or ""),
            "vector_status": str(row[6] or ""),
            "graph_content_revision": None if row[7] is None else int(row[7]),
            "vector_content_revision": None if row[8] is None else int(row[8]),
        }
    return result


def _batched(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# inventory 分类（§15.3 步骤 1）
# ---------------------------------------------------------------------------


def _add_scope_mismatch(inventory: Inventory, chunk_id: str, field: str, expected: str, actual: str) -> None:
    inventory.scope_mismatches.append(
        {"chunk_id": chunk_id, "field": field, "expected": expected, "actual": actual}
    )


def _check_row_scope(inventory: Inventory, kb_scope: Dict[str, str], chunk_id: str, row: Dict[str, Any], evidences: List[Dict[str, Any]]) -> None:
    """已有 revision 行 vs KB 登记 vs 各证据源：tenant/project 交叉一致性（审计修复 #4）。"""
    kb_tenant = str(kb_scope.get("tenant_id") or "")
    kb_project = str(kb_scope.get("project_id") or "")
    for field, row_value in (("tenant_id", str(row.get("tenant_id") or "")), ("project_id", str(row.get("project_id") or ""))):
        kb_value = kb_tenant if field == "tenant_id" else kb_project
        if kb_value and row_value and kb_value != row_value:
            _add_scope_mismatch(inventory, chunk_id, f"revision.{field}", kb_value, row_value)
        for evidence in evidences:
            source_value = str(evidence.get(field) or "")
            if row_value and source_value and row_value != source_value:
                _add_scope_mismatch(inventory, chunk_id, f"revision.{field}", row_value, source_value)


def _check_plan_scope(inventory: Inventory, kb_scope: Dict[str, str], plan: ChunkPlan, sources: List[Dict[str, Any]]) -> None:
    """new_chunk 推导作用域 vs KB 登记 vs 各证据源交叉一致性（审计修复 #4）。"""
    kb_tenant = str(kb_scope.get("tenant_id") or "")
    kb_project = str(kb_scope.get("project_id") or "")
    if kb_tenant and plan.tenant_id and kb_tenant != plan.tenant_id:
        _add_scope_mismatch(inventory, plan.chunk_id, "tenant_id", kb_tenant, plan.tenant_id)
    if kb_project and plan.project_id and kb_project != plan.project_id:
        _add_scope_mismatch(inventory, plan.chunk_id, "project_id", kb_project, plan.project_id)
    for field, plan_value in (("tenant_id", plan.tenant_id), ("project_id", plan.project_id)):
        for source in sources:
            source_value = str(source.get(field) or "")
            if plan_value and source_value and plan_value != source_value:
                _add_scope_mismatch(inventory, plan.chunk_id, field, plan_value, source_value)


def _has_recoverable_evidence(parsed: Optional[Dict[str, Any]], mil: Optional[Dict[str, Any]]) -> bool:
    """严格 UNRECOVERABLE 口径（审计修复 #2）：仅解析产物或 Milvus 记录算可恢复证据。"""
    if parsed is not None and str(parsed.get("text") or "").strip():
        return True
    return mil is not None


def _classify_projection(
    row: Dict[str, Any],
    side: str,
    capability_enabled: bool,
    evidence: Optional[Dict[str, Any]],
) -> str:
    """单投影收敛判定：ok / degraded / needs_reindex / blocked（v3.2.1）。"""
    status = row.get(f"{side}_status") or ""
    projection_rev = row.get(f"{side}_content_revision")
    current_rev = row["content_revision"]
    evidence_rev = evidence.get("content_revision") if evidence else None
    present = evidence is not None

    if status == "indexed":
        if projection_rev == current_rev and present and evidence_rev == current_rev:
            return "ok"
        return "needs_reindex" if capability_enabled else "blocked"
    if status == "skipped":
        if not capability_enabled and projection_rev is None:
            return "degraded"
        if capability_enabled:
            return "needs_reindex"
        return "blocked"
    # pending / stale / failed：能力已配置可由 reindex 收敛，否则阻断前置门
    if capability_enabled:
        return "needs_reindex"
    return "blocked"


def build_inventory(kb_id: str) -> Inventory:
    neo4j_chunks = _load_neo4j_chunks(kb_id)
    milvus_chunks = _load_milvus_chunks(kb_id)
    parsed_chunks = _load_parsed_chunks(kb_id)
    existing = _load_current_revisions(kb_id)
    kb_scope = _load_kb_scope(kb_id)

    inventory = Inventory(
        kb_id=kb_id,
        neo4j_count=len(neo4j_chunks),
        milvus_count=len(milvus_chunks),
        parsed_count=len(parsed_chunks),
        overlap_count=len(set(neo4j_chunks) & set(milvus_chunks)),
        revision_row_count=len(existing),
        graph_enabled=_graph_capability_enabled(),
        vector_enabled=_vector_capability_enabled(),
        kb_scope_missing=not kb_scope,
    )
    if inventory.vector_enabled:
        try:
            client, collection = _milvus_client()
            inventory.milvus_collection = collection
            inventory.milvus_revision_field = _milvus_has_revision_field(client, collection)
        except Exception:
            inventory.milvus_revision_field = False

    universe = sorted(set(neo4j_chunks) | set(milvus_chunks) | set(parsed_chunks) | set(existing))
    for chunk_id in universe:
        neo = neo4j_chunks.get(chunk_id)
        mil = milvus_chunks.get(chunk_id)
        parsed = parsed_chunks.get(chunk_id)

        # kb_id 归属防御校验：任何证据/行不属于本 KB 一律视为串数据（fail-closed）
        for label, item in (("neo4j", neo), ("milvus", mil), ("revision", existing.get(chunk_id))):
            if item and str(item.get("kb_id") or "") not in ("", kb_id):
                _add_scope_mismatch(inventory, chunk_id, f"{label}.kb_id", kb_id, str(item.get("kb_id")))

        row = existing.get(chunk_id)
        if row is not None:
            _check_row_scope(
                inventory, kb_scope, chunk_id, row,
                [item for item in (neo, mil, parsed) if item],
            )
            graph_state = _classify_projection(row, "graph", inventory.graph_enabled, neo)
            vector_state = _classify_projection(row, "vector", inventory.vector_enabled, mil)
            is_orphan = neo is None and mil is None
            if is_orphan:
                inventory.orphan_revisions.append(chunk_id)
            inventory.rows_skipped_existing += 1
            for side, state in (("graph", graph_state), ("vector", vector_state)):
                if state == "degraded":
                    inventory.degraded_skipped[side] += 1
            if "needs_reindex" in (graph_state, vector_state):
                inventory.needs_reindex_targets.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": row["doc_id"],
                        "tenant_id": row["tenant_id"] or kb_scope.get("tenant_id", ""),
                        "project_id": row["project_id"] or kb_scope.get("project_id", ""),
                        "target_revision": row["content_revision"],
                        "graph_state": graph_state,
                        "vector_state": vector_state,
                    }
                )
            elif "blocked" in (graph_state, vector_state):
                inventory.blocked += 1
                inventory.blocked_targets.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": row["doc_id"],
                        "reason": "PROJECTION_BLOCKED",
                        "graph_state": graph_state,
                        "vector_state": vector_state,
                    }
                )
            elif is_orphan:
                # 孤儿行不得判为 converged：两侧索引都没有该 chunk，门必须保持 OPEN
                inventory.blocked += 1
                inventory.blocked_targets.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": row["doc_id"],
                        "reason": "ORPHAN_REVISION",
                        "graph_state": graph_state,
                        "vector_state": vector_state,
                    }
                )
            else:
                inventory.converged += 1
            continue

        # new_chunks：无任何 revision 行
        if not _has_recoverable_evidence(parsed, mil):
            # 严格 UNRECOVERABLE 口径（审计修复 #2）：无解析产物且 Milvus 无记录即不可恢复，
            # 仅 Neo4j 残留文本不足以作为可恢复证据（无法还原 doc 归属与版本）。
            inventory.unrecoverable.append(chunk_id)
            continue
        texts = {
            str(item.get("text") or "").strip()
            for item in (parsed, neo, mil)
            if item and str(item.get("text") or "").strip()
        }
        authoritative = parsed.get("text") if parsed and parsed.get("text") else (
            (neo or {}).get("text") or (mil or {}).get("text") or ""
        )
        doc_id = str(
            (parsed or {}).get("doc_id")
            or (neo or {}).get("doc_id")
            or (mil or {}).get("doc_id")
            or ""
        )
        tenant_id = str((neo or {}).get("tenant_id") or (mil or {}).get("tenant_id") or kb_scope.get("tenant_id", ""))
        project_id = str(
            (neo or {}).get("project_id") or (mil or {}).get("project_id") or kb_scope.get("project_id", "")
        )
        if not doc_id or not tenant_id or not project_id:
            # 作用域三件套缺失（活栈实测：KB 未登记 knowledge_bases 且索引侧无证据）。
            # 与 §16.2 的 UNRECOVERABLE_MISMATCH 分开计数：内容可恢复但作用域无从确定，
            # 处置是补登记/重建，而不是清库；同样 fail-closed 零写入。
            inventory.scope_unresolved.append(chunk_id)
            continue
        plan = ChunkPlan(
            chunk_id=chunk_id,
            doc_id=doc_id,
            tenant_id=tenant_id,
            project_id=project_id,
            source_content=str(authoritative),
            source_version=str((parsed or {}).get("source_version") or ""),
            parser_version=str((parsed or {}).get("parser_version") or (neo or {}).get("parser_version") or ""),
            in_neo4j=neo is not None,
            in_milvus=mil is not None,
            content_mismatch=len(texts) > 1,
        )
        if plan.content_mismatch:
            inventory.content_mismatch += 1
        _check_plan_scope(
            inventory, kb_scope, plan,
            [item for item in (neo, mil) if item],
        )
        inventory.new_chunks.append(plan)
    return inventory


# ---------------------------------------------------------------------------
# 写入阶段（§15.3 步骤 2-5、步骤 7 入队）
# ---------------------------------------------------------------------------


def _insert_revision_rows(kb_id: str, plans: List[ChunkPlan], trace_id: str) -> Dict[str, Any]:
    """插入 revision 1 行（ON CONFLICT DO NOTHING 幂等）；返回本次真正新插入的 chunk_id 集合。"""
    inserted_ids: List[str] = []
    existing = 0
    with engine.begin() as conn:
        for plan in plans:
            content_hash = _sha256(plan.source_content)
            result = conn.execute(
                text(
                    "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                    "source_content, source_content_hash, content, content_hash, content_revision, "
                    "revision_status, graph_status, vector_status, revision_source, "
                    "source_version, parser_version, reason, trace_id) "
                    "VALUES (:kb_id, :tenant_id, :project_id, :doc_id, :chunk_id, "
                    ":source_content, :source_content_hash, :content, :content_hash, 1, "
                    "'current', 'pending', 'pending', 'system_initial', "
                    ":source_version, :parser_version, :reason, :trace_id) "
                    "ON CONFLICT (kb_id, chunk_id, content_revision) DO NOTHING"
                ),
                {
                    "kb_id": kb_id,
                    "tenant_id": plan.tenant_id,
                    "project_id": plan.project_id,
                    "doc_id": plan.doc_id,
                    "chunk_id": plan.chunk_id,
                    "source_content": plan.source_content,
                    "source_content_hash": _sha256(plan.source_content),
                    "content": plan.source_content,
                    "content_hash": content_hash,
                    "source_version": plan.source_version or None,
                    "parser_version": plan.parser_version or None,
                    "reason": "backfill_m5a",
                    "trace_id": trace_id,
                },
            )
            if result.rowcount:
                inserted_ids.append(plan.chunk_id)
            else:
                existing += 1
    return {"inserted_ids": inserted_ids, "existing": existing}


def _update_projection_state(
    kb_id: str,
    chunk_id: str,
    graph_status: Optional[str] = None,
    graph_content_revision: Optional[int] = None,
    vector_status: Optional[str] = None,
    vector_content_revision: Optional[int] = None,
) -> int:
    """落投影状态。backfill 只处理本轮新建的 revision 1 行，故 CAS 固定在 revision 1；
    与 reindex worker 共用 services.chunk_projection_state 的同一份回写实现。
    """
    from services.chunk_projection_state import update_projection_state

    return update_projection_state(
        kb_id,
        chunk_id,
        expected_revision=1,
        graph_status=graph_status,
        graph_content_revision=graph_content_revision,
        vector_status=vector_status,
        vector_content_revision=vector_content_revision,
    )


def _enqueue_reindex_jobs(targets: List[Dict[str, Any]], trace_id: str) -> Dict[str, Any]:
    """needs_reindex_targets 按 doc_id 分组生成 current revision 的 reindex targets 入队（§15.3 步骤 7）。

    §16.3 的去重/复用/重试分支只在 services.reindex_queue 实现一次：这里若再留一份
    INSERT SQL，build_graph 侧的入队与 backfill 侧的入队会对同一 (job_type, kb_id,
    targets_hash) 给出不同判定，运维看到的 jobs_reused 就不再可信。
    """
    from services.reindex_queue import enqueue_reindex_jobs

    return enqueue_reindex_jobs(
        targets,
        source="backfill_m5a",
        trace_id=trace_id,
        max_retries=3,
        engine=engine,
    )


def evaluate_gate(inventory: Inventory) -> Dict[str, Any]:
    """v3.2.1 前置门：needs_reindex/blocked 未收敛不关闭；skipped 放行记 DEGRADED_SKIPPED。"""
    gate = {
        "closed": (
            inventory.needs_reindex_targets == []
            and inventory.blocked == 0
            and not inventory.unrecoverable
            and not inventory.scope_unresolved
            and not inventory.scope_mismatches
        ),
        "needs_reindex": len(inventory.needs_reindex_targets),
        "blocked": inventory.blocked,
        "degraded_graph": inventory.degraded_skipped["graph"],
        "degraded_vector": inventory.degraded_skipped["vector"],
        "unrecoverable": len(inventory.unrecoverable),
        "scope_unresolved": len(inventory.scope_unresolved),
        "scope_mismatch": len(inventory.scope_mismatches),
    }
    gate["degraded_skipped"] = bool(gate["degraded_graph"] or gate["degraded_vector"])
    return gate


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def _print_report(inventory: Inventory, gate: Dict[str, Any], dry_run: bool) -> None:
    print(f"[capabilities] kb_id={inventory.kb_id} graph={'enabled' if inventory.graph_enabled else 'disabled'} "
          f"vector={'enabled' if inventory.vector_enabled else 'disabled'} "
          f"milvus_collection={inventory.milvus_collection or '-'} "
          f"milvus_revision_field={'yes' if inventory.milvus_revision_field else 'no'}")
    print("[inventory]")
    print(
        f"  neo4j_chunks={inventory.neo4j_count} milvus_chunks={inventory.milvus_count} "
        f"parsed_chunks={inventory.parsed_count} overlap={inventory.overlap_count} "
        f"revision_rows_current={inventory.revision_row_count}"
    )
    print(
        f"  new_chunks={len(inventory.new_chunks)} needs_reindex_targets={len(inventory.needs_reindex_targets)} "
        f"converged={inventory.converged} blocked={inventory.blocked} "
        f"orphan_revisions={len(inventory.orphan_revisions)} "
        f"rows_skipped_existing={inventory.rows_skipped_existing} "
        f"content_mismatch={inventory.content_mismatch} unrecoverable={len(inventory.unrecoverable)} "
        f"scope_unresolved={len(inventory.scope_unresolved)}"
    )
    if inventory.kb_scope_missing:
        print("  SCOPE_WARNING：knowledge_bases 无该 KB 登记，作用域权威校验降级为仅证据源交叉比对")
    if inventory.scope_unresolved:
        print("  SCOPE_UNRESOLVED chunk_ids（doc/tenant/project 三件套不全，无法落 NOT NULL 作用域，fail-closed 零写入）: "
              + ", ".join(inventory.scope_unresolved))
        print("  处置：先在 knowledge_bases 补该 KB 的 tenant/project 登记（或按 reindex 重建索引侧证据），"
              "禁止伪造作用域字段写入；此项不计入 UNRECOVERABLE_MISMATCH（内容仍可恢复）")
    if inventory.scope_mismatches:
        print(f"  SCOPE_MISMATCH count={len(inventory.scope_mismatches)}（fail-closed，零写入）：")
        for item in inventory.scope_mismatches[:20]:
            print(
                f"    chunk_id={item['chunk_id']} field={item['field']} "
                f"expected={item['expected']} actual={item['actual']}"
            )
    if inventory.orphan_revisions:
        print("  ORPHAN_REVISION chunk_ids（PG 有 current 行但 Neo4j/Milvus 两侧索引均无该 chunk，计入 blocked 阻断门）: "
              + ", ".join(inventory.orphan_revisions))
    if inventory.needs_reindex_targets and dry_run:
        print("  needs_reindex targets (dry-run preview):")
        for item in inventory.needs_reindex_targets:
            print(
                f"    chunk_id={item['chunk_id']} doc_id={item['doc_id']} "
                f"target_revision={item['target_revision']} "
                f"graph_state={item['graph_state']} vector_state={item['vector_state']}"
            )
    if inventory.unrecoverable:
        print("  UNRECOVERABLE_MISMATCH chunk_ids: " + ", ".join(inventory.unrecoverable))
        print("  处置：终止 backfill，走 §9 备选受控清库（reset_legacy_knowledge_data.py dry-run + --confirm）")
    if gate["degraded_skipped"]:
        print(
            f"  DEGRADED_SKIPPED graph={gate['degraded_graph']} vector={gate['degraded_vector']}"
            "（技术迁移前置门可关闭，不得宣布完整索引验收通过）"
        )
    mode = "dry-run" if dry_run else "execute"
    status = "CLOSED" if gate["closed"] else "OPEN"
    if gate["closed"] and gate["degraded_skipped"]:
        status = "CLOSED_DEGRADED"
    print(f"[gate] {status} mode={mode} needs_reindex={gate['needs_reindex']} blocked={gate['blocked']}")


def run(kb_id: str, dry_run: bool) -> int:
    trace_id = f"backfill-{kb_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    inventory = build_inventory(kb_id)
    gate = evaluate_gate(inventory)

    # 作用域冲突 fail-closed：先于任何写入与 dry-run 分支拒绝（审计修复 #4）
    if inventory.scope_mismatches:
        _print_report(inventory, gate, dry_run=dry_run)
        if dry_run:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="rejected",
                exit_code=2,
                details={"reason": "SCOPE_MISMATCH", "count": len(inventory.scope_mismatches)},
            )
        print(f"✗ 拒绝执行：SCOPE_MISMATCH {len(inventory.scope_mismatches)} 处，零写入")
        return 2

    if dry_run:
        _print_report(inventory, gate, dry_run=True)
        if inventory.unrecoverable:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="rejected",
                exit_code=2,
                details={
                    "reason": "UNRECOVERABLE_MISMATCH",
                    "count": len(inventory.unrecoverable),
                },
            )
            print("✗ UNRECOVERABLE_MISMATCH：dry-run 终止，禁止写入，请走受控清库分支")
            return 2
        if inventory.scope_unresolved:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="rejected",
                exit_code=2,
                details={
                    "reason": "SCOPE_UNRESOLVED",
                    "count": len(inventory.scope_unresolved),
                },
            )
            print(
                f"✗ SCOPE_UNRESOLVED：{len(inventory.scope_unresolved)} 个 chunk 作用域三件套不全，"
                "dry-run 终止，禁止写入（先补 knowledge_bases 登记或走 reindex 重建，不得伪造作用域落库）"
            )
            return 2
        emit_dry_run_result(
            operation="backfill_chunk_revisions",
            status=("CLOSED_DEGRADED" if gate["degraded_skipped"] else "CLOSED")
            if gate["closed"]
            else "OPEN",
            exit_code=0,
            details={
                "needs_reindex": gate["needs_reindex"],
                "blocked": gate["blocked"],
                "unrecoverable": gate["unrecoverable"],
                "scope_unresolved": gate["scope_unresolved"],
            },
        )
        print("✓ dry-run completed，未写库")
        return 0

    if inventory.unrecoverable:
        _print_report(inventory, gate, dry_run=True)
        print("✗ 拒绝执行：存在 UNRECOVERABLE_MISMATCH chunk，先走受控清库（§9 备选）")
        return 2

    if inventory.scope_unresolved:
        _print_report(inventory, gate, dry_run=True)
        print(
            f"✗ 拒绝执行：SCOPE_UNRESOLVED {len(inventory.scope_unresolved)} 个 chunk，零写入"
            "（作用域三件套不全，禁止伪造 tenant/project 落库）"
        )
        return 2

    print(f"[backfill] trace_id={trace_id}")
    pg = _insert_revision_rows(kb_id, inventory.new_chunks, trace_id)
    # 仅对本次真正新插入 revision 1 行的 chunk 补写索引侧（重复跑不触碰已有行，§17.1）
    inserted_ids = set(pg["inserted_ids"])
    fresh_plans = [plan for plan in inventory.new_chunks if plan.chunk_id in inserted_ids]

    graph_outcome: Dict[str, str] = {}
    if fresh_plans and inventory.graph_enabled:
        graph_outcome = _backfill_neo4j(kb_id, fresh_plans)
    milvus_outcome: Dict[str, str] = {}
    if fresh_plans and inventory.vector_enabled:
        milvus_outcome = _backfill_milvus(kb_id, fresh_plans)

    counts = {
        "rows_new": len(inserted_ids),
        "neo4j_indexed": 0,
        "neo4j_failed": 0,
        "milvus_upserted": 0,
        "milvus_failed": 0,
        "milvus_skipped": 0,
        "revision_field_absent": 0,
        "state_write_failed": 0,
    }
    state_write_failed: List[str] = []
    for plan in fresh_plans:
        if not inventory.graph_enabled:
            graph_status, graph_rev = "skipped", None
        elif graph_outcome.get(plan.chunk_id) == "indexed":
            graph_status, graph_rev = "indexed", 1
            counts["neo4j_indexed"] += 1
        else:
            graph_status, graph_rev = "failed", None
            counts["neo4j_failed"] += 1
        milvus_state = milvus_outcome.get(plan.chunk_id)
        if not inventory.vector_enabled:
            vector_status, vector_rev = "skipped", None
        elif milvus_state == "indexed":
            vector_status, vector_rev = "indexed", 1
            counts["milvus_upserted"] += 1
        elif milvus_state == "revision_field_absent":
            # §8.5：collection 无显式 content_revision 字段，不写不可靠版本，保持 pending
            vector_status, vector_rev = "pending", None
            counts["revision_field_absent"] += 1
        elif milvus_state == "skipped":
            vector_status, vector_rev = "skipped", None
            counts["milvus_skipped"] += 1
        else:
            vector_status, vector_rev = "failed", None
            counts["milvus_failed"] += 1
        rowcount = _update_projection_state(kb_id, plan.chunk_id, graph_status, graph_rev, vector_status, vector_rev)
        if rowcount != 1:
            state_write_failed.append(plan.chunk_id)
            counts["state_write_failed"] += 1

    from services.chunk_projection_state import aggregate_document_states

    document_states = aggregate_document_states(
        kb_id,
        sorted({plan.doc_id for plan in fresh_plans if plan.doc_id}),
    )
    if document_states:
        print(f"  document_states={json.dumps(document_states, ensure_ascii=False, sort_keys=True)}")
    if state_write_failed:
        print("  state_write_failed=" + ",".join(sorted(state_write_failed)))

    print(
        f"  rows_new={counts['rows_new']} insert_conflicts_skipped={pg['existing']} "
        f"neo4j_updated={counts['neo4j_indexed']} neo4j_failed={counts['neo4j_failed']} "
        f"milvus_upserted={counts['milvus_upserted']} milvus_failed={counts['milvus_failed']} "
        f"milvus_skipped={counts['milvus_skipped']}"
    )
    if counts["revision_field_absent"]:
        print(
            f"  MILVUS_REVISION_FIELD_ABSENT count={counts['revision_field_absent']}"
            "（collection 无显式 content_revision 字段，vector 投影保持 pending；"
            "按 §8.5/§15.4 迁移 v3 collection 后重跑 reindex）"
        )

    if state_write_failed:
        print("✗ projection state CAS did not update exactly one current row; gate remains OPEN")

    # 入队必须基于写入之后的新鲜 inventory（活栈执行态取证发现的静默漏排）：
    # 决策时清单只包含"本轮之前就有 current 行"的 chunk，本轮新写入但投影未收敛的 chunk
    # （例如 §8.5 下 vector 保持 pending）不在其中。若沿用决策时清单，同一轮最终报告会把它
    # 判为 needs_reindex 却没有为它排入任何 reindex job，运维不重跑就永远补不上。
    fresh_inventory = build_inventory(kb_id)
    # rows_skipped_existing 以写入决策时的 inventory 为准：本轮新建的 revision 行不算"已有跳过"，
    # 否则会和 rows_new 重复计数，运维无法区分"跳过存量"与"本次新增"。
    fresh_inventory.rows_skipped_existing = inventory.rows_skipped_existing

    job_report = {"enqueued": 0, "reused": 0, "targets": 0}
    if fresh_inventory.needs_reindex_targets:
        for item in fresh_inventory.needs_reindex_targets:
            item["kb_id"] = kb_id
        job_report = _enqueue_reindex_jobs(fresh_inventory.needs_reindex_targets, trace_id)
        print(
            f"[reindex] jobs_enqueued={job_report['enqueued']} jobs_reused={job_report['reused']} "
            f"jobs_retried={job_report['retried']} jobs_reset={job_report['reset']} "
            f"jobs_rejected={job_report['rejected']} targets_total={job_report['targets']}"
        )
        # §16.3 超限拒绝必须点名到 chunk：只报 rejected 计数，运维就不知道是哪条 job 卡死，
        # 门会一直 OPEN 却无人可干预。
        for item in job_report.get("rejected_detail") or []:
            print(
                f"REINDEX_REJECTED job_id={item['job_id']} kb_id={item['kb_id']} "
                f"targets_hash={item['targets_hash']} retry_count={item['retry_count']}/{item['max_retries']} "
                f"chunk_ids={','.join(item['chunk_ids'])}"
            )

    # job 尚未执行时 needs_reindex 不收敛，门保持 OPEN
    fresh_gate = evaluate_gate(fresh_inventory)
    _print_report(fresh_inventory, fresh_gate, dry_run=False)
    if not fresh_gate["closed"]:
        print("✗ 前置门 OPEN：backfill 技术迁移完成，但 needs_reindex/blocked 投影未收敛，M5-A 验收门未关闭")
        return 3
    print("✓ backfill 完成，前置门 CLOSED" + ("（含 DEGRADED_SKIPPED，不得宣布完整索引验收通过）" if fresh_gate["degraded_skipped"] else ""))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill chunk_revisions for existing KB chunks (M5-A)")
    parser.add_argument("--kb", required=True, help="目标知识库 kb_id（必填，禁止全局扫描）")
    parser.add_argument("--dry-run", action="store_true", help="只输出 inventory 与前置门预览，不写库")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        kb_id = normalize_scope_id("kb_id", args.kb)
    except Exception as exc:  # noqa: BLE001 - SCOPE_INVALID 必须显式报告
        print(f"✗ kb_id 非法: {exc}")
        if args.dry_run:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="rejected",
                exit_code=2,
                details={"reason": "SCOPE_INVALID"},
            )
        return 2
    if not kb_id:
        print("✗ kb_id 不能为空")
        if args.dry_run:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="rejected",
                exit_code=2,
                details={"reason": "SCOPE_INVALID"},
            )
        return 2

    print("=" * 60)
    print("GraphInsight chunk_revisions backfill (M5-A)")
    print("=" * 60)
    with engine.begin() as conn:
        if not _table_exists(conn, "chunk_revisions"):
            print("✗ chunk_revisions 表不存在，请先执行 migrate_chunk_revisions.py")
            if args.dry_run:
                emit_dry_run_result(
                    operation="backfill_chunk_revisions",
                    status="rejected",
                    exit_code=2,
                    details={"reason": "MIGRATION_REQUIRED", "table": "chunk_revisions"},
                )
            return 2
    try:
        return run(kb_id, dry_run=bool(args.dry_run))
    except Exception as exc:  # noqa: BLE001 - 运维脚本失败必须显式退出码
        print(f"✗ backfill 执行失败: {exc}")
        if args.dry_run:
            emit_dry_run_result(
                operation="backfill_chunk_revisions",
                status="error",
                exit_code=1,
                details={"reason": "RUNTIME_ERROR"},
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
