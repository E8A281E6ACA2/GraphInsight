"""基于 Neo4j 文档片段的 LLM 问答服务"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from config import get_settings
from core import get_logger
from services.document_graph_service import DocumentGraphService
from services.model_runtime_policy import apply_reasoning_profile, normalize_reasoning_profile
from services.openai_client_factory import build_openai_client
from services.retrieval_orchestrator import retrieval_orchestrator
from services.runtime_config import get_ai_runtime_config

logger = get_logger()
settings = get_settings()

try:  # neo4j 驱动缺失时（单元测试环境）仍可加载本模块；调用诊断方法时才需要真实驱动
    from neo4j import Query
except Exception:  # pragma: no cover - 环境相关
    Query = None  # type: ignore[assignment]

DOCQA_DIAG_CONNECT_TIMEOUT_SECONDS = 3.0
DOCQA_DIAG_QUERY_TIMEOUT_SECONDS = 4.0


def _neo4j_service():
    """懒加载 Neo4j 服务，避免模块加载即导入 neo4j 驱动。"""
    from services.neo4j_service import get_neo4j_service

    return get_neo4j_service()


class DocQAService:
    def __init__(self) -> None:
        self.enabled = settings.llm_enabled and bool(settings.llm_api_key)
        self.model = settings.llm_qa_model
        self.temperature = settings.llm_qa_temperature
        self.max_tokens = settings.llm_qa_max_tokens
        self.max_context = settings.llm_qa_max_context
        self._client = None
        if self.enabled:
            self._client = build_openai_client(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url or None,
                timeout=30.0,
            )

    def _refresh_runtime_config(self) -> None:
        config = get_ai_runtime_config()
        enabled = bool(config.get("enabled", True))
        api_key = str(config.get("api_key") or "").strip()
        base_url = str(config.get("base_url") or settings.llm_base_url or "").strip()
        model = str(config.get("model") or settings.llm_qa_model or settings.llm_model or "").strip()
        temperature = float(config.get("temperature") or settings.llm_qa_temperature)
        max_tokens = int(config.get("max_tokens") or settings.llm_qa_max_tokens)
        signature = (enabled, api_key, base_url, model, temperature, max_tokens)
        if getattr(self, "_runtime_signature", None) == signature:
            return

        self.enabled = enabled and bool(api_key) and bool(model)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = None
        if self.enabled:
            self._client = build_openai_client(
                api_key=api_key,
                base_url=base_url or None,
                timeout=30.0,
            )
        self._runtime_base_url = base_url or "default"
        self._runtime_signature = signature

    def _base_url_label(self) -> str:
        return str(getattr(self, "_runtime_base_url", None) or settings.llm_base_url or "default")

    def answer(
        self,
        question: str,
        top_k: int,
        reasoning_profile: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        *,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """文档问答（M4）：kb_ids 必填，检索只使用作用域内的证据。"""
        self._refresh_runtime_config()
        history = self._normalize_conversation_history(conversation_history)
        retrieval_query = self._contextual_retrieval_query(question, history)
        retrieval_result = retrieval_orchestrator.retrieve(
            retrieval_query,
            top_k,
            kb_ids=kb_ids,
            document_ids=document_ids,
        )
        citations = retrieval_result["items"]
        active_profile = normalize_reasoning_profile(reasoning_profile, "balanced")
        trace = {
            "retrieval": {
                "query": question,
                "contextual_query": retrieval_query,
                "top_k": top_k,
                "scope": {"kb_ids": list(kb_ids), "document_ids": list(document_ids or [])},
                "count": len(citations),
                "conversation_turns": len(history),
                "chunks": self._snapshot_citations(citations),
                "orchestrator": retrieval_result.get("trace", {}),
            },
            "generation": {
                "mode": "not_started",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
            },
        }
        if not citations:
            trace["generation"]["mode"] = "skipped_no_citations"
            return {
                "answer": "当前文档库没有找到相关内容，请先上传文档或调整问题。",
                "citations": [],
                "trace": trace,
            }

        if not self.enabled or not self._client:
            trace["generation"]["mode"] = "skipped_llm_not_configured"
            return {
                "answer": "已找到相关文档片段，但当前未配置 LLM，无法生成完整回答。",
                "citations": citations,
                "trace": trace,
            }

        context_blocks = []
        for item in citations[: self.max_context]:
            context_blocks.append(
                f"SOURCE {item['id']} | {item['title']} | {item.get('location','')}\n{item['text']}"
            )

        prompt = (
            "你是文档问答助手，只能使用提供的 SOURCE 内容回答问题。"
            "对话历史只用于理解代词、指代和省略问题，不能作为事实来源。"
            "请给出简洁、结构化的回答。若信息不足，说明需要补充文档。"
            "输出 JSON 格式：{\"answer\":\"...\",\"used_chunk_ids\":[\"...\"]}"
        )
        conversation_context = self._format_conversation_context(history)

        try:
            user_content = f"问题：{question}\n\n上下文：\n" + "\n\n".join(context_blocks)
            if conversation_context:
                user_content = f"对话历史：\n{conversation_context}\n\n{user_content}"
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ]
            parsed = self._request_llm_json(messages, reasoning_profile=active_profile)
            usage = parsed.get("_usage") if isinstance(parsed, dict) else None
            answer = parsed.get("answer") or "已生成答案。"
            used_ids = [str(item) for item in (parsed.get("used_chunk_ids") or [])]
            trace["generation"] = {
                "mode": "llm_success",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
                "used_chunk_ids": used_ids,
                "context_chunk_ids": [str(item.get("id")) for item in citations[: self.max_context]],
                "usage": usage,
                "conversation_turns": len(history),
            }
            if used_ids:
                filtered = [c for c in citations if c["id"] in set(used_ids)]
                if filtered:
                    citations = filtered
            self._focus_citation_snippets(citations, question, answer)
            trace["response"] = {
                "answer_preview": answer[:800],
                "citation_ids": [str(item.get("id")) for item in citations],
            }
            return {"answer": answer, "citations": citations, "trace": trace}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "文档问答 LLM 失败",
                context={
                    "error": str(exc),
                    "model": self.model,
                    "base_url": self._base_url_label(),
                    "reasoning_profile": active_profile,
                },
            )
            answer = self._fallback_answer(question, citations)
            self._focus_citation_snippets(citations, question, answer)
            trace["generation"] = {
                "mode": "fallback_llm_error",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
                "error": str(exc),
                "conversation_turns": len(history),
            }
            trace["response"] = {
                "answer_preview": answer[:800],
                "citation_ids": [str(item.get("id")) for item in citations],
            }
            return {
                "answer": answer,
                "citations": citations,
                "trace": trace,
            }

    def deep_research(
        self,
        question: str,
        top_k: int = 8,
        max_sub_questions: int = 4,
        reasoning_profile: Optional[str] = None,
        *,
        kb_ids: List[str],
        document_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """深度调研：问题拆解 + 多轮检索 + 结构化报告（M4：检索按 kb_ids 过滤）。"""
        self._refresh_runtime_config()
        normalized_question = (question or "").strip()
        if not normalized_question:
            return {
                "question": "",
                "summary": "",
                "final_conclusion": "问题为空，请先输入调研问题。",
                "report": "问题为空，请先输入调研问题。",
                "sub_questions": [],
                "citations": [],
                "confidence": {
                    "score": 0.0,
                    "level": "low",
                    "reason": "未提供问题",
                },
                "evidence_stats": {
                    "sub_questions": 0,
                    "answered_sub_questions": 0,
                    "coverage_ratio": 0.0,
                    "retrieved_chunks": 0,
                    "unique_citations": 0,
                    "avg_citation_confidence": 0.0,
                },
            }

        sub_questions = self._decompose_question(normalized_question, max_sub_questions)
        if not sub_questions:
            sub_questions = [normalized_question]

        citation_map: Dict[str, Dict[str, Any]] = {}
        per_question_hits: List[Dict[str, Any]] = []
        total_retrieved = 0

        for sub_q in sub_questions:
            retrieval_result = retrieval_orchestrator.retrieve(
                sub_q,
                top_k,
                kb_ids=kb_ids,
                document_ids=document_ids,
            )
            hits = retrieval_result["items"]
            total_retrieved += len(hits)
            used_ids: List[str] = []
            for hit in hits:
                cid = str(hit.get("id"))
                if not cid:
                    continue
                used_ids.append(cid)
                if cid in citation_map:
                    citation_map[cid]["hit_count"] = int(citation_map[cid].get("hit_count") or 1) + 1
                    questions = citation_map[cid].setdefault("matched_questions", [])
                    if sub_q not in questions:
                        questions.append(sub_q)
                    if (hit.get("retrieval_score") or 0) > (citation_map[cid].get("retrieval_score") or 0):
                        citation_map[cid]["retrieval_score"] = hit.get("retrieval_score")
                    merged_entities = set(citation_map[cid].get("entity_names") or [])
                    merged_entities.update(hit.get("entity_names") or [])
                    citation_map[cid]["entity_names"] = sorted(merged_entities)
                else:
                    citation_map[cid] = {
                        **hit,
                        "hit_count": 1,
                        "matched_questions": [sub_q],
                    }

            per_question_hits.append(
                {
                    "sub_question": sub_q,
                    "hit_count": len(hits),
                    "chunk_ids": used_ids,
                    "retrieval_trace": retrieval_result.get("trace", {}),
                }
            )

        citations = sorted(
            citation_map.values(),
            key=lambda item: (
                int(item.get("hit_count") or 0),
                float(item.get("retrieval_score") or 0),
                len(item.get("text") or ""),
            ),
            reverse=True,
        )

        if not citations:
            trace = {
                "retrieval": {
                    "query": normalized_question,
                    "top_k": top_k,
                    "scope": {"kb_ids": list(kb_ids), "document_ids": list(document_ids or [])},
                    "sub_questions": sub_questions,
                    "coverage": per_question_hits,
                    "count": 0,
                    "chunks": [],
                },
                "generation": {
                    "mode": "skipped_no_citations",
                    "model": self.model,
                    "base_url": self._base_url_label(),
                },
            }
            return {
                "question": normalized_question,
                "summary": "未检索到可用证据。",
                "final_conclusion": "未检索到可用文档证据，建议先上传文档或调整调研范围。",
                "report": "未检索到可用文档证据，建议先上传文档或调整调研范围。",
                "sub_questions": sub_questions,
                "citations": [],
                "confidence": {
                    "score": 0.12,
                    "level": "low",
                    "reason": "当前证据为空",
                },
                "evidence_stats": {
                    "sub_questions": len(sub_questions),
                    "answered_sub_questions": 0,
                    "coverage_ratio": 0.0,
                    "retrieved_chunks": 0,
                    "unique_citations": 0,
                    "avg_citation_confidence": 0.0,
                },
                "coverage": per_question_hits,
                "trace": trace,
            }

        top_citations = citations[: max(self.max_context * 3, 8)]
        self._attach_citation_confidence(top_citations, len(sub_questions))

        answered_sub_questions = sum(1 for item in per_question_hits if int(item.get("hit_count") or 0) > 0)
        coverage_ratio = answered_sub_questions / max(1, len(sub_questions))
        avg_citation_confidence = sum(float(item.get("confidence") or 0) for item in top_citations) / max(1, len(top_citations))

        evidence_stats: Dict[str, Any] = {
            "sub_questions": len(sub_questions),
            "answered_sub_questions": answered_sub_questions,
            "coverage_ratio": round(coverage_ratio, 3),
            "retrieved_chunks": total_retrieved,
            "unique_citations": len(citations),
            "avg_citation_confidence": round(avg_citation_confidence, 3),
        }
        active_profile = normalize_reasoning_profile(reasoning_profile, "deep")
        trace = {
            "retrieval": {
                "query": normalized_question,
                "top_k": top_k,
                "scope": {"kb_ids": list(kb_ids), "document_ids": list(document_ids or [])},
                "sub_questions": sub_questions,
                "coverage": per_question_hits,
                "count": len(citations),
                "chunks": self._snapshot_citations(top_citations),
            },
            "generation": {
                "mode": "not_started",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
            },
            "evidence_stats": evidence_stats,
        }

        base_confidence = self._compute_base_confidence(
            coverage_ratio=coverage_ratio,
            unique_citations=len(citations),
            sub_question_count=len(sub_questions),
            avg_citation_confidence=avg_citation_confidence,
        )

        if not self.enabled or not self._client:
            report = self._fallback_deep_report(normalized_question, sub_questions, top_citations)
            summary = "LLM 未配置，已返回基于证据的结构化草案。"
            final_conclusion = self._fallback_conclusion(top_citations)
            trace["generation"]["mode"] = "skipped_llm_not_configured"
            trace["response"] = {
                "summary": summary[:500],
                "final_conclusion": final_conclusion[:500],
                "citation_ids": [str(item.get("id")) for item in top_citations],
            }
            return {
                "question": normalized_question,
                "summary": summary,
                "final_conclusion": final_conclusion,
                "report": report,
                "sub_questions": sub_questions,
                "citations": top_citations,
                "confidence": {
                    "score": base_confidence["score"],
                    "level": base_confidence["level"],
                    "reason": "未启用 LLM，基于证据覆盖率与引用质量计算",
                },
                "evidence_stats": evidence_stats,
                "coverage": per_question_hits,
                "trace": trace,
            }

        try:
            llm_result = self._generate_deep_report(
                normalized_question,
                sub_questions,
                top_citations,
                reasoning_profile=active_profile,
            )
            usage = llm_result.get("_usage") if isinstance(llm_result, dict) else None
            used_ids = [str(item) for item in llm_result.get("used_chunk_ids") or []]
            if used_ids:
                used_set = set(used_ids)
                selected_citations = [c for c in top_citations if str(c.get("id")) in used_set]
                if selected_citations:
                    top_citations = selected_citations
                    self._attach_citation_confidence(top_citations, len(sub_questions))
            trace["generation"] = {
                "mode": "llm_success",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
                "used_chunk_ids": used_ids,
                "sub_questions": sub_questions,
                "usage": usage,
            }

            summary = (llm_result.get("summary") or "").strip()
            report = (llm_result.get("report") or "").strip()
            final_conclusion = (llm_result.get("final_conclusion") or "").strip()

            if not report:
                report = self._fallback_deep_report(normalized_question, sub_questions, top_citations)
            if not summary:
                summary = "已生成深度调研报告。"
            if not final_conclusion:
                final_conclusion = self._fallback_conclusion(top_citations)

            llm_score = self._safe_score(llm_result.get("confidence_score"))
            llm_reason = str(llm_result.get("confidence_reason") or "").strip()
            final_conf = self._merge_confidence(base_confidence["score"], llm_score)
            confidence_reason = llm_reason or (
                "综合证据覆盖率、引用质量与模型判定"
                if llm_score is not None
                else "基于证据覆盖率与引用质量计算"
            )

            return {
                "question": normalized_question,
                "summary": summary,
                "final_conclusion": final_conclusion,
                "report": report,
                "sub_questions": sub_questions,
                "citations": top_citations,
                "confidence": {
                    "score": final_conf,
                    "level": self._confidence_level(final_conf),
                    "reason": confidence_reason,
                },
                "evidence_stats": evidence_stats,
                "coverage": per_question_hits,
                "trace": {
                    **trace,
                    "response": {
                        "summary": summary[:500],
                        "final_conclusion": final_conclusion[:500],
                        "confidence": {
                            "score": final_conf,
                            "level": self._confidence_level(final_conf),
                            "reason": confidence_reason,
                        },
                        "citation_ids": [str(item.get("id")) for item in top_citations],
                    },
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "深度调研 LLM 失败",
                context={
                    "error": str(exc),
                    "model": self.model,
                    "base_url": self._base_url_label(),
                    "reasoning_profile": active_profile,
                },
            )
            report = self._fallback_deep_report(normalized_question, sub_questions, top_citations)
            trace["generation"] = {
                "mode": "fallback_llm_error",
                "model": self.model,
                "base_url": self._base_url_label(),
                "reasoning_profile": active_profile,
                "error": str(exc),
            }
            return {
                "question": normalized_question,
                "summary": "LLM 调研生成失败，已返回基于证据的结构化草案。",
                "final_conclusion": self._fallback_conclusion(top_citations),
                "report": report,
                "sub_questions": sub_questions,
                "citations": top_citations,
                "confidence": {
                    "score": base_confidence["score"],
                    "level": base_confidence["level"],
                    "reason": "LLM 失败，已退回证据打分",
                },
                "evidence_stats": evidence_stats,
                "coverage": per_question_hits,
                "trace": {
                    **trace,
                    "response": {
                        "summary": "LLM 调研生成失败，已返回基于证据的结构化草案。",
                        "final_conclusion": self._fallback_conclusion(top_citations)[:500],
                        "citation_ids": [str(item.get("id")) for item in top_citations],
                    },
                },
            }

    def diagnose(self, probe_llm: bool = False) -> Dict[str, Any]:
        self._refresh_runtime_config()
        service = _neo4j_service()
        result: Dict[str, Any] = {
            "llm_enabled": self.enabled,
            "llm_model": self.model,
            "llm_base_url": self._base_url_label(),
            "probe_llm": probe_llm,
            "neo4j": {"ok": False},
            "retrieval_engine": retrieval_orchestrator.health(),
            "documents": {"count": 0},
            "chunks": {"count": 0},
            "entities": {"count": 0},
            "llm": {"ok": False, "configured": bool(self._client)},
        }

        try:
            service.driver.verify_connectivity()
            result["neo4j"] = {"ok": True}
            with service.driver.session() as session:
                doc_count = session.run(
                    Query(
                        "MATCH (d:Document {source:'document_ingest'}) RETURN count(d) AS c",
                        timeout=DOCQA_DIAG_QUERY_TIMEOUT_SECONDS,
                    )
                ).single()
                chunk_count = session.run(
                    Query(
                        "MATCH (c:Chunk {source:'document_ingest'}) RETURN count(c) AS c",
                        timeout=DOCQA_DIAG_QUERY_TIMEOUT_SECONDS,
                    )
                ).single()
                entity_count = session.run(
                    Query(
                        "MATCH (e:Entity {source:'document_ingest'}) RETURN count(e) AS c",
                        timeout=DOCQA_DIAG_QUERY_TIMEOUT_SECONDS,
                    )
                ).single()
                result["documents"] = {"count": int((doc_count or {}).get("c") or 0)}
                result["chunks"] = {"count": int((chunk_count or {}).get("c") or 0)}
                result["entities"] = {"count": int((entity_count or {}).get("c") or 0)}
        except Exception as exc:  # noqa: BLE001
            result["neo4j"] = {"ok": False, "error": str(exc)}
            return result

        try:
            DocumentGraphService().ensure_schema()
            smoke_chunks = self._retrieve_chunks("文档", 1)
            result["retrieval"] = {
                "ok": len(smoke_chunks) > 0,
                "top_hit": smoke_chunks[0]["id"] if smoke_chunks else None,
            }
        except Exception as exc:  # noqa: BLE001
            result["retrieval"] = {
                "ok": False,
                "error": str(exc),
            }

        if not probe_llm:
            result["llm"]["ok"] = bool(self._client)
            return result

        if not self._client:
            result["llm"] = {"ok": False, "configured": False, "error": "LLM 未配置"}
            return result

        try:
            parsed = self._request_llm_json(
                [
                    {"role": "system", "content": "只返回 JSON：{\"answer\":\"ok\",\"used_chunk_ids\":[]}"},
                    {"role": "user", "content": "请输出上述 JSON"},
                ]
            )
            result["llm"] = {
                "ok": True,
                "configured": True,
                "sample": {"answer": str(parsed.get("answer", ""))[:40]},
            }
        except Exception as exc:  # noqa: BLE001
            result["llm"] = {"ok": False, "configured": True, "error": str(exc)}
        return result

    def _decompose_question(self, question: str, max_sub_questions: int) -> List[str]:
        max_sub_questions = max(2, min(8, int(max_sub_questions or 4)))

        if self.enabled and self._client:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是调研规划助手。"
                        "请将用户问题拆解为 3-6 个可检索的子问题，覆盖：背景、事实、风险、趋势、建议。"
                        "只输出 JSON：{\"sub_questions\":[\"...\"]}"
                    ),
                },
                {"role": "user", "content": question},
            ]
            try:
                parsed = self._request_llm_json(messages, max_tokens=500)
                candidates = parsed.get("sub_questions") or []
                if isinstance(candidates, list):
                    normalized = self._normalize_sub_questions(candidates, question, max_sub_questions)
                    if normalized:
                        return normalized
            except Exception as exc:  # noqa: BLE001
                logger.warning("问题拆解失败，使用规则回退", context={"error": str(exc)})

        templates = [
            question,
            f"{question} 的核心事实与关键数据是什么？",
            f"{question} 涉及哪些关键实体及其关系？",
            f"{question} 的主要风险与争议点是什么？",
            f"围绕 {question} 有哪些可执行建议？",
        ]
        return self._normalize_sub_questions(templates, question, max_sub_questions)

    @staticmethod
    def _normalize_sub_questions(candidates: List[Any], root_question: str, max_items: int) -> List[str]:
        normalized: List[str] = []
        seen = set()

        root = (root_question or "").strip()
        if root:
            normalized.append(root)
            seen.add(root)

        for item in candidates:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            if len(text) < 6:
                continue
            seen.add(text)
            normalized.append(text)
            if len(normalized) >= max_items:
                break

        return normalized[:max_items]

    def _generate_deep_report(
        self,
        question: str,
        sub_questions: List[str],
        citations: List[Dict[str, Any]],
        reasoning_profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        context_blocks: List[str] = []
        for item in citations[: max(self.max_context * 3, 8)]:
            snippet = item.get("text") or item.get("snippet") or ""
            trimmed = str(snippet)[:600]
            matched = ", ".join(item.get("matched_questions") or [])
            entities = ", ".join(item.get("entity_names") or [])
            context_blocks.append(
                "\n".join(
                    [
                        f"SOURCE {item['id']} | {item['title']} | {item.get('location') or '-'}",
                        f"HIT_COUNT: {item.get('hit_count', 1)}",
                        f"RETRIEVAL_SCORE: {item.get('retrieval_score', 0)}",
                        f"MATCHED_SUB_QUESTIONS: {matched or '-'}",
                        f"ENTITIES: {entities or '-'}",
                        trimmed,
                    ]
                )
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是企业知识库深度调研助手。"
                    "只能使用给定 SOURCE 证据输出结论，不得臆测。"
                    "输出必须是 JSON，结构为："
                    "{\"summary\":\"...\",\"final_conclusion\":\"...\",\"report\":\"markdown...\",\"confidence_score\":0.0,\"confidence_reason\":\"...\",\"used_chunk_ids\":[\"...\"]}。"
                    "report 必须包含这些小节："
                    "1) 执行摘要 2) 关键发现 3) 证据与引用 4) 风险与不确定性 5) 建议与下一步。"
                    "不要复述用户问题或提示词。"
                    "confidence_score 范围是 0 到 1。若证据不足，应降低 confidence_score 并在 confidence_reason 说明原因。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"调研主问题：{question}\n"
                    f"子问题：{json.dumps(sub_questions, ensure_ascii=False)}\n\n"
                    "证据：\n"
                    + "\n\n".join(context_blocks)
                ),
            },
        ]
        return self._request_llm_json(
            messages,
            max_tokens=max(self.max_tokens, 1600),
            reasoning_profile=reasoning_profile,
        )

    def _request_llm_json(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        reasoning_profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """兼容不同 OpenAI 兼容网关：有些模型不接受 temperature / max_tokens。"""
        if not self._client:
            return {}

        token_limit = int(max_tokens or self.max_tokens)
        request_payloads = [
            {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": token_limit,
            },
            {
                "model": self.model,
                "messages": messages,
                "max_tokens": token_limit,
            },
            {
                "model": self.model,
                "messages": messages,
            },
        ]

        attempt_errors: List[str] = []
        for idx, payload in enumerate(request_payloads, start=1):
            try:
                response = self._client.chat.completions.create(**apply_reasoning_profile(payload, reasoning_profile))
                usage = self._extract_usage(response)
                content = self._extract_message_content(response)
                parsed = self._parse_json(content)
                if parsed:
                    parsed["_usage"] = usage
                    return parsed
                if content.strip():
                    return {"answer": content.strip(), "used_chunk_ids": [], "_usage": usage}
            except Exception as exc:  # noqa: BLE001
                attempt_errors.append(f"attempt_{idx}: {exc}")
                continue

        if attempt_errors:
            raise RuntimeError(" | ".join(attempt_errors))
        return {}

    @staticmethod
    def _extract_message_content(response: Any) -> str:
        try:
            content = response.choices[0].message.content
        except Exception:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return str(content or "")

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "source": "missing",
            }

        def read_int(name: str) -> int:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
            try:
                return max(0, int(value or 0))
            except Exception:
                return 0

        prompt_tokens = read_int("prompt_tokens")
        completion_tokens = read_int("completion_tokens")
        total_tokens = read_int("total_tokens") or (prompt_tokens + completion_tokens)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "source": "llm_response",
        }

    @staticmethod
    def _fallback_answer(_question: str, citations: List[Dict[str, Any]]) -> str:
        snippets = []
        for item in citations[:2]:
            text = (item.get("snippet") or item.get("text") or "").strip()
            if text:
                snippets.append(text[:180])
        if not snippets:
            return "已检索到相关文档，但当前无法生成完整回答，请稍后重试。"
        return "根据已检索片段，可先参考：\n- " + "\n- ".join(snippets)

    @staticmethod
    def _normalize_conversation_history(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        for item in (history or [])[-8:]:
            role = str(item.get("role") or "").strip().lower()
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            normalized.append({"role": role, "content": content[:800]})
        return normalized

    def _contextual_retrieval_query(self, question: str, history: List[Dict[str, str]]) -> str:
        if not history:
            return question
        recent = " ".join(item["content"] for item in history[-4:])
        terms = self._extract_evidence_terms(f"{question} {recent}", limit=18)
        if not terms:
            return question
        return f"{question} " + " ".join(terms)

    @staticmethod
    def _format_conversation_context(history: List[Dict[str, str]]) -> str:
        lines = []
        for item in history[-6:]:
            label = "用户" if item["role"] == "user" else "助手"
            lines.append(f"{label}: {item['content'][:500]}")
        return "\n".join(lines)

    def _focus_citation_snippets(self, citations: List[Dict[str, Any]], question: str, answer: str) -> None:
        terms = self._extract_evidence_terms(f"{question} {answer}", limit=24)
        if not terms:
            return
        for item in citations:
            text = str(item.get("text") or item.get("snippet") or "")
            focused = self._best_evidence_window(text, terms)
            if focused:
                item["snippet"] = focused

    @staticmethod
    def _extract_evidence_terms(text: str, limit: int = 20) -> List[str]:
        stopwords = {
            "他们", "它们", "这些", "那些", "这个", "那个", "哪里", "什么", "怎么", "如何",
            "请问", "一下", "回答", "根据", "文档", "引用", "证据", "问题", "认为", "可以",
            "工作", "单位呢", "他们的", "中的", "以及", "进行", "当前",
        }
        tokens = re.findall(r"[\u4e00-\u9fff]{2,24}|[A-Za-z0-9][A-Za-z0-9_.%/-]{1,40}", text or "")
        terms: List[str] = []
        seen = set()

        def add_term(candidate: str) -> bool:
            cleaned_candidate = candidate.strip(" ，。；：:,.!?！？（）()[]【】")
            if len(cleaned_candidate) < 2 or cleaned_candidate in stopwords:
                return False
            key = cleaned_candidate.lower()
            if key in seen:
                return False
            seen.add(key)
            terms.append(cleaned_candidate)
            return len(terms) >= limit

        for token in tokens:
            cleaned = token.strip(" ，。；：:,.!?！？（）()[]【】")
            if len(cleaned) < 2 or cleaned in stopwords:
                continue
            if re.search(r"[\u4e00-\u9fff]", cleaned) and len(cleaned) >= 8:
                segments = re.split(r"(?:工作单位|单位|作者|分别为|包括|属于|来自|位于|地址|是|为|在|和|与)", cleaned)
                for segment in segments:
                    if add_term(segment):
                        break
                if len(terms) >= limit:
                    break
            if add_term(cleaned):
                break
        return terms

    @staticmethod
    def _best_evidence_window(text: str, terms: List[str], max_len: int = 260) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        if not cleaned:
            return ""
        compact_terms = [re.sub(r"\s+", "", term).lower() for term in terms if term]
        if not compact_terms:
            return cleaned[:max_len]

        candidates: List[str] = []
        candidates.extend(item.strip() for item in re.split(r"(?<=[。！？!?；;])\s*", cleaned) if item.strip())
        for term in terms:
            index = cleaned.find(term)
            if index < 0:
                index = re.sub(r"\s+", "", cleaned).lower().find(re.sub(r"\s+", "", term).lower())
                if index < 0:
                    continue
            start = max(0, index - 90)
            end = min(len(cleaned), index + max_len - 60)
            candidates.append(cleaned[start:end].strip())

        if not candidates:
            return cleaned[:max_len]

        def score(candidate: str) -> tuple[int, int]:
            compact = re.sub(r"\s+", "", candidate).lower()
            hits = sum(1 for term in compact_terms if term in compact)
            return hits, -len(candidate)

        best = max(candidates, key=score)
        if len(best) <= max_len:
            return best
        return best[:max_len].rstrip() + "..."

    def _fallback_deep_report(self, question: str, sub_questions: List[str], citations: List[Dict[str, Any]]) -> str:
        top_facts = []
        for idx, item in enumerate(citations[:6], start=1):
            snippet = (item.get("snippet") or item.get("text") or "").strip()
            if not snippet:
                continue
            top_facts.append(
                f"{idx}. {snippet[:180]}（来源：{item.get('title', '文档片段')} / {item.get('id')}）"
            )

        if not top_facts:
            top_facts.append("暂无可用证据片段，建议补充文档后重试。")

        risk_line = "当前为规则回退报告，语义归纳能力有限，建议启用可用 LLM 以获得更完整结论。"
        sub_q_lines = "\n".join([f"- {q}" for q in sub_questions]) if sub_questions else "- 无"

        return (
            f"## 执行摘要\n"
            f"围绕“{question}”，系统已完成多子问题检索，并整理证据草案。\n\n"
            f"## 关键发现\n"
            + "\n".join([f"- {line}" for line in top_facts[:3]])
            + "\n\n"
            + "## 证据与引用\n"
            + "\n".join([f"- {line}" for line in top_facts])
            + "\n\n"
            + "## 风险与不确定性\n"
            + f"- {risk_line}\n"
            + "- 证据来自当前索引到的片段，可能存在覆盖盲区。\n\n"
            + "## 建议与下一步\n"
            + "- 优先补充高质量原始文档，提升证据覆盖率。\n"
            + "- 对以下子问题进行人工复核：\n"
            + f"{sub_q_lines}\n"
        )

    @staticmethod
    def _fallback_conclusion(citations: List[Dict[str, Any]]) -> str:
        for item in citations[:3]:
            snippet = (item.get("snippet") or item.get("text") or "").strip()
            if snippet:
                return f"基于当前证据，核心结论是：{snippet[:90]}。"
        return "当前证据不足，建议补充文档后再次调研。"

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

    @staticmethod
    def _confidence_level(score: float) -> str:
        if score >= 0.8:
            return "high"
        if score >= 0.6:
            return "medium"
        return "low"

    def _merge_confidence(self, base_score: float, llm_score: Optional[float]) -> float:
        if llm_score is None:
            return round(base_score, 3)
        merged = 0.7 * base_score + 0.3 * llm_score
        return round(max(0.0, min(1.0, merged)), 3)

    def _compute_base_confidence(
        self,
        coverage_ratio: float,
        unique_citations: int,
        sub_question_count: int,
        avg_citation_confidence: float,
    ) -> Dict[str, Any]:
        citation_density = min(1.0, unique_citations / max(1, sub_question_count * 2))
        score = 0.5 * coverage_ratio + 0.25 * citation_density + 0.25 * avg_citation_confidence
        score = round(max(0.0, min(1.0, score)), 3)
        return {
            "score": score,
            "level": self._confidence_level(score),
        }

    @staticmethod
    def _snapshot_citations(citations: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
        snapshot: List[Dict[str, Any]] = []
        for item in citations[:limit]:
            text = str(item.get("text") or item.get("snippet") or "")
            snapshot.append(
                {
                    "id": str(item.get("id") or ""),
                    "kb_id": item.get("kb_id"),
                    "doc_id": item.get("doc_id"),
                    "content_revision": item.get("content_revision"),
                    "title": str(item.get("title") or ""),
                    "location": item.get("location"),
                    "entity_names": item.get("entity_names") or [],
                    "retrieval_score": item.get("retrieval_score"),
                    "retrieval_sources": item.get("retrieval_sources") or [],
                    "retrieval_ranks": item.get("retrieval_ranks") or {},
                    "confidence": item.get("confidence"),
                    "snippet": text[:240],
                }
            )
        return snapshot

    @staticmethod
    def _attach_citation_confidence(citations: List[Dict[str, Any]], sub_question_count: int) -> None:
        max_score = max((float(item.get("retrieval_score") or 0) for item in citations), default=0.0)
        for item in citations:
            hit_count = int(item.get("hit_count") or 1)
            hit_ratio = min(1.0, hit_count / max(1, sub_question_count))
            retrieval = float(item.get("retrieval_score") or 0)
            score_norm = retrieval / max_score if max_score > 0 else 0.5
            entity_bonus = min(0.15, 0.03 * len(item.get("entity_names") or []))
            confidence = 0.55 * hit_ratio + 0.35 * score_norm + entity_bonus
            confidence = round(max(0.0, min(1.0, confidence)), 3)
            item["confidence"] = confidence
            item["confidence_level"] = (
                "high" if confidence >= 0.8 else "medium" if confidence >= 0.6 else "low"
            )

    def _retrieve_chunks(self, question: str, top_k: int) -> List[Dict[str, Any]]:
        service = _neo4j_service()
        query = question.strip()
        if not query:
            return []

        items: List[Dict[str, Any]] = []
        with service.driver.session() as session:
            try:
                result = session.run(
                    Query(
                        """
                        CALL db.index.fulltext.queryNodes('chunkText', $q) YIELD node, score
                        OPTIONAL MATCH (d:Document)-[:HAS_CHUNK]->(node)
                        OPTIONAL MATCH (node)-[:MENTIONS]->(e:Entity)
                        WITH node, d, score, collect(DISTINCT e.name) AS entity_names
                        RETURN node AS c, d, score, entity_names
                        ORDER BY score DESC
                        LIMIT $limit
                        """,
                        timeout=DOCQA_DIAG_QUERY_TIMEOUT_SECONDS,
                    ),
                    {"q": query, "limit": top_k},
                )
            except Exception:
                result = session.run(
                    Query(
                        """
                        MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
                        WHERE c.text CONTAINS $q
                        OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
                        WITH c, d, collect(DISTINCT e.name) AS entity_names
                        RETURN c, d, entity_names
                        LIMIT $limit
                        """,
                        timeout=DOCQA_DIAG_QUERY_TIMEOUT_SECONDS,
                    ),
                    {"q": query, "limit": top_k},
                )

            for record in result:
                chunk = record.get("c")
                doc = record.get("d")
                if not chunk:
                    continue
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

                retrieval_score = self._safe_score(record.get("score"))
                items.append(
                    {
                        "id": str(chunk_props.get("chunk_id") or chunk.id),
                        "title": doc_props.get("name") or "文档片段",
                        "location": f"Chunk {index}" if index is not None else None,
                        "text": text,
                        "snippet": text[:160].strip() if text else "",
                        "doc_id": chunk_props.get("doc_id") or doc_props.get("doc_id"),
                        "entity_names": entity_names,
                        "retrieval_score": retrieval_score,
                    }
                )
        return items

    def _parse_json(self, content: str) -> Dict[str, Any]:
        content = content.strip()
        if not content:
            return {}
        try:
            return json.loads(content)
        except Exception:
            match = re.search(r"\{.*\}", content, re.S)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    return {}
        return {}


doc_qa_service = DocQAService()
