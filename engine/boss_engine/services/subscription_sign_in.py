"""Sign a subscription CLI in INSIDE the BOSS API container — the one supported route.

Codex, Claude Code and Grok Build are authorised by their own CLI login, held in the service
user's CLI home (/home/boss/.codex, .claude, .grok — volumes the worker shares). Nothing is ever
pasted, dropped or imported into BOSS: this module starts the provider's OWN login as the API
process user and hands the person the two things that login prints for them — the verification
URL and the one-time code. They finish it in their browser; the CLI writes its own session.
Connect (POST /v1/setup/models, kind subscription, no credential) then proves it with one
governed turn.

Observed against the CLIs in the image (throwaway homes, 2026-10-24):

- `codex login --device-auth` (0.158) prints https://auth.openai.com/codex/device and a
  `XXXX-XXXXX` code "(expires in 15 minutes)", then waits; exit 0 once the browser approves.
- `grok login --device-auth` (1.0.41) prints https://accounts.x.ai/oauth2/device?user_code=…
  and the same code on its own line, then "Waiting for authorization...".
- Claude Code runs `claude setup-token` (2.1.197; no flags but --help), NOT `claude auth login`
  (2026-10-27). `auth login` wrote the CLI's short-lived login (~8 h access token + a rotating
  refresh token) and BOSS's login was wiped twice when a refresh failed. `setup-token` mints a
  LONG-LIVED (1-year) inference token and writes nothing of its own. It is an Ink app, so it
  needs a terminal: it runs on a pseudo-terminal here (a pipe fails "Raw mode is not
  supported"). It prints "Browser didn't open? Use the url below to sign in" and an
  https://claude.com/cai/oauth/authorize?… URL, then "Paste code here if prompted >". The code
  the browser shows is typed into it (then Enter). A wrong code prints "OAuth error: Request
  failed with status code 400 … Press Enter to retry." and waits — BOSS ends it as failed. A
  good one prints "Long-lived authentication token created successfully!", "Your OAuth token
  (valid for 1 year):" and the `sk-ant-oat01-…` token on its own line, then exits 0. BOSS
  takes the token out of that output on the server and hands it to `store` (the secret store,
  claude_credential.store_long_lived_token); it is never returned, logged or kept here.

Only the URL and the code leave this module: the rest of the CLI's output may hold account data
(and, for Claude, the token) and is never returned, logged or persisted. One live session per
provider per install (the API runs one worker process — apps/api/serve.py), killed on cancel,
after 15 minutes, and on API shutdown.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

#: How long a started sign-in may wait for the person (the providers' own codes last 15 min).
SESSION_TTL_SECONDS = 15 * 60
#: How long a finished session can still be read by id.
FINISHED_RETENTION_SECONDS = 60 * 60
#: The most CLI output kept in memory while looking for the URL and code. Never returned.
_OUTPUT_CAP = 32 * 1024
#: A code pasted back for the CLI: one line, a sane length.
_CODE_MAX = 512

PROVIDERS = ("codex", "claude_code", "grok")
_ALIASES = {"claude": "claude_code", "claude-code": "claude_code", "grok_build": "grok",
            "grok-build": "grok"}

LIVE = ("starting", "waiting")


@dataclass(frozen=True)
class _Login:
    label: str
    argv: tuple[str, ...]
    #: Hosts a printed verification URL may point at (anything else is never shown).
    url_hosts: tuple[str, ...]
    #: True: the person types the printed code in the browser. False: the browser shows a code
    #: the person pastes back here (Claude).
    device_code: bool
    #: Variables that would make the CLI use something other than its own login.
    drop_env: tuple[str, ...] = ()
    #: The CLI's own sign-out, run before a fresh sign-in replaces a refused session.
    logout: tuple[str, ...] = ()
    #: Runs on a pseudo-terminal (an Ink app that refuses a pipe).
    pty: bool = False
    #: The login PRINTS a long-lived token instead of writing a session: BOSS takes it from the
    #: output and hands it to the session's `store`. Success is "stored", not "exit 0".
    captures_token: bool = False


LOGINS: dict[str, _Login] = {
    "codex": _Login("Codex", ("codex", "login", "--device-auth"),
                    ("auth.openai.com", "chatgpt.com", "openai.com"), True,
                    ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"), ("codex", "logout")),
    "grok": _Login("Grok Build", ("grok", "login", "--device-auth"),
                   ("accounts.x.ai", "x.ai", "grok.com"), True,
                   ("XAI_API_KEY", "GROK_MODELS_BASE_URL", "GROK_CLI_CHAT_PROXY_BASE_URL"),
                   ("grok", "logout")),
    # No sign-out: setup-token never touches the CLI's own login (the fallback), and the token
    # it prints replaces the stored one.
    "claude_code": _Login("Claude Code", ("claude", "setup-token"),
                          ("claude.com", "claude.ai", "anthropic.com"), False,
                          ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
                          (), pty=True, captures_token=True),
}

#: Escape sequences become a space: an Ink app moves the cursor (`ESC[23G`) where text has a
#: space, so deleting them would glue words — and a token to its neighbours.
_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[78=>]")
_URL = re.compile(r"https://[^\s<>\"'\x1b]+")
#: The device codes both CLIs print: `ABCD-EFGH1` (letters and digits around one dash).
_DEVICE_CODE = re.compile(r"(?m)^\s*([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\s*$")
_PASTE_PROMPT = re.compile(r"(?i)paste\s+(?:the\s+)?code")
#: The long-lived token `claude setup-token` prints — whole, so followed by whitespace (a
#: token cut at a read boundary is never taken).
_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])sk-ant-[A-Za-z0-9_\-]{40,}(?=\s)")
#: "(valid for 1 year)" / "(valid for 30 days)".
_VALID_FOR = re.compile(r"(?i)valid\s+for\s+(\d+)\s*(year|month|day)s?")
#: What setup-token says when the pasted code was refused (it then waits for Enter to retry).
_LOGIN_REFUSED = re.compile(r"(?i)oauth\s+error|login\s+failed|invalid\s+code")
#: The pseudo-terminal's size: wide, so a URL or token is never wrapped across lines.
_PTY_COLS, _PTY_ROWS = 1000, 50


class SignInError(RuntimeError):
    """A refusal shaped like SetupActionError: {state, headline, fix} with an HTTP status."""

    def __init__(self, status: int, headline: str, fix: str, state: str = "failed"):
        super().__init__(headline)
        self.status = status
        self.detail = {"state": state, "headline": headline, "fix": fix}


class StoreRefused(RuntimeError):
    """A `store` that refused the token for a reason the person can act on. Its message is
    shown, so it must be BOSS's own words — never anything derived from the value."""


