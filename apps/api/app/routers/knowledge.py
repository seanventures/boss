"""Knowledge graph HTTP surface (BOSSAPP-622).

GET  /v1/knowledge/search
GET  /v1/knowledge/nodes/{node_id}
GET  /v1/knowledge/blast-radius
GET  /v1/knowledge/graph-status   (/v1/knowledge/status is BOSS-356's pack-context probe)
POST /v1/knowledge/facts/{fact_id}/promote
POST /v1/knowledge/facts/{fact_id}/reject
POST /v1/knowledge/facts/{fact_id}/forget

Manage calls require knowledge:manage and step_up=confirmed. That confirmation
is explicit in the body. It is not a WebAuthn ceremony.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.knowledge.service import (
    MANAGE,
    READ,
    KnowledgeService,
    require_confirmation,
)


router = APIRouter(prefix="/v1/knowledge", tags=["knowledge"])
_settings = get_settings()
_engine = get_engine(_settings)
_service = KnowledgeService()


def _actor(connection, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


class ManageBody(BaseModel):
    project_id: str
    actor: str
    step_up: Optional[str] = None


def _http(error: Exception) -> HTTPException:
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail=error.__class__.__name__)


@router.get("/search")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def search(
    project_id: str = Query(...),
    actor: str = Query(...),
    q: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.search(connection, project_id=project_id, query=q)
    except Exception as error:  # noqa: BLE001 — mapped to HTTP below
        raise _http(error) from error


@router.get("/nodes/{node_id}")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def node(
    node_id: str,
    project_id: str = Query(...),
    actor: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.why(connection, project_id=project_id, node_id=node_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/blast-radius")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def blast(
    project_id: str = Query(...),
    actor: str = Query(...),
    node_id: str = Query(...),
) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.blast(connection, project_id=project_id, node_id=node_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.get("/graph-status")
@permit(P.KNOWLEDGE_READ, scope=Scope.project(query="project_id"))
def status(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    try:
        with begin(_engine) as connection:
            _actor(connection, project_id, actor, READ)
            return _service.status(connection, project_id=project_id)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


def _manage(body: ManageBody, action, **kwargs) -> dict[str, Any]:
    try:
        require_confirmation(body.step_up)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        with begin(_engine) as connection:
            _actor(connection, body.project_id, body.actor, MANAGE)
            return action(connection, project_id=body.project_id, actor_kind="person", **kwargs)
    except Exception as error:  # noqa: BLE001
        raise _http(error) from error


@router.post("/facts/{fact_id}/promote")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def promote(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.promote, fact_id=fact_id)


@router.post("/facts/{fact_id}/reject")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def reject(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.reject, fact_id=fact_id)


@router.post("/facts/{fact_id}/forget")
@permit(P.KNOWLEDGE_MANAGE, scope=Scope.project(body="project_id"))
def forget(fact_id: str, body: ManageBody) -> dict[str, Any]:
    return _manage(body, _service.forget, fact_id=fact_id)
