"""GitHub for agent runs: per-run, down-scoped App installation tokens; the user's own OAuth only
when the grant says "as me on GitHub"; the user-repo-access guard. Owned by S3. IDENTITY.md §7.9.

  - A run's installation token is minted with `repositories=[the run's repo(s)]` and
    `permissions = grant.github ∩ every passport.external_ceiling in the chain ∩ step needs`
    (`metadata: read` always). GitHub refusing a permission (422) is a refusal here: BOSS never
    retries with the installation's full permission set (today's adapter does; this does not).
  - The token is held in THIS process's memory (the egress proxy's), never in the vault, never in a
    sandbox, and revoked at run end and by the cascade (`DELETE /installation/token`).
  - The user's own OAuth (user-to-server, rotating refresh) is vaulted; `vault.REFRESHERS["github"]`
    and `vault.REVOKERS["github"]` are installed by `install_provider_hooks()`.
  - The guard (D28): on multi-person installs, a grant naming repos needs the PERSON to have that
    access on GitHub, checked with their own token (`GET /repos/{o}/{r}` -> permissions).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import jwks
from .principal import RunTokenClaims, TaskBinding

log = logging.getLogger(__name__)

API_BASE_ENV = "BOSS_GITHUB_API_URL"
APP_ID_ENV = "BOSS_GITHUB_APP_ID"
INSTALLATION_ENV = "BOSS_GITHUB_APP_INSTALLATION_ID"
CLIENT_ID_ENV = "BOSS_GITHUB_APP_CLIENT_ID"
CLIENT_SECRET_ENV = "BOSS_GITHUB_APP_CLIENT_SECRET"
LEVELS = {"none": 0, "read": 1, "write": 2}
NAMES = {0: "none", 1: "read", 2: "write"}


class GitHubRefused(Exception):
    def __init__(self, code: str, status: int = 403):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = field(repr=False)

    def json(self) -> Any:
        return json.loads(self.body or b"null")


def _default_transport(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]) -> Response:
    """S1's SSRF-safe client. Until it lands, the fixed public GitHub hosts only go through httpx;
    any other host (an Enterprise base URL) is refused rather than fetched unsafely."""
    from . import http

    try:
        r = http.fetch(method, url, headers=headers, data=body, purpose="github")
        return Response(r.status, r.body)
    except NotImplementedError:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ("api.github.com", "github.com") or urlsplit(url).scheme != "https":
            raise GitHubRefused("outbound_client_unavailable", 503)
        import httpx

        resp = httpx.request(method, url, headers=dict(headers), content=body, timeout=10.0,
                             follow_redirects=False)
        return Response(resp.status_code, resp.content)


#: Replaced in tests by a recording mock. Signature: (method, url, headers, body) -> Response.
TRANSPORT: Callable[[str, str, Mapping[str, str], Optional[bytes]], Response] = _default_transport


def api_base() -> str:
    return (os.environ.get(API_BASE_ENV) or "https://api.github.com").rstrip("/")


def _call(method: str, path: str, *, token: str, scheme: str = "Bearer", body: Any = None) -> Response:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "boss-identity"}
    if token:
        headers["Authorization"] = f"{scheme} {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    url = path if path.startswith("https://") else f"{api_base()}{path}"
    return TRANSPORT(method, url, headers, data)


# ---- the App ----------------------------------------------------------------------------------------

def _app_private_key_pem(db: Any) -> str:
    from sqlalchemy import text

    from . import vault

    item = db.execute(text("SELECT id FROM identity_vault_items WHERE kind = 'app_private_key' AND provider = 'github' "
                           "AND status = 'active' ORDER BY created_at DESC LIMIT 1")).scalar() if db is not None else None
    if item:
        return vault.read_secret(item, db=db, kind=vault.VaultItemKind.APP_PRIVATE_KEY)
    raise GitHubRefused("app_not_configured", 503)


def app_jwt(db: Any, *, now: Optional[int] = None) -> str:
    """RS256 App JWT (GitHub's format; BOSS's own tokens are EdDSA)."""
    from .github_setup import app_config

    app_id = app_config(db)["app_id"]
    if not app_id:
        raise GitHubRefused("app_not_configured", 503)
    key = serialization.load_pem_private_key(_app_private_key_pem(db).encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubRefused("app_key_not_rsa", 503)
    now = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    signing_input = f"{jwks.b64u(jwks.canonical(header))}.{jwks.b64u(jwks.canonical(claims))}".encode()
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode() + "." + jwks.b64u(sig)


@dataclass(frozen=True)
class InstallationToken:
    token: str = field(repr=False)
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    repositories: tuple[str, ...] = ()
    permissions: Mapping[str, str] = field(default_factory=dict)
    #: "installation" (the App's, revoked at run end) | "as_me" (the person's own connection) |
    #: "org_token" (the org's fine-grained token). Only an installation token is revoked at GitHub.
    kind: str = "installation"


def mint_installation_token(repos: list[str], permissions: Mapping[str, str], *, db: Any) -> InstallationToken:
    from .github_setup import app_config

    inst = app_config(db)["installation_id"]
    if not inst:
        raise GitHubRefused("app_not_installed", 503)
    if not repos:
        raise GitHubRefused("no_repo", 403)
    owners = {r.split("/", 1)[0] for r in repos}
    if len(owners) != 1:
        raise GitHubRefused("one_owner_per_token", 403)
    body = {"repositories": [r.split("/", 1)[1] for r in repos], "permissions": dict(permissions)}
    resp = _call("POST", f"/app/installations/{inst}/access_tokens", token=app_jwt(db), body=body)
    if resp.status != 201 and resp.status != 200:
        # 422 "permissions requested are not granted": refuse. Never widen to the installation's set.
        raise GitHubRefused(f"mint_failed_{resp.status}", 502 if resp.status >= 500 else 403)
    data = resp.json()
    exp = datetime.fromisoformat(str(data.get("expires_at", "")).replace("Z", "+00:00")) \
        if data.get("expires_at") else datetime.now(timezone.utc) + timedelta(hours=1)
    return InstallationToken(token=str(data["token"]), expires_at=exp, repositories=tuple(repos),
                             permissions=dict(permissions))


def revoke_installation_token(token: str) -> bool:
    try:
        resp = _call("DELETE", "/installation/token", token=token)
    except Exception:  # noqa: BLE001
        log.warning("installation token revocation failed (network)")
        return False
    return resp.status in (204, 401)    # 401: already dead


# ---- what a run may do on GitHub --------------------------------------------------------------------

def run_repos(task: TaskBinding) -> list[str]:
    repos = (task.resources or {}).get("github") or []
    return [r for r in repos if isinstance(r, str)]


#: What a run with no declared GitHub needs may do: read.
DEFAULT_NEEDS: Mapping[str, str] = {"contents": "read", "pull_requests": "read", "issues": "read",
                                    "checks": "read", "statuses": "read", "actions": "read"}


def run_permissions(claims: RunTokenClaims, *, db: Any) -> dict[str, str]:
    """grant.github ∩ external_ceiling(every agent in the chain) ∩ step needs. Min level per key."""
    from . import grants, passports

    grant = grants.load(claims.grant_id, db=db)
    sources: list[Mapping[str, Any]] = []
    if grant is not None:
        sources.append(grants.github_resources(grant))
    agent_ids = list(claims.chain.agent_ids) or ([claims.sub] if claims.sub.startswith("agt_") else [])
    for aid in agent_ids:
        p = passports.get(aid, db=db)
        sources.append(((p.external_ceiling if p else {}) or {}).get("github") or {})
    needs = (claims.task.resources or {}).get("github_permissions")
    # A run that declared no needs gets READ ONLY, never the grant's full GitHub authority (#M):
    # a build declares what it writes (runs.BUILD_GITHUB_NEEDS); a step that says nothing reads.
    sources.append(needs if isinstance(needs, Mapping) else DEFAULT_NEEDS)
    keys = set().union(*[set(s) for s in sources]) if sources else set()
    out: dict[str, str] = {}
    for key in keys:
        if key not in grants.GITHUB_PERMISSIONS:
            continue
        level = min(LEVELS.get(str(s.get(key, "none")), 0) for s in sources)
        if level:
            out[key] = NAMES[level]
    out["metadata"] = "read"
    return out


def push_ref_pattern(claims: RunTokenClaims, *, db: Any) -> str:
    """Run tokens push only to `refs/heads/boss/<run-id>-*` (§7.9); the passport may narrow further."""
    safe = claims.task.run_id.replace(":", "-").replace("/", "-").replace("#", "-")
    return f"refs/heads/boss/{safe}-*"


# ---- per-run tokens: minted lazily, held in memory, revoked at run end ----------------------------------

_RUN_TOKENS: dict[str, list[InstallationToken]] = {}
_lock = threading.Lock()


def mint_for_run(claims: RunTokenClaims, *, task: TaskBinding, db: Any):
    """A short-lived installation token for this run's repos, with the run's minimum permissions.
    Re-checks the grant's repos and the user-repo-access guard at run start."""
    from . import grants, vault

    repos = run_repos(task)
    if not repos:
        raise GitHubRefused("no_repo")
    grant = grants.load(claims.grant_id, db=db)
    if claims.mode.value == "obo":
        gh = grants.github_resources(grant)
        if not set(repos) <= set(gh.get("repos") or ()):
            raise GitHubRefused("repo_outside_grant")
        # At run start the guard asks GitHub NOW (no 15-minute cache): access the person lost a
        # minute ago is not lent to their agent.
        grants.check_user_repo_access(claims.org_id, claims.sub, gh, db=db, use_cache=False)
    perms = run_permissions(claims, db=db)
    if set(perms) == {"metadata"}:
        raise GitHubRefused("no_github_permissions")
    tok = _credential_for_run(claims, repos, perms, db=db)
    with _lock:
        _RUN_TOKENS.setdefault(task.run_id, []).append(tok)
    return vault.ShortLivedCredential(token=tok.token, expires_at=tok.expires_at, provider="github",
                                      scopes=frozenset(f"{k}:{v}" for k, v in perms.items()))


