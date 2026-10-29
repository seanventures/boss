"""Proactively refresh the Codex CLI's own subscription login, the same discipline
`claude_login` already gives Claude ([L:kgn_2b552c5e148b444f825c]: a dead Codex login showed
green on Setup until a build hit a 401 — Codex has no year-long setup-token, only a short-lived
access token + refresh token, so it must be refreshed ahead of time rather than discovered dead).

The Codex CLI's own login is `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`):
{"auth_mode", "last_refresh", "tokens": {"access_token", "refresh_token"}}. Nothing here prints,
logs or returns a token value.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

_log = logging.getLogger("boss.codex_login")

TOKEN_URL = "https://auth.openai.com/oauth/token"
#: The Codex CLI's own OAuth client id — unconfirmed against the installed CLI; override via
#: BOSS_CODEX_CLIENT_ID if it turns out to differ.
CLIENT_ID = os.environ.get("BOSS_CODEX_CLIENT_ID") or "app_EMoamEEZ73f0CkXaXp7hrann"

AUTH_FILE = "auth.json"

#: The proactive refresher's window: refresh when the token has less than this left.
REFRESH_WINDOW_SECONDS = float(os.environ.get("BOSS_CODEX_REFRESH_WINDOW_SECONDS") or 2 * 24 * 3600)
LOCK_TIMEOUT_SECONDS = 60.0

#: post(url, json_body) -> (http_status, response_json). Replaced in tests; never logs.
Post = Callable[[str, dict], "tuple[int, dict]"]

#: Serializes refreshes within this process (the flock below handles across processes).
_lock = threading.Lock()


def _lock_path(home: str) -> str:
    """A flock file OUTSIDE the Codex config dir (tempdir, keyed by home), so a refresh never
    leaves a stray file next to auth.json, but still shares one inode across both containers
    for a given CODEX_HOME — the single-refresh-holder a run's own write-back also honours."""
    digest = hashlib.sha256(os.path.abspath(home).encode("utf-8")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"boss-codex-login-refresh-{digest}.lock")


@contextlib.contextmanager
def _flock(home: str, timeout: float) -> Iterator[bool]:
    """flock on the shared lock file; yields whether it was taken within `timeout`."""
    fd = os.open(_lock_path(home), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    taken = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def config_dir() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME", "") or "~/.codex")


def _path(home: Optional[str] = None) -> str:
    return os.path.join(home or config_dir(), AUTH_FILE)


def read_auth(home: Optional[str] = None) -> Optional[dict]:
    """The login's auth.json, or None. For this package only: it holds the tokens."""
    try:
        with open(_path(home), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _jwt_exp(token: str) -> Optional[float]:
    """The JWT's `exp` claim (epoch seconds), or None for anything not a 3-segment JWT with
    one. Never logs `token`."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    try:
        return float(exp)
    except (TypeError, ValueError):
        return None


def seconds_left(auth: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    tokens = (auth or {}).get("tokens") if isinstance(auth, dict) else None
    access = (tokens or {}).get("access_token") if isinstance(tokens, dict) else None
    exp = _jwt_exp(str(access or ""))
    return None if exp is None else exp - (time.time() if now is None else now)


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


def _http_post(url: str, body: dict) -> tuple[int, dict]:
    import httpx

    resp = httpx.post(url, json=body, headers={"Content-Type": "application/json"}, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


def refresh_if_due(min_valid: float = REFRESH_WINDOW_SECONDS, *, post: Optional[Post] = None,
                   now: Optional[Callable[[], float]] = None,
                   lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Refresh the Codex CLI's login if its access token has less than `min_valid` seconds
    left. Returns {"result": ...}: fresh, refreshed, failed, absent, no_refresh_token. Never
    raises for a refusal."""
    clock = now or time.time
    home = config_dir()
    auth = read_auth(home)
    if auth is None:
        return {"result": "absent"}
    left = seconds_left(auth, clock())
    if left is None:                       # no readable expiry: nothing to time a refresh by
        return {"result": "fresh"}
    if left > min_valid:
        return {"result": "fresh", "seconds_left": int(left)}
    if not _lock.acquire(timeout=lock_timeout):
        return {"result": "busy"}
    try:
        with _flock(home, lock_timeout) as mine:
            if not mine:
                return {"result": "busy"}
            auth = read_auth(home)         # re-read: another caller may have refreshed already
            if auth is None:
                return {"result": "absent"}
            left = seconds_left(auth, clock())
            if left is None or left > min_valid:
                return {"result": "fresh"}
            tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
            refresh_token = str(tokens.get("refresh_token") or "")
            if not refresh_token:
                return {"result": "no_refresh_token"}
            body = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": CLIENT_ID}
            try:
                status_code, data = (post or _http_post)(TOKEN_URL, body)
            except Exception as exc:  # noqa: BLE001 — a network failure is a failure, not a wipe
                status_code, data = 0, {"error": "network_" + type(exc).__name__.lower()[:20]}
            access = str((data or {}).get("access_token") or "")
            if status_code != 200 or not access:
                kind = str((data or {}).get("error") or f"http_{status_code}")
                _log.error("Codex login refresh failed (%s); the login is kept as it is, the "
                           "operator is asked to sign in again", kind)
                return {"result": "failed", "error": kind}
            # Preserve every other field the token record carries (id_token, account_id, ...);
            # only the rotated access/refresh tokens change.
            new_tokens = dict(tokens)
            new_tokens["access_token"] = access
            new_tokens["refresh_token"] = str(data.get("refresh_token") or refresh_token)
            if isinstance(data.get("id_token"), str) and data["id_token"]:
                new_tokens["id_token"] = data["id_token"]
            # Somebody else (a run's own rotated-login write-back) may have moved last_refresh
            # forward while our HTTP call was in flight: re-read once more, right before the
            # write, and keep theirs if it is newer than the token we just refreshed from.
            current = read_auth(home) or {}
            current_tokens = current.get("tokens") if isinstance(current.get("tokens"), dict) else {}
            if str(current_tokens.get("refresh_token") or "") not in ("", refresh_token):
                return {"result": "fresh"}
            new_auth = dict(current or auth)
            new_auth["tokens"] = new_tokens
            new_auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
            _write_private(_path(home), json.dumps(new_auth))
            _log.info("Codex login refreshed")
            return {"result": "refreshed"}
    finally:
        _lock.release()


def status() -> dict[str, Any]:
    """No-secret summary: {state, seconds_left}."""
    auth = read_auth()
    if auth is None:
        return {"state": "absent"}
    left = seconds_left(auth)
    if left is None:
        return {"state": "unknown"}
    state = "expired" if left <= 0 else "expiring" if left <= REFRESH_WINDOW_SECONDS else "ok"
    return {"state": state, "seconds_left": int(left)}
