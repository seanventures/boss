#!/usr/bin/env python3
"""Discover VM coding CLIs; transfer a subscription only on an explicit request.

The installer and worker run in different containers/accounts. Host login discovery is
informational. Claude's official setup token and Codex's own auth file can be imported
through BOSS's governed credential endpoint; a Grok device session stays local to the
API/worker's shared account and is never copied or parsed here.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store_subscription_token import read_whole_file, send_to_boss  # noqa: E402


def checked_output(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout + result.stderr


def discover() -> dict[str, bool]:
    """Report only booleans; CLI status output might include private account data."""
    found = {name: bool(shutil.which(name)) for name in ("claude", "codex", "grok")}
    codex_subscription = False
    claude_subscription = False
    if found["codex"]:
        rc, output = checked_output(["codex", "login", "status"])
        codex_subscription = rc == 0 and "logged in using chatgpt" in output.lower()
    if found["claude"]:
        rc, output = checked_output(["claude", "auth", "status"])
        try:
            auth = json.loads(output)
            claude_subscription = rc == 0 and auth.get("loggedIn") is True and \
                auth.get("authMethod") in ("claude_ai", "oauth_token", "oauth")
        except (ValueError, AttributeError):
            pass
    print("Coding CLI discovery on this VM user (read only):")
    for name in ("claude", "codex", "grok"):
        print(f"  {name}: {'installed' if found[name] else 'missing'}")
    print(f"  Claude subscription login: {'detected' if claude_subscription else 'not verified'}")
    print(f"  Codex ChatGPT login: {'detected' if codex_subscription else 'not verified'}")
    print("  Grok login: check inside the BOSS API container; host status is not a container login")
    print("These checks do not transfer credentials. A laptop login is not visible to the VM.")
    return {"claude": claude_subscription, "codex": codex_subscription,
            "grok": found["grok"]}


def codex_credential() -> str:
    rc, output = checked_output(["codex", "login", "status"])
    if rc != 0 or "logged in using chatgpt" not in output.lower():
        raise RuntimeError("Sign in with ChatGPT on this VM first: codex login --device-auth")
    auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    if auth.is_symlink() or not auth.is_file():
        raise RuntimeError("Codex auth file is missing or is a symlink; sign in on this VM first")
    info = auth.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("Codex auth file must be owned by this VM user and mode 0600")
    return read_whole_file(auth)


def claude_credential() -> str:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    if not shutil.which("claude"):
        raise RuntimeError("Claude CLI missing; use --install-coding-clis first")
    print("Run `claude setup-token` in another VM terminal, then paste its token here.")
    print("The official CLI handles browser/device prompts; BOSS never displays the token.")
    try:
        with open("/dev/tty", "r"):
            pass
        token = getpass.getpass("Claude setup token (hidden): ").strip()
    except (EOFError, OSError) as exc:
        raise RuntimeError("No terminal available; supply CLAUDE_CODE_OAUTH_TOKEN in the environment") from exc
    if len(token) < 20 or any(char.isspace() for char in token) or \
            token.startswith(("http://", "https://", "<")):
        raise RuntimeError("No usable Claude setup token was entered")
    return token


def connect(api_url: str, api_key: str, provider: str) -> None:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/coding-agents/connect", method="POST",
        data=json.dumps({"project_id": "boss-dogfood", "actor": "ceo",
                         "provider": provider, "prove": True}).encode(),
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # Do not echo a response body: a provider error could contain private data.
        raise RuntimeError(f"BOSS proof refused {provider}: HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"BOSS proof could not finish for {provider}") from exc
    if not result.get("connected"):
        raise RuntimeError(f"BOSS did not report {provider} connected")
    print(f"  {provider}: connected and proved by BOSS")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--connect-codex", action="store_true")
    parser.add_argument("--connect-claude", action="store_true")
    parser.add_argument("--accept-database-fallback", action="store_true")
    args = parser.parse_args()
    found = discover()
    if not (args.connect_codex or args.connect_claude):
        print("To authenticate Codex on a headless VM: codex login --device-auth")
        print("For Grok: docker compose -f scripts/install/community-compose.yml "
              "--env-file .env exec -u boss api grok login --device-auth")
        print("For Claude: claude login, then claude setup-token in a VM terminal")
        print("To bind Codex/Claude, rerun with --connect-codex or --connect-claude explicitly.")
        return 0
    api_key = os.environ.get("BOSS_API_KEY", "")
    if not api_key:
        raise RuntimeError("BOSS_API_KEY is needed to connect subscriptions")
    for provider, wanted in (("codex", args.connect_codex),
                             ("claude_code", args.connect_claude)):
        if not wanted:
            continue
        if provider == "codex" and not found["codex"]:
            raise RuntimeError("Codex ChatGPT login was not detected on this VM")
        credential = codex_credential() if provider == "codex" else claude_credential()
        send_to_boss(args.api_url, api_key, "boss-dogfood", provider, credential,
                     accept_database_fallback=args.accept_database_fallback)
        print(f"  {provider}: credential stored by BOSS (value never displayed)")
        connect(args.api_url, api_key, provider)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Coding subscription setup: {error}", file=sys.stderr)
        raise SystemExit(1) from None
