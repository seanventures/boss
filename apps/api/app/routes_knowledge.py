"""Knowledge graph routes (KNOWLEDGE_GRAPH_SPEC.md §3.3, §5; Phase 1).

Phase 1 needs only the worker's two calls: run one leased `knowledge.index` job here, where the
parsers and the repository cache are, and queue the scheduled passes that are due. The public
search / why / blast-radius API (with `knowledge:read` and `knowledge:manage`) is Phase 3; agents
already reach the same answers through the kg_* MCP tools.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, permit


class IndexJobRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    payload: dict[str, Any]


def _engine_settings():
    from . import main

    return main.engine, main.settings


def build_knowledge_router() -> APIRouter:
    router = APIRouter(tags=["knowledge"])

    # Worker-invoked: run one leased `knowledge.index` job.
    @router.post("/v1/knowledge/index/run")
    @permit(P.WORKER_LEASE)
    def run_index(body: IndexJobRun) -> Any:
        from boss_engine.services.knowledge.index_jobs import run_index_job

        from .identity_guard import require_worker_principal

        require_worker_principal()
        engine, settings = _engine_settings()
        return run_index_job(engine, settings, body.project_id, body.payload)

    # Worker-invoked: queue the hourly incremental and nightly full passes that are due.
    @router.post("/v1/knowledge/index/tick")
    @permit(P.WORKER_LEASE)
    def tick() -> Any:
        from boss_engine.services.knowledge.index_jobs import enqueue_due

        from .identity_guard import require_worker_principal

        require_worker_principal()
        engine, _ = _engine_settings()
        with engine.begin() as c:
            return {"queued": enqueue_due(c)}

    return router
