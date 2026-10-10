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
CATALOGUE: tuple[dict[str, Any], ...] = (
    {"provider": "claude_code", "label": "Claude Code", "kind": "subscription",
     "fields": ["token"], "command": "claude setup-token"},
    {"provider": "codex", "label": "Codex", "kind": "subscription",
     "fields": ["file"], "command": "~/.codex/auth.json"},
    {"provider": "grok", "label": "Grok Build", "kind": "subscription", "fields": []},
    {"provider": "cursor", "label": "Cursor", "kind": "subscription", "fields": []},
    {"provider": "openrouter", "label": "OpenRouter", "kind": "api_key",
     "fields": ["api_key", "model"], "key_url": "https://openrouter.ai/keys",
     "example_model": "anthropic/claude-sonnet-4.5",
     "billing_url": "https://openrouter.ai/settings/credits"},
    {"provider": "openai", "label": "OpenAI", "kind": "api_key", "fields": ["api_key", "model"],
     "key_url": "https://platform.openai.com/api-keys", "example_model": "gpt-4o",
     "billing_url": "https://platform.openai.com/settings/organization/billing"},
    {"provider": "anthropic", "label": "Anthropic", "kind": "api_key",
     "fields": ["api_key", "model"], "key_url": "https://console.anthropic.com/settings/keys",
     "example_model": "claude-sonnet-4-5",
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

#: engine (harness) -> the name an operator knows it by.
ENGINE_LABELS = {
    "claude_code": "Claude Code", "codex": "Codex", "grok": "Grok Build", "cursor": "Cursor",
    "native": "OpenAI", "openrouter": "OpenRouter", "gemini": "Gemini", "xai": "xAI",
    "ollama": "Ollama", "vllm": "Local model", "azure": "Azure OpenAI", "groq": "Groq",
    "deepseek": "DeepSeek", "typesafe": "TypeSafe Jev", "aider": "Aider",
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
            expires=expires, checked=checked, retry_at=checks.get("retry_at"), now=now))

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
    return {"connection": connection_out, "repos": repos}


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
        current = github_setup.org_token(connection, str(org_id))
        if current is not None and full_name.lower() in [r.lower() for r in current["repos"]]:
            return {"ok": True}
    except Exception:  # noqa: BLE001 - an unreadable vault is not a reason to hide the row
        return {"ok": True}
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
                retry_at: Any, now: datetime) -> dict[str, Any]:
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
                          "unreachable"})
#: READY MEANS PROVEN. The header's "Repository and model checked" is only true of a row a check
#: has answered: a busy or unreachable GitHub, a check not yet run and a model nobody has tried
#: are still usable (a build may start) but they are not Ready.
PROVEN_REPOSITORY = frozenset({"working", "expiring"})
PROVEN_MODEL = frozenset({"working", "rate_limited", "paused_budget"})


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
    return found


def engine_for_provider(provider: str) -> str:
    return {"openai": "native", "custom": "native", "anthropic": "claude_code"}.get(
        provider, provider)


def provider_label(provider: str) -> str:
    entry = CATALOGUE_BY_PROVIDER.get(provider)
    if entry:
        return entry["label"]
    return ENGINE_LABELS.get(provider, provider.replace("_", " ").title())


def _model_state(*, kind: str, metadata: dict[str, Any], capacity: dict[str, Any],
                 connected: bool, budget_spent: bool, now: datetime
                 ) -> tuple[str, Optional[str], Optional[str]]:
    """(state, checked_at, retry_at) for one model source."""
    from .provider_limits import is_available

    last = metadata.get("last_check") if isinstance(metadata.get("last_check"), dict) else {}
    checked_at = last.get("at") or metadata.get("checked_at")
    if last and last.get("state") != "paused_budget":
        state = str(last.get("state") or ("working" if last.get("ok") else "failed"))
    elif kind == "subscription" and connected:
        # Recorded before checks existed, and only after a governed proof turn: it worked.
        state = "working"
    else:
        state = "not_checked"
    if capacity.get("credential_state") == "rejected":
        state = "login_expired" if kind == "subscription" else "key_rejected"
    retry_at = last.get("retry_at") if state == "rate_limited" else None
    if capacity.get("exhausted") and not is_available(capacity, now=now) \
            and state in ("working", "not_checked", "rate_limited"):
        state, retry_at = "rate_limited", capacity.get("retry_at") or retry_at
    if state == "rate_limited" and retry_at and parse_time(retry_at) \
            and parse_time(retry_at) <= now:
        state = "working" if last.get("ok") or not last else "not_checked"
    if budget_spent and kind == "metered" and state in ("working", "not_checked"):
        state = "paused_budget"
    return state, checked_at, retry_at


