"""Setup › GitHub accounts and Repositories BOSS can work on (BOSSAPP-808).

One row per GitHub account the org can build with, and one row per repository with the account it
goes through. Read from what already exists — nothing here stores anything:

  * this project's GitHub connections (`github_connections`: a pasted token, "Sign in with GitHub",
    or the org's GitHub App), each owning its repository bindings — a build of a repository uses
    the connection that owns it (`GitHubConnectionService.token_for_repository`);
  * the org's own credentials (identity/github_setup.py): the GitHub App from a manifest and the
    org's fine-grained tokens — several since 808, one per account (`org_tokens`), and a build
    picks the one that covers its repository (`org_token_for`);
  * an install configured from its settings file (BOSS_GITHUB_TOKEN) — one row, "Server settings".

An install that had ONE connection before 808 shows it as the first row (oldest first) with its
repositories: there is nothing to migrate, the rows are what was already stored.

Every reader is wrapped in a savepoint: an older schema, or no identity tables, shows fewer rows,
never a broken Setup page. No token value is read here; only that one is held.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .setup_checks import parse_time

logger = logging.getLogger(__name__)

#: An expiry worth warning about (amber): within two weeks.
EXPIRY_WARN_DAYS = 14
SIGNIN_REF_PREFIX = "identity_github:user:"
APP_REF = "identity_github:app"


def _safely(connection: Connection, fn: Callable[[], Any], default: Any) -> Any:
    try:
        with connection.begin_nested():
            return fn()
    except Exception:  # noqa: BLE001 — a missing table shows fewer rows, never a broken page
        logger.debug("setup accounts: a source was skipped", exc_info=True)
        return default


def _iso(value: Any) -> Optional[str]:
    parsed = parse_time(value) if value is not None else None
    return parsed.isoformat() if parsed else None


def _pill(state: str, tone: str, label: str, tip: str, action: Optional[str] = None) -> dict[str, Any]:
    return {"state": state, "tone": tone, "label": label, "tip": tip, "action": action}


def _org_of(connection: Connection, project_id: str) -> Optional[str]:
    return _safely(connection, lambda: connection.execute(
        text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
        {"p": project_id}).scalar(), None)


def _how(kind: str, personal: Optional[bool]) -> str:
    who = "Personal" if personal else "Organisation" if personal is False else None
    if kind == "personal_signin":
        return "Personal · signed in with GitHub"
    if kind == "org_app":
        return f"{who or 'Organisation'} · BOSS GitHub App"
    if kind == "install":
        return "Server settings · access token"
    return f"{who} · access token" if who else "Access token"


def _expiry_pill(expires: Optional[datetime], now: datetime) -> Optional[dict[str, Any]]:
    if expires is None:
        return None
    days = (expires - now).days
    if expires <= now:
        return _pill("expired", "red", "Token expired", "Builds on its repos stop. Replace the token.",
                     "replace")
    if days <= EXPIRY_WARN_DAYS:
        n = max(days, 0)
        return _pill("expiring", "amber", f"Token expires in {n} day{'s' if n != 1 else ''}",
                     "Make a new token before then and press Fix.", "replace")
    return None


def _status(repos: list[dict[str, Any]], *, connection_status: Optional[str], expires: Optional[datetime],
            now: datetime, kind: str) -> dict[str, Any]:
    fix = "fix_link" if kind == "org_app" else "sign_in" if kind == "personal_signin" else "replace"
    expiry = _expiry_pill(expires, now)
    if expiry and expiry["tone"] == "red":
        return expiry
    if connection_status == "error":
        return _pill("broken", "red", "Broken", "GitHub refused this account's last check.", fix)
    red = next((r["pill"] for r in repos if r["pill"]["tone"] == "red"), None)
    if red:
        return _pill("broken", "red", "Broken", red.get("tip") or "A repository check failed.", fix)
    if expiry:
        return expiry
    if any(r["pill"]["tone"] == "amber" for r in repos):
        amber = next(r["pill"] for r in repos if r["pill"]["tone"] == "amber")
        return _pill("needs_you", "amber", amber.get("label") or "Needs you", amber.get("tip") or "", None)
    if any(r["pill"]["tone"] == "green" for r in repos) or connection_status == "active":
        return _pill("connected", "green", "Connected", "BOSS can read, push and open pull requests.")
    if repos:
        return _pill("checking", "grey", "Not checked", "Press Test all to check it now.")
    return _pill("no_repos", "grey", "No repos yet", "Add a repo that BOSS may work on.")


def _owners_personal(login: Optional[str], repos: list[dict[str, Any]]) -> Optional[bool]:
    if not login or not repos:
        return None
    owners = {str(r["full_name"]).split("/", 1)[0].lower() for r in repos}
    return owners == {login.lower()}


def _project_connections(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT id, connection_label, auth_method, credential_ref, account_login, status, "
             "       last_test_status, expires_at, created_at "
             "  FROM github_connections "
             " WHERE project_id = :p AND status NOT IN ('disabled', 'revoked') "
             " ORDER BY created_at, id"), {"p": project_id}).mappings().all()
    return [dict(r) for r in rows]


def _used_by(connection: Connection, org_id: Optional[str], project_id: str) -> dict[str, list[dict[str, Any]]]:
    """lower(full_name) -> the projects (of the org) that use it."""
    if org_id:
        rows = connection.execute(text(
            "SELECT lower(b.full_name) AS f, p.id AS project_id, p.name FROM github_repository_bindings b "
            "JOIN projects p ON p.id = b.project_id JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE w.org_id = :o AND b.status = 'active' ORDER BY p.name"), {"o": org_id}).mappings().all()
    else:
        rows = connection.execute(text(
            "SELECT lower(b.full_name) AS f, p.id AS project_id, p.name FROM github_repository_bindings b "
            "JOIN projects p ON p.id = b.project_id WHERE b.project_id = :p AND b.status = 'active'"),
            {"p": project_id}).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        bucket = out.setdefault(r["f"], [])
        if not any(x["project_id"] == r["project_id"] for x in bucket):
            bucket.append({"project_id": r["project_id"], "name": r["name"]})
    return out


def build(connection: Connection, settings: Any, project_id: str, repository: dict[str, Any],
          now: datetime) -> dict[str, Any]:
    """`{"accounts": [...], "repositories": [...]}` for GET /v1/setup (`github_accounts`,
    `repositories`). `repository` is the view's own repository section (its repos carry pills)."""
    org_id = _org_of(connection, project_id)
    repos_by_conn: dict[str, list[dict[str, Any]]] = {}
    for repo in repository.get("repos") or []:
        repos_by_conn.setdefault(str(repo.get("connection_id") or "install"), []).append(repo)

    accounts: list[dict[str, Any]] = []
    logins: set[str] = set()
    has_app_row = False
    for row in _safely(connection, lambda: _project_connections(connection, project_id), []):
        ref = str(row.get("credential_ref") or "")
        kind = ("org_app" if row["auth_method"] == "github_app" or ref == APP_REF
                else "personal_signin" if ref.startswith(SIGNIN_REF_PREFIX) else "token")
        has_app_row = has_app_row or kind == "org_app"
        mine = repos_by_conn.get(str(row["id"]), [])
        login = row.get("account_login")
        personal = True if kind == "personal_signin" else _owners_personal(login, mine)
        if login:
            logins.add(str(login).lower())
        expires = parse_time(row.get("expires_at")) if row.get("expires_at") else None
        accounts.append({
            "id": str(row["id"]), "connection_id": str(row["id"]), "scope": "project", "kind": kind,
            "name": (login or row.get("connection_label") or "GitHub") + ("" if personal is not False else " (org)"),
            "login": login, "how": _how(kind, personal), "personal": personal,
            "repo_count": len(mine), "expires_at": _iso(row.get("expires_at")),
            "pill": _status(mine, connection_status=row.get("status"), expires=expires, now=now, kind=kind),
            "created_at": _iso(row.get("created_at")),
        })

    install = repos_by_conn.get("install") or []
    conn_out = repository.get("connection") or {}
    if install and conn_out.get("source") == "install":
        expires = parse_time(conn_out.get("expires_at")) if conn_out.get("expires_at") else None
        login = conn_out.get("account_login")
        accounts.append({
            "id": "install", "connection_id": None, "scope": "install", "kind": "install",
            "name": login or "Server token", "login": login, "how": _how("install", None),
            "personal": None, "repo_count": len(install), "expires_at": conn_out.get("expires_at"),
            "pill": _status(install, connection_status=None, expires=expires, now=now, kind="install"),
            "created_at": None,
        })

    if org_id:
        accounts.extend(_org_accounts(connection, str(org_id), logins=logins, has_app_row=has_app_row,
                                      now=now))
    by_id = {a["id"]: a for a in accounts}
    return {"accounts": accounts,
            "repositories": _repositories(connection, project_id, org_id, repository, by_id)}


