#!/usr/bin/env python3
"""Unit-style checks for document parser adapters."""
from __future__ import annotations

import sys
import tempfile
import json
from pathlib import Path
from unittest.mock import patch


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_native_text_and_json() -> None:
    from services.document_parser import NativeDocumentParser

    parser = NativeDocumentParser()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        text_file = root / "sample.txt"
        text_file.write_text("hello native parser", encoding="utf-8")
        parsed_text = parser.parse(text_file)
        _assert(parsed_text.text == "hello native parser", parsed_text.text)
        _assert(parsed_text.parser_provider == "native", parsed_text.parser_provider)

        json_file = root / "sample.json"
        json_file.write_text('{"name":"小麦","value":1}', encoding="utf-8")
        parsed_json = parser.parse(json_file)
        _assert('"name": "小麦"' in parsed_json.text, parsed_json.text)


def _check_mineru_response_parsing() -> None:
    from services.document_parser import MinerUDocumentParser

    class _FakeResponse:
        status_code = 200
        text = ""
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "data": {
                    "md_content": "# 标题\n\n正文内容",
                    "content_list": [
                        {
                            "type": "text",
                            "text": "正文内容",
                            "page_idx": 2,
                            "heading_path": ["标题"],
                            "source_location": "page=2,bbox=0,0,10,10",
                        }
                    ],
                },
                "warnings": ["low_confidence_table"],
            }

    class _FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _FakeResponse()

    parser = MinerUDocumentParser(
        {
            "base_url": "http://mineru.local",
            "endpoint_path": "/file_parse",
            "parse_mode": "pipeline",
            "output_format": "markdown,json",
            "timeout_seconds": 1,
            "parser_version": "mineru-test",
        }
    )
    with tempfile.TemporaryDirectory() as tmp, patch("services.document_parser.httpx.Client", _FakeClient):
        pdf_file = Path(tmp) / "sample.pdf"
        pdf_file.write_bytes(b"%PDF-1.4\n")
        parsed = parser.parse(pdf_file)

    _assert(parsed.text.startswith("# 标题"), parsed.text)
    _assert(parsed.parser_provider == "mineru", parsed.parser_provider)
    _assert(parsed.parser_version == "mineru-test", parsed.parser_version)
    _assert(parsed.blocks[0].page_start == 2, parsed.blocks[0])
    _assert(parsed.blocks[0].heading_path == ["标题"], parsed.blocks[0])
    _assert("low_confidence_table" in parsed.warnings, parsed.warnings)
    _assert(parsed.raw_payload is not None, "MinerU raw payload should be retained for artifact persistence")


def _check_manager_fallback_to_native() -> None:
    from services.document_parser import DocumentParserManager, ParsedDocument

    class _FakeNative:
        def parse(self, path):
            return ParsedDocument(text=f"fallback:{path.name}", parser_provider="native")

    manager = DocumentParserManager(
        {
            "provider": "mineru",
            "fallback_provider": "native",
            "base_url": "",
            "endpoint_path": "/file_parse",
            "parse_mode": "pipeline",
            "timeout_seconds": 1,
        }
    )
    manager.native = _FakeNative()

    with tempfile.TemporaryDirectory() as tmp:
        pdf_file = Path(tmp) / "broken.pdf"
        pdf_file.write_bytes(b"not really a pdf")
        parsed = manager.parse(pdf_file)

    _assert(parsed.parser_provider == "native", parsed.parser_provider)
    _assert(parsed.text == "fallback:broken.pdf", parsed.text)
    _assert(parsed.warnings and parsed.warnings[0].startswith("fallback_from_mineru"), parsed.warnings)


