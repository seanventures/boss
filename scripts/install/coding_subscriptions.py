#!/usr/bin/env python3
"""Sign a coding subscription in inside BOSS, then prove it; discover VM coding CLIs read only.

THE ONE SUBSCRIPTION ROUTE (BOSSAPP-769). Codex, Claude Code and Grok Build are authorised by
their own CLI login, held inside the BOSS API container as its boss user. `--sign-in PROVIDER`
asks BOSS to start that provider's own login there (POST /v1/setup/sign-in), prints the link
and code the login printed, waits while the person finishes it in a browser (a code is asked
for on the terminal only when the login wants one pasted back), then connects it with POST
/v1/setup/models (kind subscription, no credential), which proves it with one governed turn.
Nothing is pasted, copied or imported: no token, no auth.json.

Host login discovery is informational only. None of the discovery calls a model.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from boss_refusal import from_body  # noqa: E402

PROJECT = "boss-dogfood"
#: boss.sh's provider names -> BOSS's.
PROVIDERS = {"codex": "codex", "claude": "claude_code", "claude_code": "claude_code",
             "grok": "grok"}
LABELS = {"codex": "Codex", "claude_code": "Claude Code", "grok": "Grok Build"}
#: The `boss connect` word for each, for the next step a person is told.
COMMAND = {"codex": "codex", "claude_code": "claude", "grok": "grok"}
POLL_SECONDS = 2.0


def refusal(exc: urllib.error.HTTPError) -> str:
    """BOSS's own words for a refusal ("headline — fix"), else the status. Never `None`."""
    try:
        body = exc.read()
    except OSError:
        body = b""
    return from_body(body) or f"HTTP {exc.code}"


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


#: Pill states that mean the CLI's session inside BOSS was refused (setup_pills).
LOGIN_REFUSED = frozenset({"login_expired", "key_rejected"})


class LoginRefused(RuntimeError):
    """Connect's proof was refused for the subscription's LOGIN: a new sign-in is the fix."""


def _refusal_state(body: bytes) -> str:
    try:
        detail = json.loads((body or b"{}").decode("utf-8", "replace")).get("detail")
    except (ValueError, AttributeError):
        return ""
    return str(detail.get("state") or "") if isinstance(detail, dict) else ""


def connect(api_url: str, api_key: str, provider: str) -> None:
    # PROVED, NOT ASSUMED. Repo and models' one connect (POST /v1/setup/models) runs one trivial
    # turn on the subscription — reserved and receipted like any model call — before it calls
    # it connected, and answers with the whole setup view.
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v1/setup/models", method="POST",
        data=json.dumps({"project_id": PROJECT, "actor": "ceo",
                         "provider": provider, "kind": "subscription"}).encode(),
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            view = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        # BOSS's own refusal is one pill — what is wrong and the fix — and never contains the
        # credential. Nothing is added to it.
        try:
            body = exc.read()
        except OSError:
            body = b""
        said = f"BOSS did not connect {provider}: {from_body(body) or f'HTTP {exc.code}'}"
        if _refusal_state(body) in LOGIN_REFUSED:
            raise LoginRefused(said) from None
        raise RuntimeError(said) from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"BOSS connection check could not finish for {provider}") from exc
    row = next((m for m in view.get("models") or [] if m.get("provider") == provider), None)
    if row is None:
        raise RuntimeError(f"BOSS did not record the {provider} connection")
    pill = row.get("pill") or {}
    if pill.get("state") in LOGIN_REFUSED:
        raise LoginRefused(f"BOSS did not prove the {provider} connection with a live turn: "
                           f"{pill.get('label') or 'its login was refused'}")
    if pill.get("state") != "working":
        raise RuntimeError(f"BOSS did not prove the {provider} connection with a live turn")
    print(f"  {provider}: connected — it answered a test turn")


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
    # A token BOSS's own Sign in obtained (`long_lived_token`) is the subscription, not legacy.
    if claude.get("legacy_token") or (claude.get("token_present")
                                      and not claude.get("long_lived_token")):
        raise RuntimeError("BOSS already holds a legacy Claude token. Remove that legacy "
                           "credential before selecting the CLI-owned login path")
    if claude.get("authenticated") is not True or claude.get("auth_method") not in {
            "claude_ai", "oauth", "oauth_token", "long_lived_token"}:
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
    name = PROVIDERS[provider]
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
            row.get("auth_method") not in {"claude_ai", "oauth", "oauth_token", "long_lived_token"}:
        return False
    return row.get("authenticated") is True


