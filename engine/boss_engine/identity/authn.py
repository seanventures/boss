"""Authentication: one verified credential in, one AuthContext out. Owned by stream S1.

FROZEN IN PHASE 0: the names, formats and signatures below. The bodies marked NotImplementedError are
S1's (IDENTITY_BUILD.md §3). IDENTITY.md §4 and §5.

Rules every implementation keeps:
  - ONE credential per request. A session cookie AND a bearer together is 400 (AuthnError
    "ambiguous_credentials"); there is no precedence between them.
  - Identity comes only from the credential. `actor` body fields, `X-User-Id`, `X-Boss-Principal`
    and query parameters are never read here (A9).
  - Failure is uniform: unknown user, wrong password and locked account are the same
    `invalid_credentials` (A3). Secrets are never logged, not even a prefix (§8.3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .principal import AuthContext

# ---- names on the wire (frozen) ----------------------------------------------------------------

SESSION_COOKIE = "__Host-boss_session"     # bss_ + 32 random bytes, base64url; opaque, not a JWT
PREAUTH_COOKIE = "__Host-boss_preauth"     # binds sign-in challenges (MFA, WebAuthn, OIDC state,
                                           # magic link, email code) to the browser that asked
DEVICE_COOKIE = "__Host-boss_device"       # familiar-device marker; never a credential
CSRF_HEADER = "X-Boss-CSRF"                # HMAC(session_id), delivered by GET /v1/identity/me

SESSION_TOKEN_PREFIX = "bss_"
#: API key formats (§5.1): prefix + 43 base62 chars (32 random bytes) + 6 base62 chars (CRC32).
API_KEY_PREFIXES = {
    "pat": "boss_pat_",    # a person's personal access token
    "sak": "boss_sak_",    # a service-account key
    "scim": "boss_scim_",  # a SCIM provisioning key (sys_scim_<provider>)
}
EGRESS_CREDENTIAL_PREFIX = "boss_egr_"     # per-run sandbox credential (S3); never persisted raw
API_KEY_BODY_LEN = 43
API_KEY_CHECKSUM_LEN = 6

#: Sign-in methods recorded on a session (identity_sessions.auth_methods).
SIGN_IN_METHODS = frozenset({"pwd", "totp", "passkey", "recovery_code", "magic_link", "email_code",
                             "legacy_password", "network"})   # plus "sso:<provider_id>"

STEP_UP_WINDOW_SECONDS = 600


@dataclass(frozen=True)
class RequestCredentials:
    """The framework-free view of a request that authentication may read. Built by the API guard.
    `headers` keys are lowercase. `client_ip` is already resolved through BOSS_TRUSTED_PROXIES."""
    method: str
    path: str
    headers: Mapping[str, str]
    cookies: Mapping[str, str]
    client_ip: Optional[str] = None
    peer_ip: Optional[str] = None
    request_id: Optional[str] = None
    #: What the legacy transport middleware already decided (bridge only; S5 maps it).
    legacy_member_id: Optional[str] = None
    legacy_auth_method: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def bearer(self) -> Optional[str]:
        raw = self.headers.get("authorization", "")
        scheme, _, value = raw.partition(" ")
        return value.strip() or None if scheme.lower() in ("bearer", "dpop") else None


class AuthnError(Exception):
    """A credential was presented and is not acceptable. `code` is the stable error string the API
    returns (`unauthenticated`, `invalid_credentials`, `ambiguous_credentials`, `csrf_failed`,
    `step_up_required`, `account_disabled`); `status` the HTTP status."""

    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ParsedApiKey:
    kind: str          # pat | sak | scim
    body: str
    checksum_ok: bool


_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _base62(n: int, width: int) -> str:
    out = []
    while n:
        n, rem = divmod(n, 62)
        out.append(_BASE62[rem])
    text_value = "".join(reversed(out)) or "0"
    if len(text_value) > width:
        raise ValueError("value does not fit")
    return text_value.rjust(width, "0")


def _checksum(body: str) -> str:
    import zlib

    return _base62(zlib.crc32(body.encode("ascii")) & 0xFFFFFFFF, API_KEY_CHECKSUM_LEN)


def mint_api_key(kind: str) -> str:
    """A new BOSS key: prefix + 43 base62 chars (32 CSPRNG bytes) + 6 base62 chars of CRC32."""
    import secrets

    prefix = API_KEY_PREFIXES[kind]
    body = _base62(int.from_bytes(secrets.token_bytes(32), "big"), API_KEY_BODY_LEN)
    return prefix + body + _checksum(body)


def parse_api_key(token: str) -> Optional[ParsedApiKey]:
    """Recognise a BOSS key by prefix and verify its CRC32 checksum WITHOUT touching the database
    (A28). None for anything that is not a BOSS key."""
    if not isinstance(token, str):
        return None
    for kind, prefix in API_KEY_PREFIXES.items():
        if token.startswith(prefix):
            rest = token[len(prefix):]
            if len(rest) != API_KEY_BODY_LEN + API_KEY_CHECKSUM_LEN or any(c not in _BASE62 for c in rest):
                return ParsedApiKey(kind, rest[:API_KEY_BODY_LEN], False)
            body, checksum = rest[:API_KEY_BODY_LEN], rest[API_KEY_BODY_LEN:]
            import hmac as _hmac

            return ParsedApiKey(kind, body, _hmac.compare_digest(_checksum(body), checksum))
    return None


def hash_token(token: str) -> str:
    """SHA-256 hex of a 256-bit token (session, API key, challenge). Stored; the token never is."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request_origin(creds: RequestCredentials) -> Optional[str]:
    """The origin this request was addressed to, believing X-Forwarded-Host/Proto only from a
    trusted proxy. Used for CSRF only when no public origin is configured or bound."""
    from .authn_support import is_trusted_proxy, origin_of

    trusted_peer = is_trusted_proxy(creds.peer_ip or creds.client_ip)
    host = (creds.headers.get("x-forwarded-host") if trusted_peer else None) or creds.headers.get("host")
    proto = (creds.headers.get("x-forwarded-proto") if trusted_peer else None) or "http"
    if not host:
        return None
    return origin_of(f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}")


