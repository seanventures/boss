"""Isolated Community meetings E2E surface.

The canonical ``POST /v1/meetings/{id}/run`` lives in ``main.py`` (owned by
another agent). This router adds the no-spend fixture path and operator-visible
phase evidence without touching that file.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.meeting_service import meeting_evidence, run_simulated_meeting


router = APIRouter(tags=["meetings-e2e"])


class SimulatedMeetingRunRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    directive: Optional[str] = None
    url: str = ""
    roles: list[str] = Field(default_factory=list)
    simulated: bool = True


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


@router.post("/v1/meetings/{meeting_id}/simulated-run")
def simulated_meeting_run(meeting_id: str, payload: SimulatedMeetingRunRequest) -> dict[str, Any]:
    """Walk research→round1→round2→synthesis→draft→finalize with fixtures.

    Never calls a live model. Requires ``simulated=true``.
    """
    if not payload.simulated:
        raise HTTPException(
            status_code=422,
            detail="simulated=true is required; this route never calls a live model.",
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
                meeting_id=meeting_id,
                directive=payload.directive,
                url=payload.url,
                role_keys=payload.roles,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/meetings/simulated-run", status_code=201)
def create_and_run_simulated_meeting(payload: SimulatedMeetingRunRequest) -> dict[str, Any]:
    if not payload.simulated:
        raise HTTPException(
            status_code=422,
            detail="simulated=true is required; this route never calls a live model.",
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
                directive=payload.directive or "Community simulated panel fixture",
                url=payload.url,
                role_keys=payload.roles,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/v1/meetings/{meeting_id}/evidence")
def get_meeting_evidence(
    meeting_id: str,
    project_id: str = Query(...),
) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        payload = meeting_evidence(connection, meeting_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="meeting not found")
        if payload.get("project_id") and payload["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="meeting not found")
        return payload
