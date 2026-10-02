"""First-run setup: connect a repository, connect a model, and say what is still missing.

WHY THIS EXISTS AS ITS OWN THING. Every piece below already had an endpoint — GitHub
connections, entitlement sources, credential upload, connection tests. What did not exist was an
answer to "I just installed this, what do I do?", and the honest answer was: set two environment
variables on the API container, restart it, then call four endpoints in the right order with the
right ids. That is not onboarding. For open source it is where the evaluation ends.

So this module owns the ORDER and the VERDICT: it knows which steps configure an install and
names precisely what is missing. Community stores model keys as unverified until a governed
model run can prove them. Nothing here is a new capability; it is the sequence somebody would
otherwise have to learn from the source.
"""
from __future__ import annotations

import json
import re
from pathlib import Path as pathlib_Path
import urllib.error
import urllib.request
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


_PAID_EDITIONS = frozenset({
    "cloud", "enterprise", "enterprise_cloud", "enterprise_self_hosted",
})
_LIVE_MODEL_PROBE_DENIAL = (
    "Live model key verification is paused in Community until it has a budget reservation, "
    "leased job, and cost receipt. The key can be saved without a live test."
)


def coding_agent_live_proof_available(settings: Settings) -> bool:
    """Every edition proves a coding agent with one live turn before calling it connected.

    Community used to skip the proof because the turn was not reserved or receipted — so a
    wrong Claude credential (on boss-1, something other than the setup token had been pasted)
    was "connected" and surfaced hours later as "401 Invalid bearer token" inside a workflow.
    Community now runs the proof through `governed_proof`: budget reserved and receipt opened,
    both committed, before the turn; settled at its actual cost after.
    """
    return True


def _edition_is_paid(settings: Settings) -> bool:
    edition = str(getattr(settings, "boss_edition", "community") or "community")
    return edition.strip().lower() in _PAID_EDITIONS


def live_model_probe_allowed(settings: Settings) -> bool:
    """Unknown editions fail closed for the spendy provider request."""
    edition = str(getattr(settings, "boss_edition", "community") or "community")
    return edition.strip().lower() in _PAID_EDITIONS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep a provider probe at the URL the authorized operator supplied."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


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


