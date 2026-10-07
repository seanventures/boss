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


class RoleReassign(_Strict):
    to_role_id: str = Field(min_length=3, max_length=80)
    delete: bool = True


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


class AccessRequestApproval(_Strict):
    role_id: str = Field(min_length=3, max_length=80)
    expires_at: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=500)


class AccessRequestDenial(_Strict):
    reason: Optional[str] = Field(default=None, max_length=500)


class SeatHolders(_Strict):
    principal_ids: list[str] = Field(default_factory=list, max_length=20)


class AccessRequest(_Strict):
    permission: str = Field(min_length=3, max_length=80)
    project_id: Optional[str] = Field(default=None, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=500)


def request_context(request: Request) -> AuthContext:
    """The request's AuthContext, from the same credential the enforcement point verified (never
    from a body, query or client header). The guard has already refused an unauthenticated call
    to any non-public route before a handler runs."""
    from .. import identity_guard

    ctx = identity_guard.request_auth(request)   # the guard's own result; never re-authenticated
    if ctx is None:
        raise AccessError(401, "unauthenticated")
    return ctx



def request_identity(request: Request) -> Optional[AuthContext]:
    """The request's AuthContext, or None when there is none yet (shadow mode before S1/S5)."""
    try:
        return request_context(request)
    except Exception:  # noqa: BLE001
        return None


def require_production(request: Optional[Request], project_id: str, environment: Any = None, *,
                       always: bool = False) -> None:
    """THE handler-side production check (the declaration-side one is `production=` on @permit,
    which the guard evaluates the same way). An operation whose target environment is production
    (object_rules.is_production_environment), or `always`, also needs deployment:production at
    the TARGET's project — pass the target's project, not the caller's."""
    from boss_engine.identity.object_rules import is_production_environment

    if not always and not is_production_environment(environment):
        return
    require_permission(request, P.DEPLOYMENT_PRODUCTION, project_id, label="production")


def require_permission(request: Optional[Request], permission: str, project_id: Optional[str], *,
                       label: str = "handler") -> None:
    """THE handler-side check of a SECOND catalogue permission an operation needs beyond its
    route's declaration (the production rule, a job kind's own permission), at `project_id` (or
    org scope when None), through the same PDP the guard uses.

    Refuses whenever THIS request is enforced (its per-route mode, as the guard decided it — not
    the global BOSS_AUTHZ_MODE alone); in shadow it records the would-deny; off does nothing.
    `request` may be None in a helper that has none: the guard's context variables carry the same."""
    from boss_engine.identity.enforcement import LEDGER, AuthzMode, Outcome

    from .. import identity_guard

    mode = (getattr(request.state, "identity_mode", None) if request is not None else None) \
        or identity_guard.IDENTITY_MODE.get() or ""
    if mode == "":
        # No guard decided this call (a helper outside a request, a background path): fail
        # CLOSED to the configured global mode, never open.
        from boss_engine.config import get_settings
        from boss_engine.identity.enforcement import mode_for

        mode = mode_for("main", global_mode=get_settings().authz_mode or "enforce").value
    if mode == AuthzMode.OFF.value:
        return
    ctx = (getattr(request.state, "identity", None) or request_identity(request)
           if request is not None else identity_guard.REQUEST_IDENTITY.get())
    from fastapi import HTTPException

    if ctx is None:
        if mode == AuthzMode.ENFORCE.value:
            raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
        return
    org = ctx.principal.org_id
    scope = ResolvedScope.in_project(org, project_id) if project_id else ResolvedScope.org_level(org)
    decision = authz.check(ctx, permission, scope)
    if decision.allowed:
        return
    path = request.scope.get("path", "") if request is not None else "handler"
    LEDGER.record(router_key=label, method="CHECK", path_template=path,
                  outcome=Outcome(False, decision.reason, permission,
                                  f"handler:{label}", ctx.principal.kind.value,
                                  decision=decision),
                  mode=AuthzMode(mode))
    if mode == AuthzMode.ENFORCE.value:
        raise HTTPException(status_code=decision.status, detail=decision.error_body())


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


def declare_framework_routes(app: Any) -> None:
    """Attach identity_guard.FRAMEWORK_DECLARATIONS to the framework routes' endpoints, so the
    enforcement point (which reads the endpoint's declaration) decides /openapi.json and /docs
    the way the coverage report says it does. Without this they are undeclared at request time
    and denied under enforce."""
    from starlette.routing import Route

    from boss_engine.identity.declarations import ACCESS_ATTR

    from .. import identity_guard

    for route in app.router.routes:
        declaration = identity_guard.FRAMEWORK_DECLARATIONS.get(getattr(route, "path", ""))
        if declaration is not None and type(route) is Route:
            setattr(route.endpoint, ACCESS_ATTR, declaration)


