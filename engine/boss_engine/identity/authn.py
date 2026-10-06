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


def authenticate(creds: RequestCredentials, *, db: Any = None) -> Optional[AuthContext]:
    """Authenticate one request.

    Returns None when the request carries no credential at all (anonymous), an AuthContext when it
    carries exactly one valid credential, and raises AuthnError otherwise. Checks status LIVE
    (principal active, session/key not revoked or expired) and CSRF for cookie-authenticated
    writes (§4.3). Run tokens are verified by `delegation.verify_run_token` (S3), which this calls.
    Legacy bridges (shared password, BOSS_API_KEY, network mode) are mapped by `legacy.bridge`
    (S5), which this calls when no new-style credential is present.
    """
    raise NotImplementedError("identity.authn.authenticate is stream S1's")


def parse_api_key(token: str) -> Optional[ParsedApiKey]:
    """Recognise a BOSS key by prefix and verify its CRC32 checksum WITHOUT touching the database
    (A28). None for anything that is not a BOSS key."""
    raise NotImplementedError("identity.authn.parse_api_key is stream S1's")


def hash_token(token: str) -> str:
    """SHA-256 hex of a 256-bit token (session, API key, challenge). Stored; the token never is."""
    raise NotImplementedError("identity.authn.hash_token is stream S1's")


def check_csrf(creds: RequestCredentials, *, session_id: str, public_origin: str) -> None:
    """Origin == public origin, Sec-Fetch-Site same-origin when present, and X-Boss-CSRF ==
    HMAC(session_id). Raises AuthnError("csrf_failed", 403)."""
    raise NotImplementedError("identity.authn.check_csrf is stream S1's")
