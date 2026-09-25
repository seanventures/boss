"""Starting a workflow because it is Monday, rather than because somebody asked.

WHY THIS EXISTS
---------------
Workflows started on demand only. That is fine for "edit this video" and useless for anything
whose value IS the recurrence: a Monday digest nobody has to remember to ask for, a newsletter,
a check on actions that were supposed to be done by now. Several examples were blocked on this
one missing piece rather than on anything to do with their own subject.

NOT CRON, DELIBERATELY
----------------------
Cron is five fields of positional syntax that nobody reads correctly at a glance, and most of
its expressive range is irrelevant here — a schedule for a business report is hourly, daily or
weekly, at a time somebody chose. So the shape is explicit: a cadence, a minute, an hour, and a
weekday when it is weekly. It can be rendered back as an English sentence, validated field by
field with a message naming the field, and it cannot express the ambiguous cases that make cron
entries wrong in production.

If a genuine cron need turns up, this is a column away. Starting there would have shipped a
parser nobody needed.

TIME ZONES ARE THE POINT, NOT A DETAIL
--------------------------------------
"Monday 8am" means 8am where the person reading it lives. Storing UTC and letting the operator
do the arithmetic is how a digest arrives at 4am twice a year — so the zone is stored with the
schedule and the next occurrence is computed IN it. `next_run_at` is UTC, because an instant is
an instant; the fields that produced it are local, because that is what was meant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

CADENCES = ("hourly", "daily", "weekly")
#: Monday is 0, matching `datetime.weekday()` so no translation is needed anywhere.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: How many runs one schedule may start before BOSS stops it. A schedule that fires and fails
#: forever is a schedule that spends money forever; this is the fuse, not a feature.
MAX_CONSECUTIVE_FAILURES = 5


class ScheduleError(ValueError):
    """Something about the schedule is wrong. The message is shown to the operator."""


def validate(
    *,
    cadence: str,
    minute: int,
    hour: int,
    weekday: Optional[int],
    tz_name: str,
) -> tuple[str, int, int, Optional[int], str]:
    """Check a schedule's shape, field by field, saying which field is wrong.

    Returned normalised rather than validated in place: hour is meaningless for an hourly
    schedule and weekday for anything but a weekly one, and storing whatever was passed for
    them leaves two fields that render into the sentence and mean nothing.
    """
    cadence = str(cadence or "").strip().lower()
    if cadence not in CADENCES:
        raise ScheduleError(f"cadence must be one of {', '.join(CADENCES)} — got {cadence!r}.")

    try:
        minute = int(minute)
    except (TypeError, ValueError):
        raise ScheduleError("minute must be a whole number between 0 and 59.") from None
    if not 0 <= minute <= 59:
        raise ScheduleError(f"minute must be between 0 and 59 — got {minute}.")

    if cadence == "hourly":
        hour, weekday = 0, None
    else:
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            raise ScheduleError("hour must be a whole number between 0 and 23.") from None
        if not 0 <= hour <= 23:
            raise ScheduleError(f"hour must be between 0 and 23 — got {hour}.")

    if cadence == "weekly":
        if weekday is None:
            raise ScheduleError("a weekly schedule needs a weekday (0 is Monday).")
        try:
            weekday = int(weekday)
        except (TypeError, ValueError):
            raise ScheduleError("weekday must be a whole number, 0 for Monday.") from None
        if not 0 <= weekday <= 6:
            raise ScheduleError(
                f"weekday must be 0 (Monday) to 6 (Sunday) — got {weekday}.")
    else:
        weekday = None

    tz_name = str(tz_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(
            f"'{tz_name}' is not a time zone BOSS knows. Use an IANA name such as "
            f"Europe/London, America/New_York or UTC.") from None

    return cadence, minute, hour, weekday, tz_name


def describe(cadence: str, minute: int, hour: int, weekday: Optional[int], tz_name: str) -> str:
    """The schedule as a sentence. What the console shows, and what a test can assert on."""
    if cadence == "hourly":
        return f"every hour at :{minute:02d} ({tz_name})"
    at = f"{hour:02d}:{minute:02d}"
    if cadence == "daily":
        return f"every day at {at} ({tz_name})"
    return f"every {WEEKDAYS[int(weekday or 0)]} at {at} ({tz_name})"


def next_occurrence(
    *,
    cadence: str,
    minute: int,
    hour: int,
    weekday: Optional[int],
    tz_name: str,
    after: Optional[datetime] = None,
) -> datetime:
    """The next time this schedule is due, as an instant in UTC.

    STRICTLY AFTER `after`, never equal to it. A schedule that returns its own firing time would
    be due again the moment it ran, which is an infinite loop that looks like an eager worker.

    Computed in the schedule's own zone and converted, so a daily 08:00 stays 08:00 across a DST
    change instead of drifting an hour twice a year. The pathological cases DST creates — a
    local time that does not exist on a spring-forward day, or happens twice on a fall-back one
    — resolve to a real instant rather than raising: a digest an hour early beats a digest that
    never fires and reports nothing wrong.
    """
    zone = ZoneInfo(tz_name)
    now = (after or datetime.now(timezone.utc)).astimezone(zone)

    if cadence == "hourly":
        candidate = now.replace(minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(hours=1)
        return candidate.astimezone(timezone.utc)

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cadence == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    # Weekly. Move to the wanted weekday, then forward a week if that moment has passed.
    ahead = (int(weekday or 0) - candidate.weekday()) % 7
    candidate += timedelta(days=ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


# ── Storage ────────────────────────────────────────────────────────────────────

def _row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "workflow_key": row["workflow_key"],
        "name": row["name"],
        "cadence": row["cadence"],
        "minute": row["minute"],
        "hour": row["hour"],
        "weekday": row["weekday"],
        "timezone": row["timezone"],
        "input": row["input"] if isinstance(row["input"], dict) else {},
        "actor": row["actor"],
        "enabled": bool(row["enabled"]),
        "next_run_at": row["next_run_at"].isoformat() if row["next_run_at"] else None,
        "last_run_at": row["last_run_at"].isoformat() if row["last_run_at"] else None,
        "last_run_id": row["last_run_id"],
        "last_error": row["last_error"],
        "consecutive_failures": int(row["consecutive_failures"] or 0),
        # Rendered here rather than in the console, so the API, the CLI and the UI cannot
        # describe the same schedule three different ways.
        "describes": describe(row["cadence"], row["minute"], row["hour"],
                              row["weekday"], row["timezone"]),
    }


def create(
    connection: Connection,
    *,
    project_id: str,
    workflow_key: str,
    name: str,
    cadence: str,
    minute: int,
    actor: str,
    hour: int = 0,
    weekday: Optional[int] = None,
    tz_name: str = "UTC",
    input_data: Optional[dict[str, Any]] = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Schedule a workflow. The first occurrence is computed now, not at the first tick."""
    name = str(name or "").strip()
    if not name:
        raise ScheduleError("a schedule needs a name.")
    workflow_key = str(workflow_key or "").strip()
    if not workflow_key:
        raise ScheduleError("say which workflow this runs.")

    # THE WORKFLOW HAS TO EXIST AND BE RUNNABLE. A schedule pointing at nothing fires forever
    # into an error nobody reads, and the check costs one query now against every tick later.
    runnable = connection.execute(
        text(
            """
            SELECT d.active_version_id FROM workflow_definitions d
            WHERE d.project_id = :p AND d.workflow_key = :k
            """
        ),
        {"p": project_id, "k": workflow_key},
    ).mappings().first()
    if runnable is None:
        raise ScheduleError(f"this project has no workflow '{workflow_key}'.")
    if not runnable["active_version_id"]:
        raise ScheduleError(
            f"'{workflow_key}' has no published version yet, so there is nothing to run.")

    cadence, minute, hour, weekday, tz_name = validate(
        cadence=cadence, minute=minute, hour=hour, weekday=weekday, tz_name=tz_name)
    due = next_occurrence(cadence=cadence, minute=minute, hour=hour,
                          weekday=weekday, tz_name=tz_name)

    row = connection.execute(
        text(
            """
            INSERT INTO workflow_schedules (
                id, project_id, workflow_key, name, cadence, minute, hour, weekday,
                timezone, input, actor, enabled, next_run_at
            ) VALUES (
                :id, :p, :k, :name, :cadence, :minute, :hour, :weekday,
                :tz, CAST(:input AS JSONB), :actor, :enabled, :due
            ) RETURNING *
            """
        ),
        {"id": make_id("wsc"), "p": project_id, "k": workflow_key, "name": name,
         "cadence": cadence, "minute": minute, "hour": hour, "weekday": weekday,
         "tz": tz_name, "input": json.dumps(input_data or {}), "actor": actor,
         "enabled": bool(enabled), "due": due},
    ).mappings().one()
    emit_event(connection, project_id, "workflow.schedule_created", actor,
               "workflow_schedule", row["id"],
               {"workflow_key": workflow_key, "describes": describe(
                   cadence, minute, hour, weekday, tz_name)})
    return _row(row)


