"""A TRANSIENT NETWORK ERROR IS RETRIED WHERE IT HAPPENS, NOT BY FAILING THE BUILD.

crun_2f76585e (2026-10-13) failed its whole build after 1.1 minutes on one GitHub-side 502:
"git -c failed: Cloning into '.'... error: RPC failed; HTTP 502 curl 22 ... fatal: expected
'packfile'". Nothing was wrong with the build, the grant or the repository; GitHub hiccupped.

This is the one policy for that, used by the one place a build runs git (sandbox.Sandbox._git:
clone, fetch, ls-remote) and the one place BOSS calls the GitHub REST API (GitHubAdapter): the
operation is tried up to ATTEMPTS times with backoff (DELAYS, about 40 s in all) when the
failure is a transient NETWORK one (HTTP 5xx, RPC failed, early EOF, "expected 'packfile'",
connection reset, a timeout). An authentication or authorization answer (401/403/404, "could
not read Username", "Authentication failed", "Repository not found") is never retried: it will
say the same thing again, and repeating it only delays the person who has to fix it.

Each retry is said: logged, and handed to the reporter of the build it happens in (`reporting`),
which records it as a `code_task.transient_retry` event on the run.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import time
from typing import Any, Callable, Iterator, Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Tries in all (the first plus two retries), and the waits between them: ~40 s of patience,
#: inside the 30–60 s a GitHub blip lasts, and short of anything a person would notice.
ATTEMPTS = 3
DELAYS = (10.0, 30.0)

#: Patched by tests; never call time.sleep directly here.
_sleep: Callable[[float], None] = time.sleep

#: A git/HTTP failure that says nothing about the request itself: the network or the far side.
_TRANSIENT = re.compile(
    r"RPC failed|early EOF|expected '?packfile'?|unexpected disconnect|"
    r"the remote end hung up unexpectedly|"
    r"HTTP 5\d\d|returned error: 5\d\d|\b5\d\d (Bad Gateway|Service Unavailable|Gateway Time-?out|"
    r"Internal Server Error)|"
    r"Connection reset|connection was reset|Connection refused|Connection timed out|"
    r"Operation timed out|timed out|Could not resolve host|Temporary failure in name resolution|"
    r"Failed to connect|SSL_ERROR_SYSCALL|GnuTLS recv error|transfer closed|"
    r"index-pack failed|curl \d+ .*(?:5\d\d|reset|timed out)",
    re.IGNORECASE)

#: An answer about WHO is asking or WHAT they asked for: never transient, whatever else it says.
_AUTH = re.compile(
    r"\b40[134]\b|Authentication failed|could not read Username|could not read Password|"
    r"Permission denied|Repository not found|not found|access denied|Invalid username or password|"
    r"terminal prompts disabled|Bad credentials|Resource not accessible",
    re.IGNORECASE)


def is_transient_git_error(message: Any) -> bool:
    """True when a git failure is a network/far-side one that a retry may clear."""
    text = str(message or "")
    if not text or _AUTH.search(text):
        return False
    return bool(_TRANSIENT.search(text))


def is_transient_http_status(status: Optional[int]) -> bool:
    """5xx only. 401/403/404 (and every other 4xx) is an answer, not an outage."""
    return status is not None and 500 <= int(status) <= 599


def is_transient_http_error(error: BaseException) -> bool:
    """An httpx failure worth retrying: a 5xx answer, or no answer at all (connect/read
    timeout, a reset connection, a protocol error mid-response)."""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    if isinstance(error, httpx.HTTPStatusError):
        return is_transient_http_status(getattr(error.response, "status_code", None))
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


_REPORTER: contextvars.ContextVar[Optional[Callable[[dict[str, Any]], None]]] = contextvars.ContextVar(
    "boss_transient_retry_reporter", default=None)


@contextlib.contextmanager
def reporting(reporter: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    """Every retry inside this block is handed to `reporter` (a build records it as an event)."""
    token = _REPORTER.set(reporter)
    try:
        yield
    finally:
        _REPORTER.reset(token)


def _report(info: dict[str, Any]) -> None:
    log.warning("transient %s failure, retrying (attempt %s of %s in %ss): %s",
                info.get("operation"), info.get("next_attempt"), info.get("attempts"),
                info.get("wait_seconds"), info.get("error"))
    reporter = _REPORTER.get()
    if reporter is None:
        return
    try:
        reporter(info)
    except Exception:  # noqa: BLE001 — saying so never breaks the operation being retried
        log.exception("could not record a transient retry")


def call(fn: Callable[[], T], *, operation: str, transient: Callable[[BaseException], bool],
         redact: Optional[str] = None) -> T:
    """`fn()`, tried up to ATTEMPTS times while it fails `transient`ly. The last failure (or
    the first that is not transient) is raised unchanged."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001 — re-raised unless transient and tries remain
            if attempt >= ATTEMPTS or not transient(error):
                raise
            wait = DELAYS[min(attempt - 1, len(DELAYS) - 1)]
            said = str(error)
            if redact:
                said = said.replace(redact, "***")
            _report({"operation": operation, "attempt": attempt, "next_attempt": attempt + 1,
                     "attempts": ATTEMPTS, "wait_seconds": wait, "error": said[-300:]})
            _sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
