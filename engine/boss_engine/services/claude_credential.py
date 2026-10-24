"""WHICH ANTHROPIC CREDENTIAL A `claude` RUN USES, AND WHO PAYS FOR IT (BOSSAPP-530).

Every place BOSS starts the Claude CLI — the coding agent and its review seats, the guest VM
lane, research, the chat adapter, typed decisions — asks THIS module, so they cannot disagree.

There are three credentials, in this order by default ("subscription first"):

  (a) the Claude subscription's login (CLAUDE_CODE_OAUTH_TOKEN) — included capacity;
  (b) the Anthropic API key the operator stored in the console (Repo and models -> Anthropic
      API -> Store credential), read from the secret store exactly as the login is;
  (c) ANTHROPIC_API_KEY from this install's own environment.

(b) and (c) are metered: every call on them is money on the operator's Anthropic API account,
and is booked as such (`billing` = BILLING_METERED), so budgets and run reports show real cost.

The operator may turn the order round ("API key first") with the project knob
`anthropic_credential_order` or BOSS_ANTHROPIC_CREDENTIAL_ORDER. Disconnecting the subscription
in the console makes (a) unavailable — its stored login is forgotten and the source is marked
revoked — so runs fall through to the key with nothing else to change.

The value never leaves this module except into a child process's environment: it is registered
with the log redactor the moment it is read, `repr` never shows it, and nothing here logs it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .included_spend import BILLING_METERED, BILLING_SUBSCRIPTION

SUBSCRIPTION_FIRST = "subscription_first"
API_KEY_FIRST = "api_key_first"
ORDERS = (SUBSCRIPTION_FIRST, API_KEY_FIRST)
#: The project knob (project_settings.KNOBS) and the Settings attribute it overrides.
ORDER_KNOB = "anthropic_credential_order"

SUBSCRIPTION_SOURCE_ID = "anthropic_claude_code"
API_KEY_SOURCE_ID = "anthropic_api"
#: The name the console's Anthropic API key is stored under, in the same install-wide scope as
#: the subscription login (cli_harness.SUBSCRIPTION_SECRET_SCOPE), so a process with no database
#: connection — the sandboxed agent, the chat adapter — reads it the same way it reads the login.
STORED_KEY_NAME = "ANTHROPIC_API_KEY"
TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"

KIND_SUBSCRIPTION = "subscription"
KIND_STORED_KEY = "stored_key"
KIND_ENV_KEY = "env_key"
KIND_NONE = "none"
KINDS = (KIND_SUBSCRIPTION, KIND_STORED_KEY, KIND_ENV_KEY, KIND_NONE)


@dataclass(frozen=True)
class ClaudeCredential:
    """One resolved credential. `value` is excluded from repr/eq so it never prints."""

    kind: str
    value: str = field(default="", repr=False, compare=False)

    @property
    def is_key(self) -> bool:
        return self.kind in (KIND_STORED_KEY, KIND_ENV_KEY)

    @property
    def billing(self) -> str:
        """Who pays: the subscription for its login, the operator's API account for a key."""
        return BILLING_METERED if self.is_key else BILLING_SUBSCRIPTION

    @property
    def env_name(self) -> str:
        if self.is_key:
            return STORED_KEY_NAME
        return TOKEN_NAME if self.kind == KIND_SUBSCRIPTION else ""

    def apply(self, env: dict) -> dict:
        """Put exactly this credential in a CLI child's environment, and no other Anthropic
        credential: an inherited key next to a login would leave the CLI to choose who pays."""
        env.pop(TOKEN_NAME, None)
        env.pop(STORED_KEY_NAME, None)
        if self.value and self.env_name:
            env[self.env_name] = self.value
        return env

    def guest_fields(self) -> dict:
        """The guest_spec fields a micro-VM claude_code run carries (sandbox_contract)."""
        if self.is_key and self.value:
            return {"anthropic_api_key": self.value}
        if self.kind == KIND_SUBSCRIPTION and self.value:
            return {"claude_oauth_token": self.value}
        return {}

    def bind(self, settings: Any) -> Any:
        """`settings` carrying this resolution, so a process that has no connection later (the
        agent's own child, the chat adapter) uses it without looking again."""
        return settings.model_copy(update={
            "claude_credential_kind": self.kind,
            "claude_code_oauth_token": self.value if self.kind == KIND_SUBSCRIPTION else "",
            "claude_bound_api_key": self.value if self.is_key else "",
        })


def _register(value: str) -> None:
    try:
        from ..identity.redaction import register_secrets

        register_secrets([value])
    except Exception:  # noqa: BLE001 — redaction must never stop a run
        pass


