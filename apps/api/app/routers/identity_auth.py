"""/v1/auth/*: sign-in, MFA, passkeys, email sign-in, password reset, sign-up, claim, invitations,
step-up and sign-out (IDENTITY.md §4, §10). Stream S1.

Every route here is `@public(reason=AUTH_ENDPOINT, verifier=...)`: it authenticates by what it is
(a password, a code, a challenge bound to this browser) and is rate-limited and CSRF-checked
(Origin + Sec-Fetch-Site; the HMAC header needs a session, which these routes create). Bodies of
these routes are never logged (§8.3), and no response ever carries a token, code or link (A15).

Also the shared request helpers the identity routers use: `credentials`, `facts`, `current_context`
and `render`.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from boss_engine.db import begin
from boss_engine.identity import PublicReason, authenticated, public
from boss_engine.identity import authn, claim as claim_module, email_signin, invitations, signin, users
from boss_engine.identity.authn import AuthnError, RequestCredentials
from boss_engine.identity.authn_support import origin_of, public_origin
from boss_engine.identity.passwords import PasswordBusy, PasswordRejected, check_new_password, hash_password
from boss_engine.identity.principal import AuthContext
from boss_engine.identity import webauthn as passkeys_module
from boss_engine.identity.signin import Answer, RequestFacts


# ---- request helpers (shared by the identity routers) -------------------------------------------------

def credentials(request: Request) -> RequestCredentials:
    """The same framework-free view the enforcement point builds (identity_guard._credentials)."""
    client = request.client.host if request.client else None
    from apps.api.app import identity_guard

    return RequestCredentials(
        method=request.method, path=request.url.path,
        headers={k.lower(): v for k, v in request.headers.items()},
        cookies=dict(request.cookies), client_ip=client, peer_ip=client,
        request_id=request.headers.get("x-request-id"),
        legacy_member_id=identity_guard._legacy_member(),
        legacy_auth_method=identity_guard._legacy_method())


def facts(request: Request, db: Any) -> RequestFacts:
    creds = credentials(request)
    return RequestFacts(
        ip=authn.client_ip_of(creds), user_agent=(request.headers.get("user-agent") or "")[:400] or None,
        request_id=request.headers.get("x-request-id"),
        preauth=request.cookies.get(authn.PREAUTH_COOKIE),
        session=request.cookies.get(authn.SESSION_COOKIE),
        device=request.cookies.get(authn.DEVICE_COOKIE),
        origin=public_origin(db), request_origin=authn.request_origin(creds))


def render(answer: Answer) -> JSONResponse:
    response = JSONResponse(status_code=answer.status, content=answer.body,
                            headers={"Cache-Control": "no-store", **answer.headers})
    for cookie in answer.cookies:
        response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return response


def error(status: int, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, **extra},
                        headers={"Cache-Control": "no-store"})


def current_context(request: Request, db: Any = None) -> AuthContext:
    """The request's AuthContext (authenticated again from its one credential; cached on the
    request). Raises AuthnError — handlers render it with `refusal`."""
    from .. import identity_guard

    cached = getattr(request.state, "identity", None)
    if cached is not None:
        return cached
    if getattr(request.state, "identity_checked", False) or getattr(request.state, "identity_error", None):
        ctx = identity_guard.request_auth(request)   # re-raises the guard's refusal, never re-checks
    else:
        ctx = authn.authenticate(credentials(request), db=db)
        request.state.identity_checked = True
        if ctx is not None:
            request.state.identity = ctx
            request.state.identity_ctx = ctx
    if ctx is None:
        raise AuthnError("unauthenticated")
    return ctx


def refusal(exc: AuthnError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse(status_code=exc.status, content={"error": exc.code}, headers=headers)


def origin_guard(request: Request, db: Any) -> Optional[JSONResponse]:
    """The pre-session CSRF check for every /v1/auth/* write (A8)."""
    try:
        authn.check_origin_only(credentials(request), public_origin=public_origin(db))
    except AuthnError as exc:
        return error(exc.status, exc.code)
    return None


def _run(request: Request, flow) -> JSONResponse:
    from boss_engine.identity.passwords import hashing_for

    try:
        with begin() as db:
            blocked = origin_guard(request, db)
            if blocked is not None:
                return blocked
            f = facts(request, db)
            with hashing_for(f.ip):     # L2: this address's share of the argon2 queue
                return render(flow(db, f))
    except PasswordBusy:
        return JSONResponse(status_code=429, content={"error": "busy"}, headers={"Retry-After": "2"})


def verify_auth_request(request: Request) -> None:
    """The verifier named by every @public(AUTH_ENDPOINT) route: the Origin check above, then the
    route's own proof (password, code, browser-bound challenge)."""
    with begin() as db:
        authn.check_origin_only(credentials(request), public_origin=public_origin(db))


# ---- bodies (explicit allowlists: an unknown field is a 422, never ignored) ------------------------

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    #: blank: the shared console password (Operator for 30 days after the claim)
    email: str = Field(default="", max_length=320)
    password: str = Field(max_length=1024)


class CodeBody(_Body):
    code: str = Field(max_length=64)


class PasskeyOptionsBody(_Body):
    purpose: str = Field(pattern="^(login|mfa|step_up)$")


class PasskeyVerifyBody(_Body):
    purpose: str = Field(pattern="^(login|mfa|step_up)$")
    credential: dict[str, Any]


class EmailBody(_Body):
    email: str = Field(max_length=320)


class TokenBody(_Body):
    token: str = Field(max_length=200)


class ResetBody(_Body):
    token: str = Field(max_length=200)
    new_password: str = Field(max_length=1024)


class SignupBody(_Body):
    name: str = Field(default="", max_length=200)
    email: str = Field(max_length=320)


class SignupVerifyBody(_Body):
    token: str = Field(max_length=200)
    password: Optional[str] = Field(default=None, max_length=1024)


class ClaimBody(_Body):
    name: str = Field(max_length=200)
    email: str = Field(max_length=320)
    password: Optional[str] = Field(default=None, max_length=1024)
    setup_token: Optional[str] = Field(default=None, max_length=200)
    legacy_password: Optional[str] = Field(default=None, max_length=1024)
    confirm_origin: Optional[str] = Field(default=None, max_length=300)
    #: The console's name for confirm_origin (the origin shown on screen and confirmed).
    origin: Optional[str] = Field(default=None, max_length=300)
    #: Optional legacy approval seat (ceo, cao, …) the first Owner takes over, so history maps.
    seat: Optional[str] = Field(default=None, max_length=64)


class AcceptBody(_Body):
    token: str = Field(max_length=200)
    name: str = Field(default="", max_length=200)
    password: str = Field(max_length=1024)


class StepUpBody(_Body):
    method: str = Field(pattern="^(totp|password)$")
    code: Optional[str] = Field(default=None, max_length=64)
    password: Optional[str] = Field(default=None, max_length=1024)


AUTH = PublicReason.AUTH_ENDPOINT


def _browser_origin(request: Request) -> Optional[str]:
    """The origin the browser is on: its Origin header, else X-Forwarded-Host/Proto from a trusted
    proxy (the console). Never the Host header a server-side fetch carries."""
    from boss_engine.identity.authn_support import is_trusted_proxy

    said = origin_of(request.headers.get("origin", ""))
    if said:
        return said
    creds = credentials(request)
    host = request.headers.get("x-forwarded-host")
    if host and is_trusted_proxy(creds.peer_ip or creds.client_ip):
        proto = (request.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()
        return origin_of(f"{proto}://{host.split(',')[0].strip()}")
    return None


def build_identity_auth_router() -> APIRouter:
    router = APIRouter(tags=["identity-auth"])

    @router.get("/v1/auth/options")
    @public(reason=AUTH, verifier=verify_auth_request)
    def sign_in_options(request: Request):
        """What the sign-in page shows (AuthOptions, D33): the configured providers, the email tabs
        only with SMTP, the sign-up footer only when enabled, and — while unclaimed — the claim
        path this browser can use. Says nothing about any account."""
        from sqlalchemy import text

        bridged = _bridged(request)
        with begin() as db:
            org = claim_module.default_org_id(db)
            providers = [] if not org else [
                {"id": r.id, "name": r.name, "preset": r.preset}
                for r in db.execute(text("SELECT id, name, preset FROM identity_providers "
                                         "WHERE org_id = :o AND status = 'active' ORDER BY name"), {"o": org})]
            pol = users.get_policy(org, db=db) if org else None
            smtp = bool(email_signin.available() and pol and (pol.email_magic_link or pol.email_code)
                        and public_origin(db))
            org_name = (db.execute(text("SELECT name FROM orgs WHERE id = :o"), {"o": org}).scalar()
                        if org else None) or "BOSS"
            origin = public_origin(db) or authn.request_origin(credentials(request)) or ""
            claim_info = None
            if not claim_module.is_claimed(db):
                # read-only (R5): the API's lifespan keeps a live claim link printed
                paths = claim_module.available_paths(db, bridged)
                path = ("network_bridge" if "network" in paths
                        else "legacy_password" if "legacy_password" in paths else "setup_token")
                claim_info = {
                    "path": path,
                    # every way this browser may claim: a page holding a setup link (`#t=`, which
                    # never reaches the API) uses setup_token even where a shared password is set
                    # (compose always sets BOSS_CONSOLE_PASSWORD, R2 #10)
                    "paths": paths,
                    # the origin the claim will bind: the BROWSER's (its Origin, or the host a
                    # trusted proxy forwarded) — never the Host a server-side fetch addressed
                    # (http://api:8000 on compose, R2 #10). None: the page shows its own origin.
                    "origin": _browser_origin(request) if path == "network_bridge" else None,
                    "expires_at": claim_module.setup_expires_at(db) if "setup_token" in paths else None,
                    "seats": claim_module.legacy_seats(db),
                }
            from boss_engine.config import get_settings

            bound = public_origin(db)
            judged = bound or (claim_info or {}).get("origin") or _browser_origin(request) or origin
            blocker = passkeys_module.passkey_blocker(judged)
            passkey_reason = (f"passkey_{blocker}" if blocker and blocker != "unavailable"
                              else None if bound else "public_origin_unset")
            return JSONResponse({
                "org_name": org_name, "host": (origin.split("://", 1)[-1] if origin else ""),
                # H2: the bound origin links, passkeys and SSO use (None: none is bound, so BOSS
                # sends no emailed links) and where it came from.
                "public_origin": bound,
                "public_origin_source": ("env" if origin_of(get_settings().public_url or "")
                                         else "claim" if bound else None),
                "providers": providers, "smtp": smtp, "signup": email_signin.signup_enabled(db),
                "legacy_password": bool(claim_module.legacy_password_configured()),
                "shared_password": _shared_password_offer(db, org) if claim_info is None else None,
                # Passkeys only where a browser can make one: a bound origin whose host is a domain
                # on a secure context. `passkey_reason` says why not — judged on the bound origin, or
                # (unclaimed) the one this browser is on, which the claim would bind — e.g.
                # passkey_needs_domain for https://100.93.58.20 (webauthn.passkey_blocker).
                "passkey": bool(bound) and passkey_reason is None,
                "passkey_reason": passkey_reason,
                "claim": claim_info,
                # The policy's password minimum, so every "choose a password" field says it.
                "password_min_length": pol.password_min_length if pol else 12,
            }, headers={"Cache-Control": "no-store"})

    @router.post("/v1/auth/invitations/preview")
    @public(reason=AUTH, verifier=verify_auth_request)
    def invitation_preview(body: TokenBody, request: Request):
        """InvitationPreview for the accept page. The token travels in the BODY (never a URL path,
        which access logs keep). 404 for a used, revoked, expired or unknown token."""
        from sqlalchemy import text

        with begin() as db:
            blocked = origin_guard(request, db)
            if blocked is not None:
                return blocked
            inv = invitations.open_by_token(db, body.token)
            if inv is None:
                return error(404, "not_found")
            org_name = db.execute(text("SELECT name FROM orgs WHERE id = :o"), {"o": inv.org_id}).scalar()
            inviter = users.load_principal(db, inv.invited_by)
            roles = [b["role_key"].capitalize() + ("" if b["scope_type"] == "org" else f" · {b['scope_id']}")
                     for b in inv.bindings]
            sso = [{"id": r.id, "name": r.name, "preset": r.preset} for r in db.execute(text(
                "SELECT id, name, preset FROM identity_providers WHERE org_id = :o AND status = 'active' "
                "ORDER BY name"), {"o": inv.org_id})]
            return JSONResponse({"org": org_name or inv.org_id,
                                 "inviter": inviter.display_name if inviter else "",
                                 "email": inv.email, "roles": roles,
                                 "expires_at": inv.expires_at.isoformat(), "sso": sso},
                                headers={"Cache-Control": "no-store"})

    @router.post("/v1/auth/login")
    @public(reason=AUTH, verifier=verify_auth_request)
    def login(body: LoginBody, request: Request):
        return _run(request, lambda db, f: signin.password_login(db, f, email=body.email,
                                                                 password=body.password))

    @router.post("/v1/auth/mfa/totp")
    @public(reason=AUTH, verifier=verify_auth_request)
    def mfa_totp(body: CodeBody, request: Request):
        return _run(request, lambda db, f: signin.mfa_totp(db, f, code=body.code))

    @router.post("/v1/auth/mfa/recovery")
    @public(reason=AUTH, verifier=verify_auth_request)
    def mfa_recovery(body: CodeBody, request: Request):
        return _run(request, lambda db, f: signin.mfa_recovery(db, f, code=body.code))

    @router.post("/v1/auth/webauthn/options")
    @public(reason=AUTH, verifier=verify_auth_request)
    def webauthn_options(body: PasskeyOptionsBody, request: Request):
        return _run(request, lambda db, f: signin.passkey_options(db, f, purpose=body.purpose))

    @router.post("/v1/auth/webauthn/verify")
    @public(reason=AUTH, verifier=verify_auth_request)
    def webauthn_verify(body: PasskeyVerifyBody, request: Request):
        return _run(request, lambda db, f: signin.passkey_verify(db, f, credential=body.credential,
                                                                 purpose=body.purpose))

    @router.post("/v1/auth/password/forgot")
    @public(reason=AUTH, verifier=verify_auth_request)
    def password_forgot(body: EmailBody, request: Request):
        return _run(request, lambda db, f: email_signin.forgot_password(db, f, email=body.email))

    @router.post("/v1/auth/password/reset")
    @public(reason=AUTH, verifier=verify_auth_request)
    def password_reset(body: ResetBody, request: Request):
        return _run(request, lambda db, f: email_signin.reset_password(
            db, f, token=body.token, new_password=body.new_password))

    @router.post("/v1/auth/magic-link")
    @public(reason=AUTH, verifier=verify_auth_request)
    def magic_link(body: EmailBody, request: Request):
        return _run(request, lambda db, f: email_signin.request_sign_in(db, f, kind="magic_link",
                                                                        email=body.email))

    @router.post("/v1/auth/magic-link/verify")
    @public(reason=AUTH, verifier=verify_auth_request)
    def magic_link_verify(body: TokenBody, request: Request):
        return _run(request, lambda db, f: email_signin.verify_magic_link(db, f, token=body.token))

    @router.post("/v1/auth/email-code")
    @public(reason=AUTH, verifier=verify_auth_request)
    def email_code(body: EmailBody, request: Request):
        return _run(request, lambda db, f: email_signin.request_sign_in(db, f, kind="email_code",
                                                                        email=body.email))

    @router.post("/v1/auth/email-code/verify")
    @public(reason=AUTH, verifier=verify_auth_request)
    def email_code_verify(body: CodeBody, request: Request):
        return _run(request, lambda db, f: email_signin.verify_email_code(db, f, code=body.code))

    @router.post("/v1/auth/signup")
    @public(reason=AUTH, verifier=verify_auth_request)
    def signup(body: SignupBody, request: Request):
        return _run(request, lambda db, f: email_signin.request_signup(db, f, name=body.name,
                                                                       email=body.email))

    @router.post("/v1/auth/signup/verify")
    @public(reason=AUTH, verifier=verify_auth_request)
    def signup_verify(body: SignupVerifyBody, request: Request):
        return _run(request, lambda db, f: email_signin.complete_signup(db, f, token=body.token,
                                                                        password=body.password))

    @router.get("/v1/auth/setup/status")
    @public(reason=AUTH, verifier=verify_auth_request)
    def setup_status(request: Request):
        """Whether this BOSS still needs its first Owner, and which claim paths apply to this
        browser. Read-only (R5): while unclaimed, the API itself keeps a live setup link printed
        to its logs (main.lifespan: at boot and whenever the last one expires)."""
        bridged = _bridged(request)
        with begin() as db:
            claimed = claim_module.is_claimed(db)
            body: dict[str, Any] = {"claimed": claimed}
            if not claimed:
                body["paths"] = claim_module.available_paths(db, bridged)
                if "network" in body["paths"]:
                    body["origin"] = origin_of(request.headers.get("origin", "")) or None
            return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @router.post("/v1/auth/setup/claim")
    @public(reason=AUTH, verifier=verify_auth_request)
    def setup_claim(body: ClaimBody, request: Request):
        bridged = _bridged(request)
        creds = credentials(request)
        ip = authn.client_ip_of(creds)
        with begin() as db:
            if claim_module.is_claimed(db):
                return error(404, "not_found")          # A14: for good, before anything else
            retry = claim_module.count_attempt(db, ip)
        if retry:
            return JSONResponse(status_code=429, content={"error": "rate_limited"},
                                headers={"Retry-After": str(retry)})
        try:
            with begin() as db:
                if not bridged:
                    blocked = origin_guard(request, db)
                    if blocked is not None:
                        return blocked
                user = claim_module.claim(db, claim_module.ClaimRequest(
                    name=body.name, email=body.email, password=body.password,
                    setup_token=body.setup_token, legacy_password=body.legacy_password,
                    confirm_origin=body.confirm_origin or body.origin, seat=body.seat), ip=ip, bridged=bridged,
                    headers=dict(creds.headers), peer_ip=creds.peer_ip)
                f = facts(request, db)
                answer = signin.finish(db, user, f, methods=["pwd"] if body.password else ["network"],
                                       mfa=False, extra={"mfa_enrolment_required": True})
                return render(answer)
        except claim_module.ClaimRefused as exc:
            return error(exc.status, exc.code)
        except IntegrityError as exc:
            # L4: the one-claim constraint caught a second claim the lock did not serialise.
            if "identity_one_claim_per_org" in str(exc.orig or exc):
                return error(404, "not_found")
            raise
        except PasswordBusy:
            return JSONResponse(status_code=429, content={"error": "busy"}, headers={"Retry-After": "2"})

    @router.post("/v1/auth/invitations/accept")
    @public(reason=AUTH, verifier=verify_auth_request)
    def accept_invitation(body: AcceptBody, request: Request):
        try:
            with begin() as db:
                blocked = origin_guard(request, db)
                if blocked is not None:
                    return blocked
                inv = invitations.open_by_token(db, body.token)
                if inv is None:
                    return error(400, "invitation_invalid")
                pol = users.get_policy(inv.org_id, db=db)
                try:
                    check_new_password(body.password, email=inv.email, policy=pol)
                except PasswordRejected as exc:
                    return error(422, exc.code)
                from sqlalchemy import text as _text

                # The address is verified only when the invitation was EMAILED to it (holding the
                # token proves the mailbox); a link handed over out of band proves nothing (M2).
                emailed = bool(db.execute(_text("SELECT emailed FROM identity_invitations WHERE id = :id"),
                                          {"id": inv.id}).scalar())
                pid = invitations.accept(db, inv, display_name=body.name, password_hash=hash_password(body.password),
                                         email_verified=emailed)
                user = users.by_id(db, pid)
                assert user is not None
                return render(signin.finish(db, user, facts(request, db), methods=["pwd"], mfa=False))
        except invitations.InvitationRefused as exc:
            return error(exc.status, exc.code)
        except PasswordBusy:
            return JSONResponse(status_code=429, content={"error": "busy"}, headers={"Retry-After": "2"})

    @router.post("/v1/auth/step-up")
    @authenticated(self_only=True)
    def step_up(body: StepUpBody, request: Request):
        try:
            with begin() as db:
                ctx = current_context(request, db)
                if ctx.session is None:
                    return error(401, "step_up_required")   # bearer credentials cannot step up
                return render(signin.step_up(db, facts(request, db), method=body.method, code=body.code,
                                             password=body.password))
        except AuthnError as exc:
            return refusal(exc)
        except PasswordBusy:
            return JSONResponse(status_code=429, content={"error": "busy"}, headers={"Retry-After": "2"})

    @router.post("/v1/auth/logout")
    @public(reason=AUTH, verifier=verify_auth_request)
    def logout(request: Request):
        """Public so an expired session can still clear its cookie; it only ever ends the
        session the cookie names."""
        return _run(request, lambda db, f: signin.logout(db, f))

    return router


def session_fields(request: Request) -> dict[str, Any]:
    """GET /v1/auth/session's identity half (IDENTITY.md §10: {principal, org, mfa_at, csrf,
    expires}), merged into the one handler in main.py, which keeps rc5's fields for the clients
    that read them (the MCP API surface's scope check, the console's auth-session helper). The
    identity router's own copy of this route was unreachable: main.py's was registered first.
    Empty when the request has no identity context (a legacy key identity has not mapped)."""
    from boss_engine.identity.principal import AuthMethod
    from boss_engine.identity.sessions import csrf_token

    try:
        with begin() as db:
            ctx = current_context(request, db)
    except (AuthnError, NotImplementedError):
        return {}
    method = {AuthMethod.API_KEY: "api_key", AuthMethod.LEGACY_API_KEY: "api_key",
              AuthMethod.SESSION: "session", AuthMethod.RUN_TOKEN: "run_token",
              AuthMethod.OIDC_BEARER: "oidc", AuthMethod.NETWORK: "network",
              AuthMethod.LEGACY_PASSWORD: "session"}.get(ctx.method, str(ctx.method.value))
    cred, session = ctx.credential, ctx.session
    return {
        "principal": {"id": ctx.principal.id, "kind": ctx.principal.kind.value,
                      "display_name": ctx.principal.display_name},
        "org": ctx.principal.org_id,
        "mfa_at": session.mfa_at.isoformat() if session and session.mfa_at else None,
        "csrf": csrf_token(session.id) if session else None,
        "expires": session.absolute_expires_at.isoformat() if session else None,
        "auth_method": method,
        "key_projects": (sorted(cred.project_ids) if cred is not None and cred.project_ids is not None
                         else None),
    }


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _shared_password_offer(db: Any, org: Optional[str]) -> Optional[dict[str, Any]]:
    """After the claim, while the shared console password still signs in (as Operator): the date
    it stops, so the sign-in page can offer it. None when it is not configured or has ended."""
    from datetime import datetime, timezone

    from boss_engine.identity import migration
    from boss_engine.identity.legacy import LegacyKind

    if not org or not claim_module.legacy_password_configured():
        return None
    m = migration.mapping(db, org, LegacyKind.SHARED_PASSWORD, "BOSS_CONSOLE_PASSWORD")
    if not m or m["retired_at"] is not None:
        return None
    until = m["retire_after"]
    if until is not None and until <= datetime.now(timezone.utc):
        return None
    return {"until": until.isoformat() if until else None}


def _bridged(request: Request) -> Optional[AuthContext]:
    """The network-bridge context, when S5's legacy bridge maps this request to the network
    operator. Anything else (a cookie, a key, nothing) is not a network claim."""
    from .. import identity_guard

    try:
        ctx = identity_guard.request_auth(request)
    except (AuthnError, NotImplementedError):
        return None
    return ctx if claim_module.is_network_bridged(ctx) else None
