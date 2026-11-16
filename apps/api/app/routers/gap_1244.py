"""Chat and Plan on the builder thread (BOSSAPP-1244).

The reconciler mounts this router. Chat and Plan do not write code.
Build writes code only after a person approves the plan.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.identity import P, Scope, permit
from boss_engine.services.builder_chat_plan import (
    BuilderRefused,
    approve_plan,
    open_thread,
    post_turn,
    read_thread,
    resolve_root,
)

router = APIRouter(tags=["builder-thread"])


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1, max_length=80)


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mode: str = Field(min_length=1, max_length=20)
    text: str = Field(default="", max_length=8000)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    person_id: str = Field(default="", max_length=80)


def _root():
    return resolve_root(None)


def _fail(error: BuilderRefused) -> JSONResponse:
    return JSONResponse(error.body(), status_code=error.status)


@router.post("/v1/builder/threads", status_code=201)
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def open_builder_thread(body: OpenBody) -> Any:
    try:
        return open_thread(_root(), name=body.name)
    except BuilderRefused as error:
        return _fail(error)


@router.get("/v1/builder/threads/{thread_id}")
@permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
def get_builder_thread(thread_id: str) -> Any:
    try:
        return read_thread(_root(), thread_id)
    except BuilderRefused as error:
        return _fail(error)


@router.post("/v1/builder/threads/{thread_id}/turns")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def builder_turn(thread_id: str, body: TurnBody) -> Any:
    try:
        return post_turn(_root(), thread_id, mode=body.mode, text=body.text)
    except BuilderRefused as error:
        return _fail(error)


@router.post("/v1/builder/threads/{thread_id}/approvals")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(body="project_id"))
def builder_approval(thread_id: str, body: ApproveBody) -> Any:
    try:
        return approve_plan(_root(), thread_id, person_id=body.person_id)
    except BuilderRefused as error:
        return _fail(error)