# ---- the one subscription route: sign in inside BOSS, then prove ----------------------------

def _call(api_url: str, api_key: str, method: str, path: str,
          body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-boss-api-key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(refusal(exc)) from None
    except (OSError, ValueError) as exc:
        raise RuntimeError("BOSS did not answer; is it running? Try: boss status") from exc
    return answer if isinstance(answer, dict) else {}


def _terminal():
    """The person's terminal, or None (a pipe, CI, an unattended install)."""
    try:
        return open("/dev/tty", "r")  # noqa: SIM115 — held for the session's prompts
    except OSError:
        return None


class NeedsPerson(RuntimeError):
    """The sign-in needs a person at a terminal; nothing was changed."""


def sign_in(api_url: str, api_key: str, provider: str, *, out=None,
            terminal=None, poll_seconds: float = POLL_SECONDS, fresh: bool = False) -> None:
    """Sign `provider` in inside BOSS through its own login. Returns once it is signed in.
    `fresh`: sign the current session out first and log in again, whatever the CLI says."""
    out = out or sys.stdout
    label, word = LABELS[provider], COMMAND[provider]
    path = "/v1/setup/sign-in"
    session = _call(api_url, api_key, "POST", path,
                    {"project_id": PROJECT, "actor": "ceo", "provider": provider,
                     **({"fresh": True} if fresh else {})})
    if session.get("state") == "signed_in":
        print(f"  {label} is already signed in inside BOSS.", file=out)
        return
    sign_in_id = str(session.get("sign_in_id") or "")
    where = f"{path}/{sign_in_id}?project_id={PROJECT}"
    tty = terminal if terminal is not None else _terminal()
    if session.get("state") in ("starting", "waiting") and tty is None:
        _cancel(api_url, api_key, where)
        raise NeedsPerson(f"{label} sign-in needs a person at a terminal. "
                          f"Run: boss connect {word}")
    print(f"Signing {label} in inside BOSS. {label} runs its own login there; "
          "nothing is copied from this machine.", file=out)
    shown = False
    asked = False
    try:
        while session.get("state") in ("starting", "waiting"):
            if not shown and session.get("verification_url"):
                print(f"  1. Open  {session['verification_url']}", file=out)
                if session.get("user_code"):
                    print(f"  2. Enter the code  {session['user_code']}", file=out)
                elif session.get("needs_code_input"):
                    print("  2. Sign in, then paste the code the page shows below.", file=out)
                print("  Waiting for the sign-in (it expires in 15 minutes; Ctrl-C cancels)...",
                      file=out)
                out.flush()
                shown = True
            if session.get("needs_code_input") and not asked:
                asked = True
                code = _ask_code(tty)
                if not code:
                    _cancel(api_url, api_key, where)
                    raise RuntimeError(f"No code was entered; the {label} sign-in was "
                                       f"cancelled. Retry: boss connect {word}")
                session = _call(api_url, api_key, "POST", f"{path}/{sign_in_id}/code",
                                {"project_id": PROJECT, "code": code})
                continue
            time.sleep(poll_seconds)
            session = _call(api_url, api_key, "GET", where)
    except KeyboardInterrupt:
        _cancel(api_url, api_key, where)
        raise RuntimeError(f"{label} sign-in cancelled. Retry: boss connect {word}") from None
    if session.get("state") != "signed_in":
        raise RuntimeError(f"{session.get('headline') or 'Sign-in failed'} — "
                           f"{session.get('fix') or f'Retry: boss connect {word}'}")
    print(f"  ✓ {label} is signed in inside BOSS", file=out)


def _ask_code(tty) -> str:
    try:
        return getpass.getpass("  Code from the sign-in page (hidden): ", stream=tty).strip() \
            if hasattr(tty, "fileno") else ""
    except (EOFError, OSError):
        return ""


def _cancel(api_url: str, api_key: str, where: str) -> None:
    try:
        _call(api_url, api_key, "DELETE", where)
    except RuntimeError:
        pass


def sign_in_and_connect(api_url: str, api_key: str, provider: str, *, out=None,
                        terminal=None, poll_seconds: float = POLL_SECONDS) -> None:
    """Sign in, connect, prove. A proof refused for the LOGIN means the CLI's session is dead
    however `<cli> login status` reports it: sign it out, sign in again, and connect once more —
    never advice that sends the person back to this same command."""
    out = out or sys.stdout
    sign_in(api_url, api_key, provider, out=out, terminal=terminal, poll_seconds=poll_seconds)
    try:
        connect(api_url, api_key, provider)
        return
    except LoginRefused as refused:
        print(f"  {refused}", file=out)
        print(f"  The {LABELS[provider]} login inside BOSS was refused; signing it out and "
              "signing in again.", file=out)
    sign_in(api_url, api_key, provider, out=out, terminal=terminal, poll_seconds=poll_seconds,
            fresh=True)
    try:
        connect(api_url, api_key, provider)
    except LoginRefused as refused:
        raise RuntimeError(f"{refused}. The new sign-in was refused too: check the account "
                           f"has a {LABELS[provider]} plan.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--sign-in", choices=("codex", "claude", "grok"),
                        help="sign that subscription in inside BOSS through its own login "
                             "(prints the link and code), then connect and prove it")
    parser.add_argument("--connect-codex", action="store_true",
                        help="the same as --sign-in codex")
    parser.add_argument("--connect-claude", action="store_true",
                        help="the same as --sign-in claude")
    parser.add_argument("--connect-claude-local", action="store_true",
                        help="record the Claude Code login already made inside BOSS")
    parser.add_argument("--connect-grok-local", action="store_true",
                        help="record the Grok Build login already made inside BOSS")
    parser.add_argument("--local-status", choices=("codex", "claude", "grok"),
                        help="exit 0 when that CLI is already signed in inside the BOSS API "
                             "container, 3 when it is not, 4 when BOSS cannot say; prints nothing")
    args = parser.parse_args()
    if args.local_status:
        api_key = os.environ.get("BOSS_API_KEY", "")
        state = local_session_signed_in(args.api_url, api_key, args.local_status) \
            if api_key else None
        return 0 if state is True else 3 if state is False else 4
    wanted = [PROVIDERS[args.sign_in]] if args.sign_in else []
    if args.connect_codex and "codex" not in wanted:
        wanted.append("codex")
    if args.connect_claude and "claude_code" not in wanted:
        wanted.append("claude_code")
    if args.connect_claude and args.connect_claude_local:
        raise RuntimeError("Choose one Claude connection path for this run")
    if not (wanted or args.connect_claude_local or args.connect_grok_local):
        discover()
        print("To connect a subscription, sign it in inside BOSS (its own login; nothing is "
              "copied): boss connect codex | boss connect claude | boss connect grok")
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
    for provider in wanted:
        sign_in_and_connect(args.api_url, api_key, provider)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NeedsPerson as error:
        print(f"  next      {error}")
        raise SystemExit(2) from None
    except RuntimeError as error:
        print(f"Coding subscription setup: {error}", file=sys.stderr)
        raise SystemExit(1) from None
