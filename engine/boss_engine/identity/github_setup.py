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

from . import audit, github_app, github_urls, jwks, vault
from .github_urls import WEB_BASE_ENV  # noqa: F401 — re-exported: callers read it here
MIGRATED_NAME = "Moved from BOSS_GITHUB_TOKEN"
ORG_TOKEN_NAME = "Fine-grained token (org)"
REPO_PREFIX = "repo:"
#: What the App asks for: write code and PRs, read what a review needs. Never admin; never workflows
#: unless a repository opted in (`app_permissions`).
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read",
                   "metadata": "read", "issues": "read", "actions": "read"}
#: Added ONLY when a repository has "Builds may change CI workflows" on (services/ci_gate.py).
WORKFLOWS_PERMISSION = {"workflows": "write"}


def app_permissions(allow_workflows: bool = False) -> dict[str, str]:
    """The App's permission request: APP_PERMISSIONS, plus workflows: write for the opt-in."""
    return {**APP_PERMISSIONS, **(WORKFLOWS_PERMISSION if allow_workflows else {})}


def workflows_permission_link(db: Any, org_id: Optional[str] = None) -> dict[str, Any]:
    """Where an operator accepts `workflows: write` for an App created before the opt-in."""
    from ..services import ci_gate

    return ci_gate.permissions_link(app_config(db, org_id), web_base())


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


def org_credentials_refusal(org_id: str, *, db: Any = None) -> Optional[str]:
    """Why this caller may not change the org's GitHub credentials: None (they may), "step_up"
    (they hold github:connect for the org but have not confirmed their second factor recently),
    or "org_scope" (they do not hold it for the whole organisation). Same decision as
    may_change_org_credentials, with the reason a person can act on."""
    from . import authz, lineage
    from .authz import DenyReason
    from .permissions_gen import P
    from .scope import ResolvedScope

    if not lineage.request_enforced():
        return None
    ctx = lineage.current_context()
    if ctx is None or ctx.principal.org_id != org_id:
        return "org_scope"
    decision = authz.check(ctx, P.GITHUB_CONNECT, ResolvedScope.org_level(org_id), db=db)
    if decision.allowed:
        return None
    return "step_up" if getattr(decision, "reason", None) == DenyReason.STEP_UP_REQUIRED else "org_scope"


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
    return github_urls.web_base()


# ---- the App's configuration (env first: an operator's explicit choice wins) ---------------------------

def app_config(db: Any, org_id: Optional[str] = None) -> dict[str, Optional[str]]:
    blank = {"slug": None, "html_url": None, "account_login": None, "account_type": None,
             "owner_login": None, "owner_type": None, "verified": None, "verified_at": None}
    env = {"app_id": os.environ.get(github_app.APP_ID_ENV),
           "installation_id": os.environ.get(github_app.INSTALLATION_ENV),
           "client_id": os.environ.get(github_app.CLIENT_ID_ENV)}
    if env["app_id"]:
        return {**blank, **env, "source": "env"}
    row = None
    if db is not None:
        q = ("SELECT app_id, installation_id, client_id, slug, html_url, account_login, account_type, "
             "owner_login, owner_type, verified, verified_at FROM identity_github_app")
        row = (db.execute(_sql(q + " WHERE org_id = :o"), {"o": org_id}).mappings().first() if org_id
               else db.execute(_sql(q + " ORDER BY created_at LIMIT 1")).mappings().first())
    if row is None:
        return {**blank, "app_id": None, "installation_id": None, "client_id": None, "source": None}
    return {**dict(row), "source": "setup"}


def owner_account(cfg: dict[str, Any]) -> github_urls.Account:
    """Who owns the App (its settings and permissions pages live in this account's tree)."""
    return github_urls.Account.of(cfg.get("owner_login"), cfg.get("owner_type"))


def install_account(cfg: dict[str, Any]) -> github_urls.Account:
    """Where the App is installed (the installation's page lives in this account's tree)."""
    return github_urls.Account.of(cfg.get("account_login"), cfg.get("account_type"))


def links(cfg: dict[str, Any]) -> dict[str, Optional[str]]:
    """Every GitHub page Setup links to for this App, from github_urls (the one builder). A page
    whose account type is not known yet is None, never a guess."""
    slug = str(cfg.get("slug") or "")
    inst = cfg.get("installation_id")
    return {"app": github_urls.app_public(slug),
            "app_settings": github_urls.app_settings(slug, owner_account(cfg)),
            "permissions": github_urls.app_permissions(slug, owner_account(cfg)),
            "install": github_urls.install_app(slug),
            "installation": github_urls.installation_settings(inst, install_account(cfg)) if inst else None}


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

