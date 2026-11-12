"""ONE OWNER REFRESHES THE CODEX CHATGPT LOGIN — runs never rotate the real refresh token
(2026-10-29). Mirrors claude_login.py's pattern, adapted to Codex's own auth.json shape:
`$CODEX_HOME/auth.json` = {auth_mode, last_refresh, tokens: {access_token, refresh_token,
id_token, account_id, expires_at (epoch seconds)}}.

Codex falls back to API-key mode unless its auth.json carries a real refresh_token (852a786),
so a run's copy must KEEP that field — unlike Claude's copy, which strips it. The safety net is
therefore freshness at seed/reseed time, not field-stripping: the control plane is the only
process that ever calls the token endpoint with the real refresh token, under a lock, and a
run's copy is reseeded from the control plane's current tokens before every `codex` start so it
never needs to refresh itself.

Nothing here prints, logs or returns a token value.

REFRESH TRIGGERS (UNVERIFIED, DEFENSIVE — not observed from a real codex run). This BOSS build
sandbox cannot execute codex (the harness reports codex unavailable; the version this module was
once said to have been tested against, 0.15.0, is not what is installed), so the behaviours below
have NOT been reproduced against a real `codex` or the real `auth.openai.com` token endpoint. An
earlier version of this docstring and PR #24 asserted real-sandbox observations that were never
made; those claims have been removed. The triggers are kept as a conservative, defensive refresh
policy — refreshing early never hurts, and the control plane is the only holder of the real
refresh token regardless:
- Access-token expiry: refresh before `tokens.expires_at` passes, so a seeded run copy never has
  to refresh itself (a normal OAuth refresh, same grant as this module's).
- `last_refresh` age: refresh once `last_refresh` is older than `REFRESH_AGE_INTERVAL_SECONDS`
  (minus a margin), in case some codex build refreshes on age alone rather than solely on the
  access-token's expiry. Unverified; the interval and margin below are a defensive guess, not a
  measured threshold.
- Bare 401 / revoked refresh token: `mark_revoked` records a dead credential explicitly from
  whichever caller (a run, the proof turn, or this module's own refresh) first sees a 401, since
  a revoked refresh token is a person's problem to fix, never retried as if it were transient.
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

_log = logging.getLogger("boss.codex_login")

TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_NAME = "auth.json"
LOCK_NAME = ".boss-login-refresh.lock"
STATE_NAME = ".boss-login-state.json"

#: The proactive refresher's window: refresh when the token has less than this left.
REFRESH_WINDOW_SECONDS = 30 * 60.0
#: A control-plane-direct `codex` start (the proof turn) refreshes a token below this.
CLI_START_MIN_VALID_SECONDS = 10 * 60
#: A build sandbox's copy must outlive one stage (the copy must never refresh itself).
SEED_MIN_VALID_SECONDS = 60 * 60
INTERVAL_SECONDS = 180.0
LOCK_TIMEOUT_SECONDS = 60.0

#: UNVERIFIED, DEFENSIVE (not observed from a real codex run — see module docstring): some codex
#: builds may refresh their own ChatGPT login on last_refresh age alone rather than solely on the
#: access-token's expiry. As a conservative guard the control plane refreshes before this line is
#: crossed, with a safety margin, so a seeded copy never looks "due" to the run itself. The ~8-day
#: interval is a defensive guess, not a measured threshold.
REFRESH_AGE_INTERVAL_SECONDS = 8 * 24 * 3600.0
REFRESH_AGE_MARGIN_SECONDS = 24 * 3600.0

#: Status words (the pill and the doctor read these).
OK, EXPIRING, EXPIRED, REFRESH_FAILED, ABSENT = "ok", "expiring", "expired", "refresh_failed", "absent"

#: post(url, json_body) -> (http_status, response_json). Replaced in tests; never logs.
Post = Callable[[str, dict], "tuple[int, dict]"]


class CodexSignInNeeded(RuntimeError):
    """The Codex login cannot give a usable token and BOSS could not refresh it."""


def sign_in_words(reason: str) -> str:
    return ("Codex sign-in needed: " + reason + ". Sign in again in Repo and models "
            "(Codex, Sign in) or run `boss connect codex`.")


def config_dir() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME", "") or "~/.codex")


def _path(name: str, home: Optional[str] = None) -> str:
    return os.path.join(home or config_dir(), name)


def _read_doc(home: Optional[str] = None) -> Optional[dict]:
    try:
        with open(_path(AUTH_NAME, home), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def read_auth(home: Optional[str] = None) -> Optional[dict]:
    """The whole auth.json document (tokens + auth_mode + last_refresh), or None."""
    return _read_doc(home)


def read_tokens(home: Optional[str] = None) -> Optional[dict]:
    """The login's tokens block, or None. For this package only: it holds the real tokens."""
    doc = _read_doc(home)
    tokens = doc.get("tokens") if doc else None
    return tokens if isinstance(tokens, dict) and tokens.get("access_token") else None


