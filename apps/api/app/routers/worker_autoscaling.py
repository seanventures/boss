"""HTTP surface for distributed worker autoscaling over elastic fabric.

Prefix: /v1/worker-autoscaling
  GET  /policies
  PUT  /policies
  POST /evaluate
  POST /apply
  GET  /status
  GET  /history
"""

from __future__ import annotations

from typing import Any

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.elastic_fabric import ElasticFabricService
from boss_engine.services.resource_plane import ResourcePlaneService
from boss_engine.services.worker_autoscaling import (
    WorkerAutoscalingService,
    is_missing_relation,
)
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

router = APIRouter(
    prefix="/v1/worker-autoscaling",
    tags=["worker-autoscaling"],
)


def _service() -> WorkerAutoscalingService:
    settings = get_settings()
    resource_plane = ResourcePlaneService(settings)
    elastic = ElasticFabricService(settings, resource_plane=resource_plane)
    return WorkerAutoscalingService(
        settings, elastic_fabric=elastic, resource_plane=resource_plane
    )


def _authorize(
    connection, project_id: str, actor: str, permission: str, *,
    allow_missing: bool = False,
) -> str:
    # The HTTP principal, when present, is the authority and the audit actor.
    # Keep the explicit actor for local, unauthenticated development calls.
    from apps.api.app.main import current_api_principal, require_project_actor

    checked_actor = current_api_principal.get() or actor
    # Preserve the existing honest empty-state response for a project that has
    # never existed; there is no project data to reveal. Mutations fail closed.
    project_exists = connection.execute(
        text("SELECT 1 FROM projects WHERE id = :project_id"),
        {"project_id": project_id},
    ).first()
    if not project_exists:
        if allow_missing:
            return checked_actor
        raise HTTPException(status_code=404, detail="Project not found.")
    require_project_actor(connection, project_id, checked_actor, permission)
    return checked_actor


class PolicyUpsertRequest(BaseModel):
    project_id: str
    actor: str = "system"
    pool_key: str = "default"
    enabled: bool = True
    min_workers: int = Field(default=0, ge=0)
    max_workers: int = Field(default=8, ge=0)
    scale_up_threshold: int = Field(default=1, ge=0)
    cooldown_seconds: int = Field(default=30, ge=0)
    max_queue_depth_per_worker: int = Field(default=5, ge=1)
    kind: str = "local"
    provider_account_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    project_id: str
    actor: str = "system"
    pool_key: str | None = None
    persist_history: bool = True


class ApplyRequest(BaseModel):
    project_id: str
    actor: str = "system"
    pool_key: str | None = None
    force: bool = False
    auto_reconcile: bool = True
    cloud_spillover_enabled: bool = False
    reason: str | None = None


@router.get("/policies")
def list_policies(project_id: str = Query(...)) -> dict[str, Any]:
    svc = _service()
    try:
        with begin(get_engine()) as connection:
            _authorize(
                connection, project_id, "executive_console", "providerops_manage",
                allow_missing=True,
            )
            ready = svc.ensure_tables(connection)
            items = svc.list_policies(connection, project_id)
            return {
                "project_id": project_id,
                "items": items,
                "count": len(items),
                "schema_ready": ready,
                "configured": bool(items),
            }
    except ProgrammingError as error:
        if is_missing_relation(error):
            return {
                "project_id": project_id,
                "items": [],
                "count": 0,
                "schema_ready": False,
                "configured": False,
            }
        raise


@router.put("/policies")
def upsert_policy(payload: PolicyUpsertRequest) -> dict[str, Any]:
    svc = _service()
    with begin(get_engine()) as connection:
        actor = _authorize(connection, payload.project_id, payload.actor, "infra_provision")
        try:
            policy = svc.upsert_policy(
                connection,
                project_id=payload.project_id,
                actor=actor,
                pool_key=payload.pool_key,
                enabled=payload.enabled,
                min_workers=payload.min_workers,
                max_workers=payload.max_workers,
                scale_up_threshold=payload.scale_up_threshold,
                cooldown_seconds=payload.cooldown_seconds,
                max_queue_depth_per_worker=payload.max_queue_depth_per_worker,
                kind=payload.kind,
                provider_account_id=payload.provider_account_id,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return policy


@router.post("/evaluate")
def evaluate(payload: EvaluateRequest) -> dict[str, Any]:
    svc = _service()
    with begin(get_engine()) as connection:
        actor = _authorize(connection, payload.project_id, payload.actor, "providerops_manage")
        try:
            return svc.evaluate(
                connection,
                project_id=payload.project_id,
                actor=actor,
                pool_key=payload.pool_key,
                persist_history=payload.persist_history,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/apply")
def apply(payload: ApplyRequest) -> dict[str, Any]:
    """Evaluate + request elastic capacity (simulated/real per fabric gates)."""
    svc = _service()
    with begin(get_engine()) as connection:
        actor = _authorize(connection, payload.project_id, payload.actor, "infra_provision")
        try:
            return svc.apply(
                connection,
                project_id=payload.project_id,
                actor=actor,
                pool_key=payload.pool_key,
                force=payload.force,
                auto_reconcile=payload.auto_reconcile,
                cloud_spillover_enabled=payload.cloud_spillover_enabled,
                reason=payload.reason,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/status")
def status(project_id: str = Query(...)) -> dict[str, Any]:
    svc = _service()
    try:
        with begin(get_engine()) as connection:
            _authorize(
                connection, project_id, "executive_console", "providerops_manage",
                allow_missing=True,
            )
            svc.ensure_tables(connection)
            return svc.status(connection, project_id)
    except ProgrammingError as error:
        if is_missing_relation(error):
            return {
                "project_id": project_id,
                "queue": {
                    "queue_depth": 0,
                    "running_jobs": 0,
                    "leased_jobs": 0,
                    "retryable_jobs": 0,
                },
                "leased_jobs": 0,
                "queue_depth": 0,
                "running_jobs": 0,
                "pools": [],
                "instance_counts": {},
                "schema_ready": False,
                "configured": False,
                "infra_real_provision_allowed": bool(
                    get_settings().infra_real_provision_allowed
                ),
                "message": "Worker autoscaling tables are not applied on this runtime.",
            }
        raise


@router.get("/history")
def history(
    project_id: str = Query(...),
    pool_key: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    svc = _service()
    try:
        with begin(get_engine()) as connection:
            _authorize(
                connection, project_id, "executive_console", "providerops_manage",
                allow_missing=True,
            )
            svc.ensure_tables(connection)
            items = svc.history(
                connection, project_id, pool_key=pool_key, limit=limit
            )
            return {
                "project_id": project_id,
                "pool_key": pool_key,
                "items": items,
                "count": len(items),
            }
    except ProgrammingError as error:
        if is_missing_relation(error):
            return {
                "project_id": project_id,
                "pool_key": pool_key,
                "items": [],
                "count": 0,
                "schema_ready": False,
            }
        raise
