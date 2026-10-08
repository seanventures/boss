"""/v1/identity/me*: the signed-in principal's own identity (IDENTITY.md §10 "Me"). Stream S1.

GET /v1/identity/me and /me/permissions are the console's frozen contract (contracts.py; TS mirror
apps/web/lib/identity/contract.ts). The rest is self-service: sessions, password, TOTP, passkeys,
recovery codes, linked identities and personal API keys.

Rules: the principal comes ONLY from the request's credential (never a body, query or header
field); every route acts on the caller alone; writes that remove a sign-in factor need fresh MFA
(step-up), and adding a factor needs a sign-in within the last 10 minutes. Secrets (TOTP secret,
recovery codes, a new key) are returned exactly once.

main.py includes each identity router itself (auth, people, SSO, SCIM, admin, agents, access).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import P, authenticated, permit
from boss_engine.identity import api_keys, recovery, sessions, signin, totp, users
from boss_engine.identity import webauthn as passkeys
from boss_engine.identity.audit import Outcome
from boss_engine.identity.authn import SESSION_COOKIE, STEP_UP_WINDOW_SECONDS, AuthnError
from boss_engine.identity.authn_support import emit, public_origin, utcnow
from boss_engine.identity.contracts import (
    MeBanner,
    MeOrg,
    MePermissionsResponse,
    MeResponse,
    MeSignInMethods,
)
from boss_engine.identity.passwords import (
    PasswordBusy,
    PasswordRejected,
    check_new_password,
    hash_password,
    verify_password,
)
from boss_engine.identity.permissions_gen import CATALOGUE_FINGERPRINT
from boss_engine.identity.principal import AuthContext, PrincipalKind

from .identity_auth import current_context, error, facts, refusal


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordChangeBody(_Body):
    current: Optional[str] = Field(default=None, max_length=1024)
    new: str = Field(max_length=1024)


class CodeBody(_Body):
    code: str = Field(max_length=64)


class PasskeyRegisterBody(_Body):
    credential: dict[str, Any]
    name: str = Field(default="Passkey", max_length=80)


class RenameBody(_Body):
    name: str = Field(max_length=80)


class ProfileBody(_Body):
    """What a person may change about themselves: the name others see and their avatar colour
    (one of the console's eight tones). An allowlist: anything else is a 422."""
    display_name: Optional[str] = Field(default=None, max_length=200)
    avatar_seed: Optional[str] = Field(default=None, pattern="^[0-7]$")


class KeyCreateBody(_Body):
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


def jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}


def _qr_svg(uri: str) -> Optional[str]:
    """The enrolment QR code, rendered here so the secret never goes to a third-party service."""
    try:
        import segno
    except ImportError:
        return None
    # A STANDALONE SVG, with its namespace. The console draws it through <img src="data:image/svg+xml…">,
    # and an SVG without xmlns="http://www.w3.org/2000/svg" (what svg_inline() writes, for pasting
    # into HTML) is not an image: boss-1 showed a broken one where the QR should be.
    import io

    out = io.BytesIO()
    segno.make_qr(uri, error="m").save(out, kind="svg", xmldecl=False, svgns=True, nl=False,
                                       scale=4, dark="#111", light="#fff")
    return out.getvalue().decode("utf-8")


def _epoch(db: Any, org_id: str) -> int:
    """S5's identity epoch (change_feed): the same number the push stream announces."""
    from boss_engine.identity import change_feed

    return change_feed.current_epoch(org_id, db=db)


def is_fresh(ctx: AuthContext) -> bool:
    s = ctx.session
    return bool(s and s.mfa_at and (utcnow() - s.mfa_at).total_seconds() <= STEP_UP_WINDOW_SECONDS)


def need_step_up(ctx: AuthContext) -> Optional[JSONResponse]:
    """Fresh MFA on a session. Bearer credentials can never step up (§4.6)."""
    if ctx.session is None or not is_fresh(ctx):
        return error(401, "step_up_required")
    return None