def _org_accounts(connection: Connection, org_id: str, *, logins: set[str], has_app_row: bool,
                  now: datetime) -> list[dict[str, Any]]:
    """The org's own GitHub credentials not already shown as one of this project's connections."""
    from ..identity import github_setup

    out: list[dict[str, Any]] = []
    mode = _safely(connection, lambda: github_setup.mode(connection, org_id), None)
    if mode and not has_app_row:
        app = mode.get("app") or {}
        if app.get("installed") and app.get("source") == "setup":
            verdict = (mode.get("verdict") or {}) if mode.get("mode") == "app" else {}
            tone = verdict.get("tone") or "green"
            personal = (app.get("account_type") == "User") if app.get("account_type") else None
            count = _safely(connection, lambda: connection.execute(text(
                "SELECT count(*) FROM github_repos WHERE org_id = :o AND reachable AND source = 'app'"),
                {"o": org_id}).scalar(), None)
            out.append({
                "id": "org_app", "connection_id": None, "scope": "org", "kind": "org_app",
                "name": (app.get("account") or "GitHub App") + ("" if personal else " (org)"),
                "login": app.get("account"), "how": _how("org_app", personal), "personal": personal,
                "repo_count": int(count) if count is not None else None, "expires_at": None,
                "pill": (_pill("connected", "green", "Connected", verdict.get("tip") or "")
                         if tone == "green" else
                         _pill("broken" if tone == "red" else "needs_you", tone,
                               "Broken" if tone == "red" else "Needs you", verdict.get("tip") or "",
                               "fix_link")),
                "created_at": None,
            })
    for tok in (mode or {}).get("tokens") or []:
        login = str(tok.get("login") or "")
        if login and login.lower() in logins:
            continue                          # the same account, already a row above
        repos = list(tok.get("repos") or [])
        owners = {r.split("/", 1)[0] for r in repos}
        personal = (owners == {login.lower()}) if login and repos else None
        out.append({
            "id": f"org_token:{tok['id']}", "connection_id": None, "scope": "org", "kind": "org_token",
            "name": (login or "Org token") + ("" if personal is not False else " (org)"),
            "login": login or None, "how": _how("org_token", personal), "personal": personal,
            "repo_count": len(repos), "expires_at": None,
            "pill": _pill("connected", "green", "Connected", "Builds use it for its repositories."),
            "created_at": None, "repos": repos,
        })
    return out


