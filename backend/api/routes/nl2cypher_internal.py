"""Internal capability routes for NL2Cypher."""
from __future__ import annotations

from fastapi import APIRouter, Request

from api.internal_access import require_go_capability_request, resolve_request_scope
from api.routes.nl2cypher import (
    NL2CypherRequest,
    execute_nl2cypher,
)

internal_router = APIRouter()


@internal_router.post("/internal/nl2cypher")
async def internal_convert_nl_to_cypher(
    nl_request: NL2CypherRequest,
    http_request: Request,
):
    denied = require_go_capability_request(http_request)
    if denied is not None:
        return denied
    # 作用域强制点（契约 §2.4）：header/query/body 联合解析，缺失即 KB_SCOPE_REQUIRED
    scope = resolve_request_scope(http_request, nl_request)
    return await execute_nl2cypher(nl_request, http_request, scope=scope)
