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


def repository_token(connection: Connection, settings: Any, project_id: str, token: str, *,
                     connection_id: Optional[str] = None, accept_database_fallback: bool = False,
                     actor: str = "ceo", http: Optional[Http] = None) -> dict[str, Any]:
    """Check a pasted token, store it, and list what it can see.

    A token GitHub refuses is never stored. Pasting again — or `Replace token` — re-points the
    one connection this project has, so its repository bindings and history stay (the old
    "already has a GitHub connection with this label" dead end cannot happen).
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
    existing = None
    if connection_id:
        existing = service.get_connection(connection, project_id, connection_id)
    else:
        existing = service._connection_by_label(connection, project_id, GITHUB_LABEL)
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
        current = service.create_connection(
            connection, project_id=project_id, connection_label=GITHUB_LABEL,
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
    return {"account_login": found["account_login"], "expires_at": found["expires_at"],
            "repos": found["repos"], "connection_id": current["id"], "rechecked": rechecked}


def connection_repos(connection: Connection, settings: Any, project_id: str,
                     connection_id: str, *, http: Optional[Http] = None) -> dict[str, Any]:
    """`+ Add repo`: the repositories the STORED token can see (no paste needed)."""
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
    return binding_id


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

    if result.get("state") == "paused_budget":
        # Not a check: the budget refused to spend on it. Nothing is recorded, so the row keeps
        # what it last knew and reads "Paused · budget" exactly while the budget says so.
        return
    last = {"ok": bool(result.get("ok")), "state": result.get("state") or "failed",
            "at": iso(now_utc()), "latency_ms": result.get("latency_ms"),
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
               http: Optional[Http]) -> dict[str, Any]:
    """One governed completion on a key or a local model."""
    from .openai_adapter import estimate_cost_usd_micros
    from .setup_service import PROVIDERS, PROOF_ESTIMATE_USD_MICROS, governed_proof, \
        model_key_details

    details = model_key_details(provider=provider, base_url=base_url, model=model)
    api = (PROVIDERS.get(provider) or {}).get("api") or "openai"
    local = provider in setup_view.LOCALS

    def prove() -> dict[str, Any]:
        result = setup_checks.complete_once(api=api, endpoint=details["endpoint"],
                                            api_key=api_key, model=details["model"], http=http)
        if result.get("ok"):
            usage = {**result.get("usage", {}), "provider": provider}
            result["cost_usd_micros"] = 0 if local else min(
                estimate_cost_usd_micros(usage), PROOF_ESTIMATE_USD_MICROS)
            result["replied"] = "OK"
        return result

    proof = governed_proof(_engine(connection), settings, project_id, f"setup-{provider}",
                           actor, prove=prove, amount=0 if local else None)
    if proof.get("budget_refused"):
        proof.update(ok=False, state="paused_budget")
    proof.setdefault("state", "working" if proof.get("ok") else "failed")
    proof["model"] = details["model"]
    proof["base_url"] = details["base_url"]
    return proof


def _subscription_proof(connection: Connection, settings: Any, project_id: str, provider: str,
                        actor: str, prove_with=None) -> dict[str, Any]:
    from .setup_service import governed_proof, prove_coding_agent

    started = time.monotonic()
    proof = governed_proof(_engine(connection), settings, project_id, provider, actor,
                           prove=prove_with or (lambda: prove_coding_agent(settings, provider)),
                           amount=0)
    proof["latency_ms"] = int((time.monotonic() - started) * 1000)
    if proof.get("ok"):
        proof["state"] = "working"
    elif proof.get("budget_refused"):
        proof["state"] = "paused_budget"
    else:
        proof["state"] = setup_checks.classify_model_failure(
            0 if "could not reach" in str(proof.get("error") or "").lower() else -1,
            str(proof.get("error") or ""), subscription=True)
        if proof["state"] == "unreachable":
            proof["state"] = "failed"
        if proof["state"] == "rate_limited":
            # The CLI names when the plan resets ("Try again at …"): the pill shows it, and
            # Auto knows when to come back.
            proof["retry_at"] = setup_checks._retry_after({}, str(proof.get("error") or ""))
    return proof


def _failure(provider: str, state: str, model: Optional[str] = None,
             host: Optional[str] = None) -> SetupActionError:
    from .setup_pills import LOGIN_FIX, DEFAULT_LOGIN_FIX, MODEL, pill

    state = state if state in MODEL else "failed"
    shown = pill("model", state, model=model or "That model", host=host or "the provider",
                 provider_fix=LOGIN_FIX.get(provider, DEFAULT_LOGIN_FIX), time="", age="")
    return SetupActionError(422, state, shown["label"], shown["tip"])


def connect_model(connection: Connection, settings: Any, project_id: str, *, provider: str,
                  kind: str, api_key: str = "", credential: str = "", credential_file: str = "",
                  base_url: str = "", model: str = "", accept_database_fallback: bool = False,
                  actor: str = "ceo", http: Optional[Http] = None, prove_with=None,
                  replace_source_id: str = "") -> dict[str, Any]:
    """Store a model credential and prove it with one governed question.

    A failed check keeps nothing, except a key whose provider was busy or unreachable — that
    key may well be right, and is kept with its pill saying so.
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
                                     prove_with=prove_with)
    return _connect_key(connection, settings, project_id, provider, api_key=api_key,
                        base_url=base_url, model=model, accept=accept_database_fallback,
                        actor=actor, http=http, replace_source_id=replace_source_id)


