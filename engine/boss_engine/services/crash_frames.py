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
