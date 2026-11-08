"""What Repo and models DOES: one connect per object, one check endpoint, atomic job choices.

Every mutation ends by returning the whole `setup_view` (the API does that), so the console
replaces its state rather than patching it, and nothing it shows was judged in the browser.

Credentials: a pasted token, key or auth.json travels from the request into the secret store
and nowhere else. Nothing here logs one, returns one, or puts any part of one in an error.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import setup_checks, setup_view
from .setup_checks import Http, iso, now_utc, parse_time


class SetupActionError(RuntimeError):
    """A refusal the console shows as a pill: {state, headline, fix} with an HTTP status."""

    def __init__(self, status: int, state: str, headline: str, fix: str = "",
                 extra: Optional[dict[str, Any]] = None):
        super().__init__(headline)
        self.status = status
        self.detail = {"state": state, "headline": headline, "fix": fix, **(extra or {})}


class ConsentRequired(SetupActionError):
    """No secret store on this server: the operator must agree to the database first."""

    def __init__(self, backend: str):
        super().__init__(409, "needs_consent", "Store in BOSS database",
                         "No secret store on this server. Encrypted in Postgres.",
                         {"error": "secret_store_fallback_not_accepted", "backend": backend,
                          "resend_with": {"accept_database_fallback": True}})


# ── One press, one effect ─────────────────────────────────────────────────────────────────────

_PRESSES: dict[str, float] = {}
_PRESS_LOCK = threading.Lock()


def first_press(project_id: str, key: Optional[str], window: float = 120.0) -> bool:
    """True the first time `key` is seen for this project within `window` seconds. A missing
    key is always a first press."""
    if not key:
        return True
    now = time.monotonic()
    tag = f"{project_id}:{key}"
    with _PRESS_LOCK:
        for old in [k for k, at in _PRESSES.items() if now - at > window]:
            _PRESSES.pop(old, None)
        if tag in _PRESSES:
            return False
        _PRESSES[tag] = now
        return True


# ── Consent ───────────────────────────────────────────────────────────────────────────────────

def require_store(connection: Connection, settings: Any, project_id: str, accept: bool,
                  actor: str) -> None:
    """Refuse before anything is stored when the only store is a database nobody agreed to."""
    state = setup_view.secrets_state(connection, settings, project_id, wait=True)
    if not state["needs_consent"]:
        return
    if not accept:
        raise ConsentRequired(state["backend"])
    from .core import emit_event

    # Remembered on its own connection: the answer stands even if this connect then fails.
    engine = getattr(connection, "engine", None)
    with (engine.begin() if engine is not None else _same(connection)) as own:
        own.execute(
            text("INSERT INTO project_settings (project_id, key, value, updated_by, updated_at) "
                 "VALUES (:p, :k, CAST('true' AS JSONB), :by, now()) "
                 "ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value, "
                 "updated_by = EXCLUDED.updated_by, updated_at = now()"),
            {"p": project_id, "k": setup_view.SECRET_CONSENT_KEY, "by": actor})
        emit_event(own, project_id, "secret.fallback_accepted", actor, "project", project_id,
                   {"backend": state["backend"], "via": "repo_and_models"})


class _same:
    """`with` over a connection that is already open."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def __enter__(self) -> Connection:
        return self.connection

    def __exit__(self, *_exc: Any) -> None:
        return None


# ── Repository ────────────────────────────────────────────────────────────────────────────────

GITHUB_LABEL = "GitHub"


def _service(settings: Any):
    from .github_connections import GitHubConnectionService

    return GitHubConnectionService(settings)


def _api_base(settings: Any) -> str:
    return str(getattr(settings, "github_api_base_url", "") or "https://api.github.com")


def _dedupe_label(connection: Connection, project_id: str, login: str) -> str:
    """`login`, or `"{login} N"` for the first N >= 2 not already a label this project has."""
    existing = set(connection.execute(
        text("SELECT connection_label FROM github_connections WHERE project_id = :p"),
        {"p": project_id}).scalars().all())
    if login not in existing:
        return login
    n = 2
    while f"{login} {n}" in existing:
        n += 1
    return f"{login} {n}"


def repository_token(connection: Connection, settings: Any, project_id: str, token: str, *,
                     connection_id: Optional[str] = None, accept_database_fallback: bool = False,
                     actor: str = "ceo", http: Optional[Http] = None) -> dict[str, Any]:
    """Check a pasted token, store it, and list what it can see.

    A token GitHub refuses is never stored. Pasting with `connection_id` — `Replace token` —
    re-points that connection, so its repository bindings and history stay. Pasting with no
    `connection_id` ALWAYS adds another account: a project can hold several GitHub connections
    (e.g. a personal account and an org account), each owning its own repository bindings, each
    labelled from the paste's account login (deduped against the project's other labels).
    """
    token = (token or "").strip()
    if not token:
        raise SetupActionError(422, "token_rejected", "Paste token", "Paste a GitHub token.")
    # THE DECISION FIRST: nothing is sent anywhere until the operator has agreed where it lives.
    require_store(connection, settings, project_id, accept_database_fallback, actor)
    found = setup_checks.inspect_token(token, api_base=_api_base(settings), http=http)
    if not found["ok"]:
        from .setup_pills import REPOSITORY, pill

        shown = pill("repository", found["state"], time=setup_view._hhmm(found["retry_at"]),
                     repo="that repository") if found["state"] in REPOSITORY else {
            "label": "Token rejected", "tip": "GitHub refused this token."}
        raise SetupActionError(422, found["state"], shown["label"], shown["tip"])
    service = _service(settings)
    credential_ref = service.store_token_credential(
        connection, project_id=project_id, token=token, auth_method="personal_access_token",
        label=GITHUB_LABEL, actor=actor)
    existing = service.get_connection(connection, project_id, connection_id) if connection_id else None
    if existing is not None:
        current = service.repoint_connection(
            connection, project_id=project_id, connection_id=existing["id"],
            credential_ref=credential_ref, auth_method="personal_access_token", app_id=None,
            installation_id=None, api_base_url=_api_base(settings), actor=actor)
        if current["status"] in ("disabled", "revoked"):
            connection.execute(text("UPDATE github_connections SET status = 'configured', "
                                    "disabled_at = NULL, revoked_at = NULL WHERE id = :id"),
                               {"id": current["id"]})
    else:
        label = _dedupe_label(connection, project_id, found["account_login"] or GITHUB_LABEL)
        current = service.create_connection(
            connection, project_id=project_id, connection_label=label,
            auth_method="personal_access_token", credential_ref=credential_ref, app_id=None,
            installation_id=None, api_base_url=_api_base(settings),
            required_permissions=["pull", "push", "triage"],
            metadata={"setup": "repo_and_models"}, actor=actor)
    connection.execute(
        text("UPDATE github_connections SET account_login = :login, expires_at = :exp, "
             "updated_at = now() WHERE id = :id"),
        {"login": found["account_login"], "exp": parse_time(found["expires_at"]),
         "id": current["id"]})
    # REPLACE TOKEN RE-PROVES WHAT IT SERVES. The repositories already bound to this
    # connection are checked with the new token now, on the same bindings — so a fixed token
    # turns the same row green (no duplicate row, no "already has" error).
    rechecked = []
    if existing is not None:
        bound = connection.execute(
            text("SELECT id FROM github_repository_bindings WHERE project_id = :p "
                 "AND connection_id = :c AND status = 'active'"),
            {"p": project_id, "c": current["id"]}).scalars().all()
        for binding_id in bound:
            check_repository(connection, settings, project_id, str(binding_id), actor=actor,
                             http=http)
            rechecked.append(str(binding_id))
    if rechecked:
        # A REPLACED TOKEN REACHES BUILDS TOO. Only select_repository mirrored, so a token pasted
        # over a connection whose repo was already bound (the installer binds it; Replace token
        # re-points it) was rechecked green here and never reached the vault: Setup said the repo
        # worked while GitHub access said "Builds can't reach GitHub".
        _mirror_token_into_vault(connection, service, project_id, current["id"], actor)
    return {"account_login": found["account_login"], "expires_at": found["expires_at"],
            "repos": found["repos"], "connection_id": current["id"], "rechecked": rechecked}


APP_LABEL = "GitHub App"
APP_CREDENTIAL_REF = "identity_github:app"


def repository_from_app(connection: Connection, settings: Any, project_id: str, *,
                        actor: str = "ceo", list_repos: bool = True) -> dict[str, Any]:
    """Use the GitHub App connected in Setup › GitHub access: no token to paste. The project gets
    one connection whose credential is the App itself (`identity_github:app`, a fresh installation
    token at each use) and the picker lists the repositories the App is installed on.
    `list_repos=False` only makes sure the connection exists (the multi-repo registry already
    knows which repositories the App reaches)."""
    from ..identity import github_setup

    try:
        repos = github_setup.installation_repositories(connection) if list_repos else []
    except github_setup.GitHubSetupError as error:
        if error.code == "app_not_connected":
            raise SetupActionError(409, "app_not_connected", "Connect the App",
                                   "Connect a GitHub App in GitHub access first.") from None
        raise SetupActionError(502, "unreachable", "GitHub unreachable",
                               "GitHub did not list the App's repositories. Try again.") from None
    service = _service(settings)
    ref_kind, _, ref_key = APP_CREDENTIAL_REF.partition(":")
    have_ref = connection.execute(
        text("SELECT id FROM secret_refs WHERE project_id = :p AND ref_kind = :k AND ref_key = :r"),
        {"p": project_id, "k": ref_kind, "r": ref_key}).scalar()
    if have_ref is None:
        service.secret_refs.create_secret_ref(
            connection, project_id=project_id, ref_kind=ref_kind, ref_key=ref_key, provider="github",
            status="active", metadata={"source": "github_app"}, actor=actor)
    else:
        connection.execute(text("UPDATE secret_refs SET status = 'active', revoked_at = NULL WHERE id = :i"),
                           {"i": have_ref})
    existing = service._connection_by_label(connection, project_id, APP_LABEL)
    if existing is None:
        current = service.create_connection(
            connection, project_id=project_id, connection_label=APP_LABEL,
            auth_method="personal_access_token", credential_ref=APP_CREDENTIAL_REF, app_id=None,
            installation_id=None, api_base_url=_api_base(settings),
            required_permissions=["pull", "push", "triage"],
            metadata={"setup": "repo_and_models", "source": "github_app"}, actor=actor)
    else:
        current = existing
        if current["status"] in ("disabled", "revoked", "error"):
            connection.execute(text("UPDATE github_connections SET status = 'configured', "
                                    "disabled_at = NULL, revoked_at = NULL WHERE id = :id"),
                               {"id": current["id"]})
    account = github_setup.app_config(connection)["account_login"]
    connection.execute(text("UPDATE github_connections SET account_login = :l, updated_at = now() "
                            "WHERE id = :id"), {"l": account, "id": current["id"]})
    return {"account_login": account, "expires_at": None, "repos": repos,
            "connection_id": current["id"], "rechecked": [], "source": "github_app"}


