"""HTTP surface for first-class SRE runbooks.

Mounted at ``/v1/ai-sre/runbooks``. Steps execute only after validate;
spendy/external steps create a hold-default Decision. Never auto-applied.
Not public GA.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from ..identity_guard import writing_get  # noqa: E402  (identity int-b)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.sre_runbooks import SreRunbookError, SreRunbookService

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(prefix="/v1/ai-sre/runbooks", tags=["ai-sre"])
_service: Optional[SreRunbookService] = None


def _svc() -> SreRunbookService:
    global _service
    if _service is None:
        _service = SreRunbookService(get_settings())
    return _service


def _engine():
    return get_engine()


def _require_actor(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "providerops_manage")


def _raise(err: SreRunbookError) -> None:
    detail: Any = {"message": str(err), **(err.payload or {})}
    raise HTTPException(status_code=err.status_code, detail=detail)


class RunbookCreate(BaseModel):
    project_id: str
    actor: str = "executive_console"
    name: str
    steps: list[dict[str, Any]] = Field(default_factory=list)


class RunCreate(BaseModel):
    project_id: str
    actor: str = "executive_console"
    incident_id: str
    force: bool = False
    adapter: str = "slack_webhook"


@writing_get(router, "")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
def list_runbooks(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine()) as connection:
        items = _svc().list_runbooks(connection, project_id)
    return {"project_id": project_id, "items": items, "public_ga": False}


@router.post("", status_code=201)
@permit(P.RELIABILITY_MANAGE, scope=Scope.project(body="project_id"))
def create_runbook(payload: RunbookCreate) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            row = _svc().create_runbook(
                connection,
                payload.project_id,
                name=payload.name,
                steps=payload.steps,
                actor=payload.actor,
            )
        except SreRunbookError as err:
            _raise(err)
    return row


@router.get("/runs")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"))
def list_runs(
    project_id: str = Query(...),
    runbook_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    with begin(_engine()) as connection:
        items = _svc().list_runs(
            connection, project_id, runbook_id=runbook_id, limit=limit,
        )
    return {"project_id": project_id, "items": items, "public_ga": False}


@router.post("/{runbook_id}/runs")
@permit(P.RELIABILITY_RESPOND, scope=Scope.project(body="project_id"))
def execute_run(runbook_id: str, payload: RunCreate) -> dict[str, Any]:
    with begin(_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor)
        try:
            return _svc().execute_run(
                connection,
                payload.project_id,
                runbook_id,
                payload.incident_id,
                actor=payload.actor,
                force=payload.force,
                adapter=payload.adapter,
            )
        except SreRunbookError as err:
            _raise(err)


__all__ = ["router"]
