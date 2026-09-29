"""NL2Cypher shared capability handlers."""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel

from services.nl2cypher_service import NL2CypherService
from services.scope_contract import SearchTarget

try:  # neo4j 驱动缺失时（单元测试环境）仍可加载本模块
    from neo4j.exceptions import ServiceUnavailable
except Exception:  # pragma: no cover - 环境相关

    class ServiceUnavailable(Exception):  # type: ignore[no-redef]
        pass


class NL2CypherRequest(BaseModel):
    """NL2Cypher request payload."""

    natural_language: str
    context: Optional[Dict] = None
    # M4：显式作用域；internal 入口还会与 header/query 联合校验（契约 §3.2）
    kb_id: str | None = None
    kb_ids: List[str] | None = None


async def execute_nl2cypher(
    nl_request: NL2CypherRequest,
    http_request: Request,
    *,
    scope: SearchTarget,
):
    if not nl_request.natural_language or not nl_request.natural_language.strip():
        raise HTTPException(status_code=400, detail="自然语言查询不能为空")

    try:
        nl2cypher_service = NL2CypherService()
        result = await nl2cypher_service.convert(
            nl_request.natural_language,
            nl_request.context,
            kb_ids=scope.kb_ids,
        )
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return result