def _iso(at: Optional[float]) -> Optional[str]:
    if at is None:
        return None
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Session:
    sign_in_id: str
    provider: str
    project_id: str
    actor: str
    started: float
    expires: Optional[float]
    state: str = "starting"
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    needs_code_input: bool = False
    code_sent: bool = False
    exit_code: Optional[int] = None
    reason: str = ""
    finished: Optional[float] = None
    process: Optional[subprocess.Popen] = None
    output: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: The pseudo-terminal's controlling side (a `pty` login): read for output, written for the
    #: code. None for a pipe login (process.stdout / process.stdin).
    terminal: Any = field(default=None, repr=False)
    #: Where a captured token goes (`captures_token`): called once with the value, on the
    #: server. Never kept on the session after it returns.
    store: Optional[Callable[..., Any]] = field(default=None, repr=False)
    token_stored: bool = False
    reader: Optional[threading.Thread] = field(default=None, repr=False)


_SESSIONS: dict[str, _Session] = {}
_LOCK = threading.Lock()


def normalize_provider(provider: str) -> str:
    name = str(provider or "").strip().lower()
    name = _ALIASES.get(name, name)
    if name not in LOGINS:
        raise SignInError(422, "Not a subscription",
                          "Sign-in works for codex, claude_code and grok.")
    return name


# ---- what the person sees -------------------------------------------------------------------

def _words(session: _Session) -> tuple[str, str]:
    login = LOGINS[session.provider]
    label = login.label
    if session.state == "starting":
        return (f"Starting {label} sign-in",
                f"BOSS is starting {label}'s own login inside the BOSS server.")
    if session.state == "waiting":
        if session.needs_code_input and login.captures_token:
            return ("Open the link and sign in",
                    f"Sign in to {label} at the link, then paste the code it shows you here. "
                    f"{label} then gives BOSS a long-lived subscription token on the BOSS "
                    "server; it is stored there and never shown.")
        if session.needs_code_input:
            return ("Open the link and sign in",
                    f"Sign in to {label} at the link, then paste the code it shows you here. "
                    f"{label}'s own login inside BOSS reads it; BOSS never keeps it.")
        if session.code_sent:
            return ("Finishing sign-in", f"{label} is checking the code inside BOSS.")
        return ("Open the link and enter the code",
                f"Sign in to {label} at the link and enter the code there. "
                "This page updates when it is done.")
    if session.state == "signed_in":
        return (f"{label} is signed in inside BOSS",
                "Press Connect to prove it with one test question.")
    if session.state == "expired":
        return ("Sign-in expired",
                "The link was not used within 15 minutes. Start the sign-in again.")
    if session.state == "cancelled":
        return ("Sign-in cancelled", "Start it again when you are ready.")
    return ("Sign-in failed", session.reason or
            f"{label}'s login inside BOSS did not finish. Start the sign-in again.")


