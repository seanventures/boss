#!/usr/bin/env python3
"""What this install can use as a model: one row per source, with the command that fixes it.

`boss connect --summary` and the installer's summary card print these rows. Each source is
connected, found but not connected, or not found:

- API keys: the provider key names present in the install's private .env (never the values)
- Codex, Claude Code, Grok Build: the subscription BOSS recorded (entitlement sources), or a
  login found on this machine
- Ollama: the "Anything OpenAI-compatible" connection that points at host.docker.internal, or
  an Ollama found running or installed on this machine
- Default: which model the project's roles use, read from /v1/settings

Read only. Talks to this install's own API on this machine. When the API does not answer, the
rows say so and fall back to what this machine has.
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
    """True when any hosted or public model is connected. None when BOSS cannot say."""
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


def read_found(path: Path | None) -> dict[str, bool]:
    if path is None:
        return {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    return {name: value == "found" for name, _, value in (line.partition("=") for line in lines)}


def rows(api_url: str, api_key: str, env_file: Path, found: dict[str, bool]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    items = sources(api_url, api_key) if api_key else None
    keys = env_key_names(env_file)
    out.append(("API keys", f"connected: {' '.join(keys)} (values not shown)" if keys else
                "not found. Export a key, then: boss connect keys"))
    for name, label, source_id, found_key in SUBSCRIPTIONS:
        if items is not None and any(item.get("source_id") == source_id and
                                     item.get("connection_status") == "connected"
                                     for item in items):
            out.append((label, "connected"))
        elif found.get(found_key):
            out.append((label, f"found, not connected: boss connect {name}"))
        elif name == "codex":
            out.append((label, "not found. Sign in with codex login --device-auth, "
                               "then: boss connect codex"))
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
        items = sources(args.api_url, api_key) if api_key else None
        if items is None:
            return 4
        return 0 if ollama_connection(items) is not None else 3
    if args.hosted:
        state = hosted_connected(args.api_url, api_key, args.env_file)
        return 0 if state is True else 3 if state is False else 4
    for label, text in rows(args.api_url, api_key, args.env_file, read_found(args.found)):
        print(f"    {label:<12} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
