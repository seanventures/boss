"""The protocol endpoints (§6.2), framework-free: each takes what the request carried and returns
a Reply. Protocol errors never raise out of here, so what a failed request must persist (a burnt
code, a revoked family, an audit row) commits with it.

authorize  code flow only; PKCE S256 required; exact redirect_uri; nonce required; state passed
           through; `iss` on every response (RFC 9207). Until the client and redirect_uri are
           proven, an error is a BOSS page, never a redirect (S6.4). Workforce pools reuse the
           console session (__Host-boss_session); who may sign in is `oidc_app:sign_in`.
consent    the hosted consent form's POST: session + Origin + HMAC token (S6.7).
token      client authentication by the registered method, then authorization_code or
           refresh_token.
userinfo   an at+jwt from this issuer only, in the Authorization header (or a POST form body);
           never accepted from a URL.
revocation RFC 7009; always 200.
end_session RP-initiated logout; post_logout_redirect_uri exact, else a page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote, urlencode

from sqlalchemy import text

from . import clients, consent, pages, pools, tokens
from .util import (ACCESS_TTL, ACR_MFA, ACR_PWD, INTERACTION_TTL, SUPPORTED_SCOPES, TYP_ACCESS, TYP_ID, OidcError,
                   audit, now, random_token, sha256_hex)

AUTHORIZE_PER_IP = (120, 60)
TOKEN_PER_IP = (300, 60)
TOKEN_PER_CLIENT = (600, 60)
_SLUG = re.compile(r"[A-Za-z0-9_-]{1,100}")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
JSON_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


@dataclass
class Reply:
    status: int
    body: Any = None                    # dict -> JSON; str -> HTML; None -> empty
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Browser:
    session_cookie: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    origin: Optional[str] = None
    sec_fetch_site: Optional[str] = None


# ---- helpers ---------------------------------------------------------------------------------

def resolve(db: Any, org_slug: str) -> tuple[str, str, str]:
    """(org_id, issuer, public origin). The issuer is built only from the configured public
    origin, never from the request's Host (a forged Host would be a mix-up)."""
    from ..identity.authn_support import public_origin

    if not isinstance(org_slug, str) or not _SLUG.fullmatch(org_slug):
        raise OidcError("not_found", "unknown issuer", 404)
    if db.execute(text("SELECT 1 FROM orgs WHERE id = :o"), {"o": org_slug}).first() is None:
        raise OidcError("not_found", "unknown issuer", 404)
    origin = public_origin(db, org_slug)
    if not origin:
        raise OidcError("temporarily_unavailable", "BOSS has no public URL yet, so it cannot be an issuer", 503)
    return org_slug, f"{origin}/oidc/{org_slug}", origin


def _single(pairs: Iterable[tuple[str, str]]) -> tuple[dict[str, str], set[str]]:
    out: dict[str, str] = {}
    dups: set[str] = set()
    for k, v in pairs:
        if k in out:
            dups.add(k)
        out[k] = v
    return out, dups


def _page(status: int, title: str, message: str) -> Reply:
    body, headers = pages.error_page(title, message)
    return Reply(status, body, headers)


def _redirect(uri: str, params: Mapping[str, Optional[str]], status: int = 302) -> Reply:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    sep = "&" if "?" in uri else "?"
    return Reply(status, None, {"Location": f"{uri}{sep}{query}", "Cache-Control": "no-store", "Pragma": "no-cache",
                                "Referrer-Policy": "no-referrer"})


def _redirect_error(uri: str, issuer: str, state: Optional[str], code: str, description: str,
                    status: int = 302) -> Reply:
    return _redirect(uri, {"error": code, "error_description": description, "state": state, "iss": issuer}, status)


def _json_error(e: OidcError, *, basic: bool = False) -> Reply:
    headers = dict(JSON_NO_STORE)
    if e.status == 401:
        headers["WWW-Authenticate"] = 'Basic realm="oidc"' if basic else 'Bearer realm="oidc", error="invalid_token"'
    body = {"error": e.code}
    if e.description:
        body["error_description"] = e.description
    return Reply(e.status, body, headers)


def _rate(db: Any, bucket: str, ident: Optional[str], limit: tuple[int, int]) -> bool:
    from ..identity import ratelimit

    return ratelimit.hit(bucket, ident or "-", limit=limit[0], window_seconds=limit[1], db=db).allowed


