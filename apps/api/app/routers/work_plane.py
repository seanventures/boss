"""HTTP surface for the Native Work Plane (BOSS-331 / BOSS-358).

Prefix: ``/v1/work-plane``

* GET  /status?project_id=
* GET  /board?project_id=
* GET  /overview?project_id=
* POST /initiatives  · GET /initiatives
* POST /modules      · GET /modules
* POST /cycles       · GET /cycles
* POST /roadmap-items · GET /roadmap-items
* POST /dependencies · GET /dependencies
* POST /link-issue
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.work_plane import WorkPlaneService


router = APIRouter(prefix="/v1/work-plane", tags=["work-plane"])

_settings = get_settings()
_service = WorkPlaneService(_settings)
_engine = get_engine(_settings)


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


class ActorBody(BaseModel):
    project_id: str
    actor: str = "system"


class InitiativeRequest(ActorBody):
    title: str = Field(..., min_length=1)
    key: Optional[str] = None
    description: Optional[str] = None
    status: str = "planned"
    owner: Optional[str] = None
    target_date: Optional[str] = None
    issue_url: Optional[str] = None
    tracker: Optional[str] = None
    issue_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModuleRequest(ActorBody):
    title: str = Field(..., min_length=1)
    key: Optional[str] = None
    initiative_id: Optional[str] = None
    description: Optional[str] = None
    status: str = "planned"
    owner: Optional[str] = None
    issue_url: Optional[str] = None
    tracker: Optional[str] = None
    issue_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CycleRequest(ActorBody):
    title: str = Field(..., min_length=1)
    key: Optional[str] = None
    description: Optional[str] = None
    status: str = "planned"
    starts_on: Optional[str] = None
    ends_on: Optional[str] = None
    issue_url: Optional[str] = None
    tracker: Optional[str] = None
    issue_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoadmapItemRequest(ActorBody):
    title: str = Field(..., min_length=1)
    key: Optional[str] = None
    description: Optional[str] = None
    status: str = "planned"
    initiative_id: Optional[str] = None
    module_id: Optional[str] = None
    cycle_id: Optional[str] = None
    target_date: Optional[str] = None
    sort_order: int = 0
    issue_url: Optional[str] = None
    tracker: Optional[str] = None
    issue_key: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DependencyRequest(ActorBody):
    src_type: str
    src_id: str
    dst_type: str
    dst_id: str
    kind: str = "depends_on"
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkIssueRequest(ActorBody):
    entity_type: str
    entity_id: str
    tracker: str = Field(..., min_length=1)
    issue_key: str = Field(..., min_length=1)
    url: Optional[str] = None


@router.get("/status")
def get_status(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine) as connection:
        return _service.status_snapshot(connection, project_id)


@router.get("/board")
def get_board(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine) as connection:
        return _service.board(connection, project_id)


@router.get("/overview")
def get_overview(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine) as connection:
        try:
            return _service.overview(connection, project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/initiatives", status_code=201)
def create_initiative(payload: InitiativeRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.create_initiative(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                key=payload.key,
                description=payload.description,
                status=payload.status,
                owner=payload.owner,
                target_date=payload.target_date,
                issue_url=payload.issue_url,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/initiatives")
def list_initiatives(
    project_id: str = Query(...),
    status: Optional[str] = None,
) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_initiatives(connection, project_id, status=status)
        return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("/modules", status_code=201)
def create_module(payload: ModuleRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.create_module(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                key=payload.key,
                initiative_id=payload.initiative_id,
                description=payload.description,
                status=payload.status,
                owner=payload.owner,
                issue_url=payload.issue_url,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/modules")
def list_modules(
    project_id: str = Query(...),
    initiative_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_modules(
            connection,
            project_id,
            initiative_id=initiative_id,
            status=status,
        )
        return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("/cycles", status_code=201)
def create_cycle(payload: CycleRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.create_cycle(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                key=payload.key,
                description=payload.description,
                status=payload.status,
                starts_on=payload.starts_on,
                ends_on=payload.ends_on,
                issue_url=payload.issue_url,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/cycles")
def list_cycles(
    project_id: str = Query(...),
    status: Optional[str] = None,
) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_cycles(connection, project_id, status=status)
        return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("/roadmap-items", status_code=201)
def create_roadmap_item(payload: RoadmapItemRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.create_roadmap_item(
                connection,
                project_id=payload.project_id,
                title=payload.title,
                key=payload.key,
                description=payload.description,
                status=payload.status,
                initiative_id=payload.initiative_id,
                module_id=payload.module_id,
                cycle_id=payload.cycle_id,
                target_date=payload.target_date,
                sort_order=payload.sort_order,
                issue_url=payload.issue_url,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/roadmap-items")
def list_roadmap_items(
    project_id: str = Query(...),
    initiative_id: Optional[str] = None,
    module_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_roadmap_items(
            connection,
            project_id,
            initiative_id=initiative_id,
            module_id=module_id,
            cycle_id=cycle_id,
            status=status,
        )
        return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("/dependencies", status_code=201)
def add_dependency(payload: DependencyRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.add_dependency(
                connection,
                project_id=payload.project_id,
                src_type=payload.src_type,
                src_id=payload.src_id,
                dst_type=payload.dst_type,
                dst_id=payload.dst_id,
                kind=payload.kind,
                metadata=payload.metadata,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/dependencies")
def list_dependencies(
    project_id: str = Query(...),
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_dependencies(
            connection,
            project_id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        return {"project_id": project_id, "items": items, "count": len(items)}


@router.post("/link-issue")
def link_issue(payload: LinkIssueRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "command_submit")
        try:
            return _service.link_issue(
                connection,
                project_id=payload.project_id,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                url=payload.url,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
