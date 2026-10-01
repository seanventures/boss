#!/usr/bin/env python3
"""Find the model keys, subscriptions, coding CLIs, and Ollama already on this machine. Read only.

The installer and `boss connect` use this to report what exists before asking to connect
anything. It checks presence only:

- a provider key exported in this process's environment (the value is never read into output)
- a Codex ChatGPT login or a Claude Code subscription login, through each CLI's own status
  command (coding_subscriptions.detect_logins)
- a Grok Build login file under ~/.grok (existence only; the file is never opened)
- the claude, codex, grok, and ollama programs on PATH
- an Ollama server on this machine (loopback, or an address of this machine such as the
  Docker bridge), and the models it has already pulled

Nothing is stored, sent, or connected here. Output is found / not found per source, and an
optional KEY=found|missing summary file for the shell. Exit status is 0 whatever is found.

`--ollama-models URL` prints the model names one per line and exits 1 when nothing answers.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
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
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def env_keys(environ: dict[str, str]) -> dict[str, bool]:
    return {name: bool(environ.get(variable, "").strip()) for name, variable, _ in ENV_KEYS}


def is_this_machine(host: str | None) -> bool:
    """Loopback, or an IP literal assigned to this machine (for example the Docker bridge).

    Discovery must never probe another computer. An address this machine can bind to is one of
    its own interfaces; a hostname other than localhost is refused outright.
    """
    if not host:
        return False
    if host in LOOPBACK:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_unspecified or address.is_multicast:
        return False
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
    except OSError:
        return False
    return True


def ollama_model_names(base_url: str, timeout: float = 1.5) -> list[str] | None:
    """Names of the local models, or None when no Ollama answers on this machine.

    A name that is not a plain model tag is counted but not returned, so nothing odd from a
    local server reaches a terminal or a request.
    """
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or not is_this_machine(parsed.hostname):
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
    """Number of local models, or None when no Ollama answers on this machine."""
    names = ollama_model_names(base_url, timeout)
    return None if names is None else len(names)


def grok_login_present(home: Path) -> bool:
    auth = home / ".grok" / "auth.json"
    return auth.is_file() and not auth.is_symlink()


def _line(found: bool, text: str, missing: str = "not found") -> str:
    return f"{'found' if found else missing:<10} {text}"


def discover(environ: dict[str, str], home: Path, ollama_url: str) -> tuple[dict[str, bool], list[str]]:
    found = env_keys(environ)
    lines = []
    for name, variable, label in ENV_KEYS:
        lines.append(_line(found[name], f"{label} ({variable} in this shell)"))
    logins = detect_logins()
    found["codex_login"] = logins["codex_subscription"]
    found["claude_login"] = logins["claude_subscription"]
    found["grok_login"] = logins["grok"] and grok_login_present(home)
    lines.append(_line(found["codex_login"], "Codex ChatGPT login on this machine"))
    lines.append(_line(found["claude_login"], "Claude Code subscription login on this machine"))
    lines.append(_line(found["grok_login"], "Grok Build login on this machine"))
    for cli, label in (("claude", "Claude Code"), ("codex", "Codex"), ("grok", "Grok Build")):
        found[f"{cli}_cli"] = bool(logins.get(cli))
    clis = [label for cli, label in (("claude", "Claude Code"), ("codex", "Codex CLI"),
                                      ("grok", "Grok Build")) if found[f"{cli}_cli"]]
    lines.append(_line(bool(clis), "Coding CLIs on PATH" + (f": {', '.join(clis)}" if clis else "")))
    names = ollama_model_names(ollama_url)
    found["ollama"] = names is not None
    found["ollama_installed"] = bool(shutil.which("ollama"))
    found["ollama_models"] = bool(names)
    if names is not None:
        usable = [name for name in names if name]
        listed = f": {', '.join(usable[:5])}{' ...' if len(usable) > 5 else ''}" if usable else ""
        detail = f" ({len(names)} model{'s' if len(names) != 1 else ''} pulled{listed})"
        lines.append(_line(True, f"Ollama at {ollama_url}{detail}"))
    elif found["ollama_installed"]:
        lines.append(_line(False, f"Ollama is installed but not answering at {ollama_url}",
                           missing="stopped"))
    else:
        lines.append(_line(False, f"Ollama at {ollama_url}"))
    return found, lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, help="write name=found|missing lines here")
    parser.add_argument("--ollama-models", metavar="URL",
                        help="print the models of the Ollama at URL; exit 1 when none answers")
    args = parser.parse_args()
    if args.ollama_models:
        names = ollama_model_names(args.ollama_models, timeout=3)
        if names is None:
            return 1
        for name in names:
            if name:
                print(name)
        return 0
    ollama_url = os.environ.get("BOSS_OLLAMA_URL") or DEFAULT_OLLAMA_URL
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
