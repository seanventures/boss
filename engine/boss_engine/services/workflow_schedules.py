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


def _as_input(input_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The values a schedule will hand to every unattended run.

    Missing means none. Anything else has to be an object: a list or a string
    would be stored and then fail, or worse succeed, as something the workflow
    never declared.
    """
    if input_data in (None, ""):
        return {}
    if not isinstance(input_data, dict):
        raise ScheduleError(
            "input must be an object of the workflow's declared fields.")
    return input_data


def _active_spec(connection: Connection, project_id: str, workflow_key: str) -> dict[str, Any]:
    """The published spec a schedule will actually run. Refused when there is none."""
    row = connection.execute(
        text(
            """
            SELECT d.active_version_id, v.spec
            FROM workflow_definitions d
            LEFT JOIN workflow_versions v ON v.id = d.active_version_id
            WHERE d.project_id = :p AND d.workflow_key = :k
            """
        ),
        {"p": project_id, "k": workflow_key},
    ).mappings().first()
    if row is None:
        raise ScheduleError(f"this project has no workflow '{workflow_key}'.")
    if not row["active_version_id"]:
        raise ScheduleError(
            f"'{workflow_key}' has no published version yet, so there is nothing to run.")
    spec = row["spec"]
    if isinstance(spec, str):
        spec = json.loads(spec)
    return spec if isinstance(spec, dict) else {}


def require_schedule_inputs(spec: dict[str, Any], input_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Refuse a schedule that would start without the inputs the workflow declared.

    Checked here, not on Monday morning. A schedule runs with nobody at the keyboard, so a
    missing recipient or topic is not a prompt the operator can answer later — it is a run
    that fails after the work in front of it has already been spent, or a newsletter with
    nowhere to go. Name-to-type schemas do not say what is required, and this does not guess.
    """
    from .workflows import missing_required_inputs

    data = _as_input(input_data)
    absent = missing_required_inputs(spec or {}, data)
    if absent:
        raise ScheduleError(
            f"this workflow needs {len(absent)} input(s) it was not given: "
            f"{', '.join(absent)}. A schedule stores those values and runs them "
            "unattended, so they have to be saved with the schedule.")
    return data


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


# ── Readiness ──────────────────────────────────────────────────────────────────

def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, list) else []