def _credential_for_run(claims: RunTokenClaims, repos: list[str], perms: Mapping[str, str], *,
                        db: Any) -> InstallationToken:
    """WHICH GitHub credential egress uses for this run (Setup › Repo; github_setup):
      1. the person's own connection, when their grant says "act as me on GitHub";
      2. the org's GitHub App: an installation token for exactly these repos and permissions;
      3. the org's fine-grained token, only when it was given for every one of these repos.
    None of them ever reaches the sandbox: egress injects it and applies every rule to the request."""
    from . import grants, github_setup, tokens, vault

    if claims.mode.value == "obo" and claims.grant_id and grants.allows_as_user(claims.grant_id, "github", db=db):
        try:
            cred = vault.get_credential(vault.ConnectionRef("github", claims.sub),
                                        vault.CredentialPurpose.GITHUB_AS_USER,
                                        ctx=tokens.context_for(claims, db=db), db=db)
        except (vault.VaultNotFound, vault.VaultNeedsReauth):
            raise GitHubRefused("as_me_not_connected", 409) from None
        return InstallationToken(token=cred.token, expires_at=cred.expires_at, repositories=tuple(repos),
                                 permissions=dict(perms), kind="as_me")
    if github_setup.app_ready(db, claims.org_id):
        return mint_installation_token(repos, perms, db=db)
    org = github_setup.org_token(db, claims.org_id)
    if org is not None:
        if not {r.lower() for r in repos} <= set(org["repos"]):
            raise GitHubRefused("repo_outside_org_token")
        return InstallationToken(token=vault.org_github_token(org["id"], db=db),
                                 expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                                 repositories=tuple(repos), permissions=dict(perms), kind="org_token")
    raise GitHubRefused("github_not_connected", 503)


