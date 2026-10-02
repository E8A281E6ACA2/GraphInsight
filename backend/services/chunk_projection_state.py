"""chunk_revisions 投影状态回写与文档级聚合（设计 §6.2 / §8.2 步骤 4-5）。

backfill（M5-A）与 reindex worker（M5-B0）共用同一份实现：投影状态落库语义
（CAS 条件、skipped 必须把 `*_content_revision` 置 NULL、文档级聚合优先级）
只允许有一处定义，否则两侧收敛判定会静默分叉。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import text

# §6.2 聚合优先级：failed > stale/skipped > pending > indexed
PROJECTION_PRIORITY = {"failed": 0, "stale": 1, "skipped": 1, "pending": 2, "indexed": 3}
DOC_STATUS_FROM_PROJECTION = {"skipped": "stale"}


def _engine():
    from admin.database import engine

    return engine


def update_projection_state(
    kb_id: str,
    chunk_id: str,
    *,
    expected_revision: int,
    graph_status: Optional[str] = None,
    graph_content_revision: Optional[int] = None,
    vector_status: Optional[str] = None,
    vector_content_revision: Optional[int] = None,
) -> int:
    """CAS 回写 current 行投影状态：仅当 `content_revision` 仍等于 expected_revision 才生效。

    rowcount=0 表示 current 已移动（或该 chunk 不属于本 KB），调用方必须按过期处理，
    不得补写、也不得把索引侧已发生的写入宣布成收敛（§8.3 旧任务保护）。
    """
    assignments: List[str] = []
    params: Dict[str, Any] = {
        "kb_id": kb_id,
        "chunk_id": chunk_id,
        "expected_revision": int(expected_revision),
    }
    if graph_status is not None:
        assignments.append("graph_status = :graph_status")
        params["graph_status"] = graph_status
        assignments.append("graph_content_revision = :graph_content_revision")
        params["graph_content_revision"] = graph_content_revision
    if vector_status is not None:
        assignments.append("vector_status = :vector_status")
        params["vector_status"] = vector_status
        assignments.append("vector_content_revision = :vector_content_revision")
        params["vector_content_revision"] = vector_content_revision
    if not assignments:
        return 0
    with _engine().begin() as conn:
        result = conn.execute(
            text(
                f"UPDATE chunk_revisions SET {', '.join(assignments)} "
                "WHERE kb_id = :kb_id AND chunk_id = :chunk_id "
                "AND content_revision = :expected_revision AND revision_status = 'current'"
            ),
            params,
        )
        return int(result.rowcount or 0)


def aggregate_document_states(kb_id: str, doc_ids: List[str]) -> List[Dict[str, Any]]:
    """按 §6.2 重算文档级 `knowledge_base_documents.graph_status/vector_status`。

    聚合只看该文档的 current revision 行；`skipped` 计入文档级 `stale`，
    避免把 LLM/embedding 未配置伪装成 indexed。
    """
    clean_doc_ids = sorted({str(item).strip() for item in doc_ids if str(item or "").strip()})
    if not clean_doc_ids:
        return []
    with _engine().begin() as conn:
        try:
            conn.execute(text("SELECT 1 FROM knowledge_base_documents LIMIT 1"))
        except Exception:  # noqa: BLE001 - migration may not have created document table yet
            return []
        rows = conn.execute(
            text(
                "SELECT doc_id, graph_status, vector_status FROM chunk_revisions "
                f"WHERE kb_id = :kb_id AND revision_status = 'current' AND doc_id IN ({_placeholders(clean_doc_ids)})"
            ),
            {"kb_id": kb_id, **{f"doc_{index}": value for index, value in enumerate(clean_doc_ids)}},
        ).fetchall()

    per_doc: Dict[str, Dict[str, str]] = {}
    for row in rows:
        doc_id = str(row[0] or "")
        if not doc_id:
            continue
        side = per_doc.setdefault(doc_id, {"graph": "indexed", "vector": "indexed"})
        side["graph"] = _worse(side["graph"], str(row[1] or "pending"))
        side["vector"] = _worse(side["vector"], str(row[2] or "pending"))

    aggregated: List[Dict[str, Any]] = []
    with _engine().begin() as conn:
        for doc_id, side in sorted(per_doc.items()):
            graph_status = DOC_STATUS_FROM_PROJECTION.get(side["graph"], side["graph"])
            vector_status = DOC_STATUS_FROM_PROJECTION.get(side["vector"], side["vector"])
            conn.execute(
                text(
                    "UPDATE knowledge_base_documents SET graph_status = :graph_status, "
                    "vector_status = :vector_status WHERE kb_id = :kb_id AND doc_id = :doc_id"
                ),
                {"kb_id": kb_id, "doc_id": doc_id, "graph_status": graph_status, "vector_status": vector_status},
            )
            aggregated.append(
                {"doc_id": doc_id, "graph_status": graph_status, "vector_status": vector_status}
            )
    return aggregated


def _placeholders(values: List[str]) -> str:
    return ", ".join(f":doc_{index}" for index in range(len(values)))


def _worse(current: str, candidate: str) -> str:
    """取优先级更高（更差）的投影状态；未知状态按 pending 处理，不放大成 indexed。"""
    current_rank = PROJECTION_PRIORITY.get(current, PROJECTION_PRIORITY["pending"])
    candidate_rank = PROJECTION_PRIORITY.get(candidate, PROJECTION_PRIORITY["pending"])
    return candidate if candidate_rank < current_rank else current
