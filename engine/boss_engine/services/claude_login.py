"""ONE OWNER REFRESHES THE CLAUDE CLI'S OWN LOGIN, AHEAD OF TIME (2026-10-27).

The Claude subscription signed in inside BOSS is the CLI's own login,
`$CLAUDE_CONFIG_DIR/.credentials.json` = {claudeAiOauth: {accessToken, refreshToken, expiresAt
(ms), scopes, ...}} (claude_credential.KIND_CLI_LOGIN). The api and worker containers share
that directory (the `boss_community_claude_home` volume) and both start the CLI.

How the CLI refreshes (read from the CLI itself, 2.1.x): when the access token is within 5
minutes of `expiresAt` it takes a lock directory `<config dir>/.oauth_refresh.lock` (mkdir,
stale after 60 s), POSTs JSON {grant_type: "refresh_token", refresh_token, client_id, scope} to
https://platform.claude.com/v1/oauth/token, and writes the rotated pair back. A refresh the
server answers `invalid_grant` makes the CLI CLEAR the login on disk (accessToken,
refreshToken = "", expiresAt = 0) — "Not logged in". The login was lost that way twice, ~8 h
after sign-in (2026-10-26 15:08, 2026-10-27 09:25), the second time 5 minutes after the access
token expired.

So the CLI must never be the one that refreshes:

* `refresh_if_due` — THE refresh. Under an exclusive flock on `<config dir>/.boss-login-
  refresh.lock` (one inode on the shared volume, so one holder across both containers) AND the
  CLI's own lock directory (so a CLI that does decide to refresh waits for us), it re-reads the
  file, refreshes only if the token is still within the window, and writes the result
  atomically (temp file, fsync, rename, 0600). A failure NEVER touches the file: the state is
  recorded in `<config dir>/.boss-login-state.json` (no secret in it) and the system doctor
  raises it as "Claude sign-in needed".
* `start` — the API's thread (system_threads) calls it every few minutes with a 30-minute
  window, so a CLI never meets a token inside its own 5-minute window.
* `before_cli_start` — every `claude` child BOSS starts on the login
  (`ClaudeCredential.apply`) waits out a refresh in progress, refreshes a token that is about to
  expire, refuses to start the CLI on an expired, unrefreshable login (the CLI would wipe it),
  and, inside a build sandbox, re-copies the CURRENT access token into the run's config dir.
* `ensure_fresh` — the sandbox seeding (sandbox_identity._seed_claude_login) asks for a token
  that outlives a build stage.

Nothing here prints, logs or returns a token value.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

_log = logging.getLogger("boss.claude_login")

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
#: The Claude CLI's own OAuth client (its CLIENT_ID); a login that recorded `clientId` uses that.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_SCOPES = ("user:inference", "user:profile", "user:sessions:claude_code",
                  "user:mcp_servers", "user:file_upload")

CREDENTIALS = ".credentials.json"
LOCK_NAME = ".boss-login-refresh.lock"
CLI_LOCK_NAME = ".oauth_refresh.lock"          # the CLI's own (proper-lockfile: a directory)
CLI_LOCK_STALE_SECONDS = 60.0
STATE_NAME = ".boss-login-state.json"

#: The proactive refresher's window: refresh when the token has less than this left.
REFRESH_WINDOW_SECONDS = float(os.environ.get("BOSS_CLAUDE_REFRESH_WINDOW_SECONDS") or 30 * 60)
#: A CLI start refreshes a token with less than this left: well outside the CLI's own 5 minutes.
CLI_START_MIN_VALID_SECONDS = 10 * 60
#: A build sandbox's copy must outlive one stage (the copy cannot refresh itself).
SEED_MIN_VALID_SECONDS = float(os.environ.get("BOSS_CLAUDE_SEED_MIN_VALID_SECONDS") or 60 * 60)
INTERVAL_SECONDS = 180.0
LOCK_TIMEOUT_SECONDS = 60.0

#: Status words (the pill and the doctor read these).
OK, EXPIRING, EXPIRED, REFRESH_FAILED, ABSENT = "ok", "expiring", "expired", "refresh_failed", "absent"

#: post(url, json_body) -> (http_status, response_json). Replaced in tests; never logs.
Post = Callable[[str, dict], "tuple[int, dict]"]


class ClaudeSignInNeeded(RuntimeError):
    """The Claude login cannot give a usable token and BOSS could not refresh it. Its words
    carry "claude sign-in needed" (model_failures classifies it as a rejected login)."""


def sign_in_words(reason: str) -> str:
    return ("Claude sign-in needed: " + reason + ". Sign in again in Repo and models "
            "(Claude Code, Sign in) or run `boss connect claude`.")


def enabled() -> bool:
    return (os.environ.get("BOSS_CLAUDE_LOGIN_REFRESH") or "on").strip().lower() != "off"


def config_dir() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "") or "~/.claude")


def _path(name: str, home: Optional[str] = None) -> str:
    return os.path.join(home or config_dir(), name)


def _read_doc(home: Optional[str] = None) -> Optional[dict]:
    try:
        with open(_path(CREDENTIALS, home), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def read_oauth(home: Optional[str] = None) -> Optional[dict]:
    """The login's claudeAiOauth block, or None. For this package only: it holds the tokens."""
    doc = _read_doc(home)
    oauth = doc.get("claudeAiOauth") if doc else None
    return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None