def check_csrf(creds: RequestCredentials, *, session_id: str, public_origin: str) -> None:
    """Origin == public origin, Sec-Fetch-Site same-origin when present, and X-Boss-CSRF ==
    HMAC(session_id). Raises AuthnError("csrf_failed", 403)."""
    import hmac as _hmac

    from .authn_support import origin_of
    from .sessions import csrf_token

    origin = origin_of(creds.headers.get("origin", ""))
    expected = origin_of(public_origin or "")
    if not origin or not expected or origin != expected:
        raise AuthnError("csrf_failed", 403)
    site = creds.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        raise AuthnError("csrf_failed", 403)
    sent = creds.headers.get(CSRF_HEADER.lower(), "")
    if not sent or not _hmac.compare_digest(sent.encode(), csrf_token(session_id).encode()):
        raise AuthnError("csrf_failed", 403)


def check_writing_get_csrf(creds: RequestCredentials, *, session_id: str) -> None:
    """A cookie-authenticated GET to a route declared `writes_on_get` (a GET that writes). The
    session cookie is SameSite=Lax, so a cross-site top-level navigation carries it; such a GET
    is accepted only from the console's own page: Sec-Fetch-Site: same-origin (every current
    browser sends it on its own fetches) or a valid X-Boss-CSRF. Raises AuthnError("csrf_failed",
    403). A navigation or a cross-site request has neither."""
    import hmac as _hmac

    from .sessions import csrf_token

    site = (creds.headers.get("sec-fetch-site") or "").strip().lower()
    if site == "same-origin":
        return
    sent = creds.headers.get(CSRF_HEADER.lower(), "")
    if sent and _hmac.compare_digest(sent.encode(), csrf_token(session_id).encode()):
        return
    raise AuthnError("csrf_failed", 403)


def check_origin_only(creds: RequestCredentials, *, public_origin: Optional[str]) -> None:
    """The pre-session CSRF check for /v1/auth/* writes (no session, so no HMAC yet): the Origin
    must be the public origin (or, when none is bound, this request's own origin) and
    Sec-Fetch-Site, when sent, same-origin. Login CSRF is refused (A8, A69)."""
    from .authn_support import origin_of

    origin = origin_of(creds.headers.get("origin", ""))
    expected = origin_of(public_origin or "") or request_origin(creds)
    if not origin or not expected or origin != expected:
        raise AuthnError("csrf_failed", 403)
    site = creds.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        raise AuthnError("csrf_failed", 403)


def client_ip_of(creds: RequestCredentials) -> Optional[str]:
    from .authn_support import resolve_client_ip

    return resolve_client_ip(creds.peer_ip or creds.client_ip, creds.headers.get("x-forwarded-for")) or None


def _looks_like_jwt(token: str) -> bool:
    return token.startswith("eyJ") and token.count(".") == 2


def authenticate(creds: RequestCredentials, *, db: Any = None) -> Optional[AuthContext]:
    """Authenticate one request.

    Returns None when the request carries no credential at all (anonymous), an AuthContext when it
    carries exactly one valid credential, and raises AuthnError otherwise. Checks status LIVE
    (principal active, session/key not revoked or expired) and CSRF for cookie-authenticated
    writes (§4.3). Run tokens are verified by `delegation.verify_run_token` (S3), which this calls.
    Legacy bridges (shared password, BOSS_API_KEY, network mode) are mapped by `legacy.bridge`
    (S5), which this calls when no new-style credential is present.
    """
    if db is None:
        from ..db import begin

        with begin() as conn:
            return _authenticate(creds, conn)
    return _authenticate(creds, db)


