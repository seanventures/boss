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
from sqlalchemy import text

from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.store import Scope as KScope


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


class FactActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)


def _actor_kind() -> str:
    """"human" only for a person's own request; an agent's run token or a service key is not one."""
    from boss_engine.identity import lineage

    ctx = lineage.current_context()
    if ctx is None or ctx.run_token is not None:
        return "agent"
    return "human" if getattr(ctx.principal, "is_person", False) else "agent"


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

    # ── The public API (KNOWLEDGE_GRAPH_SPEC.md §5, Phase 3). knowledge:read for reads, knowledge:manage
    # (step-up) for promote, reject, forget and settings. Every read is filtered again inside by the
    # reader's Passport ceiling when the caller is an agent (delivery.reader_for).
    @router.get("/v1/knowledge/search")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def search(project_id: str, q: str = "", touched: str = "", role: str = "", stage: str = "",
               budget_tokens: Optional[int] = None, k: int = 20) -> Any:
        from boss_engine.services.knowledge import delivery, retrieval

        engine, _ = _engine_settings()
        names = [t for t in (x.strip() for x in touched.split(",")) if t]
        with engine.begin() as c:
            reader = delivery.reader_for(c, project_id)
            if budget_tokens:
                return delivery.pack(c, project_id, role, stage, {"text": q, "touched": names},
                                     budget_tokens=max(50, min(int(budget_tokens), 8000)), via="api",
                                     reader=reader).as_dict()
            out = retrieval.search(c, reader, q, touched=names, k=max(1, min(k, 100)))
            return {"facts": out["facts"], "sources": out["sources"]}

    @router.get("/v1/knowledge/nodes/{node_id}")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def node(node_id: str, project_id: str) -> Any:
        from boss_engine.services.knowledge import delivery, retrieval
        from boss_engine.services.knowledge.code_queries import AmbiguousTarget, KnowledgeQueries

        engine, _ = _engine_settings()
        with engine.begin() as c:
            reader = delivery.reader_for(c, project_id)
            row = c.execute(text("SELECT id, kind, node_key, name, repo, path, attrs, layer, source, confidence, "
                                 "first_seen, last_seen FROM kg_nodes WHERE id = :i AND project_id = :p"),
                            {"i": node_id, "p": project_id}).mappings().first()
            if row is None or not reader.holds(retrieval.NODE_PERMISSION.get(row["kind"],
                                                                             retrieval.DEFAULT_NODE_PERMISSION)):
                raise HTTPException(status_code=404, detail="no such node")
            out = {k: (str(v) if k in ("first_seen", "last_seen") else v) for k, v in dict(row).items()}
            if row["kind"] in ("symbol", "file", "module"):
                try:
                    out["why"] = KnowledgeQueries().why(c, KScope(project_id), row["node_key"], repo=row["repo"])
                except AmbiguousTarget as exc:
                    out["why"] = {"ambiguous": str(exc)}
            out["facts"] = [f for f in retrieval.search(c, reader, "", touched=[row["node_key"]], k=20)["facts"]
                            if f.get("subject_node_id") == node_id]
            return out

    @router.get("/v1/knowledge/blast-radius")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def blast_radius(project_id: str, target: str, depth: int = 2, repo: Optional[str] = None) -> Any:
        from boss_engine.services.knowledge import delivery
        from boss_engine.services.knowledge.code_queries import AmbiguousTarget, KnowledgeQueries

        engine, _ = _engine_settings()
        with engine.begin() as c:
            if not delivery.reader_for(c, project_id).holds("repository:read"):
                raise HTTPException(status_code=403, detail="the reader's Passport has no repository:read")
            try:
                return KnowledgeQueries().blast_radius(c, KScope(project_id), target, depth=max(1, min(depth, 4)),
                                                       repo=repo)
            except AmbiguousTarget as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/v1/knowledge/facts/{fact_id}/{action}")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
    def manage_fact(fact_id: str, action: str, body: FactActionBody) -> Any:
        from boss_engine.services.knowledge.facts import FactStore

        from .main import current_api_principal

        if action not in ("promote", "reject", "forget"):
            raise HTTPException(status_code=404, detail="unknown action")
        engine, _ = _engine_settings()
        person = str(current_api_principal.get() or "")
        kind = _actor_kind()
        with engine.begin() as c:
            store, scope = FactStore(), KScope(body.project_id)
            try:
                if action == "promote":
                    return store.promote(c, scope, fact_id, person=person, actor_kind=kind)
                if action == "reject":
                    return store.reject(c, scope, fact_id, person=person, actor_kind=kind)
                return store.forget_fact(c, scope, fact_id, actor=person)
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

    # The knowledge graph's own status (spec §5's /v1/knowledge/status: that path is BOSS-356's
    # pack-context probe, so this one lives beside it): ingest lag, sizes, cost.
    @router.get("/v1/knowledge/graph-status")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def graph_status(project_id: str) -> Any:
        from boss_engine.services.knowledge.archivist import knowledge_spend, org_settings

        engine, _ = _engine_settings()
        with engine.begin() as c:
            p = {"p": project_id}
            sizes = dict(c.execute(text(
                "SELECT 'nodes', count(*) FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL UNION ALL "
                "SELECT 'edges', count(*) FROM kg_edges WHERE project_id = :p AND valid_to IS NULL UNION ALL "
                "SELECT 'facts', count(*) FROM kg_facts WHERE project_id = :p AND valid_to IS NULL UNION ALL "
                "SELECT 'queued', count(*) FROM kg_ingest_queue WHERE project_id = :p"), p).all())
            lag = c.execute(text("SELECT EXTRACT(EPOCH FROM now() - min(finished_at)) FROM kg_ingest_queue "
                                 "WHERE project_id = :p"), p).scalar()
            last = c.execute(text("SELECT max(at) FROM kg_ingest_log WHERE project_id = :p"), p).scalar()
            s = org_settings(c, KScope(project_id))
            today, month = knowledge_spend(c, s["org_id"])
        return {"sizes": {k: int(v) for k, v in sizes.items()},
                "ingest_lag_seconds": float(lag) if lag is not None else 0.0,
                "last_ingest_at": str(last) if last else None,
                "cost_usd_micros": {"today": today, "month": month},
                "caps_usd_micros": {"daily": s["daily"], "monthly": s["monthly"], "per_run": s["ceiling"]}}

    # The org's knowledge budget and retention (defaults: $2/day, $20/month, $0.20/run, 365 days).
    @router.get("/v1/knowledge/settings")
    @permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
    def get_settings(project_id: str) -> Any:
        from boss_engine.services.knowledge.archivist import settings_view
        from boss_engine.services.knowledge.store import Scope as KScope

        engine, _ = _engine_settings()
        with engine.begin() as c:
            return settings_view(c, KScope(project_id))

    @router.put("/v1/knowledge/settings")
    @permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
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