def order_of(settings: Any, connection: Any = None, project_id: Optional[str] = None) -> str:
    """The operator's order: the project's knob, else the install's setting, else subscription
    first. Never raises."""
    value = None
    if connection is not None and project_id:
        try:
            from . import project_settings

            value = project_settings.effective(connection, project_id, settings, ORDER_KNOB)
        except Exception:  # noqa: BLE001
            value = None
    value = str(value or getattr(settings, ORDER_KNOB, "") or "").strip().lower()
    return value if value in ORDERS else SUBSCRIPTION_FIRST


def subscription_disconnected(connection: Any, project_id: Optional[str]) -> bool:
    """Whether the operator disconnected the Claude subscription for this project."""
    if connection is None or not project_id:
        return False
    try:
        from sqlalchemy import text

        status = connection.execute(
            text("SELECT connection_status FROM entitlement_sources "
                 "WHERE project_id = :p AND source_id = :s"),
            {"p": project_id, "s": SUBSCRIPTION_SOURCE_ID}).scalar()
    except Exception:  # noqa: BLE001 — unknown is not "disconnected"
        return False
    return str(status or "") == "revoked"


def _env_key(settings: Any) -> str:
    return (str(getattr(settings, "anthropic_api_key", "") or "").strip()
            or (os.environ.get(STORED_KEY_NAME) or "").strip())


def _store_lookup(settings: Any, name: str) -> str:
    """The install's secret store only (never the environment); raises
    CredentialStoreUnavailable when the store did not answer."""
    from .cli_harness import stored_credential

    return stored_credential(settings, name, strict=True, include_env=False) or ""


def _subscription_token(settings: Any, connection: Any, project_id: Optional[str]) -> str:
    token = (str(getattr(settings, "claude_code_oauth_token", "") or "").strip()
             or (os.environ.get(TOKEN_NAME) or "").strip())
    if token:
        return token
    if connection is not None and project_id:
        ref = _project_ref(connection, project_id, token=True)
        if ref:
            return _resolve_ref(connection, settings, ref, project_id)
    return _store_lookup(settings, TOKEN_NAME)


def _project_ref(connection: Any, project_id: str, *, token: bool) -> Optional[str]:
    """The newest usable SecretRef: the subscription's stored login (`token`), or the key bound
    to an Anthropic API-key source — the console's `anthropic_api` or a Repo-and-models
    `setup-anthropic[-n]` key."""
    from sqlalchemy import text

    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .secrets_service import ref_key_for

    if token:
        # This project's own login first, else the org's: the login stored in the org's model
        # home (org_models), which every project of the org uses.
        from .org_models import home_for

        home = home_for(connection, project_id) or project_id
        row = connection.execute(text(
            "SELECT id FROM secret_refs WHERE project_id IN (:p, :h) AND ref_key = :k "
            "AND status IN ('active', 'rotating', 'rotation_due') "
            "ORDER BY (project_id = :p) DESC, updated_at DESC NULLS LAST, id LIMIT 1"),
            {"p": project_id, "h": home,
             "k": ref_key_for(SUBSCRIPTION_SECRET_SCOPE, TOKEN_NAME)}).first()
        return row[0] if row else None
    row = connection.execute(text(
        "SELECT es.credential_secret_ref_id FROM entitlement_sources es "
        "JOIN secret_refs sr ON sr.id = es.credential_secret_ref_id "
        "WHERE es.project_id = :p AND (es.source_id = :api OR es.source_id LIKE 'setup-anthropic%') "
        "AND sr.status IN ('active', 'rotating', 'rotation_due') "
        "ORDER BY (es.source_id = :api) DESC, es.updated_at DESC NULLS LAST LIMIT 1"),
        {"p": project_id, "api": API_KEY_SOURCE_ID}).first()
    return row[0] if row else None


def _resolve_ref(connection: Any, settings: Any, ref: str, project_id: str) -> str:
    from .cli_harness import CredentialStoreUnavailable
    from .secret_backends import SecretUnavailable, resolve_secret_ref

    try:
        return (resolve_secret_ref(connection, ref, settings) or "").strip()
    except SecretUnavailable as exc:
        raise CredentialStoreUnavailable(
            "Anthropic credential", type(exc).__name__,
            transient=bool(getattr(exc, "transient", True))) from None


def _stored_key(settings: Any, connection: Any, project_id: Optional[str]) -> str:
    if connection is not None and project_id:
        ref = _project_ref(connection, project_id, token=False)
        if ref:
            return _resolve_ref(connection, settings, ref, project_id)
    return _store_lookup(settings, STORED_KEY_NAME)


