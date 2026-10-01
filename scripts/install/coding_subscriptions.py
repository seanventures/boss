#!/usr/bin/env python3
"""Discover VM coding CLIs; record a subscription only on an explicit request.

The installer and worker run in different containers/accounts. Host login discovery is
informational. An operator can sign Claude Code in directly as the container's boss user,
then record that local CLI connection without passing OAuth material through this script.
An explicit --connect-claude imports a setup token the operator supplies and stores it
only in the local secret store. Codex auth-file import and Grok's CLI login stay separate.
None of these setup actions call a model.
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
try:
    from store_subscription_token import read_whole_file, send_to_boss  # noqa: E402
except ImportError as _import_error:  # e.g. a host Python older than the engine needs
    # Discovery (detect_logins) must still work; only a credential import needs the engine.
    _IMPORT_PROBLEM = f"{type(_import_error).__name__}: {_import_error}"

    def read_whole_file(path: Path) -> str:
        raise RuntimeError("This machine's python3 cannot load the BOSS credential helper "
                           f"({_IMPORT_PROBLEM}). It needs a BOSS checkout and Python 3.11 "
                           "or newer. Nothing was stored.")

    def send_to_boss(*_args, **_kwargs):
        read_whole_file(Path())


def checked_output(argv: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout + result.stderr


def detect_logins() -> dict[str, bool]:
    """Booleans only, nothing printed; CLI status output might include private account data."""
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
    return {**found, "claude_subscription": claude_subscription,
            "codex_subscription": codex_subscription}


def discover() -> dict[str, bool]:
    """Report only booleans; CLI status output might include private account data."""
    logins = detect_logins()
    found = {name: logins[name] for name in ("claude", "codex", "grok")}
    codex_subscription = logins["codex_subscription"]
    claude_subscription = logins["claude_subscription"]
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
    """Operator-supplied setup token. Never print it. Empty input is an error."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    if not shutil.which("claude"):
        raise RuntimeError(
            "No Claude setup token was provided, and the Claude CLI is missing. "
            "Set CLAUDE_CODE_OAUTH_TOKEN or install the CLI with --install-coding-clis. "
            "Nothing was stored."
        )
    print("Run `claude setup-token` in another terminal, then paste its token here.")
    print("The official CLI handles browser/device prompts; BOSS never displays the token.")
    try:
        with open("/dev/tty", "r"):
            pass
        token = getpass.getpass("Claude setup token (hidden): ").strip()
    except (EOFError, OSError) as exc:
        raise RuntimeError(
            "No Claude setup token was provided and no terminal is available. "
            "Set CLAUDE_CODE_OAUTH_TOKEN for this command. Nothing was stored."
        ) from exc
    if len(token) < 16 or any(char.isspace() for char in token) or \
            token.startswith(("http://", "https://", "<")):
        raise RuntimeError("No usable Claude setup token was entered. Nothing was stored.")
    return token


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


