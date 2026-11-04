"""Repo and models, as ONE server-assembled view: `GET /v1/setup`.

WHY ONE VIEW. Setup was built as forms that perform actions, so its state was whatever the last
form said: eight GETs on load, five verdicts that disagreed, each card judging for itself. This
builds the whole page's state once — repository, models, who does what, budget and Try it — with
every pill already decided (setup_pills), so the console only renders, and the Command Center,
workflow readiness and the agent's diagnose_setup read the same answer.

READS NEVER TOUCH THE NETWORK. Nothing here calls GitHub or a provider, and nothing decrypts a
credential: it renders what `setup_checks` last recorded and when. Two local probes are cached
rather than repeated on every read: which coding-agent CLIs are signed in on the server (a
subprocess each) and which secret store answers (a health call), five minutes each.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import setup_pills
from .setup_checks import STALE_AFTER, now_utc, parse_time, repository_state

#: The smallest code-change workflow: build, then report. No research, no panel, no deploy.
#: A BOSS-made system workflow rather than a catalogue entry, so it never appears in the
#: operator's Library — it is Repo and models' `Try it`, not a template to choose.
SETUP_TRY_WORKFLOW = "setup_first_pr"

SUBSCRIPTIONS = ("claude_code", "codex", "grok", "cursor")
LOCALS = ("ollama", "vllm")

#: What Add a model offers, in the order it offers it. `fields` is the form; `command` a copy
#: chip for the one thing an operator runs on their own computer.
#:
#: A SUBSCRIPTION HAS NO FIELDS. Its provider's own login runs inside BOSS (`boss connect
#: <provider>`, or the console's Sign in), then Connect proves it with one turn. Nothing is
#: pasted or dropped: a token field or an auth.json drop zone offered a route the backend
#: refuses (`subscription_import_refusal`).
CATALOGUE: tuple[dict[str, Any], ...] = (
    {"provider": "claude_code", "label": "Claude Code", "kind": "subscription",
     "fields": [], "command": "boss connect claude"},
    {"provider": "codex", "label": "Codex", "kind": "subscription",
     "fields": [], "command": "boss connect codex"},
    {"provider": "grok", "label": "Grok Build", "kind": "subscription", "fields": [],
     "command": "boss connect grok"},
    {"provider": "openrouter", "label": "OpenRouter", "kind": "api_key",
     "fields": ["api_key", "model"], "key_url": "https://openrouter.ai/keys",
     "example_model": "anthropic/claude-sonnet-4.5",
     "billing_url": "https://openrouter.ai/settings/credits"},
    {"provider": "openai", "label": "OpenAI", "kind": "api_key", "fields": ["api_key", "model"],
     "key_url": "https://platform.openai.com/api-keys", "example_model": "gpt-4o",
     "billing_url": "https://platform.openai.com/settings/organization/billing"},
    {"provider": "anthropic", "label": "Anthropic", "kind": "api_key",
     "fields": ["api_key", "model"], "key_url": "https://console.anthropic.com/settings/keys",
     "example_model": "claude-sonnet-5",
     "billing_url": "https://console.anthropic.com/settings/billing"},
    {"provider": "gemini", "label": "Gemini", "kind": "api_key", "fields": ["api_key", "model"],
     "key_url": "https://aistudio.google.com/app/apikey", "example_model": "gemini-3.8-flash",
     "billing_url": "https://aistudio.google.com/app/plan_information"},
    {"provider": "xai", "label": "xAI", "kind": "api_key", "fields": ["api_key", "model"],
     "key_url": "https://console.x.ai/team/default/api-keys", "example_model": "grok-4.7",
     "billing_url": "https://console.x.ai"},
    {"provider": "custom", "label": "Other", "kind": "api_key",
     "fields": ["base_url", "api_key", "model"]},
    {"provider": "ollama", "label": "Ollama", "kind": "local", "fields": ["base_url", "model"],
     "example_model": ""},
    {"provider": "vllm", "label": "Other URL", "kind": "local", "fields": ["base_url", "model"]},
)
CATALOGUE_BY_PROVIDER = {entry["provider"]: entry for entry in CATALOGUE}
#: The subscriptions Add a model OFFERS: each ships its CLI in Dockerfile.community and has a
#: harness spec (`test_every_offered_subscription_ships_in_the_image`). Cursor is not offered:
#: `cursor-agent` is not in the image and its spec is unverified, so it could only ever read
#: "Not on server". SUBSCRIPTIONS still knows it, so an existing Cursor row keeps its kind.
OFFERED_SUBSCRIPTIONS = tuple(entry["provider"] for entry in CATALOGUE
                              if entry["kind"] == "subscription")

#: engine (harness) -> the name an operator knows it by.
ENGINE_LABELS = {
    "claude_code": "Claude Code", "codex": "Codex", "grok": "Grok Build", "cursor": "Cursor",
    "native": "OpenAI", "openrouter": "OpenRouter", "gemini": "Gemini", "xai": "xAI",
    "ollama": "Ollama", "vllm": "Local model", "azure": "Azure OpenAI", "groq": "Groq",
    "deepseek": "DeepSeek", "typesafe": "TypeSafe Jev", "aider": "Aider",
    "claude_agent_sdk": "Claude Agent SDK",
}

#: The provider a `setup-<x>` source or an env lane is listed under.
_ENV_PROVIDER = {"openai_api_key": "openai", "claude_code_oauth_token": "claude_code",
                 "anthropic_api_key": "anthropic", "azure_openai_key": "azure",
                 "openrouter_api_key": "openrouter", "groq_api_key": "groq",
                 "deepseek_api_key": "deepseek", "google_api_key": "gemini",
                 "xai_api_key": "xai", "vllm_base_url": "vllm", "ollama_base_url": "ollama"}

JOBS: tuple[dict[str, Any], ...] = (
    {"job": "build", "label": "Build", "more": False},
    {"job": "review", "label": "Review", "more": False},
    {"job": "chat", "label": "Chat", "more": False},
    {"job": "decisions", "label": "Decisions", "more": False},
    {"job": "hard", "label": "Hard problems", "more": True},
    {"job": "routine", "label": "Routine work", "more": True},
)

#: Who decides `secret_db_consent` — a plain project_settings row, not a console knob.
SECRET_CONSENT_KEY = "setup.secret_db_consent"


# ── Local probes, cached ──────────────────────────────────────────────────────────────────────

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}
_TTL = 300.0


def _cached(key: str, compute: Callable[[], Any], ttl: float = _TTL) -> Any:
    """Compute now (a write path may wait), and keep it for `ttl`."""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now < hit[0]:
            return hit[1]
    value = compute()
    with _CACHE_LOCK:
        _CACHE[key] = (now + ttl, value)
    return value


_PENDING: set[str] = set()


def _cached_or_later(key: str, compute: Callable[[], Any], default: Any,
                     ttl: float = _TTL) -> tuple[Any, bool]:
    """(value, pending) for a READ: the cached value (even a stale one) or `default`, never a
    wait. A missing or stale value is computed on a background thread, one at a time per key;
    `pending` says one is on its way so the page can ask again."""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        fresh = bool(hit and now < hit[0])
        running = key in _PENDING
        if not fresh and not running:
            _PENDING.add(key)
    if not fresh and not running:
        def run() -> None:
            try:
                value = compute()
                with _CACHE_LOCK:
                    _CACHE[key] = (time.monotonic() + ttl, value)
            except Exception:  # noqa: BLE001 — a probe that fails is retried next read
                pass
            finally:
                with _CACHE_LOCK:
                    _PENDING.discard(key)

        threading.Thread(target=run, name=f"boss-setup-{key}", daemon=True).start()
    return (hit[1] if hit else default), (not fresh)


def forget_cached(prefix: str = "") -> None:
    """Drop cached probes (after a connect, so the next read sees the new state)."""
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k.startswith(prefix)]:
            _CACHE.pop(key, None)


def remember_local(provider: str, base_url: str, models: list[str]) -> None:
    """A local model server answered (setup_checks.list_models): show it as Found."""
    with _CACHE_LOCK:
        _CACHE[f"local:{provider}"] = (time.monotonic() + 3600.0,
                                       {"base_url": base_url, "models": list(models)})


def local_found(provider: str) -> Optional[dict[str, Any]]:
    with _CACHE_LOCK:
        hit = _CACHE.get(f"local:{provider}")
        return hit[1] if hit and time.monotonic() < hit[0] else None


def _detect_clis(settings: Any) -> dict[str, dict[str, Any]]:
    from .cli_harness import detect_all

    return {d["name"]: d for d in detect_all(settings)}


def _cli_detection(settings: Any) -> dict[str, dict[str, Any]]:
    """Which coding CLIs are signed in on this server. Each probe spawns the CLI, so a read
    never waits for it: the last answer, refreshed in the background."""
    found, _pending = _cached_or_later("cli", lambda: _detect_clis(settings), {})
    return found


def detection_pending() -> bool:
    with _CACHE_LOCK:
        return "cli" in _PENDING or "secret_backend" in _PENDING


def _pick_backend(settings: Any) -> str:
    from .secret_backends import default_secret_backend

    return default_secret_backend(settings)


def _secret_backend(settings: Any, *, wait: bool = False) -> Optional[str]:
    """Where a pasted credential would be kept. A write may wait for the answer (a health call
    to the store); a read takes the last answer, or None while the first is on its way."""
    from .secret_backends import operator_chosen_backend

    chosen = operator_chosen_backend()
    if chosen:
        return chosen
    if wait:
        return _cached("secret_backend", lambda: _pick_backend(settings))
    found, pending = _cached_or_later("secret_backend", lambda: _pick_backend(settings), None)
    # THE FIRST READ ON A COLD CACHE WAITS A MOMENT. Without an answer the board cannot tell
    # whether to ask for database consent, so the form went out without the question and the
    # submit was refused (409) instead. The store's health call normally answers well inside
    # this bound; a store that hangs still never holds the page for longer.
    deadline = time.monotonic() + COLD_SECRET_BACKEND_WAIT_SECONDS
    while found is None and pending and time.monotonic() < deadline:
        time.sleep(0.05)
        with _CACHE_LOCK:
            hit = _CACHE.get("secret_backend")
        if hit:
            found = hit[1]
    return found


#: How long the first read waits for the secret store's answer (_secret_backend).
COLD_SECRET_BACKEND_WAIT_SECONDS = 2.0


# ── Small readers ─────────────────────────────────────────────────────────────────────────────

def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _iso(value: Any) -> Optional[str]:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed else None


def _hhmm(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.strftime("%H:%M") if parsed else ""


def _age(value: Any, now: datetime) -> str:
    parsed = parse_time(value)
    if not parsed:
        return ""
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _latest_event(connection: Connection, project_id: str, event_type: str, entity_id: str
                  ) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT data, created_at FROM events WHERE project_id = :p AND event_type = :t "
             "AND entity_id = :e ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "t": event_type, "e": entity_id}).mappings().first()
    if row is None:
        return None
    return {**_obj(row["data"]), "_at": row["created_at"]}


def secret_consent(connection: Connection, project_id: str) -> bool:
    row = connection.execute(
        text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": project_id, "k": SECRET_CONSENT_KEY}).scalar()
    return row is True or str(row).lower() in ("true", "1")


def secrets_state(connection: Connection, settings: Any, project_id: str, *,
                  wait: bool = False) -> dict[str, Any]:
    """Where a credential pasted here would be kept, and whether the operator must say yes."""
    from .secret_backends import operator_chosen_backend

    backend = _secret_backend(settings, wait=wait)
    needs = (backend == "database" and operator_chosen_backend() != "database"
             and not secret_consent(connection, project_id))
    return {"backend": backend or "checking", "needs_consent": needs}


# ── Repository ────────────────────────────────────────────────────────────────────────────────

def _repository(connection: Connection, settings: Any, project_id: str, now: datetime
                ) -> dict[str, Any]:
    rows = connection.execute(
        text("SELECT b.id, b.full_name, b.role, b.default_branch, b.visibility, b.checks, "
             "       b.checked_at, c.id AS connection_id, c.auth_method, c.account_login, "
             "       c.expires_at, c.status AS connection_status "
             "  FROM github_repository_bindings b "
             "  JOIN github_connections c ON c.id = b.connection_id "
             " WHERE b.project_id = :p AND b.status = 'active' "
             "   AND c.status NOT IN ('disabled', 'revoked') "
             " ORDER BY (b.role = 'primary') DESC, b.created_at"),
        {"p": project_id}).mappings().all()
    connections = _connections(connection, project_id)
    connection_row = None
    if rows:
        connection_row = {"id": rows[0]["connection_id"], "auth_method": rows[0]["auth_method"],
                          "account_login": rows[0]["account_login"],
                          "expires_at": rows[0]["expires_at"]}
    else:
        found = connection.execute(
            text("SELECT id, auth_method, account_login, expires_at FROM github_connections "
                 " WHERE project_id = :p AND status NOT IN ('disabled', 'revoked') "
                 " ORDER BY updated_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
        connection_row = dict(found) if found else None

    repos: list[dict[str, Any]] = []
    for row in rows:
        checks = _obj(row["checks"])
        expires = parse_time(row["expires_at"])
        checked = parse_time(row["checked_at"])
        state = repository_state(checks, expires_at=expires, checked_at=checked, now=now)
        repos.append(_repo_entry(
            binding_id=row["id"], full_name=row["full_name"],
            default=row["role"] == "primary",
            private=(None if row["visibility"] is None else row["visibility"] != "public"),
            default_branch=row["default_branch"], checks=checks, state=state,
            expires=expires, checked=checked, retry_at=checks.get("retry_at"), now=now,
            connection_id=row["connection_id"]))

    source = "connected"
    if not rows:
        owner, name, token = install_repository(settings)
        if owner and name and token:
            # AN INSTALL CONFIGURED FROM ITS SETTINGS FILE IS CONFIGURED. One row, chip
            # `From install`, checked like any other — never an empty form beside it.
            source = "install"
            last = _latest_event(connection, project_id, "setup.repository_checked", "install")
            checks = _obj((last or {}).get("checks"))
            if last:
                checks["state"] = last.get("state")
                checks["retry_at"] = last.get("retry_at")
            expires = parse_time((last or {}).get("expires_at"))
            checked = parse_time((last or {}).get("_at"))
            state = repository_state(checks, expires_at=expires, checked_at=checked, now=now)
            repos.append(_repo_entry(
                binding_id=None, full_name=f"{owner}/{name}", default=True,
                private=(last or {}).get("private"),
                default_branch=(last or {}).get("default_branch"), checks=checks,
                state=state, expires=expires, checked=checked,
                retry_at=checks.get("retry_at"), now=now))
            connection_row = {"id": "install", "auth_method": "personal_access_token",
                              "account_login": (last or {}).get("account_login"),
                              "expires_at": (last or {}).get("expires_at")}
    connection_out = None
    if connection_row:
        connection_out = {
            "id": connection_row["id"],
            "auth": "app" if connection_row["auth_method"] == "github_app" else "token",
            "account_login": connection_row.get("account_login"),
            "expires_at": _iso(connection_row.get("expires_at")),
            "source": source,
        }
        if source == "connected" and connection_out["auth"] == "token" and repos:
            connection_out["builds"] = _builds_reach(connection, project_id, connection_row["id"],
                                                     repos[0]["full_name"])
    return {"connection": connection_out, "connections": connections, "repos": repos}


def _connections(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every GitHub connection (account) that owns one of this project's repositories, for the
    console to group repos under. `connection` (singular) stays computed above for callers that
    have not migrated."""
    rows = connection.execute(
        text("SELECT DISTINCT c.id, c.connection_label, c.account_login, c.auth_method, "
             "       c.api_base_url, c.status, c.created_at "
             "  FROM github_connections c "
             "  JOIN github_repository_bindings b ON b.connection_id = c.id "
             " WHERE c.project_id = :p AND c.status NOT IN ('disabled', 'revoked') "
             "   AND b.project_id = :p AND b.status = 'active' "
             " ORDER BY c.created_at"),
        {"p": project_id}).mappings().all()
    out = []
    for row in rows:
        status = row["status"]
        if status == "active":
            kind, label = "green", "Working"
        elif status == "error":
            kind, label = "red", "Error"
        else:
            kind, label = "grey", "Not checked"
        out.append({
            "id": row["id"], "label": row["connection_label"],
            "account_login": row["account_login"],
            "auth": "app" if row["auth_method"] == "github_app" else "token",
            "host": row["api_base_url"],
            "pill": {"kind": kind, "label": label},
        })
    return out


