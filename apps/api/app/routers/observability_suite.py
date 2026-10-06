"""HTTP surface for the observability suite + knowledge-graph operator convenience.

Mounted by ``mount_platform_routers`` when present.

Routes (prefix ``/v1/observability-suite``):

* GET  ``/catalog``         — OSS components (purpose + stars note) + profiles
* GET  ``/status``          — unified connectors + stack + gaps for a project
* POST ``/recommend``       — workload_profile → recommended OSS set
* POST ``/health-check``    — dry-run (default) or live probe of configured sources
* GET  ``/graph-summary``   — KG health + optional RCA pack (subgraph + known causes)
* POST ``/graph-seed``      — seed-from-infra + health summary in one call

Also exposes (no clash with existing KG routes):

* GET  ``/v1/knowledge/graph/health`` — node/edge health summary only
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.observability_suite import ObservabilitySuiteService

from ..config import get_settings
from ..db import begin, database_is_reachable, get_engine

router = APIRouter(tags=["observability-suite"])

_suite = ObservabilitySuiteService()


class RecommendRequest(BaseModel):
    workload_profile: str = Field(
        ...,
        description="e.g. minimal, web, api, batch, microservices, k8s, enterprise, traces_focus",
    )


class HealthCheckRequest(BaseModel):
    project_id: str
    actor: str = "operator"
    source_ids: Optional[list[str]] = None
    dry_run: bool = Field(
        default=True,
        description="When true (default), only validate config — no live HTTP probes.",
    )


class GraphSeedRequest(BaseModel):
    project_id: str
    actor: str = "operator"


def _require_db() -> None:
    engine = get_engine(get_settings())
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")


# ── /v1/observability-suite/* ────────────────────────────────────────────────


@router.get("/v1/observability-suite/catalog")
@permit(P.OBSERVABILITY_READ, scope=Scope.org())
def observability_suite_catalog() -> dict[str, Any]:
    """OSS components with purpose + stars note, workload profiles, connector kinds."""
    return _suite.catalog()


@router.get("/v1/observability-suite/status")
@permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
def observability_suite_status(
    project_id: str = Query(...),
    host_target_id: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Unified status of registered connectors + BOSS stack signal + OSS recommendations."""
    _require_db()
    settings = get_settings()
    engine = get_engine(settings)
    svc = ObservabilitySuiteService(settings)
    with begin(engine) as connection:
        return svc.status(
            connection,
            project_id,
            host_target_id=host_target_id,
            engine=engine,
        )


@router.post("/v1/observability-suite/recommend")
@permit(P.OBSERVABILITY_READ, scope=Scope.org(), readonly_post=True)
def observability_suite_recommend(payload: RecommendRequest) -> dict[str, Any]:
    """Recommend an OSS observability set for a workload profile."""
    try:
        return _suite.recommend(payload.workload_profile)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/v1/observability-suite/health-check")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
def observability_suite_health_check(payload: HealthCheckRequest) -> dict[str, Any]:
    """Dry-run connectivity for configured telemetry sources (live probes optional)."""
    _require_db()
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        return _suite.health_check(
            connection,
            payload.project_id,
            source_ids=payload.source_ids,
            dry_run=payload.dry_run,
        )


@router.get("/v1/observability-suite/graph-summary")
@permit(P.OBSERVABILITY_READ, scope=Scope.project(query="project_id"))
def observability_suite_graph_summary(
    project_id: str = Query(...),
    kind: Optional[str] = Query(default=None),
    node_key: Optional[str] = Query(default=None),
    depth: int = Query(default=2, ge=0, le=10),
) -> dict[str, Any]:
    """Knowledge-graph health plus optional RCA pack (subgraph + known causes)."""
    _require_db()
    engine = get_engine(get_settings())
    with begin(engine) as connection:
        return _suite.graph_summary(
            connection,
            project_id,
            kind=kind,
            node_key=node_key,
            depth=depth,
        )


@router.post("/v1/observability-suite/graph-seed")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(body="project_id"))
def observability_suite_graph_seed(payload: GraphSeedRequest) -> dict[str, Any]:
    """Seed KG resource nodes from active infra_resources and return health summary.

    Convenience twin of POST /v1/knowledge/graph/seed-from-infra that also returns
    graph health. Actor is recorded in the response; RBAC for the primary seed
    route remains on the knowledge-graph endpoint.
    """
    _require_db()
    from boss_engine.services.observability_suite import seed_and_summarize

    engine = get_engine(get_settings())
    with begin(engine) as connection:
        return seed_and_summarize(
            connection, payload.project_id, actor=payload.actor
        )


# ── /v1/knowledge/graph/health (no clash with existing KG routes) ────────────


@router.get("/v1/knowledge/graph/health")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"))
def knowledge_graph_health(project_id: str = Query(...)) -> dict[str, Any]:
    """Node/edge counts and readiness for the project knowledge graph."""
    _require_db()
    from boss_engine.services.observability_suite import graph_health_summary

    engine = get_engine(get_settings())
    with begin(engine) as connection:
        return graph_health_summary(connection, project_id)
