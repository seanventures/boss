"""No-spend preview fixture so View HTML can be proven without a microVM."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.services.preview_service import create_fixture_preview

from ..config import get_settings
from ..db import begin, get_engine


router = APIRouter(tags=["previews"])


class PreviewFixtureRequest(BaseModel):
    project_id: str
    actor: str = "ceo"
    ttl_minutes: int = Field(default=60, ge=5, le=180)


@router.post("/v1/previews/fixture", status_code=201)
def start_preview_fixture(payload: PreviewFixtureRequest) -> dict[str, Any]:
    """Create a running fixture preview. Serves honest HTML at /web/. No sandbox."""
    settings = get_settings()
    if not payload.project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    try:
        with begin(get_engine()) as connection:
            from apps.api.app.main import require_project_actor

            require_project_actor(
                connection, payload.project_id, payload.actor, "task_create"
            )
        return create_fixture_preview(
            get_engine(),
            settings,
            project_id=payload.project_id,
            actor=payload.actor,
            ttl_minutes=payload.ttl_minutes,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
