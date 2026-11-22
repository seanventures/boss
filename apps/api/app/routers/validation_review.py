"""VALIDATION AND REVIEW over HTTP (SPEC docs/design/validation-review §3): one change's
validator record, the people's overrides of its findings, and the automatic re-run.

GET  /v1/changes/{project_id}/{number}/validation                       boss.validation/v1 +
     overrides + re-run state + the current boss.review_packet/v2
POST /v1/changes/{project_id}/{number}/findings/{finding_id}/override   accept|reject|edit|waive|reopen
POST /v1/changes/{project_id}/{number}/validation/revert-all            the target back to the partner's bytes
POST /v1/validation-reruns/{rerun_id}/run                               worker-invoked: one re-run
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

from .local_projects import _actor, _people_only, _require

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)

BASE = "/v1/changes/{project_id}/{number}"


def _refused(error: Any) -> JSONResponse:
    return JSONResponse(error.body(), status_code=int(error.status))


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "reject", "edit", "waive", "reopen"]
    head_sha: str = Field(min_length=4, max_length=80)
    text: Optional[str] = Field(default=None, max_length=4000)
    reason: Optional[str] = Field(default=None, max_length=1000)
    approver_id: Optional[str] = Field(default=None, max_length=200)
    expires_at: Optional[str] = Field(default=None, max_length=40)


class RevertAllBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    head_sha: str = Field(min_length=4, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)


@router.get(BASE + "/validation")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def change_validation(project_id: str, number: int) -> Any:
    from boss_engine.services import finding_overrides as fo

    with begin(_engine) as c:
        _require(c, project_id)
        try:
            return fo.state(c, _settings, project_id, number)
        except fo.OverrideError as error:
            return _refused(error)


def _override(project_id: str, number: int, request: Request, **kw: Any) -> Any:
    from boss_engine.services import finding_overrides as fo

    _people_only(request, "Overriding a validation finding")
    try:
        with begin(_engine) as c:
            _require(c, project_id)
            return fo.override(c, _settings, project_id, number, actor=_actor(request), **kw)
    except fo.OverrideError as error:
        return _refused(error)


@router.post(BASE + "/findings/{finding_id}/override")
@permit(P.GITHUB_MERGE, scope=Scope.project(path="project_id"))
def override_finding(project_id: str, number: int, finding_id: str, request: Request,
                     payload: OverrideBody) -> Any:
    """A person's override of one finding. A stale head_sha is 409 with the newer packet; a P0
    waive without a second person is 403; reject/edit commit to the change branch."""
    return _override(project_id, number, request, action=payload.action, finding_id=finding_id,
                     head_sha=payload.head_sha, text_=payload.text, reason=payload.reason,
                     approver_id=payload.approver_id, expires_at=payload.expires_at)


@router.post(BASE + "/validation/revert-all")
@permit(P.GITHUB_MERGE, scope=Scope.project(path="project_id"))
def revert_all(project_id: str, number: int, request: Request, payload: RevertAllBody) -> Any:
    """The target file back to the partner's exact bytes (one commit), then a re-run."""
    return _override(project_id, number, request, action="revert_all", finding_id=None,
                     head_sha=payload.head_sha, reason=payload.reason)


@router.post("/v1/validation-reruns/{rerun_id}/run")
@permit(P.WORKER_LEASE)
def run_rerun(rerun_id: str) -> Any:
    """Worker-invoked: run one leased `validation.rerun` here, where the sandbox and the
    toolchains are."""
    from boss_engine.services import finding_overrides as fo

    from ..identity_guard import require_worker_principal

    require_worker_principal()
    return fo.execute_rerun(_engine, _settings, rerun_id)