def view(session: _Session) -> dict[str, Any]:
    headline, fix = _words(session)
    live = session.state in LIVE
    return {
        "sign_in_id": session.sign_in_id,
        "provider": session.provider,
        "state": session.state,
        "verification_url": session.verification_url if live else None,
        "user_code": session.user_code if live else None,
        "needs_code_input": bool(live and session.needs_code_input and not session.code_sent),
        "expires_at": _iso(session.expires) if live else None,
        "headline": headline,
        "fix": fix,
    }


# ---- the provider's own login, as the API process user --------------------------------------

def _login_env(login: _Login) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in login.drop_env}
    # No colours, no pager, no attempt to open a browser on a server that has none.
    env.update(NO_COLOR="1", TERM="dumb", BROWSER="true", CI="1")
    if login.pty:
        # An Ink app on a terminal: a real TERM, and NOT "CI" — Ink in CI renders only its
        # last frame, at exit, so the link would never be shown while it waits.
        env.pop("CI", None)
        env.update(TERM="xterm-256color", COLUMNS=str(_PTY_COLS), LINES=str(_PTY_ROWS))
    return env


def _parse(session: _Session) -> None:
    """Pick the verification URL and code out of what the CLI printed. Nothing else is kept."""
    login = LOGINS[session.provider]
    text = _clean(session.output)
    if session.verification_url is None:
        for match in _URL.finditer(text):
            url = match.group(0).rstrip(".,;)")
            host = re.sub(r"^https://([^/:?#]+).*$", r"\1", url).lower()
            if any(host == h or host.endswith("." + h) for h in login.url_hosts):
                session.verification_url = url[:2048]
                break
    if login.device_code:
        if session.user_code is None and session.verification_url is not None:
            after = text[text.find("https://"):]
            code = _DEVICE_CODE.search(after)
            if code:
                session.user_code = code.group(1)
        ready = session.verification_url is not None and session.user_code is not None
    else:
        if _PASTE_PROMPT.search(text):
            session.needs_code_input = True
        ready = session.verification_url is not None
        if ready:
            session.needs_code_input = True
    if ready and session.state == "starting":
        session.state = "waiting"


def _clean(output: str) -> str:
    return _ANSI.sub(" ", output).replace("\r", "")


def _take_token(session: _Session) -> Optional[tuple[str, Optional[int]]]:
    """The whole token a `captures_token` login printed, and how many days it says it lasts —
    or None. The caller holds session.lock; nothing here keeps the value."""
    if not LOGINS[session.provider].captures_token or session.token_stored:
        return None
    text = _clean(session.output)
    match = _TOKEN.search(text)
    if match is None:
        return None
    days = None
    valid = _VALID_FOR.search(text)
    if valid:
        n, unit = int(valid.group(1)), valid.group(2).lower()
        days = n * (365 if unit == "year" else 30 if unit == "month" else 1)
    return match.group(0), days


def _store_token(session: _Session, token: str, days: Optional[int]) -> None:
    """Hand the captured token to the session's store, on the server, then end the session.
    The value is never logged, returned or kept; a store that fails is named by type only."""
    label = LOGINS[session.provider].label
    error = ""
    if session.store is None:
        error = "BOSS has no store for the token in this process"
    else:
        try:
            session.store(token, valid_days=days)
        except StoreRefused as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — never echo: a message could carry the value
            error = f"{type(exc).__name__}. Start the sign-in again."
    del token
    with session.lock:
        session.output = ""
        if error:
            _finish(session, "failed",
                    f"{label} signed in, but BOSS could not store its token: {error}")
        else:
            session.token_stored = True
            _finish(session, "signed_in")
        process = session.process
    _kill(process)


