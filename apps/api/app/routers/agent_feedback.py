"""Agent feedback over HTTP (BOSSAPP-889): what agents complained about or suggested.

  GET   /v1/agent-feedback?project_id=&status=&role_key=&target_kind=   the records, and the same
                                                                      records grouped by target
  PATCH /v1/agent-feedback/{feedback_id}                              a person acknowledges,
                                                                      resolves or dismisses one

Agents file with the `report_friction` tool (every agent's baseline); there is no POST here, so a
person cannot put words in an agent's mouth. Reading is activity:read; changing a record's status
is agent:update, attributed to the signed-in member (role_review), never a claimed actor.
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    actor: Optional[str] = None
    status: str = Field(pattern="^(open|acknowledged|resolved|dismissed)$")
    resolution: Optional[str] = Field(default=None, max_length=2000)


def build_agent_feedback_router(*, engine, require_reader: Callable[..., None],
                                require_write_actor: Callable[..., str]) -> APIRouter:
    from boss_engine.services import agent_feedback as fb

    router = APIRouter(tags=["agent-feedback"])

    @router.get("/v1/agent-feedback")
    @permit(P.ACTIVITY_READ, scope=Scope.project(query="project_id"))
    def list_feedback(project_id: str,
                      status: Optional[str] = Query(
                          None, pattern="^(open|acknowledged|resolved|dismissed)$"),
                      role_key: Optional[str] = Query(None, max_length=200),
                      target_kind: Optional[str] = Query(None, max_length=40),
                      limit: int = Query(200, ge=1, le=500)) -> dict:
        with begin(engine) as c:
            require_reader(c, project_id)
            try:
                items = fb.list_feedback(c, project_id, status=status, role_key=role_key,
                                         target_kind=target_kind, limit=limit)
            except fb.AgentFeedbackError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"feedback": items, "groups": fb.grouped(items),
                "kinds": list(fb.KINDS), "target_kinds": list(fb.TARGET_KINDS)}

    @router.patch("/v1/agent-feedback/{feedback_id}")
    @permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
    def set_status(feedback_id: str, payload: StatusBody) -> dict:
        with begin(engine) as c:
            actor = require_write_actor(c, payload.project_id, payload.actor, "role_review")
            try:
                item = fb.set_status(c, payload.project_id, feedback_id, status=payload.status,
                                     actor=actor, resolution=payload.resolution)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except fb.AgentFeedbackError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"feedback": item}

    return router