def _check_manager_fallback_none_fails() -> None:
    from services.document_parser import DocumentParserError, DocumentParserManager

    manager = DocumentParserManager(
        {
            "provider": "mineru",
            "fallback_provider": "none",
            "base_url": "",
            "endpoint_path": "/file_parse",
            "parse_mode": "pipeline",
            "timeout_seconds": 1,
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        pdf_file = Path(tmp) / "broken.pdf"
        pdf_file.write_bytes(b"not really a pdf")
        try:
            manager.parse(pdf_file)
        except DocumentParserError as exc:
            _assert("mineru_base_url_missing" in str(exc), str(exc))
        else:
            raise AssertionError("fallback_provider=none should fail when MinerU is unavailable")


def _check_parsed_document_artifacts() -> None:
    import services.document_graph_service as graph_module
    from services.document_graph_service import DocumentGraphService
    from services.document_parser import ParsedBlock, ParsedDocument

    service = DocumentGraphService()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graph_module.settings.parsed_document_storage_path = str(root / "parsed_documents")
        source = root / "sample.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        parsed = ParsedDocument(
            text="# 标题\n\n正文内容",
            parser_provider="mineru",
            parser_version="3.4.0",
            parse_mode="auto",
            blocks=[
                ParsedBlock(
                    text="正文内容",
                    block_type="text",
                    heading_path=["标题"],
                    page_start=1,
                    page_end=1,
                    source_location="page=1,bbox=0,0,10,10",
                )
            ],
            warnings=["sample_warning"],
            raw_payload={"data": {"md_content": "# 标题\n\n正文内容"}},
        )
        artifact_dir = service._write_parsed_document_artifacts(
            doc=source,
            doc_id="doc-1",
            kb_id="kb-1",
            parsed=parsed,
            content_hash="hash-1",
            chunks=[
                {
                    "chunk_id": "doc-1-000",
                    "doc_id": "doc-1",
                    "index": 0,
                    "text": "正文内容",
                    "parser_provider": "mineru",
                    "entities": ["小麦"],
                    "relations": [],
                }
            ],
        )

        _assert((artifact_dir / "manifest.json").exists(), "manifest should exist")
        _assert((artifact_dir / "content.md").read_text(encoding="utf-8").startswith("# 标题"), "content.md mismatch")
        _assert((artifact_dir / "blocks.json").exists(), "blocks.json should exist")
        _assert((artifact_dir / "chunks.jsonl").exists(), "chunks.jsonl should exist")
        _assert((artifact_dir / "structured_chunks.jsonl").exists(), "structured_chunks.jsonl should exist")
        _assert((artifact_dir / "document_profile.json").exists(), "document_profile.json should exist")
        _assert((artifact_dir / "extraction_schema.json").exists(), "extraction_schema.json should exist")
        _assert((artifact_dir / "extraction_plan.json").exists(), "extraction_plan.json should exist")
        _assert((artifact_dir / "raw.json").exists(), "raw.json should exist")
        manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
        _assert(manifest["parser_provider"] == "mineru", manifest)
        _assert(manifest["raw_output_path"] == "raw.json", manifest)
        _assert(manifest["structured_chunks_path"] == "structured_chunks.jsonl", manifest)
        _assert(manifest["document_profile_path"] == "document_profile.json", manifest)
        _assert(manifest["extraction_schema_path"] == "extraction_schema.json", manifest)
        _assert(manifest["extraction_plan_path"] == "extraction_plan.json", manifest)
        chunk_line = json.loads((artifact_dir / "chunks.jsonl").read_text(encoding="utf-8").strip())
        _assert("entities" not in chunk_line, chunk_line)
        _assert("relations" not in chunk_line, chunk_line)

        service._delete_parsed_document_artifacts("doc-1", "kb-1")
        _assert(not artifact_dir.exists(), "delete should remove parsed artifact directory")

        another_dir = service._write_parsed_document_artifacts(
            doc=source,
            doc_id="doc-2",
            kb_id="kb-1",
            parsed=parsed,
            content_hash="hash-2",
            chunks=[],
        )
        _assert(another_dir.exists(), "second artifact directory should exist")
        service._clear_parsed_document_artifacts("kb-1")
        _assert(not another_dir.exists(), "clear should remove parsed artifact directories")


def _check_structured_chunker_keeps_tables() -> None:
    from services.document_parser import ParsedBlock, ParsedDocument
    from services.knowledge_discovery.chunking import StructuredChunker

    parsed = ParsedDocument(
        text=(
            "# 文档标题\n\n"
            "[摘 要] 这是摘要。\n\n"
            "## 2 结果与分析\n\n"
            "从表 2 可知，处理 A 效果最好。\n\n"
            "表 2 不同处理效果\n\n"
            "<table><tr><td>处理</td><td>防效</td></tr>"
            "<tr><td>处理 A</td><td>90%</td></tr></table>\n\n"
            "注：数值越高效果越好。\n\n"
            "## 3 小结\n\n"
            "推荐处理 A。"
        ),
        parser_provider="mineru",
        parser_version="test",
        parse_mode="auto",
        blocks=[ParsedBlock(text="sample", source_location="sample.pdf")],
    )

    chunks = StructuredChunker(max_chars=200).chunk(parsed, doc_id="doc-1")
    tables = [chunk for chunk in chunks if chunk.block_type == "table"]
    _assert(len(tables) == 1, [chunk.to_dict() for chunk in chunks])
    _assert(tables[0].caption == "表 2 不同处理效果", tables[0].to_dict())
    _assert(tables[0].table_columns == ["处理", "防效"], tables[0].to_dict())
    _assert(tables[0].table_rows[0]["处理"] == "处理 A", tables[0].to_dict())
    _assert(tables[0].heading_path == ["文档标题", "2 结果与分析"], tables[0].to_dict())
    _assert("从表 2 可知" in tables[0].neighbor_before, tables[0].to_dict())
    _assert("注：数值越高" in tables[0].neighbor_after, tables[0].to_dict())


def _check_entity_normalization_and_table_relations() -> None:
    from services.document_graph_service import DocumentGraphService
    from services.knowledge_discovery.chunking import StructuredChunk
    from services.knowledge_discovery.normalization import normalize_entity_values

    dirty = [
        {"entity": "125 g/L 氟环唑 SC", "type": "药剂"},
        '{"entity": "小麦条锈病", "type": "病害"}',
        "type: 药剂",
        "[",
    ]
    clean = normalize_entity_values(dirty, max_items=10)
    _assert("125g/L氟环唑SC" in clean, clean)
    _assert("小麦条锈病" in clean, clean)
    _assert(normalize_entity_values(["25% 三唑酮 WP"], max_items=1) == ["25%三唑酮WP"], clean)
    _assert(all("entity" not in item for item in clean), clean)
    _assert("[" not in clean, clean)

    service = DocumentGraphService()
    chunk = StructuredChunk(
        text="表 2 不同处理效果\n<table></table>",
        block_type="table",
        heading_path=["2 结果与分析"],
        caption="表 2 不同药剂处理小麦条锈病的防效",
        table_columns=["处理", "平均防效"],
        table_rows=[{"处理": "125 g/L氟环唑SC", "平均防效": "89.93 a"}],
    )
    relations = service._extract_table_relations(chunk)
    _assert(any(item["label"] == "平均防效" for item in relations), relations)
    normalized = service._normalize_relations(relations)
    _assert(any(item["target"] == "平均防效: 89.93 a" for item in normalized), normalized)
    _assert(any(item.get("evidence") for item in normalized), normalized)

    llm_text = service._llm_extraction_text(chunk)
    _assert("表题：表 2 不同药剂处理小麦条锈病的防效" in llm_text, llm_text)
    _assert("列：处理 | 平均防效" in llm_text, llm_text)
    _assert("第1行：处理=125 g/L氟环唑SC；平均防效=89.93 a" in llm_text, llm_text)


def _check_document_profiler_and_schema_assets() -> None:
    from services.document_parser import ParsedBlock, ParsedDocument
    from services.knowledge_discovery.chunking import StructuredChunker
    from services.knowledge_discovery.extraction import build_extraction_schema
    from services.knowledge_discovery.profiling import document_profiler
    from services.llm_relation_extractor import LLMRelationExtractor

    parsed = ParsedDocument(
        text=(
            "# 2种新型化学药剂防治小麦条锈病和赤霉病效果\n\n"
            "摘要：为筛选高效药剂，开展田间试验。\n\n"
            "## 1 材料与方法\n\n"
            "试验在贵州省金沙县进行。\n\n"
            "## 2 结果与分析\n\n"
            "表 2 不同药剂处理小麦条锈病的防效。"
        ),
        parser_provider="mineru",
        blocks=[ParsedBlock(text="sample")],
    )
    chunks = StructuredChunker(max_chars=400).chunk(parsed, doc_id="doc-1")
    profile = document_profiler.profile(parsed, structured_chunks=chunks, file_name="sample.pdf").to_dict()
    _assert(profile["document_type"] == "academic_paper", profile)
    _assert(profile["domain"] == "agricultural_plant_protection", profile)
    _assert("指标" in profile["suggested_entity_types"], profile)

    schema = build_extraction_schema(profile).to_prompt_payload()
    relation_names = [item["name"] for item in schema["relation_types"]]
    _assert("指标结果" in relation_names, schema)

    prompt = LLMRelationExtractor._build_schema_prompt(profile)
    _assert("动态 schema" in prompt, prompt)
    _assert("学术论文" in prompt or "试验报告" in prompt, prompt)


def _check_extraction_planner_selects_high_value_chunks() -> None:
    from services.knowledge_discovery.chunking import StructuredChunk
    from services.knowledge_discovery.extraction import extraction_planner

    chunks = [
        StructuredChunk(text="普通背景说明，介绍研究现状。", block_type="section", heading_path=["1 引言"]),
        StructuredChunk(text="普通材料描述，无指标无结论。", block_type="section", heading_path=["1 引言"]),
        StructuredChunk(text="结果与分析显示，125g/L氟环唑SC平均防效为89.93%，产量最高。", block_type="section", heading_path=["2 结果与分析"]),
        StructuredChunk(text="小结：建议优先推广125g/L氟环唑SC防治小麦条锈病。", block_type="section", heading_path=["3 小结"]),
        StructuredChunk(
            text="表 2 不同药剂处理小麦条锈病的防效\n<table></table>",
            block_type="table",
            heading_path=["2 结果与分析"],
            caption="表 2 不同药剂处理小麦条锈病的防效",
        ),
    ]
    profile = {
        "document_type": "academic_paper",
        "domain": "agricultural_plant_protection",
        "main_topics": ["小麦条锈病", "氟环唑SC"],
        "important_sections": ["2 结果与分析", "3 小结"],
    }
    plan = extraction_planner.plan(
        chunks,
        document_profile=profile,
        reasoning_profile="fast",
        complex_extraction=False,
        base_llm_budget=2,
    )
    selected = [item.index for item in plan.items if item.use_llm]
    _assert(selected == [2, 3], [item.to_dict() for item in plan.items])
    table_item = plan.item_for(4)
    _assert(table_item.use_llm is False and table_item.strategy == "structured_table", table_item.to_dict())

    balanced_plan = extraction_planner.plan(
        chunks,
        document_profile=profile,
        reasoning_profile="balanced",
        complex_extraction=True,
        base_llm_budget=2,
    )
    balanced_table_item = balanced_plan.item_for(4)
    _assert(balanced_table_item.use_llm is True, balanced_table_item.to_dict())
    _assert(balanced_table_item.strategy == "structured_table_plus_llm", balanced_table_item.to_dict())


def _check_graph_extractors_use_runtime_model_config() -> None:
    from config import get_settings
    from services.llm_entity_extractor import LLMEntityExtractor
    from services.llm_relation_extractor import LLMRelationExtractor

    class _FakeClient:
        pass

    runtime = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://runtime.example/v1",
        "model": "runtime-model",
        "temperature": 0.2,
    }

    # 本用例只验证"运行时模型配置能被抽取器采用"。抽取器的 enabled 还合取了全局开关
    # settings.llm_enabled / llm_relation_enabled（CI 发布验收按无 LLM 口径跑，
    # LLM_ENABLED=0），不显式隔离就会把开关关掉的预期行为误报成回归。
    cfg = get_settings()
    with patch.object(cfg, "llm_enabled", True), patch.object(cfg, "llm_relation_enabled", True):
        with patch("services.llm_entity_extractor.get_ai_runtime_config", return_value=runtime), patch(
            "services.llm_entity_extractor.build_openai_client", return_value=_FakeClient()
        ):
            entity = LLMEntityExtractor()
            entity._refresh_runtime_config()
            _assert(entity.enabled is True, entity.enabled)
            _assert(entity.model == "runtime-model", entity.model)
            _assert(entity._resolved_model == "runtime-model", entity._resolved_model)

        with patch("services.llm_relation_extractor.get_ai_runtime_config", return_value=runtime), patch(
            "services.llm_relation_extractor.build_openai_client", return_value=_FakeClient()
        ):
            relation = LLMRelationExtractor()
            relation._refresh_runtime_config()
            _assert(relation.enabled is True, relation.enabled)
            _assert(relation.model == "runtime-model", relation.model)
            _assert(relation._resolved_model == "runtime-model", relation._resolved_model)


