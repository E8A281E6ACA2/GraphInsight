"""Route registration helpers for Python business APIs."""
from __future__ import annotations

from typing import Iterable

from fastapi import APIRouter, FastAPI

from api.routes import (
    doc_qa_internal,
    nl2cypher_internal,
    vector_internal,
)


RouterSpec = tuple[APIRouter, str, list[str]]

INTERNAL_CAPABILITY_ROUTERS: tuple[RouterSpec, ...] = (
    (doc_qa_internal.internal_router, "/api", ["文档问答"]),
    (nl2cypher_internal.internal_router, "/api", ["AI 查询"]),
    (vector_internal.internal_router, "/api", ["向量维护"]),
)


def _include_router_specs(app: FastAPI, router_specs: Iterable[RouterSpec]) -> None:
    for router, prefix, tags in router_specs:
        app.include_router(router, prefix=prefix, tags=tags)


def register_internal_capability_routes(app: FastAPI) -> None:
    """Mount internal capability routes used by the Go entry layer."""
    _include_router_specs(app, INTERNAL_CAPABILITY_ROUTERS)