def _expires_at(oauth: Optional[dict]) -> Optional[float]:
    """expiresAt in SECONDS since the epoch (the file holds ms), or None when not recorded."""
    try:
        raw = float((oauth or {}).get("expiresAt"))
    except (TypeError, ValueError):
        return None
    return raw / 1000.0 if raw > 1e11 else raw


def seconds_left(oauth: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    at = _expires_at(oauth)
    return None if at is None else at - (time.time() if now is None else now)


# --------------------------------------------------------------------------------------------
# state (no secrets)
# --------------------------------------------------------------------------------------------

def _write_private(path: str, text: str) -> None:
    """Atomic, 0600, never through a symlink."""
    tmp = f"{path}.boss-{os.getpid()}-{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _record(home: str, **fields: Any) -> None:
    try:
        state = _read_state(home)
        state.update(fields, at=datetime.now(timezone.utc).isoformat())
        _write_private(_path(STATE_NAME, home), json.dumps(state))
    except OSError:
        _log.warning("could not record the Claude login state", exc_info=False)


def _read_state(home: Optional[str] = None) -> dict:
    try:
        with open(_path(STATE_NAME, home), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def status(now: Optional[float] = None) -> dict[str, Any]:
    """What the login looks like right now, with no secret: {state, seconds_left, expires_at,
    error}. `state` is ok / expiring (inside the refresh window) / expired / refresh_failed (the
    last refresh was refused and the token has not been replaced since) / absent."""
    from .claude_credential import cli_login_present

    if not cli_login_present():
        return {"state": ABSENT}
    oauth = read_oauth()
    if oauth is None:
        return {"state": ABSENT}
    left = seconds_left(oauth, now)
    st = _read_state()
    failed = st.get("state") == REFRESH_FAILED and st.get("for_expires_at") == oauth.get("expiresAt")
    if left is not None and left <= 0:
        state = EXPIRED
    elif failed:
        state = REFRESH_FAILED
    elif left is not None and left <= REFRESH_WINDOW_SECONDS:
        state = EXPIRING
    else:
        state = OK
    out: dict[str, Any] = {"state": state, "seconds_left": None if left is None else int(left),
                           "expires_at": _expires_at(oauth)}
    if failed or state == EXPIRED:
        out["error"] = st.get("error") if failed else None
    return out


# --------------------------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------------------------

@contextlib.contextmanager
def _flock(home: str, kind: int, timeout: float) -> Iterator[bool]:
    """flock on the shared lock file; yields whether it was taken within `timeout`."""
    fd = os.open(_path(LOCK_NAME, home), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    taken = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, kind | fcntl.LOCK_NB)
                taken = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield taken
    finally:
        if taken:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _cli_lock(home: str, timeout: float) -> Iterator[bool]:
    """The CLI's own refresh lock (a directory, as proper-lockfile makes it), so a CLI that
    decides to refresh meets ELOCKED and waits instead of racing us. Stale after 60 s, as the
    CLI treats it."""
    path = _path(CLI_LOCK_NAME, home)
    deadline = time.monotonic() + timeout
    taken = False
    while True:
        try:
            os.mkdir(path, 0o700)
            taken = True
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.stat(path).st_mtime > CLI_LOCK_STALE_SECONDS:
                    os.rmdir(path)
                    continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        except OSError:
            break
    try:
        yield taken
    finally:
        if taken:
            with contextlib.suppress(OSError):
                os.rmdir(path)


# --------------------------------------------------------------------------------------------
# the refresh
# --------------------------------------------------------------------------------------------

def _http_post(url: str, body: dict) -> tuple[int, dict]:
    import httpx

    resp = httpx.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


def _error_kind(status_code: int, data: dict) -> str:
    code = str((data or {}).get("error") or "")
    code = code if re.fullmatch(r"[a-z_]{1,40}", code) else ""
    return code or f"http_{status_code}"


def refresh_if_due(min_valid: float = REFRESH_WINDOW_SECONDS, *, post: Optional[Post] = None,
                   now: Optional[Callable[[], float]] = None,
                   lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Refresh the control plane's login if it has less than `min_valid` seconds left.

    Returns {"result": ...}: fresh (nothing to do, or another process just did it), refreshed,
    failed (the file is untouched and the failure recorded), busy (the lock was not free in
    time), absent (no CLI login here), no_refresh_token, off. Never raises for a refusal."""
    from .claude_credential import cli_login_present

    clock = now or time.time
    if not enabled():
        return {"result": "off"}
    if not cli_login_present():
        return {"result": "absent"}
    left = seconds_left(read_oauth(), clock())
    if left is None:                       # no recorded expiry: nothing to time a refresh by
        return {"result": "fresh"}
    if left > min_valid:
        return {"result": "fresh", "seconds_left": int(left)}
    home = config_dir()
    with _flock(home, fcntl.LOCK_EX, lock_timeout) as mine:
        if not mine:
            return {"result": "busy"}
        with _cli_lock(home, min(lock_timeout, 15.0)) as cli_free:
            oauth = read_oauth(home)                      # re-read: the holder before us may
            if oauth is None:                             # have refreshed already
                return {"result": "absent"}
            left = seconds_left(oauth, clock())
            if left is None or left > min_valid:
                return {"result": "fresh"}
            if not cli_free:
                return {"result": "busy"}
            refresh_token = str(oauth.get("refreshToken") or "")
            if not refresh_token:
                _record(home, state=REFRESH_FAILED, error="no_refresh_token",
                        for_expires_at=oauth.get("expiresAt"))
                return {"result": "no_refresh_token"}
            scopes = oauth.get("scopes") if isinstance(oauth.get("scopes"), list) else None
            body = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": str(oauth.get("clientId") or CLIENT_ID),
                    "scope": " ".join(scopes or DEFAULT_SCOPES)}
            try:
                status_code, data = (post or _http_post)(TOKEN_URL, body)
            except Exception as exc:  # noqa: BLE001 — a network failure is a failure, not a wipe
                status_code, data = 0, {"error": "network_" + type(exc).__name__.lower()[:20]}
            access = str((data or {}).get("access_token") or "")
            try:
                expires_in = float((data or {}).get("expires_in"))
            except (TypeError, ValueError):
                expires_in = 0.0
            if status_code != 200 or not access or expires_in <= 0:
                kind = _error_kind(status_code, data) if status_code else str(data.get("error"))
                _record(home, state=REFRESH_FAILED, error=kind,
                        for_expires_at=oauth.get("expiresAt"))
                _log.error("Claude login refresh failed (%s); the login is kept as it is, "
                           "the operator is asked to sign in again", kind)
                return {"result": "failed", "error": kind}
            new = dict(oauth)
            new["accessToken"] = access
            new["refreshToken"] = str(data.get("refresh_token") or refresh_token)
            new["expiresAt"] = int((clock() + expires_in) * 1000)
            if isinstance(data.get("scope"), str) and data["scope"].strip():
                new["scopes"] = data["scope"].split()
            doc = _read_doc(home) or {}
            current = doc.get("claudeAiOauth") if isinstance(doc.get("claudeAiOauth"), dict) else {}
            if str(current.get("refreshToken") or "") not in ("", refresh_token):
                # Somebody signed in afresh while we asked: theirs wins, ours is discarded.
                return {"result": "fresh"}
            doc["claudeAiOauth"] = new
            _write_private(_path(CREDENTIALS, home), json.dumps(doc))
            _record(home, state=OK, error=None, refreshed_at=datetime.now(timezone.utc).isoformat(),
                    for_expires_at=new["expiresAt"])
            _log.info("Claude login refreshed; valid for %d minutes", int(expires_in // 60))
            return {"result": "refreshed", "seconds_left": int(expires_in)}


def ensure_fresh(min_valid: float, *, post: Optional[Post] = None,
                 now: Optional[Callable[[], float]] = None,
                 lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """(True, "") when the login holds a token with at least `min_valid` seconds left —
    refreshing it under the lock when it does not — else (False, why in plain words)."""
    clock = now or time.time
    out = refresh_if_due(min_valid, post=post, now=clock, lock_timeout=lock_timeout)
    if out["result"] == "absent":
        return False, "the Claude CLI is not signed in on this server"
    left = seconds_left(read_oauth(), clock())
    if left is None or left >= min_valid:
        return True, ""
    mins = max(0, int(left // 60))
    when = "has expired" if left <= 0 else f"expires in {mins} min"
    why = {"failed": f"refresh was refused ({out.get('error')})",
           "busy": "another refresh did not finish in time",
           "no_refresh_token": "the login holds no refresh token",
           "off": "refresh is turned off (BOSS_CLAUDE_LOGIN_REFRESH=off)"}.get(
               out["result"], out["result"])
    return False, f"the Claude login's token {when} and BOSS could not refresh it: {why}"


def before_cli_start(*, post: Optional[Post] = None) -> None:
    """Called for every `claude` child BOSS starts on the CLI login (ClaudeCredential.apply).

    Refreshes a token about to expire (so the CLI never refreshes, and never wipes), waits out
    a refresh in progress, and raises ClaudeSignInNeeded instead of starting the CLI on an
    expired login BOSS could not refresh. Inside a build sandbox it also re-copies the current
    access token into the run's own config dir, so a long build follows the refreshes."""
    if not enabled():
        return
    from .claude_credential import cli_login_present

    if not cli_login_present():
        return
    try:
        from . import sandbox_identity

        ident = sandbox_identity.current()
    except Exception:  # noqa: BLE001
        sandbox_identity, ident = None, None  # type: ignore[assignment]
    # A sandbox's copy cannot refresh itself, so it must outlive a stage; the control plane's
    # own CLI only needs to stay clear of the CLI's 5-minute refresh window.
    need = SEED_MIN_VALID_SECONDS if ident is not None else CLI_START_MIN_VALID_SECONDS
    ok, why = ensure_fresh(need, post=post)
    with _flock(config_dir(), fcntl.LOCK_SH, LOCK_TIMEOUT_SECONDS):
        pass                                     # a refresh in progress finishes first
    if ident is not None:
        if not ok:
            raise ClaudeSignInNeeded(sign_in_words(why))
        sandbox_identity.copy_claude_access_token(ident)
        return
    left = seconds_left(read_oauth())
    if not ok and left is not None and left <= 0:
        raise ClaudeSignInNeeded(sign_in_words(why))


# --------------------------------------------------------------------------------------------
# the owner's thread
# --------------------------------------------------------------------------------------------

_THREAD: Optional[threading.Thread] = None


def tick() -> dict[str, Any]:
    try:
        return refresh_if_due(REFRESH_WINDOW_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the thread must keep running
        _log.warning("Claude login refresh tick failed: %s", type(exc).__name__)
        return {"result": "error", "error": type(exc).__name__}


def start(*, interval: float = INTERVAL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            tick()
            time.sleep(interval)

    _THREAD = threading.Thread(target=_loop, name="boss-claude-login", daemon=True)
    _THREAD.start()
    return _THREAD
