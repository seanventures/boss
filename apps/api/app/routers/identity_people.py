"""/v1/identity/{users,invitations,sessions,service-accounts,api-keys}: administering people and
machine credentials (IDENTITY.md §10 "Administration", §5, §6.11, §6.12). Stream S1.

Each route declares its catalogue permission (step-up and criticality come from the catalogue and
are enforced by the PDP at the enforcement point). Handlers add what a permission cannot say:
  - the org is always the caller's; another org's row is a 404;
  - an Admin cannot disable, delete or reset the MFA of an Owner; resetting an Owner needs another
    Owner; the last active Owner can never be disabled or deleted (409 "Add another Owner first.");
  - PATCH bodies are allowlists: an unknown field is a 422 and nothing changes (A36);
  - service-account keys need the creator to hold everything the account holds (A25);
  - keys and reset links are shown once, to the authenticated admin only.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import P, permit
from boss_engine.identity import api_keys, invitations, recovery, service_accounts, sessions, signin, users
from boss_engine.identity.audit import Outcome
from boss_engine.identity.authn import AuthnError
from boss_engine.identity.authn_support import emit, public_origin, send_mail, smtp_configured
from boss_engine.identity.principal import AuthContext

from .identity_auth import current_context, error, refusal


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteBody(_Body):
    email: str = Field(max_length=320)
    bindings: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    group_ids: list[str] = Field(default_factory=list, max_length=50)


class ServiceAccountBody(_Body):
    name: str = Field(max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    owner_principal_ids: list[str] = Field(min_length=1, max_length=20)


class SakBody(_Body):
    """A new key. Accepts the console's field names (projects, networks, expires_days) and the
    spec's (project_ids, cidrs, expires_in_days). `replaces` + `overlap_hours` (0, 1, 24, 168)
    is a rotation: same as POST …/{id}/rotate."""
    name: str = Field(max_length=120)
    scopes: list[str] = Field(max_length=200)
    project_ids: Optional[list[str]] = None
    projects: Optional[list[str]] = None
    cidrs: Optional[list[str]] = None
    networks: Optional[str] = Field(default=None, max_length=2000)
    expires_in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    expires_days: Optional[int] = Field(default=None, ge=0, le=3650)
    replaces: Optional[str] = Field(default=None, max_length=64)
    overlap_hours: Optional[int] = Field(default=None, ge=0, le=168)

    def resolved(self) -> dict[str, Any]:
        nets = self.cidrs if self.cidrs is not None else (
            [n.strip() for n in self.networks.split(",") if n.strip()] if self.networks else None)
        return {"project_ids": self.project_ids if self.project_ids is not None else self.projects,
                "cidrs": nets,
                "expires_in_days": self.expires_in_days if self.expires_in_days is not None else self.expires_days}

    def grace(self) -> str:
        return {0: "0", 1: "1h", 24: "24h", 168: "7d"}.get(int(self.overlap_hours or 0), "")


class RotateBody(_Body):
    grace: str = Field(default="1h", pattern="^(0|1h|24h|7d)$")


def _person_view(db: Any, u: users.UserRow, *, detail: bool = False) -> dict[str, Any]:
    f = users.factors(db, u.principal_id)
    view: dict[str, Any] = {
        "id": u.principal_id, "display_name": u.display_name, "email": u.email, "status": u.status,
        "access_roles": users.role_keys(db, u.principal_id), "mfa": f.any_second_factor,
        "mfa_required": users.mfa_required(db, u), "password": bool(u.password_hash),
        "self_signed_up": u.self_signed_up, "network_bound": u.network_bound,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}
    if detail:
        view["factors"] = {"totp": f.totp, "passkeys": f.passkeys,
                           "recovery_codes_remaining": f.recovery_codes_remaining}
        view["sessions"] = len(sessions.list_live(db, u.principal_id))
        view["api_keys"] = len([k for k in api_keys.list_keys(db, principal_id=u.principal_id, kinds=("pat",))
                                if k.revoked_at is None])
        view["sso_links"] = [r[0] for r in db.execute(text(
            "SELECT coalesce(p.name, i.provider_id) FROM identity_user_identities i LEFT JOIN identity_providers p "
            "ON p.id = i.provider_id WHERE i.principal_id = :p"), {"p": u.principal_id})]
    return view


def _target(db: Any, ctx: AuthContext, user_id: str) -> Optional[users.UserRow]:
    """A person in the CALLER's org (another org's id is simply not found)."""
    u = users.by_id(db, user_id, org_id=ctx.principal.org_id)
    return u if u and not u.network_bound else None


def _owner_guard(db: Any, ctx: AuthContext, target: users.UserRow, *, removing: bool,
                 permission: str = "user:update") -> Optional[JSONResponse]:
    """Every action on ANOTHER person (§6.11, §6.12): the actor must hold everything the target
    holds, at every scope the target holds anything (so an Admin can act on neither an Owner nor
    a more-privileged non-Owner); and the last permanent Owner stays. One Owner lock for every
    path (bindings.lock_owners)."""
    from boss_engine.identity import bindings
    from boss_engine.identity.roles import AccessError

    org = ctx.principal.org_id
    bindings.lock_owners(db, org)
    if target.principal_id != ctx.principal.id:
        try:
            bindings.require_covers(db, ctx, target.principal_id, permission)
        except AccessError as exc:
            return JSONResponse(status_code=exc.status, content=exc.body())
    if removing and target.principal_id in bindings.owner_ids(db, org) and not bindings.active_owners(
            db, org, without_principal=target.principal_id):
        return error(409, "last_owner", message="Add another Owner first.")
    return None


def _covers(db: Any, ctx: AuthContext, principal_id: str, permission: str) -> Optional[JSONResponse]:
    """The actor ⊇ target rule for a credential that belongs to a person (sessions, keys)."""
    from boss_engine.identity import bindings
    from boss_engine.identity.roles import AccessError

    try:
        bindings.require_covers(db, ctx, principal_id, permission)
    except AccessError as exc:
        return JSONResponse(status_code=exc.status, content=exc.body())
    return None


def _cascade(db: Any, ctx: AuthContext, trigger: str, target_id: str, reason: str) -> None:
    from boss_engine.identity import delegation

    try:
        delegation.revoke_cascade(delegation.CascadeTrigger(trigger), target_id, actor=ctx, reason=reason, db=db)
    except NotImplementedError:
        pass   # S3's cascade; the person's own credentials are already refused (status is live)


def _bump(db: Any, ctx: AuthContext) -> None:
    from boss_engine.identity.authn_support import bump_epoch

    try:
        bump_epoch(ctx.principal.org_id, db=db, reason="identity change")
    except NotImplementedError:
        pass




def build_identity_people_router() -> APIRouter:
    router = APIRouter(tags=["identity-admin"])

    # ---- people ---------------------------------------------------------------------------------------

    @router.get("/v1/identity/users")
    @permit(P.USER_READ)
    def list_users(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                rows = db.execute(text(
                    "SELECT p.id FROM identity_principals p JOIN identity_users u ON u.principal_id = p.id "
                    "WHERE p.org_id = :o AND p.kind = 'user' AND NOT p.network_bound AND p.status <> 'revoked' "
                    "ORDER BY p.display_name"), {"o": ctx.principal.org_id})
                people = [users.by_id(db, r[0]) for r in rows]
                return JSONResponse({"users": [_person_view(db, u) for u in people if u]})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/users/{user_id}")
    @permit(P.USER_READ)
    def get_user(user_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                return JSONResponse(_person_view(db, u, detail=True))
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/users/{user_id}/factors")
    @permit(P.USER_READ)
    def user_factors(user_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                return JSONResponse({"factors": users.factor_list(db, u.principal_id)})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users")
    @permit(P.USER_INVITE)
    def invite_user(body: InviteBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                inv, token = invitations.create(db, inviter=ctx, email=body.email, bindings=body.bindings,
                                                group_ids=body.group_ids)
                link = f"{public_origin(db) or ''}/invite#t={token}"
                # H2 (F3): an invitation is emailed only on the BOUND public origin; without one the
                # link is shown once to the inviter instead. M1: an emailed invitation proves the
                # mailbox, so accepting it verifies the address (identity_invitations.emailed).
                if smtp_configured() and public_origin(db):
                    db.execute(text("UPDATE identity_invitations SET emailed = true WHERE id = :id"),
                               {"id": inv.id})
                    send_mail(inv.email, "You are invited to BOSS",
                              f"{ctx.principal.display_name} invited you to BOSS. Accept within 7 days:\n\n{link}")
                    return JSONResponse(status_code=201, content={
                        "invitation_id": inv.id, "emailed": True, "expires_at": inv.expires_at.isoformat()})
                # No SMTP: shown ONCE to the authenticated inviter, to hand over out of band.
                return JSONResponse(status_code=201, content={
                    "invitation_id": inv.id, "emailed": False, "link": link, "shown_once": True,
                    "expires_at": inv.expires_at.isoformat()}, headers={"Cache-Control": "no-store"})
        except invitations.InvitationRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/invitations")
    @permit(P.USER_READ)
    def list_invitations(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse({"invitations": [
                    {"id": i.id, "email": i.email, "bindings": i.bindings, "group_ids": i.group_ids,
                     "invited_by": i.invited_by, "expires_at": i.expires_at.isoformat()}
                    for i in invitations.list_open(db, ctx.principal.org_id)]})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/invitations/{invitation_id}")
    @permit(P.USER_INVITE)
    def revoke_invitation(invitation_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not invitations.revoke(db, actor=ctx, invitation_id=invitation_id):
                    return error(404, "not_found")
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.patch("/v1/identity/users/{user_id}")
    @permit(P.USER_UPDATE)
    def update_user(user_id: str, request: Request, raw: dict[str, Any]):
        extra = users.strip_unknown(raw.keys(), users.UPDATABLE_FIELDS)
        if extra:
            # Mass assignment (A36): role, org_id, status, kind... are refused, never ignored.
            return error(422, "field_not_allowed", fields=extra)
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                blocked = _owner_guard(db, ctx, u, removing=False, permission="user:update")
                if blocked:
                    return blocked
                if "display_name" in raw:
                    name = str(raw["display_name"] or "").strip()[:200]
                    if not name:
                        return error(422, "name_required")
                    db.execute(text("UPDATE identity_principals SET display_name = :n, updated_at = now() "
                                    "WHERE id = :id"), {"n": name, "id": u.principal_id})
                if "email" in raw:
                    email = str(raw["email"] or "").strip()
                    if "@" not in email or len(email) > 320:
                        return error(422, "invalid_email")
                    other = users.by_email(db, email)
                    if other and other.principal_id != u.principal_id:
                        return error(409, "email_in_use")
                    # M2 (F3): unverified again, reset links voided, the old address told.
                    users.change_email(db, u, email, by=ctx.principal.id)
                emit(db, org_id=ctx.principal.org_id, event_type="user.updated", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=u.principal_id, details={"fields": sorted(raw)})
                fresh = users.by_id(db, u.principal_id)
                assert fresh is not None
                return JSONResponse(_person_view(db, fresh))
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users/{user_id}/disable")
    @permit(P.USER_DISABLE)
    def disable_user(user_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                blocked = _owner_guard(db, ctx, u, removing=True, permission="user:disable")
                if blocked:
                    return blocked
                if u.status == "disabled":
                    return JSONResponse({"disabled": True})
                users.set_status(db, u.principal_id, "disabled", by=ctx.principal.id, reason="disabled by admin")
                ended = sessions.revoke_all(db, u.principal_id, by=ctx.principal.id, reason="person disabled")
                emit(db, org_id=ctx.principal.org_id, event_type="user.disabled", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=u.principal_id, details={"sessions_revoked": ended})
                _cascade(db, ctx, "person_disabled", u.principal_id, "person disabled")
                _bump(db, ctx)
                return JSONResponse({"disabled": True, "sessions_revoked": ended})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users/{user_id}/enable")
    @permit(P.USER_DISABLE)
    def enable_user(user_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None or u.status not in ("disabled", "active"):
                    return error(404, "not_found")
                blocked = _owner_guard(db, ctx, u, removing=False, permission="user:disable")
                if blocked:
                    return blocked
                if u.status == "disabled":
                    users.set_status(db, u.principal_id, "active", by=ctx.principal.id, reason="enabled by admin")
                    emit(db, org_id=ctx.principal.org_id, event_type="user.enabled", outcome=Outcome.SUCCESS,
                         ctx=ctx, target_type="user", target_id=u.principal_id)
                    _bump(db, ctx)
                return JSONResponse({"enabled": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/users/{user_id}")
    @permit(P.USER_DELETE)
    def delete_user(user_id: str, request: Request):
        """Erase a DISABLED person: credentials and factors go; the principal row stays as a
        tombstone (status revoked) so the audit history keeps its name."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                if u.status != "disabled":
                    return error(409, "disable_first")
                blocked = _owner_guard(db, ctx, u, removing=True, permission="user:delete")
                if blocked:
                    return blocked
                pid = u.principal_id
                for table in ("identity_totp", "identity_webauthn_credentials", "identity_recovery_codes",
                              "identity_user_identities", "identity_group_members"):
                    db.execute(text(f"DELETE FROM {table} WHERE principal_id = :p"), {"p": pid})
                db.execute(text("DELETE FROM identity_role_bindings WHERE subject_type = 'principal' "
                                "AND subject_id = :p"), {"p": pid})
                db.execute(text("UPDATE identity_users SET password_hash = NULL WHERE principal_id = :p"), {"p": pid})
                db.execute(text("UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = :by "
                                "WHERE principal_id = :p AND revoked_at IS NULL"), {"by": ctx.principal.id, "p": pid})
                sessions.revoke_all(db, pid, by=ctx.principal.id, reason="person deleted")
                users.set_status(db, pid, "revoked", by=ctx.principal.id, reason="deleted")
                emit(db, org_id=ctx.principal.org_id, event_type="user.deleted", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=pid)
                _cascade(db, ctx, "person_deleted", pid, "person deleted")
                _bump(db, ctx)
                return JSONResponse({"deleted": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users/{user_id}/reset-mfa")
    @permit(P.USER_RESET_MFA)
    def reset_mfa(user_id: str, request: Request):
        """Remove every second factor and recovery code; the person re-enrols at next sign-in.
        Resetting an Owner needs another Owner, and nobody resets their own."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                if u.principal_id == ctx.principal.id:
                    return error(403, "forbidden", permission="user:reset_mfa")
                blocked = _owner_guard(db, ctx, u, removing=False, permission="user:reset_mfa")
                if blocked:
                    return blocked
                for table in ("identity_totp", "identity_webauthn_credentials", "identity_recovery_codes"):
                    db.execute(text(f"DELETE FROM {table} WHERE principal_id = :p"), {"p": u.principal_id})
                db.execute(text("UPDATE identity_users SET mfa_required = true WHERE principal_id = :p"),
                           {"p": u.principal_id})
                # H1 (F3): the reset is what lets the person's next sign-in enrol a FIRST factor.
                signin.grant_first_factor_after_reset(db, u.principal_id)
                ended = sessions.revoke_all(db, u.principal_id, by=ctx.principal.id, reason="MFA reset")
                emit(db, org_id=ctx.principal.org_id, event_type="user.mfa_reset", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=u.principal_id, details={"sessions_revoked": ended})
                if smtp_configured():
                    send_mail(u.email, "Your BOSS second factor was reset",
                              "An administrator reset your BOSS second factor. Enrol a new one when you next sign in.")
                return JSONResponse({"reset": True, "sessions_revoked": ended})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users/{user_id}/revoke-sessions")
    @permit(P.USER_REVOKE_SESSIONS)
    def revoke_user_sessions(user_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None:
                    return error(404, "not_found")
                blocked = _owner_guard(db, ctx, u, removing=False, permission="user:revoke_sessions")
                if blocked:
                    return blocked
                ended = sessions.revoke_all(db, u.principal_id, by=ctx.principal.id, reason="revoked by admin")
                emit(db, org_id=ctx.principal.org_id, event_type="session.revoked", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=u.principal_id, details={"count": ended})
                return JSONResponse({"sessions_revoked": ended})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/users/{user_id}/reset-link")
    @permit(P.USER_UPDATE)
    def reset_link(user_id: str, request: Request):
        """Without SMTP: a 30-minute, single-use reset link shown ONCE to this authenticated admin
        to hand over out of band (never from a public endpoint). It does not bypass MFA."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                u = _target(db, ctx, user_id)
                if u is None or u.status != "active":
                    return error(404, "not_found")
                blocked = _owner_guard(db, ctx, u, removing=False, permission="user:update")
                if blocked:
                    return blocked
                token = recovery.issue_reset_token(db, u.principal_id)
                emit(db, org_id=ctx.principal.org_id, event_type="password.reset_requested",
                     outcome=Outcome.INFO, ctx=ctx, target_type="user", target_id=u.principal_id,
                     details={"by_admin": True})
                return JSONResponse({"link": f"{public_origin(db) or ''}/reset#t={token}", "shown_once": True,
                                     "expires_in_seconds": recovery.RESET_TTL_SECONDS},
                                    headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    # ---- sessions (everyone's) ------------------------------------------------------------------------

    @router.get("/v1/identity/sessions")
    @permit(P.SESSION_READ)
    def list_sessions(request: Request, principal_id: Optional[str] = None):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                rows = sessions.list_live(db, principal_id, org_id=ctx.principal.org_id)
                return JSONResponse({"sessions": [
                    {"id": r.id, "principal_id": r.principal_id, "methods": list(r.auth_methods),
                     "created_at": r.created_at.isoformat(), "last_seen_at": r.last_seen_at.isoformat(),
                     "ip": r.ip, "user_agent": r.user_agent, "mfa_at": r.mfa_at.isoformat() if r.mfa_at else None}
                    for r in rows]})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/sessions/{session_id}")
    @permit(P.SESSION_REVOKE)
    def revoke_session(session_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                row = sessions.by_id(db, session_id)
                owner = users.load_principal(db, row.principal_id) if row else None
                if row is None or owner is None or owner.org_id != ctx.principal.org_id:
                    return error(404, "not_found")
                blocked = _covers(db, ctx, owner.id, "session:revoke")
                if blocked:
                    return blocked
                if sessions.revoke(db, session_id, by=ctx.principal.id, reason="revoked by admin"):
                    emit(db, org_id=ctx.principal.org_id, event_type="session.revoked", outcome=Outcome.SUCCESS,
                         ctx=ctx, target_type="session", target_id=session_id)
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    # ---- service accounts -----------------------------------------------------------------------------

    @router.get("/v1/identity/service-accounts")
    @permit(P.SERVICE_ACCOUNT_READ)
    def list_service_accounts(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse({"service_accounts": [
                    s.public() for s in service_accounts.list_all(db, org_id=ctx.principal.org_id)]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/service-accounts")
    @permit(P.SERVICE_ACCOUNT_MANAGE)
    def create_service_account(body: ServiceAccountBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                sa = service_accounts.create(db, actor=ctx, name=body.name, description=body.description,
                                             owners=body.owner_principal_ids)
                return JSONResponse(status_code=201, content=sa.public())
        except service_accounts.ServiceAccountRefused as exc:
            return error(exc.status, exc.code)
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/service-accounts/{sa_id}")
    @permit(P.SERVICE_ACCOUNT_READ)
    def get_service_account(sa_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                sa = service_accounts.get(db, sa_id, org_id=ctx.principal.org_id)
                if sa is None:
                    return error(404, "not_found")
                return JSONResponse({**sa.public(), "access_roles": users.role_keys(db, sa_id)})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/service-accounts/{sa_id}/scopes")
    @permit(P.SERVICE_ACCOUNT_READ)
    def sa_scopes(sa_id: str, request: Request):
        """What a new key for this account may hold: everything the account holds."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                sa = service_accounts.get(db, sa_id, org_id=ctx.principal.org_id)
                if sa is None:
                    return error(404, "not_found")
                return JSONResponse({"permissions": sorted(api_keys.held_permissions(db, sa.principal()))})
        except AuthnError as exc:
            return refusal(exc)

    @router.patch("/v1/identity/service-accounts/{sa_id}")
    @permit(P.SERVICE_ACCOUNT_MANAGE)
    def update_service_account(sa_id: str, request: Request, raw: dict[str, Any]):
        if "name" in raw and "display_name" not in raw:      # the console's name for it
            raw = {**{k: v for k, v in raw.items() if k != "name"}, "display_name": raw["name"]}
        if users.strip_unknown(raw.keys(), service_accounts.UPDATABLE_FIELDS):
            return error(422, "field_not_allowed")
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse(service_accounts.update(db, actor=ctx, principal_id=sa_id, fields=raw).public())
        except service_accounts.ServiceAccountRefused as exc:
            return error(exc.status, exc.code)
        except AuthnError as exc:
            return refusal(exc)

    def _sa_status(request: Request, sa_id: str, status: str) -> JSONResponse:
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not service_accounts.set_status(db, actor=ctx, principal_id=sa_id, status=status):
                    return error(404, "not_found")
                _bump(db, ctx)
                return JSONResponse({"status": status})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/service-accounts/{sa_id}/disable")
    @permit(P.SERVICE_ACCOUNT_MANAGE)
    def disable_service_account(sa_id: str, request: Request):
        return _sa_status(request, sa_id, "disabled")

    @router.post("/v1/identity/service-accounts/{sa_id}/enable")
    @permit(P.SERVICE_ACCOUNT_MANAGE)
    def enable_service_account(sa_id: str, request: Request):
        return _sa_status(request, sa_id, "active")

    @router.delete("/v1/identity/service-accounts/{sa_id}")
    @permit(P.SERVICE_ACCOUNT_MANAGE)
    def delete_service_account(sa_id: str, request: Request):
        return _sa_status(request, sa_id, "revoked")

    @router.get("/v1/identity/service-accounts/{sa_id}/keys")
    @permit(P.SERVICE_ACCOUNT_READ)
    def list_sa_keys(sa_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if service_accounts.get(db, sa_id, org_id=ctx.principal.org_id) is None:
                    return error(404, "not_found")
                return JSONResponse({"keys": [api_keys.view(db, k)
                                              for k in api_keys.list_keys(db, principal_id=sa_id, kinds=("sak",))]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/service-accounts/{sa_id}/keys")
    @permit(P.SERVICE_ACCOUNT_KEY_MANAGE)
    def create_sa_key(sa_id: str, body: SakBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                sa = service_accounts.get(db, sa_id, org_id=ctx.principal.org_id)
                if sa is None or sa.status != "active":
                    return error(404, "not_found")
                if body.replaces:
                    if not body.grace():
                        return error(422, "invalid_overlap")
                    created = api_keys.rotate(db, actor=ctx, key_id=body.replaces, owner=sa.principal(),
                                              grace=body.grace())
                else:
                    created = api_keys.create(db, creator=ctx, owner=sa.principal(), kind="sak", name=body.name,
                                              scopes=body.scopes, **body.resolved())
                return JSONResponse(status_code=201, content=api_keys.issued(db, created, replaced=body.replaces),
                                    headers={"Cache-Control": "no-store"})
        except api_keys.KeyRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/service-accounts/{sa_id}/keys/{key_id}/rotate")
    @permit(P.SERVICE_ACCOUNT_KEY_MANAGE)
    def rotate_sa_key(sa_id: str, key_id: str, body: RotateBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                sa = service_accounts.get(db, sa_id, org_id=ctx.principal.org_id)
                if sa is None or sa.status != "active":
                    return error(404, "not_found")
                created = api_keys.rotate(db, actor=ctx, key_id=key_id, owner=sa.principal(), grace=body.grace)
                return JSONResponse(status_code=201, content=api_keys.issued(db, created, replaced=key_id),
                                    headers={"Cache-Control": "no-store"})
        except api_keys.KeyRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/service-accounts/{sa_id}/keys/{key_id}/revoke")
    @permit(P.SERVICE_ACCOUNT_KEY_MANAGE)
    def revoke_sa_key(sa_id: str, key_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not api_keys.revoke(db, actor=ctx, key_id=key_id, org_id=ctx.principal.org_id, owner_id=sa_id):
                    return error(404, "not_found")
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    # ---- every API key (metadata only) ------------------------------------------------------------------

    @router.get("/v1/identity/api-keys")
    @permit(P.API_KEY_READ)
    def list_all_keys(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse({"keys": [api_keys.view(db, k)
                                              for k in api_keys.list_keys(db, org_id=ctx.principal.org_id)]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/api-keys/{key_id}/revoke")
    @permit(P.API_KEY_REVOKE)
    def revoke_any_key(key_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                key = api_keys.get(db, key_id)
                if key is not None:
                    blocked = _covers(db, ctx, key.principal_id, "api_key:revoke")
                    if blocked:
                        return blocked
                if not api_keys.revoke(db, actor=ctx, key_id=key_id, org_id=ctx.principal.org_id):
                    return error(404, "not_found")
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    return router
