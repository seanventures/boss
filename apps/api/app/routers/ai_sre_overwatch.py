"""HTTP surface for the observe-only AI SRE overwatch tick.

Mounted at ``POST /v1/ai-sre/overwatch``. Kept in its own router so parallel
kernel work on ``ai_sre.py`` is not disturbed.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.services.ai_sre_overwatch import tick

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(prefix="/v1/ai-sre", tags=["ai-sre"])


class OverwatchRequest(BaseModel):
    project_id: str
    actor: str = "metatron"


def _require(connection, project_id: str, actor: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "agent_chat")


@router.post("/overwatch")
def ai_sre_overwatch(payload: OverwatchRequest) -> dict[str, Any]:
    """Observe-only: scan due sources, diagnose open/critical, optional dry-run page."""
    project_id = (payload.project_id or "").strip()
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        _require(connection, project_id, payload.actor)
    return tick(
        engine,
        settings,
        project_id=project_id,
        actor=payload.actor or "metatron",
    )
