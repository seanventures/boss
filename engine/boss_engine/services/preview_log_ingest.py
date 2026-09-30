"""Land a preview's CONTAINER output into `app_logs`, where the Activity log can read it.

The container log of an app BOSS builds was reachable only by asking for it: `read_preview_log`
reads a file on the preview host, or shells out to `docker logs`. That makes it available to an
agent holding a `prev_` id and to nobody else. It never reached the Activity log, so it could
not be filtered, searched, or read next to the workflow step that caused it — and an operator
looking at a failed build on that screen saw BOSS's own records and none of the app's.

This copies the log across at the one moment it is both complete and still readable: when the
preview reaches a terminal state. `app_logs` already has the right shape for it — a project, a
run, a service, a level and a message per line — so the rows join the existing `app_log` source
with no schema change and no new query.

Deliberately a SNAPSHOT, not a tail. A streaming pipeline would have to own a process per live
preview and survive its host; copying the bounded tail once, at the end, costs one insert batch
and answers the question that is actually asked ("why did this build fail"). A preview that is
still running is served by `stream_preview_logs`, which already exists.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..ports import sql
from .core import make_id

logger = logging.getLogger(__name__)

#: Per line, matching the app_logs convention. A stack trace line is long; a line longer than
#: this is almost always minified output that helps nobody.
_LINE_LIMIT = 4000

#: How many lines of the tail to keep. The snapshot itself is already capped at 12,000 chars by
#: `preview_log_snapshot`; this bounds the row count so one pathological build cannot write
#: thousands of rows into a screen meant to be read.
_MAX_LINES = 400

#: Lines carrying any of these are recorded at `error`, so the level filter finds them. The log
#: of a failed build is mostly ordinary output with two lines that matter.
_ERROR_MARKERS = ("error", "fatal", "exception", "traceback", "cannot find",
                  "could not resolve", "failed", "err!")
_WARN_MARKERS = ("warn", "deprecated")


def _level_for_line(line: str) -> str:
    lowered = line.lower()
    if any(marker in lowered for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in lowered for marker in _WARN_MARKERS):
        return "warning"
    return "info"


def ingest_preview_log(
    engine: Any,
    preview_id: str,
    *,
    project_id: str,
    run_id: Optional[str] = None,
    app_name: Optional[str] = None,
) -> int:
    """Copy one preview's log tail into `app_logs`. Returns the number of lines written.

    Never raises. This is called from the preview's finishing thread, where an exception would
    lose the status update that matters more than the log does.
    """
    try:
        from ..db import begin
        from .preview_service import preview_log_snapshot

        snapshot = preview_log_snapshot(engine, preview_id)
    except Exception:  # noqa: BLE001 — a missing preview or an unreadable host is not fatal
        logger.debug("preview %s: no log snapshot to ingest", preview_id, exc_info=True)
        return 0

    body = (snapshot.get("log") or "").strip()
    # The captured `error` is worth a row even when no log body survived — that is the common
    # case for a preview that died before writing anything, and it is the line an operator
    # needs. Without it, "no log body is readable from here" would be the whole answer.
    error_text = (snapshot.get("error") or "").strip()
    if not body and not error_text:
        return 0

    lines = [line.rstrip() for line in body.splitlines() if line.strip()][-_MAX_LINES:]
    service = app_name or f"preview:{preview_id}"
    records: list[tuple[str, str]] = [(_level_for_line(line), line[:_LINE_LIMIT])
                                      for line in lines]
    if error_text:
        failure_kind = (snapshot.get("failure_kind") or "").strip()
        prefix = f"[{failure_kind}] " if failure_kind else ""
        records.append(("error", f"{prefix}{error_text}"[:_LINE_LIMIT]))

    if not records:
        return 0

    try:
        with begin(engine) as connection:
            for level, message in records:
                connection.execute(
                    sql("INSERT INTO app_logs "
                        "(id, project_id, run_id, service, level, logger, message) "
                        "VALUES (:id,:p,:r,:s,:lv,:lg,:m)"),
                    {"id": make_id("alog"), "p": project_id, "r": run_id or None,
                     "s": service, "lv": level, "lg": "preview.container",
                     "m": message},
                )
    except Exception:  # noqa: BLE001
        logger.debug("preview %s: could not write log rows", preview_id, exc_info=True)
        return 0
    return len(records)


__all__ = ["ingest_preview_log"]
