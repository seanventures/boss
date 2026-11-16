"""Side-by-side model compare (BOSSAPP-1262).

POST /v1/model-compares   {project_id, prompt, models}

Each column spends through the bound tenant gateway. Nothing in this router
talks to a model vendor. With no gateway bound, every column fails in the open.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["model-compare"])

_gateway: Any = None


def bind_gateway(gateway: Optional[Any]) -> None:
    """Install the tenant gateway. None restores the unconnected stand-in."""
    global _gateway
    _gateway = gateway


class CompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    models: list[str] = Field(min_length=2, max_length=4)


@router.post("/v1/model-compares")
@permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
def compare_models(body: CompareBody) -> dict[str, Any]:
    from boss_engine.db import get_engine
    from boss_engine.services.model_compare import (
        CompareError,
        UnconnectedTenantGateway,
        compare_prompt,
    )

    gateway = _gateway if _gateway is not None else UnconnectedTenantGateway()
    try:
        return compare_prompt(
            get_engine(),
            project_id=body.project_id,
            prompt=body.prompt,
            models=list(body.models),
            gateway=gateway,
        )
    except CompareError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
