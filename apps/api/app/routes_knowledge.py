"""Knowledge graph routes (KNOWLEDGE_GRAPH_SPEC.md §3.3, §5; Phase 1).

Phase 1 needs only the worker's two calls: run one leased `knowledge.index` job here, where the
parsers and the repository cache are, and queue the scheduled passes that are due. Phase 2's tick
also distils the runs, reviews and incidents that finished since the last one (the Archivist). The public
search / why / blast-radius API (with `knowledge:read` and `knowledge:manage`) is Phase 3; agents
already reach the same answers through the kg_* MCP tools.
"""
from __future__ import annotations

from typing import Any

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit


class IndexJobRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    payload: dict[str, Any]


class KnowledgeSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    daily_cap_usd_micros: Optional[int] = None
    monthly_cap_usd_micros: Optional[int] = None
    per_run_ceiling_usd_micros: Optional[int] = None
    retention_days: Optional[int] = None
    pii: Optional[dict[str, Any]] = None


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
        engine, settings = _engine_settings()
        with engine.begin() as c:
            queued = enqueue_due(c)
        # Phase 2: distil the runs, reviews and incidents that finished since the last tick.
        from boss_engine.services.knowledge.archivist import distil_due

        return {"queued": queued, "distil": distil_due(engine, settings)}

    # Worker-invoked, every 30 s: distil what finished since (kg_ingest_queue, fed by triggers).
    @router.post("/v1/knowledge/distil/tick")
    @permit(P.WORKER_LEASE)
    def distil_tick() -> Any:
        from boss_engine.services.knowledge.archivist import distil_queued

        from .identity_guard import require_worker_principal

        require_worker_principal()
        engine, settings = _engine_settings()
        return {"distil": distil_queued(engine, settings)}

    # The org's knowledge budget and retention (defaults: $2/day, $20/month, $0.20/run, 365 days).
    @router.get("/v1/knowledge/settings")
    @permit(P.SETTINGS_READ, scope=Scope.project(query="project_id"))
    def get_settings(project_id: str) -> Any:
        from boss_engine.services.knowledge.archivist import settings_view
        from boss_engine.services.knowledge.store import Scope as KScope

        engine, _ = _engine_settings()
        with engine.begin() as c:
            return settings_view(c, KScope(project_id))

    @router.put("/v1/knowledge/settings")
    @permit(P.SETTINGS_UPDATE, scope=Scope.project(body="project_id"))
    def put_settings(body: KnowledgeSettingsBody) -> Any:
        from boss_engine.services.knowledge.archivist import update_settings
        from boss_engine.services.knowledge.store import Scope as KScope

        from .main import current_api_principal

        engine, _ = _engine_settings()
        actor = str(current_api_principal.get() or "operator")
        changes = body.model_dump(exclude={"project_id"}, exclude_none=True)
        try:
            with engine.begin() as c:
                return update_settings(c, KScope(body.project_id), changes, actor=actor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