def _builds_reach(connection: Connection, project_id: str, connection_id: str,
                  full_name: str) -> dict[str, Any]:
    """Whether builds can use the token this row shows: under enforce they read the ORG's GitHub
    token from the vault, never this project connection. Said plainly with the next step when
    they cannot, because the row's green pill only proves the PROJECT token works."""
    try:
        from ..identity import github_setup

        org_id = connection.execute(
            text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                 "WHERE p.id = :p"), {"p": project_id}).scalar()
        if not org_id or github_setup.app_ready(connection, str(org_id)):
            return {"ok": True}
        if github_setup.org_token_for(connection, full_name, str(org_id)) is not None:
            return {"ok": True}
        ref = str(connection.execute(text("SELECT credential_ref FROM github_connections WHERE id = :c"),
                                     {"c": connection_id}).scalar() or "")
    except Exception:  # noqa: BLE001 - an unreadable vault is not a reason to hide the row
        return {"ok": True}
    if ref.startswith("identity_github:user:"):
        # "Sign in with GitHub": a person's own sign-in is never lent to the org's builds.
        return {"ok": False, "message": "Builds use your sign-in only when you grant agents “as me”.",
                "next": "For everyone's builds, an org admin adds the GitHub App under Advanced."}
    said = _latest_event(connection, project_id, "github.org_token_unchanged", connection_id)
    message = str((said or {}).get("message") or "").strip() or (
        "Builds can't use this token yet: it is not the organisation's GitHub token, which "
        "builds use.")
    return {"ok": False, "message": message,
            "next": "Press Replace token and paste it again, or add it under Identity › GitHub."}