def cached_for_run(run_id: str) -> Optional[InstallationToken]:
    with _lock:
        toks = [t for t in _RUN_TOKENS.get(run_id, []) if t.expires_at - datetime.now(timezone.utc) > timedelta(minutes=5)]
    return toks[-1] if toks else None


def revoke_runs(run_ids: list[str], reason: str = "") -> int:
    """Revoke at GitHub every installation token minted for these runs or their children."""
    with _lock:
        doomed: list[InstallationToken] = []
        for rid in list(_RUN_TOKENS):
            if any(rid == r or rid.startswith(r + "/") for r in run_ids):
                doomed.extend(_RUN_TOKENS.pop(rid))
    count = 0
    for tok in doomed:
        if tok.kind != "installation":
            continue                         # a person's or the org's own token is never revoked by a run
        if revoke_installation_token(tok.token):
            count += 1
    return count


def install_cascade_hook() -> None:
    from . import cascade

    cascade.register_after_commit(revoke_runs)


# ---- the user's own GitHub identity -----------------------------------------------------------------

def user_has_repo_access(person_id: str, repo: str, need: str, *, db: Any) -> bool:
    """`need` is "pull" or "push". Checked with the person's own token; no token means no."""
    from . import vault

    token = vault.token_for_access_check(person_id, provider="github", db=db)
    if not token:
        return False
    resp = _call("GET", f"/repos/{repo}", token=token)
    if resp.status != 200:
        return False
    perms = (resp.json() or {}).get("permissions") or {}
    if need == "push":
        return bool(perms.get("push") or perms.get("maintain") or perms.get("admin"))
    return bool(perms.get("pull") or perms.get("push") or perms.get("admin"))


