"""jobs.schedule and the event pump: scheduled broker calls, ONE process firing each.

THE LEASE IS backup_scheduler's PATTERN (services/backup_scheduler.py acquire_lease), one row per
(extension, job) in extension_job_leases: every API process ticks; a single conditional UPDATE
claims a due row only when it is free (or its lease expired, or it is already ours), so exactly one
process runs a firing. The winner runs the job, then writes the next due time and lets the lease
go. A holder that dies mid-run stops renewing; once its lease expires another process may claim the
(still due) row and run it again — at-least-once, like the event feed.

Schedules (`spec.jobs[].schedule`): a five-field cron (minute hour day-of-month month day-of-week;
`*`, `*/n`, `a-b`, `a,b,c`, numbers) or an interval (`every 30s`, `every 10m`, `every 2h`).
Jobs run install-wide (no project) and only while the extension is enabled in at least one project.

The event pump rides the same lease table, one row per (extension, `events:<project>`), so two
processes never deliver the same batch at once (events.py holds the cursor).
"""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

_log = logging.getLogger("boss.extensions.jobs")

HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
TICK_SECONDS = 15.0
EVENT_POLL = timedelta(seconds=15)
_INTERVAL = re.compile(r"^every\s+(\d+)\s*([smh])$")
_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 6))


def _field(expr: str, lo: int, hi: int, label: str) -> frozenset[int]:
    out: set[int] = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, _, raw = part.partition("/")
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError(f"{label}: bad step {raw!r}")
            step = int(raw)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            if not (x.isdigit() and y.isdigit()):
                raise ValueError(f"{label}: bad range {part!r}")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError(f"{label}: {part!r} is not a number, range or *")
        if a < lo or b > hi or a > b:
            raise ValueError(f"{label}: {part!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


def parse_schedule(schedule: str) -> Any:
    """A timedelta for `every N[smh]`, else a tuple of five frozensets. Raises ValueError."""
    s = str(schedule or "").strip().lower()
    m = _INTERVAL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(seconds=n) if unit == "s" else timedelta(minutes=n) if unit == "m" \
            else timedelta(hours=n)
        if delta < timedelta(seconds=30):
            raise ValueError("a job runs at most every 30 seconds")
        return delta
    parts = s.split()
    if len(parts) != 5:
        raise ValueError("a schedule is five cron fields (*/15 * * * *) or `every 10m`")
    return tuple(_field(p, lo, hi, label) for p, (label, lo, hi) in zip(parts, _FIELDS))


def next_fire(schedule: str, after: datetime) -> datetime:
    """The first firing strictly after `after` (UTC)."""
    parsed = parse_schedule(schedule)
    if isinstance(parsed, timedelta):
        return after + parsed
    minute, hour, day, month, weekday = parsed
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if (t.minute in minute and t.hour in hour and t.day in day and t.month in month
                and (t.isoweekday() % 7) in weekday):
            return t
        t += timedelta(minutes=1)
    raise ValueError("the schedule never fires")


# ------------------------------------------------------------------------------------------ #
# the lease
# ------------------------------------------------------------------------------------------ #

def ensure_row(connection: Any, extension_id: str, job: str, first_due: datetime) -> None:
    connection.execute(text(
        "INSERT INTO extension_job_leases (extension_id, job, holder, expires_at, next_run_at) "
        "VALUES (:e, :j, '', :due, :due) ON CONFLICT (extension_id, job) DO NOTHING"),
        {"e": extension_id, "j": job, "due": first_due})


def claim(connection: Any, extension_id: str, job: str, holder: str, now: datetime,
          ttl: timedelta) -> bool:
    """Take the lease on a DUE row if it is free, expired or ours. True when we hold it."""
    row = connection.execute(text(
        "UPDATE extension_job_leases SET holder = :h, expires_at = :exp "
        "WHERE extension_id = :e AND job = :j AND next_run_at <= :now "
        "AND (holder = :h OR holder = '' OR expires_at < :now) RETURNING holder"),
        {"e": extension_id, "j": job, "h": holder, "exp": now + ttl, "now": now}).first()
    return bool(row and row[0] == holder)


def finish(connection: Any, extension_id: str, job: str, holder: str, *, next_run_at: datetime,
           status: str, error: Optional[str], now: datetime) -> None:
    connection.execute(text(
        "UPDATE extension_job_leases SET holder = '', expires_at = :now, next_run_at = :nxt, "
        "last_run_at = :now, last_status = :st, last_error = :err, runs = runs + 1 "
        "WHERE extension_id = :e AND job = :j AND holder = :h"),
        {"e": extension_id, "j": job, "h": holder, "now": now, "nxt": next_run_at, "st": status,
         "err": (error or None) and str(error)[:1000]})


def run_due(engine: Any, *, holder: str = HOLDER, now: Optional[datetime] = None,
            event_grace_seconds: float = 2.0) -> list[dict[str, Any]]:
    """One tick: every due job (and event pump) this process wins, run. Returns what ran."""
    from . import broker, events
    from .registry import list_installed

    now = now or datetime.now(timezone.utc)
    ran: list[dict[str, Any]] = []
    with engine.begin() as c:
        installs = [e for e in list_installed(c) if e.enabled_anywhere]
    for ext in installs:
        if not ext.trusted:
            continue
        manifest = ext.manifest
        ttl = timedelta(seconds=manifest.spec.runtime.timeoutSeconds + 60)
        for job in manifest.spec.jobs if manifest.has("jobs.schedule") else []:
            with engine.begin() as c:
                ensure_row(c, ext.extension_id, job.name, next_fire(job.schedule, now))
                won = claim(c, ext.extension_id, job.name, holder, now, ttl)
            if not won:
                continue
            outcome = broker.call(engine, ext, "job.run", {"name": job.name})
            with engine.begin() as c:
                finish(c, ext.extension_id, job.name, holder, next_run_at=next_fire(job.schedule, now),
                       status="ok" if outcome.ok else "failed", error=outcome.error, now=now)
            ran.append({"extension": ext.extension_id, "job": job.name, "ok": outcome.ok,
                        "error": outcome.error})
        if manifest.has("events.subscribe"):
            for project_id in sorted(p for p, row in ext.projects.items() if row.get("enabled")):
                key = f"events:{project_id}"
                with engine.begin() as c:
                    ensure_row(c, ext.extension_id, key, now)
                    won = claim(c, ext.extension_id, key, holder, now, ttl)
                if not won:
                    continue
                result = events.pump(engine, ext, project_id, grace_seconds=event_grace_seconds)
                with engine.begin() as c:
                    finish(c, ext.extension_id, key, holder, next_run_at=now + EVENT_POLL,
                           status="ok" if result["ok"] else "failed", error=result.get("error"),
                           now=now)
                ran.append({"extension": ext.extension_id, "job": key, **result})
    return ran


# ------------------------------------------------------------------------------------------ #
# the thread (system_threads.start_all)
# ------------------------------------------------------------------------------------------ #

_STARTED = threading.Event()


def start(engine: Any, settings: Any) -> bool:
    if _STARTED.is_set():
        return False
    _STARTED.set()
    interval = float(getattr(settings, "extensions_tick_seconds", TICK_SECONDS) or TICK_SECONDS)

    def loop() -> None:
        stop = threading.Event()
        while not stop.wait(interval):
            try:
                run_due(engine)
            except Exception:  # noqa: BLE001 — a bad tick is logged; the next one tries again
                _log.warning("extension scheduler tick failed", exc_info=True)

    threading.Thread(target=loop, daemon=True, name="boss-extension-scheduler").start()
    return True
