"""The Project experience API (BOSSAPP-913 / 914): docs/product/PROJECT_EXPERIENCE_CONTRACT.md.

GET  /v1/projects/summary                         §1  the Projects list (cards)
GET  /v1/projects/{id}/overview                   §2  the Overview tab
GET  /v1/projects/{id}/work?state=&q=             §3  the Work tab
POST /v1/projects/{id}/work                       §3  new work, through THE start door
GET  /v1/projects/{id}/knowledge                  §4  Repo & knowledge
GET  /v1/projects/{id}/deployments                §5  Deployments
GET  /v1/projects/{id}/captain                    §7  the captain and squad
POST /v1/projects/{id}/captain                    §7  change the captain (owner/admin)
GET  /v1/projects/{id}/onboarding                 §8  the onboarding agent's record
POST /v1/projects/{id}/onboarding/start           §8  start (or show) onboarding
POST /v1/projects/onboarding/execute                  the worker's leased onboarding job
GET  /v1/jump?q=&project_id=                      §9  ⌘K
GET  /v1/projects/{id}/settings                   §11 Project › Settings (sections + actions)
GET  /v1/projects/{id}/demo-logins                §11 demo users per environment (names only)
GET  /v1/projects/{id}/demo-logins/reveal?env=    §11 the passwords: owner/admin + step-up
GET  /v1/projects/{id}/health                     §12 the Health tab

Reads are one round trip per tab and scoped like every project read (project:read). Every write
goes through a path that already exists (the start door, the jobs queue) except the captain
choice, which is new state.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, authenticated, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)


def _missing(error: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error) or "Not found.")


def _require_project(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise _missing(error) from None


def _actor(request: Request) -> str:
    from apps.api.app import identity_guard
    from apps.api.app.main import current_api_principal

    ctx = identity_guard.request_auth(request)
    if ctx is not None:
        return identity_guard.actor_for(ctx)
    return current_api_principal.get() or "operator"


def _legacy_bar(connection, project_id: str, permission: str) -> None:
    """Pre-identity installs (shadow mode): the legacy membership bar, as the archive and start
    routes keep it. Under enforce, @permit has decided already."""
    from apps.api.app import identity_guard
    from apps.api.app.main import (current_api_principal, current_transport_authenticated,
                                   require_project_actor)

    principal = current_api_principal.get()
    if not identity_guard.identity_enforced() and principal and current_transport_authenticated.get():
        require_project_actor(connection, project_id, principal, permission)


# ── §1 the Projects list ────────────────────────────────────────────────────────────────────

@router.get("/v1/projects/summary")
@authenticated(note="Lists only the projects visible_project_rows says the caller may read "
                    "(project:read per project), the same answer as GET /v1/projects.")
def projects_summary(request: Request, include_archived: bool = False) -> dict[str, Any]:
    from apps.api.app.main import visible_project_rows
    from boss_engine.services.project_experience import overview

    rows = visible_project_rows(request, include_archived=include_archived)
    with begin(_engine) as connection:
        items = overview.summary(connection, [str(r["id"]) for r in rows])
    first_run = not any(i.get("repo") for i in items)
    return {"items": items, "first_run": first_run,
            "first_run_href": "?area=projects&first_run=1" if first_run else None}


# ── §2 Overview, §3 Work, §4 Knowledge, §5 Deployments, §12 Health ──────────────────────────

@router.get("/v1/projects/{project_id}/overview")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_overview(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import overview

    with begin(_engine) as connection:
        try:
            return overview.view(connection, project_id)
        except LookupError as error:
            raise _missing(error) from None


@router.get("/v1/projects/{project_id}/work")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_work(project_id: str, state: Optional[str] = None, q: Optional[str] = None
                 ) -> dict[str, Any]:
    from boss_engine.services.project_experience import work

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return work.view(connection, project_id, state=state or None, q=q or None)


class NewWorkBody(BaseModel):
    kind: str = Field("feature", description="feature | bug | ticket")
    text: str = Field(..., description="What to do, in the person's words.")
    title: Optional[str] = None
    #: Where the suggestion came from (an onboarding candidate's ref, a roadmap item).
    source_ref: Optional[str] = None
    #: The console's readiness sheet: start although these are missing (the start door's own).
    start_anyway: Optional[list[str]] = None
    accept: Optional[list[str]] = None
    dedupe_key: Optional[str] = None


def _title_of(text_in: str, fallback: str) -> str:
    """The console's startBody rule: the first line, trailing punctuation off, ≤ 80 chars."""
    first = (text_in or "").strip().splitlines()[0] if (text_in or "").strip() else ""
    first = first.rstrip(".!?").strip()[:80]
    return first or fallback


