"""Field gates (BOSSAPP-1268). The builder sets who can read or change each field.
Opening the app and writing a value both go through field_acl as the named user.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services import field_acl
from boss_engine.services.field_acl import FieldAclError, FieldWriteRefused

router = APIRouter(tags=["field-acl"])
_engine = get_engine(get_settings())


class FieldIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    label: str


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    actor: Optional[str] = None
    fields: list[FieldIn] = Field(default_factory=list)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    field_key: str
    subject_kind: str
    subject_id: str
    can_read: bool = False
    can_change: bool = False


class GrantsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    grants: list[GrantIn] = Field(default_factory=list)


class MemberBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=list)


class RecordBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str = Field(min_length=1, max_length=200)
    as_user: str
    actor: Optional[str] = None
    values: dict[str, Any] = Field(default_factory=dict)


def _call(fn):
    try:
        with begin(_engine) as connection:
            return fn(connection)
    except FieldWriteRefused as error:
        return JSONResponse({"error": error.code, "message": str(error)}, status_code=error.status)
    except FieldAclError as error:
        return JSONResponse({"error": error.code, "message": str(error)}, status_code=error.status)


@router.get("/v1/field-acl/apps")
@permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
def list_field_apps(project_id: str) -> Any:
    return _call(lambda connection: {"apps": field_acl.list_apps(connection, project_id)})


@router.post("/v1/field-acl/apps")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def create_field_app(payload: CreateBody) -> Any:
    def run(connection):
        return field_acl.create_app(
            connection, project_id=payload.project_id, name=payload.name,
            created_by=payload.actor or "builder",
            fields=[field.model_dump() for field in payload.fields],
        )

    return _call(run)


@router.put("/v1/field-acl/apps/{app_id}/grants")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def put_field_grants(app_id: str, payload: GrantsBody) -> Any:
    def run(connection):
        grants = field_acl.set_grants(
            connection, app_id, [grant.model_dump() for grant in payload.grants],
            project_id=payload.project_id,
        )
        return {"grants": grants}

    return _call(run)


@router.put("/v1/field-acl/apps/{app_id}/members/{user_id}")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def put_field_member(app_id: str, user_id: str, payload: MemberBody) -> Any:
    def run(connection):
        return field_acl.set_member(
            connection, app_id, user_id, payload.roles, project_id=payload.project_id,
        )

    return _call(run)


@router.get("/v1/field-acl/apps/{app_id}/open")
@permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
def open_field_app(app_id: str, project_id: str, as_user: str, actor: Optional[str] = None) -> Any:
    def run(connection):
        return field_acl.open_app(
            connection, app_id, as_user=as_user, actor=actor, project_id=project_id,
        )

    return _call(run)


@router.put("/v1/field-acl/apps/{app_id}/record")
@permit(P.PROJECT_UPDATE, scope=Scope.project(body="project_id"))
def put_field_record(app_id: str, payload: RecordBody) -> Any:
    def run(connection):
        return field_acl.write_fields(
            connection, app_id, as_user=payload.as_user, values=payload.values,
            actor=payload.actor, project_id=payload.project_id,
        )

    return _call(run)
