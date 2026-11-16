"""Isolated task copies and the apply board (BOSSAPP-1252).

The shared branch moves only when a finished copy is applied. An unfinished
apply is a visible refusal. A failed run and a moved branch are dead letters.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services import task_copies as copies

router = APIRouter(tags=["task-copies"])


class OpenBoard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shared_branch: str = Field(min_length=1, max_length=121)
    shared_head: str = Field(min_length=1, max_length=128)
    actor: str = Field(default="operator", min_length=1, max_length=80)


class StartCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board_id: str = Field(min_length=1, max_length=80)
    task_key: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    teammate: str = Field(min_length=1, max_length=80)
    actor: str = Field(default="operator", min_length=1, max_length=80)


class Work(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=2000)
    copy_head: str = Field(min_length=1, max_length=128)
    actor: str = Field(default="operator", min_length=1, max_length=80)


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(default="operator", min_length=1, max_length=80)


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(default="operator", min_length=1, max_length=80)


class Rebase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    copy_head: str = Field(min_length=1, max_length=128)
    actor: str = Field(default="operator", min_length=1, max_length=80)


def _err(status: int, exc: copies.TaskCopyError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=status)


def _call(fn):
    # Catch inside the transaction so a dead letter commits with the refusal.
    with begin(get_engine()) as connection:
        try:
            return fn(connection)
        except copies.TaskCopyMissing as exc:
            return _err(404, exc)
        except copies.TaskCopyConflict as exc:
            return _err(409, exc)
        except copies.TaskCopyRefused as exc:
            return _err(409, exc)
        except copies.TaskCopyError as exc:
            return _err(422, exc)


@router.post("/v1/projects/{project_id}/task-copy-boards")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def open_board(project_id: str, payload: OpenBoard):
    return _call(lambda connection: copies.open_board(
        connection, project_id=project_id, shared_branch=payload.shared_branch,
        shared_head=payload.shared_head, actor=payload.actor))


@router.get("/v1/projects/{project_id}/task-copy-boards/{board_id}")
@permit(P.WORK_ITEM_READ, scope=Scope.project(path="project_id"))
def read_board(project_id: str, board_id: str):
    return _call(lambda connection: copies.board_view(
        connection, project_id=project_id, board_id=board_id))


@router.post("/v1/projects/{project_id}/task-copies")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def start_copy(project_id: str, payload: StartCopy):
    return _call(lambda connection: copies.start_copy(
        connection, project_id=project_id, board_id=payload.board_id, task_key=payload.task_key,
        title=payload.title, teammate=payload.teammate, actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/work")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def record_work(project_id: str, copy_id: str, payload: Work):
    return _call(lambda connection: copies.record_work(
        connection, project_id=project_id, copy_id=copy_id, note=payload.note,
        copy_head=payload.copy_head, actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/finish")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def finish_copy(project_id: str, copy_id: str, payload: Actor):
    return _call(lambda connection: copies.finish_copy(
        connection, project_id=project_id, copy_id=copy_id, actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/fail")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def fail_copy(project_id: str, copy_id: str, payload: Reason):
    return _call(lambda connection: copies.fail_copy(
        connection, project_id=project_id, copy_id=copy_id, reason=payload.reason,
        actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/retry")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def retry_copy(project_id: str, copy_id: str, payload: Actor):
    return _call(lambda connection: copies.retry_copy(
        connection, project_id=project_id, copy_id=copy_id, actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/rebase")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def rebase_copy(project_id: str, copy_id: str, payload: Rebase):
    return _call(lambda connection: copies.rebase_copy(
        connection, project_id=project_id, copy_id=copy_id, copy_head=payload.copy_head,
        actor=payload.actor))


@router.post("/v1/projects/{project_id}/task-copies/{copy_id}/apply")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def apply_copy(project_id: str, copy_id: str, payload: Actor):
    return _call(lambda connection: copies.apply_copy(
        connection, project_id=project_id, copy_id=copy_id, actor=payload.actor))
