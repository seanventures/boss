"""BOSS as an OIDC relying party (and an OAuth 2.0 client for GitHub) for CONSOLE sign-in
(IDENTITY.md §4.8, §4.15; D31). Stream S1.

  start     state (32 B), nonce (32 B) and a PKCE S256 verifier, stored ONLY inside a challenge
            (kind oidc_state, id = SHA-256(state)) bound to a NEW pre-auth cookie; the payload is
            encrypted under a key derived from that cookie, so a database dump cannot read it.
            redirect_uri is fixed at ${public origin}/v1/auth/sso/{provider_id}/callback.
  callback  state must match AND be bound to this browser (A69) AND name this provider (A65); the
            RFC 9207 `iss` parameter must match when present; no code exchange without the
            verifier (A64); the id_token must verify against the provider's JWKS with an allowed
            asymmetric alg (never none/HS*, A62), iss exact, aud ∋ client_id, azp with multiple
            audiences, exp/iat/nbf with 60 s skew, nonce (A63).
  identity  (issuer, subject) only (A61, A89). Userinfo/email are display and invitation inputs.

Every fetch (discovery, JWKS, token, GitHub API) goes through the SSRF-safe client (A70, A71).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import text

from . import http as safe_http
from . import vault
from .authn import PREAUTH_COOKIE
from .authn_support import (
    b64url,
    challenge_by_secret,
    consume_challenge,
    create_challenge,
    random_token,
)
from .principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus

STATE_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 60
DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
JWKS_REFETCH_MIN_SECONDS = 60
#: Asymmetric only. `none` and every HMAC alg are refused whatever a provider row says (A62).
SAFE_ALGS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384",
                       "ES512", "EdDSA"})
MFA_AMR = frozenset({"mfa", "hwk", "otp"})

GITHUB_ISSUER = "https://github.com"
PRESETS: dict[str, dict[str, Any]] = {
    "google": {"protocol": "oidc", "issuer": "https://accounts.google.com"},
    "microsoft": {"protocol": "oidc", "issuer_template": "https://login.microsoftonline.com/{tenant}/v2.0"},
    "apple": {"protocol": "oidc", "issuer": "https://appleid.apple.com", "response_mode": "form_post"},
    "github": {"protocol": "oauth2", "issuer": GITHUB_ISSUER,
               "authorization_endpoint": "https://github.com/login/oauth/authorize",
               "token_endpoint": "https://github.com/login/oauth/access_token",
               "user_endpoint": "https://api.github.com/user",
               "emails_endpoint": "https://api.github.com/user/emails",
               "scopes": ["read:user", "user:email"]},
}


class SsoError(Exception):
    """A refused federated sign-in. `code` is safe to show (it never carries a token)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Provider:
    id: str
    org_id: str
    protocol: str
    preset: str
    name: str
    issuer: Optional[str]
    client_id: Optional[str]
    client_secret_vault_item_id: Optional[str]
    scopes: tuple[str, ...]
    allowed_algs: tuple[str, ...]
    saml_sp_entity_id: Optional[str]
    saml_certificates: tuple[str, ...]
    domains: tuple[str, ...]
    jit_mode: str
    trust_idp_mfa: bool
    enforce_sso: bool
    groups_claim: Optional[str]
    status: str
    #: SAML: the IdP's SSO endpoint (amendment S1-5). Rows saved before the column carry it in
    #: `client_id`; read it through `sso_url`.
    saml_sso_url: Optional[str] = None
    #: SAML: the AuthnContextClassRef values that count as MFA under "trust IdP MFA"; None or
    #: empty means BOSS's default list (sso_saml.MFA_CONTEXTS).
    trusted_authn_contexts: Optional[tuple[str, ...]] = None
    #: §4.8: "client_secret" (vaulted secret) or "private_key_jwt" (RFC 7523 client assertion)
    client_auth: str = "client_secret"
    client_key_vault_item_id: Optional[str] = None
    client_key_kid: Optional[str] = None
    #: the assertion's `iss` when it is not the client id (Apple: the team id)
    client_assertion_issuer: Optional[str] = None

    @property
    def sso_url(self) -> Optional[str]:
        """The SAML IdP SSO endpoint: the column, else (a provider saved before it) `client_id`."""
        if self.protocol != "saml":
            return None
        return self.saml_sso_url or self.client_id or None

    @property
    def response_mode(self) -> Optional[str]:
        return PRESETS.get(self.preset, {}).get("response_mode")


