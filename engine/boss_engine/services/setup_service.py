"""First-run setup: connect a repository, connect a model, and say what is still missing.

WHY THIS EXISTS AS ITS OWN THING. Every piece below already had an endpoint — GitHub
connections, entitlement sources, credential upload, connection tests. What did not exist was an
answer to "I just installed this, what do I do?", and the honest answer was: set two environment
variables on the API container, restart it, then call four endpoints in the right order with the
right ids. That is not onboarding. For open source it is where the evaluation ends.

So this module owns the ORDER and the VERDICT: it knows which steps make a working install, it
proves each one against the real service rather than trusting that a key was pasted correctly,
and it names precisely what is missing. Nothing here is a new capability; it is the sequence
somebody would otherwise have to learn from the source.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

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
    "custom": {
        "label": "Anything OpenAI-compatible",
        "base_url": "",
        "api": "openai",
        "example_model": "",
        "key_url": "",
        "kind": "api_key",
        "note": "LiteLLM, Ollama, vLLM, Groq, Together, or your own proxy.",
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


def provider_catalogue() -> list[dict[str, Any]]:
    """What the wizard shows, in the order it shows it."""
    return [{"provider": key, **value} for key, value in PROVIDERS.items()]


def verify_model_key(
    *, provider: str, api_key: str, base_url: str = "", model: str = "", timeout: int = 20
) -> dict[str, Any]:
    """Spend one token proving the key works, before telling anybody it is set up.

    A wizard that stores a key and reports success has verified that the operator can type. The
    first real evidence otherwise arrives when a build fails an hour later, by which point the
    key is the last thing anyone suspects. One tiny completion costs a fraction of a cent and
    turns "saved" into "working".
    """
    spec = PROVIDERS.get(provider) or PROVIDERS["custom"]
    url_base = (base_url or spec.get("base_url") or "").strip().rstrip("/")
    if not url_base:
        raise SetupError("a base URL is required for a custom provider.")
    name = (model or spec.get("example_model") or "").strip()
    if not name:
        raise SetupError("a model name is required.")

    if spec.get("api") == "anthropic":
        endpoint = f"{url_base}/messages"
        body = {"model": name, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        endpoint = f"{url_base}/chat/completions"
        body = {"model": name, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
        headers = {"Authorization": f"Bearer {api_key}",
                   "content-type": "application/json"}

    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


def setup_status(connection: Connection, settings: Settings, project_id: str) -> dict[str, Any]:
    """What is connected, what is not, and what to do about it."""
    repos = connection.execute(
        text("SELECT count(*) FROM github_repository_bindings "
             "WHERE project_id = :p AND status = 'active'"),
        {"p": project_id},
    ).scalar() or 0
    connections = connection.execute(
        text("SELECT count(*) FROM github_connections "
             "WHERE project_id = :p AND status = 'enabled'"),
        {"p": project_id},
    ).scalar() or 0
    models = connection.execute(
        text("SELECT count(*) FROM entitlement_sources "
             "WHERE project_id = :p AND credential_secret_ref_id IS NOT NULL"),
        {"p": project_id},
    ).scalar() or 0
    # THE ENVIRONMENT COUNTS TOO. An install configured the old way — a key in the API's env —
    # is set up, and a wizard that says "no model configured" to somebody whose BOSS is
    # answering questions has just told them a falsehood about their own system.
    env_model = bool(str(getattr(settings, "openai_api_key", "") or "").strip())
    steps = [
        {
            "id": "github",
            "title": "Connect a repository",
            "done": bool(connections and repos),
            "detail": ("BOSS writes code on a branch and opens a pull request. It needs a "
                       "repository it is allowed to push to."),
        },
        {
            "id": "model",
            "title": "Connect a model",
            "done": bool(models or env_model),
            "detail": ("An API key, anything that speaks the OpenAI API, or a subscription "
                       "you already pay for."),
        },
    ]
    return {
        "project_id": project_id,
        "ready": all(step["done"] for step in steps),
        "steps": steps,
        "github_connections": int(connections),
        "repositories": int(repos),
        "model_sources": int(models),
        "model_from_environment": env_model,
    }