@router.post("/v1/projects/{project_id}/work")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def project_new_work(project_id: str, payload: NewWorkBody) -> dict[str, Any]:
    """New work through THE start door (start_delivery_workflow), exactly as the project page's
    "What should we add?" box starts it: the project's mapped small-feature workflow (or Minor
    Feature) on its repository, or its new-app default when it has none. Consent, readiness and
    step-up refusals pass through unchanged so the console's existing sheets handle them."""
    from apps.api.app import identity_guard
    from apps.api.app.main import start_delivery_workflow
    from apps.api.app.request_bodies import WorkflowStartBody
    from boss_engine.services import start_project
    from boss_engine.services.project_experience import work
    from boss_engine.services.project_experience.common import project_href

    kind = (payload.kind or "feature").strip().lower()
    if kind not in ("feature", "bug", "ticket"):
        raise HTTPException(status_code=422, detail={
            "error": "bad_kind", "message": "kind is feature, bug or ticket."})
    said = (payload.text or "").strip()
    if not said:
        raise HTTPException(status_code=422, detail={
            "error": "text_required", "message": "Say what to do."})
    with begin(_engine) as connection:
        page = None
        try:
            page = start_project.view(connection, project_id)
        except LookupError as error:
            raise _missing(error) from None
    start = page["start"]
    run_input: dict[str, Any] = {"directive": said[:8000],
                                 "title": (payload.title or "").strip()[:80]
                                 or _title_of(said, page["name"]),
                                 "work_kind": kind}
    if page.get("repo"):
        run_input["repository"] = page["repo"]["full_name"]
    if payload.source_ref:
        run_input["source_ref"] = str(payload.source_ref)[:200]
    body: dict[str, Any] = {"project_id": project_id, "input": run_input,
                            "actor": identity_guard.request_actor() or None}
    if start.get("workflow_key"):
        body["workflow_key"] = start["workflow_key"]
    else:
        body["work_class"] = start.get("work_class") or start_project.NEW_APP_CLASS
    if payload.start_anyway:
        body["start_anyway"] = list(payload.start_anyway)
    if payload.accept:
        body["accept"] = list(payload.accept)
    if payload.dedupe_key:
        body["dedupe_key"] = payload.dedupe_key
    started = start_delivery_workflow(WorkflowStartBody(**body))
    run_id = str(((started or {}).get("run") or {}).get("id") or "")
    with begin(_engine) as connection:
        found = next((w for w in work.everything(connection, project_id) if w["id"] == run_id), None)
    out = {"work": found, "run_id": run_id or None,
           "href": project_href(project_id, tab="work", drawer=f"run:{run_id}") if run_id else
           project_href(project_id, tab="work"),
           "duplicate": bool(((started or {}).get("run") or {}).get("duplicate"))}
    for key in ("readiness", "precheck", "ignored_input"):
        if (started or {}).get(key):
            out[key] = started[key]
    return out


