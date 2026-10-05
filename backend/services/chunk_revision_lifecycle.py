"""chunk_revisions 权威 revision 生命周期（M5 Wave 2 / §16.1 S1 建图入口）。

口径：build_graph 每处理一次文档内容变化，必须在同一个数据库事务里建立/推进每个
chunk 的权威 revision，之后才允许写 Milvus / Neo4j 投影；否则 §8.5 shadow 侧
`content_revision` 缺源，且投影状态回写没有可 CAS 的目标行。

复用面（**不新增迁移**，用户 2026-10-04 定案）：
- `chunk_revisions` 表本身（`backend/admin/migrate_chunk_revisions.py` 建）；
- UNIQUE (kb_id, chunk_id, content_revision) 约束（版本单调，防重放）；
- 部分唯一索引 `uq_chunk_revisions_current` (kb_id, chunk_id) WHERE revision_status='current'
  （数据库级"至多一个 current"保证，不用应用层锁）；
- 状态字段 `revision_status` ∈ {current, superseded}，默认 'current'。

CAS 语义（单事务）：
- 无 current 行 → INSERT rev=1 current；
- current 存在且 content_hash 一致 → 保持不动，返回现有 revision（幂等重试）；
- current 存在且 content_hash 不同 → UPDATE old→superseded + INSERT rev=max(rev)+1 current；
- 事务任一步失败 → 整体 rollback，调用方不得进入 Milvus/Neo4j 写。

并发保证：partial unique index 让"两个并发事务都把 old→superseded + 都 INSERT new current"
必然有一个失败（第二条 current 违反 uq_chunk_revisions_current），SQLAlchemy rollback 后
调用方重试即可；不需要应用层锁。

边界（Wave 3 已落地，仍不在本模块）：本模块只负责建立 revision 真相源；影子失败持久转交与
targets_hash 去重入队在 `services/reindex_queue.py`，作业（父）终态→投影（子）状态回写在
`services/chunk_projection_state.py:write_back_job_failure`（由 `admin/services/job_service.py`
在不再自动重试时调用）。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import Engine, text


def _engine_default() -> Engine:
    from admin.database import engine

    return engine


def _sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _normalize_chunk(chunk: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """把 build_graph 的 chunk_payload 元素归一化为 revision 行需要的最小字段集。

    - `chunk_id`、`doc_id` 必须非空，否则该 chunk 被跳过（返回 None）并计入 skipped。
    - `text` 空 → 跳过（不写空投影，也不建立 revision；§8.2 步骤 1 同源）。
    - `source_content` 若缺失则退化为 `text`（backfill M5-A 也按同规则）。
    """
    chunk_id = str(chunk.get("chunk_id") or "").strip()
    doc_id = str(chunk.get("doc_id") or "").strip()
    text_value = str(chunk.get("text") or "")
    source_content = str(chunk.get("source_content") or text_value)
    if not chunk_id or not doc_id or not text_value.strip():
        return None
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "content": text_value,
        "source_content": source_content,
        "content_hash": _sha256(text_value),
        "source_content_hash": _sha256(source_content),
    }


def write_revisions_for_build_graph(
    *,
    kb_id: str,
    tenant_id: str,
    project_id: str,
    doc_id: str,
    chunks: Iterable[Dict[str, Any]],
    source_version: str = "",
    parser_version: str = "",
    revision_source: str = "system_initial",
    reason: str = "build_graph_m5_wave2",
    trace_id: str = "",
    engine: Optional[Engine] = None,
) -> Dict[str, Any]:
    """事务内建立/推进 chunk 权威 revision；返回 {revisions, inserted, kept, superseded, skipped}.

    - revisions: `Dict[chunk_id, int]`，本轮结束后每个 chunk 的权威 current revision；
      调用方必须把这个 map 传给 `retrieval_orchestrator.index_chunks`，让 Milvus
      VectorChunk 拿到 §8.5 要求的显式 content_revision。
    - inserted: 新建立 current 的 chunk_id 集合（首轮或老 current 缺失）。
    - kept: 内容与既有 current 一致、未 bump 的 chunk_id 集合（幂等重试）。
    - superseded: 内容变化被推入新 revision 的 chunk_id 集合（old→superseded, new→current）。
    - skipped: 因缺关键字段（chunk_id/doc_id/text 空）被跳过的 chunk_id 列表。
    """
    if not str(kb_id or "").strip():
        raise ValueError("write_revisions_for_build_graph 需要非空 kb_id")
    if not str(doc_id or "").strip():
        raise ValueError("write_revisions_for_build_graph 需要非空 doc_id")

    conn_engine = engine or _engine_default()
    prepared: List[Dict[str, str]] = []
    skipped: List[str] = []
    seen: set = set()
    for raw in chunks:
        item = _normalize_chunk(raw)
        if item is None:
            cid = str(raw.get("chunk_id") or "")
            skipped.append(cid)
            continue
        # 同一批次里同 chunk_id 只处理一次（后到覆盖）；build_graph 理论上不会重
        # 复，但保留 dedup 让调用方 payload 有轻微异常也不至于让事务失败。
        if item["chunk_id"] in seen:
            continue
        seen.add(item["chunk_id"])
        prepared.append(item)

    revisions: Dict[str, int] = {}
    inserted: List[str] = []
    kept: List[str] = []
    superseded: List[str] = []

    with conn_engine.begin() as conn:
        for item in prepared:
            current = conn.execute(
                text(
                    "SELECT revision_id, content_revision, content_hash "
                    "FROM chunk_revisions "
                    "WHERE kb_id = :kb_id AND chunk_id = :chunk_id AND revision_status = 'current'"
                ),
                {"kb_id": kb_id, "chunk_id": item["chunk_id"]},
            ).fetchone()
            max_rev_row = conn.execute(
                text(
                    "SELECT COALESCE(MAX(content_revision), 0) FROM chunk_revisions "
                    "WHERE kb_id = :kb_id AND chunk_id = :chunk_id"
                ),
                {"kb_id": kb_id, "chunk_id": item["chunk_id"]},
            ).scalar()
            max_rev = int(max_rev_row or 0)

            if current is None:
                new_rev = max_rev + 1
                _insert_current(
                    conn,
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    doc_id=item["doc_id"],
                    chunk_id=item["chunk_id"],
                    source_content=item["source_content"],
                    source_content_hash=item["source_content_hash"],
                    content=item["content"],
                    content_hash=item["content_hash"],
                    content_revision=new_rev,
                    revision_source=revision_source,
                    source_version=source_version,
                    parser_version=parser_version,
                    reason=reason,
                    trace_id=trace_id,
                )
                revisions[item["chunk_id"]] = new_rev
                inserted.append(item["chunk_id"])
                continue

            existing_rev = int(current[1])
            existing_hash = str(current[2] or "")
            if existing_hash == item["content_hash"]:
                # 内容未变：保持 current 不动，返回既有 revision。幂等重试路径。
                revisions[item["chunk_id"]] = existing_rev
                kept.append(item["chunk_id"])
                continue

            # 内容变化：old→superseded 再 INSERT new current。CAS 谓词锁定 old revision_id +
            # revision_status='current'，若并发事务已经抢先移动 current，rowcount=0 →
            # 由外层 raise 触发 rollback（不能出现"两条 current"）。
            moved = conn.execute(
                text(
                    "UPDATE chunk_revisions SET revision_status = 'superseded' "
                    "WHERE revision_id = :revision_id AND revision_status = 'current'"
                ),
                {"revision_id": int(current[0])},
            )
            if int(moved.rowcount or 0) != 1:
                raise RuntimeError(
                    f"chunk_revisions current 已移动（并发建图）：kb_id={kb_id} "
                    f"chunk_id={item['chunk_id']} expected_revision_id={int(current[0])}"
                )
            new_rev = max_rev + 1
            _insert_current(
                conn,
                kb_id=kb_id,
                tenant_id=tenant_id,
                project_id=project_id,
                doc_id=item["doc_id"],
                chunk_id=item["chunk_id"],
                source_content=item["source_content"],
                source_content_hash=item["source_content_hash"],
                content=item["content"],
                content_hash=item["content_hash"],
                content_revision=new_rev,
                revision_source=revision_source,
                source_version=source_version,
                parser_version=parser_version,
                reason=reason,
                trace_id=trace_id,
            )
            revisions[item["chunk_id"]] = new_rev
            superseded.append(item["chunk_id"])

    return {
        "revisions": revisions,
        "inserted": inserted,
        "kept": kept,
        "superseded": superseded,
        "skipped": skipped,
    }


def _insert_current(
    conn,
    *,
    kb_id: str,
    tenant_id: str,
    project_id: str,
    doc_id: str,
    chunk_id: str,
    source_content: str,
    source_content_hash: str,
    content: str,
    content_hash: str,
    content_revision: int,
    revision_source: str,
    source_version: str,
    parser_version: str,
    reason: str,
    trace_id: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
            "source_content, source_content_hash, content, content_hash, content_revision, "
            "revision_status, graph_status, vector_status, revision_source, "
            "source_version, parser_version, reason, trace_id) "
            "VALUES (:kb_id, :tenant_id, :project_id, :doc_id, :chunk_id, "
            ":source_content, :source_content_hash, :content, :content_hash, :content_revision, "
            "'current', 'pending', 'pending', :revision_source, "
            ":source_version, :parser_version, :reason, :trace_id)"
        ),
        {
            "kb_id": kb_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "source_content": source_content,
            "source_content_hash": source_content_hash,
            "content": content,
            "content_hash": content_hash,
            "content_revision": content_revision,
            "revision_source": revision_source,
            "source_version": source_version or None,
            "parser_version": parser_version or None,
            "reason": reason,
            "trace_id": trace_id or None,
        },
    )
