"""Publish guards over HTTP (BOSSAPP-1256).

GET  /v1/publish-guards?org_id=
PUT  /v1/publish-guards
POST /v1/publish-guards/publish
POST /v1/publish-guards/finding
POST /v1/publish-guards/source-export
POST /v1/publish-guards/chat-upload
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["publish-guards"])


class PolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    require_private: Optional[bool] = None
    blocked_severity: Optional[str] = Field(default=None, max_length=40)
    ban_source_export: Optional[bool] = None
    ban_chat_upload: Optional[bool] = None


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=200)
    visibility: str = Field(default="", max_length=40)


class FindingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=200)
    severity: str = Field(default="", max_length=40)


class OrgBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=200)


def _call(fn):
    from boss_engine.db import begin
    from boss_engine.services import publish_guards as guards

    try:
        with begin() as connection:
            return fn(connection, guards)
    except guards.PublishGuardRefused as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    except guards.PublishGuardError as exc:
        status = 403 if exc.code == "not_admin" else 422
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc


@router.get("/v1/publish-guards")
@permit(P.ORG_READ, scope=Scope.org())
def get_publish_guards(org_id: str) -> dict[str, Any]:
    return _call(lambda connection, guards: guards.policy(connection, org_id))


@router.put("/v1/publish-guards")
@permit(P.ORG_UPDATE, scope=Scope.org())
def put_publish_guards(payload: PolicyWrite) -> dict[str, Any]:
    fields = payload.model_dump(exclude_unset=True)
    org_id = fields.pop("org_id")
    actor = fields.pop("actor")
    return _call(lambda connection, guards: guards.set_policy(
        connection, org_id, actor=actor, **fields))


@router.post("/v1/publish-guards/publish")
@permit(P.ORG_READ, scope=Scope.org(), readonly_post=True)
def post_publish(payload: PublishBody) -> dict[str, Any]:
    def run(connection, guards):
        guards.check_publish(connection, payload.org_id, payload.visibility)
        return {"allowed": True}

    return _call(run)


@router.post("/v1/publish-guards/finding")
@permit(P.ORG_READ, scope=Scope.org(), readonly_post=True)
def post_finding(payload: FindingBody) -> dict[str, Any]:
    def run(connection, guards):
        guards.check_finding(connection, payload.org_id, payload.severity)
        return {"allowed": True}

    return _call(run)


@router.post("/v1/publish-guards/source-export")
@permit(P.ORG_READ, scope=Scope.org(), readonly_post=True)
def post_source_export(payload: OrgBody) -> dict[str, Any]:
    def run(connection, guards):
        guards.check_source_export(connection, payload.org_id)
        return {"allowed": True}

    return _call(run)


@router.post("/v1/publish-guards/chat-upload")
@permit(P.ORG_READ, scope=Scope.org(), readonly_post=True)
def post_chat_upload(payload: OrgBody) -> dict[str, Any]:
    def run(connection, guards):
        guards.check_chat_upload(connection, payload.org_id)
        return {"allowed": True}

    return _call(run)
