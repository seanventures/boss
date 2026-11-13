"""Hosted remote agents API (BOSSAPP-855).

Org-level profiles for agents BOSS drives over boss.harness.remote/v1 (services/remote_agents).
A profile is usable as harness `remote:<name>` only once it has a spend ceiling (required at
creation) and a person's retention acknowledgement. No token value rides this API: a profile
names the environment variable that holds it.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services import remote_agents as ra

from ..db import begin, get_engine
from ..identity_guard import writing_get

router = APIRouter(prefix="/v1/remote-agents", tags=["remote-agents"])


class CreateRequest(BaseModel):
    project_id: str
    actor: str
    name: str
    base_url: str
    spend_ceiling_usd: float
    token_env: str = ""
    provider: str = "generic"
    description: str = ""
    poll_seconds: float = Field(default=3.0, gt=0, le=60)
    timeout_seconds: int = Field(default=3600, ge=60, le=86400)


class AcknowledgeRequest(BaseModel):
    project_id: str
    actor: str
    terms: str


class UpdateRequest(BaseModel):
    project_id: str
    actor: str
    spend_ceiling_usd: Optional[float] = None
    status: Optional[str] = None


def _require(connection: Any, project_id: str, actor: str, *, mutating: bool = False) -> None:
    from apps.api.app.main import require_project_actor

    require_project_actor(connection, project_id, actor, "providerops_manage")
    if mutating:
        require_project_actor(connection, project_id, actor, "workflow_execute")


def _fail(error: Exception):
    if isinstance(error, ra.RemoteAgentError):
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    raise error


@writing_get(router, "")
@permit(P.INTEGRATION_READ, scope=Scope.project(query="project_id"), writes_on_get=True)
def list_remote_agents(project_id: str = Query(...), actor: str = Query(...)) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, project_id, actor)
        try:
            items = ra.list_profiles(connection, project_id)
        except Exception as error:  # noqa: BLE001
            _fail(error)
    return {"project_id": project_id, "items": items, "contract": ra.CONTRACT}


@router.post("")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def create_remote_agent(payload: CreateRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor, mutating=True)
        try:
            return ra.create_profile(
                connection, payload.project_id, actor=payload.actor, name=payload.name,
                base_url=payload.base_url, spend_ceiling_usd=payload.spend_ceiling_usd,
                token_env=payload.token_env, provider=payload.provider,
                description=payload.description, poll_seconds=payload.poll_seconds,
                timeout_seconds=payload.timeout_seconds)
        except Exception as error:  # noqa: BLE001
            _fail(error)


@router.post("/{profile_id}/acknowledge-retention")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def acknowledge_retention(profile_id: str, payload: AcknowledgeRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor, mutating=True)
        try:
            return ra.acknowledge_retention(connection, payload.project_id, profile_id,
                                            actor=payload.actor, terms=payload.terms)
        except Exception as error:  # noqa: BLE001
            _fail(error)


@router.patch("/{profile_id}")
@permit(P.INTEGRATION_MANAGE, scope=Scope.project(body="project_id"))
def update_remote_agent(profile_id: str, payload: UpdateRequest) -> dict[str, Any]:
    with begin(get_engine()) as connection:
        _require(connection, payload.project_id, payload.actor, mutating=True)
        try:
            return ra.update_profile(connection, payload.project_id, profile_id,
                                     actor=payload.actor,
                                     spend_ceiling_usd=payload.spend_ceiling_usd,
                                     status=payload.status)
        except Exception as error:  # noqa: BLE001
            _fail(error)