def need_person_session(ctx: AuthContext) -> Optional[JSONResponse]:
    """The handler's own gate (shadow mode, where the guard only records). Under enforce the guard
    refuses first, before the body is validated (declarations: people_only)."""
    from boss_engine.identity.enforcement import is_person_session

    if not is_person_session(ctx):
        return error(403, "people_only")
    return None


def need_own_account(db: Any, ctx: AuthContext) -> Optional[JSONResponse]:
    """A self-service change to sign-in factors needs an account of the caller's OWN. The shared
    console password's session (usr_legacy_shared) and the network bridge are shared principals
    with no password, factors or email of their own: refused with words, never a 500 (R2)."""
    if users.by_id(db, ctx.principal.id) is None:
        return JSONResponse(status_code=409, content={
            "error": "shared_account",
            "message": "This is the shared console sign-in: it has no password or second factor of its "
                       "own. Sign in with your own account (or claim one) to change them."})
    return None


def _first_factor_extras(db: Any, ctx: AuthContext, had_factor: bool) -> dict[str, Any]:
    """The first second factor a person adds comes with their recovery codes, shown once."""
    if had_factor:
        return {}
    codes = recovery.generate(db, ctx.principal.id)
    emit(db, org_id=ctx.principal.org_id, event_type="recovery_codes.generated",
         outcome=Outcome.SUCCESS, ctx=ctx, target_type="user", target_id=ctx.principal.id)
    return {"recovery_codes": codes}


def _rotate_fresh(db: Any, request: Request, ctx: AuthContext, method: str) -> list[str]:
    """Adding a factor proves it: rotate the session with fresh MFA (a NEW token)."""
    f = facts(request, db)
    row = sessions.lookup(db, f.session)
    if row is None:
        return []
    issued = sessions.issue(db, principal_id=ctx.principal.id, org_id=ctx.principal.org_id,
                            methods=[*row.auth_methods, method], mfa_at=utcnow(), ip=f.ip,
                            user_agent=f.user_agent, device_id_hash=row.device_id_hash,
                            replaces=row.id, absolute_expires_at=row.absolute_expires_at)
    emit(db, org_id=ctx.principal.org_id, event_type="session.rotated", outcome=Outcome.INFO, ctx=ctx,
         details={"replaced_by": issued.session_id, "reason": "factor_added"})
    max_age = int((issued.absolute_expires_at - utcnow()).total_seconds())
    return [sessions.set_cookie_header(SESSION_COOKIE, issued.token, max_age=max_age)]


def respond(body: dict[str, Any], cookies: list[str], status: int = 200) -> JSONResponse:
    response = JSONResponse(status_code=status, content=body, headers={"Cache-Control": "no-store"})
    for c in cookies:
        response.raw_headers.append((b"set-cookie", c.encode("latin-1")))
    return response


def _last_factor_guard(db: Any, ctx: AuthContext, removing: str) -> Optional[JSONResponse]:
    """An MFA-required person cannot remove their last second factor (§4.4)."""
    user = users.by_id(db, ctx.principal.id)
    if user is None:
        return error(404, "not_found")
    f = users.factors(db, ctx.principal.id)
    left = (0 if removing == "totp" else int(f.totp)) + (f.passkeys - (1 if removing == "passkey" else 0))
    if users.mfa_required(db, user) and left <= 0:
        return error(409, "last_factor")
    return None