def verify_model_key(
    *, settings: Settings, provider: str, api_key: str, base_url: str = "", model: str = "",
    timeout: int = 20,
) -> dict[str, Any]:
    """Spend one token proving the key works in editions allowed to use this probe.

    A wizard that stores a key and reports success has verified that the operator can type. The
    first real evidence otherwise arrives when a build fails an hour later, by which point the
    key is the last thing anyone suspects. One tiny completion costs a fraction of a cent and
    turns "saved" into "working".
    """
    if not live_model_probe_allowed(settings):
        raise SetupError(_LIVE_MODEL_PROBE_DENIAL)
    spec = PROVIDERS.get(provider) or PROVIDERS["custom"]
    details = model_key_details(provider=provider, base_url=base_url, model=model)
    name = details["model"]
    endpoint = details["endpoint"]
    url_base = details["base_url"]

    if spec.get("api") == "anthropic":
        body = {"model": name, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        body = {"model": name, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"Authorization": f"Bearer {api_key}",
                   "content-type": "application/json"}

    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return {"ok": True, "status": response.status, "model": name,
                    "endpoint": endpoint}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="ignore")[:400]
        # SAY WHICH KIND OF WRONG. "Setup failed" sends somebody to re-read their key when the
        # real problem was a model name their account cannot reach.
        hint = {
            401: "that key was rejected — check it was copied whole.",
            403: "the key is valid but not allowed to use that model.",
            404: f"no model called {name!r} at {url_base} — check the model name.",
            429: "the key works; the provider is rate-limiting right now.",
        }.get(error.code, "")
        return {"ok": False, "status": error.code, "model": name, "endpoint": endpoint,
                "error": hint or detail, "detail": detail}
    except urllib.error.URLError as error:
        return {"ok": False, "status": 0, "model": name, "endpoint": endpoint,
                "error": f"could not reach {url_base}: {error.reason}"}


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
    # openai and "anything OpenAI-compatible" are both the engine-owned native agent.
    by_api_provider = {"openai": "native", "custom": "native", "anthropic": "claude_code",
                       "openrouter": "openrouter", "groq": "groq", "deepseek": "deepseek",
                       "gemini": "gemini", "xai": "xai", "azure": "azure"}

    found: dict[str, dict[str, Any]] = {}

    def offer(engine: str, why: str, **extra: Any) -> None:
        if engine in known and engine not in found:
            found[engine] = {"engine": engine, "why": why, **extra}

    rows = connection.execute(
        text("SELECT source_id, provider, connection_status, updated_at, "
             "       credential_secret_ref_id, observed_capacity, limit_shape, cost_mode "
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
        # the role pickers, the console's model panel, an operator deciding whether to wait
        # — needs to be able to tell them apart. Recorded by provider_limits from the one
        # place every engine reports through.
        from .provider_limits import is_available
        capacity = row["observed_capacity"] if isinstance(row["observed_capacity"], dict) else {}
        offer(engine, "subscription" if connected else "api key",
              source_id=source_id,
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
    env_lanes = [("native", "openai_api_key"), ("claude_code", "claude_code_oauth_token"),
                 ("claude_code", "anthropic_api_key"), ("azure", "azure_openai_key"),
                 ("openrouter", "openrouter_api_key"), ("groq", "groq_api_key"),
                 ("deepseek", "deepseek_api_key"), ("gemini", "google_api_key"),
                 ("xai", "xai_api_key"), ("vllm", "vllm_base_url"),
                 ("ollama", "ollama_base_url")]
    for engine, attr in env_lanes:
        if _set_deliberately(settings, attr):
            offer(engine, "this install's environment", source_id=None, saved_at=None)
    return found


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
    """Which harness should WRITE THE CODE when nobody has said.

    "The main objective of BOSS is not to depend on a single model but must be able to
    connect and work with any model provider."

    The seam has been provider-agnostic for a while: one `run_role` interface, a registry of
    eleven harnesses, a native tool-calling agent for any OpenAI-compatible key. What was left
    of the old dependency was the DEFAULT. `code_agent_engine` ships as "claude_code" and
    every fallback chain ends there, so a customer who connects only an OpenRouter key gets a
    build that shells out to a `claude` binary their install does not have — and the failure
    reads as a broken pipeline rather than as a default nobody chose.

    Deliberately conservative: this only speaks when the configured engine CANNOT run here.
    An install whose Claude Code is connected behaves exactly as it did, and an operator who
    set the variable themselves is never second-guessed. What it removes is the case where the
    default points at a harness this installation has no credential for.
    """
    # THE OPERATOR'S CHOICE LIVES IN THE PROJECT, NOT IN THE ENVIRONMENT.
    #
    # This read `settings.code_agent_engine` — the env default — so the console's
    # "Implementer" picker had no effect on a build at all. Setting it to openrouter and
    # pressing save wrote `project_settings.code_agent_engine = "openrouter"`, the screen
    # showed openrouter, and every build still ran on the env's claude_code. A control that
    # cannot change what happens is worse than no control: it spends the operator's trust and
    # returns nothing. `chat_engine_for` two functions below has always read the knob; this is
    # the same `effective()`, not a second way of asking.
    from .project_settings import effective

    try:
        configured = (effective(connection, project_id, settings, "code_agent_engine")
                      or "").strip().lower()
    except Exception:  # noqa: BLE001 — an unreachable settings table falls back to the env
        configured = (getattr(settings, "code_agent_engine", "") or "").strip().lower()
    try:
        connected = connected_engines(connection, settings, project_id)
    except Exception:  # noqa: BLE001 — a lookup must not decide the pipeline cannot run
        return configured
    if not connected:
        return configured          # nothing to choose from; leave the error where it was
    if configured and configured in connected:
        return configured
    # Prefer a harness a governed task has actually completed on. `verified` is BOSS's own
    # honesty flag and it is the right tie-breaker here: an unproven adapter is a hypothesis,
    # and picking one BY DEFAULT would be presenting a hypothesis as a decision.
    try:
        from .harness_registry import available_harnesses

        order = sorted(
            (h for h in available_harnesses() if h["name"] in connected),
            key=lambda h: (not h["verified"], h["kind"] != "cli", h["name"]),
        )
    except Exception:  # noqa: BLE001
        return configured
    return order[0]["name"] if order else configured


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

    Returns a name from `project_settings.CHAT_ENGINES`, or "" to leave the choice alone.
    """
    from .project_settings import CHAT_ENGINES, effective

    try:
        chosen = (effective(connection, project_id, settings, "chat_llm_engine") or "").strip()
    except Exception:  # noqa: BLE001 — an unreachable settings table is not an answer
        chosen = (getattr(settings, "chat_llm_engine", "") or "").strip()
    if chosen:
        return chosen.lower()
    # An API key IS a configured chat model, and the adapter has always used it. Overriding
    # that with a CLI would move the conversation of every install that already worked.
    if str(getattr(settings, "openai_api_key", "") or "").strip():
        return ""
    try:
        connected = connected_engines(connection, settings, project_id)
    except Exception:  # noqa: BLE001
        return ""
    for candidate in CHAT_ENGINES:
        if candidate in connected:
            return candidate
    return ""


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
                   prove=None) -> dict[str, Any]:
    """`prove_coding_agent`, spent only after its reservation and receipt commit.

    A proof is one real model turn. On its own committed connection, before the turn: a budget
    reservation (never above the per-minute cap, so it can be granted) and an in-progress
    receipt. After the turn: the receipt succeeds or fails and the reservation settles at the
    actual cost or is released. A refused budget makes no call. `prove` is injectable for tests.
    """
    import math
    import uuid

    from .budget import BudgetGovernor
    from .receipts import ReceiptService

    budget, receipts = BudgetGovernor(settings), ReceiptService()
    prove = prove or (lambda: prove_coding_agent(settings, provider))

    def open_spend(connection):
        burst = int(budget._burst_cap(connection, project_id) or 0)
        amount = min(PROOF_ESTIMATE_USD_MICROS, burst) if burst > 0 else PROOF_ESTIMATE_USD_MICROS
        reserved = budget.reserve(connection, project_id, "model", amount,
                                  f"Connection proof for {provider}", 15, risk_tier=1)
        if reserved.get("status") != "reserved":
            return {"refused": reserved.get("message") or "Budget reservation denied."}
        scope = connection.execute(
            text("SELECT w.org_id, p.workspace_id FROM projects p "
                 "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
            {"p": project_id}).mappings().one()
        receipt, _ = receipts.create_or_reuse(
            connection, scope["org_id"], scope["workspace_id"], project_id,
            f"setup-proof:{provider}:{uuid.uuid4().hex[:16]}", "model.connection_proof",
            {"project_id": project_id, "provider": provider},
            {"provider": provider, "actor": actor})
        return {"reservation_id": reserved["reservation_id"], "receipt_id": receipt["id"],
                "amount": amount}

    with engine.begin() as connection:
        spend = open_spend(connection)
    if spend.get("refused"):
        return {"ok": False, "error": spend["refused"], "budget_refused": True}
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
    actor: str = "ceo", prove: bool = True,
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
    elif _edition_is_paid(settings):
        proof = prove_coding_agent(settings, provider)
    else:
        engine = getattr(connection, "engine", None)
        if engine is None:
            raise SetupError("the connection proof needs its own database connection.")
        proof = governed_proof(engine, settings, project_id, provider, actor)
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
        from .entitlements import EntitlementBrokerService

        payload = {
            "project_id": project_id,
            "provider": spec.entitlement_provider or provider,
            "source_id": spec.entitlement_source_id,
            "source_kind": "official_cli",
            "entitlement_kind": "subscription",
            "owner_type": "operator",
            "owner_id": actor,
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
        "use_it_with": (f"BOSS_CODE_AGENT_ENGINE={provider}"
                        if spec.require_local_session_file else
                        f"BOSS_PROVIDER_LOCK={provider}"),
        **record,
    }


def _last4(value: str) -> str:
    """Enough to tell two credentials apart, never enough to use one."""
    text_value = str(value or "").strip()
    return text_value[-4:] if len(text_value) >= 4 else ""


def _probe(url: str, headers: dict[str, str], timeout: int = 10) -> dict[str, Any]:
    """A GET that never raises. Returns the parsed body on success, the reason on failure."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(200_000)
        try:
            return {"ok": True, "status": 200, "body": json.loads(raw.decode(errors="ignore"))}
        except ValueError:
            return {"ok": True, "status": 200, "body": {}}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": error.code,
                "error": error.read().decode(errors="ignore")[:200]}
    except urllib.error.URLError as error:
        return {"ok": False, "status": 0, "error": str(error.reason)}


def check_github(settings: Settings) -> dict[str, Any]:
    """What this install is pointed at, and whether the token can actually push to it.

    A READ, and free. "Connected" that means "an environment variable is not empty" is the kind
    of status that reads green while the first build dies at push time — which is precisely the
    failure this screen exists to make impossible. GET /repos returns the caller's own
    permissions, so BOSS can say `push: true` rather than `token: present`.
    """
    token = str(getattr(settings, "github_token", "") or "").strip()
    owner = str(getattr(settings, "target_github_owner", "") or "").strip()
    repo = str(getattr(settings, "target_github_repo", "") or "").strip()
    out: dict[str, Any] = {
        "configured": bool(token and owner and repo),
        "repository": f"{owner}/{repo}" if owner and repo else "",
        "token_last4": _last4(token),
        "source": "environment",
    }
    if not out["configured"]:
        out["ok"] = False
        out["error"] = ("BOSS_GITHUB_TOKEN, BOSS_TARGET_GITHUB_OWNER and "
                        "BOSS_TARGET_GITHUB_REPO are not all set.")
        return out
    probe = _probe(
        f"https://api.github.com/repos/{owner}/{repo}",
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "User-Agent": "boss-setup-check"},
    )
    if not probe["ok"]:
        out["ok"] = False
        out["error"] = {
            401: "the token was rejected by GitHub.",
            403: "the token is valid but not allowed to see that repository.",
            404: f"GitHub has no {owner}/{repo} visible to this token.",
        }.get(probe.get("status"), probe.get("error") or "GitHub did not answer")
        return out
    body = probe.get("body") or {}
    permissions = body.get("permissions") or {}
    out["ok"] = bool(permissions.get("push"))
    out["can_push"] = bool(permissions.get("push"))
    out["default_branch"] = body.get("default_branch")
    out["private"] = body.get("private")
    if not out["ok"]:
        # NAMING THE MISSING HALF. A token that can read but not push looks connected and
        # produces a pull request that never opens.
        out["error"] = ("the token can read that repository but cannot push to it — "
                        "Contents needs read and write.")
    return out


def check_model(settings: Settings) -> dict[str, Any]:
    """Which model endpoint this install uses, with live checks only where allowed.

    Paid editions use `GET /models` to check reachability and authentication. Community and
    unknown editions report an environment key as configured but unverified, with no provider
    request. Even a successful `/models` response cannot prove a completion will work.
    """
    key = str(getattr(settings, "openai_api_key", "") or "").strip()
    base = (str(getattr(settings, "openai_base_url", "") or "").strip()
            or "https://api.openai.com/v1")
    model = (str(getattr(settings, "chat_model", "") or "").strip()
             or str(getattr(settings, "openai_model", "") or "").strip())
    out: dict[str, Any] = {
        "configured": bool(key),
        "base_url": base,
        "model": model,
        "key_last4": _last4(key),
        "source": "environment",
    }
    if not key:
        out["ok"] = False
        out["error"] = "OPENAI_API_KEY is not set."
        return out
    if not live_model_probe_allowed(settings):
        out["ok"] = False
        out["verification_state"] = "unverified"
        out["note"] = "The key is configured; Community has not contacted the model provider."
        return out
    probe = _probe(f"{base.rstrip('/')}/models", {"Authorization": f"Bearer {key}"})
    if not probe["ok"]:
        out["ok"] = False
        out["error"] = {
            401: "the key was rejected by the endpoint.",
            404: "that endpoint has no /models — it may still work for completions.",
        }.get(probe.get("status"), probe.get("error") or "the endpoint did not answer")
        return out
    listed = (probe.get("body") or {}).get("data") or []
    names = [str(item.get("id")) for item in listed if isinstance(item, dict)][:60]
    out["ok"] = True
    out["models_available"] = len(names)
    # WHETHER THE MODEL THIS INSTALL ASKS FOR IS ACTUALLY THERE. A gateway that answers and does
    # not carry the configured alias is the failure that looks like the model being "slow".
    if model and names:
        out["model_listed"] = model in names
        if not out["model_listed"]:
            out["note"] = (f"the endpoint answered but does not list {model!r}; "
                           f"it may still route it.")
    return out


def check_github_for_project(
    connection: Connection, settings: Settings, project_id: str,
) -> dict[str, Any]:
    """The repository this project builds in, checked with the credential it is connected with.

    The registry wins over the environment — that is already true of where a code task writes,
    and a status card that reports the other one describes an install nobody is running.
    """
    try:
        rows = connection.execute(
            text("SELECT b.full_name, b.connection_id, b.role, b.status, "
                 "       c.last_test_status "
                 "  FROM github_repository_bindings b "
                 "  JOIN github_connections c ON c.id = b.connection_id "
                 " WHERE b.project_id = :p AND b.status = 'active' "
                 " ORDER BY (b.role = 'primary') DESC, b.updated_at DESC LIMIT 1"),
            {"p": project_id},
        ).mappings().first()
    except Exception:  # noqa: BLE001 — a status card must not fail with the database
        rows = None
    if rows is None:
        return check_github(settings)          # nothing connected: the environment is the answer

    from .github_connections import GitHubConnectionService

    out: dict[str, Any] = {
        "configured": True,
        "repository": rows["full_name"],
        "source": "connected",
        "connection_id": rows["connection_id"],
        "last_test_status": rows["last_test_status"],
    }
    service = GitHubConnectionService(settings)
    try:
        credential_ref = service._credential_ref(connection, project_id, rows["connection_id"])
        secret = service.secret_refs.require_usable(
            connection, project_id=project_id, credential_ref=credential_ref)
        token = service._resolve_secret(secret, connection) or ""
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"the stored credential could not be read ({type(exc).__name__})"
        return out
    out["token_last4"] = _last4(token)
    owner, _, name = rows["full_name"].partition("/")
    probe = _probe(
        f"https://api.github.com/repos/{owner}/{name}",
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "User-Agent": "boss-setup-check"},
    )
    if not probe["ok"]:
        out["ok"] = False
        out["error"] = {
            401: "the token was rejected by GitHub.",
            403: "the token is valid but not allowed to see that repository.",
            404: (f"GitHub will not confirm {rows['full_name']} to this token — for a private "
                  f"repository that is what no access looks like."),
        }.get(probe.get("status"), probe.get("error") or "GitHub did not answer")
        return out
    body = probe.get("body") or {}
    permissions = body.get("permissions") or {}
    out["ok"] = bool(permissions.get("push"))
    out["can_push"] = bool(permissions.get("push"))
    out["default_branch"] = body.get("default_branch")
    out["private"] = bool(body.get("private"))
    if not out["ok"]:
        out["error"] = (f"the token can see {rows['full_name']} but cannot push to it — "
                        f"Contents needs read and write.")
    return out


