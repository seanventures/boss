"""MCP confirmations under identity (IDENTITY.md §6.13 "MCP confirmations keep their human-approval
flow"). FX-B.

An MCP client holds a key (or a delegated run token): it cannot step up, and it can never approve.
For the few operations a person may confirm on its behalf (mcp/api_surface.py
`confirmation_eligible`), the client asks; a SEPARATE person, signed in, reviews the exact request
and approves it; the client then replays exactly that request once. Every decision here is the
identity PDP's — the legacy members table is never read:

  * ask      — the requester holds the route's permission at the confirmed project (only the
               step-up a key cannot give is left to the reviewer); an identity-administration
               permission is never confirmable (authz.KEY_NEVER_STEPS_UP);
  * review   — the reviewer is a person in a human session who holds that permission there
               (fresh step-up included), and for a decision approval the seat its tier needs;
  * replay   — the confirmation is consumed for the exact request, the reviewer is checked to
               still hold the permission, and the request then counts as stepped up by them
               (authz.HUMAN_CONFIRMED) and any approval it makes is theirs (approvals.MCP_REVIEWER).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.routing import Match


def _route(app: Any, method: str, path: str) -> tuple[APIRoute, dict[str, Any]]:
    scope = {"type": "http", "path": path, "root_path": "", "method": method}
    for route in app.routes:
        if isinstance(route, APIRoute) and method in route.methods:
            match, child = route.matches(scope)
            if match == Match.FULL:
                return route, dict(child.get("path_params") or {})
    raise HTTPException(status_code=422, detail={"error": "unknown_operation"})


def decide_scope(app: Any, ctx: Any, *, method: str, path: str, project_id: str,
                 query: Optional[dict] = None, body: Any = None) -> tuple[Any, Any, Any]:
    """(the route's Permit, the resolved scope, the route) for a confirmed request — the scope the
    route itself would be decided at, which must be the confirmation's project (404 otherwise)."""
    from boss_engine.identity import scope as scope_module
    from boss_engine.identity.declarations import Permit, access_of
    from boss_engine.identity.scope import RequestValues, ScopeKind

    route, path_params = _route(app, method, path)
    access = access_of(route.endpoint)
    if not isinstance(access, Permit):
        raise HTTPException(status_code=422, detail={"error": "not_confirmable"})
    try:
        resolved = scope_module.resolve(access.scope, RequestValues(path_params, query or {}, body),
                                        org_id=ctx.principal.org_id)
    except Exception:  # noqa: BLE001 — unresolvable or another project's object: not found
        raise HTTPException(status_code=404, detail={"error": "not_found"}) from None
    if resolved.kind == ScopeKind.PROJECT and resolved.project_id != project_id:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return access, resolved, route


def _refuse(decision: Any) -> None:
    raise HTTPException(status_code=decision.status, detail=decision.error_body())


def require_requester(app: Any, ctx: Any, *, method: str, path: str, project_id: str,
                      query: Optional[dict] = None, body: Any = None) -> None:
    from boss_engine.identity import authz
    from boss_engine.identity.authz import KEY_NEVER_STEPS_UP, DenyReason

    if ctx.is_human_session:
        raise HTTPException(status_code=409, detail={
            "error": "confirm_it_yourself", "message": "You are signed in: do it directly."})
    access, resolved, _route_ = decide_scope(app, ctx, method=method, path=path,
                                             project_id=project_id, query=query, body=body)
    if access.permission in KEY_NEVER_STEPS_UP:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "permission": access.permission,
                                                     "reason": "not_confirmable"})
    decision = authz.check(ctx, access.permission, resolved)
    if not decision.allowed and decision.reason != DenyReason.STEP_UP_REQUIRED:
        _refuse(decision)


def require_reviewer(app: Any, ctx: Any, db: Any, *, method: str, path: str, project_id: str,
                     query: Optional[dict] = None, body: Any = None) -> str:
    """The reviewing person, or HTTPException. Returns the reviewer's principal id."""
    from boss_engine.identity import approvals, authz
    from boss_engine.identity.legacy import is_bridge_principal

    if ctx is None or not ctx.is_human_session or is_bridge_principal(ctx.principal.id):
        raise HTTPException(status_code=403, detail={"error": "human_session_required"})
    access, resolved, route = decide_scope(app, ctx, method=method, path=path, project_id=project_id,
                                           query=query, body=body)
    decision = authz.check(ctx, access.permission, resolved, db=db)
    if not decision.allowed:
        _refuse(decision)
    if route.path == "/v1/decisions/{decision_id}/approve":
        from sqlalchemy import text

        decision_id = _route(app, method, path)[1].get("decision_id")
        tier = db.execute(text("SELECT risk_tier FROM decisions WHERE id = :d"), {"d": decision_id}).scalar()
        wanted = approvals.seats_for_tier(tier)
        if wanted and not approvals.seats_held(db, ctx.principal.org_id, ctx.principal.id, wanted):
            raise HTTPException(status_code=403, detail={"error": "seat_required", "seats": list(wanted),
                                                         "tier": int(tier or 0)})
    return ctx.principal.id


def reviewer_may_still(app: Any, ctx: Any, db: Any, *, reviewer_id: str, method: str, path: str,
                       project_id: str, query: Optional[dict] = None, body: Any = None) -> str:
    """At replay: the reviewer still holds the permission (a demoted reviewer's yes is void).
    Returns the permission the replay is confirmed for."""
    from boss_engine.identity import authz
    from boss_engine.identity.principal import PrincipalKind
    from boss_engine.identity.users import load_principal

    access, resolved, _route_ = decide_scope(app, ctx, method=method, path=path, project_id=project_id,
                                             query=query, body=body)
    reviewer = load_principal(db, reviewer_id)
    if reviewer is None or reviewer.kind != PrincipalKind.USER or reviewer.org_id != ctx.principal.org_id \
            or access.permission not in authz.effective_permissions(reviewer, resolved, db=db).permissions:
        raise HTTPException(status_code=403, detail={"error": "reviewer_no_longer_allowed"})
    return access.permission
