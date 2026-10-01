#!/usr/bin/env python3
"""Find the model keys and subscriptions already on this machine. Read only.

The installer and `boss connect` use this to report what exists before asking to connect
anything. It checks presence only:

- a provider key exported in this process's environment (the value is never read into output)
- a Codex ChatGPT login or a Claude Code subscription login, through each CLI's own status
  command (coding_subscriptions.detect_logins)
- a Grok Build login file under ~/.grok (existence only; the file is never opened)
- an Ollama server on this machine's loopback address

Nothing is stored, sent, or connected here. Output is found / not found per source, and an
optional KEY=found|missing summary file for the shell. Exit status is 0 whatever is found.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coding_subscriptions import detect_logins  # noqa: E402

ENV_KEYS = (
    ("openai_key", "OPENAI_API_KEY", "OpenAI API key"),
    ("anthropic_key", "ANTHROPIC_API_KEY", "Anthropic API key"),
    ("google_key", "GOOGLE_API_KEY", "Google Gemini API key"),
    ("xai_key", "XAI_API_KEY", "xAI API key"),
    ("openrouter_key", "OPENROUTER_API_KEY", "OpenRouter API key"),
)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}")


def env_keys(environ: dict[str, str]) -> dict[str, bool]:
    return {name: bool(environ.get(variable, "").strip()) for name, variable, _ in ENV_KEYS}


def ollama_model_names(base_url: str, timeout: float = 1.5) -> list[str] | None:
    """Names of the local models, or None when no Ollama answers on loopback.

    A name that is not a plain model tag is counted but not returned, so nothing odd from a
    local server reaches a terminal or a request.
    """
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK:
        return None
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout) as reply:
            body = json.loads(reply.read(1_000_000).decode() or "{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        return None
    names = []
    for item in models:
        name = item.get("name") if isinstance(item, dict) else None
        names.append(name if isinstance(name, str) and MODEL_NAME.fullmatch(name) else "")
    return names


def ollama_models(base_url: str, timeout: float = 1.5) -> int | None:
    """Number of local models, or None when no Ollama answers on loopback."""
    names = ollama_model_names(base_url, timeout)
    return None if names is None else len(names)


def grok_login_present(home: Path) -> bool:
    auth = home / ".grok" / "auth.json"
    return auth.is_file() and not auth.is_symlink()


def discover(environ: dict[str, str], home: Path, ollama_url: str) -> tuple[dict[str, bool], list[str]]:
    found = env_keys(environ)
    lines = []
    for name, variable, label in ENV_KEYS:
        lines.append(f"{'found' if found[name] else 'not found':<10} {label} ({variable} in this shell)")
    logins = detect_logins()
    found["codex_login"] = logins["codex_subscription"]
    found["claude_login"] = logins["claude_subscription"]
    found["grok_login"] = logins["grok"] and grok_login_present(home)
    lines.append(f"{'found' if found['codex_login'] else 'not found':<10} Codex ChatGPT login on this machine")
    lines.append(f"{'found' if found['claude_login'] else 'not found':<10} Claude Code subscription login on this machine")
    lines.append(f"{'found' if found['grok_login'] else 'not found':<10} Grok Build login on this machine")
    models = ollama_models(ollama_url)
    found["ollama"] = models is not None
    detail = f" ({models} model{'s' if models != 1 else ''})" if models is not None else ""
    lines.append(f"{'found' if found['ollama'] else 'not found':<10} Ollama at {ollama_url}{detail}")
    return found, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, help="write name=found|missing lines here")
    args = parser.parse_args()
    ollama_url = os.environ.get("BOSS_OLLAMA_URL", "http://127.0.0.1:11434")
    found, lines = discover(dict(os.environ), Path.home(), ollama_url)
    for line in lines:
        print(f"  {line}")
    if args.summary:
        fd = os.open(args.summary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            for name, present in found.items():
                stream.write(f"{name}={'found' if present else 'missing'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