# ---- discovery and JWKS ------------------------------------------------------------------------

def discovery(db: Any, org_slug: str) -> Reply:
    try:
        _, issuer, _ = resolve(db, org_slug)
    except OidcError as e:
        return _json_error(e)
    doc = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "jwks_uri": f"{issuer}/jwks",
        "revocation_endpoint": f"{issuer}/revocation",
        "end_session_endpoint": f"{issuer}/end_session",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["pairwise", "public"],
        "id_token_signing_alg_values_supported": ["RS256", "EdDSA"],
        "token_endpoint_auth_methods_supported": list(clients.AUTH_METHODS),
        "token_endpoint_auth_signing_alg_values_supported": list(clients.ASSERTION_ALGS),
        "revocation_endpoint_auth_methods_supported": list(clients.AUTH_METHODS),
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(SUPPORTED_SCOPES),
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "auth_time", "nonce", "acr", "amr", "azp", "sid",
                             "name", "email", "email_verified", "roles"],
        "acr_values_supported": [ACR_MFA, ACR_PWD],
        "prompt_values_supported": ["none", "login", "consent"],
        "claims_parameter_supported": False,
        "request_parameter_supported": False,
        "request_uri_parameter_supported": False,
        "authorization_response_iss_parameter_supported": True,
        "frontchannel_logout_supported": False,
        "backchannel_logout_supported": False,
    }
    return Reply(200, doc, {"Cache-Control": "public, max-age=300"})


def jwks(db: Any, org_slug: str) -> Reply:
    from . import keys

    try:
        org_id, _, _ = resolve(db, org_slug)
    except OidcError as e:
        return _json_error(e)
    return Reply(200, keys.jwks(db, org_id=org_id), {"Cache-Control": "public, max-age=300"})


# ---- authorize -------------------------------------------------------------------------------

def authorize(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], browser: Browser) -> Reply:
    try:
        org_id, issuer, origin = resolve(db, org_slug)
    except OidcError as e:
        return _page(e.status, "Sign-in unavailable", e.description)
    params, dups = _single(pairs)
    if {"client_id", "redirect_uri"} & dups:
        return _page(400, "Invalid sign-in request", "The app sent a malformed sign-in request.")
    client = clients.load(db, org_id=org_id, client_id=params.get("client_id"))
    if client is None:
        return _page(400, "Unknown app", "This app is not registered to sign in with BOSS.")
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri or redirect_uri not in client["redirect_uris"]:
        # S6.4: never redirect to an address the app did not register.
        return _page(400, "Invalid sign-in request", "The app's return address is not registered with BOSS.")
    state = params.get("state")

    def fail(code: str, description: str) -> Reply:
        return _redirect_error(redirect_uri, issuer, state, code, description)

    if not _rate(db, "oidc.authorize.ip", browser.ip, AUTHORIZE_PER_IP):
        return _page(429, "Too many requests", "Too many sign-in attempts from this address. Wait a minute.")
    if dups:
        return fail("invalid_request", "a parameter was repeated")
    if "request" in params:
        return fail("request_not_supported", "request objects are not supported")
    if "request_uri" in params:
        return fail("request_uri_not_supported", "request_uri is not supported")
    if "registration" in params:
        return fail("registration_not_supported", "registration is not supported")
    if params.get("response_type") != "code":
        return fail("unsupported_response_type", "only the authorization code flow (response_type=code)")
    if params.get("response_mode") not in (None, "query"):
        return fail("invalid_request", "only response_mode=query is supported")
    if state is not None and len(state) > 2048:
        return fail("invalid_request", "state is too long")
    scopes = list(dict.fromkeys((params.get("scope") or "").split()))
    if "openid" not in scopes:
        return fail("invalid_scope", "the openid scope is required")
    if any(s not in SUPPORTED_SCOPES or s not in client["allowed_scopes"] for s in scopes):
        return fail("invalid_scope", "a requested scope is unknown or not allowed for this app")
    if params.get("code_challenge_method") != "S256":
        return fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
    challenge = params.get("code_challenge") or ""
    if not _CHALLENGE.fullmatch(challenge):
        return fail("invalid_request", "code_challenge must be a base64url SHA-256 (43 characters)")
    nonce = params.get("nonce")
    if not nonce or len(nonce) > 512:
        return fail("invalid_request", "nonce is required")
    prompt = set((params.get("prompt") or "").split())
    if not prompt <= {"none", "login", "consent"} or ("none" in prompt and len(prompt) > 1):
        return fail("invalid_request", "prompt must be none, login or consent")
    max_age: Optional[int] = None
    if params.get("max_age") is not None:
        if not re.fullmatch(r"\d{1,9}", params["max_age"]):
            return fail("invalid_request", "max_age must be a number of seconds")
        max_age = int(params["max_age"])
    mfa = ACR_MFA in (params.get("acr_values") or "").split()
    req = {"client_id": client["id"], "redirect_uri": redirect_uri, "scopes": scopes, "state": state,
           "nonce": nonce, "code_challenge": challenge, "prompt": sorted(prompt), "max_age": max_age, "mfa": mfa}
    return _proceed(db, org_id, issuer, origin, client, req, browser, since=None)


