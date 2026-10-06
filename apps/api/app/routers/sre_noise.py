"""HTTP surface for AIOps noise reduction and NL observability query plans.

Mounted at ``/v1/ai-sre``. Does not execute live PromQL/Datadog.
Not public GA.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.services.sre_ask import plan_query
from boss_engine.services.sre_noise import noise_report

from ..config import get_settings
from ..db import begin, database_is_reachable, get_engine

router = APIRouter(prefix="/v1/ai-sre", tags=["ai-sre"])


class AskBody(BaseModel):
    project_id: str
    question: str = Field(..., min_length=1)


def _require_db() -> None:
    engine = get_engine(get_settings())
    if not database_is_reachable(engine):
        raise HTTPException(status_code=503, detail="Postgres is not reachable.")


@router.get("/noise")
@permit(P.RELIABILITY_READ, scope=Scope.project(query="project_id"))
def get_noise(project_id: str = Query(...)) -> dict[str, Any]:
    """Clustered incidents vs raw signals for a project. Honest zeros when empty."""
    pid = project_id.strip()
    if not pid:
        raise HTTPException(status_code=422, detail="project_id is required")
    _require_db()
    engine = get_engine(get_settings())
    with begin(engine) as connection:
        return noise_report(connection, pid)


@router.post("/ask")
@permit(P.RELIABILITY_READ, scope=Scope.project(body="project_id"), readonly_post=True)
def post_ask(payload: AskBody) -> dict[str, Any]:
    """Translate a question into a query plan. Never executes it."""
    pid = payload.project_id.strip()
    if not pid:
        raise HTTPException(status_code=422, detail="project_id is required")
    return plan_query(pid, payload.question)