def _start_anyway(value: Any) -> list[str]:
    """The readiness keys a schedule may start without. Only a list of keys is a choice."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        raise ScheduleError("start_anyway must be a list of readiness keys, such as "
                            "['deployment_target'].")
    return sorted({k.strip() for k in value if k.strip()})


def _readiness(connection: Connection, project_id: str, workflow_key: str,
               input_data: dict[str, Any], start_anyway: list[str]) -> dict[str, Any]:
    """What this schedule's workflow needs from the project, asked when the schedule is saved.

    ANSWERED, NOT ENFORCED, AT SAVE. A schedule is not a start: saving one for a newsletter
    before the mail server is set up is a reasonable order to do things in, and the example
    setup scripts do exactly that. What the operator must not find out on Monday is that it
    will be refused — so the checklist comes back with the saved schedule, with what they
    accepted marked, and the console draws it. The firing enforces (`run_due`).
    """
    from .workflow_readiness import readiness_of_version

    version_id = connection.execute(
        text("SELECT active_version_id FROM workflow_definitions "
             "WHERE project_id = :p AND workflow_key = :k"),
        {"p": project_id, "k": workflow_key},
    ).scalar()
    if not version_id:
        return {"ready": True, "checked": [], "missing": [], "applies": False}
    readiness = readiness_of_version(connection, None, project_id, version_id=str(version_id),
                                     input_data=input_data)
    readiness["started_anyway"] = [m["key"] for m in readiness.get("missing") or []
                                   if m["key"] in start_anyway]
    readiness["will_refuse"] = [m["key"] for m in readiness.get("missing") or []
                                if m["key"] not in start_anyway]
    return readiness


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
        # What was missing when it last refused, each with its fix — the console draws these
        # rather than asking the operator to read a sentence.
        "last_missing": _json_list(row.get("last_missing")),
        "start_anyway": [str(k) for k in _json_list(row.get("start_anyway"))],
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
    start_anyway: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Schedule a workflow. The first occurrence is computed now, not at the first tick.

    The result carries `readiness`: what the workflow needs from this project, and which of
    the missing things this schedule was told it may start without."""
    accepted = _start_anyway(start_anyway)
    name = str(name or "").strip()
    if not name:
        raise ScheduleError("a schedule needs a name.")
    workflow_key = str(workflow_key or "").strip()
    if not workflow_key:
        raise ScheduleError("say which workflow this runs.")

    # THE WORKFLOW HAS TO EXIST AND BE RUNNABLE. A schedule pointing at nothing fires forever
    # into an error nobody reads, and the check costs one query now against every tick later.
    # Required inputs are part of that check: storing a schedule that cannot start is the
    # same failure, just deferred until the worker ticks.
    spec = _active_spec(connection, project_id, workflow_key)
    input_data = require_schedule_inputs(spec, input_data)

    cadence, minute, hour, weekday, tz_name = validate(
        cadence=cadence, minute=minute, hour=hour, weekday=weekday, tz_name=tz_name)
    due = next_occurrence(cadence=cadence, minute=minute, hour=hour,
                          weekday=weekday, tz_name=tz_name)

    row = connection.execute(
        text(
            """
            INSERT INTO workflow_schedules (
                id, project_id, workflow_key, name, cadence, minute, hour, weekday,
                timezone, input, actor, enabled, next_run_at, start_anyway
            ) VALUES (
                :id, :p, :k, :name, :cadence, :minute, :hour, :weekday,
                :tz, CAST(:input AS JSONB), :actor, :enabled, :due,
                CAST(:anyway AS JSONB)
            ) RETURNING *
            """
        ),
        {"id": make_id("wsc"), "p": project_id, "k": workflow_key, "name": name,
         "cadence": cadence, "minute": minute, "hour": hour, "weekday": weekday,
         "tz": tz_name, "input": json.dumps(input_data or {}), "actor": actor,
         "enabled": bool(enabled), "due": due, "anyway": json.dumps(accepted)},
    ).mappings().one()
    emit_event(connection, project_id, "workflow.schedule_created", actor,
               "workflow_schedule", row["id"],
               {"workflow_key": workflow_key, "describes": describe(
                   cadence, minute, hour, weekday, tz_name),
                **({"start_anyway": accepted} if accepted else {})})
    return {**_row(row),
            "readiness": _readiness(connection, project_id, workflow_key, input_data or {},
                                    accepted)}


