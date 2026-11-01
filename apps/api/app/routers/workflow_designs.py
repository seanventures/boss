"""Agent design review and lock, per workflow stage (design §4.6; engine stage_designs.py).

GET  /v1/delivery-workflows/{key}/designs                      every stage's design status and
                                                               reference, and the graph versions
POST /v1/delivery-workflows/{key}/designs/{stage_id}/review    not reviewed → in review
POST /v1/delivery-workflows/{key}/designs/{stage_id}/lock      in review → locked (a person,
                                                               workflow:publish, step_up=confirmed)
POST /v1/delivery-workflows/{key}/designs/{stage_id}/revise    locked → the next version in review

Reads need workflow:read; sending for review and revising need workflow:create (editing the
workflow); locking needs workflow:publish, a step-up permission, and `step_up: "confirmed"` in
the body, and is refused for an agent's credential whatever it holds. Every transition is a
`workflow.design_*` event.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge import stage_designs

from .knowledge_rules import _actor_kind, _reviewer

router = APIRouter(prefix="/v1/delivery-workflows", tags=["workflows"])
_settings = get_settings()
_engine = get_engine(_settings)


def _actor(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "workflow_manage")


def _http(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, stage_designs.DesignRefused):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail=error.__class__.__name__)


class DesignActionBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    step_up: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)


def _who(body: DesignActionBody) -> str:
    from apps.api.app.main import _delivery_workflow_actor

    return _delivery_workflow_actor({"actor": body.actor})


@router.get("/{workflow_key}/designs")
@permit(P.WORKFLOW_READ, scope=Scope.project(query="project_id"))
def list_designs(workflow_key: str, project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine) as connection:
        from apps.api.app.main import require_project_reader

        require_project_reader(connection, project_id)
        found = stage_designs.designs(connection, project_id, workflow_key)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no workflow '{workflow_key}' in this project")
    return found


@router.post("/{workflow_key}/designs/{stage_id}/review")
@permit(P.WORKFLOW_CREATE, scope=Scope.project(body="project_id"))
def send_for_review(workflow_key: str, stage_id: str, body: DesignActionBody) -> dict[str, Any]:
    actor = _who(body)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, actor)
            return stage_designs.send_for_review(connection, body.project_id, workflow_key=workflow_key,
                                                 stage_id=stage_id, actor=actor)
    except Exception as error:  # noqa: BLE001 - mapped to HTTP below
        raise _http(error) from error


@router.post("/{workflow_key}/designs/{stage_id}/lock")
@permit(P.WORKFLOW_PUBLISH, scope=Scope.project(body="project_id"))
def lock_design(workflow_key: str, stage_id: str, body: DesignActionBody) -> dict[str, Any]:
    if body.step_up != "confirmed":
        raise HTTPException(status_code=403, detail="Locking a design requires step_up=confirmed")
    actor = _who(body)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, actor)
            reviewer, name = _reviewer(actor)
            return stage_designs.lock(connection, body.project_id, workflow_key=workflow_key,
                                      stage_id=stage_id, actor=reviewer, actor_kind=_actor_kind(),
                                      actor_name=name)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/{workflow_key}/designs/{stage_id}/revise")
@permit(P.WORKFLOW_CREATE, scope=Scope.project(body="project_id"))
def revise_design(workflow_key: str, stage_id: str, body: DesignActionBody) -> dict[str, Any]:
    actor = _who(body)
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, actor)
            return stage_designs.revise(connection, body.project_id, workflow_key=workflow_key,
                                        stage_id=stage_id, actor=actor, note=body.note)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error
