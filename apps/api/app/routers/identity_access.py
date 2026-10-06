"""The access-administration API: access roles, groups, role bindings, effective access, the
catalogue and approval seats. Stream S2. IDENTITY.md §6, §10 (Administration).

Every route declares its permission (the enforcement point checks it before the handler runs) and
every write re-checks at the exact scope it lands on, applies the grant ceiling, audits and bumps
the identity epoch, in one transaction (boss_engine.identity.{roles,groups,bindings}).

The org always comes from the authenticated principal, never from the request. PATCH and POST
bodies are explicit allowlists (`extra="forbid"`): a body naming `org_id`, `role`, `status`,
`kind` or any other field it may not set is refused with 422 and nothing changes (A36).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.identity import P, Scope, authenticated, authz, permit
from boss_engine.identity import bindings as bindings_service
from boss_engine.identity import groups as groups_service
from boss_engine.identity import roles as roles_service
from boss_engine.identity.permissions_gen import (
    ACCESS_ROLES,
    CATALOGUE_FINGERPRINT,
    CATALOGUE_VERSION,
    PERMISSION_META,
)
from boss_engine.identity.principal import AuthContext
from boss_engine.identity.roles import AccessError
from boss_engine.identity.scope import ResolvedScope


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleCreate(_Strict):
    key: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    permissions: list[str] = Field(default_factory=list, max_length=200)
    bindable_scopes: list[Literal["org", "project"]] = Field(default_factory=lambda: ["org", "project"])
    cloned_from: Optional[str] = None
    confirm_critical: list[str] = Field(default_factory=list)


class RolePatch(_Strict):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    permissions: Optional[list[str]] = Field(default=None, max_length=200)
    bindable_scopes: Optional[list[Literal["org", "project"]]] = None
    confirm_critical: list[str] = Field(default_factory=list)


class RoleRevert(_Strict):
    version: int = Field(ge=1)
    confirm_critical: list[str] = Field(default_factory=list)


class RolePreview(_Strict):
    permissions: list[str] = Field(max_length=200)


class GroupCreate(_Strict):
    key: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class GroupPatch(_Strict):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)


class GroupMember(_Strict):
    principal_id: str = Field(min_length=3, max_length=80)


class BindingCreate(_Strict):
    subject_type: Literal["principal", "group"]
    subject_id: str = Field(min_length=3, max_length=80)
    role_id: str = Field(min_length=3, max_length=80)
    expires_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=500)


def request_context(request: Request) -> AuthContext:
    """The request's AuthContext, from the same credential the enforcement point verified (never
    from a body, query or client header). The guard has already refused an unauthenticated call
    to any non-public route before a handler runs."""
    from .. import identity_guard

    ctx = identity_guard._authenticate(identity_guard._credentials(request))  # noqa: SLF001
    if ctx is None:
        raise AccessError(401, "unauthenticated")
    return ctx


def _refused(error: AccessError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=error.body())


def _org(ctx: AuthContext) -> ResolvedScope:
    return ResolvedScope.org_level(ctx.principal.org_id)


# ---- composite console views (IDENTITY.md §6.3, IDENTITY_PERMISSIONS.md §9.1) ----------------------
#: Each composite view is declared @permit(project:read) at the project it names (so no principal
#: reads another project's view), and every top-level section that needs more than project:read is
#: withheld from a caller who does not hold that section's own read permission there. A withheld
#: section is absent from the body and named in `withheld_sections`.
COMPOSITE_SECTIONS: dict[str, dict[str, str]] = {
    "/v1/console/command-center": {
        "running": "workflow_run:read",
        "needs_you": "decision:read",
        "recent": "workflow_run:read",
        "counts": "workflow_run:read",
        "project_id": "project:read",
        "observed_at": "project:read",
    },
    "/v1/command-center/view": {
        "counts": "workflow_run:read",
        "status": "system:read",
        "attention": "decision:read",
        "missions": "workflow_run:read",
        "outcomes": "workflow_run:read",
        "recent": "workflow_run:read",
        "teams": "agent:read",
        "agents": "agent:read",
        "pulse": "agent:read",
        "today": "activity:read",
        "week": "activity:read",
        "cleanup": "workflow_run:read",
        "budget": "budget:read",
        "budget_refusal": "budget:read",
        "host": "host:read",
        "systems": "system:read",
        "project_id": "project:read",
        "etag": "project:read",
        "generated_at": "project:read",
        "tz": "project:read",
    },
    "/v1/console/v2/overview": {
        "company": "project:read",
        "burn": "budget:read",
        "gates": "decision:read",
        "projects": "code_task:read",
        "total_projects": "code_task:read",
        "truncated": "code_task:read",
        "autonomy": "autonomy:read",
    },
    "/v1/console/v2/workspace": {
        "found": "code_task:read",
        "app_id": "code_task:read",
        "project_id": "project:read",
        "title": "code_task:read",
        "description": "code_task:read",
        "repo": "code_task:read",
        "stage": "code_task:read",
        "status": "code_task:read",
        "pct": "code_task:read",
        "spend_usd": "budget:read",
        "deployment": "deployment:read",
        "criteria": "code_task:read",
        "verification": "code_task:read",
        "runs": "code_task:read",
        "deployable": "code_task:read",
        "deployable_reason": "code_task:read",
        "latest_run_id": "code_task:read",
        "phases": "code_task:read",
    },
    "/v1/console/v2/infra": {
        "project_id": "project:read",
        "hosts": "host:read",
        "targets": "deployment:read",
        "models": "model_provider:read",
        "integrations": "integration:read",
        "month_start": "budget:read",
        "meeting_panels": "meeting:read",
        "load_recorded": "host:read",
        "load_note": "host:read",
    },
    "/v1/console/v2/settings": {
        "project_id": "project:read",
        "roles": "agent:read",
        "repos": "repository:read",
        "unavailable": "settings:read",
    },
    "/v1/agent-studio/catalog": {
        "agents": "agent:read",
        "panels": "agent:read",
        "workflows": "workflow:read",
        "agent_runs": "agent:read",
        "panel_runs": "agent:read",
        "workflow_runs": "workflow_run:read",
        "tests": "agent:read",
        "a2a": "integration:read",
    },
    "/v1/agents/live": {
        "items": "agent:read",
        "attention": "decision:read",
    },
    "/v1/agents/live-board": {
        "counts": "agent:read",
        "attention": "decision:read",
        "missions": "workflow_run:read",
        "builds": "code_task:read",
        "outcomes": "activity:read",
        "teams": "agent:read",
        "agents": "agent:read",
    },
    "/v1/factory/floor": {
        "counts": "code_task:read",
        "attention": "decision:read",
        "builds": "code_task:read",
        "outcomes": "code_task:read",
        "stopped": "code_task:read",
        "pre_build": "meeting:read",
    },
}


def withhold_sections(request: Request, project_id: str, body: Any, view: str) -> Any:
    """Drop every section of a composite view the caller cannot read on its own."""
    sections = COMPOSITE_SECTIONS[view]
    if not isinstance(body, dict):
        return body
    try:
        ctx = request_context(request)
    except Exception:  # noqa: BLE001
        # No usable identity: only possible in shadow/off mode (under enforce the guard has
        # already authenticated this request), where the old guards still decide.
        return body
    held = authz.effective_permissions(
        ctx, ResolvedScope.in_project(ctx.principal.org_id, project_id)).permissions
    withheld = sorted(k for k in body if k in sections and sections[k] not in held)
    if not withheld:
        return body
    return {**{k: v for k, v in body.items() if k not in withheld}, "withheld_sections": withheld}


def readable_projects(request: Request, project_ids: list[str]) -> Optional[set[str]]:
    """The subset of `project_ids` (in the caller's org) where the caller holds project:read, for
    list endpoints that must never show another project (§6.7 Guests). None when the request has
    no identity yet (shadow mode): the caller keeps its existing filter."""
    try:
        ctx = request_context(request)
    except Exception:  # noqa: BLE001
        return None
    org = ctx.principal.org_id
    return {pid for pid in dict.fromkeys(project_ids)
            if P.PROJECT_READ in authz.effective_permissions(
                ctx, ResolvedScope.in_project(org, pid)).permissions}


def build_identity_access_router(engine: Any) -> APIRouter:
    router = APIRouter(tags=["identity"])

    def run(request: Request, work, *, write: bool = False):
        try:
            ctx = request_context(request)
            if write:
                with engine.begin() as db:
                    return work(db, ctx)
            with engine.connect() as db:
                return work(db, ctx)
        except AccessError as error:
            return _refused(error)

    # ---- the catalogue -------------------------------------------------------------------------
    @router.get("/v1/identity/catalogue")
    @authenticated(note="the public permission catalogue: names and flags, no holders")
    def get_catalogue():
        return {
            "version": CATALOGUE_VERSION, "fingerprint": CATALOGUE_FINGERPRINT,
            "permissions": [
                {"key": m.key, "title": m.title, "description": m.description, "category": m.category,
                 "scope": m.scope, "step_up": m.step_up, "delegable": m.delegable,
                 "critical": m.critical, "system_only": m.system_only, "roles": list(m.roles)}
                for m in PERMISSION_META.values()],
            "access_roles": [{"key": r.key, "name": r.name, "bindable_scopes": list(r.bindable_scopes),
                              "system": r.system} for r in ACCESS_ROLES.values()],
        }

    # ---- access roles --------------------------------------------------------------------------
    @router.get("/v1/identity/access-roles")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org())
    def list_access_roles(request: Request):
        return run(request, lambda db, ctx: {"roles": roles_service.list_roles(db, ctx.principal.org_id)})

    @router.get("/v1/identity/access-roles/{role_id}")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org())
    def get_access_role(role_id: str, request: Request):
        return run(request, lambda db, ctx: roles_service.get_role(db, ctx.principal.org_id, role_id))

    @router.get("/v1/identity/access-roles/{role_id}/versions")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org())
    def list_access_role_versions(role_id: str, request: Request):
        return run(request, lambda db, ctx: {
            "versions": roles_service.versions(db, ctx.principal.org_id, role_id)})

    @router.post("/v1/identity/access-roles/{role_id}/preview")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org(), readonly_post=True)
    def preview_access_role(role_id: str, body: RolePreview, request: Request):
        return run(request, lambda db, ctx: roles_service.preview(
            db, ctx.principal.org_id, role_id, body.permissions))

    @router.post("/v1/identity/access-roles", status_code=201)
    @permit(P.ACCESS_ROLE_MANAGE, scope=Scope.org())
    def create_access_role(body: RoleCreate, request: Request):
        def work(db, ctx):
            if body.cloned_from:
                return roles_service.clone_role(db, ctx, body.cloned_from, key=body.key, name=body.name,
                                                description=body.description or None,
                                                confirm_critical=body.confirm_critical)
            return roles_service.create_role(db, ctx, key=body.key, name=body.name,
                                             description=body.description, permissions=body.permissions,
                                             bindable_scopes=body.bindable_scopes,
                                             confirm_critical=body.confirm_critical)
        return run(request, work, write=True)

    @router.patch("/v1/identity/access-roles/{role_id}")
    @permit(P.ACCESS_ROLE_MANAGE, scope=Scope.org())
    def update_access_role(role_id: str, body: RolePatch, request: Request):
        return run(request, lambda db, ctx: roles_service.update_role(
            db, ctx, role_id, name=body.name, description=body.description,
            permissions=body.permissions, bindable_scopes=body.bindable_scopes,
            confirm_critical=body.confirm_critical), write=True)

    @router.post("/v1/identity/access-roles/{role_id}/revert")
    @permit(P.ACCESS_ROLE_MANAGE, scope=Scope.org())
    def revert_access_role(role_id: str, body: RoleRevert, request: Request):
        return run(request, lambda db, ctx: roles_service.revert_role(
            db, ctx, role_id, body.version, confirm_critical=body.confirm_critical), write=True)

    @router.delete("/v1/identity/access-roles/{role_id}", status_code=204)
    @permit(P.ACCESS_ROLE_MANAGE, scope=Scope.org())
    def delete_access_role(role_id: str, request: Request):
        def work(db, ctx):
            roles_service.delete_role(db, ctx, role_id)
            return JSONResponse(status_code=204, content=None)
        return run(request, work, write=True)

    # ---- groups --------------------------------------------------------------------------------
    @router.get("/v1/identity/groups")
    @permit(P.GROUP_READ, scope=Scope.org())
    def list_groups(request: Request):
        return run(request, lambda db, ctx: {"groups": groups_service.list_groups(db, ctx.principal.org_id)})

    @router.get("/v1/identity/groups/{group_id}")
    @permit(P.GROUP_READ, scope=Scope.org())
    def get_group(group_id: str, request: Request):
        return run(request, lambda db, ctx: groups_service.get_group(db, ctx.principal.org_id, group_id))

    @router.post("/v1/identity/groups", status_code=201)
    @permit(P.GROUP_MANAGE, scope=Scope.org())
    def create_group(body: GroupCreate, request: Request):
        return run(request, lambda db, ctx: groups_service.create_group(
            db, ctx, key=body.key, name=body.name, description=body.description), write=True)

    @router.patch("/v1/identity/groups/{group_id}")
    @permit(P.GROUP_MANAGE, scope=Scope.org())
    def update_group(group_id: str, body: GroupPatch, request: Request):
        return run(request, lambda db, ctx: groups_service.update_group(
            db, ctx, group_id, name=body.name, description=body.description), write=True)

    @router.delete("/v1/identity/groups/{group_id}", status_code=204)
    @permit(P.GROUP_MANAGE, scope=Scope.org())
    def delete_group(group_id: str, request: Request):
        def work(db, ctx):
            groups_service.delete_group(db, ctx, group_id)
            return JSONResponse(status_code=204, content=None)
        return run(request, work, write=True)

    @router.post("/v1/identity/groups/{group_id}/members")
    @permit(P.GROUP_MANAGE_MEMBERS, scope=Scope.org())
    def add_group_member(group_id: str, body: GroupMember, request: Request):
        return run(request, lambda db, ctx: groups_service.add_member(
            db, ctx, group_id, body.principal_id), write=True)

    @router.delete("/v1/identity/groups/{group_id}/members/{principal_id}")
    @permit(P.GROUP_MANAGE_MEMBERS, scope=Scope.org())
    def remove_group_member(group_id: str, principal_id: str, request: Request):
        return run(request, lambda db, ctx: groups_service.remove_member(
            db, ctx, group_id, principal_id), write=True)

    # ---- role bindings: org scope ----------------------------------------------------------------
    @router.get("/v1/identity/role-bindings")
    @permit(P.ROLE_BINDING_READ, scope=Scope.org())
    def list_role_bindings(request: Request, subject_id: Optional[str] = None,
                           role_id: Optional[str] = None):
        return run(request, lambda db, ctx: {"bindings": bindings_service.list_bindings(
            db, ctx.principal.org_id, subject_id=subject_id, role_id=role_id)})

    @router.post("/v1/identity/role-bindings", status_code=201)
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.org())
    def add_org_role_binding(body: BindingCreate, request: Request):
        return run(request, lambda db, ctx: bindings_service.add_binding(
            db, ctx, subject_type=body.subject_type, subject_id=body.subject_id, role_id=body.role_id,
            scope_type="org", expires_at=body.expires_at, reason=body.reason), write=True)

    @router.delete("/v1/identity/role-bindings/{binding_id}", status_code=204)
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.org())
    def remove_role_binding(binding_id: str, request: Request):
        def work(db, ctx):
            bindings_service.remove_binding(db, ctx, binding_id)
            return JSONResponse(status_code=204, content=None)
        return run(request, work, write=True)

    # ---- role bindings: one project (a project Admin manages their own project's access) ---------
    @router.get("/v1/identity/projects/{project_id}/role-bindings")
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.project(path="project_id"))
    def list_project_role_bindings(project_id: str, request: Request):
        return run(request, lambda db, ctx: {"bindings": bindings_service.list_bindings(
            db, ctx.principal.org_id, project_id=project_id)})

    @router.post("/v1/identity/projects/{project_id}/role-bindings", status_code=201)
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.project(path="project_id"))
    def add_project_role_binding(project_id: str, body: BindingCreate, request: Request):
        return run(request, lambda db, ctx: bindings_service.add_binding(
            db, ctx, subject_type=body.subject_type, subject_id=body.subject_id, role_id=body.role_id,
            scope_type="project", scope_id=project_id, expires_at=body.expires_at,
            reason=body.reason), write=True)

    @router.delete("/v1/identity/projects/{project_id}/role-bindings/{binding_id}", status_code=204)
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.project(path="project_id"))
    def remove_project_role_binding(project_id: str, binding_id: str, request: Request):
        def work(db, ctx):
            bindings_service.remove_binding(db, ctx, binding_id, expect_project=project_id)
            return JSONResponse(status_code=204, content=None)
        return run(request, work, write=True)

    # ---- effective access (explain) ------------------------------------------------------------
    @router.get("/v1/identity/principals/{principal_id}/effective")
    @permit(P.USER_READ, scope=Scope.org())
    def principal_effective_access(principal_id: str, request: Request,
                                   project_id: Optional[str] = None):
        def work(db, ctx):
            org = ctx.principal.org_id
            row = db.execute(text("SELECT id, kind, org_id, status, display_name FROM identity_principals "
                                  "WHERE id = :p AND org_id = :o"), {"p": principal_id, "o": org}
                             ).mappings().first()
            if row is None:
                raise AccessError(404, "not_found")
            if project_id:
                found = db.execute(text("SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                        "WHERE p.id = :p AND w.org_id = :o"), {"p": project_id, "o": org}).first()
                if found is None:
                    raise AccessError(404, "not_found")
            from boss_engine.identity.principal import Principal, PrincipalKind, PrincipalStatus

            subject = Principal(row["id"], PrincipalKind(row["kind"]), row["org_id"],
                                PrincipalStatus(row["status"]), row["display_name"])
            scope = (ResolvedScope.in_project(org, project_id) if project_id else _org(ctx))
            eff = authz.effective_permissions(subject, scope, db=db, with_why=True)
            return {"principal_id": row["id"], "scope": scope.label(), "epoch": eff.epoch,
                    "permissions": [{"key": k, "via": [str(w) for w in eff.why.get(k, ())]}
                                    for k in sorted(eff.permissions)]}
        return run(request, work)

    # ---- approval seats (read) -------------------------------------------------------------------
    @router.get("/v1/identity/seats")
    @permit(P.ROLE_BINDING_READ, scope=Scope.org())
    def list_approval_seats(request: Request):
        def work(db, ctx):
            rows = db.execute(text("SELECT seat_key, principal_id, assigned_by, assigned_at "
                                   "FROM identity_approval_seats WHERE org_id = :o ORDER BY seat_key"),
                              {"o": ctx.principal.org_id}).mappings().all()
            return {"seats": [dict(r) for r in rows]}
        return run(request, work)

    return router