def _repositories(connection: Connection, project_id: str, org_id: Optional[str],
                  repository: dict[str, Any], accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    used = _safely(connection, lambda: _used_by(connection, org_id, project_id), {})
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repo in repository.get("repos") or []:
        full = str(repo["full_name"])
        account = accounts.get(str(repo.get("connection_id") or "install"))
        seen.add(full.lower())
        out.append({
            "full_name": full, "binding_id": repo.get("binding_id"), "in_project": True,
            "default": bool(repo.get("default")), "account_id": account["id"] if account else None,
            "via": account["name"] if account else None,
            "projects": used.get(full.lower(), []), "checks": repo.get("checks"), "pill": repo.get("pill"),
        })
    # THE ORG'S OTHER REPOSITORIES: allowed in the registry, not (yet) in this project.
    if org_id:
        rows = _safely(connection, lambda: [dict(r) for r in connection.execute(text(
            "SELECT full_name, source, checks FROM github_repos WHERE org_id = :o AND allowed "
            "AND reachable AND NOT archived ORDER BY lower(full_name)"), {"o": org_id}).mappings().all()], [])
        token_accounts = [a for a in accounts.values() if a["kind"] == "org_token"]
        for row in rows:
            full = str(row["full_name"])
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            via = None
            if row.get("source") == "app" and "org_app" in accounts:
                via = accounts["org_app"]
            else:
                via = next((a for a in token_accounts if full.lower() in [r.lower() for r in a.get("repos", [])]),
                           None)
            raw = row.get("checks")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            checks = raw if isinstance(raw, dict) else {}
            out.append({
                "full_name": full, "binding_id": None, "in_project": False, "default": False,
                "account_id": via["id"] if via else None, "via": via["name"] if via else None,
                "projects": used.get(full.lower(), []),
                "checks": {k: {"ok": (checks.get(k) or {}).get("ok"),
                               "state": str((checks.get(k) or {}).get("state") or "not_run"), "at": None}
                           for k in ("read", "write", "pull_requests")},
                "pill": None,
            })
    return out