def _workforce_session(db: Any, cookie: Optional[str]) -> tuple[str, Any]:
    """("ok", (row, principal)) | ("none", None) | ("unusable", None) | ("enrol", None)."""
    from ..identity import sessions, signin

    row = sessions.lookup(db, cookie) if cookie else None
    if row is None:
        return "none", None
    principal = pools.workforce_principal(db, row.principal_id)
    if principal is None:
        return "unusable", None
    if row.mfa_at is None and signin.enrolment_only(db, principal.id, None):
        return "enrol", None
    return "ok", (row, principal)


def _auth_time(row: Any) -> datetime:
    return max(t for t in (row.created_at, row.mfa_at) if t is not None)


def _new_interaction(db: Any, org_id: str, client_id: str, params: Mapping[str, Any]) -> str:
    raw = random_token("oin_", 24)
    db.execute(text("INSERT INTO oidc_interactions (id_hash, org_id, client_id, params, expires_at) VALUES "
                    "(:h, :o, :c, CAST(:p AS JSONB), :e)"),
               {"h": sha256_hex(raw), "o": org_id, "c": client_id, "p": json.dumps(dict(params)),
                "e": now() + INTERACTION_TTL})
    return raw


def _take_interaction(db: Any, org_id: str, raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw or not raw.startswith("oin_") or len(raw) > 100:
        return None
    r = db.execute(text("SELECT * FROM oidc_interactions WHERE id_hash = :h AND org_id = :o FOR UPDATE"),
                   {"h": sha256_hex(raw), "o": org_id}).mappings().first()
    if r is None or r["used_at"] is not None or r["expires_at"] <= now():
        return None
    db.execute(text("UPDATE oidc_interactions SET used_at = now() WHERE id_hash = :h"), {"h": r["id_hash"]})
    return dict(r)


def _proceed(db: Any, org_id: str, issuer: str, origin: str, client: Mapping[str, Any], req: Mapping[str, Any],
             browser: Browser, *, since: Optional[datetime]) -> Reply:
    redirect_uri, state, prompt = req["redirect_uri"], req["state"], set(req["prompt"])

    def fail(code: str, description: str) -> Reply:
        return _redirect_error(redirect_uri, issuer, state, code, description)

    pool = pools.load(db, org_id=org_id, pool_id=client["pool_id"])
    if pool is None or pool["kind"] != "workforce":
        return fail("temporarily_unavailable", "this app's user pool cannot sign in yet")
    status, found = _workforce_session(db, browser.session_cookie)
    if status == "unusable":
        return _page(403, "Sign in as yourself", "This BOSS sign-in is not a person's own account, so it cannot "
                     "sign in to apps. Sign in to BOSS with your own account, then return to the app.")
    if status == "enrol":
        return _page(403, "Finish setting up BOSS", "Add a second factor to your BOSS account, then return to the app.")
    if status == "none":
        if "none" in prompt:
            return fail("login_required", "the user is not signed in")
        if since is not None:
            # Back from the console sign-in without a session: say so instead of looping.
            return _page(401, "Sign in to BOSS first", "Sign in to BOSS, then return to the app and try again.")
        raw = _new_interaction(db, org_id, client["id"], req)
        nxt = f"/oidc/{org_id}/resume?interaction={raw}"
        return Reply(302, None, {"Location": f"{origin}/login?next={quote(nxt, safe='')}", "Cache-Control": "no-store",
                                 "Referrer-Policy": "no-referrer"})
    row, principal = found
    if principal.org_id != org_id:
        return fail("access_denied", "this account is not a member of this BOSS")
    age = (now() - _auth_time(row)).total_seconds()
    reauth = "login" in prompt or (req["max_age"] is not None and age > req["max_age"])
    if reauth and not (since is not None and _auth_time(row) >= since):
        # Phase 1: BOSS cannot yet ask a signed-in person to sign in again from here.
        return fail("login_required", "a fresh sign-in is required")
    if (client["require_mfa"] or req["mfa"]) and row.mfa_at is None:
        if "none" in prompt:
            return fail("interaction_required", "a second factor is required")
        return _page(403, "A second factor is required", f"{client['name']} requires a second factor. Add one to "
                     "your BOSS account, sign in again with it, then return to the app.")
    if not pools.workforce_may_sign_in(db, principal=principal, client=client, session=row.ref()):
        audit(db, org_id=org_id, event_type="oidc.signin", outcome="denied", actor_principal_id=principal.id,
              actor_kind="user", target_type="oidc_client", target_id=client["id"], session_ref=row.id, ip=browser.ip,
              details={"reason": "no_sign_in_grant"})
        return fail("access_denied", "you may not sign in to this app")
    scopes = list(req["scopes"])
    if consent.needed(db, client=client, subject_ref=principal.id, scopes=scopes, forced="consent" in prompt):
        if "none" in prompt:
            return fail("consent_required", "consent is required")
        raw = _new_interaction(db, org_id, client["id"], {**req, "subject_ref": principal.id, "session_id": row.id})
        body, headers = pages.consent_page(
            app_name=client["name"], user_label=principal.display_name, scopes=scopes,
            action=f"/oidc/{org_id}/consent", interaction=raw, csrf=consent.csrf_token(row.id, raw),
            redirect_uri=redirect_uri)
        return Reply(200, body, headers)
    return _issue(db, org_id, issuer, client, pool, req, row, principal, browser)


def _issue(db: Any, org_id: str, issuer: str, client: Mapping[str, Any], pool: Mapping[str, Any],
           req: Mapping[str, Any], row: Any, principal: Any, browser: Browser, status: int = 302) -> Reply:
    acr = ACR_MFA if row.mfa_at else ACR_PWD
    amr = sorted(set(row.auth_methods) | ({"mfa"} if row.mfa_at else set()))
    auth_time = _auth_time(row)
    grant_id = tokens.create_grant(db, org_id=org_id, client_id=client["id"], pool_id=pool["id"],
                                   subject_ref=principal.id, session_ref=row.id, scopes=list(req["scopes"]),
                                   acr=acr, amr=amr, auth_time=auth_time)
    code = tokens.issue_code(db, grant_id=grant_id, client_id=client["id"], subject_ref=principal.id,
                             redirect_uri=req["redirect_uri"], code_challenge=req["code_challenge"],
                             nonce=req["nonce"], scopes=list(req["scopes"]), acr=acr, auth_time=auth_time)
    audit(db, org_id=org_id, event_type="oidc.signin", actor_principal_id=principal.id, actor_kind="user",
          target_type="oidc_client", target_id=client["id"], session_ref=row.id, ip=browser.ip,
          details={"grant_id": grant_id, "scopes": list(req["scopes"]), "acr": acr})
    return _redirect(req["redirect_uri"], {"code": code, "state": req["state"], "iss": issuer}, status)


def resume(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], browser: Browser) -> Reply:
    """Back from the console sign-in: the pending request, once."""
    try:
        org_id, issuer, origin = resolve(db, org_slug)
    except OidcError as e:
        return _page(e.status, "Sign-in unavailable", e.description)
    params, _ = _single(pairs)
    it = _take_interaction(db, org_id, params.get("interaction"))
    if it is None or "subject_ref" in it["params"]:
        return _page(400, "This sign-in has expired", "Go back to the app and sign in again.")
    req = it["params"]
    client = clients.load(db, org_id=org_id, client_id=req.get("client_id"))
    if client is None or req.get("redirect_uri") not in client["redirect_uris"]:
        return _page(400, "Invalid sign-in request", "The app is no longer registered for this sign-in.")
    return _proceed(db, org_id, issuer, origin, client, req, browser, since=it["created_at"])


