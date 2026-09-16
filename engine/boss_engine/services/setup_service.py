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


from pathlib import Path as pathlib_Path


def list_coding_agents(settings: Settings) -> list[dict[str, Any]]:
    """Every CLI coding agent BOSS can drive, and whether this machine can use it yet."""
    from .cli_harness import detect_all

    return detect_all(settings)


def store_coding_agent_credential(
    connection, settings: Settings, project_id: str, provider: str, credential: str, *,
    actor: str = "setup",
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
    from .secret_backends import default_secret_backend
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
    if spec.auth_file_name.endswith(".json"):
        try:
            json.loads(credential)
        except json.JSONDecodeError:
            # Malformed is all that is safe to say; the text is a live credential.
            raise SetupError(f"the {provider} credential is not valid JSON.") from None

    stored = put_secret(
        connection, settings, project_id=project_id, name=key, value=credential,
        secret_kind="api_token", backend=default_secret_backend(settings),
        scope_id=SUBSCRIPTION_SECRET_SCOPE, actor=actor,
    )
    return {"ok": True, "provider": provider, "key": key,
            "backend": stored["backend"], "ref_key": stored["ref_key"],
            "fingerprint": stored["fingerprint"], "raw_secret_stored": False}


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


def connect_coding_agent(
    connection: Connection, settings: Settings, project_id: str, provider: str, *,
    actor: str = "setup", prove: bool = True,
) -> dict[str, Any]:
    """Import a coding-agent subscription the customer already has, into BOSS.

    There is nothing to paste. A Codex or Cursor subscription is an OAuth session held by that
    CLI, so the only honest way to "configure" one is to find the CLI, confirm it is signed in,
    prove it answers, and record the entitlement. Anything else is asking the operator to
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
    found = detect(spec)
    if not found["installed"]:
        raise SetupError(
            f"the {provider} CLI is not installed on this machine. "
            f"{found['next_step']}")
    if found["authenticated"] is False:
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

    proof = {"ok": True, "skipped": True} if not prove else prove_coding_agent(settings, provider)
    if not proof.get("ok"):
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
        "use_it_with": f"BOSS_PROVIDER_LOCK={provider}",
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
    """Which model endpoint this install uses, and whether it answers.

    Deliberately `GET /models` and not a completion: it is free, it is the same credential, and
    it proves reachability plus authentication. It cannot prove the chosen model will answer —
    that is what the wizard's one-token completion is for when a key is first added.
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
    # THE ENVIRONMENT COUNTS TOO, FOR BOTH STEPS. An install configured the old way — a token
    # and a key in the API's env — IS set up, and a wizard that says "not connected" to somebody
    # whose BOSS is already opening pull requests has told them a falsehood about their own
    # system. Measured on this very deployment, which has no github_connections row and has been
    # building against paramg/boss-sandbox for weeks.
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
        "environment": {"github": check_github(settings), "model": check_model(settings)},
        "steps": steps,
        "github_connections": int(connections),
        "repositories": int(repos),
        "model_sources": int(models),
        "model_from_environment": env_model,
        "github_from_environment": env_github,
    }