def install_repository(settings: Any) -> tuple[str, str, str]:
    """(owner, name, token) an install configured in its settings file: BOSS_TARGET_GITHUB_*
    or the BOSS_GITHUB_OWNER / BOSS_GITHUB_REPO pair README installs set, with its token."""
    owner = (str(getattr(settings, "target_github_owner", "") or "").strip()
             or str(getattr(settings, "github_owner", "") or "").strip())
    name = (str(getattr(settings, "target_github_repo", "") or "").strip()
            or str(getattr(settings, "github_repo", "") or "").strip())
    token = str(getattr(settings, "github_token", "") or "").strip()
    return owner, name, token


def _repo_entry(*, binding_id: Optional[str], full_name: str, default: bool,
                private: Optional[bool], default_branch: Optional[str], checks: dict[str, Any],
                state: str, expires: Optional[datetime], checked: Optional[datetime],
                retry_at: Any, now: datetime,
                connection_id: Optional[str] = None) -> dict[str, Any]:
    days = max(0, (expires - now).days) if expires else None
    pill = setup_pills.pill(
        "repository", state, repo=full_name, n=days,
        date=expires.date().isoformat() if expires else "", time=_hhmm(retry_at),
        at=_iso(retry_at))

    def part(name: str) -> dict[str, Any]:
        raw = checks.get(name) if isinstance(checks.get(name), dict) else {}
        return {"ok": raw.get("ok"), "state": str(raw.get("state") or "not_run"),
                "at": raw.get("at")}

    return {
        "binding_id": binding_id, "full_name": full_name, "default": default,
        "connection_id": connection_id,
        "private": private, "default_branch": default_branch,
        "checks": {"read": part("read"), "write": part("write"),
                   "pull_requests": part("pull_requests")},
        "pill": pill, "checked_at": checked.isoformat() if checked else None,
        "stale": checked is None or now - checked > STALE_AFTER,
    }


#: Repository states a build can still use. "checking" is a binding no check has run on yet
#: (an install upgraded from before checks existed): it worked yesterday, and the page's
#: background check settles it within seconds of opening.
USABLE_REPOSITORY = frozenset({"working", "expiring", "checking", "rate_limited", "unreachable"})
#: Model states a job can still run on (a busy or unreachable provider recovers by itself).
USABLE_MODEL = frozenset({"working", "not_checked", "rate_limited", "paused_budget",
                          "unreachable", "login_expiring", "login_refresh_failed"})
#: A MODEL ON THIS MACHINE THAT DOES NOT ANSWER IS NOT AN OUTAGE, it is the wrong address (the
#: container's own 127.0.0.1) or a stopped server: it does not recover by itself, so Auto never
#: picks it. Applies to the Local kinds and to an OpenAI-compatible URL on the install machine
#: (`boss connect ollama`'s host.docker.internal).
USABLE_LOCAL_MODEL = USABLE_MODEL - {"unreachable"}
#: READY MEANS PROVEN. The header's "Repository and model checked" is only true of a row a check
#: has answered: a busy or unreachable GitHub, a check not yet run and a model nobody has tried
#: are still usable (a build may start) but they are not Ready.
PROVEN_REPOSITORY = frozenset({"working", "expiring"})
PROVEN_MODEL = frozenset({"working", "rate_limited", "paused_budget", "login_expiring"})


def repository_working(view_repo: dict[str, Any]) -> bool:
    return view_repo["pill"]["state"] in USABLE_REPOSITORY


# ── Models ────────────────────────────────────────────────────────────────────────────────────

def _provider_of(source_id: str, by_entitlement: dict[str, str]) -> str:
    from .setup_service import api_source_provider

    return api_source_provider(source_id) or by_entitlement.get(source_id, source_id)


def _by_entitlement(settings: Any) -> dict[str, str]:
    try:
        from .cli_harness import load_specs

        found = {spec.entitlement_source_id: spec.name for spec in load_specs(settings).values()
                 if spec.entitlement_source_id}
    except Exception:  # noqa: BLE001
        found = {}
    found.setdefault("anthropic_claude_code", "claude_code")
    # The console's Anthropic API key: an Anthropic key, run by the claude CLI (claude_credential).
    found.setdefault("anthropic_api", "anthropic")
    return found


def engine_for_provider(provider: str) -> str:
    return {"openai": "native", "custom": "native", "anthropic": "claude_code"}.get(
        provider, provider)


def provider_label(provider: str) -> str:
    entry = CATALOGUE_BY_PROVIDER.get(provider)
    if entry:
        return entry["label"]
    return ENGINE_LABELS.get(provider, provider.replace("_", " ").title())


#: A last proof the provider answered by REFUSING THE CREDENTIAL itself — not busy, not
#: unreachable, not a budget pause. Every surface that says whether a source works treats
#: these as broken (entitlements' effective status, the coding-agent list, the CLI summary).
CREDENTIAL_REFUSED = frozenset({"login_expired", "key_rejected", "key_unreadable"})


def source_state(*, kind: str, metadata: dict[str, Any], capacity: dict[str, Any],
                 budget_spent: bool, now: datetime
                 ) -> tuple[str, Optional[str], Optional[str]]:
    """THE ONE ANSWER to "does this model source work?": (state, checked_at, retry_at).

    LAST REAL PROOF FIRST, THEN THE CREDENTIAL. The state is what the last governed check
    (`setup_actions._record_model_check`) recorded; a credential a real call had refused
    (`observed_capacity.credential_state`) overrides it. A source nobody has proved is "Not
    checked" — never "Working": a subscription marked connected by a typed session ref
    (entitlements' Complete session) or by a connect that skipped its proof has answered
    nothing yet.

    Every surface reads this, so they cannot disagree: the Models row (`source_entry`), the
    Sources list (`source_pills` → GET /v1/entitlements/sources `pill`, and its
    `effective_status`), GET /v1/setup/coding-agents, the agent's diagnose_setup and
    detect_coding_agents, and `boss connect --summary`. (The live bug: Codex's last proof was
    refused 401 and the Models row said Login expired, while Sources said Active · Available
    and the CLI summary said Codex connected.)
    """
    from .provider_limits import credit_exhausted, is_available

    last = metadata.get("last_check") if isinstance(metadata.get("last_check"), dict) else {}
    checked_at = last.get("at") or metadata.get("checked_at")
    if last and last.get("state") != "paused_budget":
        state = str(last.get("state") or ("working" if last.get("ok") else "failed"))
    else:
        state = "not_checked"
    if capacity.get("credential_state") == "rejected":
        state = "login_expired" if kind == "subscription" else "key_rejected"
    retry_at = last.get("retry_at") if state == "rate_limited" else None
    if capacity.get("exhausted") and not is_available(capacity, now=now) \
            and state in ("working", "not_checked", "rate_limited"):
        # OUT OF CREDIT IS NOT A WINDOW: a real call the provider refused for money ("Credit
        # balance is too low") stays red until a person tops up and a call succeeds again. It
        # read "Working · Available" while every run on the key failed (2026-10-25).
        if credit_exhausted(str(capacity.get("detail") or "")):
            state, retry_at = "no_credit", None
        else:
            state, retry_at = "rate_limited", capacity.get("retry_at") or retry_at
    if state == "rate_limited" and retry_at and parse_time(retry_at) \
            and parse_time(retry_at) <= now:
        state = "working" if last.get("ok") or not last else "not_checked"
    if budget_spent and kind == "metered" and state in ("working", "not_checked"):
        state = "paused_budget"
    return state, checked_at, retry_at