def _connect_subscription(connection: Connection, settings: Any, project_id: str, provider: str,
                          *, credential: str, accept: bool, actor: str,
                          prove_with=None) -> dict[str, Any]:
    from .setup_service import SetupError, connect_coding_agent, store_coding_agent_credential

    spec = _subscription_spec(settings, provider)
    if spec is None:
        raise _failure(provider, "not_on_server")
    stored_ref = None
    if credential.strip():
        require_store(connection, settings, project_id, accept, actor)
        # COMMITTED BEFORE THE PROOF. The proof turn runs the CLI, which reads the credential
        # from the store on its own connection; one held in this request's open transaction is
        # invisible to it. A credential that then fails its check is revoked below.
        try:
            with _engine(connection).begin() as own:
                stored = store_coding_agent_credential(
                    own, settings, project_id, provider, credential, actor=actor,
                    accept_database_fallback=True)
        except SetupError:
            # Said as the catalogue's pill (Login expired: "Make a new token with claude
            # setup-token."), never a sentence of its own; nothing about the value is repeated.
            raise _failure(provider, "login_expired") from None
        stored_ref = stored.get("ref_key")
    setup_view.forget_cached("cli")
    proofs: list[dict[str, Any]] = []

    def prove() -> dict[str, Any]:
        from .setup_service import prove_coding_agent

        result = (prove_with or (lambda: prove_coding_agent(settings, provider)))()
        proofs.append(result)
        return result

    started = time.monotonic()
    try:
        connect_coding_agent(connection, settings, project_id, provider, actor=actor,
                             prove=True, prove_with=prove)
    except SetupError as exc:
        _forget_credential(connection, project_id, stored_ref, actor)
        message = str(exc)
        lowered = message.lower()
        if "not installed" in lowered:
            state = "not_on_server"
        elif "budget" in lowered:
            state = "paused_budget"
        elif "rejected" in lowered or "not signed in" in lowered or "login" in lowered:
            state = "login_expired"
        else:
            last = proofs[-1] if proofs else {}
            state = setup_checks.classify_model_failure(
                -1, str(last.get("error") or message), subscription=True)
        raise _failure(provider, state) from None
    _record_model_check(connection, project_id, spec.entitlement_source_id or provider,
                        {"ok": True, "state": "working",
                         "latency_ms": int((time.monotonic() - started) * 1000),
                         "receipt_id": (proofs[-1] if proofs else {}).get("receipt_id")}, actor)
    return {"ok": True, "source_id": spec.entitlement_source_id or provider}


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


