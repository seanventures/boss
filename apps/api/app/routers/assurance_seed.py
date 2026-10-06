"""Isolated Community assurance demo seed.

Creates one deterministic demo metric + case labeled ``seeded: true``.
Never claims production health. Fail-closed when assurance tables are missing.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.services.assurance import (
    AssuranceConflictError,
    AssuranceNotFoundError,
    AssuranceService,
)


router = APIRouter(tags=["assurance-seed"])


class SeedDemoRequest(BaseModel):
    project_id: str
    actor: str = "ceo"


def _require_actor(connection: Any, project_id: str, actor: str, permission: str) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, permission)


@router.post("/v1/assurance/seed-demo")
@permit(P.ASSURANCE_MANAGE, scope=Scope.project(body="project_id"))
def seed_assurance_demo(payload: SeedDemoRequest) -> dict[str, Any]:
    settings = get_settings()
    service = AssuranceService(settings)
    with begin(get_engine()) as connection:
        _require_actor(connection, payload.project_id, payload.actor, "workflow_manage")
        try:
            return service.seed_demo(
                connection,
                project_id=payload.project_id,
                actor=payload.actor,
            )
        except AssuranceNotFoundError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except AssuranceConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