SIGNIN_LABEL = "GitHub (signed in)"
SIGNIN_REF_PREFIX = "identity_github:user:"


def _signin_refusal(error: Exception) -> SetupActionError:
    code = getattr(error, "code", "")
    if code in ("not_signed_in", "signin_expired"):
        return SetupActionError(409, code, "Sign in with GitHub", "Sign in with GitHub first.")
    return SetupActionError(502, "unreachable", "GitHub unreachable",
                            "GitHub did not list your repositories. Try again.")


def repository_from_signin(connection: Connection, settings: Any, project_id: str, *, person_id: str,
                           actor: str = "ceo", list_repos: bool = True) -> dict[str, Any]:
    """Pick repos after "Sign in with GitHub" (identity/github_device): the picker lists what THIS
    person can push to where the BOSS App is installed — GitHub's own answer for their token. The
    project gets one connection whose credential is that person's own sign-in
    (`identity_github:user:<person>`): it is NOT mirrored into the org's GitHub token, so no other
    person's build is lent it, and builds still need the person's grant to say "act as me"."""
    from ..identity import github_app, github_device

    if not str(person_id or "").startswith("usr_"):
        raise SetupActionError(403, "people_only", "Sign in first",
                               "Only a signed-in person can connect their own GitHub.")
    try:
        found = github_device.reach(person_id, db=connection) if list_repos else {"repos": [], "login": None}
    except github_app.GitHubRefused as error:
        raise _signin_refusal(error) from None
    service = _service(settings)
    ref = f"{SIGNIN_REF_PREFIX}{person_id}"
    ref_kind, _, ref_key = ref.partition(":")
    have_ref = connection.execute(
        text("SELECT id FROM secret_refs WHERE project_id = :p AND ref_kind = :k AND ref_key = :r"),
        {"p": project_id, "k": ref_kind, "r": ref_key}).scalar()
    if have_ref is None:
        service.secret_refs.create_secret_ref(
            connection, project_id=project_id, ref_kind=ref_kind, ref_key=ref_key, provider="github",
            status="active", metadata={"source": "github_signin"}, actor=actor)
    else:
        connection.execute(text("UPDATE secret_refs SET status = 'active', revoked_at = NULL WHERE id = :i"),
                           {"i": have_ref})
    existing = connection.execute(
        text("SELECT id, status FROM github_connections WHERE project_id = :p AND credential_ref = :r "
             "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "r": ref}).mappings().first()
    if existing is None:
        current = service.create_connection(
            connection, project_id=project_id, connection_label=f"{SIGNIN_LABEL} · {person_id}"[:120],
            auth_method="personal_access_token", credential_ref=ref, app_id=None,
            installation_id=None, api_base_url=_api_base(settings),
            required_permissions=["pull", "push", "triage"],
            metadata={"setup": "repo_and_models", "source": "github_signin"}, actor=actor)
    else:
        current = dict(existing)
        if current["status"] in ("disabled", "revoked", "error"):
            connection.execute(text("UPDATE github_connections SET status = 'configured', "
                                    "disabled_at = NULL, revoked_at = NULL WHERE id = :id"),
                               {"id": current["id"]})
    if found.get("login"):
        connection.execute(text("UPDATE github_connections SET account_login = :l, updated_at = now() "
                                "WHERE id = :id"), {"l": found["login"], "id": current["id"]})
    return {"account_login": found.get("login"), "expires_at": None, "repos": found.get("repos") or [],
            "connection_id": current["id"], "rechecked": [], "source": "github_signin",
            "install_needed": found.get("install_needed") or [], "read_only": found.get("read_only") or 0}


def _signin_person(connection: Connection, project_id: str, connection_id: str) -> Optional[str]:
    """The person whose own sign-in this connection is, or None for any other connection."""
    ref = connection.execute(text("SELECT credential_ref FROM github_connections WHERE project_id = :p "
                                  "AND id = :c"), {"p": project_id, "c": connection_id}).scalar()
    ref = str(ref or "")
    return ref[len(SIGNIN_REF_PREFIX):] if ref.startswith(SIGNIN_REF_PREFIX) else None


def _is_app_connection(connection: Connection, project_id: str, connection_id: str) -> bool:
    ref = connection.execute(text("SELECT credential_ref FROM github_connections WHERE project_id = :p "
                                  "AND id = :c"), {"p": project_id, "c": connection_id}).scalar()
    return ref == APP_CREDENTIAL_REF


def connection_repos(connection: Connection, settings: Any, project_id: str,
                     connection_id: str, *, http: Optional[Http] = None) -> dict[str, Any]:
    """`+ Add repo`: the repositories the STORED token can see (no paste needed)."""
    if _is_app_connection(connection, project_id, connection_id):
        out = repository_from_app(connection, settings, project_id)
        return {k: out[k] for k in ("account_login", "expires_at", "repos", "connection_id")}
    person = _signin_person(connection, project_id, connection_id)
    if person:
        out = repository_from_signin(connection, settings, project_id, person_id=person)
        return {k: out[k] for k in ("account_login", "expires_at", "repos", "connection_id")}
    service = _service(settings)
    try:
        ref = service.secret_refs.require_usable(
            connection, project_id=project_id,
            credential_ref=service._credential_ref(connection, project_id, connection_id))
        token = service._resolve_secret(ref, connection)
    except Exception:  # noqa: BLE001 — an unreadable token is one to replace
        raise SetupActionError(422, "token_rejected", "Token rejected",
                               "Paste a new token.") from None
    found = setup_checks.inspect_token(token, api_base=_api_base(settings), http=http)
    if not found["ok"]:
        raise SetupActionError(422, found["state"], "Token rejected", "Paste a new token.")
    return {"account_login": found["account_login"], "expires_at": found["expires_at"],
            "repos": found["repos"], "connection_id": connection_id}


#: Add repo's pick for a repository the org's GitHub App reaches when this project has no App
#: connection yet: the select makes that connection first (repository_from_app), then binds.
ORG_APP_PICK = "org_app"


def _app_connection_id(connection: Connection, project_id: str) -> Optional[str]:
    """This project's connection whose credential is the org's GitHub App, whatever its label."""
    found = connection.execute(
        text("SELECT id FROM github_connections WHERE project_id = :p AND credential_ref = :r "
             "AND status NOT IN ('disabled', 'revoked') ORDER BY created_at, id LIMIT 1"),
        {"p": project_id, "r": APP_CREDENTIAL_REF}).scalar()
    return str(found) if found else None


def _app_repos(connection: Connection) -> list[dict[str, Any]]:
    """The org App's repositories, refused in Setup's words."""
    from ..identity import github_setup

    try:
        return github_setup.installation_repositories(connection)
    except github_setup.GitHubSetupError as error:
        if error.code == "app_not_connected":
            raise SetupActionError(409, "app_not_connected", "Connect the App",
                                   "Connect a GitHub App in GitHub access first.") from None
        raise SetupActionError(502, "unreachable", "GitHub unreachable",
                               "GitHub did not list the App's repositories. Try again.") from None
    except Exception:  # noqa: BLE001 — a GitHub refusal of any shape is this account's, not the list's
        raise SetupActionError(502, "unreachable", "GitHub unreachable",
                               "GitHub did not list the App's repositories. Try again.") from None


def available_repositories(connection: Connection, settings: Any, project_id: str, *,
                           http: Optional[Http] = None) -> dict[str, Any]:
    """`+ Add repo`: the repositories EVERY connected account can reach, one group per account.

    Add repo listed only one connection's repositories (operator, 2026-11-06: the personal
    token's, while paramg/helpdesk-ai-demo, newly granted to the BOSS GitHub App, never appeared;
    "From another account" opened the whole Add-account wizard). Every source is asked here:
    this project's connections (a pasted token, a sign-in, the App) and the org's GitHub App
    when this project has no connection to it yet. An account GitHub refuses says so in its own
    group; the others still list. A repository two accounts reach is listed once, under the
    first. Each group's `connection_id` is what the select sends (`org_app`: connect the App to
    this project first, through repository_from_app). Nothing is written here."""
    from ..identity import github_setup
    from . import setup_accounts

    groups: list[dict[str, Any]] = []
    rows = setup_accounts._safely(  # noqa: SLF001 — the same reader the accounts block uses
        connection, lambda: setup_accounts._project_connections(connection, project_id), [])  # noqa: SLF001
    has_app = False
    for row in rows:
        cid = str(row["id"])
        ref = str(row.get("credential_ref") or "")
        kind = ("org_app" if row.get("auth_method") == "github_app" or ref == APP_CREDENTIAL_REF
                else "personal_signin" if ref.startswith(SIGNIN_REF_PREFIX) else "token")
        if kind == "org_app" and has_app:
            continue                     # one App, one group
        has_app = has_app or kind == "org_app"
        group: dict[str, Any] = {
            "id": cid, "connection_id": cid, "kind": kind,
            "account": row.get("account_login") or row.get("connection_label") or "GitHub",
            "repos": [], "error": None}
        try:
            if kind == "org_app":
                group["repos"] = _app_repos(connection)
            else:
                found = connection_repos(connection, settings, project_id, cid, http=http)
                group["repos"] = list(found.get("repos") or [])
                group["account"] = found.get("account_login") or group["account"]
        except SetupActionError as error:
            group["error"] = error.detail
        groups.append(group)
    if not has_app:
        cfg = setup_accounts._safely(connection, lambda: github_setup.app_config(connection), None)  # noqa: SLF001
        ready = bool(cfg) and cfg.get("source") == "setup" and setup_accounts._safely(  # noqa: SLF001
            connection, lambda: github_setup.app_ready(connection), False)
        if ready:
            group = {"id": ORG_APP_PICK, "connection_id": ORG_APP_PICK, "kind": "org_app",
                     "account": cfg.get("account_login") or "GitHub App", "repos": [], "error": None}
            try:
                group["repos"] = _app_repos(connection)
            except SetupActionError as error:
                group["error"] = error.detail
            groups.append(group)
    bound = {str(r).lower() for r in connection.execute(
        text("SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
             "AND status = 'active'"), {"p": project_id}).scalars()}
    seen: set[str] = set()
    for group in groups:
        kept = []
        for repo in group["repos"]:
            name = str((repo or {}).get("full_name") or "")
            if "/" not in name or name.lower() in seen:
                continue
            seen.add(name.lower())
            kept.append({"full_name": name, "private": bool(repo.get("private")),
                         "default_branch": repo.get("default_branch"),
                         "owner_type": repo.get("owner_type"), "in_project": name.lower() in bound})
        group["repos"] = kept
    return {"groups": groups}


def resolve_pick_connection(connection: Connection, settings: Any, project_id: str, connection_id: str,
                            *, actor: str = "ceo") -> str:
    """The connection a picked repository binds through. `org_app` is the org's GitHub App: this
    project's App connection, made through repository_from_app when it has none yet."""
    if connection_id != ORG_APP_PICK:
        return connection_id
    existing = _app_connection_id(connection, project_id)
    if existing:
        return existing
    return str(repository_from_app(connection, settings, project_id, actor=actor, list_repos=False)
               ["connection_id"])


def _promote(connection: Connection, project_id: str, binding_id: str, actor: str) -> None:
    """Make one binding the project's default (`primary`); the old default becomes `source`."""
    from .core import emit_event

    connection.execute(
        text("UPDATE github_repository_bindings SET role = 'source', updated_at = now() "
             "WHERE project_id = :p AND role = 'primary' AND status = 'active' AND id <> :b"),
        {"p": project_id, "b": binding_id})
    connection.execute(
        text("UPDATE github_repository_bindings SET role = 'primary', updated_at = now() "
             "WHERE project_id = :p AND id = :b"), {"p": project_id, "b": binding_id})
    emit_event(connection, project_id, "github.default_repository_set", actor,
               "github_repository_binding", binding_id, {})


def select_repository(connection: Connection, settings: Any, project_id: str, connection_id: str,
                      full_name: str, *, make_default: bool = True, actor: str = "ceo",
                      http: Optional[Http] = None) -> str:
    """Bind a repository to the connection and check it. Returns the binding id."""
    service = _service(settings)
    current = service.get_connection(connection, project_id, connection_id)
    _owner, _name, full = service._normalize_repository(full_name)
    person = _signin_person(connection, project_id, connection_id)
    if person:
        # PICK, SCOPED ON THE SERVER: a signed-in person's connection binds only a repository
        # GitHub lists for THEIR token (pushable, where the BOSS App is installed) — never one
        # typed into the request that they cannot reach.
        from ..identity import github_app, github_device

        try:
            ok = github_device.reachable(person, full, db=connection)
        except github_app.GitHubRefused as error:
            raise _signin_refusal(error) from None
        if not ok:
            raise SetupActionError(403, "repo_not_reachable", "Not one of yours",
                                   f"{full} is not a repository you can push to where BOSS is installed.")
    row = connection.execute(
        text("SELECT id, status FROM github_repository_bindings "
             "WHERE project_id = :p AND lower(full_name) = lower(:f)"),
        {"p": project_id, "f": full}).mappings().first()
    has_primary = connection.execute(
        text("SELECT 1 FROM github_repository_bindings WHERE project_id = :p "
             "AND role = 'primary' AND status = 'active'"), {"p": project_id}).first()
    if row is not None:
        binding_id = str(row["id"])
        connection.execute(
            text("UPDATE github_repository_bindings SET status = 'active', "
                 "connection_id = :c, updated_at = now() WHERE id = :b"),
            {"c": current["id"], "b": binding_id})
    else:
        binding = service.create_repository_binding(
            connection, project_id=project_id, connection_id=current["id"], repository=full,
            role="source", default_branch=None, metadata={"setup": "repo_and_models"},
            actor=actor)
        binding_id = binding["id"]
    if make_default or not has_primary:
        _promote(connection, project_id, binding_id, actor)
    check_repository(connection, settings, project_id, binding_id, actor=actor, http=http)
    _mirror_token_into_vault(connection, service, project_id, current["id"], actor)
    return binding_id


def _mirror_token_into_vault(connection: Connection, service: Any, project_id: str, connection_id: str,
                             actor: str) -> None:
    """A token pasted in Setup › Repo becomes the vault's org GitHub connection, scoped to the repos
    bound to it. Under enforce that is the only way a build reaches GitHub with it: boss-egress reads it
    from the vault, the sandbox never sees it (identity/github_setup.py).

    D1: ONLY for a person who may change the org's GitHub connection (github:connect at the ORG).
    Setup › Repo is a project page: a project-bound Admin's token stays this project's connection
    and the org token is neither replaced, widened nor revoked — the event says so in words."""
    from ..identity import github_setup, vault

    if _is_app_connection(connection, project_id, connection_id):
        return                             # the App's own tokens: nothing to keep
    if _signin_person(connection, project_id, connection_id):
        return                             # a person's own sign-in: never lent to the org's builds
    org_of_project = connection.execute(
        text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
             "WHERE p.id = :p"), {"p": project_id}).scalar()
    from .core import emit_event

    def unchanged(reason: str, message: str) -> None:
        emit_event(connection, project_id, "github.org_token_unchanged", actor, "github_connection",
                   connection_id, {"reason": reason, "message": message})

    refusal = (github_setup.org_credentials_refusal(str(org_of_project), db=connection)
               if org_of_project else None)
    if refusal == "step_up":
        unchanged("step_up_required", (
            "Builds can't use this token yet: changing the organisation's GitHub token needs you to "
            "confirm it's you with your second factor. Confirm, then press Replace token and paste "
            "it again."))
        return
    if refusal:
        unchanged("org_scope_required", (
            "This token was kept for this project only. The organisation's GitHub token, which "
            "builds use, was not changed: that needs the Connect GitHub permission for the whole "
            "organisation. Ask an Owner or org Admin to add this repository under Identity › "
            "GitHub."))
        return
    try:
        ref = service.secret_refs.require_usable(
            connection, project_id=project_id,
            credential_ref=service._credential_ref(connection, project_id, connection_id))
        token = service._resolve_secret(ref, connection)
        org_id = connection.execute(
            text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                 "WHERE p.id = :p"), {"p": project_id}).scalar()
        if not token or not org_id:
            return
        repos = [str(r) for r in connection.execute(
            text("SELECT full_name FROM github_repository_bindings WHERE connection_id = :c "
                 "AND status = 'active'"), {"c": connection_id}).scalars()]
        # SEVERAL ACCOUNTS (BOSSAPP-808): the org may already hold this very token (add the repos
        # to it), or another account's token — which is KEPT: a second account never takes the
        # first one's repositories away. Only an older token of the same login is retired.
        for current in github_setup.org_tokens(connection, str(org_id)):
            if vault.org_github_token(current["id"], db=connection) == token:
                for repo in repos:
                    github_setup.add_repo_to_org_token(repo=repo, org_id=str(org_id), db=connection,
                                                       token_id=current["id"])
                return
        login = connection.execute(
            text("SELECT account_login FROM github_connections WHERE id = :c"),
            {"c": connection_id}).scalar()
        with connection.begin_nested():
            github_setup.store_org_token(token=token, repos=repos,
                                         org_id=str(org_id), db=connection,
                                         by=actor if actor.startswith("usr_") else "", check=False,
                                         keep_others=True, login=str(login) if login else None)
    except Exception as exc:  # noqa: BLE001 — Setup's own check already said whether the token works
        import logging

        logging.getLogger(__name__).warning("setup: the GitHub token was not kept as the org connection (%s: %s)",
                                            type(exc).__name__, getattr(exc, "code", ""))
        # SAID, NOT ONLY LOGGED: a warning in the API log is invisible to the person on the page.
        unchanged("store_failed", (
            "Builds can't use this token yet: BOSS could not keep it as the organisation's GitHub "
            f"token ({getattr(exc, 'code', '') or type(exc).__name__}). Paste it under Identity › "
            "GitHub, or run `boss doctor` on the server."))


