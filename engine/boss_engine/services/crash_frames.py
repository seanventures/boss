"""A CAUGHT EXCEPTION KEEPS ITS TRACEBACK.

crun_944194c4c2ec4534b84e failed after 75 minutes with nothing but
`I/O operation on closed file. [substrate=local_subprocess]`: the governed pipeline caught the
exception and stored str(exc), so where it was raised was lost everywhere — the run, the event,
the worker log. Root-causing it meant reading code until one call site fitted.

Now every exception the pipeline catches is logged with its traceback (credentials redacted with
redaction.redact_secrets), and the one that fails a run leaves its last frames on the run
(`state.crash_frames`): file, line and function only — never a local, an argument or a message,
so nothing a frame held can leak through the record.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Any, Optional

#: How many of the innermost frames a run keeps.
FRAME_LIMIT = 15


def _short(path: str) -> str:
    """The path from the package root (boss_engine/…, site-packages/…), not the image's layout."""
    for marker in ("/boss_engine/", "/boss_worker/", "/boss_api/", "/site-packages/", "/lib/python"):
        at = path.rfind(marker)
        if at >= 0:
            return path[at + 1:]
    return path


def frames(exc: Optional[BaseException], limit: int = FRAME_LIMIT) -> list[dict[str, Any]]:
    """The innermost `limit` frames of `exc`'s traceback: {file, line, function}. Never raises."""
    try:
        if exc is None or exc.__traceback__ is None:
            return []
        return [{"file": _short(f.filename), "line": int(f.lineno or 0), "function": f.name}
                for f in traceback.extract_tb(exc.__traceback__)[-limit:]]
    except Exception:  # noqa: BLE001 — a crash record must never become a second crash
        return []


def record(exc: Optional[BaseException]) -> dict[str, Any]:
    """What a failed run stores: the exception's type, its frames, and the frames of the
    exception it was raised from (or during), when there is one. No message, no values."""
    out: dict[str, Any] = {"type": type(exc).__name__ if exc is not None else None,
                           "frames": frames(exc)}
    cause = (exc.__cause__ or exc.__context__) if exc is not None else None
    if cause is not None and cause is not exc:
        out["cause"] = {"type": type(cause).__name__, "frames": frames(cause)}
    return out


def formatted(exc: Optional[BaseException]) -> str:
    """The full traceback (chained), with credentials redacted."""
    try:
        from .redaction import redact_secrets

        if exc is None:
            return ""
        return redact_secrets("".join(traceback.format_exception(type(exc), exc,
                                                                 exc.__traceback__)))
    except Exception:  # noqa: BLE001
        return f"{type(exc).__name__} (traceback unavailable)"


def log_caught(logger: logging.Logger, where: str = "",
               exc: Optional[BaseException] = None, *, level: int = logging.WARNING) -> None:
    """Log the exception being handled (or `exc`) with its redacted traceback. Call it first in
    an `except` block. Never raises."""
    try:
        exc = exc if exc is not None else sys.exc_info()[1]
        if exc is None:
            return
        logger.log(level, "caught %s%s:\n%s", type(exc).__name__,
                   f" ({where})" if where else "", formatted(exc).rstrip(),
                   stacklevel=2)  # the record names the handler's line, not this one
    except Exception:  # noqa: BLE001
        pass


#: The event a build records each time it swallows an exception on purpose (bookkeeping that
#: must never fail the run). build_profile counts them, so a swallowed error that repeats on
#: every build shows up in the profile instead of only in a warning nobody reads.
SWALLOWED_EVENT = "code_task.swallowed_error"


def record_swallowed(logger: logging.Logger, where: str, *, project_id: Optional[str],
                     run_id: Optional[str], transaction: Any = None, connection: Any = None,
                     exc: Optional[BaseException] = None) -> None:
    """A caught exception the build deliberately carries on past: log it at ERROR with the run
    id, and record a SWALLOWED_EVENT on the run. Call it first in the `except` block.

    `transaction` (a zero-argument callable returning a transaction context, e.g.
    `lambda: _transaction(engine)`) records the event in a transaction of its own; `connection` (a caller's open
    transaction) records it under a savepoint, so a failed record cannot doom the caller's
    work. With neither, it is logged only. Never raises.

    WHY. Two real errors (an AttributeError reading the budget state, a SQL syntax error
    marking a stage done) were each swallowed dozens of times at WARNING, so nothing noticed
    that every build routed blind and every resume re-ran finished stages."""
    try:
        exc = exc if exc is not None else sys.exc_info()[1]
        if exc is None:
            return
        logger.log(logging.ERROR, "caught %s (%s run=%s):\n%s", type(exc).__name__, where,
                   run_id, formatted(exc).rstrip(), stacklevel=2)
    except Exception:  # noqa: BLE001
        return
    if not (project_id and run_id) or (transaction is None and connection is None):
        return
    try:
        from .core import emit_event
        from .redaction import redact_secrets

        first = (str(exc).splitlines() or [""])[0]
        data = {"where": where, "error": type(exc).__name__,
                "message": redact_secrets(first)[:200], "frames": frames(exc, limit=3)}

        if connection is not None:
            with connection.begin_nested():
                emit_event(connection, project_id, SWALLOWED_EVENT, "boss", "code_task_run",
                           run_id, data)
        else:
            with transaction() as c:
                emit_event(c, project_id, SWALLOWED_EVENT, "boss", "code_task_run", run_id, data)
    except Exception:  # noqa: BLE001 — the log line above already carries it
        logger.error("could not record %s for run %s (%s)", SWALLOWED_EVENT, run_id, where,
                     exc_info=True)
