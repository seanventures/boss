"""The reflection agent over HTTP (BOSSAPP-888): its schedule, its runs, and Reflect now.

  GET  /v1/reflection?project_id=          the schedule (paused by default) and recent runs
  PUT  /v1/reflection/settings             turn the schedule on or off, cadence, which agents
  POST /v1/reflection/run                  reflect on one agent now

Reflection only PROPOSES: each run opens diff-bound consents (a skill_diff through the draft_skill
path, an agent_prompt_diff as an instruction proposal) that a person approves in Needs you.
Reading is agent:read; changing the schedule or pressing Reflect now is agent:update, attributed to
the signed-in member (role_review).
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None


class SettingsBody(Body):
    enabled: Optional[bool] = None
    every_hours: Optional[int] = Field(default=None, ge=1, le=2160)
    roles: Optional[list[str]] = Field(default=None, max_length=50)


class RunBody(Body):
    role_key: str = Field(min_length=1, max_length=200)
    window_days: int = Field(default=14, ge=1, le=90)


def build_reflection_router(*, engine, require_reader: Callable[..., None],
                            require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import reflection as rf

    router = APIRouter(tags=["reflection"])

    @router.get("/v1/reflection")
    @permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
    def overview(project_id: str) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            return {"settings": rf.get_settings(c, project_id),
                    "runs": rf.list_runs(c, project_id)}

    @router.put("/v1/reflection/settings")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def change_settings(payload: SettingsBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            try:
                return {"settings": rf.set_settings(
                    c, payload.project_id, actor=actor, enabled=payload.enabled,
                    every_hours=payload.every_hours, roles=payload.roles)}
            except rf.ReflectionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/reflection/run")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def run_now(payload: RunBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            try:
                return {"run": rf.reflect(c, payload.project_id, payload.role_key, actor=actor,
                                          window_days=payload.window_days)}
            except rf.ReflectionError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
