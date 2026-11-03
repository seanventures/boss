"""WHICH ANTHROPIC CREDENTIAL A `claude` RUN USES, AND WHO PAYS FOR IT (BOSSAPP-530).

Every place BOSS starts the Claude CLI — the coding agent and its review seats, the guest VM
lane, research, the chat adapter, typed decisions — asks THIS module, so they cannot disagree.

There are three credentials, in this order by default ("subscription first"):

  (a) the Claude subscription's long-lived token (CLAUDE_CODE_OAUTH_TOKEN) — included
      capacity. BOSS obtains it itself: the console's Sign in runs `claude setup-token` on the
      server (subscription_sign_in) and stores what it prints (`store_long_lived_token`);
      (a') the Claude CLI's own login (KIND_CLI_LOGIN, ~8 h, refreshed by claude_login) — the
      fallback when no token is held;
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
#: WHERE THE STORED TOKEN CAME FROM (BOSSAPP-769 follow-up, 2026-10-27). The console's Sign in
#: runs `claude setup-token` on the BOSS server and stores the long-lived (1-year) token it
#: prints under TOKEN_NAME — the name every reader already uses, sandboxes included. Next to it,
#: in the same store, BOSS writes this marker: ORIGIN_BOSS_SETUP_TOKEN and the token's SHA-256
#: (never the token). A stored token whose hash matches is BOSS's own and is never "legacy";
#: anything else under TOKEN_NAME is the retired paste import (cli_harness.legacy_token_stored),
#: which Connect may still retire.
ORIGIN_NAME = "CLAUDE_CODE_OAUTH_TOKEN_ORIGIN"
ORIGIN_BOSS_SETUP_TOKEN = "boss_setup_token"
#: How long `claude setup-token` makes its token last when it does not say (2.1.x prints
#: "valid for 1 year").
LONG_LIVED_DAYS = 365

KIND_SUBSCRIPTION = "subscription"
KIND_STORED_KEY = "stored_key"
KIND_ENV_KEY = "env_key"
KIND_NONE = "none"
#: The Claude subscription signed in INSIDE BOSS (console Sign in / `boss connect claude`): the
#: CLI's own login in its config dir, shared by the API and worker. Nothing to inject — the
#: child must simply be given no API key, which would take precedence over the login.
KIND_CLI_LOGIN = "cli_login"
KINDS = (KIND_SUBSCRIPTION, KIND_CLI_LOGIN, KIND_STORED_KEY, KIND_ENV_KEY, KIND_NONE)


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
        credential: an inherited key next to a login would leave the CLI to choose who pays.

        On the CLI's own login this is also where the CLI is kept from refreshing it
        (claude_login.before_cli_start): a token about to expire is refreshed by BOSS first, a
        refresh in progress is waited out, and an expired login BOSS could not refresh raises
        ClaudeSignInNeeded instead of starting a CLI that would wipe it."""
        if self.kind == KIND_CLI_LOGIN:
            from .claude_login import before_cli_start

            before_cli_start()
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
        keep = value or bound in (KIND_NONE, KIND_CLI_LOGIN)
        return ClaudeCredential(bound if keep else KIND_NONE, value)

    order = order_of(settings, connection, project_id)
    disconnected = subscription_disconnected(connection, project_id)

    def subscription() -> str:
        return "" if disconnected else _subscription_token(settings, connection, project_id)

    def cli_login() -> str:
        return "" if disconnected or not cli_login_present() else _CLI_LOGIN_MARK

    steps = [(KIND_SUBSCRIPTION, subscription), (KIND_CLI_LOGIN, cli_login),
             (KIND_STORED_KEY, lambda: _stored_key(settings, connection, project_id)),
             (KIND_ENV_KEY, lambda: _env_key(settings))]
    if order == API_KEY_FIRST:
        steps = steps[2:] + steps[:2]
    for kind, read in steps:
        try:
            value = (read() or "").strip()
        except CredentialStoreUnavailable:
            env = _env_key(settings)
            if env:
                _register(env)
                return ClaudeCredential(KIND_ENV_KEY, env)
            raise
        if kind == KIND_CLI_LOGIN and value:
            return ClaudeCredential(KIND_CLI_LOGIN)
        if value:
            _register(value)
            return ClaudeCredential(kind, value)
    return ClaudeCredential(KIND_NONE)


_CLI_LOGIN_MARK = "cli-login"