def _check_graph_extractor_reasoning_is_bounded() -> None:
    from services.llm_entity_extractor import LLMEntityExtractor
    from services.llm_relation_extractor import LLMRelationExtractor

    _assert(LLMEntityExtractor._bounded_graph_reasoning_profile("fast") == "fast", "fast should stay fast")
    _assert(LLMEntityExtractor._bounded_graph_reasoning_profile("balanced") == "fast", "balanced should use low effort")
    _assert(LLMEntityExtractor._bounded_graph_reasoning_profile("deep") == "balanced", "deep should cap at medium")
    _assert(LLMRelationExtractor._bounded_graph_reasoning_profile("balanced") == "fast", "balanced should use low effort")
    _assert(LLMRelationExtractor._bounded_graph_reasoning_profile("deep") == "balanced", "deep should cap at medium")


def _check_schema_aware_relations_and_evidence_validation() -> None:
    from services.document_graph_service import DocumentGraphService
    from services.knowledge_discovery.extraction import evidence_validator
    from services.llm_relation_extractor import LLMRelationExtractor

    extractor = LLMRelationExtractor()
    parsed = extractor._parse_relations(
        '[{"source":"125 g/L 氟环唑 SC","target":"小麦条锈病","label":"防治对象","evidence":"125g/L氟环唑SC对小麦条锈病平均防效为89.93%","confidence":"0.91"}]',
        {"125g/L氟环唑SC", "小麦条锈病"},
    )
    _assert(len(parsed) == 1, parsed)
    _assert(parsed[0]["source"] == "125g/L氟环唑SC", parsed)
    _assert(parsed[0]["evidence"], parsed)

    text = "125g/L氟环唑SC对小麦条锈病平均防效为89.93%。"
    valid = evidence_validator.validate_relation(parsed[0], text, require_evidence=True)
    _assert(valid is not None and valid.get("evidence"), valid)

    invalid = evidence_validator.validate_relation(
        {
            "source": "125g/L氟环唑SC",
            "target": "小麦条锈病",
            "label": "防治对象",
            "evidence": "这是一段原文不存在的解释性证据",
            "confidence": 0.9,
        },
        "本文只讨论试验设计，没有给出该药剂与病害的直接关系。",
        require_evidence=True,
    )
    _assert(invalid is None, invalid)

    service = DocumentGraphService()
    with patch(
        "services.llm_relation_extractor.llm_relation_extractor.extract",
        return_value=[
            {
                "source": "125g/L氟环唑SC",
                "target": "小麦条锈病",
                "label": "防治对象",
                "evidence": "125g/L氟环唑SC对小麦条锈病平均防效为89.93%",
                "confidence": 0.91,
            },
            {
                "source": "125g/L氟环唑SC",
                "target": "不存在证据",
                "label": "虚构关系",
                "evidence": "模型生成的解释",
                "confidence": 0.9,
            },
        ],
    ):
        relations = service._extract_relations(
            text,
            ["125g/L氟环唑SC", "小麦条锈病", "不存在证据"],
            reasoning_profile="balanced",
            use_llm=True,
        )
    _assert(any(item["label"] == "防治对象" and item.get("evidence") for item in relations), relations)
    _assert(not any(item["label"] == "虚构关系" for item in relations), relations)


