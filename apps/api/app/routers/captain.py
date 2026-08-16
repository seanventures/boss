"""Captain coordination HTTP — roster, staffing plan, coordinate."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.captain_coordination import roster

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["captain"])


class CoordinateRequest(BaseModel):
    project_id: str
    actor: str = "engineering_captain"


def _service():
    from boss_engine.services.captain_coordination import CaptainCoordinationService

    return CaptainCoordinationService(get_settings())


@router.get("/v1/captain/roster")
def captain_roster() -> dict[str, Any]:
    """Catalog of agents the captain may staff. Not a scorecard."""
    return {"agents": roster()}


@router.get("/v1/captain/plan")
def captain_plan(project_id: str, run_id: str) -> dict[str, Any]:
    if not project_id.strip() or not run_id.strip():
        raise HTTPException(status_code=422, detail="project_id and run_id are required")
    with begin(get_engine()) as connection:
        return _service().plan_for_run(connection, project_id, run_id)


@router.post("/v1/captain/coordinate")
def captain_coordinate(payload: CoordinateRequest) -> dict[str, Any]:
    """Staff in-flight work, persist now-assignments, convene panels as needed."""
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        return _service().coordinate(
            connection, payload.project_id, actor=payload.actor or "engineering_captain",
        )
