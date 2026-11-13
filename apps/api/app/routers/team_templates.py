"""Team templates over HTTP (BOSSAPP-894, services/team_templates.py).

    POST /v1/packs/{pack_id}/team    mode=dry_run: the diff (roles, reporting lines, starter work)
                                     and a plan_digest; mode=apply + plan_digest: apply exactly that,
                                     in one transaction (409 when the project moved since)
    GET  /v1/packs/{pack_id}/drift   each applied item now: in_sync, modified or missing

Mounted from main.py with the auth helpers injected.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class TeamBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    mode: Literal["dry_run", "apply"] = "dry_run"
    manager: Optional[str] = None
    plan_digest: Optional[str] = None


def build_team_templates_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import team_templates as tt
    from boss_engine.services.specpack import PackNotFoundError

    router = APIRouter(tags=["packs"])

    @router.post("/v1/packs/{pack_id}/team")
    @permit(P.PACK_INSTALL, scope=Scope.project(body="project_id"))
    def team(pack_id: str, body: TeamBody) -> dict[str, Any]:
        """Dry run first; apply names the plan_digest the dry run returned."""
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
        try:
            if body.mode == "dry_run":
                return tt.plan(engine, settings(), project_id=body.project_id, pack_id=pack_id,
                               manager=body.manager, actor=actor)
            if not body.plan_digest:
                raise HTTPException(status_code=422,
                                    detail="apply names the plan_digest its dry run returned")
            return tt.apply(engine, settings(), project_id=body.project_id, pack_id=pack_id,
                            manager=body.manager, actor=actor, plan_digest=body.plan_digest)
        except PackNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except tt.TeamTemplateError as error:
            raise HTTPException(status_code=getattr(error, "status", 422),
                                detail=str(error)) from error

    @router.get("/v1/packs/{pack_id}/drift")
    @permit(P.PACK_READ, scope=Scope.project(query="project_id"))
    def drift(pack_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            return tt.drift(connection, project_id, pack_id)

    return router