def _oauth_refresh(refresh_token: str) -> tuple[str, Optional[str], int]:
    from . import vault

    cid, secret = _client_credentials()
    if not cid or not secret:
        raise vault.RefreshRejected("client_not_configured")
    body = f"client_id={cid}&client_secret={secret}&grant_type=refresh_token&refresh_token={refresh_token}"
    resp = TRANSPORT("POST", f"{_web_base()}/login/oauth/access_token",
                     {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                     body.encode())
    data = resp.json() if resp.status == 200 else {}
    if not data or data.get("error") or not data.get("access_token"):
        # bad_refresh_token = revoked, expired, or ALREADY USED (GitHub rotates): reuse detection.
        raise vault.RefreshRejected(str(data.get("error") or resp.status))
    return str(data["access_token"]), data.get("refresh_token"), int(data.get("expires_in") or 28800)


def _oauth_revoke(access_token: str, refresh_token: Optional[str]) -> None:
    cid, secret = _client_credentials()
    if not cid or not secret:
        return
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    _call("DELETE", f"/applications/{cid}/grant", token=basic, scheme="Basic",
          body={"access_token": access_token})


def install_provider_hooks() -> None:
    from . import vault

    vault.REFRESHERS.setdefault("github", _oauth_refresh)
    vault.REVOKERS.setdefault("github", _oauth_revoke)


def commit_trailers(*, person_name: str, person_email: Optional[str], grant_id: Optional[str], run_id: str) -> str:
    """`On-behalf-of: Alice <alice@acme.com>` and `Boss-Lineage: dlg_…/run_…` (§7.9)."""
    who = f"{person_name} <{person_email}>" if person_email else person_name
    return f"On-behalf-of: {who}\nBoss-Lineage: {grant_id or 'autonomous'}/{run_id}"


# ---- connecting a person's own GitHub (user-to-server OAuth) ---------------------------------------------

def _web_base() -> str:
    from .github_setup import web_base

    return web_base()


def _client_credentials() -> tuple[Optional[str], Optional[str]]:
    """The App's OAuth client: the env (an operator's explicit setting) or the App made in Setup."""
    cid, secret = os.environ.get(CLIENT_ID_ENV), os.environ.get(CLIENT_SECRET_ENV)
    if cid and secret:
        return cid, secret
    try:
        from ..db import begin
        from .github_setup import app_config, client_secret

        with begin() as db:
            return app_config(db)["client_id"], client_secret(db)
    except Exception:  # noqa: BLE001 — not configured
        return None, None


STATE_TTL_SECONDS = 600


def _state_mac(payload: str) -> str:
    import hashlib
    import hmac

    from . import vault

    return jwks.b64u(hmac.new(vault.derived_key("oauth-state"), payload.encode(), hashlib.sha256).digest())


def oauth_state(person_id: str, provider: str = "github", *, now: Optional[int] = None) -> str:
    """Signed, expiring, single-use (checked at the callback), bound to the person who asked."""
    import secrets as _secrets

    now = int(time.time()) if now is None else now
    payload = jwks.b64u(json.dumps({"p": person_id, "v": provider, "e": now + STATE_TTL_SECONDS,
                                    "n": _secrets.token_urlsafe(12)}, separators=(",", ":")).encode())
    return f"{payload}.{_state_mac(payload)}"


def authorize_url(person_id: str, redirect_uri: str) -> str:
    from urllib.parse import urlencode

    cid = _client_credentials()[0]
    if not cid:
        raise GitHubRefused("provider_not_configured", 409)
    q = urlencode({"client_id": cid, "redirect_uri": redirect_uri, "state": oauth_state(person_id),
                   "allow_signup": "false"})
    return f"{_web_base()}/login/oauth/authorize?{q}"


def complete_oauth(*, person_id: str, org_id: str, code: str, state: str, redirect_uri: str, db: Any) -> str:
    """Verify the state (signature, expiry, the SAME person, single use), exchange the code, store
    the token and its rotating refresh in the vault. Returns the vault item id."""
    import hmac
    from urllib.parse import urlencode

    from sqlalchemy import text

    from . import audit, vault

    try:
        payload, mac = state.split(".", 1)
        data = json.loads(jwks.b64u_decode(payload))
    except Exception as exc:  # noqa: BLE001
        raise GitHubRefused("bad_state", 400) from exc
    if not hmac.compare_digest(mac, _state_mac(payload)) or data.get("p") != person_id or data.get("v") != "github":
        raise GitHubRefused("bad_state", 400)
    if int(data.get("e", 0)) < int(time.time()):
        raise GitHubRefused("state_expired", 400)
    fresh = db.execute(text("INSERT INTO identity_dpop_replay (jkt, jti, expires_at) VALUES ('oauth-state', :n, "
                            "to_timestamp(:e)) ON CONFLICT DO NOTHING RETURNING 1"),
                       {"n": str(data.get("n")), "e": int(data["e"])}).scalar()
    if not fresh:
        raise GitHubRefused("state_replayed", 400)
    cid, secret = _client_credentials()
    if not cid or not secret:
        raise GitHubRefused("provider_not_configured", 409)
    body = urlencode({"client_id": cid, "client_secret": secret, "code": code, "redirect_uri": redirect_uri}).encode()
    resp = TRANSPORT("POST", f"{_web_base()}/login/oauth/access_token",
                     {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}, body)
    tok = resp.json() if resp.status == 200 else {}
    if not tok or tok.get("error") or not tok.get("access_token"):
        raise GitHubRefused("code_exchange_failed", 400)
    me = _call("GET", "/user", token=str(tok["access_token"]))
    login = (me.json() or {}).get("login") if me.status == 200 else None
    item = vault.store(org_id=org_id, owner_principal_id=person_id, kind=vault.VaultItemKind.OAUTH_USER,
                       provider="github", secret=str(tok["access_token"]), refresh=tok.get("refresh_token"),
                       access_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"])))
                       if tok.get("expires_in") else None,
                       provider_subject=login, connection_name="GitHub", db=db,
                       granted_scopes=[s for s in str(tok.get("scope") or "").split(",") if s])
    audit.record(audit.SecurityEvent(org_id=org_id, event_type="vault.connection.linked",
                                     outcome=audit.Outcome.SUCCESS, actor_principal_id=person_id, actor_kind="user",
                                     target_type="vault_item", target_id=item, details={"provider": "github"}), db=db)
    return item
