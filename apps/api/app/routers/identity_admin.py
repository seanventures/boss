"""/v1/identity/{audit,epoch,posture,policy,shadow-report,legacy}: the security audit log, the
identity epoch (and its push stream), posture, the identity policy, the shadow -> enforce rollout
report and the legacy bridges. Stream S5. Posture and policy answer S4's shapes
(boss_engine/identity/console_shapes.py, which lists where the API keeps a stricter rule).

IDENTITY.md §8 (audit), §9.1 (posture), §10 (routes), §12 (migration); IDENTITY_PERMISSIONS.md §5
for each declaration. The identity routers are ALWAYS enforced (router key `identity*`).

There is NO route that writes an audit event from a request body (A77): export, verify, prune and
every change below record their own event, built from the verified AuthContext and the change.

Include it in main.py (lead's zone, next to identity_me):
    from .routers.identity_admin import build_identity_admin_router
    app.include_router(build_identity_admin_router())
    app.add_middleware(RequestIdMiddleware)    # boss_engine.identity.request_ids
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, authenticated, permit
from boss_engine.identity import (audit, audit_view, change_feed, console_shapes, migration,
                                  posture, shadow_report)
from boss_engine.identity.legacy import LegacyKind
from boss_engine.identity.policy import PolicyError
from boss_engine.identity.principal import AuthContext

#: the bridges an Owner can end early or extend from the console
_PATHS = {k.value: k for k in (LegacyKind.SHARED_PASSWORD, LegacyKind.CONSOLE_API_KEY,
                               LegacyKind.NETWORK_MODE)}
MIN_PRUNE_DAYS = 30


def _context(request: Request) -> Optional[AuthContext]:
    """The request's verified AuthContext: the same authentication the guard ran (S1's
    authenticate, falling back to the legacy bridge). Never built from the body."""
    from apps.api.app import identity_guard
    return identity_guard.request_auth(request)


def _refuse(status: int, error: str, detail: Optional[str] = None) -> JSONResponse:
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def _when(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_identity_admin_router(*, start_background: Optional[bool] = None) -> APIRouter:
    router = APIRouter(tags=["identity"])
    if start_background is None:
        start_background = "PYTEST_CURRENT_TEST" not in os.environ
    if start_background:
        shadow_report.start_flusher()

    # ---- the security audit log ------------------------------------------------------------------

    @router.get("/v1/identity/audit")
    @permit(P.AUDIT_READ, scope=Scope.org())
    def list_audit(request: Request, q: Optional[str] = None, outcome: Optional[str] = None,
                   actor: Optional[str] = None, grant: Optional[str] = None,
                   kind: Optional[str] = None, event: Optional[str] = None,
                   since: Optional[str] = None, until: Optional[str] = None,
                   cursor: Optional[str] = None, limit: int = 50):
        """S4's AuditPage: {events, cursor, chain}. Filters: q, outcome (comma list), actor (the
        actor or the person an agent acted for), grant, kind, event (`auth.*`), since, until."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        try:
            frm, to = _when(since), _when(until)
        except ValueError:
            return _refuse(422, "invalid_time")
        with begin() as db:
            return audit_view.page(ctx.principal.org_id, db=db, q=q, outcome=outcome, actor=actor,
                                   grant=grant, kind=kind, event=event, since=frm, until=to,
                                   cursor=cursor, limit=limit)

    @router.get("/v1/identity/audit/{event_id}")
    @permit(P.AUDIT_READ, scope=Scope.org())
    def get_audit_event(request: Request, event_id: str):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            row = audit_view.one(ctx.principal.org_id, event_id, db=db)
        return row if row else _refuse(404, "not_found")

    @router.post("/v1/identity/audit/export")
    @permit(P.AUDIT_EXPORT, scope=Scope.org())
    def export_audit(request: Request, since: Optional[str] = None, until: Optional[str] = None):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        try:
            frm, to = _when(since), _when(until)
        except ValueError:
            return _refuse(422, "invalid_time")
        with begin() as db:
            bundle = audit.export(ctx.principal.org_id, db=db, ctx=ctx, since=frm, until=to)
        # S4's shape ({url, events: count, anchor}) plus the bundle itself to save
        return {"url": None, "events": len(bundle["events"]),
                "anchor": (bundle["anchor"] or {}).get("hash"), "bundle": bundle}

    @router.post("/v1/identity/audit/verify")
    @permit(P.AUDIT_EXPORT, scope=Scope.org())
    def verify_audit(request: Request):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            result = audit.verify_chain(ctx.principal.org_id, db=db)
            audit.record(audit.SecurityEvent.from_context(
                ctx, "audit.verified",
                audit.Outcome.SUCCESS if result.ok else audit.Outcome.FAILURE,
                details={"checked": result.checked, "last_seq": result.last_seq,
                         "first_break_seq": result.first_break_seq}), db=db)
        return {"ok": result.ok, "seq": result.last_seq, "first_break": result.first_break_seq,
                "checked": result.checked, "last_seq": result.last_seq,
                "first_break_seq": result.first_break_seq,
                "verified_at": result.verified_at.isoformat() if result.verified_at else None}

    @router.post("/v1/identity/audit/prune")
    @permit(P.ACTIVITY_PRUNE, scope=Scope.org())
    def prune_audit(request: Request, payload: Optional[dict] = Body(default=None)):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        days = (payload or {}).get("older_than_days")
        if not isinstance(days, int) or isinstance(days, bool) or days < MIN_PRUNE_DAYS:
            return _refuse(422, "invalid_retention", f"older_than_days >= {MIN_PRUNE_DAYS}")
        with begin() as db:
            result = audit.prune(ctx.principal.org_id,
                                 before=datetime.now(timezone.utc) - timedelta(days=days),
                                 db=db, actor_principal_id=ctx.principal.id, ctx=ctx)
        return result.__dict__

    # ---- the identity epoch: refetch when it moves --------------------------------------------

    @router.get("/v1/identity/epoch")
    @authenticated(self_only=True)
    def get_epoch(request: Request):
        """The same number GET /v1/identity/me carries as `epoch`, without the rest of /me."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            return {"epoch": change_feed.current_epoch(ctx.principal.org_id, db=db)}

    @router.get("/v1/identity/epoch/stream")
    @authenticated(self_only=True)
    async def stream_epoch(request: Request):
        """Server-sent events: `event: epoch` with {"epoch": n} at once and whenever any identity
        change commits; a comment line every 25 s keeps proxies open. Carries no identity data."""
        from starlette.concurrency import run_in_threadpool
        ctx = await run_in_threadpool(_context, request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        change_feed.FEED.start()

        async def events():
            async for epoch in change_feed.FEED.changes(is_disconnected=request.is_disconnected):
                yield change_feed.sse(epoch)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ---- posture and policy ---------------------------------------------------------------------

    @router.get("/v1/identity/posture")
    @permit(P.USER_READ, scope=Scope.org())
    def get_posture(request: Request):
        """S4's Posture shape (console_shapes.posture_view)."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            return console_shapes.posture_view(ctx.principal.org_id, db=db)

    @router.get("/v1/identity/policy")
    @permit(P.SECURITY_POLICY_READ, scope=Scope.org())
    def read_policy(request: Request):
        """S4's SecurityPolicy, plus the policy keys it lacks (console_shapes.policy_view)."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            return console_shapes.policy_view(ctx.principal.org_id, db=db)

    @router.put("/v1/identity/policy")
    @permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
    def write_policy(request: Request, payload: Any = Body(...)):
        """Applies only what differs from the current policy; `network_mode.active=false` is
        Require sign-in. Unknown keys 422; network trust back on, or read-only fields, refused."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        try:
            with begin() as db:
                return console_shapes.apply_policy_put(ctx.principal.org_id, payload, db=db,
                                                       ctx=ctx)
        except console_shapes.PolicyRefused as exc:
            return _refuse(409, "policy_refused", str(exc))
        except PolicyError as exc:
            return _refuse(422, "invalid_policy", str(exc))

    # ---- the shadow -> enforce rollout -----------------------------------------------------------

    @router.get("/v1/identity/shadow-report")
    @permit(P.SECURITY_POLICY_READ, scope=Scope.org())
    def get_shadow_report(request: Request, days: int = shadow_report.OBSERVATION_DAYS):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        from apps.api.app import identity_guard
        with begin() as db:
            shadow_report.flush(db)
        with begin() as db:
            return shadow_report.report(db, routes=identity_guard.route_inventory(request.app),
                                        days=max(1, min(int(days), 90)))

    @router.post("/v1/identity/shadow-report/explain")
    @permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
    def explain_shadow_denial(request: Request, payload: dict = Body(...)):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        try:
            with begin() as db:
                items = shadow_report.explain_denial(ctx.principal.org_id, payload, db=db,
                                                     ctx=ctx)
        except ValueError as exc:
            return _refuse(422, "invalid_explanation", str(exc))
        return {"explained": items}

    # ---- the legacy bridges ----------------------------------------------------------------------

    @router.get("/v1/identity/legacy")
    @permit(P.SECURITY_POLICY_READ, scope=Scope.org())
    def list_legacy(request: Request):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            return {"paths": posture.legacy_paths(ctx.principal.org_id, db=db),
                    "claimed_at": migration.claimed_at(db, ctx.principal.org_id)}

    @router.post("/v1/identity/legacy/{path}/retire")
    @permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
    def retire_legacy(request: Request, path: str):
        """"Require sign-in": end a bridge now (network mode, the shared password or key)."""
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        if path not in _PATHS:
            return _refuse(404, "not_found")
        with begin() as db:
            if migration.claimed_at(db, ctx.principal.org_id) is None:
                return _refuse(409, "not_claimed", "claim the first Owner before ending a bridge")
            done = migration.retire_path(ctx.principal.org_id, _PATHS[path], db=db,
                                         actor_principal_id=ctx.principal.id, actor_ctx=ctx,
                                         reason="ended by an Owner (Require sign-in)")
        return done.__dict__

    @router.post("/v1/identity/legacy/{path}/extend")
    @permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
    def extend_legacy(request: Request, path: str, payload: dict = Body(...)):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        if path not in _PATHS:
            return _refuse(404, "not_found")
        try:
            until = _when(str(payload.get("until") or ""))
        except ValueError:
            until = None
        if until is None:
            return _refuse(422, "invalid_time", "until must be an ISO-8601 time")
        try:
            with begin() as db:
                moved = migration.extend_path(ctx.principal.org_id, _PATHS[path], until, db=db,
                                              actor_ctx=ctx)
        except PermissionError as exc:
            return _refuse(403, "owner_required", str(exc))
        except ValueError as exc:
            return _refuse(409, "cannot_extend", str(exc))
        return {"path": path, "retire_after": moved.isoformat()}

    return router
