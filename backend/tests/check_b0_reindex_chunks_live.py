"""Real projection evidence for B0-1 in disposable KB/collection namespaces.

Requires --confirm. The embedding call is deterministic and local; Neo4j and
Milvus writes are real. Every synthetic PostgreSQL, Neo4j, filesystem, and
Milvus object is removed in finally.
"""
from __future__ import annotations

import argparse
import hashlib
import uuid
from typing import Any

from sqlalchemy import text


def _seed(kb_id: str, tenant_id: str, project_id: str, chunk_id: str, doc_id: str) -> None:
    from admin.database import engine

    content = "B0 live projection evidence"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, project_id, name, status, storage_prefix) "
                "VALUES (:kb, :tenant, :project, :name, 'active', :prefix)"
            ),
            {"kb": kb_id, "tenant": tenant_id, "project": project_id, "name": "B0 live evidence", "prefix": kb_id},
        )
        conn.execute(
            text(
                "INSERT INTO chunk_revisions (kb_id, tenant_id, project_id, doc_id, chunk_id, "
                "source_content, source_content_hash, content, content_hash, content_revision, "
                "revision_status, graph_status, vector_status, revision_source, reason, trace_id) "
                "VALUES (:kb, :tenant, :project, :doc, :chunk, :content, :hash, :content, :hash, 1, "
                "'current', 'pending', 'pending', 'system_initial', 'b0_live_evidence', 'b0-live')"
            ),
            {
                "kb": kb_id,
                "tenant": tenant_id,
                "project": project_id,
                "doc": doc_id,
                "chunk": chunk_id,
                "content": content,
                "hash": digest,
            },
        )


def _cleanup(kb_id: str, collection: str) -> None:
    from admin.database import engine
    from config import get_settings
    from neo4j import GraphDatabase
    from pymilvus import MilvusClient
    from services.vector_store import vector_store

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunk_revisions WHERE kb_id = :kb"), {"kb": kb_id})
        conn.execute(text("DELETE FROM admin_jobs WHERE kb_id = :kb"), {"kb": kb_id})
        conn.execute(text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": kb_id})

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=getattr(settings, "neo4j_connection_timeout_seconds", 5.0),
    )
    try:
        with driver.session(database=getattr(settings, "neo4j_database", None) or None) as session:
            session.run("MATCH (c:Chunk {kb_id: $kb}) DETACH DELETE c", {"kb": kb_id}).consume()
    finally:
        driver.close()

    client = vector_store._get_client()
    if client.has_collection(collection):
        client.drop_collection(collection)


def _create_collection(collection: str) -> None:
    from pymilvus import DataType, MilvusClient
    from services.vector_store import vector_store

    client = vector_store._get_client()
    if client.has_collection(collection):
        client.drop_collection(collection)
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field(field_name="chunk_id", datatype=DataType.VARCHAR, is_primary=True, max_length=128)
    for name in ("doc_id", "kb_id", "tenant_id", "project_id"):
        schema.add_field(field_name=name, datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=4096)
    schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=512)
    schema.add_field(field_name="location", datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name="content_hash", datatype=DataType.VARCHAR, max_length=80)
    schema.add_field(field_name="embedding_model", datatype=DataType.VARCHAR, max_length=128)
    schema.add_field(field_name="entities_json", datatype=DataType.VARCHAR, max_length=2048)
    schema.add_field(field_name="content_revision", datatype=DataType.INT64)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=4)
    client.create_collection(collection_name=collection, schema=schema)


def main() -> int:
    parser = argparse.ArgumentParser(description="B0-1 real Neo4j/Milvus evidence in disposable namespaces")
    parser.add_argument("--confirm", action="store_true", help="allow synthetic writes and cleanup")
    args = parser.parse_args()
    if not args.confirm:
        print("refusing live writes without --confirm")
        return 2

    kb_id = f"b0-live-{uuid.uuid4().hex[:12]}"
    tenant_id = f"b0-tenant-{uuid.uuid4().hex[:8]}"
    project_id = f"b0-project-{uuid.uuid4().hex[:8]}"
    chunk_id = f"b0-chunk-{uuid.uuid4().hex[:12]}"
    doc_id = f"b0-doc-{uuid.uuid4().hex[:8]}"
    collection = f"graphinsight_chunks_v3_b0_{uuid.uuid4().hex[:8]}"

    from services.embedding_service import embedding_service
    from services.vector_store import vector_store
    from services.job_runtime import execute_job

    original_config = vector_store.config
    original_embed = embedding_service.embed_texts
    base_config = original_config()
    vector_store.config = lambda: {**base_config, "collection": collection}
    embedding_service.embed_texts = lambda texts: [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    vector_store._revision_field.clear()
    try:
        _create_collection(collection)
        _seed(kb_id, tenant_id, project_id, chunk_id, doc_id)
        result: dict[str, Any] = execute_job(
            job_id=0,
            job_type="reindex_chunks",
            payload={
                "kb_id": kb_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "targets": [{"chunk_id": chunk_id, "target_revision": 1}],
                "doc_id": doc_id,
                "source": "b0_live_evidence",
            },
        )
        if result.get("execution_status") != "completed":
            raise RuntimeError(f"unexpected execution status: {result}")
        with __import__("admin.database", fromlist=["engine"]).engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT graph_status, vector_status, graph_content_revision, vector_content_revision "
                    "FROM chunk_revisions WHERE kb_id = :kb AND chunk_id = :chunk"
                ),
                {"kb": kb_id, "chunk": chunk_id},
            ).one()
        if tuple(row) != ("indexed", "indexed", 1, 1):
            raise RuntimeError(f"unexpected projection state: {tuple(row)}")
        print("B0_LIVE_REAL_OK")
        print(f"collection={collection} kb={kb_id} graph=real neo4j vector=real milvus")
        print("embedding=deterministic-local test vector; no external embedding request")
        return 0
    finally:
        try:
            _cleanup(kb_id, collection)
        finally:
            vector_store.config = original_config
            embedding_service.embed_texts = original_embed
            vector_store._revision_field.clear()


if __name__ == "__main__":
    raise SystemExit(main())