@router.get("/v1/projects/{project_id}/knowledge")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_knowledge(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import knowledge

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return knowledge.view(connection, project_id)


@router.get("/v1/projects/{project_id}/deployments")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_deployments(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import deployments

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return deployments.view(connection, project_id)


@router.get("/v1/projects/{project_id}/health")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_health(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import health

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return health.view(connection, project_id)


# ── §7 the captain ──────────────────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/captain")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_captain(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import captain

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return captain.view(connection, project_id)


class CaptainBody(BaseModel):
    agent_id: str = Field(..., description="An agent id or role key of this project.")


@router.post("/v1/projects/{project_id}/captain")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def project_captain_change(project_id: str, payload: CaptainBody, request: Request) -> Any:
    from boss_engine.services.project_experience import captain

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "member_manage")
        try:
            return captain.assign(connection, project_id, payload.agent_id, actor=_actor(request))
        except captain.CaptainRefused as refused:
            return JSONResponse({"error": "not_an_agent", "message": str(refused)},
                                status_code=422)


# ── §8 the onboarding agent ─────────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/onboarding")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_onboarding(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import onboarding

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return onboarding.view(connection, project_id)


@router.post("/v1/projects/{project_id}/onboarding/start")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def project_onboarding_start(project_id: str, request: Request) -> dict[str, Any]:
    """Start onboarding on demand (also started on project creation and when a repository is
    added). A run still moving is returned as it is."""
    from boss_engine.services.project_experience import onboarding

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "workflow_execute")
        # An archived project answers 409 through the app's ProjectArchived handler.
        return onboarding.start(connection, project_id, trigger="manual", actor=_actor(request))


class LeasedJobBody(BaseModel):
    job_id: str
    worker_id: str
    lease_token: str


@router.post("/v1/projects/onboarding/execute")
@permit(P.WORKER_LEASE)
def project_onboarding_execute(body: LeasedJobBody) -> dict[str, Any]:
    """The worker hands its leased `project.onboarding` job here: GitHub's credentials, the
    code graph and the dossier are in this container. Fenced by the lease; no model call."""
    from apps.api.app.main import require_worker_principal
    from boss_engine.services.project_experience import onboarding

    require_worker_principal()
    return onboarding.execute_job(_engine, _settings, job_id=body.job_id,
                                  worker_id=body.worker_id, lease_token=body.lease_token)


# ── §9 jump ─────────────────────────────────────────────────────────────────────────────────

@router.get("/v1/jump")
@authenticated(note="Searches only the projects visible_project_rows says the caller may read; a "
                    "project_id outside them answers 404.")
def jump(request: Request, q: str = "", project_id: Optional[str] = None) -> dict[str, Any]:
    from apps.api.app.main import visible_project_rows
    from boss_engine.services.project_experience import jump as jump_service

    visible = [str(r["id"]) for r in visible_project_rows(request)]
    if project_id and project_id not in visible:
        raise HTTPException(status_code=404, detail=f"There is no project {project_id}.")
    with begin(_engine) as connection:
        return {"q": q, "project_id": project_id,
                "items": jump_service.search(connection, q=q, project_ids=visible,
                                             project_id=project_id)}


# ── §11 settings and demo logins ────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/settings")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_settings_view(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import settings_view

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return settings_view.view(connection, project_id)


@router.get("/v1/projects/{project_id}/demo-logins")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_demo_logins(project_id: str) -> dict[str, Any]:
    """Who to sign in as, per environment — names only; the password is behind the reveal."""
    from boss_engine.services.project_experience import demo_logins

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return demo_logins.listing(connection, project_id)


@router.get("/v1/projects/{project_id}/demo-logins/reveal")
@permit(P.SECRET_MANAGE, scope=Scope.project(path="project_id"))
def project_demo_logins_reveal(project_id: str, request: Request,
                               env: str = Query(..., description="preview, staging, …")) -> Any:
    """The demo users' passwords for one environment: owner/admin with a fresh step-up (the
    permission's own rule), only values BOSS generated, never cached, and audited."""
    from apps.api.app.routers.identity_access import request_identity
    from boss_engine.services.project_experience import demo_logins

    ctx = request_identity(request)
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "member_manage")
        org_id = ctx.principal.org_id if ctx is not None else connection.execute(
            _org_sql(), {"p": project_id}).scalar()
        try:
            out = demo_logins.reveal(connection, _settings, org_id=str(org_id),
                                     project_id=project_id, env=env)
        except LookupError as error:
            return JSONResponse({"error": "no_demo_login", "message": str(error)},
                                status_code=404, headers={"Cache-Control": "no-store"})
        demo_logins.record_reveal(connection, project_id, _actor(request), out["env"],
                                  len(out["logins"]))
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


def _org_sql():
    from sqlalchemy import text

    return text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :p")