def _check_relation_prompt_budget() -> None:
    from services.llm_relation_extractor import LLMRelationExtractor

    extractor = LLMRelationExtractor()
    extractor.text_budget = 80
    extractor.max_prompt_entities = 3
    text = "125 g/L 氟环唑 SC 用于防治小麦条锈病，平均防效为 89.93%。"
    prompt_text, prompt_entities = extractor._prepare_prompt_inputs(
        text,
        [
            "125g/L氟环唑SC",
            "小麦条锈病",
            "平均防效",
            "不存在于本文的实体A",
            "不存在于本文的实体B",
        ],
    )
    _assert(len(prompt_text) <= 80, prompt_text)
    _assert(prompt_entities == ["125g/L氟环唑SC", "小麦条锈病", "平均防效"], prompt_entities)


def _check_relation_timeout_circuit_breaker() -> None:
    from services.llm_relation_extractor import LLMRelationExtractor

    calls = {"count": 0}

    class _FakeModel:
        id = "runtime-model"

    class _FakeModels:
        def list(self):
            return type("ModelList", (), {"data": [_FakeModel()]})()

    class _FakeCompletions:
        def create(self, **_kwargs):
            calls["count"] += 1
            raise TimeoutError("Request timed out.")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        models = _FakeModels()
        chat = _FakeChat()

    runtime = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://runtime.example/v1",
        "model": "runtime-model",
        "temperature": 0.1,
    }

    with patch("services.llm_relation_extractor.get_ai_runtime_config", return_value=runtime), patch(
        "services.llm_relation_extractor.build_openai_client", return_value=_FakeClient()
    ):
        extractor = LLMRelationExtractor()
        text = "125g/L氟环唑SC用于防治小麦条锈病。"
        entities = ["125g/L氟环唑SC", "小麦条锈病"]
        _assert(extractor.extract(text, entities, reasoning_profile="balanced") == [], "first timeout should fallback")
        _assert(extractor.extract(text, entities, reasoning_profile="balanced") == [], "second timeout should fallback")
        _assert(extractor._disabled_until > 0, "timeout circuit should open after repeated timeouts")
        _assert(extractor.extract(text, entities, reasoning_profile="balanced") == [], "cooldown should short-circuit")
        _assert(calls["count"] == 2, calls)


