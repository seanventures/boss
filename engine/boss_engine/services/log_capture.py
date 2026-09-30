"""Container logs, in the Activity Log.

BOSS writes a great deal to its container logs — which command ran and for how long, how long a
stage queued for a model lane, why a preview refused. None of it reached the screen an operator
is sent to. So "Engineer implementing — 27m46s — no model call" was accurate and unhelpful: it
said the model was not the cost without saying what was, and the only honest answer to "where
did the time go" was "open a shell on the box". Most people cannot.

This captures those records into `app_logs`, which the Activity Log reads as another source.

FOUR RULES, because a logging handler that writes to Postgres is a good way to take down the
thing it is observing:

  1. BOUNDED VOLUME. Only WARNING and above, plus an allowlist of loggers whose INFO lines are
     the diagnosis (the sandbox's per-command timings, the pacing split). Everything else is
     dropped at the handler, before any work is done.
  2. NEVER IN THE REQUEST PATH. Records go to an in-memory queue; one background thread writes
     them in batches. A slow database delays the logs, never the run.
  3. BOUNDED MEMORY. The queue has a hard cap and drops the OLDEST when full, counting what it
     dropped so the gap is visible rather than silent.
  4. NEVER RECURSIVE, NEVER FATAL. The writer suppresses its own logging, and every failure is
     swallowed. A build that fell over because its log line could not be stored would be
     absurd.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional

from .core import make_id

#: INFO from these loggers is diagnosis, not chatter — they are the timings that answer "where
#: did the time go". Everything else needs WARNING or worse to be kept.
_INFO_ALLOWLIST = (
    "boss_engine.services.sandbox",                     # per-command durations
    "boss_engine.services.code_agent",                  # model dispatch: queueing vs work
    "boss_engine.services.governed_code_task_service",  # stage timings
    "boss_engine.services.preview_service",
)

#: Enough to hold a burst while the writer catches up; small enough that a stalled database
#: cannot grow the process without bound.
_MAX_QUEUE = 2000

#: One write per batch rather than per record. A busy run logs faster than Postgres wants to be
#: asked, and a row-at-a-time handler would spend more time in the driver than the work does.
_BATCH = 50
_FLUSH_SECONDS = 2.0

_MESSAGE_LIMIT = 4000

_lock = threading.Lock()
_handler: Optional["ActivityLogHandler"] = None


class ActivityLogHandler(logging.Handler):
    """Queues log records for the Activity Log. Drops rather than blocks."""

    def __init__(self, engine, service: str) -> None:
        super().__init__(level=logging.INFO)
        self._engine = engine
        self._service = service
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_MAX_QUEUE)
        self._dropped = 0
        self._stop = threading.Event()
        self._writer = threading.Thread(
            target=self._drain, name="activity-log-writer", daemon=True)
        self._writer.start()

    # -- capture ---------------------------------------------------------------

    def _keeps(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.levelno >= logging.INFO and record.name.startswith(_INFO_ALLOWLIST)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._keeps(record):
                return
            # The writer's own thread must never feed itself.
            if getattr(record, "_boss_activity_writer", False):
                return
            from .llm_telemetry import get_llm_context

            ctx = get_llm_context() or {}
            item = {
                "level": record.levelname.lower(),
                "logger": record.name[:200],
                "message": self.format(record)[:_MESSAGE_LIMIT],
                "project_id": ctx.get("project_id"),
                "run_id": ctx.get("run_id"),
                "service": self._service,
            }
            try:
                self._q.put_nowait(item)
            except queue.Full:
                # Drop the OLDEST: the newest lines are the ones describing what is happening
                # now, and a full queue during an incident is exactly when they matter.
                try:
                    self._q.get_nowait()
                    self._dropped += 1
                    self._q.put_nowait(item)
                except Exception:  # noqa: BLE001
                    self._dropped += 1
        except Exception:  # noqa: BLE001 — logging must never raise
            pass

    # -- write -----------------------------------------------------------------

    def _drain(self) -> None:
        batch: list[dict[str, Any]] = []
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
                batch.append(item)
            except queue.Empty:
                pass
            due = len(batch) >= _BATCH or (batch and time.monotonic() - last >= _FLUSH_SECONDS)
            if due:
                self._write(batch)
                batch = []
                last = time.monotonic()
        if batch:
            self._write(batch)

    def _write(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            from ..db import begin
            from ..ports import sql

            with begin(self._engine) as c:
                for item in batch:
                    c.execute(
                        sql("INSERT INTO app_logs "
                            "(id, project_id, run_id, service, level, logger, message) "
                            "VALUES (:id,:p,:r,:s,:lv,:lg,:m)"),
                        {"id": make_id("alog"), "p": item.get("project_id"),
                         "r": item.get("run_id"), "s": item.get("service"),
                         "lv": item.get("level"), "lg": item.get("logger"),
                         "m": item.get("message")},
                    )
        except Exception:  # noqa: BLE001
            # Losing a log line is a nuisance. Raising here would make the writer thread die and
            # lose every line after it, which is the actual failure worth avoiding.
            pass

    def close(self) -> None:
        self._stop.set()
        super().close()

    def note(self, message: str, *, level: str = "info") -> None:
        """Queue a record directly, bypassing the logging machinery."""
        try:
            self._q.put_nowait({
                "level": level, "logger": "boss_engine.services.log_capture",
                "message": message[:_MESSAGE_LIMIT], "project_id": None,
                "run_id": None, "service": self._service,
            })
        except Exception:  # noqa: BLE001
            pass

    @property
    def dropped(self) -> int:
        return self._dropped


def install(engine, *, service: str) -> Optional[ActivityLogHandler]:
    """Attach the handler to the root logger. Idempotent."""
    global _handler
    with _lock:
        if _handler is not None:
            return _handler
        try:
            handler = ActivityLogHandler(engine, service)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logging.getLogger().addHandler(handler)
            # A HANDLER ON THE ROOT LOGGER IS NOT ENOUGH.
            #
            # `logger.info(...)` is discarded by the LOGGER before any handler is consulted, if
            # that logger's effective level is higher — and the default is WARNING. So the
            # allowlisted INFO lines, which are the entire point of this, were never created:
            # the table stayed empty and looked like a broken handler rather than a filtered
            # one. Raising the level on exactly those loggers, and nothing else, is what makes
            # them exist. Handler-level filtering still decides what is KEPT.
            for name in _INFO_ALLOWLIST:
                logging.getLogger(name).setLevel(logging.INFO)
            # Root stays wherever the service put it, except that it must not sit above INFO or
            # it would gate propagation for anything that relies on inheritance.
            root = logging.getLogger()
            if root.level > logging.INFO:
                root.setLevel(logging.INFO)
            _handler = handler
            # ANNOUNCE ITSELF, so an empty log is unambiguous.
            #
            # After deploying, `app_logs` was empty and there was no way to tell "capture is
            # live and nothing noteworthy has happened" from "capture is broken". They look
            # identical and they need opposite responses. One row at startup settles it, and it
            # is written straight to the queue rather than through logging — a self-test that
            # depends on the thing it is testing proves nothing.
            handler.note(f"activity-log capture installed for {service}")
            return handler
        except Exception:  # noqa: BLE001
            return None


def installed() -> Optional[ActivityLogHandler]:
    return _handler