def remove_repository(connection: Connection, project_id: str, binding_id: str,
                      actor: str = "ceo") -> None:
    from .core import emit_event

    row = connection.execute(
        text("SELECT role FROM github_repository_bindings WHERE project_id = :p AND id = :b"),
        {"p": project_id, "b": binding_id}).mappings().first()
    if row is None:
        raise SetupActionError(404, "not_connected", "Not connected", "No such repository.")
    connection.execute(
        text("UPDATE github_repository_bindings SET status = 'disabled', role = "
             "CASE WHEN role = 'primary' THEN 'source' ELSE role END, updated_at = now() "
             "WHERE id = :b"), {"b": binding_id})
    if row["role"] == "primary":
        nxt = connection.execute(
            text("SELECT id FROM github_repository_bindings WHERE project_id = :p "
                 "AND status = 'active' ORDER BY created_at LIMIT 1"),
            {"p": project_id}).scalar()
        if nxt:
            _promote(connection, project_id, str(nxt), actor)
    emit_event(connection, project_id, "github.repository_removed", actor,
               "github_repository_binding", binding_id, {})


def make_default(connection: Connection, project_id: str, binding_id: str,
                 actor: str = "ceo") -> None:
    _promote(connection, project_id, binding_id, actor)


def _receipt(connection: Connection, project_id: str, effect_kind: str, scope: dict[str, Any]
             ) -> str:
    from .receipts import ReceiptService

    owner = connection.execute(
        text("SELECT w.org_id, p.workspace_id FROM projects p "
             "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
        {"p": project_id}).mappings().first()
    if owner is None:
        return ""
    receipt, _ = ReceiptService().create_or_reuse(
        connection, str(owner["org_id"]), str(owner["workspace_id"]), project_id,
        f"setup-check:{effect_kind}:{uuid.uuid4().hex[:16]}", effect_kind, scope,
        {"operation": "setup_check"}, data_boundary="internal")
    return str(receipt["id"])


def check_repository(connection: Connection, settings: Any, project_id: str,
                     binding_id: str, *, actor: str = "ceo",
                     http: Optional[Http] = None) -> dict[str, Any]:
    """Run the three probes against one repository and record what they found."""
    from .core import emit_event
    from .receipts import ReceiptService

    if binding_id == "install":
        return _check_install_repository(connection, settings, project_id, actor=actor, http=http)
    local = connection.execute(
        text("SELECT full_name FROM github_repository_bindings WHERE project_id = :p AND id = :b "
             "AND host = 'local'"), {"p": project_id, "b": binding_id}).mappings().first()
    if local is not None:
        return _check_local_repository(connection, settings, project_id, binding_id,
                                       local["full_name"], actor=actor)
    row = connection.execute(
        text("SELECT b.full_name, b.connection_id, c.api_base_url, c.status "
             "FROM github_repository_bindings b JOIN github_connections c "
             "ON c.id = b.connection_id WHERE b.project_id = :p AND b.id = :b"),
        {"p": project_id, "b": binding_id}).mappings().first()
    if row is None:
        raise SetupActionError(404, "not_connected", "Not connected", "No such repository.")
    service = _service(settings)
    try:
        ref = service.secret_refs.require_usable(
            connection, project_id=project_id,
            credential_ref=service._credential_ref(connection, project_id, row["connection_id"]))
        token = service._resolve_secret(ref, connection)
    except Exception:  # noqa: BLE001 — an unreadable token is a token GitHub cannot accept
        token = ""
    receipt_id = _receipt(connection, project_id, "github.repository.check",
                          {"repository": row["full_name"], "repository_binding_id": binding_id})
    if token:
        found = setup_checks.probe_repository(token, row["full_name"],
                                              api_base=row["api_base_url"] or _api_base(settings),
                                              http=http)
    else:
        at = iso(now_utc())
        found = {"state": "token_rejected", "expires_at": None, "retry_at": None,
                 "default_branch": None, "private": None,
                 "checks": {"read": {"ok": False, "state": "token_rejected", "at": at}}}
    checks = {**found["checks"], "state": found["state"], "retry_at": found.get("retry_at")}
    connection.execute(
        text("UPDATE github_repository_bindings SET checks = CAST(:c AS JSONB), "
             "checked_at = now(), default_branch = COALESCE(:db, default_branch), "
             "visibility = COALESCE(:vis, visibility), last_synced_at = now(), "
             "updated_at = now() WHERE id = :b"),
        {"c": json.dumps(checks), "db": found.get("default_branch"),
         "vis": (None if found.get("private") is None
                 else "private" if found["private"] else "public"), "b": binding_id})
    ok = found["state"] == "working"
    transient = found["state"] in ("rate_limited", "unreachable")
    connection.execute(
        text("UPDATE github_connections SET "
             "status = CASE WHEN :transient THEN status WHEN :ok THEN 'active' ELSE 'error' END, "
             "last_test_status = CASE WHEN :transient THEN last_test_status "
             "                        WHEN :ok THEN 'passed' ELSE 'failed' END, "
             "last_tested_at = now(), expires_at = COALESCE(:exp, expires_at), "
             "updated_at = now() WHERE id = :c"),
        {"ok": ok, "transient": transient, "exp": parse_time(found.get("expires_at")),
         "c": row["connection_id"]})
    if receipt_id:
        receipts = ReceiptService()
        summary = {"state": found["state"],
                   "checks": {k: v.get("ok") for k, v in found["checks"].items()}}
        if ok:
            receipts.mark_succeeded(connection, receipt_id, row["full_name"], summary)
        else:
            receipts.mark_failed(connection, receipt_id, summary)
    emit_event(connection, project_id, "setup.repository_checked", actor,
               "github_repository_binding", binding_id,
               {"state": found["state"], "repository": row["full_name"],
                "receipt_id": receipt_id})
    return found


def _check_local_repository(connection: Connection, settings: Any, project_id: str,
                            binding_id: str, full_name: str, *, actor: str) -> dict[str, Any]:
    """A repository BOSS keeps on this machine (BOSSAPP-918): no GitHub to ask. Read = the bare
    repository answers git; write and pull requests = BOSS owns it (commits and change sets are
    its own). Recorded like a GitHub probe so the dots, Setup and readiness read one shape."""
    from . import local_repo
    from .core import emit_event

    owner, _, name = str(full_name).partition("/")
    at = iso(now_utc())
    try:
        described = local_repo.describe(owner, name, settings)
        ok, why = bool(described.get("head_sha")) or local_repo.exists(owner, name, settings), ""
    except Exception as error:  # noqa: BLE001 — a missing or broken repository, in words
        described, ok, why = {}, False, str(error)[:200]
    if ok:
        part = {"ok": True, "state": "working", "at": at, "detail": "local"}
        checks = {"read": part, "write": dict(part), "pull_requests": dict(part),
                  "state": "working", "retry_at": None}
        state = "working"
    else:
        part = {"ok": False, "state": "no_access", "at": at,
                "detail": why or "The repository is not on this machine."}
        checks = {"read": part, "state": "no_access", "retry_at": None}
        state = "no_access"
    connection.execute(
        text("UPDATE github_repository_bindings SET checks = CAST(:c AS JSONB), "
             "checked_at = now(), default_branch = COALESCE(:db, default_branch), "
             "updated_at = now() WHERE id = :b"),
        {"c": json.dumps(checks), "db": described.get("default_branch"), "b": binding_id})
    emit_event(connection, project_id, "setup.repository_checked", actor,
               "github_repository_binding", binding_id,
               {"state": state, "repository": full_name, "host": "local"})
    return {"state": state, "expires_at": None, "retry_at": None, "host": "local",
            "default_branch": described.get("default_branch"), "private": True,
            "checks": {k: v for k, v in checks.items() if isinstance(v, dict)}}


def _check_install_repository(connection: Connection, settings: Any, project_id: str, *,
                              actor: str, http: Optional[Http]) -> dict[str, Any]:
    from .core import emit_event

    owner, name, token = setup_view.install_repository(settings)
    if not (owner and name and token):
        raise SetupActionError(404, "not_connected", "Not connected", "No repository.")
    full = f"{owner}/{name}"
    receipt_id = _receipt(connection, project_id, "github.repository.check",
                          {"repository": full, "source": "install"})
    found = setup_checks.probe_repository(token, full, api_base=_api_base(settings), http=http)
    login = None
    if found["state"] == "working":
        who = setup_checks.inspect_token(token, api_base=_api_base(settings), http=http)
        login = who.get("account_login")
    if receipt_id:
        from .receipts import ReceiptService

        summary = {"state": found["state"]}
        if found["state"] == "working":
            ReceiptService().mark_succeeded(connection, receipt_id, full, summary)
        else:
            ReceiptService().mark_failed(connection, receipt_id, summary)
    emit_event(connection, project_id, "setup.repository_checked", actor, "repository",
               "install", {"state": found["state"], "checks": found["checks"],
                           "expires_at": found.get("expires_at"),
                           "retry_at": found.get("retry_at"),
                           "default_branch": found.get("default_branch"),
                           "private": found.get("private"), "account_login": login,
                           "receipt_id": receipt_id})
    return found


# ── Models ────────────────────────────────────────────────────────────────────────────────────

def _subscription_spec(settings: Any, provider: str):
    from .cli_harness import load_specs

    return load_specs(settings).get(provider)


def _record_model_check(connection: Connection, project_id: str, source_id: str,
                        result: dict[str, Any], actor: str) -> None:
    """Write a model check onto its source (or, for an install key, as an event)."""
    from .core import emit_event

    if result.get("state") == "paused_budget" or result.get("capacity_refused"):
        # Not a check: the budget refused to spend on it. Nothing is recorded, so the row keeps
        # what it last knew and reads "Paused · budget" exactly while the budget says so.
        return
    last = {"ok": bool(result.get("ok")), "state": result.get("state") or "failed",
            "at": result.get("checked_at") or iso(now_utc()), "latency_ms": result.get("latency_ms"),
            "retry_at": result.get("retry_at"), "host": result.get("host"),
            "receipt_id": result.get("receipt_id")}
    if source_id.startswith("env-"):
        emit_event(connection, project_id, "setup.model_checked", actor, "model_source",
                   source_id, last)
        return
    connection.execute(
        text("UPDATE entitlement_sources SET connection_metadata = "
             "COALESCE(connection_metadata, '{}'::jsonb) || CAST(:m AS JSONB), "
             "updated_at = now() WHERE project_id = :p AND source_id = :s"),
        {"m": json.dumps({"last_check": last, "checked_at": last["at"],
                          "verification_state": "verified" if last["ok"] else "failed"}),
         "p": project_id, "s": source_id})
    if last["state"] == "rate_limited":
        capacity = {"exhausted": True, "retry_at": last["retry_at"],
                    "detail": "Plan limit reached during a setup check."}
    elif last["ok"]:
        capacity = {"exhausted": False, "credential_state": "ok"}
    elif last["state"] in ("login_expired", "key_rejected"):
        capacity = {"credential_state": "rejected"}
    else:
        capacity = None
    if capacity is not None:
        connection.execute(
            text("UPDATE entitlement_sources SET observed_capacity = "
                 "COALESCE(observed_capacity, '{}'::jsonb) || CAST(:c AS JSONB) "
                 "WHERE project_id = :p AND source_id = :s"),
            {"c": json.dumps(capacity), "p": project_id, "s": source_id})
    emit_event(connection, project_id, "setup.model_checked", actor, "model_source", source_id,
               last)


def _engine(connection: Connection):
    engine = getattr(connection, "engine", None)
    if engine is None:
        raise SetupActionError(503, "failed", "Check failed",
                               "The check needs its own database connection.")
    return engine


def _key_proof(connection: Connection, settings: Any, project_id: str, provider: str, *,
               api_key: str, base_url: str, model: str, actor: str,
               http: Optional[Http], intent: str = "", capacity_group: str = "",
               backend: str = "", env_name: str = "", request_target: str = "") -> dict[str, Any]:
    """One governed completion on an encrypted, proof-only key or local model."""
    from .setup_service import PROVIDERS, governed_proof, model_key_details

    details = model_key_details(provider=provider, base_url=base_url, model=model)
    api = (PROVIDERS.get(provider) or {}).get("api") or "openai"
    local = provider in setup_view.LOCALS

    def prove(bound) -> dict[str, Any]:
        if env_name:
            from .env_refs import assert_may_send
            assert_may_send(env_name, details["endpoint"])
        result = setup_checks.complete_once(api=api, endpoint=details["endpoint"],
            api_key=str(bound.get("credential") or ""), model=details["model"], http=http)
        result["usage"] = {**result.get("usage", {}), "provider": provider}
        return result

    from .secret_backends import default_secret_backend
    candidate = {"provider": provider, "source_kind": "local" if local else "api",
        "entitlement_kind": "local_capacity" if local else "api_key",
        "owner_type": "project", "owner_id": project_id,
        "cost_mode": "local_capacity" if local else "usage_based_api",
        "auth_method": "api_key", "connection_status": "connected", "status": "active",
        "runtime_supported": True, "observed_capacity": {"credential_present": local},
        "supported_models": [details["model"]], "workload_classes_allowed": ["owner_sandbox"],
        "risk_tier_max": 1, "limit_shape": {"max_concurrent": 1}}
    if env_name:
        candidate["credential_env_name"] = env_name
    candidate["connection_request_target"] = request_target
    candidate["connection_request_endpoint"] = details["endpoint"]
    candidate["connection_request_base_url"] = details["base_url"]
    group = capacity_group or "connection:" + provider + ":" + details["base_url"] + ":" + details["model"]
    proof = governed_proof(_engine(connection), settings, project_id, f"setup-{provider}",
        actor, bound_prove=prove, amount=0 if local else None, candidate=candidate,
        credential=api_key, backend=backend or default_secret_backend(settings),
        intent=intent, capacity_group=group)
    if proof.get("budget_refused"):
        proof.update(ok=False, state="paused_budget")
    elif proof.get("job_deferred"):
        proof.update(ok=False, state="job_deferred")
    elif proof.get("legacy_hold"):
        proof.update(ok=False, state="legacy_hold")
    proof.setdefault("state", "working" if proof.get("ok") else "failed")
    proof["model"] = details["model"]
    proof["base_url"] = details["base_url"]
    return proof


def _subscription_proof(connection: Connection, settings: Any, project_id: str, provider: str,
                        actor: str, prove_with=None) -> dict[str, Any]:
    from .setup_service import governed_proof, prove_coding_agent

    started = time.monotonic()
    # On BOSS's long-lived token the CLI login's expiry is not what runs (claude_credential).
    long_lived = False
    if provider == "claude_code":
        from .claude_credential import long_lived_token_info

        long_lived = long_lived_token_info(connection, project_id) is not None
    refused = _claude_login_refused(provider) if prove_with is None and not long_lived else None
    if refused is not None:
        # The CLI is never started on an expired login BOSS could not refresh: it would try
        # itself, be refused, and wipe the login (claude_login).
        refused["latency_ms"] = int((time.monotonic() - started) * 1000)
        return refused
    proof = governed_proof(_engine(connection), settings, project_id, provider, actor,
                           prove=prove_with or (lambda: prove_coding_agent(settings, provider)),
                           amount=0)
    proof["latency_ms"] = int((time.monotonic() - started) * 1000)
    if proof.get("ok"):
        proof["state"] = "working"
    elif proof.get("budget_refused"):
        proof["state"] = "paused_budget"
    elif proof.get("job_deferred"):
        proof["state"] = "job_deferred"
    elif proof.get("legacy_hold"):
        proof["state"] = "legacy_hold"
    else:
        # A CLI whose own sign-in failed before any model call comes back HTTP-401-shaped
        # (setup_service.prove_coding_agent): a known rejection, so Login expired.
        status = proof.get("status")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        proof["state"] = setup_checks.classify_model_failure(
            status if status is not None else
            0 if "could not reach" in str(proof.get("error") or "").lower() else -1,
            str(proof.get("error") or ""), subscription=True)
        if proof["state"] == "unreachable":
            proof["state"] = "failed"
        if proof["state"] == "rate_limited":
            # The CLI names when the plan resets ("Try again at …"): the pill shows it, and
            # Auto knows when to come back.
            proof["retry_at"] = setup_checks._retry_after({}, str(proof.get("error") or ""))
    if proof.get("state") == "working":
        # Answered, but the login's real expiry still decides: Expiring / Renewal failed.
        proof["state"], _ = setup_view.claude_login_state(
            "working", provider=provider, kind="subscription", long_lived=long_lived)
    return proof


def _claude_login_refused(provider: str) -> Optional[dict[str, Any]]:
    """For Claude Code on the CLI's own login: refresh a token about to expire first, and
    answer Login expired — without starting the CLI — when it has expired and cannot be
    refreshed. None when the check should go ahead."""
    if provider != "claude_code":
        return None
    try:
        from . import claude_login
        from .claude_credential import cli_login_present

        if not cli_login_present():
            return None
        ok, why = claude_login.ensure_fresh(claude_login.CLI_START_MIN_VALID_SECONDS)
        left = claude_login.seconds_left(claude_login.read_oauth())
    except Exception:  # noqa: BLE001 — the check itself will say what is wrong
        return None
    if ok or left is None or left > 0:
        return None
    return {"ok": False, "state": "login_expired", "status": 401,
            "error": claude_login.sign_in_words(why)}


def _failure(provider: str, state: str, model: Optional[str] = None,
             host: Optional[str] = None, summary: str = "",
             decision_id: str = "") -> SetupActionError:
    from .setup_pills import LOGIN_FIX, DEFAULT_LOGIN_FIX, MODEL, pill

    state = state if state in MODEL else "failed"
    # cap_name: this one-off failure (not an existing model row) has no SetupView budget
    # period to name precisely, so it says "Budget cap" rather than leaving {cap_name} blank
    # ("reached. Raise it..." with no subject) — `summary`, ahead of it, already names the
    # actual period in words (budget.py's own "this month's budget is …").
    shown = pill("model", state, model=model or "That model", host=host or "the provider",
                 provider_fix=LOGIN_FIX.get(provider, DEFAULT_LOGIN_FIX), time="", age="",
                 cap_name="Budget cap", decision_id=decision_id)
    # THE PROVIDER'S OWN REASON FIRST ("Anthropic refused: credit balance too low"), then ours.
    fix = f"{summary} {shown['tip']}".strip() if summary else shown["tip"]
    extra: dict[str, Any] = {}
    if summary:
        extra["summary"] = summary
    if decision_id:
        extra["decision_id"] = decision_id
        extra["href"] = shown.get("fix_url")
    return SetupActionError(422, state, shown["label"], fix, extra or None)


def connect_model(connection: Connection, settings: Any, project_id: str, *, provider: str,
                  kind: str, api_key: str = "", credential: str = "", credential_file: str = "",
                  base_url: str = "", model: str = "", accept_database_fallback: bool = False,
                  actor: str = "ceo", http: Optional[Http] = None, prove_with=None,
                  replace_source_id: str = "", intent: str = "") -> dict[str, Any]:
    """Store a model credential and prove it with one governed question.

    Unknown provider acceptance or cost retains financial authority for reconciliation.
    A failed or uncertain replacement leaves the working connection unchanged.
    """
    provider = (provider or "").strip().lower()
    entry = setup_view.CATALOGUE_BY_PROVIDER.get(provider)
    if entry is None:
        raise SetupActionError(400, "failed", "Unknown provider",
                               f"Pick one of: {', '.join(setup_view.CATALOGUE_BY_PROVIDER)}.")
    wanted = {"api_key": "api_key", "subscription": "subscription", "local": "local"}.get(kind)
    if wanted and wanted != entry["kind"]:
        raise SetupActionError(400, "failed", "Wrong kind", f"{entry['label']} is {entry['kind']}.")
    if entry["kind"] == "subscription":
        return _connect_subscription(connection, settings, project_id, provider,
                                     credential=credential or credential_file,
                                     accept=accept_database_fallback, actor=actor,
                                     prove_with=prove_with, intent=intent)
    return _connect_key(connection, settings, project_id, provider, api_key=api_key,
                        base_url=base_url, model=model, accept=accept_database_fallback,
                        actor=actor, http=http, replace_source_id=replace_source_id, intent=intent)


def _connect_subscription(connection: Connection, settings: Any, project_id: str, provider: str,
                          *, credential: str, accept: bool, actor: str,
                          prove_with=None, intent: str = "") -> dict[str, Any]:
    from .setup_service import SetupError, connect_coding_agent

    if credential.strip():
        # The official harness restores a canonical shared session. It has no
        # candidate-only OAuth import override, so testing a pasted token here
        # would overwrite working material before its proof. Refuse before any
        # store mutation, job, receipt or provider contact.
        raise subscription_import_refusal()

    spec = _subscription_spec(settings, provider)
    if spec is None:
        raise _failure(provider, "not_on_server")
    setup_view.forget_cached("cli")

    started = time.monotonic()
    try:
        connected = connect_coding_agent(connection, settings, project_id, provider, actor=actor,
                                        prove=True, prove_with=prove_with, intent=intent)
    except SetupError as exc:
        message = str(exc)
        lowered = message.lower()
        if "legacy claude token" in lowered:
            state = "legacy_token"
        elif "not installed" in lowered:
            state = "not_on_server"
        elif "budget" in lowered:
            state = "paused_budget"
        elif "capacity" in lowered:
            raise SetupActionError(409, "failed", "Model capacity busy",
                "The selected CLI capacity is in use. No proof was submitted; "
                "try a new explicit Connect intent after that work finishes.") from None
        elif "rejected" in lowered or "not signed in" in lowered or "login" in lowered:
            state = "login_expired"
        else:
            state = setup_checks.classify_model_failure(
                -1, message, subscription=True)
        raise _failure(provider, state) from None
    proof = connected["proof"]
    _record_model_check(connection, project_id, spec.entitlement_source_id or provider,
                        {"ok": True, "state": "working",
                         "latency_ms": int((time.monotonic() - started) * 1000),
                         "receipt_id": proof.get("receipt_id"),
                         "checked_at": proof.get("checked_at")}, actor)
    return {"ok": True, "source_id": spec.entitlement_source_id or provider}


def subscription_import_refusal() -> SetupActionError:
    """Modern and deprecated HTTP doors share the same safe import boundary."""
    return SetupActionError(409, "failed", "CLI credential import held",
        "Nothing pasted is used: sign in inside BOSS (boss connect <provider>, or Sign in "
        "in the console), then Connect. The existing stored session was left unchanged.")


def _first_sentence(message: str) -> str:
    head = re.split(r"(?<=[.!?])\s", message.strip(), maxsplit=1)[0]
    return head[:120]


def _forget_credential(connection: Connection, project_id: str, ref_key: Optional[str],
                       actor: str) -> None:
    """A credential that failed its check is not kept."""
    if not ref_key:
        return
    engine = getattr(connection, "engine", None)
    if engine is None:
        return
    with engine.begin() as own:
        own.execute(
            text("UPDATE secret_refs SET status = 'revoked', revoked_at = now(), "
                 "revoked_by = :a WHERE project_id = :p AND ref_key = :k "
                 "AND status <> 'revoked'"),
            {"a": actor, "p": project_id, "k": ref_key})


def _new_source_id(connection: Connection, project_id: str, provider: str) -> str:
    """ONE SOURCE PER CONNECTION: `setup-<provider>` while it is free, else the next
    `setup-<provider>-<n>`. A second key for a provider is a second model, not a replacement."""
    taken = set(connection.execute(
        text("SELECT source_id FROM entitlement_sources WHERE project_id = :p "
             "AND (source_id = :s OR source_id LIKE :like) "
             "AND (credential_secret_ref_id IS NOT NULL OR connection_status = 'connected')"),
        {"p": project_id, "s": f"setup-{provider}", "like": f"setup-{provider}-%"}).scalars())
    if f"setup-{provider}" not in taken:
        return f"setup-{provider}"
    n = 2
    while f"setup-{provider}-{n}" in taken:
        n += 1
    return f"setup-{provider}-{n}"


def _same_connection(connection: Connection, project_id: str, provider: str, *,
                     base_url: str, model: str) -> Optional[str]:
    """The `setup-<provider>[-n]` row already pointing at this URL and model, if any."""
    from .setup_service import api_source_provider

    def norm(url: Any) -> str:
        return str(url or "").strip().rstrip("/").lower()

    rows = connection.execute(
        text("SELECT source_id, connection_metadata, supported_models FROM entitlement_sources "
             "WHERE project_id = :p AND (source_id = :s OR source_id LIKE :like) "
             "AND (credential_secret_ref_id IS NOT NULL OR connection_status = 'connected') "
             "ORDER BY source_id"),
        {"p": project_id, "s": f"setup-{provider}", "like": f"setup-{provider}-%"}).mappings()
    for row in rows:
        if api_source_provider(row["source_id"]) != provider:
            continue
        meta = setup_view._obj(row["connection_metadata"])
        models = setup_view._list(row["supported_models"])
        stored_model = str(meta.get("model") or (models[0] if models else "") or "")
        if norm(meta.get("base_url")) == norm(base_url) and stored_model == model:
            return str(row["source_id"])
    return None


def _backend_it_lives_in(connection: Connection, project_id: str, source_id: str) -> Optional[str]:
    """REPLACE KEEPS THE KEY WHERE IT LIVES (operator decision 2026-10-04). A key stored in Vault
    from Repo and models' Capacity row is replaced in Vault, not moved to the default store;
    only a first-time store asks (or takes the default)."""
    row = connection.execute(
        text("SELECT r.storage_backend FROM entitlement_sources e "
             "JOIN secret_refs r ON r.id = e.credential_secret_ref_id "
             "WHERE e.project_id = :p AND e.source_id = :s"),
        {"p": project_id, "s": source_id}).scalar()
    return str(row) if row else None


def _connect_key(connection: Connection, settings: Any, project_id: str, provider: str, *,
                 api_key: str, base_url: str, model: str, accept: bool, actor: str,
                 http: Optional[Http], replace_source_id: str = "",
                 intent: str = "") -> dict[str, Any]:
    from .entitlements import EntitlementBrokerService
    from .model_gateway import ModelGatewayService
    from .secret_backends import default_secret_backend
    from .setup_service import PROVIDERS, SetupError

    local = provider in setup_view.LOCALS
    api_key = (api_key or "").strip()
    if not local and not api_key:
        raise SetupActionError(422, "key_rejected", "API key", "Paste the provider's API key.")
    if provider == "ollama" and not base_url:
        from .setup_service import ollama_base_url

        found = setup_view.local_found("ollama")
        base_url = (found or {}).get("base_url") or ollama_base_url(settings)
    if api_key:
        # Asked before the provider is called: a refusal sends nothing anywhere.
        require_store(connection, settings, project_id, accept, actor)
    from .setup_service import model_key_details
    try:
        details = model_key_details(provider=provider, base_url=base_url, model=model)
    except SetupError as exc:
        # A FORM MISTAKE IS A PILL, NOT A 500: "api.example.com/v1" (no scheme), a file: URL or
        # a missing model is refused before anything is stored or any provider is called.
        if "url" in str(exc).lower():
            raise SetupActionError(422, "failed", "Base URL",
                "Use the full address, starting with https:// "
                "(for example https://api.example.com/v1).") from None
        raise SetupActionError(422, "failed", "Model", "Name the model first.") from None
    existing_id = replace_source_id or _same_connection(connection, project_id, provider,
        base_url=details["base_url"], model=details["model"]) or ""
    capacity_group = existing_id
    if existing_id:
        metadata = connection.execute(text("SELECT connection_metadata FROM entitlement_sources "
            "WHERE project_id=:p AND source_id=:s"), {"p":project_id,"s":existing_id}).scalar()
        capacity_group = str(setup_view._obj(metadata).get("capacity_group") or existing_id)
    try:
        proof = _key_proof(connection, settings, project_id, provider, api_key=api_key,
                           base_url=base_url, model=model, actor=actor, http=http,
                           intent=intent, capacity_group=capacity_group,
                           request_target=replace_source_id,
                           backend=_backend_it_lives_in(connection, project_id, existing_id)
                           if existing_id else "")
    except SetupError as exc:
        raise SetupActionError(422, "unreachable", "Can't reach", _first_sentence(str(exc))) \
            from None
    if proof.get("reconciliation_required"):
        summary = str(proof.get("summary") or "The provider's outcome is unknown.")
        raise SetupActionError(409, "held", "Proof held",
            f"{summary} Review it to release the hold, then Connect again.",
            {"receipt_id": proof.get("receipt_id"), "reconciliation_required": True,
             "summary": summary})
    if proof.get("intent_conflict"):
        raise SetupActionError(409, "failed", "Connection intent changed",
            "The credential, endpoint, model or target changed under an existing intent. "
            "The working connection was left unchanged; use a new explicit intent.")
    if proof.get("capacity_refused"):
        raise SetupActionError(409, "failed", "Model capacity busy",
            "The selected connection capacity is in use. No proof was submitted; "
            "try a new explicit Connect intent after that work finishes.")
    receipt_id = proof.get("receipt_id")
    if receipt_id:
        saved = connection.execute(text("SELECT result FROM receipts "
            "WHERE project_id=:p AND id=:r FOR UPDATE"),
            {"p": project_id, "r": receipt_id}).scalar_one()
        saved_source = setup_view._obj(saved).get("connection_saved_source_id")
        if saved_source:
            current = connection.execute(text("SELECT connection_metadata FROM entitlement_sources "
                "WHERE project_id=:p AND source_id=:s"),
                {"p": project_id, "s": saved_source}).scalar()
            if setup_view._obj(current).get("connect_receipt_id") != receipt_id:
                raise SetupActionError(409, "failed", "Connection changed",
                    "This intent already connected an earlier credential. Use a new explicit "
                    "intent to change the current connection; it was left unchanged.")
            return {"ok": bool(proof.get("ok")), "source_id": saved_source,
                    "state": proof.get("state")}
    keep = proof.get("ok") or proof.get("state") in ("rate_limited", "unreachable")
    if not keep:
        raise _failure(provider, str(proof.get("state") or "failed"), proof.get("model"),
                       proof.get("host"), str(proof.get("summary") or ""),
                       str(proof.get("decision_id") or ""))
    from .setup_service import api_source_provider, last_check_broken

    spec = PROVIDERS.get(provider) or {}
    if not (replace_source_id and api_source_provider(replace_source_id) == provider):
        # THE SAME CONNECTION ADDED AGAIN IS THAT ROW, re-tested: provider, URL and model are
        # the identity, so a second press never makes a second identical row.
        replace_source_id = _same_connection(
            connection, project_id, provider,
            base_url=proof.get("base_url") or base_url or spec.get("base_url") or "",
            model=str(proof.get("model") or model or "")) or ""
    if replace_source_id:
        # REPLACE NEVER DOWNGRADES. A key that is only busy or unreachable does not replace one
        # whose last check worked; the working key stays and the refusal says why.
        current = connection.execute(
            text("SELECT connection_metadata, observed_capacity, credential_secret_ref_id "
                 "FROM entitlement_sources WHERE project_id = :p AND source_id = :s"),
            {"p": project_id, "s": replace_source_id}).mappings().first()
        working = bool(current and current["credential_secret_ref_id"]
                       and not last_check_broken(current["connection_metadata"],
                                                 current["observed_capacity"]))
        if not proof.get("ok") and working:
            raise _failure(provider, str(proof.get("state") or "failed"), proof.get("model"),
                           proof.get("host"))
        source_id = replace_source_id
    else:
        source_id = _new_source_id(connection, project_id, provider)
    EntitlementBrokerService(settings).upsert_source(
        connection,
        {"project_id": project_id, "provider": provider, "source_id": source_id,
         "source_kind": "local" if local else "api",
         "entitlement_kind": "local_capacity" if local else "api_key",
         "owner_type": "project", "owner_id": project_id,
         "cost_mode": "local_capacity" if local else "usage_based_api",
         "auth_method": "api_key", "status": "declared", "runtime_supported": True,
         **({"connection_status": "connected"} if local and not api_key else {}),
         "supported_models": [proof["model"]],
         "connection_metadata": {"base_url": proof.get("base_url") or spec.get("base_url") or "",
                                 "model": proof["model"],
                                 "connect_receipt_id": receipt_id,
                                 "capacity_group": proof.get("capacity_group") or source_id}},
        actor=actor)
    if api_key:
        ModelGatewayService(settings).upload_credential(
            connection, project_id, source_id, value=api_key,
            backend=_backend_it_lives_in(connection, project_id, source_id)
            or default_secret_backend(settings), actor=actor)
    _record_model_check(connection, project_id, source_id, proof, actor)
    if receipt_id:
        from .receipts import ReceiptService
        ReceiptService().merge_result(connection, receipt_id,
                                      {"connection_saved_source_id": source_id})
    return {"ok": bool(proof.get("ok")), "source_id": source_id, "state": proof.get("state")}


def check_model(connection: Connection, settings: Any, project_id: str, source_id: str, *,
                actor: str = "ceo", http: Optional[Http] = None, prove_with=None
                ) -> dict[str, Any]:
    """Ask one model source one tiny question again, and record the answer."""
    from .secret_backends import resolve_secret_ref

    if source_id.startswith("env-"):
        provider = source_id[len("env-"):]
        return _check_env_model(connection, settings, project_id, provider, actor=actor,
                                http=http)
    row = connection.execute(
        text("SELECT source_id, credential_secret_ref_id, supported_models, "
             "connection_metadata, connection_status FROM entitlement_sources "
             "WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id}).mappings().first()
    if row is None:
        raise SetupActionError(404, "failed", "Not connected", "No such model.")
    provider = setup_view._provider_of(source_id, setup_view._by_entitlement(settings))
    if provider in setup_view.SUBSCRIPTIONS:
        proof = _subscription_proof(connection, settings, project_id, provider, actor,
                                    prove_with=prove_with)
    else:
        metadata = setup_view._obj(row["connection_metadata"])
        models = setup_view._list(row["supported_models"])
        key = ""
        if row["credential_secret_ref_id"]:
            try:
                key = resolve_secret_ref(connection, row["credential_secret_ref_id"], settings)
            except Exception:  # noqa: BLE001 — BOSS cannot read it; the provider never saw it
                proof = {"ok": False, "state": "key_unreadable"}
                _record_model_check(connection, project_id, source_id, proof, actor)
                return proof
        try:
            proof = _key_proof(connection, settings, project_id, provider, api_key=key,
                               base_url=str(metadata.get("base_url") or ""),
                               model=str(models[0] if models else metadata.get("model") or ""),
                               actor=actor, http=http,
                               capacity_group=str(metadata.get("capacity_group") or source_id),
                               backend=_backend_it_lives_in(connection, project_id, source_id) or "")
        except Exception as exc:  # noqa: BLE001
            proof = {"ok": False, "state": "unreachable", "error": type(exc).__name__}
    _record_model_check(connection, project_id, source_id, proof, actor)
    return proof


def _check_env_model(connection: Connection, settings: Any, project_id: str, provider: str, *,
                     actor: str, http: Optional[Http]) -> dict[str, Any]:
    attr = next((a for a, p in setup_view._ENV_PROVIDER.items() if p == provider), None)
    if attr is None:
        raise SetupActionError(404, "failed", "Not connected", "No such model.")
    if provider == "claude_code":
        proof = _subscription_proof(connection, settings, project_id, provider, actor)
    else:
        key = "" if provider in setup_view.LOCALS else str(getattr(settings, attr, "") or "")
        base = (str(getattr(settings, attr, "") or "") if provider in setup_view.LOCALS
                else str(getattr(settings, "openai_base_url", "") or "")
                if provider == "openai" else "")
        model = str(getattr(settings, "openai_model", "") or "") if provider == "openai" else ""
        env_name = ""
        if key:
            import os
            from .env_refs import env_name_allowed
            names = {"openai":"OPENAI_API_KEY", "anthropic":"ANTHROPIC_API_KEY",
                "openrouter":"OPENROUTER_API_KEY", "groq":"GROQ_API_KEY",
                "deepseek":"DEEPSEEK_API_KEY", "gemini":"GOOGLE_API_KEY", "xai":"XAI_API_KEY"}
            wanted = names.get(provider, "")
            env_name = next((n for n in (wanted, "BOSS_" + wanted)
                if n and env_name_allowed(n) and os.environ.get(n) == key), "")
            if not env_name:
                raise SetupActionError(409,"key_unreadable","Proof held",
                    "Connect this key through its chosen store before checking it.")
        try:
            proof = _key_proof(connection, settings, project_id, provider, api_key=key,
                               base_url=base, model=model, actor=actor, http=http,
                               env_name=env_name, capacity_group=f"env-{provider}")
        except Exception as exc:  # noqa: BLE001
            proof = {"ok": False, "state": "unreachable", "error": type(exc).__name__}
    _record_model_check(connection, project_id, f"env-{provider}", proof, actor)
    return proof


def set_source_model(connection: Connection, settings: Any, project_id: str, source_id: str,
                     model: str, *, actor: str = "ceo", http: Optional[Http] = None) -> None:
    model = (model or "").strip()
    if not model:
        raise SetupActionError(422, "model_not_found", "Model", "Pick a model.")
    updated = connection.execute(
        text("UPDATE entitlement_sources SET supported_models = CAST(:m AS JSONB), "
             "connection_metadata = COALESCE(connection_metadata, '{}'::jsonb) "
             "  || jsonb_build_object('model', CAST(:name AS text)), updated_at = now() "
             "WHERE project_id = :p AND source_id = :s"),
        {"m": json.dumps([model]), "name": model, "p": project_id, "s": source_id})
    if not updated.rowcount:
        raise SetupActionError(404, "failed", "Not connected", "No such model.")
    check_model(connection, settings, project_id, source_id, actor=actor, http=http)


def jobs_using(connection: Connection, settings: Any, project_id: str, source_id: str
               ) -> list[str]:
    """Jobs explicitly chosen on this source's engine."""
    from . import project_settings
    from .model_routing import JOB_KNOBS

    provider = setup_view._provider_of(source_id, setup_view._by_entitlement(settings))
    engine = setup_view.engine_for_provider(provider)
    chosen = project_settings.get_all(connection, project_id)
    labels = {j["job"]: j["label"] for j in setup_view.JOBS}
    out = []
    for job, (engine_knob, _model_knob) in JOB_KNOBS.items():
        value = str(chosen.get(engine_knob) or "")
        if job == "decisions":
            hit = (value == "typesafe" and source_id == "typesafe_jev") or (
                value == "claude" and engine == "claude_code")
        else:
            hit = value == engine
        if hit:
            out.append(labels[job])
    return out


def remove_model(connection: Connection, settings: Any, project_id: str, source_id: str, *,
                 confirm: bool = False, actor: str = "ceo") -> None:
    from . import project_settings
    from .core import emit_event
    from .model_routing import JOB_KNOBS

    if source_id.startswith("env-"):
        raise SetupActionError(409, "failed", "From install",
                               "Set in the server's settings file. Replace it here first.")
    using = jobs_using(connection, settings, project_id, source_id)
    if using and not confirm:
        raise SetupActionError(409, "in_use", "In use", ", ".join(using), {"jobs": using})
    labels = {j["label"]: j["job"] for j in setup_view.JOBS}
    for label in using:
        engine_knob, model_knob = JOB_KNOBS[labels[label]]
        project_settings.clear_one(connection, project_id, engine_knob)
        if model_knob:
            project_settings.clear_one(connection, project_id, model_knob)
    row = connection.execute(
        text("SELECT credential_secret_ref_id FROM entitlement_sources "
             "WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id}).mappings().first()
    if row is None:
        raise SetupActionError(404, "failed", "Not connected", "No such model.")
    if row["credential_secret_ref_id"]:
        connection.execute(
            text("UPDATE secret_refs SET status = 'revoked', revoked_at = now(), "
                 "revoked_by = :a WHERE id = :id"),
            {"a": actor, "id": row["credential_secret_ref_id"]})
    if source_id == "anthropic_claude_code":
        # Removing the subscription forgets its stored login: `claude` runs fall through to
        # the Anthropic API key (claude_credential).
        from .claude_credential import forget_subscription_login

        forget_subscription_login(connection, settings, project_id, actor=actor)
    connection.execute(
        text("UPDATE entitlement_sources SET credential_secret_ref_id = NULL, "
             "connection_status = 'not_connected', status = 'declared', "
             "connection_metadata = COALESCE(connection_metadata, '{}'::jsonb) - 'last_check', "
             "updated_at = now() WHERE project_id = :p AND source_id = :s"),
        {"p": project_id, "s": source_id})
    emit_event(connection, project_id, "setup.model_removed", actor, "model_source", source_id,
               {"jobs_reset": using})


# ── Model lists (the Model picker) ────────────────────────────────────────────────────────────

_LISTS: dict[str, tuple[float, list[str]]] = {}


def model_catalogue(connection: Connection, settings: Any, project_id: str, provider: str, *,
                    api_key: str = "", source_id: str = "", base_url: str = "",
                    http: Optional[Http] = None) -> list[str]:
    """The provider's own model list for this key, cached ten minutes (never by key value)."""
    from .secret_backends import resolve_secret_ref
    from .setup_service import PROVIDERS

    provider = (provider or "").strip().lower()
    spec = PROVIDERS.get(provider)
    if spec is None:
        return []
    if provider in setup_view.SUBSCRIPTIONS:
        try:
            from .cli_harness import offered_models

            return list(offered_models(provider))
        except Exception:  # noqa: BLE001
            return []
    cache_key = f"{project_id}:{provider}:{source_id or ('pasted' if api_key else '')}:{base_url}"
    hit = _LISTS.get(cache_key)
    if hit and time.monotonic() < hit[0] and not api_key:
        return hit[1]
    key = api_key
    if not key and source_id:
        row = connection.execute(
            text("SELECT credential_secret_ref_id, connection_metadata FROM entitlement_sources "
                 "WHERE project_id = :p AND source_id = :s"),
            {"p": project_id, "s": source_id}).mappings().first()
        if row and row["credential_secret_ref_id"]:
            try:
                key = resolve_secret_ref(connection, row["credential_secret_ref_id"], settings)
            except Exception:  # noqa: BLE001
                key = ""
        if row and not base_url:
            base_url = str(setup_view._obj(row["connection_metadata"]).get("base_url") or "")
    if not base_url and provider == "ollama":
        from .setup_service import ollama_base_url

        base_url = ollama_base_url(settings)
    base = (base_url or spec.get("base_url") or "").strip()
    if not base:
        return []
    names = setup_checks.list_models(api=spec.get("api") or "openai", base_url=base, api_key=key,
                                     local_ollama=provider == "ollama", http=http)
    if provider in setup_view.LOCALS and names:
        setup_view.remember_local(provider, base, names)
    _LISTS[cache_key] = (time.monotonic() + 600.0, names)
    return names


def detect_local(settings: Any, http: Optional[Http] = None) -> None:
    """Look for an Ollama where this BOSS is configured to find it (part of `Test all`, never
    of a read): OLLAMA_BASE_URL, the host's Ollama under the Community compose."""
    from .setup_service import ollama_base_url

    base = ollama_base_url(settings)
    try:
        names = setup_checks.list_models(api="openai", base_url=base, local_ollama=True,
                                         http=http)
    except Exception:  # noqa: BLE001
        names = []
    if names:
        setup_view.remember_local("ollama", base, names)


# ── Who does what ─────────────────────────────────────────────────────────────────────────────

def set_job(connection: Connection, settings: Any, project_id: str, job: str,
            choice: Optional[dict[str, Any]], *, actor: str = "ceo",
            scope: Optional[str] = None) -> dict[str, Any]:
    """Write a job's engine and model together, or hand the job back to Auto. All or nothing:
    a choice the view does not offer is a 422 and nothing is written.

    The choice is the ORG's (org_models: every project of the org) unless scope='project' pins
    it for this project; Auto with scope='project' removes only the pin."""
    from . import project_settings
    from .core import emit_event
    from .model_routing import JOB_KNOBS, chat_model_knob

    if job not in JOB_KNOBS:
        raise SetupActionError(404, "failed", "Unknown job", f"One of: {', '.join(JOB_KNOBS)}.")
    if scope not in (None, "", "org", "project"):
        raise SetupActionError(422, "failed", "Scope", "All projects (org) or this project.")
    engine_knob, model_knob = JOB_KNOBS[job]
    if not choice:
        try:
            project_settings.clear_one(connection, project_id, engine_knob, scope=scope)
            for knob in ([model_knob] if model_knob else
                         ["claude_cli_model", "codex_cli_model", "chat_model"]):
                project_settings.clear_one(connection, project_id, knob, scope=scope)
        except PermissionError as exc:
            raise SetupActionError(403, "failed", "Org admin", str(exc)) from None
        emit_event(connection, project_id, "setup.job_set", actor, "job", job,
                   {"auto": True, "scope": scope or "org"})
        return {"job": job, "auto": True}
    engine = str(choice.get("engine") or "").strip().lower()
    model = str(choice.get("model") or "").strip() or None
    view = setup_view.build(connection, settings, project_id)
    spec = next(j for j in view["jobs"] if j["job"] == job)
    offered = {(o["engine"], o.get("model")) for o in spec["options"]}
    if (engine, model) not in offered and not (engine, None) in offered:
        raise SetupActionError(422, "failed", "Not offered",
                               f"{engine} · {model or 'default'} is not a working model here.")
    if job == "decisions":
        if engine == "typesafe":
            option = next(o for o in spec["options"] if o["engine"] == "typesafe")
            if option.get("needs_key"):
                raise SetupActionError(422, "failed", "Add key", "Connect a TypeSafe key first.")
            values = {"decision_backend": "typesafe"}
        else:
            values = {"decision_backend": "claude", "decision_model": model or ""}
    else:
        values = {engine_knob: engine,
                  (model_knob or chat_model_knob(engine)): model or ""}
    try:
        for key, value in values.items():
            if value == "":
                project_settings.clear_one(connection, project_id, key, scope=scope)
            else:
                project_settings.set_one(connection, project_id, key, value, actor, scope=scope)
    except PermissionError as exc:
        raise SetupActionError(403, "failed", "Org admin", str(exc)) from None
    except ValueError as exc:
        raise SetupActionError(422, "failed", "Not offered", _first_sentence(str(exc))) from None
    emit_event(connection, project_id, "setup.job_set", actor, "job", job,
               {"engine": engine, "model": model, "scope": scope or "org"})
    return {"job": job, "engine": engine, "model": model}


def set_claude_credential_order(connection: Connection, settings: Any, project_id: str,
                                order: str, *, actor: str = "ceo",
                                scope: Optional[str] = None) -> dict[str, Any]:
    """Subscription first or API key first for every claude BOSS starts (claude_credential).
    The org's choice unless scope='project'. Recorded as an event: it decides who pays."""
    from . import claude_credential, project_settings
    from .core import emit_event

    if order not in claude_credential.ORDERS:
        raise SetupActionError(422, "failed", "Order",
                               "Subscription first or API key first.")
    if scope not in (None, "", "org", "project"):
        raise SetupActionError(422, "failed", "Scope", "All projects (org) or this project.")
    try:
        project_settings.set_one(connection, project_id, claude_credential.ORDER_KNOB, order,
                                 actor, scope=scope)
    except PermissionError as exc:
        raise SetupActionError(403, "failed", "Org admin", str(exc)) from None
    emit_event(connection, project_id, "setup.claude_credential_order", actor, "setting",
               claude_credential.ORDER_KNOB, {"order": order, "scope": scope or "org"})
    return {"order": order}


# ── Budget ────────────────────────────────────────────────────────────────────────────────────

def set_budget(connection: Connection, settings: Any, project_id: str, monthly_cap_usd: Any, *,
               actor: str = "ceo", via: str = "repo_and_models") -> None:
    """The monthly cap — the same setting Govern edits."""
    from .core import emit_event, make_id

    try:
        dollars = float(monthly_cap_usd)
    except (TypeError, ValueError):
        raise SetupActionError(422, "failed", "Monthly cap ($)", "A number of dollars.") \
            from None
    if not (0 < dollars <= 1_000_000):
        raise SetupActionError(422, "failed", "Monthly cap ($)", "Between $0.01 and $1,000,000.")
    micros = int(round(dollars * 1_000_000))
    updated = connection.execute(
        text("UPDATE budget_policies SET monthly_cap_usd_micros = :m, version = version + 1 "
             "WHERE scope_type = 'project' AND scope_id = :p"), {"m": micros, "p": project_id})
    if not updated.rowcount:
        connection.execute(
            text("INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                 "monthly_cap_usd_micros) VALUES (:id, 'project', :p, :d, :m)"),
            {"id": make_id("bp"), "p": project_id,
             "d": int(getattr(settings, "daily_cap_usd_micros", 10_000_000) or 10_000_000),
             "m": micros})
    from .budget import release_budget_holds

    release_budget_holds(connection, project_id)
    emit_event(connection, project_id, "budget.monthly_cap_set", actor, "budget_policy",
               project_id, {"monthly_cap_usd_micros": micros,
                            "via": via if via in ("govern", "repo_and_models") else
                            "repo_and_models"})


# ── Checks ────────────────────────────────────────────────────────────────────────────────────

def run_checks(connection: Connection, settings: Any, project_id: str, target: str, *,
               actor: str = "ceo", http: Optional[Http] = None, model_http: Optional[Http] = None,
               prove_with=None) -> None:
    """`all`, `repository`, `repository:<binding_id>` or `model:<source_id>`."""
    target = (target or "all").strip()
    view = setup_view.build(connection, settings, project_id)
    repos = [r["binding_id"] or "install" for r in view["repository"]["repos"]]
    models = [m["source_id"] for m in view["models"]]
    if target == "all":
        setup_view.forget_cached("cli")
        detect_local(settings, http=model_http)
        todo = [("repository", r) for r in repos] + [("model", m) for m in models]
    elif target == "repository":
        todo = [("repository", r) for r in repos]
    elif target.startswith("repository:"):
        todo = [("repository", target.split(":", 1)[1])]
    elif target.startswith("model:"):
        todo = [("model", target.split(":", 1)[1])]
    elif target == "stale":
        todo = [tuple(t.split(":", 1)) for t in setup_view.stale_targets(view)]  # type: ignore
    else:
        raise SetupActionError(422, "failed", "Unknown check",
                               "all, repository, repository:<id> or model:<id>.")
    if target == "all":
        _verify_github_app(connection)
    for kind, item in todo:
        if kind == "repository":
            check_repository(connection, settings, project_id, item, actor=actor, http=http)
        else:
            check_model(connection, settings, project_id, item, actor=actor, http=model_http,
                        prove_with=prove_with)


def _verify_github_app(connection: Connection) -> None:
    """Test all also asks GitHub what the Setup-created App's installation actually accepted
    (identity/github_setup.verify): its answer is what GitHub access shows. Best effort: no App,
    no identity tables or GitHub down leaves the last answer standing."""
    from ..identity import github_setup

    point = connection.begin_nested()
    try:
        cfg = github_setup.app_config(connection)
        if cfg.get("source") == "setup" and cfg.get("app_id"):
            github_setup.verify(connection)
        point.commit()
    except Exception:  # noqa: BLE001
        point.rollback()


# ── Try it ────────────────────────────────────────────────────────────────────────────────────

def try_spec() -> dict[str, Any]:
    """Build, then report: the smallest path from one sentence to a pull request."""
    from .workflow_catalogue import _build_inputs, _chain, _step

    steps = _chain([_step("build", "build", _build_inputs()), _step("report", "report")])
    return {"entry_step": "build", "steps": steps,
            "objective": ("Make the one small change described, with a test where it fits. "
                          "Open a pull request for a person to review; never merge."),
            "workflow_name": "First PR",
            "input_schema": {"type": "object", "required": ["title", "directive"],
                             "properties": {
                                 "title": {"type": "string", "title": "Title"},
                                 "directive": {"type": "string", "format": "textarea",
                                               "title": "What should change?"},
                                 "repository": {"type": "string", "title": "Repository"}}}}


def ensure_try_workflow(connection: Connection, project_id: str) -> str:
    """Install (or update) the Try it workflow. Its version id.

    PRODUCT namespace — it is the operator's work on their repository, and the capability
    runner advances product runs only — and hidden from the Library (workflows.HIDDEN_PRODUCT_KEYS),
    because it is not a template to choose."""
    from . import workflows

    key = setup_view.SETUP_TRY_WORKFLOW
    existing = connection.execute(
        text("SELECT id FROM workflow_definitions WHERE project_id = :p AND workflow_key = :k"),
        {"p": project_id, "k": key}).scalar()
    if existing is None:
        definition = workflows.create_definition(
            connection, project_id=project_id, workflow_key=key, name="First PR",
            description="Repo and models › Try it: one sentence to a pull request.",
            actor="boss", namespace=workflows.PRODUCT_NAMESPACE, made_by_boss=True)
        existing = definition["id"]
    else:
        # An install that made it before it ran under product: the capability runner ticks
        # product runs only, so a system-namespace Try it sat ready and unleased forever.
        connection.execute(
            text("UPDATE workflow_definitions SET namespace = 'product', updated_at = now() "
                 "WHERE id = :id AND COALESCE(namespace, 'system') <> 'product'"),
            {"id": existing})
    try:
        workflows.publish_version(connection, project_id=project_id, definition_id=existing,
                                  spec=try_spec(), actor="boss")
    except workflows.WorkflowConflictError:
        pass                                      # same content: already published
    running = workflows.running_version(connection, project_id, key)
    if not running:
        raise SetupActionError(503, "failed", "Try it", "The Try it workflow is not installed.")
    return str(running["version_id"])


def start_try(connection: Connection, settings: Any, project_id: str, directive: str, *,
              actor: str = "ceo", idempotency_key: str = "") -> dict[str, Any]:
    """One sentence → a governed build on the default repository → a pull request."""
    from . import workflows
    from .core import emit_event
    from .workflow_readiness import (
        WorkflowNotReadyError, record_started_anyway, refuse_unless_ready,
    )

    directive = " ".join((directive or "").split())
    if len(directive) < 4:
        raise SetupActionError(422, "failed", "Describe a change", "One sentence is enough.")
    view = setup_view.build(connection, settings, project_id)
    if not view["ready"]:
        raise SetupActionError(409, "failed", "Not ready",
                               "Connect a repository and a model first.")
    default = next((r for r in view["repository"]["repos"] if r["default"]), None)
    version_id = ensure_try_workflow(connection, project_id)
    title = directive if len(directive) <= 80 else directive[:77] + "…"
    run_input = {"title": title, "directive": directive,
                 **({"repository": default["full_name"]} if default else {})}
    try:
        readiness = refuse_unless_ready(connection, settings, project_id, version_id=version_id,
                                        input_data=run_input, start_anyway=[])
    except WorkflowNotReadyError as exc:
        raise SetupActionError(409, "failed", "Not ready", _first_sentence(str(exc))) from None
    run = workflows.start_run(
        connection, project_id=project_id, actor=actor, input_data=run_input,
        dedupe_key=f"setup-try:{idempotency_key or uuid.uuid4().hex[:12]}",
        workflow_version_id=version_id, selector_kind=None, selector_value=None)
    if not run.get("duplicate"):
        record_started_anyway(connection, project_id, actor, run.get("id") or "", readiness)
        emit_event(connection, project_id, "setup.try_started", actor, "workflow",
                   setup_view.SETUP_TRY_WORKFLOW, {"run_id": run.get("id"),
                                                   "repository": run_input.get("repository")})
    return {"run_id": run.get("id"), "duplicate": bool(run.get("duplicate"))}