#: A JOB'S OWN CLAUDE CREDENTIAL (boss-1, 2026-11-03). The operator wants "Claude Code ·
#: subscription" and "Claude Code · API key" side by side, one job on each — but the order above
#: is one answer for every claude BOSS starts. The choice rides on the job's MODEL value, the one
#: thing every route already carries to the launch: "sub:sonnet" runs sonnet on the subscription,
#: "key:opus" runs opus on the API key. `split_pick` strips it before `--model`; the launcher
#: resolves with that order instead of the global one. No prefix = the operator's order, as before.
PICK_SUBSCRIPTION, PICK_KEY = "sub", "key"
PICKS = (PICK_SUBSCRIPTION, PICK_KEY)


def split_pick(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """("sub" | "key" | None, the model name to run)."""
    raw = str(model or "").strip()
    head, sep, rest = raw.partition(":")
    if sep and head.lower() in PICKS:
        return head.lower(), (rest.strip() or None)
    return None, (raw or None)


def with_pick(pick: Optional[str], model: Optional[str]) -> Optional[str]:
    """The stored model value for a Claude choice: "sub:sonnet", "key:opus" (None model: the
    CLI's default on that credential, "sub:" / "key:")."""
    if pick not in PICKS:
        return model
    return f"{pick}:{model or ''}"


def resolve_for_pick(settings: Any, pick: Optional[str], *, connection: Any = None,
                     project_id: Optional[str] = None) -> ClaudeCredential:
    """The credential for one launch: the job's pick when it made one (that credential first,
    the other only when it is absent), else the operator's order (resolve_claude_credential).
    A pre-bound answer (`bind`) is set aside for a pick — it was bound in the global order."""
    if pick not in PICKS:
        return resolve_claude_credential(settings, connection=connection, project_id=project_id)
    order = SUBSCRIPTION_FIRST if pick == PICK_SUBSCRIPTION else API_KEY_FIRST
    picked = settings.model_copy(update={"claude_credential_kind": "", ORDER_KNOB: order})
    return resolve_claude_credential(picked)


def cli_login_present() -> bool:
    """Is the Claude CLI signed in with its OWN login in this process's config dir — the
    private, service-user-owned `.credentials.json` that `claude auth login` writes? A file
    check, not a CLI call: it runs on every resolution. Never raises; never reads a value."""
    import stat as _stat

    home = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "") or "~/.claude")
    path = os.path.join(home, ".credentials.json")
    try:
        info = os.stat(path)
    except OSError:
        return False
    return (_stat.S_ISREG(info.st_mode) and info.st_size > 2 and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0)


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
                "SELECT source_id, connection_status, credential_secret_ref_id, observed_capacity, "
                "status, updated_at "
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
            # Both can be offered at once: a job may pick either (split_pick).
            "has_subscription": bool(has_sub), "has_key": bool(has_stored or has_env),
            "billing": cred.billing if active != KIND_NONE else None,
            "source_id": (key_source_id(rows) if active == KIND_STORED_KEY
                          else source_for_kind(active)),
            "subscription_disconnected": disconnected}


