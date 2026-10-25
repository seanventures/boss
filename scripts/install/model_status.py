#!/usr/bin/env python3
"""What this install can use as a model: one row per source, with the command that fixes it.

`boss connect --summary` and the installer's summary card print these rows. They come from
BOSS's own answer, GET /v1/setup — the view Repo and models renders — so the CLI says exactly
what the console says:

- every model BOSS holds (a console-stored key, a key from the install's .env, a subscription,
  a local model), with its pill from the LAST REAL PROOF: Working, Login expired, Key
  rejected, Not checked... A login the provider refused is never "connected".
- each subscription BOSS does not hold yet: signed in inside BOSS, needs sign-in, or not in
  this image (and a legacy stored Claude token, with how to remove it)
- Ollama when nothing local is connected: found or installed on this machine
- Default: which engine and source the Build job actually resolves to, then the other jobs

Read only. Talks to this install's own API on this machine. When /v1/setup does not answer,
the rows fall back to what the .env, the recorded sources and this machine say, and say so.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT = "boss-dogfood"
KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
             "OPENROUTER_API_KEY")
SUBSCRIPTIONS = (
    ("codex", "Codex", "openai_codex_chatgpt", "codex_login"),
    ("claude", "Claude Code", "anthropic_claude_code", "claude_login"),
    ("grok", "Grok Build", "xai_grok_build", "grok_login"),
)
#: `boss connect <name>` -> the provider GET /v1/setup names it by.
SETUP_PROVIDER = {"codex": "codex", "claude": "claude_code", "grok": "grok"}
#: Pill states a job can still run on — setup_view.USABLE_MODEL (a test holds them equal).
USABLE_STATES = frozenset({"working", "not_checked", "rate_limited", "paused_budget",
                           "unreachable"})
LOCAL_PROVIDERS = ("ollama", "vllm")
#: engine -> the name an operator knows it by (setup_view.ENGINE_LABELS, the ones jobs use).
ENGINE_LABELS = {
    "claude_code": "Claude Code", "codex": "Codex", "grok": "Grok Build", "cursor": "Cursor",
    "native": "OpenAI-compatible", "openrouter": "OpenRouter", "gemini": "Gemini", "xai": "xAI",
    "ollama": "Ollama", "vllm": "Local model", "typesafe": "TypeSafe Jev",
    "claude_agent_sdk": "Claude Agent SDK",
}
HOSTED_SETUP = ("setup-openai", "setup-anthropic", "setup-gemini", "setup-xai", "setup-openrouter")
ROLE_ENGINES = ("code_agent_engine", "code_review_engine", "code_agent_strong_engine",
                "code_agent_low_cost_engine")
LOCAL_HOST = "host.docker.internal"


def _env_lines(env_file: Path) -> list[str]:
    try:
        return env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _env_value(lines: list[str], name: str) -> str:
    for line in lines:
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def chat_is_local(env_file: Path) -> bool:
    """OPENAI_* point at the local Ollama (the placeholder key and a local base URL)."""
    lines = _env_lines(env_file)
    host = urllib.parse.urlparse(_env_value(lines, "OPENAI_BASE_URL")).hostname
    return _env_value(lines, "OPENAI_API_KEY") == "ollama" and \
        host in {LOCAL_HOST, "localhost", "127.0.0.1", "::1"}


def env_key_names(env_file: Path) -> list[str]:
    """Hosted provider keys in .env. The local Ollama's placeholder OpenAI key is not one."""
    lines = _env_lines(env_file)
    local_openai = chat_is_local(env_file)
    return [name for name in KEY_NAMES if _env_value(lines, name)
            and not (name == "OPENAI_API_KEY" and local_openai)]