def consent_submit(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], browser: Browser) -> Reply:
    from ..identity.authn_support import origin_of

    try:
        org_id, issuer, origin = resolve(db, org_slug)
    except OidcError as e:
        return _page(e.status, "Sign-in unavailable", e.description)
    params, dups = _single(pairs)
    forged = _page(403, "Request refused", "This consent did not come from BOSS's own page. Go back to the app and "
                   "sign in again.")
    if dups or origin_of(browser.origin or "") != origin or \
            (browser.sec_fetch_site is not None and browser.sec_fetch_site.lower() != "same-origin"):
        return forged
    status, found = _workforce_session(db, browser.session_cookie)
    if status != "ok":
        return forged
    row, principal = found
    raw = params.get("interaction")
    if not consent.csrf_ok(row.id, raw or "", params.get("csrf")):
        return forged
    it = _take_interaction(db, org_id, raw)
    if it is None:
        return _page(400, "This sign-in has expired", "Go back to the app and sign in again.")
    req = it["params"]
    if req.get("session_id") != row.id or req.get("subject_ref") != principal.id:
        return forged
    client = clients.load(db, org_id=org_id, client_id=req.get("client_id"))
    if client is None or req.get("redirect_uri") not in client["redirect_uris"]:
        return _page(400, "Invalid sign-in request", "The app is no longer registered for this sign-in.")
    if params.get("decision") != "allow":
        return _redirect_error(req["redirect_uri"], issuer, req["state"], "access_denied", "the user declined", 303)
    pool = pools.load(db, org_id=org_id, pool_id=client["pool_id"])
    if pool is None or pool["kind"] != "workforce" or \
            not pools.workforce_may_sign_in(db, principal=principal, client=client, session=row.ref()):
        return _redirect_error(req["redirect_uri"], issuer, req["state"], "access_denied",
                               "you may not sign in to this app", 303)
    consent.grant(db, client=client, subject_ref=principal.id, scopes=req["scopes"], actor_kind="user", ip=browser.ip)
    return _issue(db, org_id, issuer, client, pool, req, row, principal, browser, status=303)


