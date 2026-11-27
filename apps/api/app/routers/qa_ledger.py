"""THE TEST PLAN'S TOOLS over HTTP (`boss qa`).

GET  /v1/qa/ledger?project_id=P[&since=ISO][&case=REC-01]  one row per attempt at a fault case
                                                           (read-only; free text scrubbed as the
                                                           diagnostics report scrubs it;
                                                           services/qa_ledger.py)
PUT  /v1/qa/disk-pressure {level, hours}                   move the storage thresholds just above
                                                           today's free space for a test, marked
                                                           and expiring (qa_disk_pressure.py)
POST /v1/qa/disk-pressure/off                              put back what the test moved
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["qa"])


@router.get("/v1/qa/ledger")
@permit(P.CODE_TASK_READ, scope=Scope.project(query="project_id"))
def get_qa_ledger(project_id: str, since: Optional[datetime] = None, case: Optional[str] = None,
                  limit: int = 200) -> dict[str, Any]:
    from boss_engine.services import qa_ledger
    from boss_engine.services.project_experience.common import require_project

    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with begin(get_engine()) as connection:
        try:
            require_project(connection, project_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from None
        try:
            out = qa_ledger.ledger(connection, project_id, since=since, case=case, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
    return {**out, "markdown": qa_ledger.as_markdown(out["rows"])}


class PressureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = Field(pattern="^(warn|critical|floor)$")
    hours: float = Field(default=24, gt=0, le=72)
    host_id: Optional[str] = Field(default=None, max_length=120)


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "id", None) or getattr(principal, "principal_id", None)
               or "operator")


@router.put("/v1/qa/disk-pressure")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def put_qa_disk_pressure(request: Request, payload: PressureBody) -> dict[str, Any]:
    from boss_engine.services import qa_disk_pressure

    with begin(get_engine()) as connection:
        try:
            return qa_disk_pressure.apply(connection, level=payload.level, hours=payload.hours,
                                          host_id=payload.host_id, actor=_actor(request))
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None


@router.post("/v1/qa/disk-pressure/off")
@permit(P.AUTONOMY_MANAGE, scope=Scope.org())
def post_qa_disk_pressure_off(request: Request) -> dict[str, Any]:
    from boss_engine.services import qa_disk_pressure

    with begin(get_engine()) as connection:
        return qa_disk_pressure.restore(connection, actor=_actor(request),
                                        why="boss qa disk-pressure off")