def app_name(host: str) -> str:
    """A GitHub App's name is unique across ALL of GitHub (34 characters at most). "BOSS
    localhost" plus the default port was the same for every laptop install, so the second stranger's App was
    refused as "already taken". The host says which install it is; a short random tail makes it
    unique."""
    tail = secrets.token_hex(2)
    clean = "".join(c if c.isalnum() or c in "-." else "-" for c in host).strip("-.") or "install"
    return f"BOSS {clean[:34 - len('BOSS ') - len(tail) - 1]} {tail}"


def resolve_owner(owner: Optional[str]) -> github_urls.Account:
    """The account a new App will belong to. Blank: the signed-in person's own account. A name:
    GitHub says whether it is a person or an organisation (GET /users/{name}), because the two
    create Apps on different pages and a personal name on the organisation page is a 404."""
    login = (owner or "").strip()
    if not login:
        return github_urls.Account(None, github_urls.USER)
    resp = github_app._call("GET", f"/users/{login}", token="", scheme="")  # noqa: SLF001
    if resp.status == 404:
        raise GitHubSetupError("github_account_not_found", 422, owner=login,
                               message=f"GitHub has no account named {login}. Check the spelling, or "
                                       "leave it blank to create the App on your own account.")
    if resp.status != 200:
        raise GitHubSetupError("github_unreachable", 502, status=resp.status,
                               message="GitHub did not answer. Try again in a minute.")
    data = resp.json() or {}
    account = github_urls.Account.of(data.get("login") or login, data.get("type"))
    if not account.known:
        raise GitHubSetupError("github_account_kind", 422, owner=login,
                               message=f"{login} is not a person or an organisation on GitHub.")
    return account


def manifest(*, origin: str, person_id: str, owner: Optional[str] = None,
             allow_workflows: bool = False, owner_account: Optional[github_urls.Account] = None) -> dict[str, Any]:
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
        "name": app_name(host),
        "url": origin,
        "redirect_url": f"{origin}/v1/identity/github/app/created",
        "callback_urls": [f"{origin}/v1/identity/me/connections/github/callback"],
        "setup_url": f"{origin}/v1/identity/github/app/installed",
        "setup_on_update": True,
        # PUBLIC ON PURPOSE (BOSSAPP-1016). A private GitHub App installs ONLY on its creator, and
        # GitHub has no API to change visibility later — `False` here walled every second account
        # out of the app route (a raw 404 on the install page). Public only means other accounts
        # MAY install the App; each installation still grants only the repositories and
        # permissions that installation itself picked. Multi-account is table stakes.
        "public": True,
        "request_oauth_on_install": False,
        "default_permissions": app_permissions(allow_workflows),
        "default_events": [],
    }
    state = _state(person_id, "app-manifest")
    account = owner_account or resolve_owner(owner)
    return {"post_url": github_urls.app_new(account, state=state), "manifest": body, "state": state,
            "owner": {"login": account.login, "type": account.type}}


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
    owner = data.get("owner") or {}
    db.execute(_sql(
        "INSERT INTO identity_github_app (org_id, app_id, slug, html_url, client_id, account_login, owner_login, "
        "owner_type, created_by) VALUES (:o, :a, :s, :h, :c, :l, :l, :t, :by) ON CONFLICT (org_id) DO UPDATE "
        "SET app_id = EXCLUDED.app_id, slug = EXCLUDED.slug, html_url = EXCLUDED.html_url, "
        "client_id = EXCLUDED.client_id, account_login = EXCLUDED.account_login, owner_login = EXCLUDED.owner_login, "
        "owner_type = EXCLUDED.owner_type, account_type = NULL, installation_id = NULL, verified = NULL, "
        "verified_at = NULL, updated_at = now()"),
        {"o": org_id, "a": app_id, "s": data.get("slug"), "h": data.get("html_url"),
         "c": data.get("client_id"), "l": owner.get("login"), "t": github_urls.account_type(owner.get("type")),
         "by": person})
    _event(db, org_id, person, "github_app", app_id, {"how": "app", "step": "created", "slug": data.get("slug"),
                                                     "key_item": key_item})
    slug = data.get("slug") or app_id
    install = github_urls.install_app(slug, state=_state(person, "app-install"))
    return {"app_id": app_id, "slug": slug, "install_url": install}


