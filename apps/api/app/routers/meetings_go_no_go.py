"""Isolated Community go/no-go meeting fixture.

Mounted from ``platform.py``. Never calls a live model, never enqueues a
worker job, never reserves budget. Parallel to ``POST /v1/meetings/standup``.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.meeting_service import run_simulated_meeting


router = APIRouter(tags=["meetings-go-no-go"])


class GoNoGoMeetingRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    directive: Optional[str] = None
    url: str = ""
    roles: list[str] = Field(default_factory=list)
    simulated: bool = True


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


@router.post("/v1/meetings/go-no-go", status_code=201)
@permit(P.MEETING_CONVENE, scope=Scope.project(body="project_id"))
def create_and_run_go_no_go(payload: GoNoGoMeetingRequest) -> dict[str, Any]:
    """Convene a typed go/no-go meeting and complete it with the no-spend fixture.

    Produces minutes, an explicit go/no-go Decision, and optional dissent.
    Duplicate running convenes are refused. Requires ``simulated=true``.
    """
    if not payload.simulated:
        raise HTTPException(
            status_code=422,
            detail="simulated=true is required; go/no-go Community path never calls a live model.",
        )
    settings = get_settings()
    with begin(get_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "task_create")
        try:
            return run_simulated_meeting(
                connection,
                settings,
                project_id=payload.project_id,
                actor=payload.actor,
                meeting_id=None,
                directive=payload.directive,
                url=payload.url,
                role_keys=payload.roles,
                meeting_type="go_no_go",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