def grok_local_session_ready(api_url: str, api_key: str) -> None:
    """Read sanitized container-side Grok status. Do not open auth.json or call a model."""
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/coding-agents?project_id=boss-dogfood",
        headers={"x-boss-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"BOSS Grok status refused: HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError("BOSS Grok status could not be read") from exc
    agents = body.get("agents") if isinstance(body, dict) else None
    grok = next((row for row in agents if isinstance(row, dict)
                 and row.get("name") == "grok"), None) if isinstance(agents, list) else None
    if not grok or grok.get("installed") is not True:
        raise RuntimeError(
            "Grok is not installed in the BOSS API container. Nothing was recorded. "
            "Install it, then run grok login --device-auth as the boss user."
        )
    if grok.get("authenticated") is not True:
        raise RuntimeError(
            "Grok is not signed in inside the BOSS API container. "
            "Run grok login --device-auth as the boss user, then rerun with "
            "--connect-grok-local. Nothing was recorded."
        )


def local_session_signed_in(api_url: str, api_key: str, provider: str) -> bool | None:
    """Is the CLI already signed in inside the BOSS API container? None when BOSS cannot say.

    Reads only BOSS's sanitized status; never opens a CLI auth file or calls a model.
    """
    name = {"claude": "claude_code", "grok": "grok"}[provider]
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/coding-agents?project_id=boss-dogfood",
        headers={"x-boss-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode() or "{}")
    except (OSError, ValueError):
        return None
    agents = body.get("agents") if isinstance(body, dict) else None
    row = next((item for item in agents if isinstance(item, dict)
                and item.get("name") == name), None) if isinstance(agents, list) else None
    if not row or row.get("installed") is not True:
        return None
    if provider == "claude" and row.get("authenticated") is True and \
            row.get("auth_method") not in {"claude_ai", "oauth", "oauth_token"}:
        return False
    return row.get("authenticated") is True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--connect-codex", action="store_true")
    parser.add_argument("--connect-claude", action="store_true")
    parser.add_argument("--connect-claude-local", action="store_true")
    parser.add_argument("--connect-grok-local", action="store_true")
    parser.add_argument("--accept-database-fallback", action="store_true")
    parser.add_argument("--local-status", choices=("claude", "grok"),
                        help="exit 0 when that CLI is already signed in inside the BOSS API "
                             "container, 3 when it is not, 4 when BOSS cannot say; prints nothing")
    args = parser.parse_args()
    if args.local_status:
        api_key = os.environ.get("BOSS_API_KEY", "")
        state = local_session_signed_in(args.api_url, api_key, args.local_status) \
            if api_key else None
        return 0 if state is True else 3 if state is False else 4
    if args.connect_claude and args.connect_claude_local:
        raise RuntimeError("Choose one Claude connection path for this run")
    # A container-owned login has nothing to do with this VM user's CLI home. In
    # particular, do not ask the host Claude CLI to read its own session for it.
    local_only = ((args.connect_claude_local or args.connect_grok_local)
                  and not args.connect_codex and not args.connect_claude)
    found = {} if local_only else discover()
    if not (args.connect_codex or args.connect_claude or args.connect_claude_local
            or args.connect_grok_local):
        print("To authenticate Codex on a headless VM: codex login --device-auth")
        print("Then import that CLI auth file with --connect-codex.")
        print("For Grok: docker compose -f scripts/install/community-compose.yml "
              "--env-file .env exec -u boss api grok login --device-auth")
        print("Then rerun the installer with --connect-grok-local. "
              "BOSS does not copy auth.json and does not call the model.")
        print("For Claude local login: run `claude auth login` as boss inside the API container")
        print("Then rerun the installer with --connect-claude-local; no token is copied "
              "and no model call is made.")
        print("To import a Claude setup token you supply, rerun with --connect-claude. "
              "It is stored encrypted, with its key outside BOSS's database.")
        return 0
    api_key = os.environ.get("BOSS_API_KEY", "")
    if not api_key:
        raise RuntimeError("BOSS_API_KEY is needed to connect subscriptions")
    if args.connect_claude_local:
        claude_local_session_ready(args.api_url, api_key)
        connect(args.api_url, api_key, "claude_code")
    if args.connect_grok_local:
        grok_local_session_ready(args.api_url, api_key)
        connect(args.api_url, api_key, "grok")
    for provider, wanted in (("codex", args.connect_codex),
                             ("claude_code", args.connect_claude)):
        if not wanted:
            continue
        if provider == "codex" and not found["codex"]:
            raise RuntimeError("Codex ChatGPT login was not detected on this VM")
        credential = codex_credential() if provider == "codex" else claude_credential()
        fallback = False if provider == "claude_code" else args.accept_database_fallback
        result = send_to_boss(args.api_url, api_key, "boss-dogfood", provider, credential,
                              accept_database_fallback=fallback)
        # THE SERVER DECIDES WHERE A CLAUDE TOKEN MAY LIVE (setup_service.
        # store_coding_agent_credential): a secret store, or the database store only when its
        # key is outside the database. This demanded "local_vault" exactly, so a token the
        # server had correctly stored was reported as a failure and the connection was never
        # recorded. Only a reply that does not say where the token went is refused.
        if provider == "claude_code" and not result.get("backend"):
            raise RuntimeError(
                "BOSS did not confirm where it stored the Claude setup token. "
                "Nothing else was accepted."
            )
        print(f"  {provider}: credential stored by BOSS (value never displayed)")
        connect(args.api_url, api_key, provider)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Coding subscription setup: {error}", file=sys.stderr)
        raise SystemExit(1) from None
