"""/v1/templates* and an app's templates, secrets and setup guide (F2, the template system).

Spec: docs/product/APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6. Owner: stream S2. The engine is
`boss_engine.templates` (service, importer, validator, apply, secrets, gate, trial).

The paths, methods and declarations are the day-1 contract (stream S0); the handlers are S2's.
Rules the declarations cannot express are checked here:
  * approve/reject: the importer never approves their own import (D3, seat separation);
  * apply: `workflow_run:start` at the project (route) AND `template:read` (handler);
  * a live secret: `app_secret:live` at the project (handler), and a person's decision before
    any deploy uses it;
  * every template read and write is confined to the caller's org.

App-level routes (`/v1/apps/{app_key}/...`) name their project in the request (`project_id`): an
app key is unique within a project, not across projects.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit

SPEC = "APP_TEMPLATES_AND_SECURITY_LOOP.md §2.6"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportBody(_Body):
    git_url: str = Field(min_length=8, max_length=500)
    ref: Optional[str] = Field(default=None, max_length=200)
    connection_id: Optional[str] = Field(default=None, max_length=120)
    acknowledge_insecure: bool = False


class ReviewBody(_Body):
    reason: Optional[str] = Field(default=None, max_length=500)


class ApplyBody(_Body):
    project_id: str = Field(min_length=1, max_length=120)
    app_key: Optional[str] = Field(default=None, max_length=200)
    extends_run_id: Optional[str] = Field(default=None, max_length=120)
    new_app: Optional[dict[str, Any]] = None
    stack: str = Field(min_length=1, max_length=20)
    framework: Optional[str] = Field(default=None, max_length=48)
    options: Optional[dict[str, Any]] = None
    note: Optional[str] = Field(default=None, max_length=4000)
    workflow_key: Optional[str] = Field(default=None, max_length=120)
    accept_egress: Optional[Any] = None
    dedupe_key: Optional[str] = Field(default=None, max_length=200)
    #: readiness keys the person was shown missing and chose to start without (the composer's
    #: "Start anyway"; passed through to the start path unchanged)
    start_anyway: Optional[list[str]] = Field(default=None, max_length=20)


class SecretBody(_Body):
    project_id: str = Field(min_length=1, max_length=120)
    env: str = Field(min_length=1, max_length=20)
    mode: str = Field(default="test", max_length=20)
    #: write-only: never echoed, logged or returned
    value: Optional[str] = Field(default=None, max_length=8192, repr=False)
    #: the template declaring this secret, when it is set before the template is applied
    template_id: Optional[str] = Field(default=None, max_length=120)


def _ctx(request: Request):
    from .routers.identity_access import request_identity

    ctx = request_identity(request)
    if ctx is None:
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
    return ctx


def _refused(exc) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status)


def _holds(request: Request, permission: str, project_id: Optional[str]) -> bool:
    from .routers.identity_access import require_permission

    try:
        require_permission(request, permission, project_id, label="templates")
    except HTTPException:
        return False
    return True


def _project_in_org(db, project_id: str, org_id: str) -> None:
    from sqlalchemy import text

    found = db.execute(text(
        "SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p AND w.org_id = :o"), {"p": project_id, "o": org_id}).first()
    if not found:
        raise HTTPException(status_code=404, detail={"error": "project_not_found"})


def _allow_local_import() -> bool:
    """file:// imports (a local mirror; the tests). Off unless the install turns it on."""
    return (os.environ.get("BOSS_TEMPLATE_IMPORT_ALLOW_LOCAL") or "").strip().lower() in ("1", "true", "yes")


def _github_access(owner: str, repo: str):
    """How the import reads a github.com repository: boss-egress's GitHub path, read-only, under
    BOSS's audited system repo reader (the org's GitHub App or token; a public repository it does
    not cover is read anonymously there). Never the CONNECT proxy, which refuses GitHub by design.
    Without egress (a developer's machine) it is git straight to GitHub, anonymously."""
    from boss_engine.identity import runs as identity_runs

    from . import main

    return identity_runs.repo_access(main.engine, host="github", owner=owner, repo=repo,
                                     project_id=None, token="")


