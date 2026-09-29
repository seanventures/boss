#!/usr/bin/env python3
"""Discover VM coding CLIs; record a subscription only on an explicit request.

The installer and worker run in different containers/accounts. Host login discovery is
informational. An operator can sign Claude Code in directly as the container's boss user,
then record that local CLI connection without passing OAuth material through this script.
The older Claude setup-token import is disabled: no provider or counsel clearance
is recorded for copying that session into BOSS.
"""
from __future__ import annotations

import argparse
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
    raise RuntimeError(
        "Claude setup-token import is disabled. No provider or counsel clearance is "
        "recorded for copying a Claude session token into BOSS. Use --connect-claude-local "
        "after claude auth login as the boss user inside the API container."
    )


def connect(api_url: str, api_key: str, provider: str) -> None:
    # Community cannot run an unmetered CLI proof turn from setup. The credential is
    # restored and the CLI login is checked by BOSS, but the first governed job must
    # establish whether this subscription can actually answer.
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/coding-agents/connect", method="POST",
        data=json.dumps({"project_id": "boss-dogfood", "actor": "ceo",
                         "provider": provider, "prove": False}).encode(),
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # Do not echo a response body: a provider error could contain private data.
        raise RuntimeError(f"BOSS connection refused {provider}: HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"BOSS connection check could not finish for {provider}") from exc
    if not result.get("connected") or not result.get("entitlement_recorded"):
        raise RuntimeError(f"BOSS did not record the {provider} connection")
    proof = result.get("proof")
    if not isinstance(proof, dict) or proof.get("skipped") is not True:
        raise RuntimeError(f"BOSS returned an unexpected {provider} proof state")
    print(f"  {provider}: connection recorded; model use remains unverified")


def claude_local_session_ready(api_url: str, api_key: str) -> None:
    """Read only BOSS's sanitized container-side status; never inspect CLI auth files."""
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/coding-agents?project_id=boss-dogfood",
        headers={"x-boss-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"BOSS Claude status refused: HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError("BOSS Claude status could not be read") from exc
    agents = body.get("agents") if isinstance(body, dict) else None
    claude = next((row for row in agents if isinstance(row, dict)
                   and row.get("name") == "claude_code"), None) if isinstance(agents, list) else None
    if not claude or claude.get("installed") is not True:
        raise RuntimeError("Claude Code is not installed in the BOSS API container")
    if claude.get("token_present"):
        raise RuntimeError("BOSS already holds a Claude setup token. Remove that legacy "
                           "credential before selecting the CLI-owned login path")
    if claude.get("authenticated") is not True or claude.get("auth_method") not in {
            "claude_ai", "oauth", "oauth_token"}:
        raise RuntimeError("Claude Code is not signed in with a subscription in the BOSS API "
                           "container. Run the provider's login as the boss user there first")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--connect-codex", action="store_true")
    parser.add_argument("--connect-claude", action="store_true")
    parser.add_argument("--connect-claude-local", action="store_true")
    parser.add_argument("--accept-database-fallback", action="store_true")
    args = parser.parse_args()
    if args.connect_claude:
        raise RuntimeError(
            "Claude setup-token import is disabled. No provider or counsel clearance is "
            "recorded for copying a Claude session token into BOSS. Use "
            "--connect-claude-local after claude auth login as the boss user inside the "
            "API container."
        )
    if args.connect_claude and args.connect_claude_local:
        raise RuntimeError("Choose one Claude connection path for this run")
    # A container-owned login has nothing to do with this VM user's CLI home. In
    # particular, do not ask the host Claude CLI to read its own session for it.
    found = ({} if args.connect_claude_local and not args.connect_codex else discover())
    if not (args.connect_codex or args.connect_claude or args.connect_claude_local):
        print("To authenticate Codex on a headless VM: codex login --device-auth")
        print("For Grok: docker compose -f scripts/install/community-compose.yml "
              "--env-file .env exec -u boss api grok login --device-auth")
        print("For Claude local login: run `claude auth login` as boss inside the API container")
        print("Then rerun the installer with --connect-claude-local; no token is copied")
        print("To import a Codex CLI auth file, use --connect-codex.")
        print("Claude setup-token import is disabled; use --connect-claude-local.")
        return 0
    api_key = os.environ.get("BOSS_API_KEY", "")
    if not api_key:
        raise RuntimeError("BOSS_API_KEY is needed to connect subscriptions")
    if args.connect_claude_local:
        claude_local_session_ready(args.api_url, api_key)
        connect(args.api_url, api_key, "claude_code")
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