#: The states a live Claude login expiry may replace: the ones that would otherwise say the
#: login works, or has not been tried.
_LOGIN_EXPIRY_REPLACES = frozenset({"working", "not_checked", "rate_limited", "login_expiring",
                                    "login_refresh_failed"})


def claude_login_state(state: str, *, provider: str, kind: str,
                       login: Optional[dict[str, Any]] = None,
                       long_lived: bool = False) -> tuple[str, Optional[int]]:
    """THE CLAUDE CLI LOGIN'S REAL EXPIRY over the last check: (state, minutes left).

    Only for the Claude Code subscription on the CLI's own login (claude_login.status): an
    expired token is Login expired, a refused renewal Renewal failed, a token inside the
    refresh window Expiring — never "Working"."""
    import os

    if provider != "claude_code" or kind != "subscription" or state not in _LOGIN_EXPIRY_REPLACES:
        return state, None
    if long_lived:
        return state, None                 # runs on BOSS's long-lived token, not the CLI login
    if login is None and (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return state, None                 # the CLI runs on that token, not on its own login
    if login is None:
        try:
            from . import claude_login

            login = claude_login.status()
        except Exception:  # noqa: BLE001 — unknown expiry leaves the check's answer
            return state, None
    live = str((login or {}).get("state") or "")
    left = (login or {}).get("seconds_left")
    minutes = None if left is None else max(0, int(left) // 60)
    if live == "expired":
        return "login_expired", 0
    if live == "refresh_failed":
        return "login_refresh_failed", minutes
    if live == "expiring":
        return "login_expiring", minutes
    if state in ("login_expiring", "login_refresh_failed"):
        return "working", minutes
    return state, minutes


def _source_kind(provider: str, row: Any) -> str:
    connected = row["connection_status"] == "connected"
    if provider in SUBSCRIPTIONS and (row["cost_mode"] == "subscription" or connected) \
            and not row["credential_secret_ref_id"]:
        return "subscription"
    if provider in SUBSCRIPTIONS and row["cost_mode"] == "subscription":
        return "subscription"
    if provider in LOCALS:
        return "local"
    return "metered"


def _model_rows(connection: Connection, project_id: str) -> list[Any]:
    """The sources Repo and models lists as models: a stored credential or a connection."""
    return connection.execute(
        text("SELECT source_id, provider, connection_status, credential_secret_ref_id, "
             "       cost_mode, supported_models, connection_metadata, observed_capacity, "
             "       updated_at "
             "  FROM entitlement_sources "
             " WHERE project_id = :p AND source_id <> 'typesafe_jev' "
             "   AND (credential_secret_ref_id IS NOT NULL OR connection_status = 'connected') "
             " ORDER BY created_at, source_id"), {"p": project_id}).mappings().all()


def source_entry(row: Any, *, by_entitlement: dict[str, str], now: datetime,
                 budget_spent: bool = False, budget_period: str = "month",
                 claude: Optional[dict[str, Any]] = None
                 ) -> Optional[dict[str, Any]]:
    """One Models row — its pill decided by `source_state` — or None when the source is not
    a model this page lists (a retired connection proof)."""
    source_id = str(row["source_id"])
    provider = _provider_of(source_id, by_entitlement)
    metadata = _obj(row["connection_metadata"])
    if metadata.get("proof_only"):
        return None
    capacity = _obj(row["observed_capacity"])
    kind = _source_kind(provider, row)
    state, checked_at, retry_at = source_state(
        kind=kind, metadata=metadata, capacity=capacity, budget_spent=budget_spent, now=now)
    # What Claude Code runs on (claude_credential.credential_summary): BOSS's long-lived token
    # never expires in hours, so the CLI login's expiry does not decide its pill.
    claude = claude if provider == "claude_code" and kind == "subscription" else None
    state, minutes_left = claude_login_state(
        state, provider=provider, kind=kind,
        long_lived=bool(claude and claude.get("kind") == "long_lived_token"))
    if checked_at is None and state == "working":
        checked_at = _iso(row["updated_at"])
    models = [m for m in _list(row["supported_models"]) if isinstance(m, str) and m.strip()]
    model = models[0] if models else (metadata.get("model") or None)
    last = metadata.get("last_check") if isinstance(metadata.get("last_check"), dict) else {}
    return _model_entry(
        source_id=source_id, provider=provider, kind=kind, model=model, state=state,
        checked_at=checked_at, retry_at=retry_at, source="connected", now=now,
        host=last.get("host") or "", latency_ms=last.get("latency_ms"),
        base_url=str(metadata.get("base_url") or ""), budget_period=budget_period,
        minutes_left=minutes_left, credential=claude)


def _claude_summary(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    try:
        from .claude_credential import credential_summary

        return credential_summary(connection, project_id)
    except Exception:  # noqa: BLE001 — unknown says nothing extra
        return None


def source_pills(connection: Connection, settings: Any, project_id: str, *,
                 now: Optional[datetime] = None) -> dict[str, dict[str, Any]]:
    """{source_id: the pill its Models row shows} — `{state, label, tone, checked_at}` — for
    every source Repo and models lists. A source absent here has no Models row (its pill is
    null). The same `source_entry` the page builds, with the same budget answer."""
    now = now or now_utc()
    try:
        budget = _budget(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — a budget that cannot be read pauses nothing here
        budget = {"state": "ok", "period": "month"}
    by_entitlement = _by_entitlement(settings)
    out: dict[str, dict[str, Any]] = {}
    claude = _claude_summary(connection, project_id)
    for row in _model_rows(connection, project_id):
        entry = source_entry(row, by_entitlement=by_entitlement, now=now,
                             budget_spent=budget["state"] == "spent",
                             budget_period=budget["period"],
                             claude=claude)
        if entry is None:
            continue
        out[entry["source_id"]] = {"state": entry["pill"]["state"],
                                   "label": entry["pill"]["label"],
                                   "tone": entry["pill"]["tone"],
                                   "checked_at": entry["checked_at"]}
    return out


def _models(connection: Connection, settings: Any, project_id: str, now: datetime,
            budget_spent: bool, *, budget_period: str = "month") -> list[dict[str, Any]]:
    from .setup_service import ENV_LANES, _set_deliberately

    by_entitlement = _by_entitlement(settings)
    out: list[dict[str, Any]] = []
    engines_seen: set[str] = set()
    claude = _claude_summary(connection, project_id)
    try:
        from .org_models import home_for

        is_home = home_for(connection, project_id) == project_id
    except Exception:  # noqa: BLE001
        is_home = False
    for row in _model_rows(connection, project_id):
        entry = source_entry(row, by_entitlement=by_entitlement, now=now,
                             budget_spent=budget_spent, budget_period=budget_period,
                             claude=claude)
        if entry is None:
            continue
        engines_seen.add(engine_for_provider(entry["provider"]))
        # The org's connection (a mirror of the org's model home, org_models) or one this
        # project made for itself.
        metadata = _obj(row["connection_metadata"])
        entry["scope"] = "org" if (metadata.get("org_source") or is_home) else "project"
        out.append(entry)
    # THE INSTALL'S OWN SETTINGS FILE. A key there is a model like any other, with the chip
    # `From install` — never a mystery "Unverified" model nobody remembers choosing.
    for engine, attr in ENV_LANES:
        if engine in engines_seen or not _set_deliberately(settings, attr):
            continue
        provider = _ENV_PROVIDER.get(attr, engine)
        source_id = f"env-{provider}"
        kind = ("local" if provider in LOCALS else
                "subscription" if attr == "claude_code_oauth_token" else "metered")
        last = _latest_event(connection, project_id, "setup.model_checked", source_id)
        metadata = {"last_check": {k: v for k, v in (last or {}).items() if k != "_at"}} \
            if last else {}
        if last:
            metadata["last_check"]["at"] = _iso(last["_at"])
        state, checked_at, retry_at = source_state(
            kind=kind, metadata=metadata, capacity={}, budget_spent=budget_spent, now=now)
        model = (str(getattr(settings, "openai_model", "") or "") or None) \
            if provider == "openai" else None
        engines_seen.add(engine)
        out.append(_model_entry(
            source_id=source_id, provider=provider, kind=kind, model=model, state=state,
            checked_at=checked_at, retry_at=retry_at, source="install", now=now,
            host=(last or {}).get("host") or "", latency_ms=(last or {}).get("latency_ms"),
            budget_period=budget_period))
    return out


def _model_entry(*, source_id: str, provider: str, kind: str, model: Optional[str], state: str,
                 checked_at: Optional[str], retry_at: Optional[str], source: str,
                 now: datetime, host: str, latency_ms: Any, base_url: str = "",
                 budget_period: str = "month", minutes_left: Optional[int] = None,
                 credential: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    entry = CATALOGUE_BY_PROVIDER.get(provider) or {}
    pill = setup_pills.pill(
        "model", state, n="" if minutes_left is None else minutes_left,
        age=_age(checked_at, now) or "a moment", time=_hhmm(retry_at),
        at=_iso(retry_at),
        model=model or "That model", host=host or "the provider",
        provider_fix=setup_pills.LOGIN_FIX.get(provider, setup_pills.DEFAULT_LOGIN_FIX),
        fix_url=entry.get("billing_url") if state == "no_credit" else None,
        # THE PILL NAMES THE CAP THAT IS ACTUALLY BINDING, never a hardcoded "Monthly cap":
        # `_budget` already worked out which period (day, month, or a burst refusal) is tightest.
        cap_name=_cap_name(budget_period))
    checked = parse_time(checked_at)
    return {
        "source_id": source_id, "provider": provider,
        "label": _label_with_host(provider, base_url),
        "kind": kind, "model": model, "used_for": [], "pill": pill,
        "checked_at": checked.isoformat() if checked else None,
        "source": source, "retry_at": retry_at, "latency_ms": latency_ms,
        "stale": checked is None or now - checked > STALE_AFTER,
        # Replace asks for the KEY only: the form keeps where it points (never a password that
        # was written into the URL).
        "base_url": _without_userinfo(base_url) or None,
        # Claude Code only: what it runs on — "long-lived subscription token (renews …)" or
        # "CLI login (expires in X h)". Never a secret.
        **({"credential": {"kind": credential.get("kind"), "label": credential.get("label"),
                           "expires_at": credential.get("expires_at")}} if credential else {}),
    }


def _without_userinfo(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    if not url:
        return ""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _label_with_host(provider: str, base_url: str) -> str:
    """"Other" alone says nothing: an endpoint the operator typed is named by its host."""
    label = provider_label(provider)
    if provider in ("custom", "vllm", "ollama") and base_url:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        if host:
            return f"{label} · {host}{f':{parsed.port}' if parsed.port else ''}"
    return label


def _catalogue(settings: Any) -> list[dict[str, Any]]:
    """The drawer's tiles. Ollama carries the address this BOSS looks at when its URL is left
    empty (OLLAMA_BASE_URL: the host's Ollama under Docker), so the placeholder is never the
    container's own loopback."""
    from .setup_service import ollama_base_url

    out = [dict(entry) for entry in CATALOGUE]
    for entry in out:
        if entry["provider"] == "ollama":
            entry["example_base_url"] = ollama_base_url(settings)
    return out


def model_is_local(entry: dict[str, Any]) -> bool:
    """A model served from the install machine: a Local row, or any row whose URL is there."""
    from .code_agent import is_local_model_endpoint

    return entry.get("kind") == "local" or is_local_model_endpoint(str(entry.get("base_url") or ""))


def model_working(entry: dict[str, Any]) -> bool:
    usable = USABLE_LOCAL_MODEL if model_is_local(entry) else USABLE_MODEL
    return entry["pill"]["state"] in usable


# ── Detected, on the server ───────────────────────────────────────────────────────────────────

def _detected(settings: Any) -> list[dict[str, Any]]:
    """What is on the server. A subscription is `signed_in` (its CLI is signed in inside
    BOSS), `needs_sign_in` (installed, not signed in — `boss connect <provider>` or Sign in
    does it there) or `not_on_server` (the CLI is not in this image). There is no token to
    paste and no file to drop, so no state asks for one."""
    found = _cli_detection(settings)
    out: list[dict[str, Any]] = []
    for provider in OFFERED_SUBSCRIPTIONS:
        info = found.get(provider) or {}
        if info.get("installed") and info.get("authenticated"):
            state = "signed_in"
        elif info.get("installed"):
            state = "needs_sign_in"
        else:
            state = "not_on_server"
        entry = {"provider": provider, "label": provider_label(provider),
                 "kind": "subscription", "found": state,
                 # A Claude token BOSS once stored (the retired setup-token import) is not a
                 # sign-in: it is reported, with the one governed way to remove it.
                 "legacy_token": bool(info.get("legacy_token"))}
        if info.get("legacy_token"):
            entry["fix"] = info.get("legacy_token_fix") or ""
        out.append(entry)
    for provider in LOCALS:
        local = local_found(provider)
        entry = {"provider": provider, "label": provider_label(provider),
                 "kind": "local", "found": "found" if local else "not_on_server"}
        if local:
            entry["base_url"] = local["base_url"]
        out.append(entry)
    # Found first: the drawer's tile order.
    order = {"signed_in": 0, "found": 0, "needs_sign_in": 1, "not_on_server": 2}
    return sorted(out, key=lambda d: (d["kind"] != "subscription", order[d["found"]]))


# ── Who does what ─────────────────────────────────────────────────────────────────────────────

def _offered_models(engine: str, model: Optional[str]) -> list[Optional[str]]:
    if engine in SUBSCRIPTIONS:
        try:
            from .cli_harness import offered_models

            names = list(offered_models(engine))
        except Exception:  # noqa: BLE001
            names = []
        return names or [None]
    return [model] if model else [None]


def _jobs(connection: Connection, settings: Any, project_id: str,
          models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from . import project_settings
    from .model_routing import CHAT_ROUTABLE, JOB_KNOBS, chat_model_knob, resolve_job

    chosen, own = {}, {}
    try:
        chosen = project_settings.get_all(connection, project_id)
        own = project_settings.get_own(connection, project_id)
    except Exception:  # noqa: BLE001
        chosen, own = {}, {}
    by_engine: dict[str, dict[str, Any]] = {}
    for entry in models:
        if model_working(entry):
            by_engine.setdefault(engine_for_provider(entry["provider"]), entry)
    _offer_agent_sdk(settings, models, by_engine)
    _say_which_claude_credential(connection, settings, project_id, by_engine)
    try:
        from .typed_decisions import _typesafe_key
        from .project_settings import applied

        jev_key = bool(_typesafe_key(applied(connection, project_id, settings)))
    except Exception:  # noqa: BLE001
        jev_key = False

    out: list[dict[str, Any]] = []
    for spec in JOBS:
        job = spec["job"]
        engine_knob, model_knob = JOB_KNOBS[job]
        options: list[dict[str, Any]] = []
        if job == "decisions":
            if "claude_code" in by_engine:
                for name in _offered_models("claude_code", None):
                    options.append(_option("claude_code", name, by_engine["claude_code"]))
            options.append({"engine": "typesafe", "model": None, "label": "TypeSafe Jev",
                            "metered": True, "needs_key": not jev_key})
        else:
            for engine, entry in by_engine.items():
                if job == "chat" and engine not in CHAT_ROUTABLE:
                    continue
                if entry.get("picks"):
                    # BOTH WAYS TO PAY, side by side (Claude Code, Codex): each model on the
                    # subscription and on the API key; the console folds them into one entry
                    # per model with a toggle (lib/job-picks), and each job takes either.
                    options.extend(_picked_options(engine, entry))
                    continue
                for name in _offered_models(engine, entry.get("model")):
                    options.append(_option(engine, name, entry))
        choice = None
        raw = chosen.get(engine_knob)
        if raw:
            engine = str(raw)
            if job == "decisions":
                engine = "typesafe" if engine == "typesafe" else "claude_code"
            knob = model_knob or chat_model_knob(engine)
            choice = {"engine": engine, "model": chosen.get(knob) or None}
        resolved = None
        try:
            picked = resolve_job(connection, settings, project_id, job)
        except Exception:  # noqa: BLE001
            picked = None
        if picked is not None and picked.engine:
            name = _source_label(picked.engine, picked.source_id, models)
            label = claude_choice_label(picked.model, picked.engine) \
                or (f"{name} · {picked.model}" if picked.model else name)
            resolved = {"engine": picked.engine, "model": picked.model,
                        "label": label if choice else f"Auto · {name}", "why": picked.why,
                        "source_id": picked.source_id}
        metered = bool(picked and picked.metered)
        if picked is not None and picked.engine in PICK_NAMES:
            from .claude_credential import PICK_KEY, PICK_SUBSCRIPTION, split_pick

            pick = split_pick(picked.model)[0]
            if pick in (PICK_KEY, PICK_SUBSCRIPTION):
                metered = pick == PICK_KEY  # the job's own pick decides who pays
        # WHOSE CHOICE: the org's (every project) or this project's pin, which Setup shows with
        # a Remove that hands the job back to the org's choice.
        scope = ("project" if engine_knob in own or (model_knob and model_knob in own)
                 else "org")
        out.append({**spec, "choice": choice, "resolved": resolved, "metered": metered,
                    "options": options, "scope": scope})
    for entry in models:
        entry["used_for"] = [j["label"] for j in out if j["resolved"]
                             and _resolves_to(j["resolved"], entry)]
    return out


def _offer_agent_sdk(settings: Any, models: list[dict[str, Any]],
                     by_engine: dict[str, dict[str, Any]]) -> None:
    """The Claude Agent SDK engine, as an option beside the Anthropic API key it runs on: a
    working key entry (metered) and the package importable (agent_sdk_engine.readiness)."""
    from .agent_sdk_engine import DEFAULT_MODEL, ENGINE, sdk_available

    key = next((e for e in models if e["provider"] == "anthropic" and e["kind"] == "metered"
                and model_working(e)), None)
    if key is None or ENGINE in by_engine or not sdk_available():
        return
    by_engine[ENGINE] = {**key, "label": ENGINE_LABELS[ENGINE],
                         "model": getattr(settings, "agent_sdk_model", "") or DEFAULT_MODEL}


#: What each Claude choice bills, said in its label (claude_credential's kinds).
CLAUDE_ON_SUBSCRIPTION = "Claude Code · subscription"
CLAUDE_CODE_ON_KEY = "Claude Code · API key"
CLAUDE_SDK_ON_KEY = "Claude · API key"


def _say_which_claude_credential(connection: Connection, settings: Any, project_id: str,
                                 by_engine: dict[str, dict[str, Any]]) -> None:
    """TWO CLAUDE CHOICES, EACH SAYING WHAT IT BILLS (boss-1, 2026-11-03). The Claude
    subscription and the Anthropic API key both map to the claude_code engine, and the list
    keeps ONE entry per engine — so the job menus showed a single "Claude Code" that silently
    ran on whichever credential claude_credential's order picked ("API key first" had been set,
    and $112 of builds billed the key while the subscription sat idle). The operator could not
    tell, nor pick the other one for a job.

    Now: the claude_code entry is labelled by the credential it will really use (subscription
    or API key, from claude_credential.describe — the same order every launch follows) and
    priced accordingly; the Claude Agent SDK engine, which always runs on the key, is offered
    beside it as "Claude · API key". Each job picks either."""
    from . import claude_credential

    try:
        cred = claude_credential.describe(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — the menus still list what works
        cred = {}
    active = str(cred.get("active") or "")
    claude = by_engine.get("claude_code")
    if claude is not None:
        # Which credentials a job may pick for itself (both, when both are connected).
        picks = [p for p, ok in ((claude_credential.PICK_SUBSCRIPTION, cred.get("has_subscription")),
                                 (claude_credential.PICK_KEY, cred.get("has_key"))) if ok]
        by_engine["claude_code"] = claude = {**claude, "picks": picks}
    if claude is not None and active:
        on_sub = active in (claude_credential.KIND_SUBSCRIPTION, claude_credential.KIND_CLI_LOGIN)
        by_engine["claude_code"] = {
            **claude, "label": CLAUDE_ON_SUBSCRIPTION if on_sub else CLAUDE_CODE_ON_KEY,
            "kind": "subscription" if on_sub else "metered"}
    sdk = by_engine.get("claude_agent_sdk")
    if sdk is not None:
        by_engine["claude_agent_sdk"] = {**sdk, "label": CLAUDE_SDK_ON_KEY, "kind": "metered"}
    # CODEX TOO: its subscription (the ChatGPT login) and an OpenAI API key, when both are held
    # (cli_harness.apply_codex_pick runs each job on the one it picked).
    codex = by_engine.get("codex")
    has_openai_key = "native" in by_engine or bool(
        str(getattr(settings, "openai_api_key", "") or "").strip())
    if codex is not None and has_openai_key:
        by_engine["codex"] = {**codex, "picks": [claude_credential.PICK_SUBSCRIPTION,
                                                  claude_credential.PICK_KEY]}


def _source_label(engine: str, source_id: Optional[str], models: list[dict[str, Any]]) -> str:
    for entry in models:
        if source_id and entry["source_id"] == source_id:
            return entry["label"]
    for entry in models:
        if engine_for_provider(entry["provider"]) == engine:
            return entry["label"]
    return ENGINE_LABELS.get(engine, engine)


def _resolves_to(resolved: dict[str, Any], entry: dict[str, Any]) -> bool:
    if resolved.get("source_id") and resolved["source_id"] == entry["source_id"]:
        return True
    return engine_for_provider(entry["provider"]) == resolved["engine"] and (
        not resolved.get("source_id") or entry["source"] == "install")


PICK_NAMES = {"claude_code": (CLAUDE_ON_SUBSCRIPTION, CLAUDE_CODE_ON_KEY),
              "codex": ("Codex · subscription", "Codex · API key")}


def _picked_options(engine: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
    from .claude_credential import PICK_SUBSCRIPTION, with_pick

    on_sub, on_key = PICK_NAMES[engine]
    out = []
    for pick in entry["picks"]:
        name = on_sub if pick == PICK_SUBSCRIPTION else on_key
        for model in _offered_models(engine, entry.get("model")):
            out.append({"engine": engine, "model": with_pick(pick, model),
                        "label": f"{name} · {model}" if model else name,
                        "metered": pick != PICK_SUBSCRIPTION})
    return out


def claude_choice_label(model: Optional[str], engine: str = "claude_code") -> Optional[str]:
    """"Claude Code · API key · opus" for a stored "key:opus"; None when it carries no pick."""
    from .claude_credential import PICK_SUBSCRIPTION, split_pick

    pick, bare = split_pick(model)
    if pick is None or engine not in PICK_NAMES:
        return None
    name = PICK_NAMES[engine][0 if pick == PICK_SUBSCRIPTION else 1]
    return f"{name} · {bare}" if bare else name


def _option(engine: str, model: Optional[str], entry: dict[str, Any]) -> dict[str, Any]:
    name = entry["label"]
    return {"engine": engine, "model": model,
            "label": f"{name} · {model}" if model else name,
            "metered": entry["kind"] == "metered"}


# ── Budget ────────────────────────────────────────────────────────────────────────────────────

def _budget(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """The budget row, the header's "Budget spent" and every metered model's "Paused · budget"
    — ONE answer, and it is the gate's own: would the budget refuse one metered model check
    (`BudgetGovernor.cap_refusal` with the check's reservation) on the money already spent?
    A $0.01 cap read "OK" while every metered call was refused; now it reads Spent."""
    from .budget import BudgetGovernor
    from .setup_service import PROOF_ESTIMATE_USD_MICROS

    governor = BudgetGovernor(settings)
    try:
        status = governor.status_for_project(connection, project_id)
    except Exception:  # noqa: BLE001
        status = {}
    periods = status.get("periods") or {}
    month = periods.get("month") or {}
    refusal = None
    try:
        refusal = governor.cap_refusal(status, PROOF_ESTIMATE_USD_MICROS) if periods else None
    except Exception:  # noqa: BLE001
        refusal = None
    if refusal is not None and refusal.get("kind") == "cap":
        binding, state = refusal, "spent"
    else:
        near = [p for p in periods.values() if p.get("budget_state") in ("orange", "yellow", "red")]
        binding = (max(near, key=lambda p: p.get("utilization_ratio") or 0) if near else month)
        state = "near" if near else "ok"
    cap_micros = int(binding.get("cap_usd_micros")
                     or getattr(settings, "monthly_cap_usd_micros", 200_000_000) or 0)
    spent_micros = int(binding.get("committed_usd_micros") or 0)
    period = binding.get("period") or "month"
    resets = binding.get("resets_at") or status.get("resets_at") or ""
    spent, cap = spent_micros / 1_000_000, cap_micros / 1_000_000
    when = parse_time(resets)
    pill = setup_pills.pill("budget", state, spent=_usd(spent), cap=_usd(cap),
                            date=(when.astimezone().strftime("%b %-d") if when else "reset"))
    return {"period": period, "spent_usd": round(spent, 2), "cap_usd": round(cap, 2),
            "state": state, "resets_at": resets, "pill": pill,
            "monthly_cap_usd": round(int(month.get("cap_usd_micros") or cap_micros)
                                     / 1_000_000, 2)}


def _usd(value: float) -> str:
    return f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"


def _cap_name(period: str) -> str:
    """"Daily cap" / "Monthly cap" / "Burst cap" — the words for whichever cap is actually
    binding (`_budget`'s `period`), never a hardcoded "Monthly cap" (J8 follow-up, boss-1
    rc10.9: a model paused by the daily or a burst cap still read "Monthly cap reached")."""
    from .budget import PERIOD_WORDS

    _, cap_name = PERIOD_WORDS.get(period, PERIOD_WORDS["month"])
    return cap_name[:1].upper() + cap_name[1:]


# ── Try it ────────────────────────────────────────────────────────────────────────────────────

def _try(connection: Connection, project_id: str) -> dict[str, Any]:
    last = _latest_event(connection, project_id, "setup.try_started", SETUP_TRY_WORKFLOW)
    any_pr = connection.execute(
        text("SELECT 1 FROM code_task_runs WHERE project_id = :p "
             "AND pull_request_url IS NOT NULL LIMIT 1"), {"p": project_id}).first()
    out: dict[str, Any] = {"workflow_key": SETUP_TRY_WORKFLOW, "last_run": None,
                           "done": bool(any_pr)}
    if not last or not last.get("run_id"):
        return out
    run_id = str(last["run_id"])
    run = connection.execute(
        text("SELECT status, error FROM workflow_runs WHERE id = :r AND project_id = :p"),
        {"r": run_id, "p": project_id}).mappings().first()
    if run is None:
        return out
    step = connection.execute(
        text("SELECT step_id, status, output, last_error AS error FROM workflow_step_runs "
             " WHERE workflow_run_id = :r AND step_id = 'build' "
             " ORDER BY iteration DESC LIMIT 1"), {"r": run_id}).mappings().first()
    output = _obj((step or {}).get("output"))
    task_id = output.get("code_task_run_id")
    task = None
    if task_id:
        task = connection.execute(
            text("SELECT status, phase, pull_request_url, pull_request_number, error "
                 "  FROM code_task_runs WHERE id = :t"), {"t": task_id}).mappings().first()
    pr_url = (task or {}).get("pull_request_url") or output.get("pull_request_url")
    pr_number = (task or {}).get("pull_request_number") or output.get("pull_request_number")
    status = str(run["status"])
    if pr_url:
        state = "pr_open"
    elif status in ("failed", "deadlettered", "cancelled") \
            or (step or {}).get("status") in ("failed", "deadlettered"):
        state = "failed"
    else:
        phase = str((task or {}).get("phase") or "queued")
        state = ("testing" if phase in ("validating", "opening_pr") else
                 "building" if phase in ("coding",) else "planning")
    headline = ""
    if state == "failed":
        from .model_failures import explain

        error = run.get("error") or (step or {}).get("error") or (task or {}).get("error") or ""
        headline = explain(error).get("headline") or "The build did not open a PR."
    pill = setup_pills.pill("try", state, n=pr_number or "", headline=headline)
    out["last_run"] = {"run_id": run_id, "status": status, "pr_url": pr_url,
                       "pr_number": pr_number, "pill": pill}
    if pr_url:
        out["done"] = True
    return out


def _say_signed_out(models: list[dict[str, Any]], jobs: list[dict[str, Any]],
                    routing: Optional[dict[str, Any]]) -> None:
    """A subscription whose login failed says so EVERYWHERE (BOSSAPP-808 walk): its routing
    headroom reads its pill ("Login expired"), never "Plenty", and a job it would run carries the
    same pill. Usage left says nothing about a login the provider refused."""
    bad: dict[str, dict[str, Any]] = {}
    for m in models:
        if m.get("kind") == "subscription" and (m.get("pill") or {}).get("tone") == "red":
            bad.setdefault(engine_for_provider(str(m.get("provider") or "")), m["pill"])
    if not bad:
        return
    for job in jobs:
        resolved = job.get("resolved")
        if isinstance(resolved, dict) and resolved.get("engine") in bad:
            resolved["pill"] = dict(bad[resolved["engine"]])
    if not routing:
        return
    for h in routing.get("headroom") or []:
        pill = bad.get(h.get("engine"))
        if pill:
            h.update({"state": "exhausted", "left": 0, "label": pill.get("label") or "Signed out",
                      "describe": pill.get("tip") or "Sign in again.", "signed_out": True})
    for src in routing.get("sources") or []:
        if src.get("engine") in bad:
            src["working"] = False


def _accounts(connection: Connection, settings: Any, project_id: str, repository: dict[str, Any],
              now: datetime) -> dict[str, Any]:
    """Setup's accounts and repositories (setup_accounts.py). Never breaks the page."""
    from . import setup_accounts

    try:
        return setup_accounts.build(connection, settings, project_id, repository, now)
    except Exception:  # noqa: BLE001 — the rows above still render; the new blocks fall back
        import logging

        logging.getLogger(__name__).warning("setup: GitHub accounts were not read", exc_info=True)
        return {"accounts": [], "repositories": []}


# ── The view ──────────────────────────────────────────────────────────────────────────────────

def build_basics(connection: Connection, settings: Any, project_id: str, *,
                 now: Optional[datetime] = None) -> dict[str, Any]:
    """Only the repository and model rows — what every workflow Start asks (`basics`)."""
    now = now or now_utc()
    return {"repository": _repository(connection, settings, project_id, now),
            "models": _models(connection, settings, project_id, now, False)}


def build(connection: Connection, settings: Any, project_id: str, *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """The whole of Repo and models. No network, no decryption, NO SUBPROCESS: every CLI
    catalog and detection comes from a cache refreshed in the background (`detecting` says a
    refresh is on its way, so the page asks again)."""
    from .cli_harness import catalogs_from_cache_only

    with catalogs_from_cache_only():
        view = _build(connection, settings, project_id, now=now)
    view["detecting"] = detection_pending()
    return view


def _build(connection: Connection, settings: Any, project_id: str, *,
           now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or now_utc()
    budget = _budget(connection, settings, project_id)
    repository = _repository(connection, settings, project_id, now)
    models = _models(connection, settings, project_id, now, budget["state"] == "spent",
                     budget_period=budget["period"])
    jobs = _jobs(connection, settings, project_id, models)
    detected = _detected(settings)
    trying = _try(connection, project_id)
    accounts = _accounts(connection, settings, project_id, repository, now)
    routing = _routing(connection, settings, project_id)
    _say_signed_out(models, jobs, routing)

    # READY MEANS PROVEN (PROVEN_REPOSITORY / PROVEN_MODEL): a row a check has not answered
    # still lets a workflow start (basics), but the header does not say Ready over it.
    repo_ok = any(r["pill"]["state"] in PROVEN_REPOSITORY for r in repository["repos"])
    model_ok = any(m["pill"]["state"] in PROVEN_MODEL for m in models)
    ready = repo_ok and model_ok
    problems_list = [r["pill"] for r in repository["repos"] if r["pill"]["tone"] == "red"] \
        + [m["pill"] for m in models if m["pill"]["tone"] == "red"]
    problems = len(problems_list)
    # THE STEPPER IS FOR THE FIRST RUN ONLY. Once a first PR exists the page is a board for
    # good: a token that expires later is a red row on the board, not a return to step 1
    # (rows renumbered, Who does what collapsed to its defaults mid-edit).
    next_step = (None if trying["done"] else "repository" if not repo_ok
                 else "model" if not model_ok else "try")

    if budget["state"] == "spent":
        verdict = setup_pills.pill("verdict", "budget_spent", date=budget["pill"]["tip"])
        verdict["tip"] = budget["pill"]["tip"]
    elif problems:
        first = problems_list[0]
        verdict = setup_pills.pill("verdict", "problem" if problems == 1 else "problems",
                                   n=problems, headline=first["tip"])
        verdict["at"] = first.get("at")
    elif ready:
        verdict = setup_pills.pill("verdict", "ready")
    else:
        # NAME WHAT IS MISSING: a connected subscription is not told to connect a model.
        missing = " and ".join(part for part, gone in (("a repository", not repo_ok),
                                                       ("a model", not model_ok)) if gone)
        verdict = setup_pills.pill("verdict", "to_go", n=int(not repo_ok) + int(not model_ok),
                                   missing=missing)
        # SAY WHY, not the generic line: an org approval pending, a busy GitHub, a check still
        # running is the reason it is not ready, and the chip's tip names it.
        waiting = next((row["pill"] for row in [*repository["repos"], *models]
                        if row["pill"]["tone"] in ("amber", "busy")), None)
        if waiting:
            verdict["tip"], verdict["at"] = waiting["tip"], waiting.get("at")
    return {
        "project_id": project_id,
        "ready": ready,
        "next": next_step,
        # `at` travels with any tip that names a time, so the console shows it in local time.
        "verdict": {"label": verdict["label"], "tone": verdict["tone"], "tip": verdict["tip"],
                    "at": verdict.get("at")},
        "problems": problems,
        "secrets": secrets_state(connection, settings, project_id),
        "repository": repository,
        # Setup's GitHub accounts and Repositories BOSS can work on (BOSSAPP-808): one row per
        # account (this project's connections and the org's own App and tokens), one per repo
        # with the account it goes through and the projects that use it.
        "github_accounts": accounts["accounts"],
        "repositories": accounts["repositories"],
        "models": models,
        "detected": detected,
        "catalogue": _catalogue(settings),
        # Connection proofs whose money is held until a person reviews them (Review → Release).
        "held_proofs": _held_proofs(connection, project_id),
        "jobs": jobs,
        # Model routing (docs/product/MODEL_ROUTER.md): Smart / Fixed, tiers, floors, headroom,
        # the classifier and this week's mix. None when the router's tables are not there yet.
        "routing": routing,
        "budget": budget,
        "try": trying,
        # Build toolchains in this image (Python and Node always; Java when turned on).
        "toolchains": _toolchains(),
        "build_database": build_database(connection, settings, project_id),
        "build_speed": build_speed(connection, settings, project_id),
        # The embedding model, its own slot (knowledge/embedding_slot.py): only that a key is
        # held, never the key; whether meaning-based search is on, and why not in words.
        "embedding_model": embedding_model(connection, settings, project_id),
        # Whose models these are: the org's (set once, every project uses them), the org's model
        # home, and this project's pins (org_models.view).
        "models_scope": _models_scope(connection, project_id),
        # Which credential Claude Code runs on, and the operator's order (subscription first or
        # API key first) — never a secret, only which kind is active (claude_credential.describe).
        "claude_credential": _claude_credential(connection, settings, project_id),
        "generated_at": now.isoformat(),
    }


def _claude_credential(connection: Connection, settings: Any, project_id: str
                       ) -> Optional[dict[str, Any]]:
    from . import claude_credential

    try:
        found = claude_credential.describe(connection, settings, project_id)
    except Exception:  # noqa: BLE001
        return None
    return {"order": found.get("order"), "active": found.get("active"),
            "billing": found.get("billing"),
            "subscription_disconnected": bool(found.get("subscription_disconnected"))}


def _models_scope(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    from . import org_models

    try:
        return org_models.view(connection, project_id)
    except Exception:  # noqa: BLE001 — the scope line is information, never a failed view
        return None


def _held_proofs(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    from .connection_spend import held_proofs

    try:
        with connection.begin_nested():
            return held_proofs(connection, project_id)
    except Exception:  # noqa: BLE001 — the board still renders; the hold stays on its receipt
        return []


def _toolchains() -> list[dict[str, Any]]:
    from . import toolchains

    return toolchains.setup_summary()


def build_database(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """"Give builds a database (Postgres)": the project setting `build_services`
    (services/build_services.py). On, every build of this project gets its own empty PostgreSQL
    database, dropped when the build ends. `configured` says whether this install has a
    build-services server at all (the variable only: no network here)."""
    import os

    from . import build_services, project_settings

    try:
        with connection.begin_nested():
            chosen = project_settings.get_all(connection, project_id)
    except Exception:  # noqa: BLE001
        chosen = {}
    mode = build_services.mode_of(chosen.get(build_services.SETTING_KEY)
                                  or getattr(settings, "build_services", None))
    return {"key": build_services.SETTING_KEY, "mode": mode, "enabled": mode != build_services.NEVER,
            "source": "project" if build_services.SETTING_KEY in chosen else "default",
            "configured": bool((os.environ.get(build_services.SERVER_URL_ENV) or "").strip()),
            "permission": "settings:update"}


def embedding_model(connection: Connection, settings: Any, project_id: str) -> Optional[dict[str, Any]]:
    """Setup › Repo and models › Embedding model (the org's). None only when it cannot be read (the rest still renders)."""
    from .knowledge import embedding_slot

    try:
        with connection.begin_nested():
            return embedding_slot.status(connection, settings, project_id)
    except Exception:  # noqa: BLE001
        return None


def build_speed(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """"Build speed": how many parallel sub-agents a build's writing stages may use
    (project_settings.build_parallelism). `parallelism` is what a build gets right now, after
    the machine's own cap; `machine` is that cap. Same model and same checks at every speed."""
    from . import project_settings

    try:
        with connection.begin_nested():
            chosen = project_settings.get_all(connection, project_id)
            parallelism = project_settings.build_parallelism(connection, project_id, settings)
    except Exception:  # noqa: BLE001 — the rest of Repo and models still renders
        chosen, parallelism = {}, 1
    mode = str(chosen.get("build_speed") or getattr(settings, "build_speed", None)
               or "balanced").lower()
    if mode not in project_settings.BUILD_SPEED_PARALLELISM:
        mode = "balanced"
    return {"key": "build_speed", "mode": mode, "parallelism": int(parallelism),
            "machine": project_settings.machine_parallelism(),
            "asks": dict(project_settings.BUILD_SPEED_PARALLELISM),
            "source": "project" if "build_speed" in chosen else "default",
            "permission": "settings:update"}


def _routing(connection: Connection, settings: Any, project_id: str) -> Optional[dict[str, Any]]:
    try:
        from .model_router.view import routing_view

        with connection.begin_nested():
            return routing_view(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — the rest of Repo and models still renders
        import logging

        logging.getLogger("boss.setup_view").warning("routing view failed", exc_info=True)
        return None


def basics(view: dict[str, Any], *, router_runnable: Optional[bool] = None) -> dict[str, Any]:
    """`connected_basics`' contract, read from the view: {github, model, repository}.

    `router_runnable`: whether the chat router can build an adapter at all
    (`model_routing.choose(...).runnable`). A model counts when a row is usable AND the router
    can reach one — a CLI-only install is connected for builds, not for a step's model call.
    """
    repos = view["repository"]["repos"]
    default = next((r for r in repos if r["default"]), repos[0] if repos else None)
    usable = any(model_working(m) for m in view["models"])
    github = any(repository_working(r) for r in repos)
    # SAY THE ROW'S OWN REASON. An expired token is not "no repository is connected": a
    # workflow Start names what the page's pill says ("Token expired on …. Paste a new one.").
    why_repo = None if github or not repos else f"{default['full_name']}: {default['pill']['tip']}"
    broken = [m for m in view["models"] if not model_working(m)]
    why_model = None if usable or not broken else f"{broken[0]['label']}: {broken[0]['pill']['tip']}"
    return {"github": github,
            "model": usable if router_runnable is None else (usable and router_runnable),
            "repository": default["full_name"] if default else None,
            "repository_problem": why_repo, "model_problem": why_model}


def stale_targets(view: dict[str, Any]) -> list[str]:
    """Check targets whose last result is over a day old (or missing)."""
    targets = [f"repository:{r['binding_id']}" for r in view["repository"]["repos"]
               if r["stale"] and r["binding_id"]]
    if any(r["stale"] and not r["binding_id"] for r in view["repository"]["repos"]):
        targets.append("repository:install")
    targets += [f"model:{m['source_id']}" for m in view["models"] if m["stale"]]
    return targets


def first_problem(view: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The first red row, for the Command Center: {row, label, tip, href}."""
    for repo in view["repository"]["repos"]:
        if repo["pill"]["tone"] == "red":
            return {"row": "repository", "label": repo["pill"]["label"],
                    "tip": repo["pill"]["tip"], "href": "/?area=setup&focus=repository"}
    for model in view["models"]:
        if model["pill"]["tone"] == "red":
            return {"row": "models", "label": model["pill"]["label"], "tip": model["pill"]["tip"],
                    "href": f"/?area=setup&focus=models&source={model['source_id']}"}
    return None


def expiring_within(view: dict[str, Any], days: int = 3) -> Optional[dict[str, Any]]:
    """A connected token that stops working within `days`: {full_name, days}."""
    expires = parse_time((view["repository"]["connection"] or {}).get("expires_at"))
    if not expires:
        return None
    left = expires - now_utc()
    if timedelta(0) < left <= timedelta(days=days):
        repos = view["repository"]["repos"]
        return {"full_name": repos[0]["full_name"] if repos else "", "days": max(1, left.days)}
    return None


__all__ = ["build", "basics", "SETUP_TRY_WORKFLOW", "CATALOGUE", "stale_targets",
           "first_problem", "expiring_within", "remember_local", "forget_cached",
           "secrets_state", "provider_label", "engine_for_provider"]
