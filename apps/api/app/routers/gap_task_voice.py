"""File a submitted voice transcript on a task. No phone line, no speech vendor.

The reconciler mounts `router`. A repeat of the same transcript id is the
stored row, not a second receipt.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit
from boss_engine.services.task_voice import TaskVoiceError, TaskVoiceService

router = APIRouter(tags=["task-voice"])

_NOT_FOUND = frozenset({"task_not_found", "project_not_found"})


class AttachVoiceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    transcript_id: str = Field(min_length=1, max_length=200)
    source: Literal["browser", "phone"]
    body: str = Field(min_length=1, max_length=20000)
    submitted_by: str = Field(min_length=1, max_length=200)


def _http(exc: TaskVoiceError) -> HTTPException:
    status = 404 if exc.code in _NOT_FOUND else 422
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.post("/v1/tasks/{task_id}/voice")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(body="project_id"))
def attach_voice(task_id: str, payload: AttachVoiceBody) -> dict:
    with begin(get_engine()) as connection:
        try:
            return TaskVoiceService().attach(
                connection,
                org_id=payload.org_id,
                project_id=payload.project_id,
                task_id=task_id,
                transcript_id=payload.transcript_id,
                source=payload.source,
                body=payload.body,
                submitted_by=payload.submitted_by,
            )
        except TaskVoiceError as exc:
            raise _http(exc) from exc


@router.get("/v1/tasks/{task_id}/voice")
@permit(P.WORK_ITEM_READ, scope=Scope.project(query="project_id"))
def list_voice(task_id: str, org_id: str, project_id: str) -> dict:
    with begin(get_engine()) as connection:
        try:
            transcripts = TaskVoiceService().list_for_task(
                connection,
                org_id=org_id,
                project_id=project_id,
                task_id=task_id,
            )
        except TaskVoiceError as exc:
            raise _http(exc) from exc
    return {"transcripts": transcripts}


__all__ = ["router", "AttachVoiceBody"]