def key_source_id(rows: dict[str, Any]) -> str:
    """THE SOURCE THAT HOLDS THE STORED KEY, which is where a call on it books its capacity.

    The key `_project_ref` resolves is bound to `anthropic_api` (the console's) or to a Repo and
    models `setup-anthropic[-n]` source. Booking every stored key on `anthropic_api` refused
    every governed call on an install whose key lives on `setup-anthropic` (the laptop,
    2026-10-24: a read-only investigation, `no_eligible_source` in 0.4 s, because
    `anthropic_api` was declared, not connected, with no runtime). Same order as `_project_ref`:
    `anthropic_api` when it holds a key, else the newest `setup-anthropic*` that does."""
    held = [(sid, r) for sid, r in rows.items()
            if sid != SUBSCRIPTION_SOURCE_ID and r.get("credential_secret_ref_id")
            and str(r.get("status") or "active") not in ("inactive", "revoked")]
    if any(sid == API_KEY_SOURCE_ID for sid, _ in held):
        return API_KEY_SOURCE_ID
    held.sort(key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
    return held[0][0] if held else API_KEY_SOURCE_ID


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
        # The origin marker goes with the token it describes.
        for name in (key, ref_key_for(SUBSCRIPTION_SECRET_SCOPE, ORIGIN_NAME)):
            try:
                backend_for(row["storage_backend"] or row["ref_kind"], settings).delete(name)
            except Exception as exc:  # noqa: BLE001 — revoked in BOSS either way
                logging.getLogger(__name__).warning(
                    "could not delete the stored %s from its store: %s", TOKEN_NAME,
                    type(exc).__name__)
    return len(rows)


def retire_legacy_token(connection: Any, settings: Any, *, actor: str) -> int:
    """Retire the token the retired setup-token import stored, EVERYWHERE it is stored.

    Called when the operator presses Connect after signing the CLI in inside BOSS: that login
    supersedes the imported token, but `claude` reads a stored CLAUDE_CODE_OAUTH_TOKEN ahead
    of its own login, so while the token is stored the new login can never be proved. The
    token is install-wide (looked up by name), so every project's reference goes, under both
    names it was written with; a per-project Disconnect from another project left it in place
    and the console's Connect refused forever. Returns how many references were revoked.
    Never logs a value.
    """
    import logging

    from sqlalchemy import text

    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .secret_backends import SecretNotFound, backend_for, pick_default_backend
    from .secrets_service import ref_key_for

    if boss_obtained_token_stored(settings):
        # A TOKEN BOSS OBTAINED ITSELF (`claude setup-token` run by the console's Sign in) is
        # the subscription, never a legacy import: Connect proves it and nothing retires it.
        return 0
    keys = (TOKEN_NAME, ref_key_for(SUBSCRIPTION_SECRET_SCOPE, TOKEN_NAME))
    rows = connection.execute(text(
        "UPDATE secret_refs SET status = 'revoked', revoked_at = now(), revoked_by = :a "
        "WHERE ref_key = ANY(:k) AND status <> 'revoked' "
        "RETURNING id, project_id, ref_key, storage_backend, ref_kind"),
        {"a": actor, "k": list(keys)}).mappings().all()
    stores = {str(row["storage_backend"] or row["ref_kind"]) for row in rows}
    try:
        stores.add(pick_default_backend(settings)[0])
    except Exception:  # noqa: BLE001 — the rows above are revoked either way
        pass
    for kind in stores:
        for key in keys:
            try:
                backend_for(kind, settings).delete(key)
            except SecretNotFound:
                continue
            except Exception as exc:  # noqa: BLE001 — revoked in BOSS either way
                logging.getLogger(__name__).warning(
                    "could not delete the stored %s from %s: %s", TOKEN_NAME, kind,
                    type(exc).__name__)
    return len(rows)


# ---- the long-lived token BOSS obtains itself (`claude setup-token` on the server) ----------

def token_fingerprint(value: str) -> str:
    """SHA-256 of a token: enough to tell whether the stored token is the one BOSS obtained,
    never enough to be it."""
    import hashlib

    return hashlib.sha256((value or "").strip().encode()).hexdigest()


def _origin_marker(value: str) -> str:
    return f"{ORIGIN_BOSS_SETUP_TOKEN}:{token_fingerprint(value)}"


def boss_obtained_token_stored(settings: Any) -> bool:
    """Is the token stored under TOKEN_NAME the one BOSS's own sign-in obtained? Reads the
    store only (an install's .env token is its own choice). Never raises; never returns or
    logs the value."""
    if settings is None:
        return False
    try:
        from .cli_harness import stored_credential

        token = stored_credential(settings, TOKEN_NAME, include_env=False) or ""
        marker = stored_credential(settings, ORIGIN_NAME, include_env=False) or ""
    except Exception:  # noqa: BLE001 — an unreadable store is "not BOSS's"
        return False
    return bool(token) and marker == _origin_marker(token)


def store_long_lived_token(connection: Any, settings: Any, project_id: str, token: str, *,
                           actor: str, valid_days: Optional[int] = None) -> dict[str, Any]:
    """Store the token `claude setup-token` printed on the BOSS server as THE subscription
    credential (kind subscription), and record that BOSS obtained it.

    Whatever the retired import left under TOKEN_NAME goes first (under the plain name it would
    shadow this one). The value goes to the secret store only; the reference keeps origin,
    obtained_at and expires_at — never the value or any part of it. Returns metadata only."""
    import json
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .core import emit_event
    from .secret_backends import backend_for
    from .secrets_service import ref_key_for
    from .setup_service import store_coding_agent_credential

    token = (token or "").strip()
    retired = 0 if boss_obtained_token_stored(settings) else \
        retire_legacy_token(connection, settings, actor=actor)
    stored = store_coding_agent_credential(connection, settings, project_id, "claude_code",
                                           token, actor=actor)
    backend_for(stored["backend"], settings).put(
        ref_key_for(SUBSCRIPTION_SECRET_SCOPE, ORIGIN_NAME), _origin_marker(token))
    now = datetime.now(timezone.utc)
    origin = {"origin": ORIGIN_BOSS_SETUP_TOKEN, "obtained_at": now.isoformat(),
              "expires_at": (now + timedelta(days=int(valid_days or LONG_LIVED_DAYS))).isoformat(),
              "last4": None}
    connection.execute(text(
        "UPDATE secret_refs SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS JSONB) "
        "WHERE project_id = :p AND ref_key = :k"),
        {"m": json.dumps(origin), "p": project_id, "k": stored["ref_key"]})
    emit_event(connection, project_id, "entitlement.subscription_token_obtained", actor,
               "entitlement_source", SUBSCRIPTION_SOURCE_ID,
               {"provider": "claude_code", "origin": ORIGIN_BOSS_SETUP_TOKEN,
                "expires_at": origin["expires_at"], "legacy_references_revoked": retired})
    return {"ok": True, "origin": ORIGIN_BOSS_SETUP_TOKEN, "expires_at": origin["expires_at"],
            "backend": stored["backend"]}


def sign_in_store(db_engine: Any, settings: Any, project_id: str, actor: str):
    """The `store` the server-side Claude sign-in hands the printed token to
    (subscription_sign_in.start): `store_long_lived_token` in its own transaction. A refusal
    in BOSS's own words (e.g. a database store whose key lives in the database) is shown as
    the sign-in's fix; anything else is named by type only."""
    def store(token: str, *, valid_days: Optional[int] = None) -> None:
        from ..db import begin
        from .setup_service import SetupError
        from .subscription_sign_in import StoreRefused

        try:
            with begin(db_engine) as connection:
                store_long_lived_token(connection, settings, project_id, token, actor=actor,
                                       valid_days=valid_days)
        except SetupError as exc:
            raise StoreRefused(str(exc)) from None
    return store


def long_lived_token_info(connection: Any, project_id: Optional[str]) -> Optional[dict]:
    """{origin, obtained_at, expires_at} of the long-lived token BOSS obtained for this project
    (or its org's model home), or None. Reads the reference's metadata, never the value."""
    if connection is None or not project_id:
        return None
    try:
        from sqlalchemy import text

        from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
        from .org_models import home_for
        from .secrets_service import ref_key_for

        with connection.begin_nested():
            home = home_for(connection, project_id) or project_id
            row = connection.execute(text(
                "SELECT metadata FROM secret_refs WHERE project_id IN (:p, :h) AND ref_key = :k "
                "AND status IN ('active', 'rotating', 'rotation_due') "
                "ORDER BY (project_id = :p) DESC, updated_at DESC NULLS LAST LIMIT 1"),
                {"p": project_id, "h": home,
                 "k": ref_key_for(SUBSCRIPTION_SECRET_SCOPE, TOKEN_NAME)}).first()
    except Exception:  # noqa: BLE001 — unknown is "none"
        return None
    meta = row[0] if row else None
    if isinstance(meta, str):
        import json

        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if not isinstance(meta, dict) or meta.get("origin") != ORIGIN_BOSS_SETUP_TOKEN:
        return None
    return {k: meta.get(k) for k in ("origin", "obtained_at", "expires_at")}


def credential_summary(connection: Any, project_id: Optional[str], *,
                       login: Optional[dict] = None) -> Optional[dict[str, Any]]:
    """What the Claude Code subscription row says it runs on, reading no secret:

    - {"kind": "long_lived_token", "label": "long-lived subscription token (renews Oct 27, 2027)"}
    - {"kind": "cli_login", "label": "CLI login (expires in 7 h)"}

    or None when neither is held. Same order as the resolution: the token wins."""
    from datetime import datetime

    info = long_lived_token_info(connection, project_id)
    if info is not None:
        label = "long-lived subscription token"
        try:
            expires = datetime.fromisoformat(str(info.get("expires_at") or ""))
            label += f" (renews {expires.strftime('%b')} {expires.day}, {expires.year})"
        except ValueError:
            pass
        return {"kind": "long_lived_token", "label": label,
                "expires_at": info.get("expires_at")}
    if login is None:
        if not cli_login_present():
            return None
        try:
            from . import claude_login

            login = claude_login.status()
        except Exception:  # noqa: BLE001
            login = {}
    if str((login or {}).get("state") or "") in ("", "absent"):
        return None
    left = (login or {}).get("seconds_left")
    if left is None:
        return {"kind": "cli_login", "label": "CLI login", "expires_at": None}
    left = max(0, int(left))
    when = (f"expires in {left // 3600} h" if left >= 3600 else
            f"expires in {left // 60} min" if left > 0 else "expired")
    return {"kind": "cli_login", "label": f"CLI login ({when})", "expires_at": None}
