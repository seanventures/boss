"""HTTP surface for project delivery board + release planning.

Prefix: ``/v1/release-planning``

* GET  /status?project_id=
* GET  /board?project_id=
* POST /releases
* GET  /releases?project_id=
* GET  /releases/{id}
* POST /releases/{id}/link-issue
* POST /sync-from-features
* POST /sync-from-trackers
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.release_planning import ReleasePlanningService


router = APIRouter(prefix="/v1/release-planning", tags=["release-planning"])

_settings = get_settings()
_service = ReleasePlanningService(_settings)
_engine = get_engine(_settings)


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    """Lazy import avoids circular import with main (which mounts this router)."""
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


class ActorBody(BaseModel):
    project_id: str
    actor: str = "system"


class CreateReleaseRequest(ActorBody):
    name: str = Field(..., min_length=1)
    target_date: Optional[str] = None
    item_ids: list[str] = Field(default_factory=list)
    status: str = "planned"
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkIssueRequest(ActorBody):
    tracker: str = Field(..., min_length=1)
    issue_key: str = Field(..., min_length=1)
    url: Optional[str] = None


@router.get("/status")
def get_status(project_id: str = Query(...)) -> dict[str, Any]:
    """Mission Control status snapshot: built / todo / in_progress / blocked."""
    with begin(_engine) as connection:
        return _service.status_snapshot(connection, project_id)


@router.get("/board")
def get_board(project_id: str = Query(...)) -> dict[str, Any]:
    """Delivery board columns: built, building, planned, blocked."""
    with begin(_engine) as connection:
        return _service.board(connection, project_id)


@router.post("/releases", status_code=201)
def create_release(payload: CreateReleaseRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(
            connection, payload.project_id, payload.actor, "command_submit"
        )
        try:
            return _service.create_release(
                connection,
                project_id=payload.project_id,
                name=payload.name,
                target_date=payload.target_date,
                item_ids=payload.item_ids,
                actor=payload.actor,
                status=payload.status,
                metadata=payload.metadata,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/releases")
def list_releases(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(_engine) as connection:
        items = _service.list_releases(connection, project_id)
        return {
            "project_id": project_id,
            "items": items,
            "count": len(items),
        }


@router.get("/releases/{release_id}")
def get_release(
    release_id: str, project_id: str = Query(...)
) -> dict[str, Any]:
    with begin(_engine) as connection:
        try:
            return _service.get_release(connection, project_id, release_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/releases/{release_id}/link-issue")
def link_issue(release_id: str, payload: LinkIssueRequest) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require_actor(
            connection, payload.project_id, payload.actor, "command_submit"
        )
        try:
            return _service.link_issue(
                connection,
                project_id=payload.project_id,
                release_id=release_id,
                tracker=payload.tracker,
                issue_key=payload.issue_key,
                url=payload.url,
                actor=payload.actor,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/sync-from-features")
def sync_from_features(payload: ActorBody) -> dict[str, Any]:
    """Pull feature catalog entries into the durable board."""
    with begin(_engine) as connection:
        _require_actor(
            connection, payload.project_id, payload.actor, "command_submit"
        )
        return _service.sync_from_features(
            connection, payload.project_id, actor=payload.actor
        )


@router.post("/sync-from-trackers")
def sync_from_trackers(payload: ActorBody) -> dict[str, Any]:
    """Best-effort list from work tracker bindings (always soft-success)."""
    with begin(_engine) as connection:
        _require_actor(
            connection, payload.project_id, payload.actor, "command_submit"
        )
        return _service.sync_from_trackers(
            connection, payload.project_id, actor=payload.actor
        )