# ---- token -----------------------------------------------------------------------------------

def token(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], headers: Mapping[str, str],
          ip: Optional[str]) -> Reply:
    basic = (headers.get("authorization") or "")[:6].lower() == "basic "
    try:
        org_id, issuer, _ = resolve(db, org_slug)
        if not (headers.get("content-type") or "").lower().startswith("application/x-www-form-urlencoded"):
            raise OidcError("invalid_request", "the body must be application/x-www-form-urlencoded")
        params, dups = _single(pairs)
        if dups:
            raise OidcError("invalid_request", "a parameter was repeated")
        if not _rate(db, "oidc.token.ip", ip, TOKEN_PER_IP):
            raise OidcError("slow_down", "too many requests", 429)
        client = clients.authenticate(db, org_id=org_id, form=params, headers=headers,
                                      audiences=(f"{issuer}/token", issuer), ip=ip)
        if not _rate(db, "oidc.token.client", client["id"], TOKEN_PER_CLIENT):
            raise OidcError("slow_down", "too many requests", 429)
        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            return _code_grant(db, issuer, client, params, ip)
        if grant_type == "refresh_token":
            return _refresh_grant(db, issuer, client, params, ip)
        raise OidcError("unsupported_grant_type", "authorization_code or refresh_token")
    except OidcError as e:
        return _json_error(e, basic=basic)


def _token_response(db: Any, issuer: str, client: Mapping[str, Any], pool: Mapping[str, Any], grant: Mapping[str, Any],
                    scopes: list[str], *, nonce: Optional[str], refresh_scopes: Optional[list[str]]) -> Reply:
    claims = pools.claims_for(db, pool=pool, client=client, subject_ref=grant["subject_ref"], scopes=scopes)
    body = {
        "access_token": tokens.mint_access_token(db, issuer=issuer, client=client, grant=grant, sub=claims["sub"],
                                                 scopes=scopes),
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "id_token": tokens.mint_id_token(db, issuer=issuer, client=client, grant=grant, claims=claims, nonce=nonce),
        "scope": " ".join(scopes),
    }
    if refresh_scopes is not None:
        body["refresh_token"] = tokens.issue_refresh(db, grant=grant, scopes=refresh_scopes)
    return Reply(200, body, dict(JSON_NO_STORE))