def resolve_claude_credential(settings: Any, *, connection: Any = None,
                              project_id: Optional[str] = None) -> ClaudeCredential:
    """THE ONE RESOLUTION. See the module note for the order.

    Settings that already carry a resolution (`ClaudeCredential.bind`) are answered from it.

    A secret store that did not answer is never read as "nothing stored": the walk stops there,
    and only the install's own ANTHROPIC_API_KEY may still answer (as it always could); without
    one, `CredentialStoreUnavailable` is raised and every surface explains it as a wait.
    """
    from .cli_harness import CredentialStoreUnavailable

    bound = str(getattr(settings, "claude_credential_kind", "") or "").strip()
    if bound in KINDS:
        if bound == KIND_SUBSCRIPTION:
            value = str(getattr(settings, "claude_code_oauth_token", "") or "").strip()
        elif bound in (KIND_STORED_KEY, KIND_ENV_KEY):
            value = str(getattr(settings, "claude_bound_api_key", "") or "").strip()
        else:
            value = ""
        if value:
            _register(value)
        return ClaudeCredential(bound if value or bound == KIND_NONE else KIND_NONE, value)

    order = order_of(settings, connection, project_id)
    disconnected = subscription_disconnected(connection, project_id)

    def subscription() -> str:
        return "" if disconnected else _subscription_token(settings, connection, project_id)

    steps = [(KIND_SUBSCRIPTION, subscription),
             (KIND_STORED_KEY, lambda: _stored_key(settings, connection, project_id)),
             (KIND_ENV_KEY, lambda: _env_key(settings))]
    if order == API_KEY_FIRST:
        steps = steps[1:] + steps[:1]
    for kind, read in steps:
        try:
            value = (read() or "").strip()
        except CredentialStoreUnavailable:
            env = _env_key(settings)
            if env:
                _register(env)
                return ClaudeCredential(KIND_ENV_KEY, env)
            raise
        if value:
            _register(value)
            return ClaudeCredential(kind, value)
    return ClaudeCredential(KIND_NONE)


def bind_claude_credential(settings: Any, *, connection: Any = None,
                           project_id: Optional[str] = None) -> Any:
    """`settings` with the resolved credential bound (code_agent._bind_lane_credentials).

    A secret store that did not answer is not bound as an answer: the settings go unbound, and
    the `claude` child resolves again when it starts — raising there, as it always did, if the
    store is still down."""
    from .cli_harness import CredentialStoreUnavailable

    try:
        cred = resolve_claude_credential(settings, connection=connection, project_id=project_id)
    except CredentialStoreUnavailable:
        return settings
    return cred.bind(settings)


def resolve_anthropic_api_key(settings: Any, *, connection: Any = None,
                              project_id: Optional[str] = None) -> ClaudeCredential:
    """THE ANTHROPIC API KEY ONLY — for an engine that must never run on the subscription login
    (claude_agent_sdk: Agent SDK usage is billed to the operator's API account, which is what
    the operator's API credits cover; the subscription does not).

    The console's stored key, else ANTHROPIC_API_KEY from this install's environment. The
    operator's order and a disconnected subscription do not matter here: the login is never an
    answer. Settings bound by `bind_anthropic_api_key` (or by `bind_claude_credential` to a key)
    are answered from the binding. Returns KIND_NONE when no key is held; raises
    CredentialStoreUnavailable when the store did not answer and the environment has no key.
    """
    from .cli_harness import CredentialStoreUnavailable

    bound = str(getattr(settings, "claude_credential_kind", "") or "").strip()
    if bound in (KIND_STORED_KEY, KIND_ENV_KEY):
        value = str(getattr(settings, "claude_bound_api_key", "") or "").strip()
        if value:
            _register(value)
            return ClaudeCredential(bound, value)
    try:
        value = (_stored_key(settings, connection, project_id) or "").strip()
    except CredentialStoreUnavailable:
        value = ""
        if not _env_key(settings):
            raise
    if value:
        _register(value)
        return ClaudeCredential(KIND_STORED_KEY, value)
    env = _env_key(settings)
    if env:
        _register(env)
        return ClaudeCredential(KIND_ENV_KEY, env)
    return ClaudeCredential(KIND_NONE)


def bind_anthropic_api_key(settings: Any, *, connection: Any = None,
                           project_id: Optional[str] = None) -> Any:
    """`settings` with the Anthropic API key bound (never the login); unbound when the store
    did not answer, so the engine resolves again — and fails closed — when it starts."""
    from .cli_harness import CredentialStoreUnavailable

    try:
        cred = resolve_anthropic_api_key(settings, connection=connection, project_id=project_id)
    except CredentialStoreUnavailable:
        return settings
    if not cred.is_key:
        return settings
    return cred.bind(settings)


