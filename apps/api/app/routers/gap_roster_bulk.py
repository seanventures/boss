"""One action sets the model connection and default model on many agents.

The reconciler mounts `router`. This module does not write the roles row.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.roster_bulk import RosterBulkError, apply, snapshot

router = APIRouter(tags=["roster-bulk"])


class _PinSeen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: Optional[str] = None
    default_model: Optional[str] = None


class _AgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_key: str
    expected: Optional[_PinSeen] = None


class _BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    actor: str
    dedupe_key: str
    connection_id: Optional[str] = None
    default_model: Optional[str] = None
    agents: list[Union[str, _AgentIn]]


def _open():
    return begin(get_engine())


def _agents(body: _BulkIn) -> list[Any]:
    parsed: list[Any] = []
    for item in body.agents:
        if isinstance(item, str):
            parsed.append(item)
        else:
            parsed.append(item.model_dump())
    return parsed


@router.get("/v1/agents/roster-bulk")
@permit(P.AGENT_READ, scope=Scope.project(query="project_id"))
def list_roster(project_id: str) -> dict[str, Any]:
    try:
        with _open() as connection:
            return snapshot(connection, project_id)
    except RosterBulkError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/v1/agents/roster-bulk")
@permit(P.AGENT_UPDATE, scope=Scope.project(body="project_id"))
def post_roster(body: _BulkIn) -> dict[str, Any]:
    try:
        with _open() as connection:
            return apply(
                connection,
                body.project_id,
                actor=body.actor,
                dedupe_key=body.dedupe_key,
                agents=_agents(body),
                connection_id=body.connection_id,
                default_model=body.default_model,
            )
    except RosterBulkError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