def build_templates_router() -> APIRouter:
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates import secrets as secrets_service
    from boss_engine.templates import service
    from boss_engine.templates.service import TemplateError

    router = APIRouter(tags=["templates"])

    def _declared(db, project_id: str, app_key: str, template_id: Optional[str], org_id: str):
        """The manifests whose secrets this app may hold: its applied (or applying) templates,
        plus the one named in the request (setting a key before the first apply)."""
        from sqlalchemy import text

        rows = db.execute(text(
            "SELECT DISTINCT t.id, t.manifest FROM app_template_applications a JOIN app_templates t "
            "ON t.id = a.template_id WHERE a.project_id = :p AND a.app_key = :k "
            "AND a.status <> 'abandoned'"), {"p": project_id, "k": app_key}).mappings().all()
        out = [service.manifest_of(dict(r)) for r in rows]
        if template_id:
            row = service.get_row(db, org_id, template_id)
            if row["status"] != "active":
                raise TemplateError(409, "not_active", "that template is not approved")
            out.append(service.manifest_of(row))
        return out

    # ---- the gallery and its management (org-wide: templates belong to the org) ----------------
    @router.get("/v1/templates")
    @permit(P.TEMPLATE_READ, scope=Scope.org())
    def list_templates(request: Request, stack: Optional[str] = None, status: Optional[str] = None,
                       label: Optional[str] = None) -> dict:
        ctx = _ctx(request)
        with begin() as db:
            items = service.list_templates(db, ctx.principal.org_id, stack=stack, status=status, label=label)
        return {"templates": items}

    @router.post("/v1/templates/import")
    @permit(P.TEMPLATE_IMPORT, scope=Scope.org())
    def import_template(request: Request, body: ImportBody) -> JSONResponse:
        ctx = _ctx(request)
        if body.connection_id:
            # A github.com URL is read through this install's GitHub connection already; naming a
            # connection adds nothing. Said plainly rather than ignored.
            return JSONResponse({"error": "connection_not_needed",
                                 "message": "leave the connection out: a https://github.com/<owner>/<repo> "
                                            "URL is read through this install's GitHub connection"},
                                status_code=422)
        may_insecure = body.acknowledge_insecure and _holds(request, P.TEMPLATE_APPROVE, None)
        try:
            result = service.import_from_git(
                begin, ctx.principal.org_id, actor_id=ctx.principal.id, git_url=body.git_url,
                ref=body.ref, may_label_insecure=may_insecure, ack_insecure=body.acknowledge_insecure,
                allow_local=_allow_local_import(), github_access=_github_access)
        except TemplateError as exc:
            return _refused(exc)
        return JSONResponse(result, status_code=201 if result["status"] == "pending_review" else 422)

    @router.get("/v1/templates/labs")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def template_labs(request: Request, project_id: str) -> dict:
        """The lab projects this project's insecure-template apps are built in (templates.lab):
        the console links to each, since a lab's runs live in that project, not this one."""
        from boss_engine.templates import lab as lab_service

        ctx = _ctx(request)
        with begin() as db:
            _project_in_org(db, project_id, ctx.principal.org_id)
            return {"project_id": project_id, "labs": lab_service.labs_of(db, project_id)}

    @router.get("/v1/templates/{template_id}")
    @permit(P.TEMPLATE_READ, scope=Scope.org())
    def describe_template(request: Request, template_id: str) -> Any:
        ctx = _ctx(request)
        try:
            with begin() as db:
                return service.describe(db, ctx.principal.org_id, template_id)
        except TemplateError as exc:
            return _refused(exc)

    # Seat-separated in the handler: the importer never approves their own import (D3).
    @router.post("/v1/templates/{template_id}/approve")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def approve_template(request: Request, template_id: str) -> Any:
        ctx = _ctx(request)
        try:
            with begin() as db:
                return service.approve(db, ctx.principal.org_id, template_id, actor_id=ctx.principal.id)
        except TemplateError as exc:
            return _refused(exc)

    @router.post("/v1/templates/{template_id}/reject")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def reject_template(request: Request, template_id: str, body: Optional[ReviewBody] = None) -> Any:
        ctx = _ctx(request)
        try:
            with begin() as db:
                return service.reject(db, ctx.principal.org_id, template_id, actor_id=ctx.principal.id,
                                      reason=(body.reason if body else "") or "")
        except TemplateError as exc:
            return _refused(exc)

    @router.post("/v1/templates/{template_id}/trial")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def trial_template(request: Request, template_id: str) -> Any:
        from boss_engine.config import get_settings
        from boss_engine.templates import trial

        ctx = _ctx(request)
        try:
            with begin() as db:
                row = service.get_row(db, ctx.principal.org_id, template_id)
                if row["status"] != "active":
                    raise TemplateError(409, "not_active", "only an approved template is trialled: "
                                                           "a trial runs its code")
                files = service.files_of(db, row["id"])
            result = trial.run(service.manifest_of(row), files, settings=get_settings(),
                               trial_id=f"trial-{row['id']}")
            with begin() as db:
                service.record_trial(db, row["id"], result)
        except TemplateError as exc:
            return _refused(exc)
        return {"id": row["id"], "trial": result}

    # Built-ins cannot be deleted, only hidden.
    @router.delete("/v1/templates/{template_id}")
    @permit(P.TEMPLATE_APPROVE, scope=Scope.org())
    def delete_template(request: Request, template_id: str, hide: bool = False) -> Any:
        ctx = _ctx(request)
        try:
            with begin() as db:
                if hide:
                    return service.hide(db, ctx.principal.org_id, template_id)
                return service.delete(db, ctx.principal.org_id, template_id)
        except TemplateError as exc:
            return _refused(exc)

    # Apply = a governed build. The route checks workflow_run:start at the project; the handler
    # also requires template:read (§2.6).
    #
    # A REFUSED APPLY WRITES NOTHING. Everything that can be refused is asked first, writing
    # nothing (plan with write=False); then the test-default secret bindings, the start (with its
    # consent and readiness checks) and the application row go in ONE transaction, so a 409 from
    # the start rolls the bindings back with it. The one thing kept across a refusal is an
    # insecure template's lab project (templates.lab): the consent a person gives names it, and
    # the retry must land in the same one.
    @router.post("/v1/templates/{template_id}/apply")
    @permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
    def apply_template(request: Request, template_id: str, body: ApplyBody) -> Any:
        from boss_engine.config import get_settings
        from boss_engine.templates import lab as lab_service

        from .routers.identity_access import require_permission

        ctx = _ctx(request)
        require_permission(request, P.TEMPLATE_READ, None, label="templates.apply")
        org_id = ctx.principal.org_id
        target_project = body.project_id
        lab: Optional[dict] = None
        try:
            with begin() as db:
                _project_in_org(db, body.project_id, org_id)
                if lab_service.lab_of(db, body.project_id):
                    raise TemplateError(409, "lab_project",
                                        "this is a lab project for an insecure template; apply "
                                        "templates to apps in your own projects")
                preflight = apply_service.plan(db, org_id=org_id, project_id=body.project_id,
                                               template_ref=template_id, body=body.model_dump(),
                                               actor_id=ctx.principal.id, write=False)
            if preflight["manifest"].insecure_by_design:
                with begin() as db:
                    lab = lab_service.ensure_lab(
                        db, get_settings(), source_project_id=body.project_id,
                        app_key=preflight["app"]["app_key"], app_name=preflight["app"]["app_name"],
                        template_key=preflight["manifest"].id, actor_id=ctx.principal.id)
                target_project = lab["project_id"]
        except TemplateError as exc:
            return _refused(exc)
        from . import main
        from .request_bodies import WorkflowStartBody

        try:
            with begin() as db:
                plan = apply_service.plan(db, org_id=org_id, project_id=target_project,
                                          template_ref=template_id, body=body.model_dump(),
                                          actor_id=ctx.principal.id)
                # THE SAME START PATH as the composer's "Add to an existing app": every gate,
                # review and the PR are the workflow's; the template only adds two gates.
                # The SIGNED-IN person starts it: their actor, as the guard bound it to this
                # request (an unnamed actor fell back to "executive_console" and was refused as
                # actor_mismatch on every real apply under enforce).
                from . import identity_guard

                started = main.start_delivery_workflow(WorkflowStartBody(
                    project_id=target_project, actor=identity_guard.request_actor() or None,
                    workflow_key=plan["workflow_key"], input=plan["run_input"],
                    dedupe_key=body.dedupe_key or f"template:{plan['application_id']}",
                    start_anyway=body.start_anyway), connection_in=db)
                run = started.get("run") or {}
                out = apply_service.record(db, plan, project_id=target_project,
                                           workflow_run_id=run.get("id"), actor_id=ctx.principal.id)
        except TemplateError as exc:
            return _refused(exc)
        return JSONResponse({**out, "template": plan["run_input"]["template"],
                             "app_key": plan["app"]["app_key"], "project_id": target_project,
                             **({"lab": {"project_id": lab["project_id"], "name": lab["name"],
                                         "why": "an insecure-by-design template is applied only in "
                                                "its own lab project, never beside real apps"}}
                                if lab else {}),
                             **({"extends": started["extends"]} if started.get("extends") else {})},
                            status_code=201)

    # ---- one app's templates, secrets and setup guide --------------------------------------------
    @router.get("/v1/apps/{app_key}/templates")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def app_templates(request: Request, app_key: str, project_id: str) -> dict:
        ctx = _ctx(request)
        with begin() as db:
            _project_in_org(db, project_id, ctx.principal.org_id)
            return {"app_key": app_key, "templates": apply_service.applications(db, project_id, app_key)}

    # Presence, mode and a shape verdict only; never a value, hash, prefix or length (§2.4).
    @router.get("/v1/apps/{app_key}/secrets")
    @permit(P.APP_SECRET_READ_METADATA, scope=Scope.project(query="project_id"))
    def app_secrets(request: Request, app_key: str, project_id: str) -> dict:
        ctx = _ctx(request)
        with begin() as db:
            _project_in_org(db, project_id, ctx.principal.org_id)
            bound = secrets_service.list_bindings(db, project_id=project_id, app_key=app_key)
            declared = _declared(db, project_id, app_key, None, ctx.principal.org_id)
        names = {b["name"] for b in bound}
        return {"app_key": app_key, "secrets": bound,
                "declared": [{"name": s.name, "purpose": s.purpose, "test_default": s.test_default,
                              "bound": s.name in names} for m in declared for s in m.secrets]}

    # Write-only. mode=live additionally needs app_secret:live, checked in the handler.
    @router.put("/v1/apps/{app_key}/secrets/{name}")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(body="project_id"))
    def put_app_secret(request: Request, app_key: str, name: str, body: SecretBody) -> Any:
        ctx = _ctx(request)
        may_live = body.mode == "live" and _holds(request, P.APP_SECRET_LIVE, body.project_id)
        if body.mode == "live" and not may_live:
            return JSONResponse({"error": "needs_app_secret_live",
                                 "message": "setting a live key needs app_secret:live"}, status_code=403)
        try:
            with begin() as db:
                _project_in_org(db, body.project_id, ctx.principal.org_id)
                spec = secrets_service.declared_secret(
                    _declared(db, body.project_id, app_key, body.template_id, ctx.principal.org_id), name)
                if spec is None:
                    raise TemplateError(404, "secret_not_declared",
                                        f"no template of this app declares {name}; an app holds only "
                                        "the secrets its templates declare")
                return secrets_service.put(
                    db, org_id=ctx.principal.org_id, project_id=body.project_id, app_key=app_key,
                    name=name, env=body.env, mode=body.mode, value=body.value, actor_id=ctx.principal.id,
                    spec=spec, may_live=may_live)
        except TemplateError as exc:
            return _refused(exc)

    @router.delete("/v1/apps/{app_key}/secrets/{name}")
    @permit(P.APP_SECRET_WRITE, scope=Scope.project(query="project_id"))
    def delete_app_secret(request: Request, app_key: str, name: str, project_id: str, env: str) -> Any:
        ctx = _ctx(request)
        try:
            with begin() as db:
                _project_in_org(db, project_id, ctx.principal.org_id)
                return secrets_service.delete(db, project_id=project_id, app_key=app_key, name=name, env=env)
        except TemplateError as exc:
            return _refused(exc)

    @router.get("/v1/apps/{app_key}/setup-guide")
    @permit(P.PROJECT_READ, scope=Scope.project(query="project_id"))
    def app_setup_guide(request: Request, app_key: str, project_id: str) -> dict:
        ctx = _ctx(request)
        from boss_engine.services.preview_service import public_preview_url

        with begin() as db:
            _project_in_org(db, project_id, ctx.principal.org_id)
            preview_id = apply_service.preview_for(db, project_id, app_key)
            # The address the person's browser and the provider's callback REACH: the preview
            # origin for the host this request came in on, never the app's port inside BOSS.
            preview_url = public_preview_url(
                preview_id, request.headers.get("host"),
                "https" if request.url.scheme == "https" else "http") if preview_id else None
            guides = apply_service.setup_guide(
                db, project_id=project_id, app_key=app_key,
                public_url=os.environ.get("BOSS_PUBLIC_URL", ""), preview_url=preview_url)
        return {"app_key": app_key, "guides": guides}

    return router