def _authenticate(creds: RequestCredentials, db: Any) -> Optional[AuthContext]:
    cookie = creds.cookies.get(SESSION_COOKIE)
    bearer = creds.bearer
    if cookie and bearer:
        raise AuthnError("ambiguous_credentials", 400)
    ip = client_ip_of(creds)
    ua = (creds.headers.get("user-agent") or "")[:400] or None
    if cookie:
        return _from_session(creds, db, cookie, ip, ua)
    if bearer:
        parsed = parse_api_key(bearer)
        if parsed is not None:
            if not parsed.checksum_ok:
                # A28: a typo or forgery is refused before any database access.
                raise AuthnError("invalid_credentials")
            from .api_keys import authenticate_key

            return authenticate_key(db, bearer, parsed, ip=ip, user_agent=ua,
                                    request_id=creds.request_id)
        if _looks_like_jwt(bearer):
            return _from_run_token(creds, db, bearer, ip, ua)
    return _bridge(creds, db, presented=bool(bearer))


def _bridge(creds: RequestCredentials, db: Any, *, presented: bool) -> Optional[AuthContext]:
    from . import legacy

    try:
        ctx = legacy.bridge(creds, db=db)
    except NotImplementedError:
        if presented or creds.legacy_member_id or creds.legacy_auth_method:
            raise  # a legacy credential S5 has not mapped yet: not evaluated (fail closed)
        return None
    if ctx is None and presented:
        raise AuthnError("invalid_credentials")
    return ctx


def _from_session(creds: RequestCredentials, db: Any, token: str, ip: Optional[str],
                  ua: Optional[str]) -> AuthContext:
    from . import sessions, users
    from .authn_support import public_origin
    from .principal import AuthMethod

    row = sessions.lookup(db, token)
    if row is None:
        raise AuthnError("unauthenticated")
    principal = users.load_principal(db, row.principal_id)
    if principal is None or not principal.active:
        raise AuthnError("unauthenticated")
    if creds.method.upper() in _UNSAFE_METHODS:
        expected = public_origin(db, principal.org_id) or request_origin(creds)
        check_csrf(creds, session_id=row.id, public_origin=expected or "")
    sessions.touch(db, row, org_id=principal.org_id)
    return AuthContext(principal=principal, method=AuthMethod.SESSION, session=row.ref(), ip=ip,
                       user_agent=ua, request_id=creds.request_id)


def _from_run_token(creds: RequestCredentials, db: Any, token: str, ip: Optional[str],
                    ua: Optional[str]) -> AuthContext:
    """A `boss-run+jwt` (S3): verified (signature, audience, DPoP proof bound to THIS method and
    URL, jti live) by delegation.verify_run_token, then turned into a context by S3's
    tokens.context_for: the ACTOR is the agent instance's Passport principal and `on_behalf_of`
    names the person — an agent never authenticates as the person directly."""
    import dataclasses

    from . import delegation
    from .authn_support import public_origin

    audience = delegation.AUD_MCP if creds.path.startswith("/mcp") else delegation.AUD_API
    base = public_origin(db) or request_origin(creds) or ""
    claims = delegation.verify_run_token(token, audience=audience,
                                         dpop_proof=creds.headers.get("dpop"),
                                         htm=creds.method.upper(), htu=base + creds.path, db=db)
    ctx = _run_token_context(claims, db)
    if not ctx.principal.active or ctx.principal.org_id != claims.org_id:
        raise AuthnError("unauthenticated")
    return dataclasses.replace(ctx, ip=ip, user_agent=ua, request_id=creds.request_id)


def _run_token_context(claims: Any, db: Any) -> AuthContext:
    try:
        from . import tokens  # S3's module; present once S3 lands
    except ImportError:
        tokens = None
    if tokens is not None and hasattr(tokens, "context_for"):
        return tokens.context_for(claims, db=db)
    # Until S3 lands: the same rule as tokens.context_for.
    from . import users
    from .principal import AuthMethod

    subject = users.load_principal(db, claims.sub)
    if subject is None:
        raise AuthnError("unauthenticated")
    principal = subject
    if claims.chain.current is not None:
        agent = users.load_principal(db, claims.chain.current.agent_id)
        if agent is None:
            raise AuthnError("unauthenticated")
        principal = agent
        if subject.status.value != "active":
            raise AuthnError("unauthenticated")
    return AuthContext(principal=principal, method=AuthMethod.RUN_TOKEN, run_token=claims)
