"""Factory flow debug board + improve tick."""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["factory-flow"])


class FlowTickRequest(BaseModel):
    project_id: str
    actor: str = "factory_flow"


def _service():
    from boss_engine.services.factory_flow import FactoryFlowService

    return FactoryFlowService(get_settings())


@router.get("/v1/factory/flow")
@permit(P.CODE_TASK_READ, scope=Scope.project(query="project_id"))
def factory_flow_board(project_id: str) -> dict[str, Any]:
    """Unified official/factory + captain-swarm flow. Evidence only."""
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        return _service().board(connection, project_id)


@router.get("/v1/factory/flow/{run_id}")
@permit(P.CODE_TASK_READ, scope=Scope.project(query="project_id"))
def factory_flow_one(run_id: str, project_id: str) -> dict[str, Any]:
    if not project_id.strip() or not run_id.strip():
        raise HTTPException(status_code=422, detail="project_id and run_id are required")
    from sqlalchemy import text

    with begin(get_engine()) as connection:
        row = connection.execute(
            text(
                "SELECT id, status, phase, title, error, state, finalize_ctx, updated_at "
                "FROM code_task_runs WHERE id=:id AND project_id=:p"
            ),
            {"id": run_id, "p": project_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="code task not found")
        return _service().snapshot(connection, project_id, dict(row))


@router.post("/v1/factory/flow/tick")
@permit(P.SYSTEM_OPERATE, scope=Scope.project(body="project_id"))
def factory_flow_tick(payload: FlowTickRequest) -> dict[str, Any]:
    """Observe stalls, record findings, attach missing official swarms. No code edits."""
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    with begin(get_engine()) as connection:
        return _service().tick(
            connection, payload.project_id, actor=payload.actor or "factory_flow",
        )
