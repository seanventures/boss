"""How BOSS reaches GitHub for builds (Setup › Repo). Three ways, all used ONLY by boss-egress —
a build's sandbox never holds a GitHub credential:

  app     a GitHub App created from a manifest (recommended): per-run installation tokens, scoped
          to the run's repos and the run's permissions, revoked when the run ends;
  as_me   a person's own GitHub connection (My account › Agents), for runs whose grant says
          "act as me on GitHub";
  token   a fine-grained personal access token held in the vault as an ORG connection, scoped to
          the repos it was given for. Less safe than the App: it is long-lived and acts as whoever
          created it; egress still applies every rule (refs, paths, PR merges) to it.

An install that still carries BOSS_GITHUB_TOKEN can move it into the vault as the `token`
connection in one click; after that, under enforce, the environment variable is not read again.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, Iterable, Optional

from . import audit, github_app, jwks, vault

WEB_BASE_ENV = "BOSS_GITHUB_WEB_URL"           # github.com; a GitHub Enterprise or test host
MIGRATED_NAME = "Moved from BOSS_GITHUB_TOKEN"
ORG_TOKEN_NAME = "Fine-grained token (org)"
REPO_PREFIX = "repo:"
#: What the App asks for: write code and PRs, read what a review needs. Never admin, never workflows.
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read",
                   "metadata": "read", "issues": "read", "actions": "read"}


class GitHubSetupError(Exception):
    def __init__(self, code: str, status: int = 400, **extra: Any):
        super().__init__(code)
        self.code, self.status, self.extra = code, status, extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, **self.extra}


def may_change_org_credentials(org_id: str, *, db: Any = None) -> bool:
    """D1: the org's GitHub credentials (the org token, its repo scope, the App) are an ORG-scope
    setting. Inside an enforced request the caller must hold `github:connect` at the ORG through
    the PDP (step-up included) — a project-bound Admin holds it only at their project and never
    changes, widens or revokes what every other project builds with. Outside a request (the host
    shell, a migration) the in-process caller is trusted, as everywhere else."""
    from . import authz, lineage
    from .permissions_gen import P
    from .scope import ResolvedScope

    enforced = lineage.request_enforced()
    if not enforced:
        return True
    ctx = lineage.current_context()
    if ctx is None or ctx.principal.org_id != org_id:
        return False
    return authz.check(ctx, P.GITHUB_CONNECT, ResolvedScope.org_level(org_id), db=db).allowed


def require_org_credentials_authority(org_id: str, *, db: Any = None) -> None:
    if not may_change_org_credentials(org_id, db=db):
        raise GitHubSetupError(
            "org_scope_required", 403, permission="github:connect",
            message="The organisation's GitHub connection is used by every project, so changing it "
                    "needs github:connect for the whole organisation (an Owner or an org Admin).")


def _sql(statement: str):
    from sqlalchemy import text

    return text(statement)


def web_base() -> str:
    return (os.environ.get(WEB_BASE_ENV) or "https://github.com").rstrip("/")


# ---- the App's configuration (env first: an operator's explicit choice wins) ---------------------------

def app_config(db: Any, org_id: Optional[str] = None) -> dict[str, Optional[str]]:
    env = {"app_id": os.environ.get(github_app.APP_ID_ENV),
           "installation_id": os.environ.get(github_app.INSTALLATION_ENV),
           "client_id": os.environ.get(github_app.CLIENT_ID_ENV)}
    if env["app_id"]:
        return {**env, "source": "env", "slug": None, "html_url": None, "account_login": None}
    row = None
    if db is not None:
        q = "SELECT app_id, installation_id, client_id, slug, html_url, account_login FROM identity_github_app"
        row = (db.execute(_sql(q + " WHERE org_id = :o"), {"o": org_id}).mappings().first() if org_id
               else db.execute(_sql(q + " ORDER BY created_at LIMIT 1")).mappings().first())
    if row is None:
        return {"app_id": None, "installation_id": None, "client_id": None, "source": None, "slug": None,
                "html_url": None, "account_login": None}
    return {**dict(row), "source": "setup"}


def app_ready(db: Any, org_id: Optional[str] = None) -> bool:
    cfg = app_config(db, org_id)
    if not (cfg["app_id"] and cfg["installation_id"]):
        return False
    return db.execute(_sql("SELECT 1 FROM identity_vault_items WHERE kind = 'app_private_key' AND "
                           "provider = 'github' AND status = 'active' LIMIT 1")).first() is not None


# ---- state for the manifest and install redirects (signed, expiring, single use, bound to the person) --

def _state(person_id: str, purpose: str) -> str:
    payload = jwks.b64u(json.dumps({"p": person_id, "v": purpose, "e": int(time.time()) + github_app.STATE_TTL_SECONDS,
                                    "n": secrets.token_urlsafe(12)}, separators=(",", ":")).encode())
    return f"{payload}.{github_app._state_mac(payload)}"  # noqa: SLF001 — the same vault-derived key


def _check_state(state: str, purpose: str, *, db: Any, consume: bool = True) -> str:
    import hmac

    try:
        payload, mac = state.split(".", 1)
        data = json.loads(jwks.b64u_decode(payload))
    except Exception as exc:  # noqa: BLE001
        raise GitHubSetupError("bad_state") from exc
    if not hmac.compare_digest(mac, github_app._state_mac(payload)) or data.get("v") != purpose:  # noqa: SLF001
        raise GitHubSetupError("bad_state")
    if int(data.get("e", 0)) < int(time.time()):
        raise GitHubSetupError("state_expired")
    if consume:
        fresh = db.execute(_sql("INSERT INTO identity_dpop_replay (jkt, jti, expires_at) VALUES (:k, :n, "
                                "to_timestamp(:e)) ON CONFLICT DO NOTHING RETURNING 1"),
                           {"k": f"gh-{purpose}", "n": str(data.get("n")), "e": int(data["e"])}).scalar()
        if not fresh:
            raise GitHubSetupError("state_replayed")
    return str(data.get("p") or "")


# ---- (a) the GitHub App, from a manifest -----------------------------------------------------------------

def manifest(*, origin: str, person_id: str, owner: Optional[str] = None) -> dict[str, Any]:
    """What the console POSTs to GitHub (a form with `manifest` = this JSON) to create the App.

    No webhook: BOSS subscribes to no events (default_events is empty), and GitHub refuses a
    manifest whose hook URL is not reachable from the public internet, so on a laptop or a
    private install (localhost, a tailnet name, a private IP) even an inactive hook made the
    App impossible to create."""
    if not origin:
        raise GitHubSetupError("public_url_required", 409,
                               message="Set BOSS_PUBLIC_URL first: GitHub sends you back to it.")
    host = origin.split("//", 1)[-1]
    body = {
        "name": f"BOSS {host}"[:34],
        "url": origin,
        "redirect_url": f"{origin}/v1/identity/github/app/created",
        "callback_urls": [f"{origin}/v1/identity/me/connections/github/callback"],
        "setup_url": f"{origin}/v1/identity/github/app/installed",
        "setup_on_update": True,
        "public": False,
        "request_oauth_on_install": False,
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": [],
    }
    state = _state(person_id, "app-manifest")
    path = f"/organizations/{owner}/settings/apps/new" if owner else "/settings/apps/new"
    return {"post_url": f"{web_base()}{path}?state={state}", "manifest": body, "state": state}


def complete_manifest(*, code: str, state: str, org_id: str, db: Any) -> dict[str, Any]:
    """GitHub sent the person back with a one-time `code`: convert it into the App's id and keys,
    keep the keys in the vault, and send the person on to install it."""
    person = _check_state(state, "app-manifest", db=db)
    if not code or len(code) > 100 or not all(c.isalnum() or c in "-_" for c in code):
        raise GitHubSetupError("bad_code")
    resp = github_app._call("POST", f"/app-manifests/{code}/conversions", token="", scheme="")  # noqa: SLF001
    if resp.status not in (200, 201):
        raise GitHubSetupError("manifest_conversion_failed", 502, status=resp.status)
    data = resp.json() or {}
    pem, app_id = str(data.get("pem") or ""), str(data.get("id") or "")
    if not pem or not app_id:
        raise GitHubSetupError("manifest_conversion_failed", 502)
    db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'app_private_key' "
                    "AND provider = 'github' AND status = 'active'"))
    key_item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.APP_PRIVATE_KEY,
                           provider="github", secret=pem, connection_name=f"GitHub App {data.get('slug') or app_id}",
                           db=db)
    if data.get("client_secret"):
        db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'oidc_client_secret' "
                        "AND provider = 'github-app' AND status = 'active'"))
        vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET,
                    provider="github-app", secret=str(data["client_secret"]), connection_name="GitHub App OAuth client",
                    db=db)
    owner = (data.get("owner") or {}).get("login")
    db.execute(_sql(
        "INSERT INTO identity_github_app (org_id, app_id, slug, html_url, client_id, account_login, created_by) "
        "VALUES (:o, :a, :s, :h, :c, :l, :by) ON CONFLICT (org_id) DO UPDATE SET app_id = EXCLUDED.app_id, "
        "slug = EXCLUDED.slug, html_url = EXCLUDED.html_url, client_id = EXCLUDED.client_id, "
        "account_login = EXCLUDED.account_login, installation_id = NULL, updated_at = now()"),
        {"o": org_id, "a": app_id, "s": data.get("slug"), "h": data.get("html_url"),
         "c": data.get("client_id"), "l": owner, "by": person})
    _event(db, org_id, person, "github_app", app_id, {"how": "app", "step": "created", "slug": data.get("slug"),
                                                     "key_item": key_item})
    slug = data.get("slug") or app_id
    install = f"{web_base()}/apps/{slug}/installations/new?state={_state(person, 'app-install')}"
    return {"app_id": app_id, "slug": slug, "install_url": install}


def complete_install(*, installation_id: str, state: Optional[str], org_id: str, db: Any) -> dict[str, Any]:
    """GitHub sent the person back after installing the App: keep the installation once the App's
    own JWT confirms it is one of ITS installations."""
    person = _check_state(state or "", "app-install", db=db)      # no state, no change: the picker is the other way
    return select_installation(installation_id=installation_id, org_id=org_id, db=db, by=person)


def installations(*, db: Any, org_id: str) -> list[dict[str, Any]]:
    if not app_config(db, org_id)["app_id"]:
        raise GitHubSetupError("app_not_configured", 409)
    resp = github_app._call("GET", "/app/installations", token=github_app.app_jwt(db))  # noqa: SLF001
    if resp.status != 200:
        raise GitHubSetupError("installations_unavailable", 502, status=resp.status)
    return [{"id": str(i.get("id")), "account": (i.get("account") or {}).get("login"),
             "repository_selection": i.get("repository_selection")} for i in (resp.json() or [])]


def select_installation(*, installation_id: str, org_id: str, db: Any, by: str) -> dict[str, Any]:
    require_org_credentials_authority(org_id, db=db)
    installation_id = str(installation_id or "").strip()
    if not installation_id.isdigit():
        raise GitHubSetupError("bad_installation", 422)
    resp = github_app._call("GET", f"/app/installations/{installation_id}", token=github_app.app_jwt(db))  # noqa: SLF001
    if resp.status != 200:
        raise GitHubSetupError("not_this_apps_installation", 403)
    account = ((resp.json() or {}).get("account") or {}).get("login")
    db.execute(_sql("UPDATE identity_github_app SET installation_id = :i, account_login = coalesce(:l, "
                    "account_login), updated_at = now() WHERE org_id = :o"),
               {"i": installation_id, "l": account, "o": org_id})
    _event(db, org_id, by, "github_app", installation_id, {"how": "app", "step": "installed", "account": account})
    return {"installation_id": installation_id, "account": account}


def disconnect_app(*, org_id: str, db: Any, by: str) -> None:
    require_org_credentials_authority(org_id, db=db)
    db.execute(_sql("DELETE FROM identity_github_app WHERE org_id = :o"), {"o": org_id})
    db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE provider IN ('github', 'github-app') "
                    "AND kind IN ('app_private_key', 'oidc_client_secret') AND status = 'active'"))
    _event(db, org_id, by, "github_app", "-", {"how": "app", "step": "disconnected"}, revoked=True)


def client_secret(db: Any) -> Optional[str]:
    row = db.execute(_sql("SELECT id FROM identity_vault_items WHERE kind = 'oidc_client_secret' AND "
                          "provider = 'github-app' AND status = 'active' ORDER BY created_at DESC LIMIT 1")).first()
    return vault.read_secret(row[0], db=db, kind=vault.VaultItemKind.OIDC_CLIENT_SECRET) if row else None


# ---- (c) a fine-grained token as an org connection --------------------------------------------------------

def _repos(values: Iterable[str]) -> list[str]:
    out = []
    for r in values or ():
        r = str(r).strip().lower()
        if r.count("/") == 1 and all(part and part not in (".", "..") for part in r.split("/")):
            out.append(r)
    return sorted(set(out))


def org_token(db: Any, org_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    q = ("SELECT id, connection_name, granted_scopes, provider_subject, created_at FROM identity_vault_items "
         "WHERE kind = 'legacy_pat' AND owner_principal_id IS NULL AND provider = 'github' AND status = 'active'")
    row = (db.execute(_sql(q + " AND org_id = :o ORDER BY created_at DESC LIMIT 1"), {"o": org_id}).mappings().first()
           if org_id else db.execute(_sql(q + " ORDER BY created_at DESC LIMIT 1")).mappings().first())
    if row is None:
        return None
    repos = [s[len(REPO_PREFIX):] for s in (row["granted_scopes"] or []) if s.startswith(REPO_PREFIX)]
    return {"id": row["id"], "repos": repos, "login": row["provider_subject"],
            "migrated": row["connection_name"] == MIGRATED_NAME, "created_at": row["created_at"]}


def store_org_token(*, token: str, repos: Iterable[str], org_id: str, db: Any, by: str,
                    migrated: bool = False, check: bool = True) -> dict[str, Any]:
    require_org_credentials_authority(org_id, db=db)
    token = (token or "").strip()
    if not token or len(token) > 400 or any(c.isspace() for c in token):
        raise GitHubSetupError("bad_token", 422)
    wanted = _repos(repos)
    if not wanted:
        raise GitHubSetupError("repos_required", 422, message="Name the repositories this token is for.")
    login = None
    if check:
        me = github_app._call("GET", "/user", token=token)  # noqa: SLF001
        if me.status != 200:
            raise GitHubSetupError("token_refused_by_github", 422)
        login = (me.json() or {}).get("login")
        for repo in wanted:
            r = github_app._call("GET", f"/repos/{repo}", token=token)  # noqa: SLF001
            if r.status != 200:
                raise GitHubSetupError("token_cannot_see_repo", 422, repo=repo)
    db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' AND "
                    "owner_principal_id IS NULL AND provider = 'github' AND org_id = :o AND status = 'active'"),
               {"o": org_id})
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.LEGACY_PAT,
                       provider="github", secret=token, provider_subject=login,
                       connection_name=MIGRATED_NAME if migrated else ORG_TOKEN_NAME,
                       granted_scopes=[REPO_PREFIX + r for r in wanted], db=db)
    _event(db, org_id, by, "vault_item", item, {"how": "token", "repos": wanted, "migrated": migrated})
    return {"id": item, "repos": wanted, "login": login, "migrated": migrated}


def add_repo_to_org_token(*, repo: str, org_id: str, db: Any) -> None:
    """Setup › Repo picked another repository for the stored token: the org connection covers it too."""
    require_org_credentials_authority(org_id, db=db)
    current = org_token(db, org_id)
    wanted = _repos([repo])
    if current is None or not wanted or wanted[0] in current["repos"]:
        return
    db.execute(_sql("UPDATE identity_vault_items SET granted_scopes = array_append(granted_scopes, :r) "
                    "WHERE id = :i"), {"r": REPO_PREFIX + wanted[0], "i": current["id"]})


def remove_org_token(*, org_id: str, db: Any, by: str) -> bool:
    require_org_credentials_authority(org_id, db=db)
    current = org_token(db, org_id)
    if current is None:
        return False
    db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE id = :i"), {"i": current["id"]})
    _event(db, org_id, by, "vault_item", current["id"], {"how": "token"}, revoked=True)
    return True


def migrate_env_token(*, repos: Iterable[str], org_id: str, db: Any, by: str, settings: Any) -> dict[str, Any]:
    token = (getattr(settings, "github_token", "") or os.environ.get("BOSS_GITHUB_TOKEN") or "").strip()
    if not token:
        raise GitHubSetupError("no_env_token", 409)
    out = store_org_token(token=token, repos=repos, org_id=org_id, db=db, by=by, migrated=True)
    forget_env_token(settings)
    return out


def forget_env_token(settings: Any) -> None:
    """Under enforce, once the token lives in the vault, this process stops reading the env copy."""
    from ..config import get_settings

    if (getattr(settings, "authz_mode", "") or "enforce").lower() != "enforce":
        return
    import sys

    holders = {id(settings): settings, id(get_settings()): get_settings()}
    main = sys.modules.get("apps.api.app.main")
    if main is not None and getattr(main, "settings", None) is not None:
        holders[id(main.settings)] = main.settings
    for s in holders.values():
        try:
            s.github_token = None
        except Exception:  # noqa: BLE001
            pass
    for name in ("BOSS_GITHUB_TOKEN", "GITHUB_TOKEN"):
        os.environ.pop(name, None)


def forget_env_token_if_moved(engine: Any, settings: Any) -> bool:
    """At boot (API and worker): a token already moved into the vault is not read from the env."""
    try:
        with engine.begin() as db:
            moved = db.execute(_sql("SELECT 1 FROM identity_vault_items WHERE kind = 'legacy_pat' AND "
                                    "owner_principal_id IS NULL AND connection_name = :n AND status = 'active'"),
                               {"n": MIGRATED_NAME}).first() is not None
    except Exception:  # noqa: BLE001
        return False
    if moved:
        forget_env_token(settings)
    return moved


# ---- the control plane's own GitHub writes (opening the PR, reading checks) -----------------------------

#: What BOSS's worker needs to open and follow a pull request; never admin, never workflows.
CONTROL_PLANE_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read",
                             "checks": "read", "statuses": "read"}


# Every installation token minted for the control plane is ACCOUNTED FOR: during a build it is
# registered under that build's run (github_app._RUN_TOKENS) and revoked at GitHub with the run's
# other tokens at run end (runs.end_run -> github_app.revoke_runs); outside a build (a Setup check,
# CI polling after the run) it is revoked at GitHub once it is CONTROL_PLANE_TTL_SECONDS old, at
# the next mint. GitHub's own 1-hour expiry is the backstop, never the plan.
CONTROL_PLANE_TTL_SECONDS = 600
_CP_RUN: "ContextVar[Optional[str]]" = ContextVar("boss_github_control_plane_run", default=None)
_CP_BOUND: dict[str, Any] = {}
_UNSCOPED: list[tuple[float, str]] = []
_cp_lock = threading.Lock()


def bind_control_plane_run(run_id: str) -> None:
    """From here until unbind (same thread), control-plane tokens belong to this run."""
    _CP_BOUND[run_id] = _CP_RUN.set(run_id)


def unbind_control_plane_run(run_id: str) -> None:
    token = _CP_BOUND.pop(run_id, None)
    try:
        if token is not None:
            _CP_RUN.reset(token)
            return
    except ValueError:                 # unbound from another context: just clear this one
        pass
    if _CP_RUN.get() == run_id:
        _CP_RUN.set(None)


def _account_for(tok: "github_app.InstallationToken") -> None:
    run_id = _CP_RUN.get()
    if run_id:
        with github_app._lock:  # noqa: SLF001 — the run's token list, revoked at run end
            github_app._RUN_TOKENS.setdefault(run_id, []).append(tok)  # noqa: SLF001
        return
    now = time.time()
    with _cp_lock:
        _UNSCOPED.append((now, tok.token))
        due = [t for at, t in _UNSCOPED if now - at >= CONTROL_PLANE_TTL_SECONDS]
        _UNSCOPED[:] = [(at, t) for at, t in _UNSCOPED if now - at < CONTROL_PLANE_TTL_SECONDS]
    for old in due:
        github_app.revoke_installation_token(old)


def sweep_control_plane_tokens(*, everything: bool = False) -> int:
    """Revoke unscoped control-plane tokens that are due (all of them with everything=True)."""
    now = time.time()
    with _cp_lock:
        due = [t for at, t in _UNSCOPED if everything or now - at >= CONTROL_PLANE_TTL_SECONDS]
        _UNSCOPED[:] = [] if everything else [(at, t) for at, t in _UNSCOPED if now - at < CONTROL_PLANE_TTL_SECONDS]
    for old in due:
        github_app.revoke_installation_token(old)
    return len(due)


def control_plane_token(db: Any, owner: str, repo: str) -> Optional[str]:
    """The credential the WORKER uses for one repo when the operator connected GitHub in Setup ›
    GitHub access: an installation token of the Setup-created App (1 hour, this repo only), else
    the org's fine-grained token when it was given for this repo. None when neither covers it.
    (An App configured in the environment is the legacy adapter's own; it is not read here.)"""
    full = f"{owner}/{repo}".lower()
    cfg = app_config(db)
    if cfg["source"] == "setup" and app_ready(db):
        try:
            tok = github_app.mint_installation_token([full], CONTROL_PLANE_PERMISSIONS, db=db)
            _account_for(replace(tok, kind=github_app.CONTROL_PLANE_KIND))
            return tok.token
        except github_app.GitHubRefused:
            pass
    token = org_token(db)
    if token is not None and full in token["repos"]:
        return vault.org_github_token(token["id"], db=db)
    return None


def installation_token_all(db: Any) -> str:
    """A 1-hour installation token for every repo the Setup-created App is installed on, with the
    control plane's permissions. Backs Setup › Repo's "GitHub App" connection (secret ref
    `identity_github:app`); the worker uses it, a build's sandbox never does."""
    cfg = app_config(db)
    if cfg["source"] != "setup" or not app_ready(db):
        raise GitHubSetupError("app_not_connected", 409)
    resp = github_app._call("POST", f"/app/installations/{cfg['installation_id']}/access_tokens",  # noqa: SLF001
                            token=github_app.app_jwt(db), body={"permissions": dict(CONTROL_PLANE_PERMISSIONS)})
    if resp.status not in (200, 201):
        raise GitHubSetupError("mint_failed", 502, status=resp.status)
    token = str((resp.json() or {})["token"])
    from datetime import datetime, timedelta, timezone

    _account_for(github_app.InstallationToken(token=token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                              repositories=(), permissions=dict(CONTROL_PLANE_PERMISSIONS),
                                              kind=github_app.CONTROL_PLANE_KIND))
    return token


def installation_repositories(db: Any) -> list[dict[str, Any]]:
    """The repositories the App can reach, in Setup's repo-picker shape."""
    token = installation_token_all(db)
    out: list[dict[str, Any]] = []
    for page in range(1, 4):
        resp = github_app._call("GET", f"/installation/repositories?per_page=100&page={page}", token=token)  # noqa: SLF001
        if resp.status != 200:
            raise GitHubSetupError("repositories_unavailable", 502, status=resp.status)
        items = (resp.json() or {}).get("repositories") or []
        for item in items:
            if isinstance(item, dict) and "/" in str(item.get("full_name") or ""):
                owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
                out.append({"full_name": str(item["full_name"]), "private": bool(item.get("private")),
                            "default_branch": item.get("default_branch"),
                            "owner_type": str(owner.get("type") or "Organization")})
        if len(items) < 100:
            break
    return out


# ---- what egress uses, and what posture says ----------------------------------------------------------------

def mode(db: Any, org_id: Optional[str] = None) -> dict[str, Any]:
    cfg = app_config(db, org_id)
    token = org_token(db, org_id)
    people = int(db.execute(_sql("SELECT count(DISTINCT owner_principal_id) FROM identity_vault_items WHERE "
                                 "kind = 'oauth_user' AND provider = 'github' AND status = 'active'")).scalar() or 0)
    from ..config import get_settings

    env_token = bool((get_settings().github_token or "").strip())
    active = "app" if app_ready(db, org_id) else ("token" if token else ("as_me" if people else "none"))
    read_by_builds = env_token and (get_settings().authz_mode or "enforce") != "enforce"
    return {"mode": active, "verdict": _verdict(active, cfg["account_login"], read_by_builds),
            "app": {"configured": bool(cfg["app_id"]), "installed": bool(cfg["installation_id"]),
                    "slug": cfg["slug"], "html_url": cfg["html_url"], "account": cfg["account_login"],
                    "source": cfg["source"]},
            "token": ({"repos": token["repos"], "login": token["login"], "migrated": token["migrated"]}
                      if token else None),
            "people_connected": people,
            "env_token": {"present": env_token, "read_by_builds": read_by_builds}}


def _verdict(active: str, account: Optional[str], env_read: bool) -> dict[str, str]:
    """The Setup row's one pill, decided here (the console never maps a state to a colour)."""
    if active == "app":
        return {"tone": "green", "label": "GitHub App" + (f" · @{account}" if account else ""),
                "tip": "Each build gets a short-lived token for its own repos."}
    if active == "token":
        return {"tone": "amber", "label": "Fine-grained token", "tip": "Works, but long-lived. A GitHub App is safer."}
    if env_read:
        return {"tone": "amber", "label": "BOSS_GITHUB_TOKEN",
                "tip": "Read from the server's environment. Move it into the vault."}
    if active == "as_me":
        return {"tone": "amber", "label": "Only “as me” runs",
                "tip": "Builds whose grant does not say “as me” cannot reach GitHub."}
    return {"tone": "red", "label": "Builds can't reach GitHub", "tip": "Connect a GitHub App below."}


def _event(db: Any, org_id: str, by: str, target_type: str, target_id: str, details: dict[str, Any], *,
           revoked: bool = False) -> None:
    audit.record(audit.SecurityEvent(
        org_id=org_id, event_type="vault.connection.revoked" if revoked else "vault.connection.linked",
        outcome=audit.Outcome.SUCCESS, actor_principal_id=by or None, actor_kind="user" if (by or "").startswith("usr_") else "system",
        target_type=target_type, target_id=target_id, details={"provider": "github", **details}), db=db)