def _live(db: Any, client: Mapping[str, Any], grant: Mapping[str, Any]) -> Mapping[str, Any]:
    pool = pools.load(db, org_id=client["org_id"], pool_id=grant["pool_id"])
    if pool is None or pool["id"] != client["pool_id"] or \
            not pools.subject_alive(db, pool=pool, client=client, subject_ref=grant["subject_ref"]):
        tokens.revoke_grant(db, grant["id"], reason="subject_not_allowed")
        raise OidcError("invalid_grant", "the user may no longer sign in to this app")
    return pool


def _code_grant(db: Any, issuer: str, client: Mapping[str, Any], params: Mapping[str, str], ip: Optional[str]) -> Reply:
    row = tokens.redeem_code(db, client=client, code=params.get("code"), redirect_uri=params.get("redirect_uri"),
                             code_verifier=params.get("code_verifier"))
    grant = row["grant"]
    pool = _live(db, client, grant)
    scopes = list(row["scopes"])
    offline = "offline_access" in scopes and \
        consent.covers(db, client=client, subject_ref=grant["subject_ref"], scopes=["offline_access"])
    reply = _token_response(db, issuer, client, pool, grant, scopes, nonce=row["nonce"],
                            refresh_scopes=scopes if offline else None)
    audit(db, org_id=client["org_id"], event_type="oidc.token.issued", actor_kind="oidc_client",
          target_type="oidc_client", target_id=client["id"], ip=ip,
          details={"grant_id": grant["id"], "grant_type": "authorization_code", "refresh": offline})
    return reply


def _refresh_grant(db: Any, issuer: str, client: Mapping[str, Any], params: Mapping[str, str], ip: Optional[str]) -> Reply:
    row = tokens.use_refresh(db, client=client, token=params.get("refresh_token"))
    grant = row["grant"]
    pool = _live(db, client, grant)
    family_scopes = list(row["scopes"])
    if not consent.covers(db, client=client, subject_ref=grant["subject_ref"], scopes=family_scopes):
        tokens.revoke_grant(db, grant["id"], reason="consent_withdrawn")
        raise OidcError("invalid_grant", "consent was withdrawn")
    scopes = family_scopes
    if params.get("scope"):
        asked = list(dict.fromkeys(params["scope"].split()))
        if not set(asked) <= set(family_scopes):
            raise OidcError("invalid_scope", "a refresh cannot widen the scopes")
        scopes = asked
    reply = _token_response(db, issuer, client, pool, grant, scopes, nonce=None, refresh_scopes=family_scopes)
    audit(db, org_id=client["org_id"], event_type="oidc.token.refreshed", actor_kind="oidc_client",
          target_type="oidc_client", target_id=client["id"], ip=ip, details={"grant_id": grant["id"]})
    return reply


# ---- userinfo --------------------------------------------------------------------------------

def userinfo(db: Any, org_slug: str, *, method: str, headers: Mapping[str, str], query: Iterable[tuple[str, str]],
             form: Iterable[tuple[str, str]]) -> Reply:
    try:
        org_id, issuer, _ = resolve(db, org_slug)
        if any(k == "access_token" for k, _ in query):
            raise OidcError("invalid_request", "an access token is never accepted in a URL")
        auth = headers.get("authorization") or ""
        form_params, dups = _single(form)
        from_form = form_params.get("access_token") if method.upper() == "POST" else None
        if dups or (auth and from_form):
            raise OidcError("invalid_request", "send the access token once")
        if auth:
            if auth[:7].lower() != "bearer ":
                raise OidcError("invalid_token", "bearer token required", 401)
            presented = auth[7:].strip()
        elif from_form:
            presented = from_form
        else:
            return Reply(401, {"error": "invalid_request"}, {**JSON_NO_STORE, "WWW-Authenticate": 'Bearer realm="oidc"'})
        claims = tokens.verify_own(db, org_id=org_id, issuer=issuer, token=presented, typ=TYP_ACCESS)
        grant = tokens.load_grant(db, claims.get("sid"))
        if grant is None or grant["revoked_at"] is not None or grant["org_id"] != org_id \
                or grant["client_id"] != claims.get("client_id"):
            raise OidcError("invalid_token", "the token is not valid here", 401)
        client = clients.load(db, org_id=org_id, client_id=grant["client_id"])
        if client is None:
            raise OidcError("invalid_token", "the token is not valid here", 401)
        pool = pools.load(db, org_id=org_id, pool_id=grant["pool_id"])
        if pool is None or not pools.subject_alive(db, pool=pool, client=client, subject_ref=grant["subject_ref"]):
            raise OidcError("invalid_token", "the token is not valid here", 401)
        scopes = str(claims.get("scope") or "").split()
        if "openid" not in scopes:
            return Reply(403, {"error": "insufficient_scope"},
                         {**JSON_NO_STORE, "WWW-Authenticate": 'Bearer realm="oidc", error="insufficient_scope"'})
        out = pools.claims_for(db, pool=pool, client=client, subject_ref=grant["subject_ref"], scopes=scopes)
        if out["sub"] != claims.get("sub"):
            raise OidcError("invalid_token", "the token is not valid here", 401)
        return Reply(200, out, dict(JSON_NO_STORE))
    except OidcError as e:
        return _json_error(e)