def complete_install(*, installation_id: str, state: Optional[str], org_id: str, db: Any) -> dict[str, Any]:
    """GitHub sent the person back after installing the App: keep the installation once the App's
    own JWT confirms it is one of ITS installations."""
    person = _check_state(state or "", "app-install", db=db)      # no state, no change: the picker is the other way
    return select_installation(installation_id=installation_id, org_id=org_id, db=db, by=person)


#: A state GitHub carried back that no longer opens the install path: signed for another
#: moment, already used, or expired. On GitHub's own return (a repository-access change on
#: GitHub's installation-settings page) that is not a failure — the change is already saved on
#: GitHub — so the callback confirms it through the signed-in session and the App's API instead.
STALE_STATE_CODES = frozenset({"bad_state", "state_expired", "state_replayed"})


def confirm_installation(*, installation_id: str, org_id: str, db: Any) -> Optional[str]:
    """GitHub sent a signed-in person back with an installation id and no usable state: ask the
    App's own API whether that id is one of ITS installations (the query alone proves nothing).
    Nothing is stored: a redirect never swaps the installation builds use. Returns the account
    the installation is on, or None when no Setup-created App is here to ask. Raises
    GitHubSetupError `not_this_apps_installation` when the App's API does not know the id."""
    cfg = app_config(db, org_id)
    installation_id = str(installation_id or "").strip()
    if cfg.get("source") != "setup" or not cfg.get("app_id") or not installation_id.isdigit():
        return None
    resp = github_app._call("GET", f"/app/installations/{installation_id}", token=github_app.app_jwt(db))  # noqa: SLF001
    if resp.status != 200:
        raise GitHubSetupError("not_this_apps_installation", 403)
    return ((resp.json() or {}).get("account") or {}).get("login")


def installations(*, db: Any, org_id: str) -> list[dict[str, Any]]:
    if not app_config(db, org_id)["app_id"]:
        raise GitHubSetupError("app_not_configured", 409)
    resp = github_app._call("GET", "/app/installations", token=github_app.app_jwt(db))  # noqa: SLF001
    if resp.status != 200:
        raise GitHubSetupError("installations_unavailable", 502, status=resp.status)
    return [{"id": str(i.get("id")), "account": (i.get("account") or {}).get("login"),
             "account_type": github_urls.account_type((i.get("account") or {}).get("type")),
             "repository_selection": i.get("repository_selection")} for i in (resp.json() or [])]


def select_installation(*, installation_id: str, org_id: str, db: Any, by: str) -> dict[str, Any]:
    require_org_credentials_authority(org_id, db=db)
    installation_id = str(installation_id or "").strip()
    if not installation_id.isdigit():
        raise GitHubSetupError("bad_installation", 422)
    resp = github_app._call("GET", f"/app/installations/{installation_id}", token=github_app.app_jwt(db))  # noqa: SLF001
    if resp.status != 200:
        raise GitHubSetupError("not_this_apps_installation", 403)
    data = resp.json() or {}
    account = (data.get("account") or {}).get("login")
    kind = github_urls.account_type((data.get("account") or {}).get("type"))
    db.execute(_sql("UPDATE identity_github_app SET installation_id = :i, account_login = coalesce(:l, "
                    "account_login), account_type = :t, verified = NULL, verified_at = NULL, updated_at = now() "
                    "WHERE org_id = :o"),
               {"i": installation_id, "l": account, "t": kind, "o": org_id})
    _keep_default(db, org_id, installation_id, account, kind, by)
    _event(db, org_id, by, "github_app", installation_id, {"how": "app", "step": "installed", "account": account})
    return {"installation_id": installation_id, "account": account}


# ---- every account the App is installed on (BOSSAPP-945) ----------------------------------------------

def _savepoint(db: Any, fn: Any, default: Any) -> Any:
    """One statement on the installations table; an older schema (no table yet) answers `default`."""
    nested = getattr(db, "begin_nested", None)
    point = nested() if callable(nested) else None
    try:
        out = fn()
    except Exception:  # noqa: BLE001 — no table on this schema yet
        if point is not None:
            point.rollback()
        return default
    if point is not None:
        point.commit()
    return out


