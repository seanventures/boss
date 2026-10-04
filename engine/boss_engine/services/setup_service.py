"""Connecting models and coding agents: the credential stores, the proofs, the provider list.

Repo and models' state lives in `setup_view` (one server-assembled view, no network) and its
actions in `setup_actions` (one connect per object, one checks endpoint). This module keeps
what both build on: the provider catalogue, which engines a project has connected
(`connected_engines`), storing a subscription's credential, and `governed_proof` — one real
model turn, reserved against the budget and receipted, in every edition.
"""
from __future__ import annotations

import json
import re
from pathlib import Path as pathlib_Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings


#: The providers the first screen offers, with everything needed to reach them.
#:
#: A catalogue rather than a free-text form because the two things a new user gets wrong are the
#: base URL and the model name, and both are knowable. "custom" is here because the widest OSS
#: coverage is not a provider at all — it is anything that speaks the OpenAI API: LiteLLM,
#: Ollama, vLLM, Groq, Together, a proxy of your own.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api": "openai",
        "example_model": "anthropic/claude-sonnet-4.5",
        "key_url": "https://openrouter.ai/keys",
        "kind": "api_key",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api": "openai",
        "example_model": "gpt-4o",
        "key_url": "https://platform.openai.com/api-keys",
        "kind": "api_key",
    },
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api": "anthropic",
        "example_model": "claude-sonnet-4-5",
        "key_url": "https://console.anthropic.com/settings/keys",
        "kind": "api_key",
    },
    "gemini": {
        "label": "Google Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api": "openai",
        "example_model": "gemini-3.8-flash",
        "key_url": "https://aistudio.google.com/app/apikey",
        "kind": "api_key",
        "note": "Uses Google's OpenAI-compatible chat endpoint; provider-specific tools may differ.",
    },
    "xai": {
        "label": "xAI Grok API",
        "base_url": "https://api.x.ai/v1",
        "api": "openai",
        "example_model": "grok-4.7",
        "key_url": "https://console.x.ai/team/default/api-keys",
        "kind": "api_key",
    },
    # LOCAL MODELS. OpenAI-compatible servers on the operator's own hardware: no key, no
    # per-call money. `setup-ollama` / `setup-vllm` are the lanes' own sources.
    "ollama": {
        "label": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "api": "openai",
        "example_model": "",
        "key_url": "",
        "kind": "local",
    },
    "vllm": {
        "label": "Local model",
        "base_url": "",
        "api": "openai",
        "example_model": "",
        "key_url": "",
        "kind": "local",
    },
    "custom": {
        "label": "Anything OpenAI-compatible",
        "base_url": "",
        "api": "openai",
        "example_model": "",
        "key_url": "",
        "kind": "api_key",
        "note": "LiteLLM, Ollama, vLLM, Groq, Together, or your own proxy.",
    },
    # A KEY THAT IS NOT FOR BUILDING. Jev answers multiple-choice decisions and cannot write
    # code, so it must never become the model a build runs on — but this list is where an
    # operator looks for "where do I paste a key", and leaving it out sent them hunting. Listed
    # with its own kind. Its key proof and every decision spend only through the committed
    # reservation, lease, and receipt path (BOSS-451).
    "typesafe": {
        "label": "TypeSafe Jev — workflow decisions only",
        "api": "typesafe",
        "kind": "decision",
        "key_url": "https://console.typesafe.ai/keys",
        "note": "Answers the multiple-choice decisions in workflows, billed to your own TypeSafe "
                "account. BOSS checks the key with one tiny decision before saving it; that "
                "check, like every decision, is reserved against the budget and receipted. "
                "It does not write code.",
    },
    # SUBSCRIPTIONS ARE NOT KEYS, and BOSS already refuses to pretend otherwise: a subscription
    # source whose kind is an official CLI must be authenticated through a connection session
    # (see _requires_official_oauth). Listed here so the wizard can OFFER them and route to that
    # flow, rather than showing a key box that would be rejected on submit.
    "claude_code": {
        "label": "Claude Code subscription",
        "api": "subscription",
        "kind": "subscription",
        "source_kind": "official_coding_agent",
        "note": "Authorised through Claude Code's own login, not an API key.",
    },
    "codex": {
        "label": "Codex subscription",
        "api": "subscription",
        "kind": "subscription",
        "source_kind": "official_cli",
        "note": "Authorised through the Codex CLI's own login.",
    },
    "grok": {
        "label": "Grok Build subscription",
        "api": "subscription",
        "kind": "subscription",
        "source_kind": "official_cli",
        "note": "Authorised through the official Grok CLI's service-user device login.",
    },
    "cursor": {
        "label": "Cursor subscription",
        "api": "subscription",
        "kind": "subscription",
        "source_kind": "official_cli",
        "note": "Authorised through Cursor's own login.",
    },
}


class SetupError(RuntimeError):
    """Something the operator can fix, phrased for them."""


def coding_agent_live_proof_available(settings: Settings) -> bool:
    """Every edition proves a coding agent with one governed live turn before calling it
    connected (`governed_proof`: reserved and receipted before, settled after)."""
    return True