def build_me_response(db: Any, ctx: AuthContext) -> MeResponse:
    from boss_engine.identity.sessions import csrf_token

    p = ctx.principal
    org_name = db.execute(text("SELECT name FROM orgs WHERE id = :o"), {"o": p.org_id}).scalar() or p.org_id
    user = users.by_id(db, p.id) if p.kind == PrincipalKind.USER else None
    f = users.factors(db, p.id)
    links = [r[0] for r in db.execute(text(
        "SELECT DISTINCT coalesce(pr.name, i.provider_id) FROM identity_user_identities i LEFT JOIN "
        "identity_providers pr ON pr.id = i.provider_id WHERE i.principal_id = :p"), {"p": p.id})]
    required = bool(user and users.mfa_required(db, user))
    banners: list[MeBanner] = []
    if required and not f.any_second_factor:
        banners.append(MeBanner(kind="mfa_enrolment_required",
                                message="Your access needs a second factor. Add a passkey or an authenticator app."))
    from boss_engine.identity.authn_support import smtp_configured
    from boss_engine.identity.where import sign_in_origin

    if user is not None and not smtp_configured():
        # Without SMTP, a sign-in from a new device is told here (with SMTP it was emailed).
        seen = db.execute(text(
            "SELECT ts, ip, user_agent FROM identity_security_events WHERE org_id = :o AND "
            "event_type = 'auth.new_device' AND actor_principal_id = :p AND ts > now() - interval '7 days' "
            "AND (CAST(:s AS TEXT) IS NULL OR session_ref IS DISTINCT FROM :s) ORDER BY ts DESC LIMIT 1"),
            {"o": p.org_id, "p": p.id, "s": ctx.session.id if ctx.session else None}).mappings().first()
        if seen:
            banners.append(MeBanner(kind="new_device", message=(
                f"New sign-in {sign_in_origin(seen['ip'])} on "
                f"{seen['ts'].strftime('%Y-%m-%d %H:%M UTC')}. Not you? Sign out everywhere in "
                "Sessions and change your password.")))
    # The legacy countdowns, the network bridge, low recovery codes and an Owner recovered from
    # the host shell: posture owns every other console-wide banner (one source, IDENTITY.md §12).
    from boss_engine.identity import posture

    banners.extend(MeBanner(**b) for b in posture.banners(ctx, db=db))
    s = ctx.session
    return MeResponse(
        principal_id=p.id, kind=p.kind.value, display_name=p.display_name,
        email=user.email if user else None, org=MeOrg(id=p.org_id, name=str(org_name)),
        status=p.status.value, access_roles=users.role_keys(db, p.id, org_scope_only=True),
        sign_in=MeSignInMethods(password=bool(user and user.password_hash), totp=f.totp,
                                passkeys=f.passkeys, recovery_codes_remaining=f.recovery_codes_remaining,
                                sso_links=sorted(links)),
        mfa_required=required, mfa_at=s.mfa_at if s else None, session_id=s.id if s else None,
        session_expires_at=s.absolute_expires_at if s else None,
        csrf_token=csrf_token(s.id) if s else "", epoch=_epoch(db, p.org_id),
        network_bound=p.network_bound, banners=banners, avatar_seed=user.avatar_seed if user else None)




def cross_context(headers: Any) -> bool:
    """A browser request from a sandboxed (opaque-origin) document or a cross-site fetch. Such a
    caller — a previewed app's script on the console's origin — must never be handed the CSRF
    token, even if a cookie reached us: the token is only for the console's own pages."""
    if (headers.get("origin") or "").strip().lower() == "null":
        return True
    return (headers.get("sec-fetch-site") or "").lower() == "cross-site" and \
        (headers.get("sec-fetch-mode") or "").lower() != "navigate"