_OPERATIONS_BY_PERMISSION: dict[int, dict[str, int]] = {}
_TOOLS_BY_PERMISSION: dict[str, int] = {}


def operation_counts(app: Any, added: set[str], removed: set[str]) -> dict[str, int]:
    """§6.10: how many API operations are declared with a permission this change adds or removes."""
    from boss_engine.identity.declarations import Permit

    from .. import identity_guard

    table = _OPERATIONS_BY_PERMISSION.get(id(app))
    if table is None:
        table = {}
        for entry in identity_guard.route_inventory(app):
            if isinstance(entry.access, Permit):
                table[entry.access.permission] = table.get(entry.access.permission, 0) + len(entry.labels())
        _OPERATIONS_BY_PERMISSION[id(app)] = table
    return {"gained": sum(table.get(k, 0) for k in added), "lost": sum(table.get(k, 0) for k in removed)}


def tool_counts(added: set[str], removed: set[str]) -> dict[str, int]:
    """§6.10: how many MCP tools need a permission this change adds or removes."""
    if not _TOOLS_BY_PERMISSION:
        from boss_engine.tools import build_default_registry

        for spec in build_default_registry().specs():
            keys = {spec.permission} | {k for _v, k in (spec.permission_by_arg[1] if spec.permission_by_arg else ())}
            for key in keys - {None}:
                _TOOLS_BY_PERMISSION[key] = _TOOLS_BY_PERMISSION.get(key, 0) + 1
    return {"gained": sum(_TOOLS_BY_PERMISSION.get(k, 0) for k in added),
            "lost": sum(_TOOLS_BY_PERMISSION.get(k, 0) for k in removed)}


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

    @router.get("/v1/identity/access-roles/{role_id}/versions/{number}")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org())
    def get_access_role_version(role_id: str, number: int, request: Request):
        return run(request, lambda db, ctx: roles_service.version(
            db, ctx.principal.org_id, role_id, number))

    @router.post("/v1/identity/access-roles/{role_id}/preview")
    @permit(P.ACCESS_ROLE_READ, scope=Scope.org(), readonly_post=True)
    def preview_access_role(role_id: str, body: RolePreview, request: Request):
        def work(db, ctx):
            out = roles_service.preview(db, ctx.principal.org_id, role_id, body.permissions)
            added, removed = set(out["added"]), set(out["removed"])
            out["routes"] = operation_counts(request.app, added, removed)
            out["tools"] = tool_counts(added, removed)
            return out
        return run(request, work)

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

    @router.post("/v1/identity/access-roles/{role_id}/reassign")
    @permit(P.ACCESS_ROLE_MANAGE, scope=Scope.org())
    def reassign_access_role(role_id: str, body: RoleReassign, request: Request):
        """Move every binding of this role to another (each under §6.11), then delete it."""
        return run(request, lambda db, ctx: bindings_service.reassign_role(
            db, ctx, role_id, body.to_role_id, delete=body.delete), write=True)

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

    # ---- who can grant it, and asking them (the Access-denied page) ----------------------------------
    @router.get("/v1/identity/grantors")
    @authenticated(note="display names of who could grant a permission; no security detail")
    def list_grantors(request: Request, permission: str, project_id: Optional[str] = None):
        def work(db, ctx):
            if permission not in PERMISSION_META:
                raise AccessError(422, "unknown_permission")
            org = ctx.principal.org_id
            if project_id and db.execute(text(
                    "SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                    "WHERE p.id = :p AND w.org_id = :o"), {"p": project_id, "o": org}).first() is None:
                raise AccessError(404, "not_found")
            return {"permission": permission,
                    "scope": f"project:{project_id}" if project_id else "org",
                    "grantors": bindings_service.grantors(db, org, permission, project_id=project_id)}
        return run(request, work)

    @router.post("/v1/identity/access-requests", status_code=202)
    @authenticated(self_only=True, note="the caller asks for access for themselves")
    def request_access(body: AccessRequest, request: Request):
        """Amendment A13: the request is stored (one open request per person, permission and
        scope) and recorded as `access.requested`; the answer names who can grant it."""
        from boss_engine.identity import audit as audit_module
        from boss_engine.identity.ids import new_id

        def work(db, ctx):
            if body.permission not in PERMISSION_META:
                raise AccessError(422, "unknown_permission")
            org = ctx.principal.org_id
            if body.project_id and db.execute(text(
                    "SELECT 1 FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                    "WHERE p.id = :p AND w.org_id = :o"), {"p": body.project_id, "o": org}).first() is None:
                raise AccessError(404, "not_found")
            grantors = bindings_service.grantors(db, org, body.permission, project_id=body.project_id)
            existing = db.execute(text(
                "SELECT id FROM identity_access_requests WHERE org_id = :o AND requester_principal_id = :r "
                "AND permission_key = :k AND coalesce(project_id, '') = coalesce(:p, '') AND status = 'open'"),
                {"o": org, "r": ctx.principal.id, "k": body.permission, "p": body.project_id}).scalar()
            if existing:
                return {"status": "requested", "id": existing, "already_open": True,
                        "permission": body.permission, "grantors": grantors}
            request_id = new_id("acr")
            db.execute(text(
                "INSERT INTO identity_access_requests (id, org_id, requester_principal_id, permission_key, "
                "project_id, reason) VALUES (:id, :o, :r, :k, :p, :why)"),
                {"id": request_id, "o": org, "r": ctx.principal.id, "k": body.permission,
                 "p": body.project_id, "why": (body.reason or None)})
            audit_module.record(audit_module.SecurityEvent.from_context(
                ctx, "access.requested", audit_module.Outcome.INFO, target_type="permission",
                target_id=body.permission,
                scope=f"project:{body.project_id}" if body.project_id else "org",
                details={"request_id": request_id}), db=db)
            return {"status": "requested", "id": request_id, "already_open": False,
                    "permission": body.permission, "grantors": grantors}
        return run(request, work, write=True)

    _REQUEST_COLUMNS = (
        "SELECT r.id, r.requester_principal_id, p.display_name AS requester_name, r.permission_key, "
        "r.project_id, r.reason, r.status, r.created_at, r.resolved_at, r.resolved_by "
        "FROM identity_access_requests r JOIN identity_principals p ON p.id = r.requester_principal_id ")

    def _candidate_roles(db, org: str, permission: str, project_id: Optional[str]) -> list[dict]:
        """Roles that carry the permission and bind at the request's scope."""
        rows = db.execute(text(
            "SELECT r.id, r.key, r.name, r.bindable_scopes FROM identity_access_roles r JOIN "
            "identity_access_role_permissions rp ON rp.role_id = r.id WHERE rp.permission_key = :k "
            "AND NOT r.system AND (r.org_id IS NULL OR r.org_id = :o) ORDER BY r.builtin DESC, r.key"),
            {"k": permission, "o": org}).mappings().all()
        scope = "project" if project_id else "org"
        return [{"id": r["id"], "key": r["key"], "name": r["name"]} for r in rows
                if scope in r["bindable_scopes"]]

    @router.get("/v1/identity/access-requests")
    @permit(P.ROLE_BINDING_READ, scope=Scope.org())
    def list_access_requests(request: Request, status: str = "open"):
        """Open (or decided) access requests, for whoever can see bindings, each with the roles
        that would grant it."""
        def work(db, ctx):
            org = ctx.principal.org_id
            rows = db.execute(text(_REQUEST_COLUMNS + "WHERE r.org_id = :o AND r.status = :s "
                                   "ORDER BY r.created_at DESC LIMIT 200"),
                              {"o": org, "s": status}).mappings().all()
            return {"requests": [{**dict(r), "candidate_roles": _candidate_roles(
                db, org, r["permission_key"], r["project_id"])} for r in rows]}
        return run(request, work)

    @router.get("/v1/identity/projects/{project_id}/access-requests")
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.project(path="project_id"))
    def list_project_access_requests(project_id: str, request: Request, status: str = "open"):
        """A project Admin's queue: the requests for this project only."""
        def work(db, ctx):
            org = ctx.principal.org_id
            rows = db.execute(text(_REQUEST_COLUMNS + "WHERE r.org_id = :o AND r.project_id = :p AND "
                                   "r.status = :s ORDER BY r.created_at DESC LIMIT 200"),
                              {"o": org, "p": project_id, "s": status}).mappings().all()
            return {"requests": [{**dict(r), "candidate_roles": _candidate_roles(
                db, org, r["permission_key"], r["project_id"])} for r in rows]}
        return run(request, work)

    def _open_request(db, ctx, request_id: str) -> dict:
        row = db.execute(text(_REQUEST_COLUMNS + "WHERE r.id = :i AND r.org_id = :o FOR UPDATE OF r"),
                         {"i": request_id, "o": ctx.principal.org_id}).mappings().first()
        if row is None:
            raise AccessError(404, "not_found")
        if row["status"] != "open":
            raise AccessError(409, "already_resolved", extra={"status": row["status"]})
        return dict(row)

    def _resolve(db, ctx, row: dict, status: str, event: str, details: dict) -> dict:
        from boss_engine.identity import audit as audit_module

        db.execute(text("UPDATE identity_access_requests SET status = :s, resolved_at = now(), "
                        "resolved_by = :b WHERE id = :i"),
                   {"s": status, "b": ctx.principal.id, "i": row["id"]})
        audit_module.record(audit_module.SecurityEvent.from_context(
            ctx, event, audit_module.Outcome.SUCCESS, target_type="principal",
            target_id=row["requester_principal_id"], permission=row["permission_key"],
            scope=f"project:{row['project_id']}" if row["project_id"] else "org",
            details={"request_id": row["id"], **details}), db=db)
        return dict(db.execute(text(_REQUEST_COLUMNS + "WHERE r.id = :i"), {"i": row["id"]}).mappings().first())

    @router.post("/v1/identity/access-requests/{request_id}/approve")
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.of("access_request", path="request_id"))
    def approve_access_request(request_id: str, body: AccessRequestApproval, request: Request):
        """Grant an access request by binding a role that carries the permission, at the request's
        scope, under every §6.11 rule (the same add_binding as POST /role-bindings)."""
        def work(db, ctx):
            row = _open_request(db, ctx, request_id)
            if row["permission_key"] not in roles_service.permissions_of(db, body.role_id):
                raise AccessError(422, "role_lacks_permission",
                                  "That role does not carry the permission asked for.")
            binding = bindings_service.add_binding(
                db, ctx, subject_type="principal", subject_id=row["requester_principal_id"],
                role_id=body.role_id, scope_type="project" if row["project_id"] else "org",
                scope_id=row["project_id"], expires_at=body.expires_at,
                reason=body.reason or f"access request {row['id']}")
            return {"request": _resolve(db, ctx, row, "granted", "access.request_granted",
                                        {"role_id": body.role_id, "binding_id": binding["id"]}),
                    "binding": binding}
        return run(request, work, write=True)

    @router.post("/v1/identity/access-requests/{request_id}/deny")
    @permit(P.ROLE_BINDING_ASSIGN, scope=Scope.of("access_request", path="request_id"))
    def deny_access_request(request_id: str, body: AccessRequestDenial, request: Request):
        def work(db, ctx):
            row = _open_request(db, ctx, request_id)
            return {"request": _resolve(db, ctx, row, "declined", "access.request_declined",
                                        {"reason": (body.reason or "")[:500]})}
        return run(request, work, write=True)

    # ---- approval seats (§6.13) -------------------------------------------------------------------
    @router.get("/v1/identity/seats")
    @permit(P.ROLE_BINDING_READ, scope=Scope.org())
    def list_approval_seats(request: Request):
        from boss_engine.identity import approvals

        def work(db, ctx):
            org = ctx.principal.org_id
            return {"seats": approvals.seat_rows(db, org), "known_seats": approvals.known_seats(),
                    "tiers": {str(t): list(seats) for t, seats in sorted(approvals.tier_seats().items())},
                    "two_person_tiers": sorted(approvals.TWO_PERSON_TIERS),
                    "vacant_tiers": approvals.seat_vacant_tiers(db, org),
                    "single_operator_waiver": approvals.waiver(db, org)}
        return run(request, work)

    @router.put("/v1/identity/seats/{seat_key}")
    @permit(P.APPROVAL_RULE_MANAGE, scope=Scope.org())
    def put_approval_seat(seat_key: str, body: SeatHolders, request: Request):
        """Replace who holds an approval seat (Owner; critical, step-up)."""
        from boss_engine.identity import approvals

        return run(request, lambda db, ctx: approvals.set_seat(db, ctx, seat_key, body.principal_ids),
                   write=True)

    @router.get("/v1/identity/decisions/{decision_id}/approvals")
    @permit(P.DECISION_READ, scope=Scope.of("decision", path="decision_id"))
    def decision_approvals(decision_id: str, request: Request):
        """Who has approved a decision so far, and how many approvals it needs (§6.13)."""
        from boss_engine.identity import approvals

        def work(db, ctx):
            row = db.execute(text("SELECT id, risk_tier FROM decisions WHERE id = :d"),
                             {"d": decision_id}).mappings().first()
            if row is None:
                raise AccessError(404, "not_found")
            return approvals.status(db, ctx.principal.org_id, dict(row))
        return run(request, work)

    return router