def describe(connection: Any, settings: Any, project_id: str) -> dict[str, Any]:
    """Which credential is active for this project, WITHOUT reading any secret value — for the
    console (entitlement source rows). Same order as `resolve_claude_credential`."""
    from sqlalchemy import text

    rows = {}
    try:
        for r in connection.execute(text(
                "SELECT source_id, connection_status, credential_secret_ref_id, observed_capacity "
                "FROM entitlement_sources WHERE project_id = :p "
                "AND (source_id IN (:sub, :api) OR source_id LIKE 'setup-anthropic%')"),
                {"p": project_id, "sub": SUBSCRIPTION_SOURCE_ID, "api": API_KEY_SOURCE_ID}
        ).mappings().all():
            rows[r["source_id"]] = r
    except Exception:  # noqa: BLE001
        rows = {}
    sub = rows.get(SUBSCRIPTION_SOURCE_ID) or {}
    observed = sub.get("observed_capacity") or {}
    disconnected = str(sub.get("connection_status") or "") == "revoked"
    has_sub = not disconnected and bool(
        str(sub.get("connection_status") or "") == "connected"
        or (isinstance(observed, dict) and observed.get("official_oauth_connected"))
        or str(getattr(settings, "claude_code_oauth_token", "") or "").strip()
        or (os.environ.get(TOKEN_NAME) or "").strip())
    has_stored = any(r.get("credential_secret_ref_id") for sid, r in rows.items()
                     if sid != SUBSCRIPTION_SOURCE_ID)
    has_env = bool(_env_key(settings))
    order = order_of(settings, connection, project_id)
    present = [(KIND_SUBSCRIPTION, has_sub), (KIND_STORED_KEY, has_stored),
               (KIND_ENV_KEY, has_env)]
    if order == API_KEY_FIRST:
        present = present[1:] + present[:1]
    active = next((k for k, ok in present if ok), KIND_NONE)
    cred = ClaudeCredential(active)
    return {"active": active, "order": order,
            "billing": cred.billing if active != KIND_NONE else None,
            "source_id": source_for_kind(active),
            "subscription_disconnected": disconnected}


def source_for_kind(kind: str) -> str:
    """The entitlement source a `claude` call on this credential books its capacity against:
    the subscription for its login, the Anthropic API source for a key."""
    return API_KEY_SOURCE_ID if kind in (KIND_STORED_KEY, KIND_ENV_KEY) else SUBSCRIPTION_SOURCE_ID


def active_source_id(connection: Any, settings: Any, project_id: Optional[str]) -> str:
    """`source_for_kind` of this project's active credential (no secret is read). Never raises:
    an unreadable state books against the subscription, as before."""
    if connection is None or not project_id:
        return SUBSCRIPTION_SOURCE_ID
    try:
        return describe(connection, settings, project_id)["source_id"]
    except Exception:  # noqa: BLE001
        return SUBSCRIPTION_SOURCE_ID


def forget_subscription_login(connection: Any, settings: Any, project_id: str, *,
                              actor: str) -> int:
    """Disconnecting the Claude subscription FORGETS the login BOSS stored for it.

    Marking the source revoked is enough for every run that resolves with a database connection
    (`subscription_disconnected`); a process without one — the chat adapter, a sandboxed agent —
    reads the store by name, so the stored value itself goes. Runs then fall through to the
    Anthropic API key. A login set in this install's environment cannot be removed from here;
    connection-aware runs still skip it. Returns how many references were revoked. Never logs a
    value; a store that cannot delete is logged by name and type only.
    """
    import logging

    from sqlalchemy import text

    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .secret_backends import backend_for
    from .secrets_service import ref_key_for

    key = ref_key_for(SUBSCRIPTION_SECRET_SCOPE, TOKEN_NAME)
    rows = connection.execute(text(
        "UPDATE secret_refs SET status = 'revoked', revoked_at = now(), revoked_by = :a "
        "WHERE project_id = :p AND ref_key = :k AND status <> 'revoked' "
        "RETURNING id, storage_backend, ref_kind"),
        {"a": actor, "p": project_id, "k": key}).mappings().all()
    for row in rows:
        try:
            backend_for(row["storage_backend"] or row["ref_kind"], settings).delete(key)
        except Exception as exc:  # noqa: BLE001 — revoked in BOSS either way
            logging.getLogger(__name__).warning(
                "could not delete the stored %s from its store: %s", TOKEN_NAME,
                type(exc).__name__)
    return len(rows)