@dataclass(frozen=True)
class FederatedIdentity:
    issuer: str
    subject: str
    email: Optional[str] = None
    email_verified: bool = False
    name: Optional[str] = None
    amr: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    mfa: bool = False                 # set by the protocol layer when the IdP vouched for MFA


@dataclass(frozen=True)
class StartedFlow:
    url: str = field(repr=False)      # carries state: never logged
    cookie: str = field(repr=False)


_PROVIDER_COLS = ("id, org_id, protocol, preset, name, issuer, client_id, client_secret_vault_item_id, "
                  "scopes, allowed_algs, saml_sp_entity_id, saml_certificates, domains, jit_mode, "
                  "trust_idp_mfa, enforce_sso, groups_claim, status, saml_sso_url, trusted_authn_contexts, "
                  "client_auth, client_key_vault_item_id, client_key_kid, client_assertion_issuer")


def provider_from_row(r: Any) -> Provider:
    contexts = getattr(r, "trusted_authn_contexts", None)
    return Provider(r.id, r.org_id, r.protocol, r.preset, r.name, r.issuer, r.client_id,
                    r.client_secret_vault_item_id, tuple(r.scopes or ()), tuple(r.allowed_algs or ()),
                    r.saml_sp_entity_id, tuple(r.saml_certificates or ()), tuple(r.domains or ()),
                    r.jit_mode, bool(r.trust_idp_mfa), bool(r.enforce_sso), r.groups_claim, r.status,
                    saml_sso_url=getattr(r, "saml_sso_url", None),
                    trusted_authn_contexts=tuple(contexts) if contexts else None,
                    client_auth=getattr(r, "client_auth", None) or "client_secret",
                    client_key_vault_item_id=getattr(r, "client_key_vault_item_id", None),
                    client_key_kid=getattr(r, "client_key_kid", None),
                    client_assertion_issuer=getattr(r, "client_assertion_issuer", None))


def load_provider(db: Any, provider_id: str, *, active_only: bool = True) -> Optional[Provider]:
    sql = f"SELECT {_PROVIDER_COLS} FROM identity_providers WHERE id = :id"
    if active_only:
        sql += " AND status = 'active'"
    r = db.execute(text(sql), {"id": provider_id}).first()
    return provider_from_row(r) if r else None


def _fetch(method: str, url: str, **kwargs: Any) -> safe_http.SafeResponse:
    """The one door out (tests replace it with a fake IdP that delegates unknown URLs here)."""
    return safe_http.fetch(method, url, **kwargs)


def _json(resp: safe_http.SafeResponse, what: str) -> dict[str, Any]:
    if resp.status != 200:
        raise SsoError(f"{what}_failed")
    try:
        data = resp.json()
    except (ValueError, UnicodeDecodeError):
        raise SsoError(f"{what}_invalid") from None
    if not isinstance(data, dict):
        raise SsoError(f"{what}_invalid")
    return data


# ---- discovery and JWKS (cached) ------------------------------------------------------------------

_LOCK = threading.Lock()
_DISCOVERY: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_caches() -> None:
    with _LOCK:
        _DISCOVERY.clear()
        _JWKS.clear()


