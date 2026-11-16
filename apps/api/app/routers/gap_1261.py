"""Project prompt playground (BOSSAPP-1261).

POST /v1/playground/runs — one prompt, one model. Spend goes through the
tenant model gateway inside prompt_playground.run. This router does not call
a provider.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.prompt_playground import (
    PlaygroundError,
    PlaygroundFailed,
    PlaygroundPending,
    PlaygroundRefused,
    run,
)

from ..config import get_settings
from ..db import begin, get_engine

router = APIRouter(tags=["playground"])


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = Field(default=None, max_length=200)


@router.post("/v1/playground/runs")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def run_prompt(payload: RunBody) -> dict[str, Any]:
    project_id = payload.project_id.strip()
    if not project_id:
        raise HTTPException(status_code=422, detail="Name a project.")
    actor = (payload.actor or "").strip() or "operator"
    settings = get_settings()
    engine = get_engine(settings)
    with begin(engine) as connection:
        from apps.api.app.main import require_project_actor

        require_project_actor(connection, project_id, actor, "assistant:chat")
    try:
        return run(
            engine,
            settings,
            project_id,
            prompt=payload.prompt,
            model=payload.model,
            actor=actor,
        )
    except PlaygroundRefused as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except PlaygroundPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlaygroundFailed as exc:
        code = 409 if exc.replay else 502
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except PlaygroundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
