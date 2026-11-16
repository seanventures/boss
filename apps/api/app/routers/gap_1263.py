"""Saved prompts over HTTP (BOSSAPP-1263).

POST /v1/prompts                          save a prompt. Does not hire an agent.
GET  /v1/prompts                          the project's saved prompts.
POST /v1/prompts/{prompt_id}/decision     ask for a decision. Still no agent.
POST /v1/prompts/{prompt_id}/promote      hire the agent only when that decision
                                          is approved, through the existing door.

The reconciler mounts `router`. This module does not mount itself.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.db import begin, get_engine
from boss_engine.services.prompt_promote import PromoteError, ask, get_prompt, list_prompts, promote, save_prompt


router = APIRouter(tags=["prompt-promote"])


class SaveBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    title: str
    body: str
    name: Optional[str] = None
    purpose: Optional[str] = None
    role_key: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[Any] = Field(default_factory=list)
    reports_to: Optional[str] = None
    boundaries: Optional[dict[str, Any]] = None
    persona: Optional[dict[str, Any]] = None
    born_from: Optional[dict[str, Any]] = None
    team: Optional[str] = None


class PromptBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


def _fail(error: Exception) -> None:
    if isinstance(error, PromoteError):
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    raise error


def _writer(connection: Any, project_id: str, claimed: Optional[str], permission: str) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, claimed, permission)


def _reader(connection: Any, project_id: str) -> None:
    from apps.api.app.main import require_project_reader

    require_project_reader(connection, project_id)


def _agent(body: SaveBody) -> dict[str, Any] | None:
    given = body.model_fields_set
    agent = {
        name: getattr(body, name)
        for name in ("name", "purpose", "role_key", "skills", "mcp_servers", "reports_to",
                     "boundaries", "persona", "born_from", "team")
        if name in given
    }
    return agent or None


@router.get("/v1/prompts")
@permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
def read_prompts(project_id: str = Query(...)) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _reader(connection, project_id)
        return {"project_id": project_id, "prompts": list_prompts(connection, project_id=project_id)}


@router.post("/v1/prompts", status_code=201)
@permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
def create_prompt(body: SaveBody) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        actor = _writer(connection, body.project_id, body.actor, "role_review")
        try:
            return save_prompt(connection, project_id=body.project_id, actor=actor,
                               title=body.title, body=body.body, agent=_agent(body))
        except PromoteError as error:
            _fail(error)


@router.post("/v1/prompts/{prompt_id}/decision", status_code=202)
@permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
def ask_for_decision(prompt_id: str, body: PromptBody) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        actor = _writer(connection, body.project_id, body.actor, "role_review")
        try:
            return ask(connection, project_id=body.project_id, prompt_id=prompt_id, actor=actor)
        except PromoteError as error:
            _fail(error)


@router.post("/v1/prompts/{prompt_id}/promote", status_code=201)
@permit(P.AGENT_CREATE, scope=Scope.project(body="project_id"))
def promote_prompt(prompt_id: str, body: PromptBody) -> dict[str, Any]:
    """Refuse unless the prompt's decision is approved. The service hires the agent."""
    with begin(get_engine()) as connection:
        actor = _writer(connection, body.project_id, body.actor, "role_review")
        try:
            current = get_prompt(connection, project_id=body.project_id, prompt_id=prompt_id)
        except PromoteError as error:
            _fail(error)
        if current.get("skills") or current.get("mcp_servers"):
            _writer(connection, body.project_id, actor, "workflow_manage")
        try:
            return promote(connection, project_id=body.project_id, prompt_id=prompt_id, actor=actor)
        except PromoteError as error:
            _fail(error)