def build_identity_me_router() -> APIRouter:
    router = APIRouter(tags=["identity"])

    @router.get("/v1/identity/me", response_model=MeResponse)
    @authenticated(self_only=True)
    def get_me(request: Request):
        try:
            with begin() as db:
                me = build_me_response(db, current_context(request, db))
            body = me.model_dump(mode="json")
            if cross_context(request.headers):
                body["csrf_token"] = ""
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/me/permissions", response_model=MePermissionsResponse)
    @authenticated(self_only=True)
    def get_my_permissions(request: Request, project_id: Optional[str] = None):
        from boss_engine.identity import authz
        from boss_engine.identity.scope import ResolvedScope

        try:
            with begin() as db:
                ctx = current_context(request, db)
                org = ctx.principal.org_id
                if project_id:
                    found = db.execute(text(
                        "SELECT 1 FROM projects pr JOIN workspaces w ON w.id = pr.workspace_id "
                        "WHERE pr.id = :p AND w.org_id = :o"), {"p": project_id, "o": org}).first()
                    if found is None:
                        return error(404, "not_found")
                    scope = ResolvedScope.in_project(org, project_id)
                else:
                    scope = ResolvedScope.org_level(org)
                eff = authz.effective_permissions(ctx, scope, db=db)
                if project_id and "project:read" not in eff.permissions:
                    return error(404, "not_found")     # an invisible project: no existence oracle
                fresh_until = (sessions.step_up_fresh_until(ctx.session, window_seconds=STEP_UP_WINDOW_SECONDS)
                               if ctx.session else None)
                body = MePermissionsResponse(
                    epoch=eff.epoch, scope=scope.label(), permissions=sorted(eff.permissions),
                    step_up_fresh_until=fresh_until, catalogue_fingerprint=CATALOGUE_FINGERPRINT)
            return JSONResponse(body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/me/projects")
    @authenticated(self_only=True)
    def get_my_projects(request: Request):
        """The projects where the signed-in principal holds access through a role binding — its
        own or a group's, at that project or org-wide — each with the permissions it holds THERE
        (the PDP's effective permissions, A3 applied). One read, so a page never probes projects
        one by one to find where the person may act."""
        from boss_engine.identity import authz
        from boss_engine.identity.scope import ResolvedScope

        try:
            with begin() as db:
                ctx = current_context(request, db)
                org = ctx.principal.org_id
                bound = db.execute(text(
                    "SELECT DISTINCT b.scope_type, b.scope_id FROM identity_role_bindings b "
                    "WHERE b.org_id = :o AND (b.expires_at IS NULL OR b.expires_at > now()) AND ("
                    "(b.subject_type = 'principal' AND b.subject_id = :p) OR (b.subject_type = 'group' "
                    "AND b.subject_id IN (SELECT m.group_id FROM identity_group_members m "
                    "WHERE m.principal_id = :p)))"), {"o": org, "p": ctx.principal.id}).all()
                org_wide = any(r[0] == "org" for r in bound)
                at_project = {r[1] for r in bound if r[0] == "project"}
                rows = db.execute(text(
                    "SELECT pr.id, pr.name FROM projects pr JOIN workspaces w ON w.id = pr.workspace_id "
                    "WHERE w.org_id = :o ORDER BY pr.name, pr.id"), {"o": org}).all()
                items = []
                for pid, name in rows:
                    if not org_wide and pid not in at_project:
                        continue
                    perms = authz.effective_permissions(ctx, ResolvedScope.in_project(org, pid),
                                                        db=db).permissions
                    if not perms:
                        continue
                    items.append({"project_id": pid, "name": name, "permissions": sorted(perms),
                                  "via": sorted({"org"} if org_wide else set())
                                  + (["project"] if pid in at_project else [])})
            return JSONResponse({"projects": items}, headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    # ---- profile: your own name and avatar colour -----------------------------------------------------

    def _profile(db: Any, principal_id: str) -> dict[str, Any]:
        """Always the full shape. A person principal with no account row (the shared console
        password's usr_legacy_shared) has a name and nothing else — `{}` crashed My account."""
        row = db.execute(text(
            "SELECT p.display_name, u.email, u.avatar_seed FROM identity_principals p "
            "LEFT JOIN identity_users u ON u.principal_id = p.id WHERE p.id = :id"), {"id": principal_id}).first()
        if row is None:
            return {"display_name": "", "email": None, "avatar_seed": None}
        return {"display_name": row.display_name or "", "email": row.email, "avatar_seed": row.avatar_seed}

    @router.get("/v1/identity/me/profile")
    @authenticated(self_only=True, people_only=True)
    def my_profile(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx)
                if blocked:
                    return blocked
                return JSONResponse(_profile(db, ctx.principal.id), headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.patch("/v1/identity/me")
    @authenticated(self_only=True, people_only=True)
    def update_me(body: ProfileBody, request: Request):
        """Rename yourself, or pick your avatar colour. Only the caller, only these two fields;
        your email and access are an admin's (PATCH /v1/identity/users/{id})."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx)
                if blocked:
                    return blocked
                fields = body.model_dump(exclude_unset=True)
                if "display_name" in fields:
                    name = (fields["display_name"] or "").strip()[:200]
                    if not name:
                        return error(422, "name_required")
                    db.execute(text("UPDATE identity_principals SET display_name = :n, updated_at = now() "
                                    "WHERE id = :id"), {"n": name, "id": ctx.principal.id})
                if "avatar_seed" in fields:
                    db.execute(text("UPDATE identity_users SET avatar_seed = :s WHERE principal_id = :id"),
                               {"s": fields["avatar_seed"], "id": ctx.principal.id})
                if fields:
                    emit(db, org_id=ctx.principal.org_id, event_type="user.updated", outcome=Outcome.SUCCESS,
                         ctx=ctx, target_type="user", target_id=ctx.principal.id,
                         details={"fields": sorted(fields), "self": True})
                return JSONResponse(_profile(db, ctx.principal.id), headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    # ---- sessions -----------------------------------------------------------------------------------

    @router.get("/v1/identity/me/sessions")
    @authenticated(self_only=True)
    def my_sessions(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                current = ctx.session.id if ctx.session else None
                rows = sessions.list_live(db, ctx.principal.id)
                return JSONResponse({"sessions": [
                    {"id": r.id, "current": r.id == current, "methods": list(r.auth_methods),
                     "created_at": r.created_at.isoformat(), "last_seen_at": r.last_seen_at.isoformat(),
                     "ip": r.ip, "user_agent": r.user_agent,
                     "expires_at": r.absolute_expires_at.isoformat()} for r in rows]},
                    headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/me/sessions/{session_id}")
    @authenticated(self_only=True)
    def end_my_session(session_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                row = sessions.by_id(db, session_id)
                if row is None or row.principal_id != ctx.principal.id:
                    return error(404, "not_found")
                if sessions.revoke(db, session_id, by=ctx.principal.id, reason="ended by owner"):
                    emit(db, org_id=ctx.principal.org_id, event_type="session.revoked",
                         outcome=Outcome.SUCCESS, ctx=ctx, target_type="session", target_id=session_id)
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/me/sessions")
    @authenticated(self_only=True)
    def end_my_other_sessions(request: Request):
        """Sign out everywhere else: every live session but this one."""
        try:
            with begin() as db:
                ctx = current_context(request, db)
                ended = sessions.revoke_all(db, ctx.principal.id, by=ctx.principal.id, reason="ended by owner",
                                            except_session=ctx.session.id if ctx.session else None)
                if ended:
                    emit(db, org_id=ctx.principal.org_id, event_type="session.revoked", outcome=Outcome.SUCCESS,
                         ctx=ctx, target_type="user", target_id=ctx.principal.id, details={"count": ended})
                return JSONResponse({"revoked": ended})
        except AuthnError as exc:
            return refusal(exc)

    @router.get("/v1/identity/me/factors")
    @authenticated(self_only=True)
    def my_factors(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse({"factors": users.factor_list(db, ctx.principal.id)})
        except AuthnError as exc:
            return refusal(exc)

    # ---- password -----------------------------------------------------------------------------------

    @router.post("/v1/identity/me/password")
    @authenticated(self_only=True, people_only=True)
    def change_password(body: PasswordChangeBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_own_account(db, ctx)
                if blocked:
                    return blocked
                user = users.by_id(db, ctx.principal.id)
                if user.password_hash:
                    if not verify_password(body.current or "", user.password_hash):
                        return error(401, "invalid_credentials")
                elif not is_fresh(ctx):
                    return error(401, "step_up_required")   # setting a first password
                try:
                    check_new_password(body.new, email=user.email,
                                       policy=users.get_policy(user.org_id, db=db))
                except PasswordRejected as exc:
                    return error(422, exc.code)
                users.set_password_hash(db, user.principal_id, hash_password(body.new))
                recovery.void_reset_tokens(db, user.principal_id)
                ended = sessions.revoke_all(db, user.principal_id, by=user.principal_id,
                                            reason="password changed",
                                            except_session=ctx.session.id if ctx.session else None)
                emit(db, org_id=user.org_id, event_type="password.changed", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=user.principal_id,
                     details={"other_sessions_revoked": ended})
                signin.notify(user.email, "Your BOSS password changed",
                              "Your BOSS password was changed and your other sessions were signed "
                              "out. Review your API keys under My account.")
                return JSONResponse({"changed": True, "other_sessions_revoked": ended,
                                     "review_api_keys": True})
        except AuthnError as exc:
            return refusal(exc)
        except PasswordBusy:
            return JSONResponse(status_code=429, content={"error": "busy"}, headers={"Retry-After": "2"})

    # ---- TOTP --------------------------------------------------------------------------------------

    @router.post("/v1/identity/me/mfa/totp")
    @authenticated(self_only=True, people_only=True)
    def totp_begin(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_own_account(db, ctx)
                if blocked:
                    return blocked
                token = request.cookies.get(SESSION_COOKIE) or ""
                row = sessions.lookup(db, token)
                user = users.by_id(db, ctx.principal.id)
                if row is None or user is None:
                    return error(401, "step_up_required")
                refused = signin.may_enrol(db, row, user)
                if refused:
                    return error(*refused)
                secret, uri = totp.begin_enrolment(db, principal_id=user.principal_id, account=user.email,
                                                   session_token=token)
                return JSONResponse({"secret": secret, "otpauth_uri": uri, "qr_svg": _qr_svg(uri)},
                                    headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/mfa/totp/confirm")
    @authenticated(self_only=True, people_only=True)
    def totp_confirm(body: CodeBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx)
                if blocked:
                    return blocked
                had = users.factors(db, ctx.principal.id).any_second_factor
                token = request.cookies.get(SESSION_COOKIE) or ""
                if not totp.has_pending_enrolment(db, principal=ctx.principal, session_token=token):
                    # Not a wrong code: there is no key to check it against. Said as such, so the
                    # console starts a fresh enrolment instead of "That code didn't work" forever.
                    return error(409, "enrolment_expired")
                if not totp.confirm_enrolment(db, principal=ctx.principal, session_token=token,
                                              code=body.code):
                    return error(400, "invalid_code")
                emit(db, org_id=ctx.principal.org_id, event_type="mfa.enrolled", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=ctx.principal.id, details={"method": "totp"})
                extras = _first_factor_extras(db, ctx, had)
                cookies = _rotate_fresh(db, request, ctx, "totp")
                return respond({"enrolled": True, **extras}, cookies)
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/me/mfa/totp")
    @authenticated(self_only=True, people_only=True)
    def totp_remove(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_step_up(ctx) or _last_factor_guard(db, ctx, "totp")
                if blocked:
                    return blocked
                if not totp.remove(db, ctx.principal.id):
                    return error(404, "not_found")
                emit(db, org_id=ctx.principal.org_id, event_type="mfa.removed", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="user", target_id=ctx.principal.id, details={"method": "totp"})
                return JSONResponse({"removed": True})
        except AuthnError as exc:
            return refusal(exc)

    # ---- passkeys ------------------------------------------------------------------------------------

    @router.get("/v1/identity/me/passkeys")
    @authenticated(self_only=True)
    def my_passkeys(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                return JSONResponse({"passkeys": [jsonable(i) for i in passkeys.list_passkeys(db, ctx.principal.id)]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/passkeys/options")
    @authenticated(self_only=True, people_only=True)
    def passkey_registration_options(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_own_account(db, ctx)
                if blocked:
                    return blocked
                token = request.cookies.get(SESSION_COOKIE) or ""
                row = sessions.lookup(db, token)
                user = users.by_id(db, ctx.principal.id)
                if row is None or user is None:
                    return error(401, "step_up_required")
                refused = signin.may_enrol(db, row, user)
                if refused:
                    return error(*refused)
                try:
                    options = passkeys.registration_options(
                        db, principal_id=user.principal_id, email=user.email,
                        display_name=ctx.principal.display_name, origin=public_origin(db),
                        binding_secret=token)
                except passkeys.PasskeyError as exc:
                    return error(409 if exc.code in ("unavailable", "limit_reached", "needs_domain", "needs_https") else 400,
                                 f"passkey_{exc.code}")
                return JSONResponse({"publicKey": options}, headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/passkeys")
    @authenticated(self_only=True, people_only=True)
    def passkey_register(body: PasskeyRegisterBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx)
                if blocked:
                    return blocked
                had = users.factors(db, ctx.principal.id).any_second_factor
                try:
                    pid = passkeys.verify_registration(
                        db, principal_id=ctx.principal.id, origin=public_origin(db),
                        binding_secret=request.cookies.get(SESSION_COOKIE) or "",
                        credential=body.credential, name=body.name)
                except passkeys.PasskeyError as exc:
                    return error(400, f"passkey_{exc.code}")
                emit(db, org_id=ctx.principal.org_id, event_type="passkey.added", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="passkey", target_id=pid)
                extras = _first_factor_extras(db, ctx, had)
                cookies = _rotate_fresh(db, request, ctx, "passkey")
                return respond({"id": pid, **extras}, cookies, status=201)
        except AuthnError as exc:
            return refusal(exc)

    @router.patch("/v1/identity/me/passkeys/{passkey_id}")
    @authenticated(self_only=True)
    def passkey_rename(passkey_id: str, body: RenameBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not passkeys.rename(db, ctx.principal.id, passkey_id, body.name):
                    return error(404, "not_found")
                return JSONResponse({"renamed": True})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/me/passkeys/{passkey_id}")
    @authenticated(self_only=True, people_only=True)
    def passkey_remove(passkey_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = (need_person_session(ctx) or need_step_up(ctx)
                           or _last_factor_guard(db, ctx, "passkey"))
                if blocked:
                    return blocked
                if not passkeys.delete(db, ctx.principal.id, passkey_id):
                    return error(404, "not_found")
                emit(db, org_id=ctx.principal.org_id, event_type="passkey.removed", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="passkey", target_id=passkey_id)
                return JSONResponse({"removed": True})
        except AuthnError as exc:
            return refusal(exc)

    # ---- recovery codes ---------------------------------------------------------------------------------

    @router.post("/v1/identity/me/recovery-codes")
    @authenticated(self_only=True, people_only=True)
    def regenerate_recovery_codes(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_step_up(ctx)
                if blocked:
                    return blocked
                if not users.factors(db, ctx.principal.id).any_second_factor:
                    return error(409, "no_second_factor")
                codes = recovery.generate(db, ctx.principal.id)
                emit(db, org_id=ctx.principal.org_id, event_type="recovery_codes.generated",
                     outcome=Outcome.SUCCESS, ctx=ctx, target_type="user", target_id=ctx.principal.id)
                return JSONResponse({"recovery_codes": codes}, headers={"Cache-Control": "no-store"})
        except AuthnError as exc:
            return refusal(exc)

    # ---- linked identities (linking starts at /v1/auth/sso/{provider_id}/start?purpose=link) ----------

    @router.get("/v1/identity/me/identities")
    @authenticated(self_only=True)
    def my_identities(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                rows = db.execute(text(
                    "SELECT i.id, coalesce(p.name, i.provider_id) AS provider, coalesce(p.preset, 'generic') AS preset, "
                    "i.issuer, i.linked_at FROM identity_user_identities i LEFT JOIN identity_providers p ON "
                    "p.id = i.provider_id WHERE i.principal_id = :p ORDER BY i.linked_at"),
                    {"p": ctx.principal.id})
                # LinkedIdentity: the provider and its host, never the subject or the email at link time.
                return JSONResponse({"identities": [
                    {"id": r.id, "provider": r.provider, "preset": r.preset,
                     "label": (r.issuer or "").split("://", 1)[-1].split("/", 1)[0],
                     "linked_at": r.linked_at.isoformat()} for r in rows]})
        except AuthnError as exc:
            return refusal(exc)

    @router.delete("/v1/identity/me/identities/{identity_id}")
    @authenticated(self_only=True, people_only=True)
    def unlink_identity(identity_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_step_up(ctx)
                if blocked:
                    return blocked
                gone = db.execute(text("DELETE FROM identity_user_identities WHERE id = :id AND principal_id = :p "
                                       "RETURNING provider_id"), {"id": identity_id, "p": ctx.principal.id}).first()
                if gone is None:
                    return error(404, "not_found")
                emit(db, org_id=ctx.principal.org_id, event_type="sso.unlink", outcome=Outcome.SUCCESS,
                     ctx=ctx, target_type="identity", target_id=identity_id,
                     details={"provider_id": gone[0]})
                return JSONResponse({"unlinked": True})
        except AuthnError as exc:
            return refusal(exc)

    # ---- personal API keys ------------------------------------------------------------------------------

    @router.get("/v1/identity/me/api-keys")
    @authenticated(self_only=True)
    def my_keys(request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                rows = api_keys.list_keys(db, principal_id=ctx.principal.id, kinds=("pat",))
                return JSONResponse({"keys": [api_keys.view(db, r) for r in rows]})
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/api-keys")
    @permit(P.API_KEY_MANAGE_OWN)
    def create_my_key(body: KeyCreateBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_step_up(ctx)
                if blocked:
                    return blocked
                if body.replaces:
                    if not body.grace():
                        return error(422, "invalid_overlap")
                    created = api_keys.rotate(db, actor=ctx, key_id=body.replaces, owner=ctx.principal,
                                              grace=body.grace())
                else:
                    created = api_keys.create(db, creator=ctx, owner=ctx.principal, kind="pat", name=body.name,
                                              scopes=body.scopes, **body.resolved())
                return JSONResponse(status_code=201, content=api_keys.issued(db, created, replaced=body.replaces),
                                    headers={"Cache-Control": "no-store"})
        except api_keys.KeyRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/api-keys/{key_id}/rotate")
    @permit(P.API_KEY_MANAGE_OWN)
    def rotate_my_key(key_id: str, body: RotateBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                blocked = need_person_session(ctx) or need_step_up(ctx)
                if blocked:
                    return blocked
                created = api_keys.rotate(db, actor=ctx, key_id=key_id, owner=ctx.principal, grace=body.grace)
                return JSONResponse(status_code=201, content=api_keys.issued(db, created, replaced=key_id),
                                    headers={"Cache-Control": "no-store"})
        except api_keys.KeyRefused as exc:
            return error(exc.status, exc.code, **exc.detail)
        except AuthnError as exc:
            return refusal(exc)

    @router.post("/v1/identity/me/api-keys/{key_id}/revoke")
    @permit(P.API_KEY_MANAGE_OWN)
    def revoke_my_key(key_id: str, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if not api_keys.revoke(db, actor=ctx, key_id=key_id, org_id=ctx.principal.org_id,
                                       owner_id=ctx.principal.id):
                    return error(404, "not_found")
                return JSONResponse({"revoked": True})
        except AuthnError as exc:
            return refusal(exc)

    return router