def _read(session: _Session, stream) -> None:
    try:
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                return
            taken = None
            refused = False
            with session.lock:
                # The LATEST output, not the first 32 KiB: the token is printed at the end, after
                # the banner, the spinner and the prompt.
                session.output = (session.output + chunk.decode("utf-8", "replace"))[-_OUTPUT_CAP:]
                _parse(session)
                if session.state in LIVE:
                    taken = _take_token(session)
                    refused = (taken is None and session.code_sent
                               and LOGINS[session.provider].captures_token
                               and bool(_LOGIN_REFUSED.search(_clean(session.output))))
                    if refused:
                        _finish(session, "failed",
                                f"{LOGINS[session.provider].label} refused that code. Start the "
                                "sign-in again and paste the code exactly as shown.")
                        process = session.process
            if taken is not None:
                _store_token(session, *taken)
                del taken
                return
            if refused:
                _kill(process)
                return
    except (OSError, ValueError):
        return


def _kill(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _close_terminal(session: _Session) -> None:
    terminal, session.terminal = session.terminal, None
    if terminal is not None:
        try:
            terminal.close()
        except OSError:
            pass


def _finish(session: _Session, state: str, reason: str = "") -> None:
    """Close a session (the caller holds session.lock). The CLI's output is dropped."""
    if session.state not in LIVE:
        return
    session.state = state
    session.reason = reason
    session.finished = time.time()
    session.output = ""


def _wait(session: _Session, still_signed_in: Callable[[], bool]) -> None:
    process = session.process
    if process is None:
        return
    code = process.wait()
    login = LOGINS[session.provider]
    if login.captures_token:
        # The reader may still hold the last of the output (the token is printed just before
        # exit): let it finish, then success is "the token was stored", never "exit 0".
        if session.reader is not None:
            session.reader.join(timeout=10)
        _close_terminal(session)
        signed_in = session.token_stored
    else:
        signed_in = code == 0 and _safe(still_signed_in)
    with session.lock:
        session.exit_code = code
        if session.state not in LIVE:
            return
        if signed_in:
            _finish(session, "signed_in")
        else:
            label = LOGINS[session.provider].label
            _finish(session, "failed",
                    f"{label}'s login inside BOSS stopped without signing in "
                    f"(exit {code}). Start the sign-in again"
                    + (" and paste the code exactly as shown." if session.code_sent else "."))


def _safe(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 — an unreadable status is "not signed in"
        return False


def _expire_later(session: _Session) -> None:
    def expire() -> None:
        with session.lock:
            if session.state in LIVE:
                _finish(session, "expired")
                process = session.process
            else:
                return
        _kill(process)

    timer = threading.Timer(max(0.0, (session.expires or time.time()) - time.time()), expire)
    timer.daemon = True
    timer.start()


def _sweep(now: float) -> None:
    """Lazily expire live sessions past their time and forget old finished ones (holds _LOCK)."""
    for key, session in list(_SESSIONS.items()):
        with session.lock:
            if session.state in LIVE and session.expires is not None and now >= session.expires:
                _finish(session, "expired")
                process = session.process
            else:
                process = None
            stale = (session.state not in LIVE and session.finished is not None
                     and now - session.finished > FINISHED_RETENTION_SECONDS)
        _kill(process)
        if stale:
            _SESSIONS.pop(key, None)


def _signed_in_check(provider: str, settings: Any) -> Callable[[], bool]:
    """Is the CLI's OWN login signed in, in this process's CLI home? Never a stored token —
    except Claude Code's: its sign-in OBTAINS a long-lived token, so "signed in" is "BOSS holds
    the token its own sign-in obtained" (the CLI's own login alone still gets a fresh sign-in,
    which is how an install on the short-lived login moves to the long-lived token)."""
    if LOGINS.get(provider) is not None and LOGINS[provider].captures_token:
        def has_token() -> bool:
            from .claude_credential import boss_obtained_token_stored

            return boss_obtained_token_stored(settings)
        return has_token

    def check() -> bool:
        from .cli_harness import detect, load_specs

        spec = load_specs(settings).get(provider)
        if spec is None:
            return False
        # No settings: a credential held in BOSS's secret store is not the CLI's own login.
        return bool(detect(spec).get("ready"))
    return check


def _sign_out(login: _Login, argv: Optional[tuple[str, ...]] = None) -> None:
    """THE REFUSED SESSION GOES FIRST: the CLI's own sign-out, so the fresh login replaces it and
    nothing reads the dead one in between. Its output is never kept; failing is harmless (the
    login that follows writes a new session either way)."""
    command = tuple(argv if argv is not None else login.logout)
    binary = shutil.which(command[0]) if command else None
    if binary is None:
        return
    try:
        subprocess.run((binary, *command[1:]), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=_login_env(login), timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _spawn_on_terminal(argv: tuple[str, ...], env: dict[str, str]):
    """Start a login on a fresh pseudo-terminal (wide, so nothing wraps). Returns the process
    and the terminal's controlling side, opened for unbuffered binary read and write."""
    import fcntl
    import pty
    import struct
    import termios

    controller, child = pty.openpty()
    try:
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0))
        process = subprocess.Popen(argv, stdin=child, stdout=child, stderr=child, env=env,
                                   start_new_session=True, close_fds=True)
    except BaseException:
        os.close(controller)
        raise
    finally:
        os.close(child)
    return process, os.fdopen(controller, "r+b", buffering=0)


def start(provider: str, *, project_id: str, actor: str, settings: Any = None,
          login_rejected: bool = False, fresh: bool = False,
          signed_in: Optional[Callable[[], bool]] = None,
          argv: Optional[tuple[str, ...]] = None,
          logout_argv: Optional[tuple[str, ...]] = None,
          store: Optional[Callable[..., Any]] = None) -> dict[str, Any]:
    """Start (or return the live) sign-in for one provider.

    `login_rejected` is True when this install's last proof of that subscription was refused
    for its login (expired / revoked): the CLI may still say "logged in" about a dead session,
    so the session is signed out and a fresh sign-in starts instead of answering signed_in.
    `fresh` asks for the same explicitly (the console's Sign in on a refused row, `boss connect`
    after Connect was refused for the login). `signed_in`, `argv` and `logout_argv` exist for
    tests (a stub CLI); production uses the provider's real login, sign-out and status check.
    `store(token, valid_days=…)` receives the token a `captures_token` login (Claude Code's
    `claude setup-token`) prints, on the server (the API passes
    claude_credential.store_long_lived_token bound to its connection and project).
    """
    replace = login_rejected or fresh
    name = normalize_provider(provider)
    login = LOGINS[name]
    check = signed_in or _signed_in_check(name, settings)
    command = tuple(argv or login.argv)
    now = time.time()
    with _LOCK:
        _sweep(now)
        live = next((s for s in _SESSIONS.values() if s.provider == name and s.state in LIVE),
                    None)
        if live is not None:
            with live.lock:
                return view(live)
        session = _Session(sign_in_id=f"si_{secrets.token_hex(10)}", provider=name,
                           project_id=project_id, actor=actor, started=now,
                           expires=now + SESSION_TTL_SECONDS)
        if not replace and _safe(check):
            session.state = "signed_in"
            session.expires = None
            session.finished = now
            _SESSIONS[session.sign_in_id] = session
            return view(session)
        binary = shutil.which(command[0])
        if binary is None:
            session.state = "failed"
            session.reason = (f"{login.label}'s CLI is not installed in the BOSS server. "
                              "Upgrade BOSS (the Community image ships it), then try again.")
            session.finished = now
            _SESSIONS[session.sign_in_id] = session
            return view(session)
        if replace and (logout_argv or login.logout):
            _sign_out(login, logout_argv)
        session.store = store
        try:
            if login.pty:
                process, terminal = _spawn_on_terminal((binary, *command[1:]), _login_env(login))
                session.terminal = terminal
            else:
                process = subprocess.Popen(
                    (binary, *command[1:]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=_login_env(login), start_new_session=True,
                    close_fds=True)
        except OSError:
            session.state = "failed"
            session.reason = f"{login.label}'s login could not start inside BOSS. Try again."
            session.finished = now
            _SESSIONS[session.sign_in_id] = session
            return view(session)
        session.process = process
        _SESSIONS[session.sign_in_id] = session
    reader = threading.Thread(target=_read, args=(session, session.terminal or process.stdout),
                              daemon=True, name=f"sign-in-read-{name}")
    session.reader = reader
    reader.start()
    threading.Thread(target=_wait, args=(session, check), daemon=True,
                     name=f"sign-in-wait-{name}").start()
    _expire_later(session)
    # Give the CLI a moment to print its link, so the first answer usually carries it.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        with session.lock:
            if session.state != "starting":
                break
        time.sleep(0.05)
    with session.lock:
        return view(session)


def _get(sign_in_id: str) -> _Session:
    with _LOCK:
        _sweep(time.time())
        session = _SESSIONS.get(str(sign_in_id or ""))
    if session is None:
        raise SignInError(404, "No such sign-in",
                          "It finished more than an hour ago or BOSS restarted. Start the "
                          "sign-in again.")
    return session


def status(sign_in_id: str) -> dict[str, Any]:
    session = _get(sign_in_id)
    with session.lock:
        return view(session)


def submit_code(sign_in_id: str, code: str) -> dict[str, Any]:
    """Hand the code the browser showed to the provider's own login. Never stored or logged."""
    session = _get(sign_in_id)
    value = str(code or "").strip()
    if not value or len(value) > _CODE_MAX or any(c in value for c in "\r\n\0"):
        raise SignInError(422, "Not a code", "Paste the one code the sign-in page showed.")
    with session.lock:
        if session.state not in LIVE or not session.needs_code_input or session.code_sent:
            raise SignInError(409, "No code expected",
                              "This sign-in is not waiting for a code. Start it again if it "
                              "stopped.", state=session.state)
        process = session.process
        try:
            if session.terminal is not None:
                # Typed, then Enter (a terminal in raw mode reads CR). The output so far is
                # dropped: whatever follows is the answer to this code.
                session.terminal.write(value.encode() + b"\r")
                session.output = ""
            else:
                assert process is not None and process.stdin is not None
                process.stdin.write(value.encode() + b"\n")
                process.stdin.flush()
        except (AssertionError, OSError, ValueError):
            _finish(session, "failed", "The login inside BOSS stopped before it read the code. "
                                       "Start the sign-in again.")
            return view(session)
        session.code_sent = True
        return view(session)


def cancel(sign_in_id: str) -> dict[str, Any]:
    session = _get(sign_in_id)
    with session.lock:
        process = session.process if session.state in LIVE else None
        _finish(session, "cancelled")
    _kill(process)
    with session.lock:
        return view(session)


def shutdown() -> None:
    """Kill every live login (API shutdown). Idempotent."""
    with _LOCK:
        sessions = list(_SESSIONS.values())
    for session in sessions:
        with session.lock:
            process = session.process if session.state in LIVE else None
            _finish(session, "cancelled")
        _kill(process)


def reset_for_tests() -> None:
    shutdown()
    with _LOCK:
        _SESSIONS.clear()


def last_login_rejected(connection, project_ids: list[str], provider: str, settings: Any) -> bool:
    """Was this subscription's login refused? THE SAME ANSWER every surface shows: the one status
    function (setup_view.source_state — last real proof, then the credential a real call refused).

    The live loop (2026-10-25): Codex's last proof was recorded `failed` while the credential a
    real call had refused said `rejected`; the Models row read Login expired, but this asked only
    `last_check.state == login_expired`, answered "not refused", the sign-in reported "already
    signed in" on `codex login status`, and Connect failed again — `boss connect codex` looped."""
    from sqlalchemy import text

    from .cli_harness import load_specs
    from .core import now_utc
    from .setup_view import source_state

    spec = load_specs(settings).get(normalize_provider(provider))
    source_id = getattr(spec, "entitlement_source_id", "") if spec else ""
    if not source_id:
        return False
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT connection_metadata, observed_capacity FROM entitlement_sources "
                "WHERE source_id = :s AND project_id = ANY(:p)"),
                {"s": source_id,
                 "p": list(dict.fromkeys(p for p in project_ids if p))}).mappings().all()
    except Exception:  # noqa: BLE001 — no record is "not rejected"
        return False
    now = now_utc()

    def _obj(value: Any) -> dict:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    for row in rows:
        metadata, capacity = _obj(row["connection_metadata"]), _obj(row["observed_capacity"])
        state, _, _ = source_state(kind="subscription", metadata=metadata, capacity=capacity,
                                   budget_spent=False, now=now)
        if state in REFUSED_LOGIN_STATES:
            return True
    return False


#: The status-function states that mean the CLI's session is not usable however it reports.
REFUSED_LOGIN_STATES = frozenset({"login_expired", "key_rejected"})


def sign_in_sources(settings: Any = None) -> dict[str, str]:
    """{entitlement source id: the provider BOSS signs in inside the server} — from the CLI
    harness specs, so a Sources row knows its Sign in without a list of ids in the console."""
    try:
        from .cli_harness import load_specs

        specs = load_specs(settings)
    except Exception:  # noqa: BLE001 — no specs, no Sign in offered
        return {}
    return {str(spec.entitlement_source_id): name for name, spec in specs.items()
            if name in LOGINS and getattr(spec, "entitlement_source_id", "")}
