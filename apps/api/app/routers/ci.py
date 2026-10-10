"""GitHub Actions CI, governed (services/ci_gate.py): the per-repo opt-in, static checks of a
workflow change, waiting for CI, and what the CI gates say about a decision.

  GET  /v1/setup/repository/{binding_id}/ci-workflows   the opt-in, and where to accept the
                                                        App's new permission at GitHub
  PUT  /v1/setup/repository/{binding_id}/ci-workflows   turn it on/off (github:connect: Owner or
                                                        Admin, step-up; recorded)
  POST /v1/ci/workflow-checks                           "CI changed?" + static findings for files
  POST /v1/ci/waits                                     start waiting for a PR's / commit's CI
  POST /v1/ci/waits/{wait_id}/poll                      one look now (the runner looks each tick)
  GET  /v1/ci/waits/{wait_id}                           the wait, in words
  GET  /v1/ci/gate                                      a decision's CI gates: CI changed (a
                                                        person must approve), CI status

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit


class CiWorkflowsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    enabled: bool
    actor: Optional[str] = None


class WorkflowFile(BaseModel):
    path: str
    content: str = ""


class WorkflowChecksBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    files: list[WorkflowFile]


class CiWaitBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    pull_request_url: Optional[str] = None
    head_sha: Optional[str] = None
    repository: Optional[str] = None
    timeout_minutes: Optional[float] = None
    actor: Optional[str] = None


class CiPollBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str


def build_ci_router(*, engine, get_settings: Callable[[], Any], require_actor: Callable[..., None],
                    principal: Callable[[], Optional[str]]) -> APIRouter:
    from boss_engine.identity import github_setup
    from boss_engine.services import ci_gate

    router = APIRouter(tags=["ci"])

    def actor_for(claimed: Optional[str]) -> str:
        return str(principal() or claimed or "ceo").strip() or "ceo"

    def fail(error: ci_gate.CiSettingError):
        raise HTTPException(status_code=error.status,
                            detail={"error": error.code, "message": error.message}) from error

    def _app_cfg(connection) -> dict[str, Any]:
        try:
            return dict(github_setup.app_config(connection))
        except Exception:  # noqa: BLE001 — no identity tables: no App to link
            return {}

    @router.get("/v1/setup/repository/{binding_id}/ci-workflows")
    @permit(P.GITHUB_READ, scope=Scope.project(query="project_id"))
    def get_ci_workflows(binding_id: str, project_id: str) -> dict[str, Any]:
        """"Builds may change CI workflows" for one repository, and — when on — the exact GitHub
        pages where the App's `workflows: write` permission is accepted."""
        with begin(engine) as connection:
            try:
                return ci_gate.setting_view(connection, project_id, binding_id,
                                            _app_cfg(connection), github_setup.web_base())
            except ci_gate.CiSettingError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.put("/v1/setup/repository/{binding_id}/ci-workflows")
    @permit(P.GITHUB_CONNECT, scope=Scope.project(body="project_id"))
    def put_ci_workflows(binding_id: str, body: CiWorkflowsBody) -> dict[str, Any]:
        """Turn the opt-in on or off. github:connect is Owner/Admin and needs a fresh step-up
        (the PDP refuses `step_up_required` otherwise); the change is recorded."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                ci_gate.set_workflows_write(connection, body.project_id, binding_id,
                                            body.enabled, actor=actor)
                return ci_gate.setting_view(connection, body.project_id, binding_id,
                                            _app_cfg(connection), github_setup.web_base())
            except ci_gate.CiSettingError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.post("/v1/ci/workflow-checks")
    @permit(P.CODE_TASK_READ, scope=Scope.project(body="project_id"))
    def post_workflow_checks(body: WorkflowChecksBody) -> dict[str, Any]:
        """Whether these files change CI, and BOSS's deterministic findings for them (the same
        checks a build's review runs). Reads nothing, writes nothing."""
        files = [f.model_dump() for f in body.files]
        evidence = ci_gate.ci_changed_evidence(files)
        findings = [ci_gate.Finding(**f) for f in evidence.get("ci_findings") or []]
        return {"ci_changed": bool(evidence), "files": evidence.get("ci_changed_files") or [],
                "findings": evidence.get("ci_findings") or [],
                "requires_person_approval": bool(evidence),
                "pull_request_comment": (ci_gate.ci_changed_comment(
                    evidence["ci_changed_files"], findings) if evidence else None)}

    @router.post("/v1/ci/waits")
    @permit(P.GITHUB_SYNC, scope=Scope.project(body="project_id"))
    def post_ci_wait(body: CiWaitBody) -> dict[str, Any]:
        """Start waiting for CI on a pull request's head (or a commit), and take the first look."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                parsed = ci_gate.parse_pr_url(body.pull_request_url or "")
                repository = body.repository or (parsed[0] if parsed else None)
                reader = ci_gate.reader_for_project(connection, get_settings(), body.project_id,
                                                    repository)
                sha = (body.head_sha or "").strip()
                if not sha and parsed:
                    sha = str(((reader.pull_request(parsed[1]) or {}).get("head") or {})
                              .get("sha") or "")
                timeout = (int(body.timeout_minutes * 60) if body.timeout_minutes
                           else int(getattr(get_settings(), "ci_wait_timeout_seconds", 0) or 0)
                           or ci_gate.DEFAULT_TIMEOUT_SECONDS)
                wait = ci_gate.start_wait(
                    connection, project_id=body.project_id,
                    repository=repository or reader.repository, head_sha=sha,
                    pull_request_url=body.pull_request_url, timeout_seconds=timeout,
                    reader=reader, actor=actor)
                return ci_gate.poll_wait(connection, wait["id"], reader, force=True)
            except ci_gate.CiSettingError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.post("/v1/ci/waits/{wait_id}/poll")
    @permit(P.GITHUB_SYNC, scope=Scope.project(body="project_id"))
    def post_ci_wait_poll(wait_id: str, body: CiPollBody) -> dict[str, Any]:
        with begin(engine) as connection:
            try:
                wait = ci_gate.get_wait(connection, wait_id)
                if wait["project_id"] != body.project_id:
                    raise ci_gate.CiSettingError("ci_wait_not_found", 404, "No such CI wait.")
                reader = ci_gate.reader_for_project(connection, get_settings(), body.project_id,
                                                    wait["repository"])
                return ci_gate.poll_wait(connection, wait_id, reader, force=True)
            except ci_gate.CiSettingError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.get("/v1/ci/waits/{wait_id}")
    @permit(P.GITHUB_READ, scope=Scope.project(query="project_id"))
    def get_ci_wait(wait_id: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            try:
                wait = ci_gate.get_wait(connection, wait_id)
            except ci_gate.CiSettingError as error:
                fail(error)
            if wait["project_id"] != project_id:
                raise HTTPException(status_code=404, detail={"error": "ci_wait_not_found"})
            return wait

    @router.get("/v1/ci/gate")
    @permit(P.GITHUB_READ, scope=Scope.project(query="project_id"))
    def get_ci_gate(project_id: str, decision_id: str) -> dict[str, Any]:
        """What the CI gates say about one decision: whether it ships a CI change (then only a
        person can approve it) and the CI wait that blocks or clears it."""
        from sqlalchemy import text

        with begin(engine) as connection:
            row = connection.execute(text(
                "SELECT * FROM decisions WHERE id = :d AND project_id = :p"),
                {"d": decision_id, "p": project_id}).mappings().first()
            if row is None:
                raise HTTPException(status_code=404, detail={"error": "decision_not_found"})
            rec = ci_gate._rec(dict(row))  # noqa: SLF001
            changed = ci_gate.ci_changed_for(connection, project_id, rec)
            wait = ci_gate.latest_wait(connection, project_id,
                                       pull_request_url=rec.get("pull_request_url"),
                                       shas=ci_gate._shas(rec))  # noqa: SLF001
            return {
                "decision_id": decision_id, "action": rec.get("action"),
                "ci_changed": changed is not None,
                "ci_changed_files": (changed or {}).get("files") or [],
                "person_approval_required": changed is not None,
                "ci_wait": wait,
                "ci_blocks": bool(wait and wait["status"] not in ci_gate.CLEARED),
            }

    return router