# ---- revocation ------------------------------------------------------------------------------

def revocation(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], headers: Mapping[str, str],
               ip: Optional[str]) -> Reply:
    basic = (headers.get("authorization") or "")[:6].lower() == "basic "
    try:
        org_id, issuer, _ = resolve(db, org_slug)
        params, dups = _single(pairs)
        if dups:
            raise OidcError("invalid_request", "a parameter was repeated")
        client = clients.authenticate(db, org_id=org_id, form=params, headers=headers,
                                      audiences=(f"{issuer}/revocation", f"{issuer}/token", issuer), ip=ip)
        presented = params.get("token")
        if not presented:
            raise OidcError("invalid_request", "token is required")
        grant_id = None
        if presented.startswith(tokens.REFRESH_PREFIX) and len(presented) < 200:
            r = db.execute(text("SELECT family_id, client_id FROM oidc_refresh_tokens WHERE token_hash = :h"),
                           {"h": sha256_hex(presented)}).first()
            if r is not None and r[1] == client["id"]:
                grant_id = r[0]
        else:
            try:
                claims = tokens.verify_own(db, org_id=org_id, issuer=issuer, token=presented, typ=TYP_ACCESS)
                if claims.get("client_id") == client["id"]:
                    grant_id = claims.get("sid")
            except OidcError:
                grant_id = None
        if grant_id:
            tokens.revoke_grant(db, grant_id, reason="revoked_by_client")
            audit(db, org_id=org_id, event_type="oidc.token.revoked", actor_kind="oidc_client",
                  target_type="oidc_client", target_id=client["id"], ip=ip, details={"grant_id": grant_id})
        return Reply(200, None, dict(JSON_NO_STORE))
    except OidcError as e:
        return _json_error(e, basic=basic)


# ---- end_session -----------------------------------------------------------------------------

def end_session(db: Any, org_slug: str, pairs: Iterable[tuple[str, str]], browser: Browser) -> Reply:
    try:
        org_id, issuer, _ = resolve(db, org_slug)
    except OidcError as e:
        return _page(e.status, "Sign-out unavailable", e.description)
    params, dups = _single(pairs)
    invalid = _page(400, "Invalid sign-out request", "The app sent a sign-out request BOSS cannot honour.")
    if dups:
        return invalid
    claims = None
    hint = params.get("id_token_hint")
    if hint:
        try:
            claims = tokens.verify_own(db, org_id=org_id, issuer=issuer, token=hint, typ=TYP_ID, verify_exp=False)
        except OidcError:
            return invalid
    hinted = claims.get("aud") if claims else None
    if isinstance(hinted, list):
        hinted = hinted[0] if len(hinted) == 1 else None
    client_id = params.get("client_id") or hinted
    if params.get("client_id") and hinted and params["client_id"] != hinted:
        return invalid
    client = clients.load(db, org_id=org_id, client_id=client_id) if client_id else None
    target = params.get("post_logout_redirect_uri")
    if target is not None and (client is None or target not in client["post_logout_redirect_uris"]):
        # S6.4: never redirect to an unregistered address.
        return invalid
    if claims and client is not None:
        grant = tokens.load_grant(db, claims.get("sid"))
        if grant is not None and grant["client_id"] == client["id"] and grant["revoked_at"] is None:
            tokens.revoke_grant(db, grant["id"], reason="logout")
            audit(db, org_id=org_id, event_type="oidc.logout", actor_kind="oidc_client", target_type="oidc_client",
                  target_id=client["id"], ip=browser.ip, details={"grant_id": grant["id"]})
    if target is not None:
        return _redirect(target, {"state": params.get("state")})
    body, headers = pages.signed_out_page()
    return Reply(200, body, headers)