def seconds_left(tokens: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    try:
        at = float((tokens or {}).get("expires_at"))
    except (TypeError, ValueError):
        return None
    return at - (time.time() if now is None else now)


def _last_refresh_age(doc: Optional[dict], now: float) -> Optional[float]:
    """Seconds since `doc["last_refresh"]` (an ISO-8601 timestamp), or None if absent/unparsable."""
    raw = str((doc or {}).get("last_refresh") or "")
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return now - at.timestamp()


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
        _log.warning("could not record the Codex login state", exc_info=False)


def _read_state(home: Optional[str] = None) -> dict:
    try:
        with open(_path(STATE_NAME, home), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_revoked(error: str) -> None:
    """Codex's own words for a dead credential (an "invalidated oauth token" / "refresh token
    was revoked" from a run, the proof turn, or our own refresh attempt) flip the state to
    refresh_failed immediately — a 401 from Codex is a credential problem for a person to fix,
    never retried as if it were transient."""
    _record(config_dir(), state=REFRESH_FAILED, error=str(error)[:200])


def status(now: Optional[float] = None) -> dict[str, Any]:
    """What the login looks like right now, with no secret: {state, seconds_left, expires_at,
    error}."""
    tokens = read_tokens()
    if tokens is None:
        return {"state": ABSENT}
    left = seconds_left(tokens, now)
    st = _read_state()
    failed = st.get("state") == REFRESH_FAILED
    if left is not None and left <= 0:
        state = EXPIRED
    elif failed:
        state = REFRESH_FAILED
    elif left is not None and left <= REFRESH_WINDOW_SECONDS:
        state = EXPIRING
    else:
        state = OK
    out: dict[str, Any] = {"state": state, "seconds_left": None if left is None else int(left),
                           "expires_at": (tokens or {}).get("expires_at")}
    if failed or state == EXPIRED:
        out["error"] = st.get("error") if failed else None
    return out


# --------------------------------------------------------------------------------------------
# the lock
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
    """Refresh the control plane's Codex login if it has less than `min_valid` seconds left.

    Also refreshes when the login's `last_refresh` is older than REFRESH_AGE_INTERVAL_SECONDS
    minus REFRESH_AGE_MARGIN_SECONDS: some codex builds refresh on last_refresh age alone, and a
    seeded copy must never cross that line looking "due" to the run itself.

    Returns {"result": ...}: fresh, refreshed, failed, busy, absent, no_refresh_token.
    Never raises for a refusal."""
    clock = now or time.time
    if read_tokens() is None:
        return {"result": "absent"}
    age_due_at = REFRESH_AGE_INTERVAL_SECONDS - REFRESH_AGE_MARGIN_SECONDS

    def _due(doc: Optional[dict]) -> tuple[bool, Optional[float]]:
        tokens = (doc or {}).get("tokens") if isinstance(doc, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return False, None
        left = seconds_left(tokens, clock())
        if left is not None and left <= min_valid:
            return True, left
        age = _last_refresh_age(doc, clock())
        if age is not None and age >= age_due_at:
            return True, left
        return False, left

    due, left = _due(read_auth())
    if not due:
        return {"result": "fresh"} if left is None else {"result": "fresh", "seconds_left": int(left)}
    home = config_dir()
    with _flock(home, fcntl.LOCK_EX, lock_timeout) as mine:
        if not mine:
            return {"result": "busy"}
        doc = read_auth(home)                            # re-read: the holder before us may
        if doc is None:                                   # have refreshed already
            return {"result": "absent"}
        tokens = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else None
        if tokens is None:
            return {"result": "absent"}
        due, left = _due(doc)
        if not due:
            return {"result": "fresh"}
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            _record(home, state=REFRESH_FAILED, error="no_refresh_token")
            return {"result": "no_refresh_token"}
        body = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CLIENT_ID, "scope": "openid profile email"}
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
            _record(home, state=REFRESH_FAILED, error=kind)
            _log.error("Codex login refresh failed (%s); the login is kept as it is, "
                       "the operator is asked to sign in again", kind)
            return {"result": "failed", "error": kind}
        doc = _read_doc(home) or {}
        current = doc.get("tokens") if isinstance(doc.get("tokens"), dict) else {}
        if str(current.get("refresh_token") or "") not in ("", refresh_token):
            # Somebody signed in afresh while we asked: theirs wins, ours is discarded.
            return {"result": "fresh"}
        new = dict(current)
        new["access_token"] = access
        new["refresh_token"] = str(data.get("refresh_token") or refresh_token)
        if data.get("id_token"):
            new["id_token"] = data["id_token"]
        new["expires_at"] = int(clock() + expires_in)
        doc["tokens"] = new
        doc["last_refresh"] = datetime.now(timezone.utc).isoformat()
        _write_private(_path(AUTH_NAME, home), json.dumps(doc))
        _record(home, state=OK, error=None)
        _log.info("Codex login refreshed; valid for %d minutes", int(expires_in // 60))
        return {"result": "refreshed", "seconds_left": int(expires_in),
                "store_synced": _store_sync_after_refresh()}


def _store_sync_after_refresh() -> bool:
    """The refresh that just succeeded ROTATED the credential: the secret store's copy is now
    revoked. Write the new generation back at once (cli_harness.sync_rotated_credential).

    This module is the only process that ever rotates the real refresh token (see the module
    docstring), so it is the only place the store can be kept live from — the run-path
    write-back (`remember_rotated_credential`) stopped mattering the day rotation moved here,
    and nobody moved the sync with it. Measured (laptop install, 2026-11-12): this refresher
    kept the container's login alive for six weeks while the store froze at its 1 Oct copy,
    and every restore from the store signed a working surface out.

    Never raises, and the refresh stands either way: a failed sync is loud in the log and in
    the returned flag, never a reason to lose the refresh that already happened."""
    try:
        from ..config import get_settings
        from .cli_harness import load_specs, sync_rotated_credential

        settings = get_settings()
        spec = load_specs(settings).get("codex")
        if spec is None:
            return False
        out = sync_rotated_credential(spec, settings)
        if out.get("state") == "failed":
            _log.warning("the refreshed Codex login was NOT written back to the secret store: "
                         "%s", out.get("detail"))
        return bool(out.get("synced"))
    except Exception as exc:  # noqa: BLE001 — the refresh stands; the sync says why it did not
        _log.warning("the refreshed Codex login was NOT written back to the secret store (%s)",
                     type(exc).__name__)
        return False


def ensure_fresh(min_valid: float, *, post: Optional[Post] = None,
                 now: Optional[Callable[[], float]] = None,
                 lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """(True, "") when the login holds a token with at least `min_valid` seconds left —
    refreshing it under the lock when it does not — else (False, why in plain words)."""
    clock = now or time.time
    out = refresh_if_due(min_valid, post=post, now=clock, lock_timeout=lock_timeout)
    if out["result"] == "absent":
        return False, "Codex is not signed in on this server"
    left = seconds_left(read_tokens(), clock())
    if left is None or left >= min_valid:
        return True, ""
    mins = max(0, int(left // 60))
    when = "has expired" if left <= 0 else f"expires in {mins} min"
    why = {"failed": f"refresh was refused ({out.get('error')})",
           "busy": "another refresh did not finish in time",
           "no_refresh_token": "the login holds no refresh token"}.get(
               out["result"], out["result"])
    return False, f"the Codex login's token {when} and BOSS could not refresh it: {why}"


def before_cli_start(*, post: Optional[Post] = None) -> None:
    """Called ahead of every control-plane `codex` start (the proof turn, a direct probe):
    refreshes a token about to expire so `codex` never has to, and raises CodexSignInNeeded
    instead of starting it on an expired, unrefreshable login."""
    if read_tokens() is None:
        return
    ok, why = ensure_fresh(CLI_START_MIN_VALID_SECONDS, post=post)
    with _flock(config_dir(), fcntl.LOCK_SH, LOCK_TIMEOUT_SECONDS):
        pass                                     # a refresh in progress finishes first
    left = seconds_left(read_tokens())
    if not ok and left is not None and left <= 0:
        raise CodexSignInNeeded(sign_in_words(why))


# --------------------------------------------------------------------------------------------
# the owner's thread
# --------------------------------------------------------------------------------------------

_THREAD: Optional[threading.Thread] = None


def tick() -> dict[str, Any]:
    try:
        return refresh_if_due(REFRESH_WINDOW_SECONDS)
    except Exception as exc:  # noqa: BLE001 — the thread must keep running
        _log.warning("Codex login refresh tick failed: %s", type(exc).__name__)
        return {"result": "error", "error": type(exc).__name__}


def start(*, interval: float = INTERVAL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            tick()
            time.sleep(interval)

    _THREAD = threading.Thread(target=_loop, name="boss-codex-login", daemon=True)
    _THREAD.start()
    return _THREAD
