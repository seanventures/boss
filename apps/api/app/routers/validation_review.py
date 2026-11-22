"""VALIDATION AND REVIEW over HTTP (SPEC docs/design/validation-review §3): one change's
validator record, the people's overrides of its findings, and the automatic re-run.

GET  /v1/changes/{project_id}/{number}/validation                       boss.validation/v1 +
     overrides + re-run state + the current boss.review_packet/v2
POST /v1/changes/{project_id}/{number}/findings/{finding_id}/override   accept|reject|edit|remove|waive|reopen
     (`remove` deletes the finding's whole block; an `edit` with empty text is a `remove`)
POST /v1/changes/{project_id}/{number}/validation/revert-all            the target back to the partner's bytes
POST /v1/changes/{project_id}/{number}/validation/rerun                 "Rebuild packet": re-run now
GET  /v1/projects/{project_id}/rulesets                                 the base branch's rulesets
POST /v1/projects/{project_id}/rulesets/{ruleset_id}/propose            a ruleset edit (or a new one), as a change
GET  /v1/projects/{project_id}/validators                               the base branch's .boss/validators.yaml
POST /v1/projects/{project_id}/validators/propose                       add / edit it, as a change
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
    action: Literal["accept", "reject", "edit", "remove", "waive", "reopen"]
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


class RerunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = Field(default=None, max_length=1000)


@router.post(BASE + "/validation/rerun")
@permit(P.GITHUB_MERGE, scope=Scope.project(path="project_id"))
def rebuild_packet(project_id: str, number: int, request: Request, payload: RerunBody) -> Any:
    """"Rebuild packet": the recorded validator again at the branch head; the merge waits."""
    from boss_engine.services import finding_overrides as fo

    _people_only(request, "Rebuilding the review packet")
    try:
        with begin(_engine) as c:
            _require(c, project_id)
            return fo.rebuild(c, _settings, project_id, number, actor=_actor(request),
                              reason=payload.reason)
    except fo.OverrideError as error:
        return _refused(error)


@router.get("/v1/projects/{project_id}/rulesets")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def project_rulesets(project_id: str) -> Any:
    """The rulesets on the project's base branch (`.boss/rulesets/*.yaml`), version and sha."""
    from boss_engine.services import ruleset_changes as rc

    with begin(_engine) as c:
        _require(c, project_id)
        try:
            return rc.list_for_project(c, _settings, project_id)
        except rc.RulesetChangeError as error:
            return _refused(error)


class ProposeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[dict[str, Any]] = Field(max_length=500)
    base_version: Optional[int] = None
    base_sha: Optional[str] = Field(default=None, max_length=80)
    # Creating a ruleset (none on the base branch yet): the validator it layers on.
    validator: Optional[str] = Field(default=None, max_length=100)


@router.post("/v1/projects/{project_id}/rulesets/{ruleset_id}/propose")
@permit(P.POLICY_MANAGE, scope=Scope.project(path="project_id"))
def propose_ruleset(project_id: str, ruleset_id: str, request: Request,
                    payload: ProposeBody) -> Any:
    """Open "Ruleset <id> v<n+1>" as a change. A P0 downgrade needs a second approver."""
    from boss_engine.services import ruleset_changes as rc

    _people_only(request, "Changing a ruleset")
    try:
        with begin(_engine) as c:
            _require(c, project_id)
            return rc.propose(c, _settings, project_id, ruleset_id, rules=payload.rules,
                              base_version=payload.base_version, base_sha=payload.base_sha,
                              actor=_actor(request), validator=payload.validator)
    except rc.RulesetChangeError as error:
        return _refused(error)


@router.get("/v1/projects/{project_id}/validators")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def project_validators(project_id: str) -> Any:
    """The base branch's `.boss/validators.yaml` (``exists: false`` when there is none yet)."""
    from boss_engine.services import ruleset_changes as rc

    with begin(_engine) as c:
        _require(c, project_id)
        try:
            return rc.list_validators(c, _settings, project_id)
        except rc.RulesetChangeError as error:
            return _refused(error)


class ValidatorsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validators: list[dict[str, Any]] = Field(max_length=50)
    base_sha: Optional[str] = Field(default=None, max_length=80)


@router.post("/v1/projects/{project_id}/validators/propose")
@permit(P.POLICY_MANAGE, scope=Scope.project(path="project_id"))
def propose_validators(project_id: str, request: Request, payload: ValidatorsBody) -> Any:
    """Add or edit `.boss/validators.yaml` as a change a person merges."""
    from boss_engine.services import ruleset_changes as rc

    _people_only(request, "Changing the validators")
    try:
        with begin(_engine) as c:
            _require(c, project_id)
            return rc.propose_validators(c, _settings, project_id, validators=payload.validators,
                                         base_sha=payload.base_sha, actor=_actor(request))
    except rc.RulesetChangeError as error:
        return _refused(error)


@router.post("/v1/validation-reruns/{rerun_id}/run")
@permit(P.WORKER_LEASE)
def run_rerun(rerun_id: str) -> Any:
    """Worker-invoked: run one leased `validation.rerun` here, where the sandbox and the
    toolchains are."""
    from boss_engine.services import finding_overrides as fo

    from ..identity_guard import require_worker_principal

    require_worker_principal()
    return fo.execute_rerun(_engine, _settings, rerun_id)