def list_schedules(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT * FROM workflow_schedules WHERE project_id=:p ORDER BY name"),
        {"p": project_id},
    ).mappings().all()
    return [_row(r) for r in rows]


def set_enabled(
    connection: Connection, project_id: str, schedule_id: str, enabled: bool, actor: str,
) -> dict[str, Any]:
    """Pause or resume. Resuming recomputes the next occurrence.

    Without that, a schedule paused for a month comes back due immediately and fires at once —
    which is never what "resume" meant.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_schedules WHERE id=:i AND project_id=:p"),
        {"i": schedule_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise ScheduleError(f"no schedule '{schedule_id}'.")
    due = next_occurrence(cadence=row["cadence"], minute=row["minute"], hour=row["hour"],
                          weekday=row["weekday"], tz_name=row["timezone"]) if enabled \
        else row["next_run_at"]
    updated = connection.execute(
        text(
            """
            UPDATE workflow_schedules
            SET enabled=:e, next_run_at=:due, consecutive_failures=0, last_error=NULL,
                updated_at=now()
            WHERE id=:i AND project_id=:p RETURNING *
            """
        ),
        {"e": bool(enabled), "due": due, "i": schedule_id, "p": project_id},
    ).mappings().one()
    emit_event(connection, project_id,
               "workflow.schedule_enabled" if enabled else "workflow.schedule_disabled",
               actor, "workflow_schedule", schedule_id, {"name": row["name"]})
    return _row(updated)


def delete(connection: Connection, project_id: str, schedule_id: str, actor: str) -> bool:
    result = connection.execute(
        text("DELETE FROM workflow_schedules WHERE id=:i AND project_id=:p"),
        {"i": schedule_id, "p": project_id},
    )
    if result.rowcount:
        emit_event(connection, project_id, "workflow.schedule_deleted", actor,
                   "workflow_schedule", schedule_id, {})
    return bool(result.rowcount)


# ── Firing ─────────────────────────────────────────────────────────────────────

def run_due(
    connection: Connection,
    *,
    project_id: str,
    now: Optional[datetime] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Start a run for every schedule that is due, and move each one on.

    THE NEXT OCCURRENCE IS ADVANCED WHETHER OR NOT THE RUN STARTED. A schedule that failed to
    start and kept its due time is due on the next tick, and the one after — fifteen seconds
    apart, forever, against whatever made it fail. Advancing first means a failure costs one
    run, not a loop.

    Computed from NOW rather than from the missed time, so an install that was off for a week
    starts one run when it comes back rather than seven. Nobody wants six stale digests.

    SELECT ... FOR UPDATE SKIP LOCKED because more than one worker may tick at once: two
    workers claiming the same schedule is the same digest sent twice.
    """
    from .workflows import start_run

    now = now or datetime.now(timezone.utc)
    due_rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_schedules
            WHERE project_id = :p AND enabled AND next_run_at <= :now
            ORDER BY next_run_at
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"p": project_id, "now": now, "limit": int(limit)},
    ).mappings().all()

    started: list[dict[str, Any]] = []
    for row in due_rows:
        following = next_occurrence(
            cadence=row["cadence"], minute=row["minute"], hour=row["hour"],
            weekday=row["weekday"], tz_name=row["timezone"], after=now)

        fired_at = row["next_run_at"]
        try:
            # RESOLVED AT FIRE TIME, EVERY TIME. The schedule stores a workflow KEY, so it runs
            # whatever version is current when it fires — edit the workflow and the next run
            # picks the change up. Pinning a version at creation would quietly keep running the
            # one that happened to be active months ago.
            #
            # Not via selector_kind: those resolve through the workflow MAPPING table, which is
            # a different question ("which workflow handles this class of work") and would make
            # a schedule depend on a mapping somebody may not have.
            active = connection.execute(
                text(
                    """
                    SELECT active_version_id FROM workflow_definitions
                    WHERE project_id = :p AND workflow_key = :k
                    """
                ),
                {"p": project_id, "k": row["workflow_key"]},
            ).scalar()
            if not active:
                raise ScheduleError(
                    f"'{row['workflow_key']}' has no published version to run.")
            run = start_run(
                connection,
                project_id=project_id,
                actor=row["actor"],
                input_data=dict(row["input"] if isinstance(row["input"], dict) else {}),
                # THE SCHEDULED TIME, NOT NOW. Two workers that both got past the lock, or a
                # tick retried after a crash, produce the same key and therefore one run.
                dedupe_key=f"schedule:{row['id']}:{fired_at.isoformat()}",
                workflow_version_id=str(active),
            )
            connection.execute(
                text(
                    """
                    UPDATE workflow_schedules
                    SET next_run_at=:due, last_run_at=:fired, last_run_id=:run,
                        last_error=NULL, consecutive_failures=0, updated_at=now()
                    WHERE id=:i
                    """
                ),
                {"due": following, "fired": now, "run": run.get("id"), "i": row["id"]},
            )
            started.append({"schedule_id": row["id"], "name": row["name"],
                            "workflow_key": row["workflow_key"], "run_id": run.get("id"),
                            "next_run_at": following.isoformat()})
        except Exception as error:  # noqa: BLE001 — one bad schedule must not stop the rest
            failures = int(row["consecutive_failures"] or 0) + 1
            stop = failures >= MAX_CONSECUTIVE_FAILURES
            connection.execute(
                text(
                    """
                    UPDATE workflow_schedules
                    SET next_run_at=:due, last_error=:err, consecutive_failures=:n,
                        enabled = CASE WHEN :stop THEN FALSE ELSE enabled END,
                        updated_at=now()
                    WHERE id=:i
                    """
                ),
                {"due": following, "err": str(error)[:500], "n": failures,
                 "stop": stop, "i": row["id"]},
            )
            emit_event(
                connection, project_id,
                "workflow.schedule_disabled" if stop else "workflow.schedule_failed",
                row["actor"], "workflow_schedule", row["id"],
                {"name": row["name"], "error": str(error)[:500],
                 "consecutive_failures": failures},
            )
            started.append({"schedule_id": row["id"], "name": row["name"],
                            "error": str(error)[:200], "disabled": stop})
    return started