def _models(connection: Connection, settings: Any, project_id: str, now: datetime,
            budget_spent: bool) -> list[dict[str, Any]]:
    from .setup_service import ENV_LANES, _set_deliberately

    by_entitlement = _by_entitlement(settings)
    rows = connection.execute(
        text("SELECT source_id, provider, connection_status, credential_secret_ref_id, "
             "       cost_mode, supported_models, connection_metadata, observed_capacity, "
             "       updated_at "
             "  FROM entitlement_sources "
             " WHERE project_id = :p AND source_id <> 'typesafe_jev' "
             "   AND (credential_secret_ref_id IS NOT NULL OR connection_status = 'connected') "
             " ORDER BY created_at, source_id"), {"p": project_id}).mappings().all()
    out: list[dict[str, Any]] = []
    engines_seen: set[str] = set()
    for row in rows:
        source_id = str(row["source_id"])
        provider = _provider_of(source_id, by_entitlement)
        metadata = _obj(row["connection_metadata"])
        if metadata.get("proof_only"):
            continue
        capacity = _obj(row["observed_capacity"])
        connected = row["connection_status"] == "connected"
        if provider in SUBSCRIPTIONS and (row["cost_mode"] == "subscription" or connected) \
                and not row["credential_secret_ref_id"]:
            kind = "subscription"
        elif provider in SUBSCRIPTIONS and row["cost_mode"] == "subscription":
            kind = "subscription"
        elif provider in LOCALS:
            kind = "local"
        else:
            kind = "metered"
        state, checked_at, retry_at = _model_state(
            kind=kind, metadata=metadata, capacity=capacity, connected=connected,
            budget_spent=budget_spent, now=now)
        if checked_at is None and state == "working":
            checked_at = _iso(row["updated_at"])
        models = [m for m in _list(row["supported_models"]) if isinstance(m, str) and m.strip()]
        model = models[0] if models else (metadata.get("model") or None)
        last = metadata.get("last_check") if isinstance(metadata.get("last_check"), dict) else {}
        engines_seen.add(engine_for_provider(provider))
        out.append(_model_entry(
            source_id=source_id, provider=provider, kind=kind, model=model, state=state,
            checked_at=checked_at, retry_at=retry_at, source="connected", now=now,
            host=last.get("host") or "", latency_ms=last.get("latency_ms"),
            base_url=str(metadata.get("base_url") or "")))
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
        state, checked_at, retry_at = _model_state(
            kind=kind, metadata=metadata, capacity={}, connected=False,
            budget_spent=budget_spent, now=now)
        model = (str(getattr(settings, "openai_model", "") or "") or None) \
            if provider == "openai" else None
        engines_seen.add(engine)
        out.append(_model_entry(
            source_id=source_id, provider=provider, kind=kind, model=model, state=state,
            checked_at=checked_at, retry_at=retry_at, source="install", now=now,
            host=(last or {}).get("host") or "", latency_ms=(last or {}).get("latency_ms")))
    return out


def _model_entry(*, source_id: str, provider: str, kind: str, model: Optional[str], state: str,
                 checked_at: Optional[str], retry_at: Optional[str], source: str,
                 now: datetime, host: str, latency_ms: Any, base_url: str = "") -> dict[str, Any]:
    entry = CATALOGUE_BY_PROVIDER.get(provider) or {}
    pill = setup_pills.pill(
        "model", state, age=_age(checked_at, now) or "a moment", time=_hhmm(retry_at),
        at=_iso(retry_at),
        model=model or "That model", host=host or "the provider",
        provider_fix=setup_pills.LOGIN_FIX.get(provider, setup_pills.DEFAULT_LOGIN_FIX),
        fix_url=entry.get("billing_url") if state == "no_credit" else None)
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


def model_working(entry: dict[str, Any]) -> bool:
    return entry["pill"]["state"] in USABLE_MODEL


# ── Detected, on the server ───────────────────────────────────────────────────────────────────

def _detected(settings: Any) -> list[dict[str, Any]]:
    found = _cli_detection(settings)
    out: list[dict[str, Any]] = []
    for provider in SUBSCRIPTIONS:
        info = found.get(provider) or {}
        if info.get("installed") and info.get("authenticated"):
            state = "signed_in"
        elif not info.get("installed"):
            state = "not_on_server"
        elif provider == "claude_code":
            state = "needs_token"
        elif provider == "codex":
            state = "needs_file"
        else:
            state = "not_on_server"
        out.append({"provider": provider, "label": provider_label(provider),
                    "kind": "subscription", "found": state})
    for provider in LOCALS:
        local = local_found(provider)
        entry: dict[str, Any] = {"provider": provider, "label": provider_label(provider),
                                 "kind": "local", "found": "found" if local else "not_on_server"}
        if local:
            entry["base_url"] = local["base_url"]
        out.append(entry)
    # Found first: the drawer's tile order.
    order = {"signed_in": 0, "found": 0, "needs_token": 1, "needs_file": 1, "not_on_server": 2}
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

    chosen = {}
    try:
        chosen = project_settings.get_all(connection, project_id)
    except Exception:  # noqa: BLE001
        chosen = {}
    by_engine: dict[str, dict[str, Any]] = {}
    for entry in models:
        if model_working(entry):
            by_engine.setdefault(engine_for_provider(entry["provider"]), entry)
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
            label = f"{name} · {picked.model}" if picked.model else name
            resolved = {"engine": picked.engine, "model": picked.model,
                        "label": label if choice else f"Auto · {name}", "why": picked.why,
                        "source_id": picked.source_id}
        metered = bool(picked and picked.metered)
        out.append({**spec, "choice": choice, "resolved": resolved, "metered": metered,
                    "options": options})
    for entry in models:
        entry["used_for"] = [j["label"] for j in out if j["resolved"]
                             and _resolves_to(j["resolved"], entry)]
    return out


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
    models = _models(connection, settings, project_id, now, budget["state"] == "spent")
    jobs = _jobs(connection, settings, project_id, models)
    detected = _detected(settings)
    trying = _try(connection, project_id)

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
        "models": models,
        "detected": detected,
        "catalogue": [dict(entry) for entry in CATALOGUE],
        "jobs": jobs,
        # Model routing (docs/product/MODEL_ROUTER.md): Smart / Fixed, tiers, floors, headroom,
        # the classifier and this week's mix. None when the router's tables are not there yet.
        "routing": _routing(connection, settings, project_id),
        "budget": budget,
        "try": trying,
        "build_database": build_database(connection, settings, project_id),
        "generated_at": now.isoformat(),
    }


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
    can reach one — a Cursor-only install is connected for builds, not for a step's model call.
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
