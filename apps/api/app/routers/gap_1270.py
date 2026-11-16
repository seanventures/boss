"""Live action rules over HTTP (BOSSAPP-1270).

GET  /v1/live-action-rules            the saved rules, in the order they are read
POST /v1/live-action-rules            record one sentence
POST /v1/live-action-rules/attempt    run the action only when a saved rule allows it

Evaluation is the saved row. This router does not call a model.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services import live_action_rules as rules


router = APIRouter(tags=["live-action-rules"])


class RecordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    app_key: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    actor: str = Field(min_length=1, max_length=200)
    position: int = Field(default=100, ge=0, le=10000)


class AttemptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)
    app_key: str = Field(min_length=1, max_length=64)
    action: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = Field(default=None, max_length=200)


def admit_reader(connection: Any, project_id: str) -> None:
    from apps.api.app.main import require_project_reader

    require_project_reader(connection, project_id)


def admit_writer(connection: Any, project_id: str, actor: Optional[str]) -> str:
    from apps.api.app.main import require_project_write_actor

    return require_project_write_actor(connection, project_id, actor, "decision_resolve")


def _call(fn):
    try:
        return fn()
    except rules.LiveActionFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except rules.LiveActionRuleError as exc:
        status = 404 if str(exc) == "Project not found." else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/v1/live-action-rules")
@permit(P.POLICY_READ, scope=Scope.project(query="project_id"))
def list_live_action_rules(project_id: str = Query(min_length=1, max_length=200),
                           app_key: Optional[str] = Query(default=None, max_length=64)) -> dict:
    with begin(get_engine()) as connection:
        admit_reader(connection, project_id)
        found = _call(lambda: rules.list_rules(connection, project_id, app_key=app_key))
    return {"rules": found, "default": "block"}


@router.post("/v1/live-action-rules", status_code=201)
@permit(P.POLICY_MANAGE, scope=Scope.project(body="project_id"))
def record_live_action_rule(payload: RecordBody) -> dict:
    with begin(get_engine()) as connection:
        actor = admit_writer(connection, payload.project_id, payload.actor)
        rule = _call(lambda: rules.record_rule(
            connection, payload.project_id, payload.description, actor=actor,
            app_key=payload.app_key, position=payload.position))
    return {"rule": rule}


@router.post("/v1/live-action-rules/attempt")
@permit(P.POLICY_MANAGE, scope=Scope.project(body="project_id"))
def attempt_live_action(payload: AttemptBody) -> dict:
    with begin(get_engine()) as connection:
        actor = admit_writer(connection, payload.project_id, payload.actor)
        return _call(lambda: rules.attempt(
            connection, payload.project_id, app_key=payload.app_key, action=payload.action,
            actor=actor, payload=payload.payload, dedupe_key=payload.dedupe_key))
