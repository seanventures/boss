"""Isolated Community meetings E2E surface.

The canonical ``POST /v1/meetings/{id}/run`` lives in ``main.py`` (owned by
another agent). This router adds the no-spend fixture path and operator-visible
phase evidence without touching that file.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services import meeting_consensus
from boss_engine.services.meeting_service import (
    DEFAULT_MEETING_TYPE,
    meeting_evidence,
    normalize_meeting_type,
    run_simulated_meeting,
)


router = APIRouter(tags=["meetings-e2e"])


class SimulatedMeetingRunRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    directive: Optional[str] = None
    url: str = ""
    roles: list[str] = Field(default_factory=list)
    simulated: bool = True
    meeting_type: Literal["prd_panel", "standup"] = DEFAULT_MEETING_TYPE


class StandupMeetingRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    directive: Optional[str] = None
    url: str = ""
    roles: list[str] = Field(default_factory=list)
    simulated: bool = True


class BoundMeetingTallyRequest(BaseModel):
    actor: str = "ceo"
    votes: list[dict[str, Any]] = Field(default_factory=list)
    eligible_count: Optional[int] = Field(default=None, ge=0)
    quorum: Optional[int] = Field(default=None, ge=1)
    vote_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    default_action: str = "hold"


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


@router.post("/v1/meetings/{meeting_id}/simulated-run")
@permit(P.MEETING_CONVENE, scope=Scope.of("meeting", path="meeting_id"))
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
                meeting_type=normalize_meeting_type(payload.meeting_type),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/meetings/simulated-run", status_code=201)
@permit(P.MEETING_CONVENE, scope=Scope.project(body="project_id"))
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
                meeting_type=normalize_meeting_type(payload.meeting_type),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/meetings/standup", status_code=201)
@permit(P.MEETING_CONVENE, scope=Scope.project(body="project_id"))
def create_and_run_standup(payload: StandupMeetingRequest) -> dict[str, Any]:
    """Convene a typed standup and complete it with the no-spend fixture.

    Never calls a live model. Requires ``simulated=true``.
    """
    if not payload.simulated:
        raise HTTPException(
            status_code=422,
            detail="simulated=true is required; standup Community path never calls a live model.",
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
                meeting_type="standup",
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/v1/meetings/{meeting_id}/tally")
@permit(P.MEETING_CONVENE, scope=Scope.of("meeting", path="meeting_id"))
def tally_bound_to_meeting(meeting_id: str, payload: BoundMeetingTallyRequest) -> dict[str, Any]:
    """Tally votes against a real meeting and persist dissent on that row."""
    if not payload.votes:
        raise HTTPException(status_code=422, detail="votes must contain at least one entry.")

    with begin(get_engine()) as connection:
        meeting = connection.execute(
            text("SELECT id, project_id, participants, dissent_records FROM meetings WHERE id = :id"),
            {"id": meeting_id},
        ).mappings().first()
        if meeting is None:
            row = connection.execute(
                text("SELECT meeting_id FROM meeting_runs WHERE id = :id"),
                {"id": meeting_id},
            ).mappings().first()
            if row and row["meeting_id"]:
                meeting = connection.execute(
                    text(
                        "SELECT id, project_id, participants, dissent_records "
                        "FROM meetings WHERE id = :id"
                    ),
                    {"id": row["meeting_id"]},
                ).mappings().first()
            elif row:
                # The panel exists but has not written its minutes: there is nothing to vote on
                # yet. Said in words, not "meeting not found" about a meeting on screen.
                raise HTTPException(
                    status_code=409,
                    detail="This panel has not recorded its minutes yet, so there is no vote to "
                           "take. The vote opens when the panel finishes.")
        if meeting is None:
            raise HTTPException(status_code=404, detail="meeting not found")
        _require_actor(connection, meeting["project_id"], payload.actor, "task_create")
        eligible = (
            payload.eligible_count
            if payload.eligible_count is not None
            else (len(meeting["participants"] or []) or len(payload.votes))
        )
        tally = meeting_consensus.tally_votes(
            payload.votes,
            eligible_count=eligible,
            quorum=payload.quorum,
            threshold=payload.vote_threshold,
            default_action=payload.default_action,
        )
        existing = list(meeting["dissent_records"] or [])
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except Exception:
                existing = []
        bound_dissent = list(existing) + list(tally.get("dissent_records") or [])
        connection.execute(
            text(
                """
                UPDATE meetings
                SET dissent_records = CAST(:dissent AS JSONB),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {
                "dissent": json.dumps(bound_dissent),
                "id": meeting["id"],
            },
        )
        return {**tally, "meeting_id": meeting["id"]}


@router.get("/v1/meetings/{meeting_id}/evidence")
@permit(P.MEETING_READ, scope=Scope.of("meeting", path="meeting_id"))
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