def api_get(api_url: str, api_key: str, path: str) -> dict | None:
    request = urllib.request.Request(f"{api_url.rstrip('/')}{path}",
                                     headers={"x-boss-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode() or "{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return body if isinstance(body, dict) else None


def sources(api_url: str, api_key: str) -> list[dict] | None:
    body = api_get(api_url, api_key, f"/v1/entitlements/sources?project_id={PROJECT}")
    if body is None:
        return None
    items = body.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _base_url(item: dict) -> str:
    metadata = item.get("connection_metadata")
    return str(metadata.get("base_url") or "") if isinstance(metadata, dict) else ""


def ollama_connection(items: list[dict]) -> str | None:
    """The model of the Ollama connection, or None when there is none."""
    for item in items:
        if item.get("source_id") == "setup-custom" and item.get("credential_secret_ref_id") and \
                urllib.parse.urlparse(_base_url(item)).hostname == LOCAL_HOST:
            models = item.get("supported_models")
            names = [m for m in models if isinstance(m, str) and m] if isinstance(models, list) else []
            return names[0] if names else ""
    return None


def hosted_sources(items: list[dict]) -> list[str]:
    """Hosted model connections BOSS recorded: subscriptions and API keys, never Ollama."""
    hosted = []
    for _name, label, source_id, _found in SUBSCRIPTIONS:
        if any(item.get("source_id") == source_id and item.get("connection_status") == "connected"
               for item in items):
            hosted.append(label)
    for item in items:
        source_id = item.get("source_id")
        if not item.get("credential_secret_ref_id"):
            continue
        if source_id in HOSTED_SETUP:
            hosted.append(source_id[len("setup-"):])
        elif source_id == "setup-custom" and \
                urllib.parse.urlparse(_base_url(item)).hostname != LOCAL_HOST:
            hosted.append("OpenAI-compatible")
    return hosted


def hosted_connected(api_url: str, api_key: str, env_file: Path) -> bool | None:
    """True when a hosted or public model BOSS can use is connected — from its proof, when
    BOSS's setup view answers (a refused login is not usable). None when BOSS cannot say."""
    view = setup_view(api_url, api_key)
    if view is not None:
        return any(usable(m) and not is_local(m) for m in _models(view))
    if env_key_names(env_file):
        return True
    items = sources(api_url, api_key)
    if items is None:
        return None
    return bool(hosted_sources(items))


def role_settings(api_url: str, api_key: str) -> dict[str, dict] | None:
    body = api_get(api_url, api_key, f"/v1/settings?project_id={PROJECT}")
    if body is None:
        return None
    knobs = body.get("knobs")
    return {knob["key"]: knob for knob in knobs if isinstance(knob, dict) and "key" in knob} \
        if isinstance(knobs, list) else {}


# ── From BOSS's own view (GET /v1/setup) ──────────────────────────────────────────────────────

def setup_view(api_url: str, api_key: str) -> dict | None:
    """GET /v1/setup, or None when BOSS did not answer with a view."""
    if not api_key:
        return None
    body = api_get(api_url, api_key, f"/v1/setup?project_id={PROJECT}")
    return body if body is not None and isinstance(body.get("models"), list) else None


def _models(view: dict) -> list[dict]:
    return [m for m in view.get("models") or [] if isinstance(m, dict)]


def _pill(model: dict) -> dict:
    pill = model.get("pill")
    return pill if isinstance(pill, dict) else {}


def is_local(model: dict) -> bool:
    """A model on this machine: Ollama or a local URL, or the OpenAI-compatible connection
    `boss connect ollama` makes at host.docker.internal."""
    if model.get("provider") in LOCAL_PROVIDERS or model.get("kind") == "local":
        return True
    host = urllib.parse.urlparse(str(model.get("base_url") or "")).hostname
    return model.get("provider") == "custom" and host == LOCAL_HOST


def usable(model: dict) -> bool:
    return _pill(model).get("state") in USABLE_STATES


def _origin(model: dict) -> str:
    if model.get("kind") == "subscription":
        return "subscription"
    if is_local(model):
        return "local"
    return "install key" if model.get("source") == "install" else "console key"


def _model_text(model: dict) -> str:
    pill = _pill(model)
    text = str(pill.get("label") or pill.get("state") or "unknown")
    if model.get("model") and model.get("kind") != "subscription":
        text += f" ({model['model']})"
    tip = str(pill.get("tip") or "").strip()
    return f"{text}. {tip}" if tip else text


def _detected_text(entry: dict | None, name: str, found_here: bool, detecting: bool) -> str:
    if entry and entry.get("legacy_token"):
        return f"BOSS holds a legacy Claude token. {entry.get('fix') or ''}".strip()
    state = (entry or {}).get("found")
    if state == "signed_in":
        return f"signed in inside BOSS, not connected yet: boss connect {name}"
    if state == "needs_sign_in" or (detecting and not state):
        return f"not signed in inside BOSS. Sign in there: boss connect {name}"
    if found_here:
        return (f"signed in on this machine, not inside BOSS. Sign in there: "
                f"boss connect {name}")
    return f"not connected. Sign in inside BOSS: boss connect {name}"


def _job_text(job: dict) -> str:
    resolved = job.get("resolved") if isinstance(job.get("resolved"), dict) else None
    if not resolved or not resolved.get("engine"):
        return f"{job.get('label') or job.get('job')} → nothing"
    engine = str(resolved["engine"])
    name = ENGINE_LABELS.get(engine, engine)
    model = f" · {resolved['model']}" if resolved.get("model") else ""
    source = f" on {resolved['source_id']}" if resolved.get("source_id") else ""
    return f"{job.get('label') or job.get('job')} → {name}{model}{source}"


def view_rows(view: dict, env_file: Path, found: dict[str, bool]) -> list[tuple[str, str]]:
    """The rows, from BOSS's own view: every pill is the console's."""
    out: list[tuple[str, str]] = []
    models = _models(view)
    for model in models:
        out.append((f"{model.get('label') or model.get('provider')} ({_origin(model)})",
                    _model_text(model)))
    held = {m.get("provider") for m in models}
    detected = {d.get("provider"): d for d in view.get("detected") or [] if isinstance(d, dict)}
    for name, label, _source_id, found_key in SUBSCRIPTIONS:
        provider = SETUP_PROVIDER[name]
        if provider in held:
            continue
        out.append((label, _detected_text(detected.get(provider), name,
                                          bool(found.get(found_key)),
                                          bool(view.get("detecting")))))
    if not any(is_local(m) for m in models):
        if found.get("ollama"):
            out.append(("Ollama", "found, not connected: boss connect ollama"))
        elif found.get("ollama_installed"):
            out.append(("Ollama", "installed, not running: boss connect --install-ollama"))
        else:
            out.append(("Ollama", "not found. Set it up: boss connect --install-ollama"))
    jobs = [j for j in view.get("jobs") or [] if isinstance(j, dict)]
    build = next((j for j in jobs if j.get("job") == "build"), None)
    if build is not None and (build.get("resolved") or {}).get("engine"):
        out.append(("Default", _job_text(build)))
    elif not any(usable(m) for m in models):
        out.append(("Default", "none. Model calls fail closed until one is connected: "
                               "boss connect"))
    else:
        out.append(("Default", "nothing resolves for Build. Choose one in Repo and models, "
                               "Who does what"))
    others = [_job_text(j) for j in jobs if j.get("job") != "build"]
    if others:
        out.append(("Jobs", "; ".join(others)))
    if chat_is_local(env_file):
        out.append(("Chat", "the local Ollama model (OPENAI_* in .env point at it)"))
    return out


def read_found(path: Path | None) -> dict[str, bool]:
    if path is None:
        return {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    return {name: value == "found" for name, _, value in (line.partition("=") for line in lines)}


def rows(api_url: str, api_key: str, env_file: Path, found: dict[str, bool]) -> list[tuple[str, str]]:
    view = setup_view(api_url, api_key)
    if view is not None:
        return view_rows(view, env_file, found)
    return fallback_rows(api_url, api_key, env_file, found)


def fallback_rows(api_url: str, api_key: str, env_file: Path,
                  found: dict[str, bool]) -> list[tuple[str, str]]:
    """When /v1/setup does not answer: the .env, the recorded sources and this machine. No
    pill is known, so nothing here is called Working."""
    out: list[tuple[str, str]] = []
    items = sources(api_url, api_key) if api_key else None
    keys = env_key_names(env_file)
    out.append(("API keys", f"connected: {' '.join(keys)} (values not shown)" if keys else
                "not found. Export a key, then: boss connect keys"))
    for name, label, source_id, found_key in SUBSCRIPTIONS:
        item = next((i for i in items or [] if i.get("source_id") == source_id), None)
        pill = item.get("pill") if item and isinstance(item.get("pill"), dict) else None
        if pill:
            out.append((label, str(pill.get("label") or pill.get("state"))))
        elif item is not None and item.get("connection_status") == "connected":
            out.append((label, "connected (recorded; its last proof is not known here — "
                               "see Repo and models)"))
        elif found.get(found_key):
            out.append((label, f"found, not connected: boss connect {name}"))
        else:
            out.append((label, f"not found. Sign in inside BOSS: boss connect {name}"))
    model = ollama_connection(items) if items is not None else None
    if model is not None:
        out.append(("Ollama", f"connected: {model or 'model not recorded'}"))
    elif found.get("ollama"):
        out.append(("Ollama", "found, not connected: boss connect ollama"))
    elif found.get("ollama_installed"):
        out.append(("Ollama", "installed, not running: boss connect --install-ollama"))
    else:
        out.append(("Ollama", "not found. Set it up: boss connect --install-ollama"))
    out.append(("Default", default_text(api_url, api_key, items, keys, model)))
    if chat_is_local(env_file):
        out.append(("Chat", "the local Ollama model (OPENAI_* in .env point at it)"))
    if items is None:
        out.append(("Note", "BOSS did not answer, so connected subscriptions are not shown. "
                            "Try: boss status"))
    else:
        out.append(("Note", "BOSS's setup view did not answer; these rows are unproven. "
                            "Try: boss status"))
    return out


def default_text(api_url: str, api_key: str, items: list[dict] | None, keys: list[str],
                 ollama_model: str | None) -> str:
    knobs = role_settings(api_url, api_key) if items is not None else None
    engine = model = ""
    if knobs:
        engine = str((knobs.get("code_agent_engine") or {}).get("value") or "")
        model = str((knobs.get("code_agent_model") or {}).get("value") or "")
    if ollama_model and engine == "native" and model == ollama_model:
        return f"Ollama {ollama_model} for every role (no hosted model was connected)"
    hosted = (hosted_sources(items) if items is not None else []) + keys
    if hosted:
        lane = f"{engine}{f' ({model})' if model else ''}" if engine else "the deployment default"
        return (f"hosted: {lane}. Change roles in Mission Control, Settings, Who does what"
                + ("; Ollama is added but not the default" if ollama_model is not None else ""))
    if ollama_model is not None:
        return "Ollama is connected; choose it for each role in Mission Control, Settings"
    return "none. Model calls fail closed until one is connected: boss connect"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--found", type=Path, help="discover_models.py --summary output")
    parser.add_argument("--hosted", action="store_true",
                        help="exit 0 when a hosted model is connected, 3 when none, 4 unknown")
    parser.add_argument("--local", action="store_true",
                        help="exit 0 when Ollama is connected, 3 when not, 4 unknown")
    args = parser.parse_args()
    api_key = os.environ.get("BOSS_API_KEY", "")
    if args.local:
        view = setup_view(args.api_url, api_key)
        if view is not None:
            return 0 if any(usable(m) and is_local(m) for m in _models(view)) else 3
        items = sources(args.api_url, api_key) if api_key else None
        if items is None:
            return 4
        return 0 if ollama_connection(items) is not None else 3
    if args.hosted:
        state = hosted_connected(args.api_url, api_key, args.env_file)
        return 0 if state is True else 3 if state is False else 4
    shown = rows(args.api_url, api_key, args.env_file, read_found(args.found))
    width = max([12, *(len(label) for label, _ in shown)])
    for label, text in shown:
        print(f"    {label:<{width}} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
