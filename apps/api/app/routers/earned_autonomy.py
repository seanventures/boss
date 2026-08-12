"""Earned autonomy + value-based scheduling API (BOSS-348 / G1–G3).

Additive router — keeps score read and schedule-preference out of the contested
``main.py`` surface. Evaluate and gate-check remain on the canonical
``/v1/roles/{role_key}/autonomy/*`` paths in main; this router mirrors score
read and exposes the pure schedule-preference ranking used under budget pressure.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.schemas import AutonomyScoreResponse
from boss_engine.services.autonomy_engine import AutonomyEngine
from boss_engine.services.specpack import RoleNotFoundError
from boss_engine.services.work_orchestration import WorkOrchestrationService

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["earned-autonomy"])


class ScheduleCandidate(BaseModel):
    id: str
    priority_score: float = Field(default=0, ge=0, le=100)
    estimated_cost_usd_micros: int = Field(default=0, ge=0)
    # Optional free-form fields pass through ranking unchanged.
    title: Optional[str] = None
    kind: Optional[str] = None


class SchedulePreferenceRequest(BaseModel):
    candidates: list[ScheduleCandidate] = Field(default_factory=list)
    budget_state: str = "green"


@router.get(
    "/v1/roles/{role_key}/autonomy/score",
    response_model=AutonomyScoreResponse,
)
def get_autonomy_score(
    role_key: str,
    project_id: str = Query(...),
) -> AutonomyScoreResponse:
    """G1 — read the current earned-autonomy score (trust + level + risk ceiling)."""
    settings = get_settings()
    engine = AutonomyEngine(settings)
    try:
        with begin(get_engine()) as connection:
            payload = engine.get_score(
                connection, project_id=project_id, role_key=role_key
            )
    except RoleNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return AutonomyScoreResponse(**payload)


@router.post("/v1/work-orchestration/schedule-preference")
def schedule_preference(payload: SchedulePreferenceRequest) -> dict[str, Any]:
    """G3 — pure ranking of work by priority or value density under budget state.

    No DB write. Under yellow/orange/red, value-per-dollar wins; green prefers
    priority_score with density as a tie-break. Claim path uses the same helper.
    """
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    try:
        return service.schedule_preference(
            [item.model_dump() for item in payload.candidates],
            budget_state=payload.budget_state,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