def replace_inputs(
    connection: Connection,
    *,
    project_id: str,
    schedule_id: str,
    actor: str,
    input_data: Optional[dict[str, Any]],
    tz_name: Optional[str] = None,
    start_anyway: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Replace the values a schedule will run with. Timing stays unless the zone changes.

    `start_anyway` replaces the stored choice when given; omitted, the stored one stays (so
    an edit of a recipient does not silently withdraw an acceptance). The result carries the
    readiness for the new values, as `create` does.

    The whole input object is replaced, not merged, so a cleared optional field stays cleared.
    Required fields are checked against the version that is published now: the next run uses
    that version, and editing against an older declaration would save a schedule the next
    Monday cannot start. The audit event records which keys changed, never the values.
    """
    row = connection.execute(
        text("SELECT * FROM workflow_schedules WHERE id=:i AND project_id=:p"),
        {"i": schedule_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise ScheduleError(f"no schedule '{schedule_id}'.")
    spec = _active_spec(connection, project_id, row["workflow_key"])
    data = require_schedule_inputs(spec, input_data)
    zone = row["timezone"]
    due = row["next_run_at"]
    if tz_name is not None:
        _, _, _, _, zone = validate(
            cadence=row["cadence"], minute=row["minute"], hour=row["hour"],
            weekday=row["weekday"], tz_name=tz_name)
        # A zone change moves the instant. Recompute only then, and only while the schedule
        # is running — the same rule as resume, so editing inputs does not make a paused
        # schedule due, and does not skip the occurrence the operator already had.
        if zone != row["timezone"] and row["enabled"]:
            due = next_occurrence(
                cadence=row["cadence"], minute=row["minute"], hour=row["hour"],
                weekday=row["weekday"], tz_name=zone)
    accepted = (_start_anyway(start_anyway) if start_anyway is not None
                else [str(k) for k in _json_list(row.get("start_anyway"))])
    updated = connection.execute(
        text(
            """
            UPDATE workflow_schedules
            SET input=CAST(:input AS JSONB), timezone=:tz, next_run_at=:due,
                start_anyway=CAST(:anyway AS JSONB), updated_at=now()
            WHERE id=:i AND project_id=:p
            RETURNING *
            """
        ),
        {"input": json.dumps(data), "tz": zone, "due": due, "anyway": json.dumps(accepted),
         "i": schedule_id, "p": project_id},
    ).mappings().one()
    emit_event(
        connection, project_id, "workflow.schedule_inputs_updated", actor,
        "workflow_schedule", schedule_id,
        {"workflow_key": row["workflow_key"],
         "input_keys": sorted(str(key) for key in data),
         "timezone": zone, "start_anyway": accepted})
    return {**_row(updated),
            "readiness": _readiness(connection, project_id, row["workflow_key"], data,
                                    accepted)}


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
                last_missing=NULL, updated_at=now()
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

def _refused(connection: Connection, project_id: str, row: Any, following: datetime,
             readiness: dict[str, Any]) -> None:
    """A firing refused because the project lacks something: say so, keep what, move on.

    NOT A FAILURE TOWARD THE FUSE. The fuse exists because a schedule that fails forever
    spends money forever; a refusal spends nothing — nothing ran. Counting it disabled a
    deploy-ending schedule after five quiet weeks on a project with no host (review of the
    readiness change), so the day the host was added the schedule was off and nobody knew
    why. It stays on, refuses visibly each time, and runs the first time it can.

    Kept as STRUCTURE, not a truncated sentence: `last_missing` is each item with its fix,
    `last_error` one short line, and a `workflow.start_refused` event names the schedule.
    """
    from .workflow_readiness import record_refused

    unaccepted = {**readiness, "missing": [
        m for m in readiness.get("missing") or []
        if m["key"] not in (readiness.get("started_anyway") or [])]}
    record = record_refused(
        connection, project_id, row["actor"], door="schedule", door_id=row["id"],
        door_name=f"The schedule '{row['name']}'", workflow_key=row["workflow_key"],
        readiness=unaccepted)
    connection.execute(
        text(
            """
            UPDATE workflow_schedules
            SET next_run_at=:due, last_error=:err, last_missing=CAST(:missing AS JSONB),
                updated_at=now()
            WHERE id=:i
            """
        ),
        {"due": following, "err": record["message"], "missing": json.dumps(record["missing"]),
         "i": row["id"]},
    )


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
            # CHECKED AT EVERY FIRING, NOT ONCE. A schedule runs unattended, so a newsletter on
            # an install with no mail server would research every Monday and fail at the send
            # every Monday. What the operator accepted when saving the schedule
            # (`start_anyway`) is honoured; anything else missing refuses — see the handler
            # below for what a refusal records.
            from .workflow_readiness import WorkflowNotReadyError, refuse_unless_ready

            try:
                readiness = refuse_unless_ready(
                    connection, None, project_id, version_id=str(active),
                    input_data=dict(row["input"] if isinstance(row["input"], dict) else {}),
                    start_anyway=[str(k) for k in _json_list(row.get("start_anyway"))])
            except WorkflowNotReadyError as refused:
                _refused(connection, project_id, row, following, refused.readiness)
                started.append({"schedule_id": row["id"], "name": row["name"],
                                "error": "not ready", "disabled": False,
                                "missing": [m["key"] for m in refused.readiness["missing"]
                                            if m["key"] not in refused.readiness.get(
                                                "started_anyway", [])]})
                continue
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
            if not run.get("duplicate"):
                from .workflow_readiness import record_started_anyway

                record_started_anyway(connection, project_id, row["actor"],
                                      str(run.get("id") or ""), readiness)
            connection.execute(
                text(
                    """
                    UPDATE workflow_schedules
                    SET next_run_at=:due, last_run_at=:fired, last_run_id=:run,
                        last_error=NULL, last_missing=NULL, consecutive_failures=0,
                        updated_at=now()
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