def _connect_key(connection: Connection, settings: Any, project_id: str, provider: str, *,
                 api_key: str, base_url: str, model: str, accept: bool, actor: str,
                 http: Optional[Http], replace_source_id: str = "") -> dict[str, Any]:
    from .entitlements import EntitlementBrokerService
    from .model_gateway import ModelGatewayService
    from .secret_backends import default_secret_backend
    from .setup_service import PROVIDERS, SetupError

    local = provider in setup_view.LOCALS
    api_key = (api_key or "").strip()
    if not local and not api_key:
        raise SetupActionError(422, "key_rejected", "API key", "Paste the provider's API key.")
    if provider == "ollama" and not base_url:
        found = setup_view.local_found("ollama")
        base_url = (found or {}).get("base_url") or ""
    if api_key:
        # Asked before the provider is called: a refusal sends nothing anywhere.
        require_store(connection, settings, project_id, accept, actor)
    try:
        proof = _key_proof(connection, settings, project_id, provider, api_key=api_key,
                           base_url=base_url, model=model, actor=actor, http=http)
    except SetupError as exc:
        raise SetupActionError(422, "unreachable", "Can't reach", _first_sentence(str(exc))) \
            from None
    keep = proof.get("ok") or proof.get("state") in ("rate_limited", "unreachable")
    if not keep:
        raise _failure(provider, str(proof.get("state") or "failed"), proof.get("model"),
                       proof.get("host"))
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
                                 "model": proof["model"]}},
        actor=actor)
    if api_key:
        ModelGatewayService(settings).upload_credential(
            connection, project_id, source_id, value=api_key,
            backend=default_secret_backend(settings), actor=actor)
    _record_model_check(connection, project_id, source_id, proof, actor)
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
                               actor=actor, http=http)
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
        try:
            proof = _key_proof(connection, settings, project_id, provider, api_key=key,
                               base_url=base, model=model, actor=actor, http=http)
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
    """Look for an Ollama on its usual address (part of `Test all`, never of a read)."""
    base = str(getattr(settings, "ollama_base_url", "") or "http://127.0.0.1:11434/v1")
    try:
        names = setup_checks.list_models(api="openai", base_url=base, local_ollama=True,
                                         http=http)
    except Exception:  # noqa: BLE001
        names = []
    if names:
        setup_view.remember_local("ollama", base, names)


# ── Who does what ─────────────────────────────────────────────────────────────────────────────

def set_job(connection: Connection, settings: Any, project_id: str, job: str,
            choice: Optional[dict[str, Any]], *, actor: str = "ceo") -> dict[str, Any]:
    """Write a job's engine and model together, or hand the job back to Auto. All or nothing:
    a choice the view does not offer is a 422 and nothing is written."""
    from . import project_settings
    from .core import emit_event
    from .model_routing import JOB_KNOBS, chat_model_knob

    if job not in JOB_KNOBS:
        raise SetupActionError(404, "failed", "Unknown job", f"One of: {', '.join(JOB_KNOBS)}.")
    engine_knob, model_knob = JOB_KNOBS[job]
    if not choice:
        project_settings.clear_one(connection, project_id, engine_knob)
        for knob in ([model_knob] if model_knob else
                     ["claude_cli_model", "codex_cli_model", "chat_model"]):
            project_settings.clear_one(connection, project_id, knob)
        emit_event(connection, project_id, "setup.job_set", actor, "job", job, {"auto": True})
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
                project_settings.clear_one(connection, project_id, key)
            else:
                project_settings.set_one(connection, project_id, key, value, actor)
    except ValueError as exc:
        raise SetupActionError(422, "failed", "Not offered", _first_sentence(str(exc))) from None
    emit_event(connection, project_id, "setup.job_set", actor, "job", job,
               {"engine": engine, "model": model})
    return {"job": job, "engine": engine, "model": model}


# ── Budget ────────────────────────────────────────────────────────────────────────────────────

def set_budget(connection: Connection, settings: Any, project_id: str, monthly_cap_usd: Any, *,
               actor: str = "ceo") -> None:
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
    emit_event(connection, project_id, "budget.monthly_cap_set", actor, "budget_policy",
               project_id, {"monthly_cap_usd_micros": micros, "via": "repo_and_models"})


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
    for kind, item in todo:
        if kind == "repository":
            check_repository(connection, settings, project_id, item, actor=actor, http=http)
        else:
            check_model(connection, settings, project_id, item, actor=actor, http=model_http,
                        prove_with=prove_with)


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