def recorded_installations(db: Any, org_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The App's installations BOSS knows, oldest first (the default one is on identity_github_app)."""
    q = ("SELECT installation_id, account_login, account_type, account_id, repository_selection "
         "FROM identity_github_installations")

    def read() -> list[dict[str, Any]]:
        res = (db.execute(_sql(q + " WHERE org_id = :o ORDER BY created_at"), {"o": org_id}) if org_id
               else db.execute(_sql(q + " ORDER BY created_at")))
        return [dict(r) for r in res.mappings().all()]
    return _savepoint(db, read, []) if db is not None else []


def installation_ids(db: Any, org_id: Optional[str] = None) -> list[str]:
    """The default installation first, then every other account's."""
    out: list[str] = []
    first = app_config(db, org_id).get("installation_id")
    if first:
        out.append(str(first))
    for row in recorded_installations(db, org_id):
        inst = str(row["installation_id"])
        if inst not in out:
            out.append(inst)
    return out


def installation_for_owner(db: Any, owner: str) -> Optional[str]:
    """The installation on the account that owns a repository (Add a GitHub account), or None:
    the caller uses the default installation."""
    if not owner or db is None:
        return None
    found = _savepoint(db, lambda: db.execute(_sql(
        "SELECT installation_id FROM identity_github_installations WHERE lower(account_login) = lower(:l) "
        "ORDER BY updated_at DESC LIMIT 1"), {"l": owner}).scalar(), None)
    return str(found) if found else None


def record_installation(db: Any, org_id: str, installation: dict[str, Any], *, by: str) -> dict[str, Any]:
    """Keep one of the App's installations, as GitHub's own API listed it (GET /app/installations,
    asked with the App's key: the list proves it is the App's). The first one becomes the default
    on identity_github_app, as the install callback does; later ones sit beside it, so adding an
    account never moves the repositories that already build."""
    require_org_credentials_authority(org_id, db=db)
    inst = str(installation.get("id") or "").strip()
    if not inst.isdigit():
        raise GitHubSetupError("bad_installation", 422)
    account = installation.get("account") or {}
    login = account.get("login")
    kind = github_urls.account_type(account.get("type"))
    fresh = db.execute(_sql(
        "INSERT INTO identity_github_installations (org_id, installation_id, account_login, account_type, "
        "account_id, repository_selection, created_by) VALUES (:o, :i, :l, :t, :a, :s, :by) "
        "ON CONFLICT (org_id, installation_id) DO UPDATE SET account_login = EXCLUDED.account_login, "
        "account_type = EXCLUDED.account_type, account_id = EXCLUDED.account_id, "
        "repository_selection = EXCLUDED.repository_selection, updated_at = now() "
        "RETURNING (xmax = 0) AS inserted"),
        {"o": org_id, "i": inst, "l": login, "t": kind, "a": str(account.get("id") or "") or None,
         "s": installation.get("repository_selection"), "by": by or None}).scalar()
    became_default = False
    cfg = app_config(db, org_id)
    if cfg.get("source") == "setup" and not cfg.get("installation_id"):
        db.execute(_sql("UPDATE identity_github_app SET installation_id = :i, account_login = coalesce(:l, "
                        "account_login), account_type = :t, verified = NULL, verified_at = NULL, updated_at = now() "
                        "WHERE org_id = :o"), {"i": inst, "l": login, "t": kind, "o": org_id})
        became_default = True
    if fresh:
        _event(db, org_id, by, "github_app", inst, {"how": "app", "step": "installed", "account": login,
                                                    "default": became_default})
    return {"installation_id": inst, "account": login, "account_type": kind, "default": became_default,
            "new": bool(fresh)}


def _keep_default(db: Any, org_id: str, installation_id: str, account: Any, kind: Optional[str], by: str) -> None:
    """The default installation is one of the recorded ones too (Add a GitHub account lists them)."""
    def write() -> None:
        db.execute(_sql(
            "INSERT INTO identity_github_installations (org_id, installation_id, account_login, account_type, "
            "created_by) VALUES (:o, :i, :l, :t, :by) ON CONFLICT (org_id, installation_id) DO UPDATE SET "
            "account_login = EXCLUDED.account_login, account_type = EXCLUDED.account_type, updated_at = now()"),
            {"o": org_id, "i": installation_id, "l": account, "t": kind, "by": by or None})
    _savepoint(db, write, None)


def disconnect_app(*, org_id: str, db: Any, by: str) -> None:
    require_org_credentials_authority(org_id, db=db)
    db.execute(_sql("DELETE FROM identity_github_app WHERE org_id = :o"), {"o": org_id})
    _savepoint(db, lambda: db.execute(_sql("DELETE FROM identity_github_installations WHERE org_id = :o"),
                                      {"o": org_id}), None)
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


_ORG_TOKENS_Q = ("SELECT id, connection_name, granted_scopes, provider_subject, created_at FROM identity_vault_items "
                 "WHERE kind = 'legacy_pat' AND owner_principal_id IS NULL AND provider = 'github' "
                 "AND status = 'active'")


def _token_row(row: Any) -> dict[str, Any]:
    repos = [s[len(REPO_PREFIX):] for s in (row["granted_scopes"] or []) if s.startswith(REPO_PREFIX)]
    return {"id": row["id"], "repos": repos, "login": row["provider_subject"],
            "migrated": row["connection_name"] == MIGRATED_NAME, "created_at": row["created_at"]}


def org_token(db: Any, org_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The org's NEWEST fine-grained token (the one Identity › GitHub shows). An org may hold
    several since BOSSAPP-808 (one per GitHub account); `org_token_for` picks the one a repo uses."""
    tokens = org_tokens(db, org_id, limit=1)
    return tokens[0] if tokens else None


def org_tokens(db: Any, org_id: Optional[str] = None, *, limit: int = 50) -> list[dict[str, Any]]:
    """Every active org GitHub token, newest first: one per GitHub account the org connected
    with a token (a personal account and one or more organisations)."""
    q = _ORG_TOKENS_Q + (" AND org_id = :o" if org_id else "") + " ORDER BY created_at DESC, id DESC LIMIT :lim"
    params: dict[str, Any] = {"lim": int(limit)}
    if org_id:
        params["o"] = org_id
    return [_token_row(r) for r in db.execute(_sql(q), params).mappings().all()]


def org_token_for(db: Any, repos: Iterable[str] | str, org_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The org token that covers EVERY one of `repos` (newest first), or None. Builds use the
    account that owns the repository, never "whichever token was stored last"."""
    wanted = {str(r).strip().lower() for r in ([repos] if isinstance(repos, str) else repos)}
    for tok in org_tokens(db, org_id):
        if wanted and wanted <= set(tok["repos"]):
            return tok
    return None


def store_org_token(*, token: str, repos: Iterable[str], org_id: str, db: Any, by: str,
                    migrated: bool = False, check: bool = True, keep_others: bool = False,
                    login: Optional[str] = None) -> dict[str, Any]:
    """Keep `token` as an org GitHub connection for `repos`.

    `keep_others=False` (Identity › GitHub's paste, the env migration): it REPLACES the org's
    token, as it always did. `keep_others=True` (Setup › Add GitHub account): it is ANOTHER
    account — only an older token of the same GitHub login is retired (its repos carry over), so
    a second account never takes the first one's repositories away."""
    require_org_credentials_authority(org_id, db=db)
    token = (token or "").strip()
    if not token or len(token) > 400 or any(c.isspace() for c in token):
        raise GitHubSetupError("bad_token", 422)
    wanted = _repos(repos)
    if not wanted:
        raise GitHubSetupError("repos_required", 422, message="Name the repositories this token is for.")
    if check:
        me = github_app._call("GET", "/user", token=token)  # noqa: SLF001
        if me.status != 200:
            raise GitHubSetupError("token_refused_by_github", 422)
        login = (me.json() or {}).get("login")
        for repo in wanted:
            r = github_app._call("GET", f"/repos/{repo}", token=token)  # noqa: SLF001
            if r.status != 200:
                raise GitHubSetupError("token_cannot_see_repo", 422, repo=repo)
    if keep_others:
        same = [t for t in org_tokens(db, org_id)
                if login and str(t["login"] or "").lower() == str(login).lower()]
        wanted = _repos([*wanted, *(r for t in same for r in t["repos"])])
        if same:
            db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE id = ANY(:ids)"),
                       {"ids": [t["id"] for t in same]})
    else:
        db.execute(_sql("UPDATE identity_vault_items SET status = 'revoked' WHERE kind = 'legacy_pat' AND "
                        "owner_principal_id IS NULL AND provider = 'github' AND org_id = :o AND status = 'active'"),
                   {"o": org_id})
    item = vault.store(org_id=org_id, owner_principal_id=None, kind=vault.VaultItemKind.LEGACY_PAT,
                       provider="github", secret=token, provider_subject=login,
                       connection_name=MIGRATED_NAME if migrated else ORG_TOKEN_NAME,
                       granted_scopes=[REPO_PREFIX + r for r in wanted], db=db)
    _event(db, org_id, by, "vault_item", item, {"how": "token", "repos": wanted, "migrated": migrated})
    return {"id": item, "repos": wanted, "login": login, "migrated": migrated}


def add_repo_to_org_token(*, repo: str, org_id: str, db: Any, token_id: Optional[str] = None) -> None:
    """Setup › Repo picked another repository for a stored token: that org connection covers it too.
    `token_id` names which (an org may hold several); without it, the newest."""
    require_org_credentials_authority(org_id, db=db)
    current = (next((t for t in org_tokens(db, org_id) if t["id"] == token_id), None) if token_id
               else org_token(db, org_id))
    wanted = _repos([repo])
    if current is None or not wanted or wanted[0] in current["repos"]:
        return
    db.execute(_sql("UPDATE identity_vault_items SET granted_scopes = array_append(granted_scopes, :r) "
                    "WHERE id = :i"), {"r": REPO_PREFIX + wanted[0], "i": current["id"]})


def remove_org_token(*, org_id: str, db: Any, by: str, token_id: Optional[str] = None) -> bool:
    require_org_credentials_authority(org_id, db=db)
    current = (next((t for t in org_tokens(db, org_id) if t["id"] == token_id), None) if token_id
               else org_token(db, org_id))
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


def control_plane_permissions(db: Any, full_name: str) -> dict[str, str]:
    """The control plane's permissions for one repository: CONTROL_PLANE_PERMISSIONS, plus
    `workflows: write` while that repository has "Builds may change CI workflows" on.

    The opt-in raised the build's grant and its builder's ceiling, but the token that PUSHES a
    finished build is this one, minted from the fixed set — so with the opt-in on and
    `workflows: write` accepted on the installation, the Order API build's push of
    .github/workflows/order-api.yml was refused 403 three times (crun_4eec9642, D2). Read live:
    turning the opt-in off withdraws it at the next mint; an unreadable opt-in never widens."""
    from ..services.github_app_adapter import _workflows_opted_in

    perms = dict(CONTROL_PLANE_PERMISSIONS)
    if _workflows_opted_in(full_name, db):
        perms.update(WORKFLOWS_PERMISSION)
    return perms


def control_plane_token(db: Any, owner: str, repo: str) -> Optional[str]:
    """The credential the WORKER uses for one repo when the operator connected GitHub in Setup ›
    GitHub access: an installation token of the Setup-created App (1 hour, this repo only), else
    the org's fine-grained token when it was given for this repo. None when neither covers it.
    (An App configured in the environment is the legacy adapter's own; it is not read here.)"""
    full = f"{owner}/{repo}".lower()
    cfg = app_config(db)
    if cfg["source"] == "setup" and app_ready(db):
        try:
            tok = github_app.mint_installation_token([full], control_plane_permissions(db, full),
                                                     db=db)
            _account_for(replace(tok, kind=github_app.CONTROL_PLANE_KIND))
            return tok.token
        except github_app.GitHubRefused:
            pass
    token = org_token_for(db, full)
    if token is not None:
        return vault.org_github_token(token["id"], db=db)
    return None


def installation_token_all(db: Any, installation_id: Optional[str] = None) -> str:
    """A 1-hour installation token for every repo the Setup-created App is installed on, with the
    control plane's permissions. Backs Setup › Repo's "GitHub App" connection (secret ref
    `identity_github:app`); the worker uses it, a build's sandbox never does. `installation_id`:
    one of the App's other installations (another account, Add a GitHub account) instead of the
    default one."""
    cfg = app_config(db)
    if cfg["source"] != "setup" or not app_ready(db):
        raise GitHubSetupError("app_not_connected", 409)
    inst = str(installation_id or cfg["installation_id"])
    resp = github_app._call("POST", f"/app/installations/{inst}/access_tokens",  # noqa: SLF001
                            token=github_app.app_jwt(db), body={"permissions": dict(CONTROL_PLANE_PERMISSIONS)})
    if resp.status not in (200, 201):
        raise GitHubSetupError("mint_failed", 502, status=resp.status)
    token = str((resp.json() or {})["token"])
    from datetime import datetime, timedelta, timezone

    _account_for(github_app.InstallationToken(token=token, expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                                              repositories=(), permissions=dict(CONTROL_PLANE_PERMISSIONS),
                                              kind=github_app.CONTROL_PLANE_KIND))
    return token


def installation_repositories(db: Any, installation_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The repositories the App can reach, in Setup's repo-picker shape: on every account it is
    installed on (each installation in turn, a repository once), or on one installation."""
    if installation_id:
        return _one_installation_repositories(db, str(installation_id))
    default = str(app_config(db).get("installation_id") or "")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for inst in installation_ids(db) or [default]:
        try:
            listed = _one_installation_repositories(db, inst or None)
        except GitHubSetupError:
            if not inst or inst == default:
                raise                      # the default installation: as before
            continue                       # another account's, gone or suspended: the rest still list
        for item in listed:
            if item["full_name"].lower() not in seen:
                seen.add(item["full_name"].lower())
                out.append(item)
    return out


def _one_installation_repositories(db: Any, installation_id: Optional[str]) -> list[dict[str, Any]]:
    token = installation_token_all(db, installation_id)
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


# ---- verify, don't trust: what GitHub actually granted ------------------------------------------------------

_LEVELS = {"none": 0, "read": 1, "write": 2, "admin": 3}


def _level(value: object) -> int:
    return _LEVELS.get(str(value or "none").lower(), 0)


def _registered_repos(db: Any) -> list[str]:
    nested = getattr(db, "begin_nested", None)
    point = nested() if callable(nested) else None
    try:
        rows = db.execute(_sql("SELECT DISTINCT lower(full_name) AS r FROM github_repository_bindings "
                               "WHERE status = 'active' AND full_name LIKE '%/%' "
                               # A repository on this machine is not the GitHub App's (BOSSAPP-918).
                               "AND full_name NOT LIKE 'local\\_%' ORDER BY 1")).scalars().all()
    except Exception:  # noqa: BLE001 — an older schema: nothing registered
        if point is not None:
            point.rollback()
        return []
    if point is not None:
        point.commit()
    return [str(r) for r in rows]


def verify(db: Any, org_id: Optional[str] = None) -> dict[str, Any]:
    """Ask GitHub what the App's installation was ACTUALLY granted, instead of trusting what BOSS
    asked for: the installation's accepted permissions (GET /app/installations/{id}) against what
    the App needs now, and the repositories it was installed on against the ones registered here.
    Each gap names the GitHub page that closes it (github_urls). Kept on the row (`verified`) so
    the console shows it without asking GitHub on every page load; refreshed on return from
    GitHub, by Refresh and by Test all."""
    from ..services import ci_gate

    org_id = org_id or (db.execute(_sql("SELECT org_id FROM identity_github_app ORDER BY created_at LIMIT 1"))
                        .scalar() if db is not None else None)
    cfg = app_config(db, org_id)
    if cfg["source"] != "setup" or not cfg["app_id"]:
        return {"state": "no_app"}
    out: dict[str, Any] = {"state": "ok", "checked_at": int(time.time())}
    jwt = github_app.app_jwt(db)
    me = github_app._call("GET", "/app", token=jwt)  # noqa: SLF001
    app_perms: dict[str, str] = {}
    if me.status == 200:
        owner = (me.json() or {}).get("owner") or {}
        app_perms = dict((me.json() or {}).get("permissions") or {})
        kind = github_urls.account_type(owner.get("type"))
        if kind and (kind != cfg.get("owner_type") or owner.get("login") != cfg.get("owner_login")):
            db.execute(_sql("UPDATE identity_github_app SET owner_login = :l, owner_type = :t WHERE org_id = :o"),
                       {"l": owner.get("login"), "t": kind, "o": org_id})
            cfg.update(owner_login=owner.get("login"), owner_type=kind)
    elif me.status in (401, 404):
        out["state"] = "app_gone"
    else:
        out["state"] = "unreachable"
    inst = cfg.get("installation_id")
    granted: dict[str, str] = {}
    if out["state"] == "ok" and not inst:
        out["state"] = "not_installed"
    elif out["state"] == "ok":
        resp = github_app._call("GET", f"/app/installations/{inst}", token=jwt)  # noqa: SLF001
        if resp.status == 404:
            out["state"] = "installation_gone"
        elif resp.status != 200:
            out["state"] = "unreachable"
        else:
            data = resp.json() or {}
            account = data.get("account") or {}
            kind = github_urls.account_type(account.get("type"))
            if kind and (kind != cfg.get("account_type") or account.get("login") != cfg.get("account_login")):
                db.execute(_sql("UPDATE identity_github_app SET account_login = :l, account_type = :t "
                                "WHERE org_id = :o"), {"l": account.get("login"), "t": kind, "o": org_id})
                cfg.update(account_login=account.get("login"), account_type=kind)
            granted = dict(data.get("permissions") or {})
            out["repository_selection"] = data.get("repository_selection")
    page = links(cfg)
    needed = app_permissions(ci_gate.any_repo_allows_workflows(db))
    perms = []
    for name, want in sorted(needed.items()):
        has = granted.get(name)
        if _level(has) >= _level(want):
            state, fix = "accepted", None
        elif app_perms and _level(app_perms.get(name)) < _level(want):
            state, fix = "app_lacks", page["permissions"]          # the App itself must ask first
        else:
            state, fix = "waiting", page["installation"]           # accept on the installation
        perms.append({"name": name, "wanted": want, "granted": has, "state": state, "fix_url": fix})
    out["permissions"] = perms if granted or out["state"] == "ok" else []
    repos: list[dict[str, Any]] = []
    if out["state"] == "ok":
        try:
            reachable = {r["full_name"].lower() for r in installation_repositories(db)}
        except (GitHubSetupError, github_app.GitHubRefused):
            reachable = None
        for full in _registered_repos(db):
            ok = None if reachable is None else full in reachable
            repos.append({"full_name": full, "installed": ok, "fix_url": None if ok else page["installation"]})
        out["installed_on"] = sorted(reachable) if reachable is not None else None
    out["repos"] = repos
    out["account"] = {"login": cfg.get("account_login"), "type": cfg.get("account_type")}
    out["owner"] = {"login": cfg.get("owner_login"), "type": cfg.get("owner_type")}
    out["links"] = page
    out["ok"] = (out["state"] == "ok" and all(p["state"] == "accepted" for p in perms)
                 and all(r["installed"] is not False for r in repos))
    db.execute(_sql("UPDATE identity_github_app SET verified = CAST(:v AS jsonb), verified_at = now() "
                    "WHERE org_id = :o"), {"v": json.dumps(out), "o": org_id})
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
    return {"mode": active, "verdict": _verdict(active, cfg["account_login"], read_by_builds, _verified(cfg)),
            "app": {"configured": bool(cfg["app_id"]), "installed": bool(cfg["installation_id"]),
                    "slug": cfg["slug"], "html_url": cfg["html_url"], "account": cfg["account_login"],
                    "account_type": cfg.get("account_type"), "owner": cfg.get("owner_login"),
                    "owner_type": cfg.get("owner_type"), "source": cfg["source"],
                    "links": links(cfg) if cfg["source"] == "setup" else {},
                    "verified": _verified(cfg)},
            "token": ({"repos": token["repos"], "login": token["login"], "migrated": token["migrated"]}
                      if token else None),
            # Every org token (one per GitHub account), newest first. Never a secret.
            "tokens": [{"id": t["id"], "repos": t["repos"], "login": t["login"], "migrated": t["migrated"]}
                       for t in org_tokens(db, org_id)],
            "people_connected": people,
            "env_token": {"present": env_token, "read_by_builds": read_by_builds}}


def _verified(cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = cfg.get("verified")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else None


def _verdict(active: str, account: Optional[str], env_read: bool,
             verified: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """The Setup row's one pill, decided here (the console never maps a state to a colour). For
    the App it is what GitHub last SAID (verify), not what BOSS asked for."""
    if active == "app" and verified and verified.get("state") in ("app_gone", "installation_gone"):
        return {"tone": "red", "label": "GitHub App gone",
                "tip": "GitHub no longer has this App or its installation. See below."}
    if active == "app" and verified and verified.get("ok") is False and verified.get("state") == "ok":
        return {"tone": "amber", "label": "GitHub App · needs you",
                "tip": "GitHub has not granted everything builds need. See below."}
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
