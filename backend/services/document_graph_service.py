"""
文档解析并入库 Neo4j 的服务（M3：知识库作用域隔离）

作用域不变量（docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md §2）：
- build_graph 输入为显式 doc_ids（来自 knowledge_base_documents 注册表），禁止目录扫描；
- 所有 Neo4j 写入/删除携带 kb_id（Document/Chunk 另带 tenant_id/project_id）；
- Entity MERGE 键为 {entity_key, kb_id}，entity_key = scope_contract.entity_key(...)；
- delete/clear 必须显式携带 kb_id，缺少即 KB_SCOPE_REQUIRED，不触碰数据库；
- 解析产物目录为 parsed_documents/{kb_id}/{doc_id}/。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import get_settings
from core import get_logger
from core.exceptions import ErrorCode, KnowledgeScopeError
from services.document_parser import DocumentParserManager, ParsedDocument
from services.knowledge_discovery.chunking import StructuredChunk, StructuredChunker
from services.knowledge_discovery.extraction import build_extraction_schema, evidence_validator, extraction_planner
from services.knowledge_discovery.normalization import normalize_entity_name, normalize_entity_values
from services.knowledge_discovery.profiling import document_profiler
from services.scope_contract import entity_key as scope_entity_key
from services.scope_contract import normalize_scope_id

logger = get_logger()
settings = get_settings()

# 抽取管线目前只产出实体名称，不产出类型；统一使用稳定默认类型参与 entity_key 计算。
# 同名实体跨 KB 仍不会合并（entity_key 材料含 kb_id），见契约 §2.2。
DEFAULT_ENTITY_TYPE = "entity"


def __getattr__(name: str):
    """惰性解析旧模块级符号，保持既有 patch 目标可用，同时模块导入期不依赖 neo4j/openai。"""
    if name == "llm_entity_extractor":
        from services import llm_entity_extractor as module

        return module
    if name == "llm_relation_extractor":
        from services import llm_relation_extractor as module

        return module
    if name == "retrieval_orchestrator":
        from services.retrieval_orchestrator import retrieval_orchestrator as instance

        return instance
    if name == "get_neo4j_service":
        from services.neo4j_service import get_neo4j_service as getter

        return getter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SUPPORTED_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".json",
    ".log",
    ".docx",
    ".pdf",
}

STOPWORDS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "have",
    "for",
    "are",
    "was",
    "were",
    "but",
    "not",
    "you",
    "your",
    "他们",
    "我们",
    "这里",
    "以及",
    "但是",
    "因为",
    "因此",
    "进行",
    "已经",
    "相关",
    "作为",
    "本次",
    "信息",
}

STAGE_KEYWORDS = {
    "抽穗扬花期",
    "扬花期",
    "抽穗期",
    "开花期",
    "孕穗期",
    "拔节期",
    "分蘖期",
    "灌浆期",
    "乳熟期",
    "蜡熟期",
    "成熟期",
}

SCHEMA_RELATION_LABELS = {
    "作者",
    "防治对象",
    "平均防效",
    "病情指数",
    "产量",
    "增产率",
    "使用剂量",
    "发生阶段",
    "时间",
    "地点",
    "海拔",
    "土壤类型",
    "土壤肥力",
    "土壤pH",
    "供试品种",
    "供试药剂",
    "提供方",
    "工作单位",
    "属于",
    "影响",
    "高发期",
    "表格主题",
    "同段提及",
    "研究对象",
    "采用方法",
    "对照关系",
    "指标结果",
    "作者单位",
    "结论支持",
    "签署",
    "付款义务",
    "交付义务",
    "违约责任",
    "解除条件",
    "管辖",
    "金额",
    "贡献收入",
    "同比变化",
    "环比变化",
    "成本构成",
    "利润影响",
    "现金流影响",
    "风险关联",
    "发布",
    "适用于",
    "要求",
    "禁止",
    "处罚",
    "依据",
    "实施时间",
    "包含部件",
    "参数",
    "操作顺序",
    "触发告警",
    "故障原因",
    "解决方案",
    "限制条件",
    "参会",
    "讨论",
    "形成决议",
    "负责",
    "截止时间",
    "依赖",
    "风险",
}

RULE_RELATION_KEYWORDS = {
    "作者",
    "属于",
    "隶属",
    "位于",
    "包括",
    "包含",
    "使用",
    "采用",
    "导致",
    "影响",
    "合作",
    "生产",
    "采购",
    "批准",
    "发布",
    "制定",
    "实施",
    "支持",
    "关联",
    "相关",
    "时间",
    "地点",
    "海拔",
    "土壤类型",
    "土壤肥力",
    "土壤pH",
    "供试品种",
    "供试药剂",
    "提供方",
    "工作单位",
}

MIN_DEFAULT_RELATION_CONFIDENCE = 0.3

class DocumentGraphService:
    def __init__(self) -> None:
        self.neo4j = None

    def _neo4j(self):
        """惰性获取 Neo4j 服务（模块导入期不依赖 neo4j driver）。"""
        if self.neo4j is None:
            from services.neo4j_service import get_neo4j_service

            self.neo4j = get_neo4j_service()
        return self.neo4j

    def ensure_schema(self) -> None:
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            self._ensure_schema(session)

    def build_graph(
        self,
        *,
        kb_id: str,
        doc_ids: Optional[List[str]] = None,
        force: bool = False,
        reasoning_profile: Optional[str] = None,
        complex_extraction: bool = False,
        parser_provider: Optional[str] = None,
        tenant_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """按注册表显式 doc_ids 建图；不扫描目录，永不跨 KB 写入。

        - kb_id / doc_ids 缺失 → KB_SCOPE_REQUIRED（在任何数据库访问之前拒绝）；
        - 每个 doc 的作用域（kb/tenant/project）来自其注册表行；
          与 payload kb_id/tenant_id/project_id 不一致 → 跳过并报告；
        - force 仅表示忽略"未变更跳过"对目标 doc 重建投影，不做全局清空。
        """
        normalized_kb = normalize_scope_id("kb_id", kb_id)
        if not normalized_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="build_graph 必须显式携带 kb_id",
            )
        clean_doc_ids: List[str] = []
        for item in doc_ids or []:
            value = str(item or "").strip()
            if value and value not in clean_doc_ids:
                clean_doc_ids.append(value)
        if not clean_doc_ids:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="build_graph 需要显式 doc_ids，禁止目录扫描",
                details={"kb_id": normalized_kb},
            )
        normalized_tenant = normalize_scope_id("tenant_id", tenant_id) if tenant_id else ""
        normalized_project = normalize_scope_id("project_id", project_id) if project_id else ""

        # 注册表解析：doc_id -> registry row（Go 上传时写入，Python 数据面只读消费）
        from services import document_registry

        kb_row = document_registry.get_knowledge_base(normalized_kb)
        if kb_row is None:
            raise KnowledgeScopeError(
                ErrorCode.KB_NOT_FOUND,
                message=f"知识库不存在: {normalized_kb}",
                details={"kb_id": normalized_kb},
            )
        if str(kb_row.status or "").strip().lower() == "archived":
            raise KnowledgeScopeError(
                ErrorCode.KB_ARCHIVED,
                message="知识库已归档，禁止建图写入",
                details={"kb_id": normalized_kb},
            )
        rows = document_registry.get_documents(clean_doc_ids)

        jobs: List[Dict[str, Any]] = []
        skipped_cross_scope: List[Dict[str, str]] = []
        failures: List[Dict[str, str]] = []
        for doc_id in clean_doc_ids:
            row = rows.get(doc_id)
            if row is None:
                failures.append({"file": doc_id, "reason": "doc_not_found_in_registry"})
                continue
            row_kb = str(row.kb_id or "").strip().lower()
            if row_kb != normalized_kb:
                # 与 payload kb_id 不一致：跳过并报告，绝不跨库写入
                skipped_cross_scope.append({"doc_id": doc_id, "kb_id": row_kb})
                continue
            scope = document_registry.document_scope(row)
            if normalized_tenant and scope["tenant_id"] and normalized_tenant != scope["tenant_id"]:
                skipped_cross_scope.append({"doc_id": doc_id, "field": "tenant_id"})
                continue
            if normalized_project and scope["project_id"] and normalized_project != scope["project_id"]:
                skipped_cross_scope.append({"doc_id": doc_id, "field": "project_id"})
                continue
            try:
                file_path = document_registry.resolve_document_file_path(row, kb_row)
            except KnowledgeScopeError as exc:
                failures.append({"file": doc_id, "reason": exc.error_code})
                continue
            if not file_path.exists():
                failures.append({"file": str(getattr(row, "name", "") or doc_id), "reason": "source_file_missing"})
                continue
            jobs.append({"doc_id": doc_id, "file": file_path, "scope": scope})

        if not jobs:
            logger.warning(
                "注册表中无可建图文档",
                context={"kb_id": normalized_kb, "requested": len(clean_doc_ids)},
            )
            return {
                "documents": 0,
                "chunks": 0,
                "entities": 0,
                "total_documents": 0,
                "skipped_documents": 0,
                "failures": failures,
                "skipped_cross_scope": skipped_cross_scope,
                "scope": "kb_scoped",
                "kb_id": normalized_kb,
                "target_doc_ids": clean_doc_ids,
                "reasoning_profile": reasoning_profile or "",
                "complex_extraction": complex_extraction,
                "parser_provider": parser_provider or "",
            }

        logger.info(
            "开始解析文档",
            context={
                "kb_id": normalized_kb,
                "count": len(jobs),
                "force": force,
                "doc_ids_count": len(clean_doc_ids),
                "reasoning_profile": reasoning_profile or "",
                "complex_extraction": complex_extraction,
                "parser_provider": parser_provider or "",
            },
        )

        apoc_available = False
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            self._ensure_schema(session)
            if settings.llm_relation_dynamic_type:
                apoc_available = self._check_apoc_available(session)

        total_documents = len(jobs)
        doc_count = 0
        chunk_count = 0
        entity_count = 0
        relation_count = 0
        vector_indexed = 0
        vector_failures: List[str] = []
        skipped_documents = 0
        processed_doc_ids: List[str] = []
        doc_tenant_id = ""
        doc_project_id = ""
        entity_names: set[str] = set()
        parse_warnings: List[Dict[str, str]] = []
        use_dynamic_relations = settings.llm_relation_dynamic_type and apoc_available
        dynamic_relation_failed_logged = False
        parser_manager = DocumentParserManager()
        structured_chunker = StructuredChunker()

        for job in jobs:
            doc = job["file"]
            doc_id = job["doc_id"]
            doc_kb_id = job["scope"]["kb_id"]
            doc_tenant_id = job["scope"]["tenant_id"]
            doc_project_id = job["scope"]["project_id"]
            try:
                parsed = parser_manager.parse(doc, provider_override=parser_provider)
                text = parsed.text
                for warning in parsed.warnings[:5]:
                    parse_warnings.append({"file": doc.name, "warning": warning})
                if not text.strip():
                    reason = parsed.warnings[0] if parsed.warnings else "empty_text"
                    failures.append({"file": doc.name, "reason": reason})
                    logger.warning(
                        "文档解析为空，已跳过",
                        context={"file": str(doc), "parser_provider": parsed.parser_provider},
                    )
                    continue

                content_hash = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()

                structured_chunks = structured_chunker.chunk(parsed, doc_id=doc_id)
                if not structured_chunks:
                    structured_chunks = [
                        StructuredChunk(text=chunk, block_type="paragraph", source_location=f"Chunk {idx}")
                        for idx, chunk in enumerate(self._chunk_text(text))
                    ]
                document_profile = document_profiler.profile(
                    parsed,
                    structured_chunks=structured_chunks,
                    file_name=doc.name,
                ).to_dict()
                extraction_schema = build_extraction_schema(document_profile).to_dict()
                extraction_plan = extraction_planner.plan(
                    structured_chunks,
                    document_profile=document_profile,
                    reasoning_profile=reasoning_profile,
                    complex_extraction=complex_extraction,
                    base_llm_budget=max(settings.llm_graph_extract_max_llm_chunks, 0),
                )
                chunk_payload = []
                for idx, structured_chunk in enumerate(structured_chunks):
                    chunk = structured_chunk.text
                    llm_extraction_text = self._llm_extraction_text(structured_chunk)
                    parser_metadata = self._chunk_parser_metadata(parsed, idx, structured_chunk)
                    plan_item = extraction_plan.item_for(idx)
                    use_llm_extraction = plan_item.use_llm
                    entities = self._merge_entities(
                        self._extract_entities(
                            llm_extraction_text,
                            reasoning_profile=reasoning_profile,
                            use_llm=use_llm_extraction,
                            document_profile=document_profile,
                        ),
                        self._entities_from_structured_chunk(structured_chunk),
                    )
                    relation_profile = reasoning_profile
                    if complex_extraction and not relation_profile:
                        relation_profile = "balanced"
                    semantic_relations = self._extract_relations(
                        llm_extraction_text,
                        entities,
                        reasoning_profile=relation_profile,
                        use_llm=use_llm_extraction,
                        document_profile=document_profile,
                        chunk_metadata={
                            **parser_metadata,
                            "caption": structured_chunk.caption,
                            "document_type": document_profile.get("document_type"),
                            "domain": document_profile.get("domain"),
                            "extraction_strategy": plan_item.strategy,
                            "extraction_priority": plan_item.priority,
                            "extraction_reasons": plan_item.reasons,
                            "table_columns": structured_chunk.table_columns,
                        },
                    )
                    table_relations = self._normalize_relations(
                        self._extract_table_relations(structured_chunk),
                        max_relations=max(settings.llm_max_relations * 16, 128),
                    )
                    relations = self._merge_relations(
                        table_relations,
                        semantic_relations,
                        max_relations=(
                            len(table_relations) + settings.llm_max_relations
                            if table_relations
                            else settings.llm_max_relations
                        ),
                    )
                    entity_names.update(entities)
                    entity_nodes = [
                        {
                            "name": name,
                            "entity_key": scope_entity_key(doc_kb_id, name, DEFAULT_ENTITY_TYPE),
                        }
                        for name in entities
                    ]
                    for rel in relations:
                        rel["source_key"] = scope_entity_key(
                            doc_kb_id, str(rel.get("source") or ""), DEFAULT_ENTITY_TYPE
                        )
                        rel["target_key"] = scope_entity_key(
                            doc_kb_id, str(rel.get("target") or ""), DEFAULT_ENTITY_TYPE
                        )
                    chunk_payload.append(
                        {
                            "chunk_id": f"{doc_id}-{idx:03d}",
                            "doc_id": doc_id,
                            "index": idx,
                            "text": chunk,
                            "title": doc.name,
                            "location": structured_chunk.caption or f"Chunk {idx}",
                            **parser_metadata,
                            "document_type": document_profile.get("document_type"),
                            "domain": document_profile.get("domain"),
                            "profile_version": document_profile.get("profile_version"),
                            "extraction_planner_version": extraction_plan.planner_version,
                            "extraction_strategy": plan_item.strategy,
                            "extraction_use_llm": plan_item.use_llm,
                            "extraction_priority": plan_item.priority,
                            "extraction_reasons": plan_item.reasons,
                            "caption": structured_chunk.caption,
                            "neighbor_before": structured_chunk.neighbor_before,
                            "neighbor_after": structured_chunk.neighbor_after,
                            "table_columns": structured_chunk.table_columns,
                            "table_rows_json": json.dumps(structured_chunk.table_rows, ensure_ascii=False)[:4096],
                            "entities": entities,
                            "entity_nodes": entity_nodes,
                            "relations": relations,
                        }
                    )
                parsed_artifact_path = self._write_parsed_document_artifacts(
                    doc=doc,
                    doc_id=doc_id,
                    kb_id=doc_kb_id,
                    parsed=parsed,
                    content_hash=content_hash,
                    chunks=chunk_payload,
                    structured_chunks=structured_chunks,
                    document_profile=document_profile,
                    extraction_schema=extraction_schema,
                    extraction_plan=extraction_plan.to_dict(),
                )

                with neo4j.session() as session:
                    existing = session.run(
                        """
                        MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id})
                        RETURN d.hash AS hash, d.parser_provider AS parser_provider
                        """,
                        {"doc_id": doc_id, "kb_id": doc_kb_id},
                    ).single()
                    if (
                        existing
                        and existing.get("hash") == content_hash
                        and existing.get("parser_provider") == parsed.parser_provider
                        and not force
                    ):
                        session.run(
                            """
                            MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id})
                            SET d.parser_version = $parser_version,
                                d.parse_mode = $parse_mode,
                                d.parsed_artifact_path = $parsed_artifact_path,
                                d.document_type = $document_type,
                                d.domain = $domain,
                                d.profile_confidence = $profile_confidence,
                                d.profile_version = $profile_version
                            """,
                            {
                                "doc_id": doc_id,
                                "kb_id": doc_kb_id,
                                "parser_version": parsed.parser_version,
                                "parse_mode": parsed.parse_mode,
                                "parsed_artifact_path": str(parsed_artifact_path),
                                "document_type": document_profile.get("document_type"),
                                "domain": document_profile.get("domain"),
                                "profile_confidence": document_profile.get("confidence"),
                                "profile_version": document_profile.get("profile_version"),
                            },
                        )
                        skipped_documents += 1
                        logger.info("文档未变更，跳过", context={"doc": doc.name, "kb_id": doc_kb_id})
                        continue

                    session.run(
                        """
                        MERGE (d:Document {doc_id: $doc_id, kb_id: $kb_id})
                        ON CREATE SET d.created_at = timestamp()
                        SET d.name = $name,
                            d.tenant_id = $tenant_id,
                            d.project_id = $project_id,
                            d.kb_id = $kb_id,
                            d.path = $path,
                            d.ext = $ext,
                            d.size = $size,
                            d.hash = $hash,
                            d.parser_provider = $parser_provider,
                            d.parser_version = $parser_version,
                            d.parse_mode = $parse_mode,
                            d.parsed_artifact_path = $parsed_artifact_path,
                            d.document_type = $document_type,
                            d.domain = $domain,
                            d.profile_confidence = $profile_confidence,
                            d.profile_version = $profile_version,
                            d.updated_at = timestamp(),
                            d.source = 'document_ingest'
                        """,
                        {
                            "doc_id": doc_id,
                            "kb_id": doc_kb_id,
                            "tenant_id": doc_tenant_id,
                            "project_id": doc_project_id,
                            "name": doc.name,
                            "path": str(doc),
                            "ext": doc.suffix.lower(),
                            "size": doc.stat().st_size,
                            "hash": content_hash,
                            "parser_provider": parsed.parser_provider,
                            "parser_version": parsed.parser_version,
                            "parse_mode": parsed.parse_mode,
                            "parsed_artifact_path": str(parsed_artifact_path),
                            "document_type": document_profile.get("document_type"),
                            "domain": document_profile.get("domain"),
                            "profile_confidence": document_profile.get("confidence"),
                            "profile_version": document_profile.get("profile_version"),
                        },
                    )

                    session.run(
                        """
                        MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id})-[:HAS_CHUNK]->(c:Chunk)
                        DETACH DELETE c
                        """,
                        {"doc_id": doc_id, "kb_id": doc_kb_id},
                    )

                    session.run(
                        """
                        MATCH (:Entity)-[r]->(:Entity)
                        WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
                        DELETE r
                        """,
                        {"doc_id": doc_id, "kb_id": doc_kb_id},
                    )
                    self._cleanup_orphan_entities(session, doc_kb_id)

                    for batch in self._batch(chunk_payload, 50):
                        session.run(
                            """
                            UNWIND $chunks AS c
                            MERGE (ch:Chunk {chunk_id: c.chunk_id, kb_id: $kb_id})
                            SET ch.text = c.text,
                                ch.index = c.index,
                                ch.doc_id = $doc_id,
                                ch.kb_id = $kb_id,
                                ch.tenant_id = $tenant_id,
                                ch.project_id = $project_id,
                                ch.parser_provider = c.parser_provider,
                                ch.parser_version = c.parser_version,
                                ch.parse_mode = c.parse_mode,
                                ch.block_type = c.block_type,
                                ch.heading_path = c.heading_path,
                                ch.page_start = c.page_start,
                                ch.page_end = c.page_end,
                                ch.source_location = c.source_location,
                                ch.document_type = c.document_type,
                                ch.domain = c.domain,
                                ch.profile_version = c.profile_version,
                                ch.extraction_planner_version = c.extraction_planner_version,
                                ch.extraction_strategy = c.extraction_strategy,
                                ch.extraction_use_llm = c.extraction_use_llm,
                                ch.extraction_priority = c.extraction_priority,
                                ch.extraction_reasons = c.extraction_reasons,
                                ch.caption = c.caption,
                                ch.neighbor_before = c.neighbor_before,
                                ch.neighbor_after = c.neighbor_after,
                                ch.table_columns = c.table_columns,
                                ch.table_rows_json = c.table_rows_json,
                                ch.source = 'document_ingest'
                            WITH ch, c
                            MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id})
                            MERGE (d)-[:HAS_CHUNK]->(ch)
                            WITH ch, c
                            UNWIND c.entity_nodes AS ent
                            MERGE (e:Entity {entity_key: ent.entity_key, kb_id: $kb_id})
                            ON CREATE SET e.name = ent.name,
                                e.entity_type = $entity_type,
                                e.tenant_id = $tenant_id,
                                e.project_id = $project_id,
                                e.source = 'document_ingest'
                            SET e.name = ent.name
                            MERGE (ch)-[:MENTIONS]->(e)
                            """,
                            {
                                "doc_id": doc_id,
                                "kb_id": doc_kb_id,
                                "tenant_id": doc_tenant_id,
                                "project_id": doc_project_id,
                                "entity_type": DEFAULT_ENTITY_TYPE,
                                "chunks": batch,
                            },
                        )

                        has_relations = any(item.get("relations") for item in batch)
                        if not has_relations:
                            continue

                        if use_dynamic_relations:
                            try:
                                session.run(
                                    """
                                    UNWIND $chunks AS c
                                    UNWIND c.relations AS rel
                                    WITH c, rel
                                    WHERE rel.source IS NOT NULL
                                      AND rel.target IS NOT NULL
                                      AND rel.label IS NOT NULL
                                      AND rel.rel_type IS NOT NULL
                                      AND rel.source_key IS NOT NULL
                                      AND rel.target_key IS NOT NULL
                                    MERGE (s:Entity {entity_key: rel.source_key, kb_id: $kb_id})
                                    ON CREATE SET s.name = rel.source,
                                        s.entity_type = $entity_type,
                                        s.tenant_id = $tenant_id,
                                        s.project_id = $project_id,
                                        s.source = 'document_ingest'
                                    MERGE (t:Entity {entity_key: rel.target_key, kb_id: $kb_id})
                                    ON CREATE SET t.name = rel.target,
                                        t.entity_type = $entity_type,
                                        t.tenant_id = $tenant_id,
                                        t.project_id = $project_id,
                                        t.source = 'document_ingest'
                                    CALL apoc.create.relationship(
                                      s,
                                      rel.rel_type,
                                      {
                                        label: rel.label,
                                        doc_id: $doc_id,
                                        kb_id: $kb_id,
                                        tenant_id: $tenant_id,
                                        project_id: $project_id,
                                        chunk_id: c.chunk_id,
                                        source: 'document_ingest',
                                        confidence: rel.confidence,
                                        evidence: rel.evidence,
                                        relation_type: rel.relation_type
                                      },
                                      t
                                    ) YIELD rel AS r
                                    RETURN count(r) AS created
                                    """,
                                    {
                                        "doc_id": doc_id,
                                        "kb_id": doc_kb_id,
                                        "tenant_id": doc_tenant_id,
                                        "project_id": doc_project_id,
                                        "entity_type": DEFAULT_ENTITY_TYPE,
                                        "chunks": batch,
                                    },
                                )
                            except Exception as exc:  # noqa: BLE001
                                if not dynamic_relation_failed_logged:
                                    logger.warning(
                                        "动态关系写入失败，回退固定关系",
                                        context={"doc": doc.name, "error": str(exc)},
                                    )
                                    dynamic_relation_failed_logged = True
                                use_dynamic_relations = False

                        if not use_dynamic_relations:
                            session.run(
                                """
                                UNWIND $chunks AS c
                                UNWIND c.relations AS rel
                                WITH c, rel
                                WHERE rel.source IS NOT NULL
                                  AND rel.target IS NOT NULL
                                  AND rel.label IS NOT NULL
                                MERGE (s:Entity {entity_key: rel.source_key, kb_id: $kb_id})
                                ON CREATE SET s.name = rel.source,
                                    s.entity_type = $entity_type,
                                    s.tenant_id = $tenant_id,
                                    s.project_id = $project_id,
                                    s.source = 'document_ingest'
                                MERGE (t:Entity {entity_key: rel.target_key, kb_id: $kb_id})
                                ON CREATE SET t.name = rel.target,
                                    t.entity_type = $entity_type,
                                    t.tenant_id = $tenant_id,
                                    t.project_id = $project_id,
                                    t.source = 'document_ingest'
                                MERGE (s)-[r:RELATION {label: rel.label, doc_id: $doc_id, chunk_id: c.chunk_id, kb_id: $kb_id}]->(t)
                                SET r.tenant_id = $tenant_id,
                                    r.project_id = $project_id,
                                    r.source = 'document_ingest',
                                    r.confidence = coalesce(rel.confidence, r.confidence),
                                    r.evidence = coalesce(rel.evidence, r.evidence),
                                    r.relation_type = coalesce(rel.relation_type, r.relation_type)
                                """,
                                {
                                    "doc_id": doc_id,
                                    "kb_id": doc_kb_id,
                                    "tenant_id": doc_tenant_id,
                                    "project_id": doc_project_id,
                                    "entity_type": DEFAULT_ENTITY_TYPE,
                                    "chunks": batch,
                                },
                            )

                from services.retrieval_orchestrator import retrieval_orchestrator

                vector_result = retrieval_orchestrator.index_chunks(
                    chunk_payload,
                    kb_id=doc_kb_id,
                    tenant_id=doc_tenant_id,
                    project_id=doc_project_id,
                )
                vector_indexed += int(vector_result.get("indexed") or 0)
                vector_failures.extend([str(item) for item in (vector_result.get("failures") or [])])

                doc_count += 1
                processed_doc_ids.append(doc_id)
                chunk_count += len(chunk_payload)
                relation_count += sum(len(item.get("relations", [])) for item in chunk_payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "文档建图失败",
                    context={"file": str(doc), "error": str(exc)},
                )
                failures.append({"file": doc.name, "reason": str(exc)})
                continue

        entity_count = len(entity_names)
        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "entities": entity_count,
            "relations": relation_count,
            "vector_indexed": vector_indexed,
            "vector_failures": vector_failures[:10],
            "total_documents": total_documents,
            "skipped_documents": skipped_documents,
            "failures": failures,
            "skipped_cross_scope": skipped_cross_scope,
            "parse_warnings": parse_warnings[:20],
            "scope": "kb_scoped",
            "kb_id": normalized_kb,
            "tenant_id": doc_tenant_id,
            "project_id": doc_project_id,
            "target_doc_ids": clean_doc_ids,
            "processed_doc_ids": processed_doc_ids,
            "reasoning_profile": reasoning_profile or "",
            "complex_extraction": complex_extraction,
            "parser_provider": parser_provider or "",
        }

    def _require_kb_scope(self, kb_id: Optional[str]) -> str:
        """delete/clear 类操作的 kb 强制点：缺失/非法时在任何 DB 访问之前拒绝。"""
        normalized = normalize_scope_id("kb_id", kb_id)
        if not normalized:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="该操作必须显式携带 kb_id，禁止全局清理",
            )
        return normalized

    def delete_document_graph(self, doc_id: str, kb_id: str) -> Dict[str, int]:
        clean_kb = self._require_kb_scope(kb_id)
        clean_doc = str(doc_id or "").strip()
        if not clean_doc:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="delete_document_graph 必须显式携带 doc_id",
                details={"kb_id": clean_kb},
            )
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            relation_count = self._count_document_relations_for_doc(session, clean_doc, clean_kb)
            if relation_count:
                session.run(
                    """
                    MATCH (:Entity)-[r]->(:Entity)
                    WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
                    DELETE r
                    """,
                    {"doc_id": clean_doc, "kb_id": clean_kb},
                )

            chunk_count = int(
                (
                    session.run(
                        "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(c) AS c",
                        {"doc_id": clean_doc, "kb_id": clean_kb},
                    ).single()
                    or {}
                ).get("c")
                or 0
            )
            if chunk_count:
                session.run(
                    "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) DETACH DELETE c",
                    {"doc_id": clean_doc, "kb_id": clean_kb},
                )

            doc_count = int(
                (
                    session.run(
                        "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(d) AS c",
                        {"doc_id": clean_doc, "kb_id": clean_kb},
                    ).single()
                    or {}
                ).get("c")
                or 0
            )
            if doc_count:
                session.run(
                    "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) DETACH DELETE d",
                    {"doc_id": clean_doc, "kb_id": clean_kb},
                )

            orphan_entities = self._cleanup_orphan_entities(session, clean_kb)
        from services.retrieval_orchestrator import retrieval_orchestrator

        retrieval_orchestrator.delete_doc(clean_doc, clean_kb)
        self._delete_parsed_document_artifacts(clean_doc, clean_kb)

        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "relations": relation_count,
            "orphan_entities": orphan_entities,
            "kb_id": clean_kb,
        }

    def clear_document_graph(self, kb_id: str) -> Dict[str, int]:
        """清空单个知识库的图谱/向量/解析产物；kb_id 必填，无全局清理路径。"""
        clean_kb = self._require_kb_scope(kb_id)
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            stats = self._clear_graph_data(session, clean_kb)
        from services.retrieval_orchestrator import retrieval_orchestrator

        retrieval_orchestrator.clear([clean_kb])
        self._clear_parsed_document_artifacts(clean_kb)
        stats["kb_id"] = clean_kb
        return stats

    def preview_delete_document_graph(self, doc_id: str, kb_id: str) -> Dict[str, int]:
        clean_kb = self._require_kb_scope(kb_id)
        clean_doc = str(doc_id or "").strip()
        if not clean_doc:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="preview_delete_document_graph 必须显式携带 doc_id",
                details={"kb_id": clean_kb},
            )
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            relation_count = self._count_document_relations_for_doc(session, clean_doc, clean_kb)
            chunk_count = int(
                (
                    session.run(
                        "MATCH (c:Chunk {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(c) AS c",
                        {"doc_id": clean_doc, "kb_id": clean_kb},
                    ).single()
                    or {}
                ).get("c")
                or 0
            )
            doc_count = int(
                (
                    session.run(
                        "MATCH (d:Document {doc_id: $doc_id, kb_id: $kb_id}) RETURN count(d) AS c",
                        {"doc_id": clean_doc, "kb_id": clean_kb},
                    ).single()
                    or {}
                ).get("c")
                or 0
            )

        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "relations": relation_count,
            "orphan_entities": 0,
            "kb_id": clean_kb,
        }

    def preview_clear_document_graph(self, kb_id: str) -> Dict[str, int]:
        clean_kb = self._require_kb_scope(kb_id)
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            totals = self._get_graph_totals(session, clean_kb)
        return {
            "documents": totals["documents"],
            "chunks": totals["chunks"],
            "relations": totals["relations"],
            "orphan_entities": 0,
            "kb_id": clean_kb,
        }

    def get_graph_totals(self, kb_id: str) -> Dict[str, int]:
        clean_kb = self._require_kb_scope(kb_id)
        neo4j = self._neo4j()
        neo4j.ensure_connected()
        with neo4j.session() as session:
            return self._get_graph_totals(session, clean_kb)

    def _clear_graph_data(self, session, kb_id: str) -> Dict[str, int]:
        """清空指定 kb_id 的知识图谱数据（Document/Chunk/Relation + 孤儿实体）。"""
        clean_kb = self._require_kb_scope(kb_id)
        relation_count = self._count_document_relations(session, clean_kb)
        if relation_count:
            session.run(
                """
                MATCH (:Entity)-[r]->(:Entity)
                WHERE r.kb_id = $kb_id
                  AND (r.source = 'document_ingest' OR r.doc_id IS NOT NULL)
                DELETE r
                """,
                {"kb_id": clean_kb},
            )

        chunk_count = int(
            (
                session.run(
                    """
                    MATCH (c:Chunk)
                    WHERE c.kb_id = $kb_id
                      AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
                    RETURN count(c) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        if chunk_count:
            session.run(
                """
                MATCH (c:Chunk)
                WHERE c.kb_id = $kb_id
                  AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
                DETACH DELETE c
                """,
                {"kb_id": clean_kb},
            )

        doc_count = int(
            (
                session.run(
                    """
                    MATCH (d:Document)
                    WHERE d.kb_id = $kb_id AND d.source = 'document_ingest'
                    RETURN count(d) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        if doc_count:
            session.run(
                """
                MATCH (d:Document)
                WHERE d.kb_id = $kb_id AND d.source = 'document_ingest'
                DETACH DELETE d
                """,
                {"kb_id": clean_kb},
            )

        orphan_entities = self._cleanup_orphan_entities(session, clean_kb)
        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "relations": relation_count,
            "orphan_entities": orphan_entities,
        }

    def _get_graph_totals(self, session, kb_id: str) -> Dict[str, int]:
        clean_kb = self._require_kb_scope(kb_id)
        relation_count = self._count_document_relations(session, clean_kb)
        chunk_count = int(
            (
                session.run(
                    """
                    MATCH (c:Chunk)
                    WHERE c.kb_id = $kb_id
                      AND (c.source = 'document_ingest' OR c.doc_id IS NOT NULL)
                    RETURN count(c) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        doc_count = int(
            (
                session.run(
                    """
                    MATCH (d:Document)
                    WHERE d.kb_id = $kb_id AND d.source = 'document_ingest'
                    RETURN count(d) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        entity_count = int(
            (
                session.run(
                    """
                    MATCH (e:Entity)
                    WHERE e.kb_id = $kb_id AND e.source = 'document_ingest'
                    RETURN count(e) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        return {
            "documents": doc_count,
            "chunks": chunk_count,
            "relations": relation_count,
            "entities": entity_count,
        }

    @staticmethod
    def _scalar_count(session, cypher: str, parameters: Optional[Dict[str, object]] = None) -> int:
        return int(((session.run(cypher, parameters or {}).single() or {}).get("c")) or 0)

    def _count_document_relations_for_doc(self, session, doc_id: str, kb_id: str) -> int:
        clean_kb = self._require_kb_scope(kb_id)
        return (
            self._scalar_count(
                session,
                "MATCH (:Document {doc_id: $doc_id, kb_id: $kb_id})-[r:HAS_CHUNK]->(:Chunk) RETURN count(r) AS c",
                {"doc_id": doc_id, "kb_id": clean_kb},
            )
            + self._scalar_count(
                session,
                "MATCH (:Chunk {doc_id: $doc_id, kb_id: $kb_id})-[r:MENTIONS]->(:Entity) RETURN count(r) AS c",
                {"doc_id": doc_id, "kb_id": clean_kb},
            )
            + self._scalar_count(
                session,
                """
                MATCH (:Entity)-[r]->(:Entity)
                WHERE r.doc_id = $doc_id AND r.kb_id = $kb_id
                RETURN count(r) AS c
                """,
                {"doc_id": doc_id, "kb_id": clean_kb},
            )
        )

    def _count_document_relations(self, session, kb_id: str) -> int:
        clean_kb = self._require_kb_scope(kb_id)
        return (
            self._scalar_count(
                session,
                """
                MATCH (:Document)-[r:HAS_CHUNK]->(:Chunk)
                WHERE r.kb_id = $kb_id
                RETURN count(r) AS c
                """,
                {"kb_id": clean_kb},
            )
            + self._scalar_count(
                session,
                """
                MATCH (c:Chunk)-[r:MENTIONS]->(:Entity)
                WHERE c.kb_id = $kb_id
                RETURN count(r) AS c
                """,
                {"kb_id": clean_kb},
            )
            + self._scalar_count(
                session,
                """
                MATCH (:Entity)-[r]->(:Entity)
                WHERE r.kb_id = $kb_id
                  AND (r.source = 'document_ingest' OR r.doc_id IS NOT NULL)
                RETURN count(r) AS c
                """,
                {"kb_id": clean_kb},
            )
        )

    @staticmethod
    def _cleanup_orphan_entities(session, kb_id: str) -> int:
        """清理指定 kb_id 内不再被任何关系引用的实体（孤儿清理按 kb 作用域执行）。"""
        clean_kb = normalize_scope_id("kb_id", kb_id)
        if not clean_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="孤儿实体清理必须显式携带 kb_id",
            )
        count = int(
            (
                session.run(
                    """
                    MATCH (e:Entity {kb_id: $kb_id, source: 'document_ingest'})
                    WHERE NOT (e)--()
                    RETURN count(e) AS c
                    """,
                    {"kb_id": clean_kb},
                ).single()
                or {}
            ).get("c")
            or 0
        )
        if count:
            session.run(
                """
                MATCH (e:Entity {kb_id: $kb_id, source: 'document_ingest'})
                WHERE NOT (e)--()
                DELETE e
                """,
                {"kb_id": clean_kb},
            )
        return count

    def _write_parsed_document_artifacts(
        self,
        *,
        doc: Path,
        doc_id: str,
        kb_id: str,
        parsed: ParsedDocument,
        content_hash: str,
        chunks: List[Dict[str, Any]],
        structured_chunks: Optional[List[StructuredChunk]] = None,
        document_profile: Optional[Dict[str, Any]] = None,
        extraction_schema: Optional[Dict[str, Any]] = None,
        extraction_plan: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """解析产物布局（手册 §9）：parsed_documents/{kb_id}/{doc_id}/。"""
        clean_kb = self._require_kb_scope(kb_id)
        root = (Path(settings.parsed_document_storage_path).resolve() / clean_kb).resolve()
        target_dir = root / doc_id
        tmp_dir = root / f".{doc_id}.tmp"
        root.mkdir(parents=True, exist_ok=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        raw_output_path = self._write_raw_parser_payload(tmp_dir, parsed.raw_payload)
        parsed.raw_output_path = raw_output_path.name if raw_output_path else ""

        content_path = tmp_dir / "content.md"
        content_path.write_text(parsed.text, encoding="utf-8")

        blocks_path = tmp_dir / "blocks.json"
        blocks_path.write_text(
            json.dumps([asdict(block) for block in parsed.blocks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        chunks_path = tmp_dir / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as file_obj:
            for item in chunks:
                safe_item = {
                    key: value
                    for key, value in item.items()
                    if key not in {"entities", "entity_nodes", "relations"}
                }
                file_obj.write(json.dumps(safe_item, ensure_ascii=False) + "\n")

        structured_chunks_path = tmp_dir / "structured_chunks.jsonl"
        with structured_chunks_path.open("w", encoding="utf-8") as file_obj:
            for item in structured_chunks or []:
                file_obj.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

        document_profile_path = tmp_dir / "document_profile.json"
        document_profile_path.write_text(
            json.dumps(document_profile or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        extraction_schema_path = tmp_dir / "extraction_schema.json"
        extraction_schema_path.write_text(
            json.dumps(extraction_schema or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        extraction_plan_path = tmp_dir / "extraction_plan.json"
        extraction_plan_path.write_text(
            json.dumps(extraction_plan or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        manifest = {
            "doc_id": doc_id,
            "kb_id": clean_kb,
            "file_name": doc.name,
            "source_file": str(doc),
            "source_ext": doc.suffix.lower(),
            "source_size": doc.stat().st_size,
            "source_mtime": int(doc.stat().st_mtime),
            "content_hash": content_hash,
            "parser_provider": parsed.parser_provider,
            "parser_version": parsed.parser_version,
            "parse_mode": parsed.parse_mode,
            "document_type": (document_profile or {}).get("document_type"),
            "domain": (document_profile or {}).get("domain"),
            "profile_confidence": (document_profile or {}).get("confidence"),
            "profile_version": (document_profile or {}).get("profile_version"),
            "raw_output_path": parsed.raw_output_path,
            "content_path": "content.md",
            "blocks_path": "blocks.json",
            "chunks_path": "chunks.jsonl",
            "structured_chunks_path": "structured_chunks.jsonl",
            "document_profile_path": "document_profile.json",
            "extraction_schema_path": "extraction_schema.json",
            "extraction_plan_path": "extraction_plan.json",
            "text_chars": len(parsed.text),
            "block_count": len(parsed.blocks),
            "chunk_count": len(chunks),
            "structured_chunk_count": len(structured_chunks or []),
            "chunker_mode": "structured" if structured_chunks else "legacy",
            "warnings": parsed.warnings[:20],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if target_dir.exists():
            shutil.rmtree(target_dir)
        tmp_dir.replace(target_dir)
        return target_dir

    @staticmethod
    def _write_raw_parser_payload(target_dir: Path, payload: Any) -> Optional[Path]:
        if payload is None:
            return None
        if isinstance(payload, (dict, list)):
            raw_path = target_dir / "raw.json"
            raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return raw_path
        raw_path = target_dir / "raw.txt"
        raw_path.write_text(str(payload), encoding="utf-8")
        return raw_path

    @staticmethod
    def _delete_parsed_document_artifacts(doc_id: str, kb_id: str) -> None:
        clean_doc_id = str(doc_id or "").strip()
        if not clean_doc_id:
            return
        clean_kb = normalize_scope_id("kb_id", kb_id)
        if not clean_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="删除解析产物必须显式携带 kb_id",
            )
        target_dir = (
            Path(settings.parsed_document_storage_path).resolve() / clean_kb / clean_doc_id
        )
        if target_dir.exists():
            shutil.rmtree(target_dir)

    @staticmethod
    def _clear_parsed_document_artifacts(kb_id: str) -> None:
        """仅清理指定 kb_id 的解析产物目录；README 等根目录文件保留。"""
        clean_kb = normalize_scope_id("kb_id", kb_id)
        if not clean_kb:
            raise KnowledgeScopeError(
                ErrorCode.KB_SCOPE_REQUIRED,
                message="清理解析产物必须显式携带 kb_id",
            )
        kb_root = Path(settings.parsed_document_storage_path).resolve() / clean_kb
        if not kb_root.exists():
            return
        for path in kb_root.iterdir():
            if path.name == "README.md":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    @staticmethod
    def _chunk_parser_metadata(
        parsed: ParsedDocument,
        index: int,
        structured_chunk: Optional[StructuredChunk] = None,
    ) -> Dict[str, object]:
        block = parsed.blocks[index] if index < len(parsed.blocks) else None
        page_start, page_end = parsed.page_range()
        heading_path = structured_chunk.heading_path if structured_chunk else (block.heading_path if block else [])
        source_location = (
            structured_chunk.source_location
            if structured_chunk and structured_chunk.source_location
            else block.source_location if block and block.source_location else f"Chunk {index}"
        )
        return {
            "parser_provider": parsed.parser_provider,
            "parser_version": parsed.parser_version,
            "parse_mode": parsed.parse_mode,
            "block_type": structured_chunk.block_type if structured_chunk else (block.block_type if block else "text"),
            "heading_path": heading_path,
            "page_start": (
                structured_chunk.page_start
                if structured_chunk and structured_chunk.page_start is not None
                else block.page_start if block and block.page_start is not None else page_start
            ),
            "page_end": (
                structured_chunk.page_end
                if structured_chunk and structured_chunk.page_end is not None
                else block.page_end if block and block.page_end is not None else page_end
            ),
            "source_location": source_location,
        }

    def _chunk_text(self, text: str, max_chars: int = 800, overlap: int = 120) -> List[str]:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return []
        chunks = []
        start = 0
        length = len(cleaned)
        while start < length:
            end = min(length, start + max_chars)
            if end < length:
                tail = cleaned[end : min(length, end + 200)]
                match = re.search(r"[。.!?；;]\s", tail)
                if match:
                    end += match.end()
            chunk = cleaned[start:end].strip()
            if len(chunk) >= 30:
                chunks.append(chunk)
            next_start = end - overlap
            if next_start <= start:
                next_start = end
            start = next_start
        return chunks

    @staticmethod
    def _llm_extraction_text(chunk: StructuredChunk) -> str:
        if chunk.block_type != "table" or not chunk.table_columns or not chunk.table_rows:
            return chunk.text

        parts: List[str] = []
        heading = " > ".join(part for part in chunk.heading_path if str(part).strip())
        if heading:
            parts.append(f"标题路径：{heading}")
        if chunk.caption:
            parts.append(f"表题：{chunk.caption}")
        if chunk.neighbor_before:
            parts.append(f"表前上下文：{chunk.neighbor_before}")
        parts.append("列：" + " | ".join(chunk.table_columns))

        max_rows = 60
        for index, row in enumerate(chunk.table_rows[:max_rows], start=1):
            cells = []
            for column in chunk.table_columns:
                value = str(row.get(column) or "").strip()
                if value:
                    cells.append(f"{column}={value}")
            if cells:
                parts.append(f"第{index}行：" + "；".join(cells))
        if len(chunk.table_rows) > max_rows:
            parts.append(f"其余行数：{len(chunk.table_rows) - max_rows}")
        if chunk.neighbor_after:
            parts.append(f"表后上下文：{chunk.neighbor_after}")
        return "\n".join(parts).strip() or chunk.text

    @staticmethod
    def _merge_entities(*groups: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for group in groups:
            for entity in normalize_entity_values(group, max_items=128):
                key = re.sub(r"\s+", "", entity).lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(entity)
        return merged[: max(settings.llm_max_entities, 64)]

    def _entities_from_structured_chunk(self, chunk: StructuredChunk) -> List[str]:
        candidates: List[object] = []
        if chunk.block_type == "table" and chunk.table_rows and chunk.table_columns:
            subject_column = chunk.table_columns[0]
            for row in chunk.table_rows:
                candidates.append(row.get(subject_column))
                for column, value in row.items():
                    if column == subject_column:
                        continue
                    if str(value or "").strip():
                        candidates.append(f"{column}: {value}")
        topic = self._infer_table_topic(chunk)
        if topic:
            candidates.append(topic)
        return normalize_entity_values(candidates, max_items=64)

    def _extract_table_relations(self, chunk: StructuredChunk) -> List[Dict[str, object]]:
        if chunk.block_type != "table" or not chunk.table_rows or not chunk.table_columns:
            return []

        subject_column = chunk.table_columns[0]
        topic = self._infer_table_topic(chunk)
        relations: List[Dict[str, object]] = []
        evidence_prefix = chunk.caption or "表格"

        for row in chunk.table_rows:
            source = normalize_entity_name(row.get(subject_column))
            if not source:
                continue
            row_evidence = "；".join(
                f"{column}={value}" for column, value in row.items() if str(value or "").strip()
            )
            if topic and source != topic:
                relations.append(
                    {
                        "source": source,
                        "target": topic,
                        "label": "表格主题",
                        "confidence": 0.72,
                        "evidence": f"{evidence_prefix}：{row_evidence}",
                    }
                )
            for column in chunk.table_columns[1:]:
                value = str(row.get(column) or "").strip()
                if not value:
                    continue
                target = normalize_entity_name(f"{column}: {value}")
                if not target:
                    continue
                relations.append(
                    {
                        "source": source,
                        "target": target,
                        "label": column,
                        "confidence": 0.86,
                        "evidence": f"{evidence_prefix}：{row_evidence}",
                    }
                )
        return relations

    @staticmethod
    def _infer_table_topic(chunk: StructuredChunk) -> str:
        text = " ".join([chunk.caption or "", " ".join(chunk.heading_path), chunk.neighbor_before or ""])
        compact = re.sub(r"\s+", "", text)
        disease_match = re.search(r"[\u4e00-\u9fff]{1,12}病", compact)
        if disease_match:
            topic = disease_match.group(0)
            for marker in ("防治", "处理", "对", "及"):
                if marker in topic:
                    topic = topic.split(marker)[-1]
            return normalize_entity_name(topic)
        title_match = re.search(r"(?:关于|针对|处理|防治|分析)([\u4e00-\u9fffA-Za-z0-9%/ .-]{2,24})", text)
        if title_match:
            return normalize_entity_name(title_match.group(1))
        return ""

    def _extract_entities(
        self,
        text: str,
        max_entities: int = 12,
        reasoning_profile: Optional[str] = None,
        use_llm: bool = True,
        document_profile: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        from services.llm_entity_extractor import llm_entity_extractor  # 惰性导入（openai 依赖）

        llm_entities = (
            llm_entity_extractor.extract(
                text,
                reasoning_profile=reasoning_profile,
                document_profile=document_profile,
            )
            if use_llm
            else []
        )
        rule_entities = self._extract_entities_by_rules(text)
        if llm_entities or rule_entities:
            return normalize_entity_values(
                [*rule_entities, *llm_entities],
                max_items=max(max_entities, settings.llm_max_entities, 32),
            )

        tokens = re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z][A-Za-z0-9_-]{2,}", text)
        freq: Dict[str, int] = {}
        for token in tokens:
            lower = token.lower()
            if lower in STOPWORDS:
                continue
            freq[lower] = freq.get(lower, 0) + 1
        ranked = sorted(freq.items(), key=lambda item: item[1], reverse=True)
        result = [token for token, _ in ranked[:max_entities]]
        return normalize_entity_values(result, max_items=max_entities)

    @staticmethod
    def _extract_entities_by_rules(text: str) -> List[str]:
        raw = text or ""
        compact = re.sub(r"\s+", "", raw)
        candidates: List[object] = []

        def clean_location(value: str) -> str:
            value = re.sub(r"^.*[在于]", "", value)
            value = re.sub(r"^(?:至|到|从|自)", "", value)
            return value

        contextual_location = re.search(
            r"[在于]([\u4e00-\u9fff]{2,8}省[\u4e00-\u9fff]{2,12}县[\u4e00-\u9fff]{1,12}镇[\u4e00-\u9fff]{1,12}村)进行",
            compact,
        )
        if contextual_location:
            candidates.append(contextual_location.group(1))

        for match in re.finditer(r"(?<![年月日])[\u4e00-\u9fff]{2,12}(?:省|市|县|区|镇|乡|村|州|盟|旗)", compact):
            value = clean_location(match.group(0))
            if len(value) >= 4:
                candidates.append(value)

        for match in re.finditer(
            r"(?<![年月日])[\u4e00-\u9fff]{2,8}省[\u4e00-\u9fff]{2,12}县[\u4e00-\u9fff]{1,12}镇[\u4e00-\u9fff]{1,12}村",
            compact,
        ):
            candidates.append(clean_location(match.group(0)))
        for match in re.finditer(r"[\u4e00-\u9fff]{2,12}镇[\u4e00-\u9fff]{1,12}村", compact):
            candidates.append(clean_location(match.group(0)))

        for match in re.finditer(r"(?:19|20)\d{2}年\d{1,2}月至(?:19|20)\d{2}年\d{1,2}月", compact):
            candidates.append(match.group(0))
        for match in re.finditer(r"(?:19|20)\d{2}年", compact):
            candidates.append(match.group(0))

        altitude = re.search(r"海拔(?:约|为)?\s*(\d+(?:\.\d+)?)\s*m", raw, flags=re.IGNORECASE)
        if altitude:
            candidates.append(f"海拔{altitude.group(1)}m")
            candidates.append("海拔")

        soil_type = re.search(r"土壤为([\u4e00-\u9fffA-Za-z0-9-]{2,16})", compact)
        if soil_type:
            candidates.append(soil_type.group(1))
        fertility = re.search(r"土壤肥力([\u4e00-\u9fffA-Za-z0-9-]{1,12})", compact)
        if fertility:
            candidates.append(f"土壤肥力{fertility.group(1)}")
        ph = re.search(r"pH\s*([0-9]+(?:\.[0-9]+)?)", raw, flags=re.IGNORECASE)
        if ph:
            candidates.append(f"pH{ph.group(1)}")

        wheat_variety = re.search(r"供试小麦品种为([\u4e00-\u9fffA-Za-z0-9-]{2,24})", compact)
        if wheat_variety:
            candidates.append(wheat_variety.group(1))
        equipment = re.search(r"施药器械为([\u4e00-\u9fffA-Za-z0-9 ./%-]{2,32})", raw)
        if equipment:
            candidates.append(equipment.group(1).strip())

        for match in re.finditer(r"\d+(?:\.\d+)?\s*(?:%|g/L|mg/L)?\s*[\u4e00-\u9fff]{2,12}\s*(?:WP|SC|EC|ME|SE|WG)?", raw):
            value = match.group(0).strip()
            if re.search(r"(酮|醇|唑|环唑|药剂|WP|SC|EC)", value):
                candidates.append(value)

        for match in re.finditer(r"[\u4e00-\u9fff]{2,24}(?:公司|中心|研究所|学院|大学|合作社|有限公司)", compact):
            candidates.append(match.group(0))

        author_line = re.match(r"^([\u4e00-\u9fff，、,\s]{4,80})（([^）]{4,80})）", raw.strip())
        if author_line:
            for name in re.split(r"[，、,\s]+", author_line.group(1)):
                if 2 <= len(name) <= 4:
                    candidates.append(name)
            org = author_line.group(2).split("，")[0].strip()
            if org:
                candidates.append(org)

        if "试验" in compact:
            candidates.append("试验")
        if "供试药剂" in compact:
            candidates.append("供试药剂")
        if "供试小麦品种" in compact:
            candidates.append("供试小麦品种")
        if "施药器械" in compact:
            candidates.append("施药器械")

        return normalize_entity_values(candidates, max_items=64)

    def _extract_relations(
        self,
        text: str,
        entities: List[str],
        reasoning_profile: Optional[str] = None,
        use_llm: bool = True,
        document_profile: Optional[Dict[str, Any]] = None,
        chunk_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, object]]:
        if len(entities) < 2:
            return []
        from services.llm_relation_extractor import llm_relation_extractor  # 惰性导入（openai 依赖）

        llm_relations = (
            llm_relation_extractor.extract(
                text,
                entities,
                reasoning_profile=reasoning_profile,
                document_profile=document_profile,
                chunk_metadata=chunk_metadata,
            )
            if use_llm
            else []
        )
        relations = [
            item
            for item in (
                evidence_validator.validate_relation(rel, text, require_evidence=True)
                for rel in llm_relations
            )
            if item
        ]
        high_value_relations = self._extract_high_value_relations(text, entities)
        if high_value_relations:
            relations = (relations or []) + high_value_relations
        stage_relations = self._extract_stage_relations(text, entities)
        if stage_relations:
            relations = (relations or []) + stage_relations
        if not relations:
            # LLM 失败时回退到规则抽取，避免图谱只剩 Chunk -> Entity 的结构。
            relations = self._extract_relations_by_rules(text, entities)
        return self._normalize_relations(relations)

    @staticmethod
    def _extract_high_value_relations(text: str, entities: List[str]) -> List[Dict[str, object]]:
        raw = text or ""
        compact = re.sub(r"\s+", "", raw)
        entity_keys = {re.sub(r"\s+", "", item).lower(): item for item in entities if item}
        relations: List[Dict[str, object]] = []
        seen = set()

        def canonical(name: object) -> str:
            normalized = normalize_entity_name(name)
            if not normalized:
                return ""
            return entity_keys.get(re.sub(r"\s+", "", normalized).lower(), normalized)

        def add(source: object, target: object, label: str, confidence: float, terms: List[str]) -> None:
            source_name = canonical(source)
            target_name = canonical(target)
            if not source_name or not target_name or source_name == target_name:
                return
            key = (source_name.lower(), target_name.lower(), label)
            if key in seen:
                return
            evidence = evidence_validator.find_evidence(raw, terms, require_all=False)
            if not evidence:
                return
            seen.add(key)
            relations.append(
                {
                    "source": source_name,
                    "target": target_name,
                    "label": label,
                    "confidence": confidence,
                    "evidence": evidence,
                }
            )

        experiment = "试验" if "试验" in compact else ""
        location = ""
        location_match = re.search(
            r"在([\u4e00-\u9fff]{2,8}省[\u4e00-\u9fff]{2,12}县[\u4e00-\u9fff]{1,12}镇[\u4e00-\u9fff]{1,12}村)进行",
            compact,
        )
        if location_match:
            location = location_match.group(1)
        elif experiment:
            fallback_location = re.search(r"在([\u4e00-\u9fff]{2,12}镇[\u4e00-\u9fff]{1,12}村)进行", compact)
            if fallback_location:
                location = fallback_location.group(1)

        time_match = re.search(r"((?:19|20)\d{2}年\d{1,2}月至(?:19|20)\d{2}年\d{1,2}月)", compact)
        if experiment and location:
            add(experiment, location, "地点", 0.9, [location, "试验"])
        if experiment and time_match:
            add(experiment, time_match.group(1), "时间", 0.9, [time_match.group(1), "试验"])

        anchor = location or experiment or "试验"
        altitude_match = re.search(r"海拔(?:约|为)?\s*(\d+(?:\.\d+)?)\s*m", raw, flags=re.IGNORECASE)
        if altitude_match:
            add(anchor, f"海拔{altitude_match.group(1)}m", "海拔", 0.9, ["海拔", altitude_match.group(1)])

        soil_type = re.search(r"土壤为([\u4e00-\u9fffA-Za-z0-9-]{2,16})", compact)
        if soil_type:
            add(anchor, soil_type.group(1), "土壤类型", 0.86, ["土壤", soil_type.group(1)])
        fertility = re.search(r"土壤肥力([\u4e00-\u9fffA-Za-z0-9-]{1,12})", compact)
        if fertility:
            add(anchor, f"土壤肥力{fertility.group(1)}", "土壤肥力", 0.84, ["土壤肥力", fertility.group(1)])
        ph = re.search(r"pH\s*([0-9]+(?:\.[0-9]+)?)", raw, flags=re.IGNORECASE)
        if ph:
            add(anchor, f"pH{ph.group(1)}", "土壤pH", 0.84, ["pH", ph.group(1)])

        variety = re.search(r"供试小麦品种为([\u4e00-\u9fffA-Za-z0-9-]{2,24})", compact)
        if variety:
            add("供试小麦品种", variety.group(1), "供试品种", 0.88, ["供试小麦品种", variety.group(1)])
            if experiment:
                add(experiment, variety.group(1), "供试品种", 0.78, ["供试小麦品种", variety.group(1)])

        provider = re.search(r"均由([\u4e00-\u9fff]{2,24}(?:公司|中心|研究所|学院|大学|合作社|有限公司))提供", compact)
        if provider:
            add("供试药剂", provider.group(1), "提供方", 0.88, ["供试药剂", provider.group(1), "提供"])

        equipment = re.search(r"施药器械为([\u4e00-\u9fffA-Za-z0-9 ./%-]{2,32})", raw)
        if equipment:
            add("施药器械", equipment.group(1).strip(), "使用", 0.82, ["施药器械", equipment.group(1).strip()])

        author_line = re.match(r"^([\u4e00-\u9fff，、,\s]{4,80})（([^）]{4,80})）", raw.strip())
        if author_line:
            org = author_line.group(2).split("，")[0].strip()
            for name in re.split(r"[，、,\s]+", author_line.group(1)):
                if 2 <= len(name) <= 4:
                    add(name, org, "工作单位", 0.9, [name, org])

        return relations

    @staticmethod
    def _merge_relations(
        *groups: List[Dict[str, object]],
        max_relations: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        merged: List[Dict[str, object]] = []
        seen = set()
        for group in groups:
            for rel in group:
                source = normalize_entity_name(rel.get("source") or "")
                target = normalize_entity_name(rel.get("target") or "")
                rel_type = str(rel.get("rel_type") or rel.get("relation_type") or rel.get("label") or "").strip()
                key = (source.lower(), target.lower(), rel_type.lower())
                if not source or not target or not rel_type or key in seen:
                    continue
                seen.add(key)
                merged.append(rel)
                if max_relations is not None and len(merged) >= max_relations:
                    return merged
        return merged

    def _normalize_relations(
        self,
        relations: List[Dict[str, object]],
        *,
        max_relations: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        normalized: List[Dict[str, object]] = []
        seen = set()
        limit = max_relations if max_relations is not None else settings.llm_max_relations
        for rel in relations:
            source = normalize_entity_name(rel.get("source") or "")
            target = normalize_entity_name(rel.get("target") or "")
            label = str(rel.get("label") or rel.get("relation") or rel.get("type") or "").strip()
            if not source or not target or not label:
                continue
            if source == target:
                continue
            rel_type = self._normalize_relation_type(label)
            confidence = rel.get("confidence")
            if isinstance(confidence, (int, float)) and float(confidence) < MIN_DEFAULT_RELATION_CONFIDENCE:
                continue
            if (
                label not in SCHEMA_RELATION_LABELS
                and label not in RULE_RELATION_KEYWORDS
                and (not isinstance(confidence, (int, float)) or float(confidence) < 0.8)
            ):
                continue
            key = (source.lower(), target.lower(), rel_type)
            if key in seen:
                continue
            seen.add(key)
            item: Dict[str, object] = {
                "source": source,
                "target": target,
                "label": label,
                "rel_type": rel_type,
                "relation_type": label,
            }
            if isinstance(confidence, (int, float)):
                item["confidence"] = float(confidence)
            evidence = str(rel.get("evidence") or "").strip()
            if evidence:
                item["evidence"] = evidence[:1000]
            normalized.append(item)
            if len(normalized) >= limit:
                break
        return normalized

    def _extract_relations_by_rules(self, text: str, entities: List[str]) -> List[Dict[str, object]]:
        relations = self._extract_stage_relations(text, entities)
        seen_pairs = {(item.get("source"), item.get("target"), item.get("label")) for item in relations}

        positions: List[tuple[int, int, str]] = []
        for entity in entities:
            if not entity:
                continue
            escaped = re.escape(entity)
            for match in re.finditer(escaped, text):
                positions.append((match.start(), match.end(), entity))
                # 每个实体保留前两个命中即可，避免大文本组合爆炸
                if sum(1 for p in positions if p[2] == entity) >= 2:
                    break
        positions.sort(key=lambda item: item[0])
        if len(positions) < 2:
            return relations

        max_gap = 80

        for idx, (left_start, left_end, left_entity) in enumerate(positions):
            for right_start, right_end, right_entity in positions[idx + 1 :]:
                if right_start <= left_end:
                    continue
                gap = right_start - left_end
                if gap > max_gap:
                    break
                if left_entity == right_entity:
                    continue
                between = text[left_end:right_start]
                label = self._infer_relation_label(between)
                relations.append(
                    {
                        "source": left_entity,
                        "target": right_entity,
                        "label": label,
                        "confidence": 0.45 if label != "同段提及" else 0.25,
                        "evidence": self._relation_evidence_from_positions(text, left_start, right_end),
                    }
                )

        if relations:
            return relations

        # 兜底：按实体出现顺序构造弱关系，确保实体层至少有可浏览连边。
        ordered_entities = [item[2] for item in positions]
        deduped_entities: List[str] = []
        seen = set()
        for item in ordered_entities:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped_entities.append(item)
        fallback: List[Dict[str, object]] = []
        for i in range(len(deduped_entities) - 1):
            fallback.append(
                {
                    "source": deduped_entities[i],
                    "target": deduped_entities[i + 1],
                    "label": "同段提及",
                    "confidence": 0.2,
                    "evidence": evidence_validator.find_evidence(text, [deduped_entities[i], deduped_entities[i + 1]]),
                }
            )
            if len(fallback) >= settings.llm_max_relations:
                break
        return fallback

    @staticmethod
    def _relation_evidence_from_positions(text: str, start: int, end: int, limit: int = 240) -> str:
        cleaned = text or ""
        left = max(0, start - 60)
        right = min(len(cleaned), end + 60)
        snippet = re.sub(r"\s+", " ", cleaned[left:right]).strip()
        return snippet[:limit]

    @staticmethod
    def _infer_relation_label(window: str) -> str:
        compact = re.sub(r"\s+", "", window or "")
        if not compact:
            return "同段提及"

        for keyword in RULE_RELATION_KEYWORDS:
            if keyword in compact:
                return keyword
        return "同段提及"

    def _extract_stage_relations(self, text: str, entities: List[str]) -> List[Dict[str, object]]:
        compact = re.sub(r"\s+", "", text or "")
        relations: List[Dict[str, object]] = []
        seen_pairs = set()

        stage_terms = {term for term in STAGE_KEYWORDS if term in compact}
        # 兼容“抽穗-扬花期 / 抽穗至扬花期 / 抽穗/扬花期”等写法
        if re.search(r"抽穗(?:至|—|-|~|–|/)?扬花期", compact):
            stage_terms.add("抽穗扬花期")
        if re.search(r"抽穗(?:至|—|-|~|–|/)?开花期", compact):
            stage_terms.add("抽穗开花期")
        disease_terms: List[str] = []
        for entity in entities:
            entity_clean = str(entity or "").strip()
            if not entity_clean:
                continue
            if entity_clean.endswith("病") and entity_clean.replace(" ", "") in compact:
                disease_terms.append(entity_clean)
        if "赤霉病" in compact and "赤霉病" not in disease_terms:
            disease_terms.append("赤霉病")

        for disease in disease_terms:
            for stage in stage_terms:
                key = (disease, stage, "高发期")
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                relations.append(
                    {
                        "source": disease,
                        "target": stage,
                        "label": "高发期",
                        "confidence": 0.75,
                        "evidence": evidence_validator.find_evidence(text, [disease, stage]),
                    }
                )

        return relations

    @staticmethod
    def _normalize_relation_type(label: str) -> str:
        text = str(label or "").strip()
        if not text:
            return "RELATION"
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^\w\u4e00-\u9fff]", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        if not text:
            suffix = hashlib.sha1(label.encode("utf-8", errors="ignore")).hexdigest()[:8]
            return f"REL_{suffix}"
        if re.match(r"^[\d_]", text):
            text = f"REL_{text}"
        if len(text) > 32:
            suffix = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:6]
            text = f"{text[:24]}_{suffix}"
        if re.match(r"^[A-Za-z0-9_]+$", text):
            return text.upper()
        return text

    def _check_apoc_available(self, session) -> bool:
        try:
            session.run("RETURN apoc.version() AS version").single()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "APOC 未启用，动态关系类型已关闭",
                context={"error": str(exc)},
            )
            return False

    def _ensure_schema(self, session) -> None:
        """约束：Entity 唯一键从全局 name 改为 (entity_key, kb_id) 复合唯一（契约 §2.2）。"""
        try:
            session.run(
                "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT entity_scope_key IF NOT EXISTS FOR (e:Entity) REQUIRE (e.entity_key, e.kb_id) IS UNIQUE"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建约束失败", context={"error": str(exc)})

        # 旧全局 name 唯一约束会阻止多 KB 同名实体共存；全新初始化下必须移除。
        try:
            session.run("DROP CONSTRAINT entity_name IF EXISTS")
        except Exception as exc:  # noqa: BLE001
            logger.warning("移除旧 entity_name 唯一约束失败", context={"error": str(exc)})

        try:
            session.run(
                "CREATE FULLTEXT INDEX chunkText IF NOT EXISTS FOR (c:Chunk) ON EACH [c.text]"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("创建全文索引失败", context={"error": str(exc)})

        for cypher in (
            "CREATE INDEX document_source IF NOT EXISTS FOR (d:Document) ON (d.source)",
            "CREATE INDEX document_kb_id IF NOT EXISTS FOR (d:Document) ON (d.kb_id)",
            "CREATE INDEX chunk_doc_id IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id)",
            "CREATE INDEX chunk_kb_id IF NOT EXISTS FOR (c:Chunk) ON (c.kb_id)",
            "CREATE INDEX chunk_kb_doc IF NOT EXISTS FOR (c:Chunk) ON (c.kb_id, c.doc_id)",
            "CREATE INDEX chunk_source IF NOT EXISTS FOR (c:Chunk) ON (c.source)",
            "CREATE INDEX entity_kb_id IF NOT EXISTS FOR (e:Entity) ON (e.kb_id)",
            "CREATE INDEX entity_source IF NOT EXISTS FOR (e:Entity) ON (e.source)",
        ):
            try:
                session.run(cypher)
            except Exception as exc:  # noqa: BLE001
                logger.warning("创建文档图谱索引失败", context={"query": cypher, "error": str(exc)})

    @staticmethod
    def _batch(items: List[Dict], size: int) -> List[List[Dict]]:
        return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = ["DocumentGraphService"]