def discovery(provider: Provider) -> dict[str, Any]:
    if provider.protocol == "oauth2":
        preset = PRESETS.get(provider.preset)
        if not preset:
            raise SsoError("provider_misconfigured")
        return {"issuer": preset["issuer"], "authorization_endpoint": preset["authorization_endpoint"],
                "token_endpoint": preset["token_endpoint"]}
    issuer = provider.issuer or ""
    if not issuer:
        raise SsoError("provider_misconfigured")
    with _LOCK:
        hit = _DISCOVERY.get(issuer)
    if hit and time.monotonic() - hit[0] < DISCOVERY_TTL_SECONDS:
        return hit[1]
    try:
        resp = _fetch("GET", issuer.rstrip("/") + "/.well-known/openid-configuration",
                      purpose="oidc.discovery")
    except safe_http.OutboundRefused:
        raise SsoError("discovery_refused") from None
    doc = _json(resp, "discovery")
    # Exact issuer match: a discovery document claiming another issuer is a mix-up (A65).
    if doc.get("issuer") != issuer:
        raise SsoError("issuer_mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(doc.get(key), str):
            raise SsoError("discovery_invalid")
    with _LOCK:
        _DISCOVERY[issuer] = (time.monotonic(), doc)
    return doc


def _jwks(jwks_uri: str, *, force: bool = False) -> dict[str, Any]:
    with _LOCK:
        hit = _JWKS.get(jwks_uri)
    now = time.monotonic()
    if hit and not force and now - hit[0] < JWKS_TTL_SECONDS:
        return hit[1]
    if hit and force and now - hit[0] < JWKS_REFETCH_MIN_SECONDS:
        return hit[1]          # an unknown kid refetches at most once a minute
    try:
        resp = _fetch("GET", jwks_uri, purpose="oidc.jwks")
    except safe_http.OutboundRefused:
        raise SsoError("jwks_refused") from None
    doc = _json(resp, "jwks")
    if not isinstance(doc.get("keys"), list):
        raise SsoError("jwks_invalid")
    with _LOCK:
        _JWKS[jwks_uri] = (now, doc)
    return doc


def _key_for(jwks_uri: str, kid: Optional[str], alg: str) -> Any:
    import jwt

    for force in (False, True):
        for jwk in _jwks(jwks_uri, force=force).get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                continue
            if kid is not None and jwk.get("kid") != kid:
                continue
            if jwk.get("kty") == "oct":
                continue       # never a symmetric key
            if jwk.get("alg") and jwk["alg"] != alg:
                continue
            try:
                return jwt.PyJWK(jwk, algorithm=alg).key
            except Exception:  # noqa: BLE001 — an unusable key is skipped
                continue
    raise SsoError("unknown_key")


# ---- start ------------------------------------------------------------------------------------

def redirect_uri(origin: str, provider_id: str) -> str:
    return f"{origin}/v1/auth/sso/{provider_id}/callback"


def start(db: Any, provider: Provider, *, origin: str, purpose: str = "login",
          link_principal: Optional[str] = None) -> StartedFlow:
    from .sessions import set_cookie_header

    if purpose not in ("login", "link") or (purpose == "link" and not link_principal):
        raise SsoError("invalid_purpose")
    if not provider.client_id:
        raise SsoError("provider_misconfigured")
    doc = discovery(provider)
    preauth = random_token("bpa_")
    state = random_token()
    nonce = random_token()
    verifier = random_token(nbytes=48)
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    create_challenge(db, kind="oidc_state", binding_secret=preauth, ttl_seconds=STATE_TTL_SECONDS,
                     secret=state, principal_id=link_principal,
                     payload={"nonce": nonce, "verifier": verifier, "provider_id": provider.id,
                              "purpose": purpose, "link_principal": link_principal})
    params = {"response_type": "code", "client_id": provider.client_id,
              "redirect_uri": redirect_uri(origin, provider.id), "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if provider.protocol == "oidc":
        scopes = list(dict.fromkeys(["openid", "email", "profile", *provider.scopes]))
        params.update(scope=" ".join(scopes), nonce=nonce)
        if provider.response_mode:
            params["response_mode"] = provider.response_mode
    else:
        params["scope"] = " ".join(provider.scopes or PRESETS["github"]["scopes"])
    samesite = "None" if provider.response_mode == "form_post" else "Lax"
    cookie = set_cookie_header(PREAUTH_COOKIE, preauth, max_age=STATE_TTL_SECONDS, samesite=samesite)
    return StartedFlow(f"{doc['authorization_endpoint']}?{urlencode(params)}", cookie)


# ---- callback -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class CallbackResult:
    identity: FederatedIdentity
    purpose: str
    link_principal: Optional[str]


def callback(db: Any, provider: Provider, *, params: dict[str, str], preauth: Optional[str],
             origin: str) -> CallbackResult:
    if params.get("error"):
        raise SsoError("idp_error")
    state = params.get("state") or ""
    ch = challenge_by_secret(db, kind="oidc_state", secret=state, binding_secret=preauth,
                             require_binding=True)
    if ch is None:
        raise SsoError("state_invalid")                       # A69: another browser's state
    if not consume_challenge(db, ch.id_hash):
        raise SsoError("state_invalid")                       # single use
    payload = ch.payload
    if payload.get("provider_id") != provider.id:
        raise SsoError("provider_mismatch")                   # A65: A's state at B's callback
    if provider.protocol == "oidc":
        iss_param = params.get("iss")
        if iss_param is not None and iss_param != provider.issuer:
            raise SsoError("issuer_mismatch")                 # RFC 9207
    verifier = payload.get("verifier")
    code = params.get("code") or ""
    if not verifier or not code:
        raise SsoError("verifier_missing")                    # A64: never exchange without one
    doc = discovery(provider)
    tokens = _exchange(db, provider, doc, code=code, verifier=verifier, origin=origin)
    if provider.protocol == "oauth2":
        ident = _github_identity(tokens)
    else:
        ident = _verify_id_token(provider, doc, tokens.get("id_token"), nonce=payload.get("nonce") or "")
    return CallbackResult(ident, payload.get("purpose", "login"), payload.get("link_principal"))


def _vaulted(db: Any, provider: Provider, item_id: Optional[str]) -> Optional[str]:
    if not item_id:
        return None
    system = Principal("sys_sso", PrincipalKind.SYSTEM, provider.org_id, PrincipalStatus.ACTIVE, "SSO")
    cred = vault.get_credential(
        vault.ConnectionRef(provider.id, None, item_id),
        vault.CredentialPurpose.SSO_CLIENT, ctx=AuthContext(system, AuthMethod.SYSTEM), db=db)
    return cred.token


def _client_secret(db: Any, provider: Provider) -> Optional[str]:
    return _vaulted(db, provider, provider.client_secret_vault_item_id)


# ---- private_key_jwt (RFC 7523; §4.8) -------------------------------------------------------------------

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_SECONDS = 300


def generate_client_key() -> str:
    """A new P-256 private key (PEM) for a provider that registers BOSS's public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def _load_private_key(pem: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
        return key
    raise ValueError("the client key must be a P-256 EC key or an RSA key of 2048 bits or more")


def client_key_alg(pem: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec

    return "ES256" if isinstance(_load_private_key(pem), ec.EllipticCurvePrivateKey) else "RS256"


def public_jwk(pem: str, kid: Optional[str]) -> dict[str, Any]:
    """The PUBLIC half as a JWK (never the private parts), for the admin to register at the IdP."""
    import json

    import jwt

    key = _load_private_key(pem)
    alg = client_key_alg(pem)
    algo = jwt.algorithms.ECAlgorithm(jwt.algorithms.ECAlgorithm.SHA256) if alg == "ES256" \
        else jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256)
    jwk = json.loads(algo.to_jwk(key.public_key()))
    jwk.update({"use": "sig", "alg": alg, **({"kid": kid} if kid else {})})
    return {k: v for k, v in jwk.items() if k not in ("d", "p", "q", "dp", "dq", "qi")}


def client_key_thumbprint(pem: str) -> str:
    """A stable kid for a generated key: the RFC 7638 thumbprint of its public JWK (16 chars)."""
    import base64
    import hashlib
    import json

    jwk = public_jwk(pem, None)
    members = {k: jwk[k] for k in (("crv", "kty", "x", "y") if jwk["kty"] == "EC" else ("e", "kty", "n"))}
    digest = hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:16]


def client_assertion(provider: Provider, pem: str, *, token_endpoint: str, now: Optional[int] = None) -> str:
    """The signed client assertion. Apple's form (preset apple) is its `client_secret`: aud is the
    issuer and iss the team id; the RFC 7523 form's aud is the token endpoint and iss = sub = client_id."""
    import secrets as _secrets
    import time

    import jwt

    issued = int(now if now is not None else time.time())
    apple = provider.preset == "apple"
    claims = {"iss": provider.client_assertion_issuer or provider.client_id, "sub": provider.client_id,
              "aud": (provider.issuer if apple else token_endpoint), "iat": issued,
              "exp": issued + ASSERTION_SECONDS, "jti": _secrets.token_urlsafe(16)}
    headers = {"kid": provider.client_key_kid} if provider.client_key_kid else {}
    return jwt.encode(claims, _load_private_key(pem), algorithm=client_key_alg(pem), headers=headers)


def _client_authentication(db: Any, provider: Provider, token_endpoint: str) -> dict[str, str]:
    """The token request's client authentication fields (never both a secret and an assertion)."""
    if provider.client_auth == "private_key_jwt":
        pem = _vaulted(db, provider, provider.client_key_vault_item_id)
        if not pem:
            raise SsoError("client_key_missing")
        assertion = client_assertion(provider, pem, token_endpoint=token_endpoint)
        if provider.preset == "apple":
            return {"client_secret": assertion}
        return {"client_assertion_type": ASSERTION_TYPE, "client_assertion": assertion}
    secret = _client_secret(db, provider)
    return {"client_secret": secret} if secret else {}


def _exchange(db: Any, provider: Provider, doc: dict[str, Any], *, code: str, verifier: str,
              origin: str) -> dict[str, Any]:
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri(origin, provider.id), "code_verifier": verifier,
            "client_id": provider.client_id or ""}
    form.update(_client_authentication(db, provider, doc["token_endpoint"]))
    try:
        resp = _fetch("POST", doc["token_endpoint"], data=urlencode(form).encode("ascii"),
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "Accept": "application/json"}, purpose="oidc.token")
    except safe_http.OutboundRefused:
        raise SsoError("token_refused") from None
    tokens = _json(resp, "token")
    if tokens.get("error"):
        raise SsoError("token_failed")
    return tokens