def _provider_endpoint(url_base: str, suffix: str) -> str:
    """urllib can also fetch file URLs; model probes require an HTTP host."""
    try:
        parsed = urlparse(url_base)
    except ValueError as exc:
        raise SetupError("the provider URL is malformed.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SetupError("the provider URL must be http or https and include a host.")
    return f"{url_base.rstrip('/')}{suffix}"


def provider_catalogue() -> list[dict[str, Any]]:
    """What the wizard shows, in the order it shows it."""
    return [{"provider": key, **value} for key, value in PROVIDERS.items()]


def model_key_details(*, provider: str, base_url: str = "", model: str = "") -> dict[str, str]:
    """Validate and normalize setup fields without contacting the provider."""
    spec = PROVIDERS.get(provider) or PROVIDERS["custom"]
    url_base = (base_url or spec.get("base_url") or "").strip().rstrip("/")
    if not url_base:
        raise SetupError("a base URL is required for a custom provider.")
    name = (model or spec.get("example_model") or "").strip()
    if not name:
        raise SetupError("a model name is required.")
    suffix = "/messages" if spec.get("api") == "anthropic" else "/chat/completions"
    return {"model": name, "endpoint": _provider_endpoint(url_base, suffix),
            "base_url": url_base}


def list_coding_agents(settings: Settings) -> list[dict[str, Any]]:
    """Every CLI coding agent BOSS can drive, and whether this machine can use it yet."""
    from .cli_harness import detect_all

    return detect_all(settings)


def connected_engines(
    connection: Connection, settings: Settings, project_id: str,
) -> dict[str, dict[str, Any]]:
    """The engines this project can actually run a role on, and why each one qualifies.

    "Looks like I could connect only one model in the UI.. I want multiple models and choose
    only those active models to be assigned in the role."

    The role pickers were offering every harness BOSS can DRIVE — eleven of them, on an
    install where exactly one was connected. That is not a choice, it is a quiz: ten of the
    options fail at build time with a missing credential, and nothing on screen says which.

    An engine qualifies here if this project has a credential or a connection for it:

      * a connected subscription (`entitlement_sources.connection_status = 'connected'`),
      * a stored API key (a source with a credential reference), or
      * a credential in this install's own environment — an install configured the old way is
        configured, and dropping those would take working engines off the menu.

    Returns {engine: {"why": ..., "source_id": ..., "saved_at": ...}}, keyed by harness name
    so the caller can intersect it with the registry without knowing about entitlements.
    """
    from .cli_harness import load_specs
    from .harness_registry import available_harnesses

    known = {h["name"] for h in available_harnesses()}

    # source_id -> engine. Asked of the specs rather than written down, so a harness added
    # later is not silently unofferable.
    by_entitlement = {spec.entitlement_source_id: spec.name
                      for spec in load_specs(settings).values()
                      if spec.entitlement_source_id}
    by_entitlement.setdefault("anthropic_claude_code", "claude_code")
    # What the API-key route records, `setup-<provider>`, mapped to the lane that spends it.
    # openai and "anything OpenAI-compatible" are both the engine-owned native agent; a local
    # model connected in Repo and models is `setup-ollama` or `setup-vllm`.
    by_api_provider = {"openai": "native", "custom": "native", "anthropic": "claude_code",
                       "openrouter": "openrouter", "groq": "groq", "deepseek": "deepseek",
                       "gemini": "gemini", "xai": "xai", "azure": "azure",
                       "ollama": "ollama", "vllm": "vllm"}

    found: dict[str, dict[str, Any]] = {}

    def offer(engine: str, why: str, **extra: Any) -> None:
        if engine in known and engine not in found:
            found[engine] = {"engine": engine, "why": why, **extra}

    rows = connection.execute(
        text("SELECT source_id, provider, connection_status, updated_at, "
             "       credential_secret_ref_id, observed_capacity, limit_shape, cost_mode, "
             "       connection_metadata "
             "FROM entitlement_sources WHERE project_id = :p "
             "ORDER BY updated_at DESC"),
        {"p": project_id},
    ).mappings().all()
    for row in rows:
        source_id = row["source_id"] or ""
        connected = row["connection_status"] == "connected"
        has_key = bool(row["credential_secret_ref_id"])
        if not connected and not has_key:
            continue          # a seeded catalogue row: nobody has connected this
        engine = by_entitlement.get(source_id) or (
            by_api_provider.get(source_id[len("setup-"):])
            if source_id.startswith("setup-") else None)
        if not engine:
            continue
        # CAPACITY TRAVELS WITH THE CONNECTION. An engine that is connected and out of
        # quota is not the same as one that is ready, and every caller that picks a lane —
        # the job pickers, the console's model rows, an operator deciding whether to wait
        # — needs to be able to tell them apart. Recorded by provider_limits from the one
        # place every engine reports through.
        from .provider_limits import is_available
        capacity = _json_object(row["observed_capacity"])
        metadata = _json_object(row["connection_metadata"])
        # WHAT KIND OF MONEY, AND WHETHER IT LAST WORKED — what `resolve_job`'s Auto ranks by
        # (a working subscription, then local, then a metered key). A source whose last check
        # or last call was refused stays listed, so a job chosen on it keeps its choice, but
        # Auto never picks it over one that works.
        local = source_id in ("setup-ollama", "setup-vllm")
        kind = ("subscription" if (connected and not has_key)
                or (row["cost_mode"] or "") == "subscription"
                else "local" if local else "metered")
        last = metadata.get("last_check") if isinstance(metadata.get("last_check"), dict) else {}
        broken = (capacity.get("credential_state") == "rejected"
                  or (last.get("ok") is False
                      and last.get("state") not in ("rate_limited", "unreachable")))
        offer(engine, "subscription" if connected else "api key",
              source_id=source_id, kind=kind, working=not broken,
              plan={"cost_mode": row["cost_mode"] or "",
                    "limits": row["limit_shape"] if isinstance(row["limit_shape"], dict) else {}},
              capacity={"available": is_available(capacity),
                        "exhausted": bool(capacity.get("exhausted")),
                        "retry_at": capacity.get("retry_at"),
                        "detail": capacity.get("detail") or ""},
              saved_at=(row["updated_at"].isoformat()
                        if hasattr(row["updated_at"], "isoformat")
                        else str(row["updated_at"] or "")) or None)

    # And the environment, for an install that was set up before any of this existed.
    for engine, entry in environment_engines(settings).items():
        if engine in known and engine not in found:
            found[engine] = entry
    return found


def environment_engines(settings: Settings) -> dict[str, dict[str, Any]]:
    """Engines this install's own settings file holds a credential for. Reads settings only, so
    it still answers when the database cannot."""
    found: dict[str, dict[str, Any]] = {}
    for engine, attr in ENV_LANES:
        if engine not in found and _set_deliberately(settings, attr):
            found[engine] = {"engine": engine, "why": "this install's environment",
                             "source_id": None, "saved_at": None, "kind": "env",
                             "working": True, "env_attr": attr}
    return found


#: Model credentials an install can carry in its environment, and the engine each one runs.
ENV_LANES: tuple[tuple[str, str], ...] = (
    ("native", "openai_api_key"), ("claude_code", "claude_code_oauth_token"),
    ("claude_code", "anthropic_api_key"), ("azure", "azure_openai_key"),
    ("openrouter", "openrouter_api_key"), ("groq", "groq_api_key"),
    ("deepseek", "deepseek_api_key"), ("gemini", "google_api_key"),
    ("xai", "xai_api_key"), ("vllm", "vllm_base_url"),
    ("ollama", "ollama_base_url"),
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _set_deliberately(settings: Settings, attr: str) -> bool:
    """Is this setting a value somebody CHOSE, rather than the one BOSS ships?

    Measured: `ollama_base_url` defaults to the local daemon's address, so "is it non-empty"
    reported Ollama as a connected model on an install with no Ollama running — and it went
    into the role pickers ahead of the models the operator had actually connected. A shipped
    default is not a connection.
    """
    value = str(getattr(settings, attr, "") or "").strip()
    if not value:
        return False
    fields = getattr(type(settings), "model_fields", {}) or {}
    field = fields.get(attr)
    default = getattr(field, "default", None) if field is not None else None
    return value != str(default or "").strip()


def build_engine_for(
    connection: Connection, settings: Settings, project_id: str,
) -> str:
    """Which harness WRITES THE CODE: `model_routing.resolve_job(..., "build")`.

    A thin wrapper since Repo and models gained one resolver for every job. The project's
    choice still comes first; an install with nothing connected keeps the engine it is
    configured with, so the build fails where it always did, naming that engine.
    """
    from .model_routing import resolve_job

    try:
        picked = resolve_job(connection, settings, project_id, "build")
    except Exception:  # noqa: BLE001 — a lookup must not decide the pipeline cannot run
        picked = None
    if picked is not None and picked.engine:
        return picked.engine
    from .project_settings import effective

    try:
        return (effective(connection, project_id, settings, "code_agent_engine")
                or "").strip().lower()
    except Exception:  # noqa: BLE001
        return (getattr(settings, "code_agent_engine", "") or "").strip().lower()


def chat_engine_for(
    connection: Connection, settings: Settings, project_id: str,
) -> str:
    """Which model should answer in the chat, meetings and agent panels.

    "I've configured the claude and codex right? why the agent did not work" — and the answer
    was:

        IntegrationConfigurationError: OPENAI_API_KEY is not configured

    True, and useless. The subscription the operator had just connected can hold a
    conversation perfectly well: the adapters for both CLIs already existed. What was missing
    was anything choosing one. `chat_llm_engine` defaults to empty, which inherits
    `llm_engine`, which defaults to "auto", which means the OpenAI adapter — so an install
    with two working subscriptions and no API key had a chat that could not answer.

    Order, most deliberate first:

      1. what this project chose in the console,
      2. what this deployment set in its environment,
      3. a connected subscription — Claude first, because it is the one with a headless token
         designed for it — but only when there is no API key, so an install that has always
         run on a key keeps running on it,
      4. nothing, which is the OpenAI adapter and its own error message.

    Returns the engine `model_routing.choose` picked, or "" when the deployment's own
    environment decides (or nothing can) — "leave the choice alone". The decision itself lives
    in model_routing, so this, readiness and the adapter cannot disagree.
    """
    from .model_routing import ENVIRONMENT, choose

    picked = choose(connection, settings, project_id)
    return "" if picked.why == ENVIRONMENT else picked.engine


def with_recorded_connections(
    agents: list[dict[str, Any]], connection: Connection, project_id: str,
) -> list[dict[str, Any]]:
    """Add what was RECORDED to what detection found. Mutates and returns the agents.

    Two different questions, and the wizard needs both answers. Detection says whether a build
    could run this minute; the record says whether anybody ever finished the step. Reporting
    only the first made a completed connection look undone — after a refresh or a redeploy the
    operator saw the same Connect button as on a fresh install, with nothing on screen saying
    the work had already been done, and reasonably concluded it had not been saved.
    """
    recorded: dict[str, dict[str, Any]] = {}
    if project_id:
        rows = connection.execute(
            # connection_status, NOT the row's existence. A project is SEEDED with a catalogue
            # row for every provider BOSS knows about, all of them 'not_connected' — so
            # "a row exists" would report every CLI as connected, including ones nobody has
            # ever signed in to.
            text("SELECT source_id, status, connection_status, auth_method, updated_at "
                 "FROM entitlement_sources "
                 "WHERE project_id = :p AND connection_status = 'connected'"),
            {"p": project_id},
        ).mappings().all()
        recorded = {r["source_id"]: dict(r) for r in rows}
    for agent in agents:
        row = recorded.get(agent.get("entitlement_source_id") or "")
        agent["connected"] = bool(row)
        agent["connected_at"] = str(row["updated_at"]) if row else None
        agent["connected_status"] = row["connection_status"] if row else None
    return agents


class SecretFallbackNotAccepted(SetupError):
    """The only store available is one the operator should be asked about first.

    Not a SetupError the caller can merely retry: nothing is wrong with the request, and
    repeating it verbatim will fail identically. What is missing is a decision.
    """

    def __init__(self, backend: str, message: str):
        super().__init__(message)
        self.backend = backend


def engine_login_states(connection, project_id: str) -> dict[str, str]:
    """{engine: "rejected"} for each coding agent whose last model call had its login rejected.

    A subscription whose stored credential Claude rejects stayed green "Connected" in Setup
    while every discovery stage failed with 401. Governed calls now record the outcome on the
    model source (model_failures.mark_login); this reads it back for the engines that use them.
    """
    from .cli_harness import BUILTIN_CLI_HARNESSES

    by_source = {spec.entitlement_source_id: name for name, spec in BUILTIN_CLI_HARNESSES.items()
                 if spec.entitlement_source_id}
    if not by_source:
        return {}
    rows = connection.execute(
        text("SELECT source_id, observed_capacity->>'credential_state' AS state "
             "FROM entitlement_sources WHERE project_id = :p AND source_id = ANY(:ids)"),
        {"p": project_id, "ids": list(by_source)},
    ).mappings().all()
    return {by_source[row["source_id"]]: row["state"] for row in rows
            if row["state"] == "rejected"}


def store_coding_agent_credential(
    connection, settings: Settings, project_id: str, provider: str, credential: str, *,
    actor: str = "ceo", accept_database_fallback: bool = False,
) -> dict[str, Any]:
    """Put a subscription's credential into whatever store THIS installation has.

    The point is that the customer needs no store of their own. `default_secret_backend` picks
    the best thing available — a managed store, a vault, the local encrypted file vault, and
    Postgres last — so an installation with nothing configured still works, and one with a
    vault uses it, without the operator choosing or knowing.

    This is deliberately NOT `upload_credential`. That route refuses subscription sources
    because it exists for pasted API keys, and that rule is worth keeping. What arrives here is
    not a pasted key: it is the credential the provider's own CLI minted through the official
    login that rule is protecting, so it gets its own door rather than a hole in that one.
    """
    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE, load_specs
    from .core import emit_event
    from .secret_backends import (
        database_key_is_external, default_secret_backend, operator_chosen_backend,
    )
    from .secrets_service import put_secret

    spec = load_specs(settings).get(provider)
    if spec is None:
        raise SetupError(
            f"no CLI coding agent called {provider!r}. Known: "
            f"{', '.join(sorted(load_specs(settings)))}.")
    key = spec.auth_file_secret or spec.token_env
    if not key:
        raise SetupError(
            f"{provider} cannot be used from a server — it has no credential that travels.")
    credential = (credential or "").strip()
    if not credential:
        raise SetupError("credential is required.")
    if spec.token_format and not re.match(spec.token_format, credential):
        # Never echo it, its length or any part of it: a wrong paste is often a password.
        raise SetupError(
            f"that is not a {provider} token — `claude setup-token` prints a long value that "
            "starts with sk-ant-. Copy that whole value and paste it again. Nothing was stored."
            if provider == "claude_code" else
            f"that is not a {provider} token. Nothing was stored.")
    if spec.auth_file_name.endswith(".json"):
        try:
            json.loads(credential)
        except json.JSONDecodeError:
            # Malformed is all that is safe to say; the text is a live credential.
            raise SetupError(f"the {provider} credential is not valid JSON.") from None

    # A Claude setup token is a long-lived credential for a whole subscription, so it never sits
    # in Postgres in a form a database backup could use. That was enforced as "local_vault
    # only" — a store no installer configures — and later as "never the database", which made
    # an install that CHOSE the database unable to connect Claude at all.
    #
    # What a backup can use is the deciding fact. The database store encrypts every value; the
    # question is where its key is. With BOSS_KMS_MASTER_KEY in .env (the installer writes one),
    # Postgres holds only ciphertext and a dump is not a key compromise, so the token may go
    # there. With the key provisioned INSIDE Postgres, a dump carries both — refused.
    backend = default_secret_backend(settings)
    if provider == "claude_code" and backend == "database" and not database_key_is_external():
        raise SetupError(
            "Claude setup-token import needs the database store's key to live outside the "
            "database (BOSS_KMS_MASTER_KEY in .env), and on this install it does not, so a "
            "database backup would carry a usable token. Re-run the installer — it writes the "
            "key — or install with --secret-store infisical. The token was not stored.")
    # ASK BEFORE FALLING BACK TO POSTGRES. `default_secret_backend` takes the best store the
    # installation has and quietly drops to `database` when it has none — which is right for
    # not blocking a first run, and wrong as a silent default for a live subscription
    # credential. The operator gets told once, in the words of this module's own comment: the
    # database is not a good place for a token.
    # Asked only when BOSS FELL BACK to the database. An operator who chose "database" at the
    # installer's prompt has already answered this question.
    if (provider != "claude_code" and backend == "database" and not accept_database_fallback
            and operator_chosen_backend() != "database"):
        raise SecretFallbackNotAccepted(
            backend,
            "this installation has no secret store configured, so the credential would go "
            "into BOSS's Postgres database. That works and survives restarts, but anyone with "
            "the database has the credential. Set up a secret store (see "
            "scripts/install/bootstrap_infisical.sh), or re-send with "
            "accept_database_fallback=true to store it there deliberately.")

    stored = put_secret(
        connection, settings, project_id=project_id, name=key, value=credential,
        secret_kind="api_token", backend=backend,
        scope_id=SUBSCRIPTION_SECRET_SCOPE, actor=actor,
    )
    # NAME THE PROVIDER ON THE REFERENCE. `put_secret` records the secret KIND in that column
    # ("api_token"), which is not what reads it: the runtime looks a lane's credential up by
    # provider — `resolve_project_credential(provider="anthropic")` for claude_code — so a
    # token stored without this is written successfully and then never found. The API-key
    # route already does the same fix-up for the same reason.
    if spec.entitlement_provider:
        connection.execute(
            text("UPDATE secret_refs SET provider = :provider WHERE id = :id"),
            {"provider": spec.entitlement_provider, "id": stored["secret_ref_id"]},
        )
    if backend == "database":
        # Recorded, not just permitted. "Who accepted a weaker store, and when" is a question
        # somebody asks after an incident, and an answer that depends on memory is no answer.
        emit_event(connection, project_id, "secret.fallback_accepted", actor,
                   "secret_ref", stored["secret_ref_id"],
                   {"backend": backend, "key": key, "provider": provider})
    # A new credential replaces whatever was rejected; Setup stops showing it as broken.
    from .model_failures import clear_login_state

    clear_login_state(connection, project_id, spec.entitlement_source_id)
    return {"ok": True, "provider": provider, "key": key,
            "backend": stored["backend"], "ref_key": stored["ref_key"],
            "fingerprint": stored["fingerprint"], "raw_secret_stored": False,
            "fallback_accepted": backend == "database"}


def prove_coding_agent(settings: Settings, provider: str, timeout: int = 90) -> dict[str, Any]:
    """Make the agent complete one real turn before calling it connected.

    The same rule as `verify_model_key`, for a thing that has no key to check. A subscription
    CLI reports "logged in" from a token on disk, which says the operator signed in once — not
    that the session still works, that the plan is active, or that this machine can reach the
    model. One trivial turn costs nothing against a subscription and turns "signed in" into
    "working".

    Run in an empty directory with the read-only sandbox where the CLI has one, so proving
    the connection can never edit anything.
    """
    import subprocess
    import tempfile

    from .cli_harness import CliHarnessUnavailable, GenericCliCodeAgent, load_specs

    spec = load_specs(settings).get(provider)
    if spec is None:
        raise SetupError(f"no CLI coding agent called {provider!r}.")
    agent = GenericCliCodeAgent(settings, spec)
    read_only = spec.supports_read_only()
    with tempfile.TemporaryDirectory(prefix="boss-agent-probe-") as work:
        try:
            out = agent.run_role(
                "Reply with exactly: BOSS_OK. Do not create or modify any file.",
                pathlib_Path(work), read_only=read_only,
            )
        except CliHarnessUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        except subprocess.SubprocessError as exc:  # noqa: PERF203 - narrow, reported not raised
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    text_out = (out.get("summary") or "")
    ok = out.get("exit_code") == 0
    return {
        "ok": ok,
        "exit_code": out.get("exit_code"),
        "read_only_probe": read_only,
        "replied": text_out[:200],
        "error": None if ok else (text_out[:300] or "the agent exited non-zero"),
    }


#: The most a connection proof may reserve; one trivial turn costs a fraction of this.
PROOF_ESTIMATE_USD_MICROS = 50_000


def governed_proof(engine, settings: Settings, project_id: str, provider: str, actor: str,
                   prove=None, *, amount: int = None) -> dict[str, Any]:  # type: ignore[assignment]
    """`prove_coding_agent`, spent only after its reservation and receipt commit.

    A proof is one real model turn. On its own committed connection, before the turn: a budget
    reservation of the proof's estimate and an in-progress
    receipt. After the turn: the receipt succeeds or fails and the reservation settles at the
    actual cost or is released. A refused budget makes no call. `prove` is injectable for tests.

    `amount` is what the proof reserves: the full estimate for a metered key, 0 for a
    subscription or a local model — a gate rather than a hold, so a flat-rate proof still passes
    while the budget is red and is refused only once spend is already over the cap.
    """
    import uuid

    from .budget import BudgetGovernor
    from .receipts import ReceiptService

    budget, receipts = BudgetGovernor(settings), ReceiptService()
    prove = prove or (lambda: prove_coding_agent(settings, provider))

    # The reservation and the in-progress receipt commit on their own connection BEFORE the
    # turn, in this function, so the same function visibly closes the receipt it opened.
    with engine.begin() as connection:
        # The full estimate: the per-minute cap measures actual spend, not reservations.
        amount = PROOF_ESTIMATE_USD_MICROS if amount is None else max(0, int(amount))
        reserved = budget.reserve(connection, project_id, "model", amount,
                                  f"Connection proof for {provider}", 15, risk_tier=1)
        refused = None
        if reserved.get("status") != "reserved":
            refused = reserved.get("message") or "Budget reservation denied."
        else:
            scope = connection.execute(
                text("SELECT w.org_id, p.workspace_id FROM projects p "
                     "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
                {"p": project_id}).mappings().one()
            receipt, _ = receipts.create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                f"setup-proof:{provider}:{uuid.uuid4().hex[:16]}", "model.connection_proof",
                {"project_id": project_id, "provider": provider},
                {"provider": provider, "actor": actor})
            spend = {"reservation_id": reserved["reservation_id"], "receipt_id": receipt["id"],
                     "amount": amount}
    if refused is not None:
        return {"ok": False, "error": refused, "budget_refused": True}
    try:
        proof = prove()
    except Exception as exc:  # noqa: BLE001 — recorded below, then reported
        proof = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    with engine.begin() as connection:
        if proof.get("ok"):
            receipts.mark_succeeded(connection, spend["receipt_id"], f"{provider}:proof",
                                    {"replied": str(proof.get("replied") or "")[:120]})
            budget.settle(connection, project_id, spend["reservation_id"], spend["receipt_id"],
                          "model", provider, spend["amount"],
                          int(proof.get("cost_usd_micros") or 0))
        else:
            receipts.mark_failed(connection, spend["receipt_id"],
                                 {"message": str(proof.get("error") or "")[:500]})
            budget.release(connection, project_id, spend["reservation_id"],
                           "Connection proof did not complete a turn.")
    proof["receipt_id"] = spend["receipt_id"]
    return proof


def connect_coding_agent(
    connection: Connection, settings: Settings, project_id: str, provider: str, *,
    actor: str = "ceo", prove: bool = True, prove_with=None,
) -> dict[str, Any]:
    """Import a coding-agent subscription the customer already has, into BOSS.

    There is nothing to paste. A Codex or Cursor subscription is an OAuth session held by that
    CLI, so configuration finds the CLI, confirms it is signed in, and records the entitlement.
    Paid editions also prove it answers before recording. Anything else is asking the operator to
    transcribe a credential that does not exist.

    Refuses rather than half-succeeds: not installed and signed-out are different problems with
    different next steps, and both are more useful than a stored setting that fails at build
    time an hour later.
    """
    from .cli_harness import GenericCliCodeAgent, detect, load_specs

    spec = load_specs(settings).get(provider)
    if spec is None:
        raise SetupError(
            f"no CLI coding agent called {provider!r}. Known: "
            f"{', '.join(sorted(load_specs(settings)))}.")
    # prove=false records a CLI-owned local session. It must not start a model turn.
    # A signed-out CLI still fails below.
    found = detect(spec)
    if not found["installed"]:
        raise SetupError(
            f"the {provider} CLI is not installed on this machine. "
            f"{found['next_step']}")
    if found["authenticated"] is False:
        if spec.require_local_session_file:
            raise SetupError(
                f"the {provider} CLI cannot use this service user's subscription. "
                f"{found['next_step']}")
        # TRY THE STORED CREDENTIAL BEFORE REFUSING. On a server the CLI is never signed in
        # by hand — nobody can open a browser there — so "not signed in" is the expected
        # starting state, not a verdict. Refusing here made the console's Connect button
        # permanently unusable on exactly the deployments it exists for, while telling the
        # operator to run `codex login` on a machine they cannot log into.
        restored = GenericCliCodeAgent(settings, spec).ensure_authorised()
        if not restored.get("authorised"):
            raise SetupError(
                f"the {provider} CLI is installed but not signed in. {found['next_step']} "
                f"({restored.get('reason') or 'no stored credential for this install'})")
        found = detect(spec)

    if not prove:
        proof = {"ok": True, "skipped": True}
    else:
        # EVERY EDITION, GOVERNED. One read-only turn, reserved (as a $0 gate: a subscription
        # turn costs no money) and receipted before it is sent.
        engine = getattr(connection, "engine", None)
        if engine is None:
            raise SetupError("the connection proof needs its own database connection.")
        proof = governed_proof(engine, settings, project_id, provider, actor,
                               prove=prove_with, amount=0)
    if not proof.get("ok"):
        from .model_failures import explain, is_login_rejected, mark_login

        if is_login_rejected(proof.get("error")):
            # Setup shows this subscription as broken until a working credential replaces it.
            # On ITS OWN connection: the refusal below rolls this request's transaction back,
            # and with it a mark written there — Setup kept showing the rejected login green.
            engine = getattr(connection, "engine", None)
            if engine is not None:
                with engine.begin() as own:
                    mark_login(own, project_id, spec.entitlement_source_id, rejected=True)
            said = explain(proof.get("error"), source_id=spec.entitlement_source_id)
            raise SetupError(f"{said['headline']} {said['action']} BOSS checked it with one "
                             "trivial turn, and it is not connected.")
        if proof.get("budget_refused"):
            raise SetupError(f"BOSS could not check the {provider} connection: the budget "
                             f"refused its one test turn ({proof.get('error')}). Try again "
                             "in a minute.")
        raise SetupError(
            f"the {provider} CLI is signed in but could not complete a turn: "
            f"{proof.get('error')}. Signed in is not the same as working — check the "
            f"subscription is active on this account.")

    record: dict[str, Any] = {"connected": True, "entitlement_recorded": False}
    if spec.entitlement_source_id:
        from .entitlements import WORKSPACE_OPERATOR_ID, EntitlementBrokerService

        payload = {
            "project_id": project_id,
            "provider": spec.entitlement_provider or provider,
            "source_id": spec.entitlement_source_id,
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            # THE WORKSPACE OPERATOR'S, whoever pressed Connect — the same owner the seed gives
            # every subscription. Stamping the requesting principal made it personally owned by
            # the console's own identity ("executive_console") on boss-1, and every discovery
            # and decision a member ("ceo") started was refused as pooling it.
            "owner_type": "workspace_operator",
            "owner_id": WORKSPACE_OPERATOR_ID,
            # A subscription seat is the operator's own. Saying so is what stops it being
            # handed to multi-tenant work it is not licensed for.
            "multi_tenant_allowed": False,
            "auth_method": "official_cli_oauth",
            "connection_status": "connected",
            "connection_ref": f"{spec.binary}:{found.get('version') or 'unknown'}",
            "cost_mode": "subscription",
            "runtime_supported": True,
            "observed_capacity": {"credential_present": True},
            "notes": f"Imported from the local {spec.binary} CLI. "
                     f"{found.get('auth_detail') or ''}".strip(),
        }
        try:
            EntitlementBrokerService(settings).upsert_source(
                connection, payload, actor=actor)
            record["entitlement_recorded"] = True
        except Exception as exc:  # noqa: BLE001 - the agent works either way; say what failed
            record["entitlement_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return {
        "ok": True,
        "provider": provider,
        "detected": found,
        "proof": proof,
        "harness": provider,
        **record,
    }


def connected_basics(connection: Connection, settings: Settings, project_id: str
                     ) -> dict[str, Any]:
    """Whether this project has a usable repository and model — read, never probed.

    Every workflow Start asks this, so it reads the same rows Repo and models renders
    (`setup_view.build_basics`) and the page's pills and Start's refusal cannot disagree.
    """
    from . import setup_view
    from .model_routing import choose

    return setup_view.basics(setup_view.build_basics(connection, settings, project_id),
                             router_runnable=choose(connection, settings, project_id).runnable)


def setup_status(connection: Connection, settings: Settings, project_id: str) -> dict[str, Any]:
    """DEPRECATED shape for `GET /v1/setup/status` (one release): the view, plus `steps`.

    Kept for the installer's checkup and older MCP clients; it reads `setup_view.build`, so it
    no longer calls GitHub on every load.
    """
    from . import setup_view

    view = setup_view.build(connection, settings, project_id)
    found = setup_view.basics(view)
    return {
        **view,
        "steps": [
            {"id": "github", "title": "Connect a repository", "done": found["github"],
             "repository": found["repository"], "href": "/?area=setup&focus=repository"},
            {"id": "model", "title": "Connect a model", "done": found["model"],
             "href": "/?area=setup&focus=models"},
        ],
    }



# --- The decision model --------------------------------------------------------------------- #
DECISION_KEY_NAME = "TYPESAFE_API_KEY"
#: TypeSafe's own model source (packages/specpack/models/providers.yaml).
TYPESAFE_SOURCE_ID = "typesafe_jev"


class DecisionModelGovernanceHold(SetupError):
    """Live key proof is a model call and must wait for durable spend control."""


def decision_model_status(connection, settings: Settings, project_id: str) -> dict[str, Any]:
    """Which backend decides on this project, and whether the Jev key it would need is held.

    Only the fact of a key, never the key: this answer goes to a browser. Decisions run on the
    governed path (BOSS-451): each provider attempt commits a budget reservation, a lease and
    a receipt before the call, so execution is available whenever a model is.
    """
    from . import project_settings
    from .typed_decisions import _typesafe_key, typesafe_decides

    effective = project_settings.applied(connection, project_id, settings)
    backend = (effective.decision_backend or "typesafe").strip().lower()
    has_key = bool(_typesafe_key(effective))
    deciding = "typesafe" if typesafe_decides(effective) else "claude"
    return {
        "backend": backend,
        "claude_model": effective.decision_model or "",
        "typesafe_key": has_key,
        "typesafe_model": effective.typesafe_model,
        "effective_backend": deciding,
        "configured_backend": deciding,
        "execution_available": True,
        "hold_reason": "",
        "worker_fallback": "the install's API model",
        "key_url": "https://console.typesafe.ai",
    }


#: The one decision a key proof asks. Tiny on purpose: a few hundred input tokens.
_KEY_PROOF_BINDING = {"typed_decision": {
    "question": "Is this a connection check?",
    "options": {"yes": "It is.", "no": "It is not."},
}}


def store_decision_key(
    connection, settings: Settings, project_id: str, api_key: str, *,
    actor: str = "setup", accept_database_fallback: bool = False,
) -> dict[str, Any]:
    """Prove a TypeSafe key with one governed decision, store it, and switch decisions to Jev.

    BOSS-451 step 5. The proof is a billed provider call, so it goes through the same path as
    a workflow decision: a budget reservation, a job and model lease, and an in-progress receipt
    commit on their own connections BEFORE TypeSafe is called, and the receipt is settled with
    the actual cost after. A failed key write or a crash after the call can no longer erase the
    evidence that the call was made — which is what the proof inside this request's transaction
    used to do.

    Where the key will be stored is settled FIRST, so nobody pays for a proof of a key that is
    then refused for having nowhere to go.
    """
    import uuid

    from . import project_settings
    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .core import emit_event
    from .secret_backends import default_secret_backend, operator_chosen_backend
    from .secrets_service import put_secret
    from .typed_decision_spend import (
        TypedDecisionReconciliationRequired,
        TypedDecisionSpend,
        TypedDecisionSpendError,
    )
    from .typed_decisions import GOVERNANCE_HOLD

    api_key = (api_key or "").strip()
    if not api_key:
        raise SetupError("paste the API key from console.typesafe.ai.")

    backend = default_secret_backend(settings)
    if (backend == "database" and not accept_database_fallback
            and operator_chosen_backend() != "database"):
        raise SecretFallbackNotAccepted(
            backend,
            "this installation has no secret store configured, so the key would go into BOSS's "
            "Postgres database. That works and survives restarts, but anyone with the database "
            "has the key. Set up a secret store, or re-send with accept_database_fallback=true "
            "to store it there deliberately.")

    engine = getattr(connection, "engine", None)
    if engine is None:
        # No way to commit the reservation and receipt apart from this request: no call.
        raise DecisionModelGovernanceHold(GOVERNANCE_HOLD)

    from .entitlements import EntitlementBrokerService
    from .model_gateway import ModelGatewayService

    gateway = ModelGatewayService(settings)

    def keep(conn, name: str) -> dict[str, Any]:
        """Store the key under `name` and make it the TypeSafe source's credential."""
        stored = put_secret(
            conn, settings, project_id=project_id, name=name, value=api_key,
            secret_kind="api_token", backend=backend,
            scope_id=SUBSCRIPTION_SECRET_SCOPE, actor=actor,
        )
        conn.execute(text("UPDATE secret_refs SET provider = 'typesafe' WHERE id = :id"),
                     {"id": stored["secret_ref_id"]})
        gateway.bind_credential_secret_ref(conn, project_id, TYPESAFE_SOURCE_ID,
                                           stored["secret_ref_id"], actor=actor)
        return stored

    # THE KEY IS HELD AS A CANDIDATE WHILE IT IS PROVED. The proof's capacity lease is booked
    # against TypeSafe's own source, and the gateway will only lease a paid source that has a
    # credential bound — so the candidate is bound, on its own committed connection, for the
    # length of the proof. A key that fails is revoked and the source goes back to exactly
    # what it held before: a bad paste never replaces a working key.
    with engine.begin() as held:
        # An install upgraded from before TypeSafe had a source gets it here.
        EntitlementBrokerService(settings).seed_default_sources(held, project_id, actor=actor)
        previous = held.execute(text(
            "SELECT credential_secret_ref_id, connection_ref, connection_status, status, "
            "runtime_supported, observed_capacity FROM entitlement_sources "
            "WHERE project_id = :p AND source_id = :s"),
            {"p": project_id, "s": TYPESAFE_SOURCE_ID}).mappings().first()
        candidate = keep(held, f"{DECISION_KEY_NAME}_CANDIDATE")

    def forget_candidate() -> None:
        with engine.begin() as held:
            held.execute(text(
                "UPDATE secret_refs SET status = 'revoked', revoked_at = now(), "
                "revoked_by = :a WHERE id = :id"),
                {"a": actor, "id": candidate["secret_ref_id"]})
            if previous is not None:
                held.execute(text(
                    "UPDATE entitlement_sources SET credential_secret_ref_id = :ref, "
                    "connection_ref = :cref, connection_status = :cs, status = :st, "
                    "runtime_supported = :rs, observed_capacity = CAST(:oc AS JSONB), "
                    "updated_at = now() WHERE project_id = :p AND source_id = :s"),
                    {"ref": previous["credential_secret_ref_id"],
                     "cref": previous["connection_ref"], "cs": previous["connection_status"],
                     "st": previous["status"], "rs": previous["runtime_supported"],
                     "oc": json.dumps(previous["observed_capacity"] or {}),
                     "p": project_id, "s": TYPESAFE_SOURCE_ID})

    proving = settings.model_copy(update={
        "decision_backend": "typesafe", "typesafe_api_key": api_key,
        # The proof is of THIS key. A Claude second opinion would prove nothing about it.
        "decision_fallback_below": 0.0,
    })
    spend = TypedDecisionSpend.for_runner(
        proving, engine=engine, project_id=project_id, run_id="setup:typesafe-key",
        step_id=f"key-proof-{uuid.uuid4().hex[:12]}", attempt=1, actor=actor,
        worker_id=f"setup:{actor}"[:120],
    )
    try:
        proof = spend.execute(proving, _KEY_PROOF_BINDING,
                              "BOSS is checking that this key works.", fallback=False)
    except TypedDecisionReconciliationRequired:
        forget_candidate()
        raise SetupError(
            "TypeSafe did not answer in time. BOSS kept a record of the attempt so it can be "
            "reconciled rather than charged twice; try again in a minute.") from None
    except TypedDecisionSpendError as exc:
        forget_candidate()
        message = str(exc)
        if " 401" in message:
            raise SetupError("TypeSafe rejected that key — check it was copied whole.") from None
        if "fund" in message.lower() or " 402" in message:
            raise SetupError("the key is valid but the TypeSafe account has no credit. Add "
                             "funds at console.typesafe.ai and try again.") from None
        raise SetupError(f"could not check that key: {message[:200]}") from None
    except BaseException:
        forget_candidate()
        raise

    stored = keep(connection, DECISION_KEY_NAME)
    connection.execute(text(
        "UPDATE secret_refs SET status = 'revoked', revoked_at = now(), revoked_by = :a "
        "WHERE id = :id"), {"a": actor, "id": candidate["secret_ref_id"]})
    project_settings.set_one(connection, project_id, "decision_backend", "typesafe", actor=actor)
    emit_event(connection, project_id, "setup.decision_model_connected", actor,
               "secret_ref", stored["secret_ref_id"],
               {"backend": backend, "model": proof.get("model"),
                "latency_ms": proof.get("latency_ms"), "receipt_id": proof.get("receipt_id")})
    return {"ok": True, "backend": "typesafe", "model": proof.get("model"),
            "latency_ms": proof.get("latency_ms"), "store": stored["backend"],
            "fingerprint": stored["fingerprint"], "raw_secret_stored": False,
            "receipt_id": proof.get("receipt_id"),
            "cost_usd_micros": proof.get("cost_usd_micros"),
            "fallback_accepted": backend == "database"}
