"""/v1/identity/{audit,posture,policy,shadow-report,legacy}: the security audit log, posture, the
identity policy, the shadow -> enforce rollout report and the legacy bridges. Stream S5.

IDENTITY.md §8 (audit), §9.1 (posture), §10 (routes), §12 (migration); IDENTITY_PERMISSIONS.md §5
for each declaration. The identity routers are ALWAYS enforced (router key `identity*`).

There is NO route that writes an audit event from a request body (A77): export, verify, prune and
every change below record their own event, built from the verified AuthContext and the change.

Include it in main.py (lead's zone, next to identity_me):
    from .routers.identity_admin import build_identity_admin_router
    app.include_router(build_identity_admin_router())
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from boss_engine.db import begin
from boss_engine.identity import P, Scope, permit
from boss_engine.identity import audit, migration, posture, shadow_report
from boss_engine.identity.legacy import LegacyKind
from boss_engine.identity.policy import PolicyError, get_policy, put_policy, to_mapping
from boss_engine.identity.principal import AuthContext

#: the bridges an Owner can end early or extend from the console
_PATHS = {k.value: k for k in (LegacyKind.SHARED_PASSWORD, LegacyKind.CONSOLE_API_KEY,
                               LegacyKind.NETWORK_MODE)}
MIN_PRUNE_DAYS = 30


def _context(request: Request) -> Optional[AuthContext]:
    """The request's verified AuthContext: the same authentication the guard ran (S1's
    authenticate, falling back to the legacy bridge). Never built from the body."""
    from apps.api.app import identity_guard
    return identity_guard._authenticate(identity_guard._credentials(request))


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
    def list_audit(request: Request, actor: Optional[str] = None, kind: Optional[str] = None,
                   event: Optional[str] = None, outcome: Optional[str] = None,
                   grant: Optional[str] = None, since: Optional[str] = None,
                   until: Optional[str] = None, cursor: Optional[int] = None, limit: int = 50):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        try:
            frm, to = _when(since), _when(until)
        except ValueError:
            return _refuse(422, "invalid_time")
        with begin() as db:
            return audit.list_events(ctx.principal.org_id, db=db, actor=actor, kind=kind,
                                     event=event, outcome=outcome, grant=grant, since=frm,
                                     until=to, cursor=cursor, limit=limit)

    @router.get("/v1/identity/audit/{event_id}")
    @permit(P.AUDIT_READ, scope=Scope.org())
    def get_audit_event(request: Request, event_id: int):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            row = audit.get_event(ctx.principal.org_id, event_id, db=db)
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
            return audit.export(ctx.principal.org_id, db=db, ctx=ctx, since=frm, until=to)

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
        return {"ok": result.ok, "checked": result.checked, "last_seq": result.last_seq,
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

    # ---- posture and policy ---------------------------------------------------------------------

    @router.get("/v1/identity/posture")
    @permit(P.USER_READ, scope=Scope.org())
    def get_posture(request: Request):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            return posture.posture(ctx.principal.org_id, db=db)

    @router.get("/v1/identity/policy")
    @permit(P.SECURITY_POLICY_READ, scope=Scope.org())
    def read_policy(request: Request):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        with begin() as db:
            current = to_mapping(get_policy(ctx.principal.org_id, db=db))
        current.pop("extra", None)     # internal bookkeeping, not an operator setting
        return current

    @router.put("/v1/identity/policy")
    @permit(P.SECURITY_POLICY_MANAGE, scope=Scope.org())
    def write_policy(request: Request, payload: dict = Body(...)):
        ctx = _context(request)
        if ctx is None:
            return _refuse(401, "unauthenticated")
        if "extra" in payload:
            return _refuse(422, "invalid_policy", "extra is not an operator setting")
        try:
            with begin() as db:
                updated = put_policy(ctx.principal.org_id, payload, db=db, actor=ctx)
        except PolicyError as exc:
            return _refuse(422, "invalid_policy", str(exc))
        out = to_mapping(updated)
        out.pop("extra", None)
        return out

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
        except ValueError as exc:
            return _refuse(409, "cannot_extend", str(exc))
        return {"path": path, "retire_after": moved.isoformat()}

    return router