def _verify_id_token(provider: Provider, doc: dict[str, Any], id_token: Any, *, nonce: str) -> FederatedIdentity:
    import jwt

    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise SsoError("id_token_missing")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise SsoError("id_token_invalid") from None
    alg = str(header.get("alg") or "")
    allowed = SAFE_ALGS.intersection(provider.allowed_algs or ())
    if alg not in allowed:
        raise SsoError("alg_not_allowed")                     # A62: none, HS*, or not configured
    key = _key_for(doc["jwks_uri"], header.get("kid"), alg)  # jku/x5u/jwk headers are never read
    try:
        claims = jwt.decode(id_token, key, algorithms=[alg], audience=provider.client_id,
                            issuer=provider.issuer, leeway=CLOCK_SKEW_SECONDS,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.InvalidAudienceError:
        raise SsoError("audience_mismatch") from None
    except jwt.InvalidIssuerError:
        raise SsoError("issuer_mismatch") from None
    except jwt.PyJWTError:
        raise SsoError("id_token_invalid") from None
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    azp = claims.get("azp")
    if len(audiences) > 1 and azp is None:
        raise SsoError("azp_missing")                         # A63
    if azp is not None and azp != provider.client_id:
        raise SsoError("azp_mismatch")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise SsoError("nonce_mismatch")                      # A63
    amr = tuple(str(a) for a in (claims.get("amr") or []) if isinstance(a, str))
    groups_raw = claims.get(provider.groups_claim) if provider.groups_claim else None
    groups = tuple(str(g) for g in groups_raw) if isinstance(groups_raw, list) else ()
    verified = claims.get("email_verified")
    return FederatedIdentity(
        issuer=str(claims["iss"]), subject=str(claims["sub"]), email=claims.get("email"),
        email_verified=verified is True or verified == "true", name=claims.get("name"),
        amr=amr, groups=groups, mfa=bool(provider.trust_idp_mfa and MFA_AMR.intersection(amr)))


def _github_identity(tokens: dict[str, Any]) -> FederatedIdentity:
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise SsoError("token_failed")
    headers = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
    try:
        user = _json(_fetch("GET", PRESETS["github"]["user_endpoint"], headers=headers,
                            purpose="oauth.github.user"), "userinfo")
        emails_resp = _fetch("GET", PRESETS["github"]["emails_endpoint"], headers=headers,
                             purpose="oauth.github.emails")
    except safe_http.OutboundRefused:
        raise SsoError("userinfo_refused") from None
    gid = user.get("id")
    if not isinstance(gid, int) or isinstance(gid, bool):
        raise SsoError("userinfo_invalid")
    email, verified = None, False
    if emails_resp.status == 200:
        try:
            for item in json.loads(emails_resp.body):
                if isinstance(item, dict) and item.get("primary") and item.get("verified"):
                    email, verified = item.get("email"), True
        except ValueError:
            pass
    return FederatedIdentity(issuer=GITHUB_ISSUER, subject=str(gid), email=email,
                             email_verified=verified, name=user.get("name") or user.get("login"))