def setup_status(connection: Connection, settings: Settings, project_id: str) -> dict[str, Any]:
    """What is connected, what is not, and what to do about it."""
    repos = connection.execute(
        text("SELECT count(*) FROM github_repository_bindings "
             "WHERE project_id = :p AND status = 'active'"),
        {"p": project_id},
    ).scalar() or 0
    connections = connection.execute(
        text("SELECT count(*) FROM github_connections "
             # 'configured' and 'active' ARE the healthy states; 'enabled' is not a value this
             # column's CHECK constraint permits, so this counted zero forever. With the env
             # defaults emptied it stopped being masked: a project with a connected,
             # push-tested repository was told "Connect a repository — not done", directly
             # under a card saying CAN PUSH about that same repository.
             "WHERE project_id = :p AND status IN ('configured', 'active')"),
        {"p": project_id},
    ).scalar() or 0
    # A CONNECTED SUBSCRIPTION IS A CONNECTED MODEL. Counting stored credentials alone meant
    # an operator could sign the Codex CLI in, press Connect, watch BOSS prove a live turn and
    # record the entitlement — and still be told "Connect a model" on the next page load,
    # because a subscription has no credential to store a reference to. The step looked unsaved
    # when the only thing missing was this clause.
    models = connection.execute(
        text("SELECT count(*) FROM entitlement_sources "
             "WHERE project_id = :p AND (credential_secret_ref_id IS NOT NULL "
             "                           OR connection_status = 'connected')"),
        {"p": project_id},
    ).scalar() or 0
    # WHICH ONE, AND WHEN — not just how many. "Connect a model" with a tick beside it and
    # nothing else on screen is the shape of a wizard that has forgotten what you told it: the
    # operator cannot tell whether the thing they connected an hour ago is the thing that is
    # saved. Newest first, so the head of this list is the last thing anybody connected.
    saved_rows = connection.execute(
        text("SELECT source_id, provider, auth_method, connection_status, status, "
             "connection_metadata, updated_at "
             "FROM entitlement_sources "
             "WHERE project_id = :p AND (credential_secret_ref_id IS NOT NULL "
             "                           OR connection_status = 'connected') "
             "ORDER BY updated_at DESC"),
        {"p": project_id},
    ).mappings().all()
    # WHICH DROPDOWN ENTRY THIS IS. The console needs to reselect the thing that was saved,
    # and a source_id is not a wizard provider key: the API-key route writes "setup-<key>",
    # the CLI route writes the harness's entitlement id. Resolved here, from the harness
    # registry, so the console never has to guess the mapping and cannot drift from it.
    from .cli_harness import load_specs

    by_entitlement = {spec.entitlement_source_id: spec.name
                      for spec in load_specs(settings).values()
                      if spec.entitlement_source_id}
    # claude_code is native rather than a CLI harness, so it has no spec to read this from.
    by_entitlement.setdefault("anthropic_claude_code", "claude_code")

    def _wizard_provider(source_id: str) -> str:
        if source_id.startswith("setup-"):
            return source_id[len("setup-"):]
        return by_entitlement.get(source_id, "")

    def _verification_state(row: Any) -> bool:
        metadata = row["connection_metadata"] or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        state = metadata.get("verification_state")
        if state in {"verified", "unverified"}:
            return state == "verified"
        return row["status"] == "verified" or bool(metadata.get("verified_endpoint"))

    saved = [
        {
            "source_id": r["source_id"],
            "provider": r["provider"],
            "wizard_provider": _wizard_provider(r["source_id"]),
            "auth_method": r["auth_method"],
            "kind": ("subscription" if str(r["auth_method"] or "").endswith("oauth")
                     else "api_key"),
            "connected": r["connection_status"] == "connected",
            "verified": _verification_state(r),
            # Postgres hands back a datetime; a driver that hands back a string is equally
            # displayable, and neither is worth an exception on a status screen.
            "saved_at": (r["updated_at"].isoformat()
                         if hasattr(r["updated_at"], "isoformat")
                         else (str(r["updated_at"]) or None)) if r["updated_at"] else None,
        }
        for r in saved_rows
    ]

    # THE ENVIRONMENT COUNTS TOO, FOR BOTH STEPS. An install configured the old way — a token
    # and a key in the API's env — IS set up, and a wizard that says "not connected" to somebody
    # whose BOSS is already opening pull requests has told them a falsehood about their own
    # system. Measured on this very deployment, which has no github_connections row and has been
    # building against example-user/boss-sandbox for weeks.
    env_model = bool(str(getattr(settings, "openai_api_key", "") or "").strip())
    env_repo = str(getattr(settings, "target_github_repo", "") or "").strip()
    env_owner = str(getattr(settings, "target_github_owner", "") or "").strip()
    env_github = bool(str(getattr(settings, "github_token", "") or "").strip()
                      and env_repo and env_owner)
    steps = [
        {
            "id": "github",
            "title": "Connect a repository",
            "done": bool((connections and repos) or env_github),
            "detail": ("BOSS writes code on a branch and opens a pull request. It needs a "
                       "repository it is allowed to push to."),
            "from_environment": env_github and not (connections and repos),
            "repository": f"{env_owner}/{env_repo}" if env_github else None,
        },
        {
            "id": "model",
            "title": "Connect a model",
            "done": bool(models or env_model),
            "from_environment": env_model and not models,
            "detail": ("An API key, anything that speaks the OpenAI API, or a subscription "
                       "you already pay for."),
        },
    ]
    return {
        "project_id": project_id,
        "ready": all(step["done"] for step in steps),
        # WHAT THIS INSTALL IS ACTUALLY RUNNING ON. The steps above answer "is it set up"; these
        # answer "set up as WHAT, and does it work" — which is the question an operator has when
        # they already believe it is configured, and the one a screen of empty forms refuses to
        # answer.
        # WHAT THIS PROJECT IS ACTUALLY POINTED AT, which is not always the environment.
        #
        # This card reported the env pair and nothing else, so an install whose operator had
        # connected a repository — tested, passing, and the thing every build will use — was
        # shown a developer sandbox as failed from a shipped default, with the
        # working connection mentioned nowhere. A screen called "What BOSS is running on" has
        # to name the repository BOSS will run on.
        "environment": {"github": check_github_for_project(connection, settings, project_id),
                        "model": check_model(settings)},
        "steps": steps,
        "github_connections": int(connections),
        "repositories": int(repos),
        "model_sources": int(models),
        "model_probe_available": live_model_probe_allowed(settings),
        "model_saved": saved,
        "model_from_environment": env_model,
        "github_from_environment": env_github,
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