def _check_high_value_experiment_fact_rules() -> None:
    from services.document_graph_service import DocumentGraphService

    service = DocumentGraphService()
    text = (
        "试验于 2022 年 10 月至 2023 年 5 月在贵州省金沙县茶园镇民乐村进行，"
        "海拔 850 m，土地平整，土壤为黄泥土，土壤肥力中等，pH 5.7。"
    )
    entities = service._extract_entities(text, use_llm=False)
    _assert("贵州省金沙县茶园镇民乐村" in entities, entities)
    _assert("2022年10月至2023年5月" in entities, entities)
    _assert("海拔850m" in entities, entities)
    _assert("黄泥土" in entities, entities)

    relations = service._extract_relations(text, entities, use_llm=False)
    triples = {(item["source"], item["label"], item["target"]) for item in relations}
    _assert(("试验", "地点", "贵州省金沙县茶园镇民乐村") in triples, relations)
    _assert(("试验", "时间", "2022年10月至2023年5月") in triples, relations)
    _assert(("贵州省金沙县茶园镇民乐村", "海拔", "海拔850m") in triples, relations)
    _assert(("贵州省金沙县茶园镇民乐村", "土壤类型", "黄泥土") in triples, relations)


def main() -> int:
    _check_native_text_and_json()
    _check_mineru_response_parsing()
    _check_manager_fallback_to_native()
    _check_manager_fallback_none_fails()
    _check_parsed_document_artifacts()
    _check_structured_chunker_keeps_tables()
    _check_entity_normalization_and_table_relations()
    _check_document_profiler_and_schema_assets()
    _check_extraction_planner_selects_high_value_chunks()
    _check_graph_extractors_use_runtime_model_config()
    _check_graph_extractor_reasoning_is_bounded()
    _check_schema_aware_relations_and_evidence_validation()
    _check_relation_prompt_budget()
    _check_relation_timeout_circuit_breaker()
    _check_high_value_experiment_fact_rules()
    print("DOCUMENT_PARSER_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
