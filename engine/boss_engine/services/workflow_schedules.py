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
import re
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


def describe(cadence: str, minute: int, hour: int, weekday: Optional[int], tz_name: str,
             cron_expr: Optional[str] = None) -> str:
    """The schedule as a sentence. What the console shows, and what a test can assert on."""
    if cadence == "cron":
        return f"on cron '{cron_expr}' ({tz_name})"
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


# ── Cron, when the three cadences are not enough (BOSSAPP-877) ─────────────────────────────────
# The note at the top said cron was "a column away"; this is that column. Five standard fields
# (minute hour day-of-month month day-of-week; `*`, lists, ranges, `/step`, month and day names,
# 7 = Sunday), read in the schedule's own zone like every other cadence. Day-of-month and
# day-of-week together mean EITHER, as cron always has.

_CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12),
                ("day of week", 0, 7))
_CRON_NAMES = {
    3: {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                         "sep", "oct", "nov", "dec"))},
    4: {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))},
}
#: The furthest a cron search looks ahead before calling the expression unsatisfiable (Feb 30).
_CRON_HORIZON_DAYS = 366 * 5


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    """The five fields as sets of allowed values, or ScheduleError naming the field that is wrong.
    Returns (minutes, hours, days, months, weekdays, dom_restricted, dow_restricted); the last
    two are flags, non-empty when that field was restricted (not `*`)."""
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise ScheduleError("a cron expression has five fields: minute hour day-of-month month "
                            f"day-of-week — got {len(parts)} in {expr!r}.")
    out: list[frozenset[int]] = []
    for index, (part, (name, low, high)) in enumerate(zip(parts, _CRON_FIELDS)):
        values: set[int] = set()
        for piece in part.lower().split(","):
            step = 1
            had_step = "/" in piece
            if had_step:
                piece, _, raw_step = piece.partition("/")
                if not raw_step.isdigit() or int(raw_step) < 1:
                    raise ScheduleError(f"the {name} field's step must be a whole number — got {part!r}.")
                step = int(raw_step)
            if piece in ("*", ""):
                start, end = low, high
            else:
                bounds = piece.split("-")
                if len(bounds) > 2:
                    raise ScheduleError(f"the {name} field is not a value or range — got {part!r}.")
                nums = []
                for b in bounds:
                    b = _CRON_NAMES.get(index, {}).get(b, b)
                    if not str(b).isdigit():
                        raise ScheduleError(f"the {name} field has {b!r}, which is not a number.")
                    nums.append(int(b))
                start, end = nums[0], nums[-1]
                if had_step and len(bounds) == 1:
                    end = high
            if not (low <= start <= high and low <= end <= high) or start > end:
                raise ScheduleError(f"the {name} field must be within {low}-{high} — got {part!r}.")
            values.update(range(start, end + 1, step))
        if index == 4 and 7 in values:
            values.discard(7)
            values.add(0)
        out.append(frozenset(values))
    out.append(frozenset({1}) if parts[2] != "*" else frozenset())
    out.append(frozenset({1}) if parts[4] != "*" else frozenset())
    return tuple(out)


def next_cron(expr: str, tz_name: str, after: Optional[datetime] = None) -> datetime:
    """The next instant (UTC) the cron expression names, STRICTLY after `after`, in its zone."""
    minutes, hours, days, months, weekdays, dom_set, dow_set = parse_cron(expr)
    zone = ZoneInfo(tz_name)
    start = (after or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = start.astimezone(zone).replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
    limit = local + timedelta(days=_CRON_HORIZON_DAYS)

    def day_ok(t: datetime) -> bool:
        dom = t.day in days
        dow = ((t.weekday() + 1) % 7) in weekdays
        if dom_set and dow_set:
            return dom or dow
        return dom and dow

    t = local
    while t < limit:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        if not day_ok(t):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in minutes:
            t += timedelta(minutes=1)
            continue
        instant = t.replace(tzinfo=zone).astimezone(timezone.utc)
        if instant > start:
            return instant
        t += timedelta(minutes=1)
    raise ScheduleError(f"the cron expression {expr!r} never comes round (no date matches it).")


def next_for(row: Any, *, after: Optional[datetime] = None, tz_name: Optional[str] = None) -> datetime:
    """The next occurrence of a stored schedule (any cadence, cron included)."""
    zone = tz_name or row["timezone"]
    if row["cadence"] == "cron":
        return next_cron(str(row.get("cron_expr") or ""), zone, after)
    return next_occurrence(cadence=row["cadence"], minute=row["minute"], hour=row["hour"],
                           weekday=row["weekday"], tz_name=zone, after=after)


CONCURRENCY_POLICIES = ("always_enqueue", "skip_if_active", "coalesce")
CATCH_UP = ("none", "one", "all")
#: How late a firing may run under catch_up 'none' and still count as on time (a slow tick).
ON_TIME_GRACE = timedelta(minutes=5)
#: How many missed occurrences one tick will even look at.
_MAX_MISSED_SCAN = 200
_TEMPLATE = re.compile(r"\{\{\s*(fired_at|date|since|last_run_at|schedule_name)\s*\}\}")


def _routine(concurrency_policy: Any, catch_up: Any, catch_up_max: Any,
             activity_gate: Any) -> tuple[str, str, int, Optional[dict[str, Any]]]:
    policy = str(concurrency_policy or "always_enqueue").strip().lower()
    if policy not in CONCURRENCY_POLICIES:
        raise ScheduleError(f"concurrency_policy must be one of {', '.join(CONCURRENCY_POLICIES)}.")
    catch = str(catch_up or "one").strip().lower()
    if catch not in CATCH_UP:
        raise ScheduleError(f"catch_up must be one of {', '.join(CATCH_UP)}.")
    try:
        cap = int(catch_up_max if catch_up_max is not None else 3)
    except (TypeError, ValueError):
        raise ScheduleError("catch_up_max must be a whole number from 1 to 50.") from None
    if not 1 <= cap <= 50:
        raise ScheduleError("catch_up_max must be from 1 to 50.")
    gate = None
    if activity_gate not in (None, "", False, {}):
        if activity_gate is True:
            activity_gate = {}
        if not isinstance(activity_gate, dict):
            raise ScheduleError("activity_gate is an object: {\"kinds\": [\"work_item.\"], \"min_events\": 1}.")
        kinds = activity_gate.get("kinds") or ["work_item.", "git.", "code_task.", "document."]
        if not isinstance(kinds, list) or not all(isinstance(k, str) and k.strip() for k in kinds):
            raise ScheduleError("activity_gate.kinds is a list of event-type prefixes, such as \"work_item.\".")
        try:
            least = int(activity_gate.get("min_events") or 1)
        except (TypeError, ValueError):
            raise ScheduleError("activity_gate.min_events must be a whole number.") from None
        gate = {"kinds": sorted({k.strip() for k in kinds}), "min_events": max(1, least)}
    return policy, catch, cap, gate


def render_input(value: Any, context: dict[str, str]) -> Any:
    """A schedule's stored input with {{fired_at}} {{date}} {{since}} {{last_run_at}}
    {{schedule_name}} filled in for this firing. Pure; anything else is left as written."""
    if isinstance(value, str):
        return _TEMPLATE.sub(lambda m: context.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: render_input(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_input(v, context) for v in value]
    return value


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
                              row["weekday"], row["timezone"], row.get("cron_expr")),
        # BOSSAPP-877: the routine's own rules.
        "cron_expr": row.get("cron_expr"),
        "concurrency_policy": row.get("concurrency_policy") or "always_enqueue",
        "catch_up": row.get("catch_up") or "one",
        "catch_up_max": int(row.get("catch_up_max") or 3),
        "activity_gate": row.get("activity_gate") if isinstance(row.get("activity_gate"), dict) else None,
        "coalesced_pending": bool(row.get("coalesced_pending")),
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
    cron_expr: Optional[str] = None,
    concurrency_policy: str = "always_enqueue",
    catch_up: str = "one",
    catch_up_max: int = 3,
    activity_gate: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Schedule a workflow. The first occurrence is computed now, not at the first tick.

    A `cron_expr` makes it a cron schedule (cadence 'cron'; minute/hour/weekday are unused).
    `concurrency_policy`, `catch_up`/`catch_up_max` and `activity_gate` are the routine's rules
    (BOSSAPP-877; see run_due).

    The result carries `readiness`: what the workflow needs from this project, and which of
    the missing things this schedule was told it may start without."""
    accepted = _start_anyway(start_anyway)
    name = str(name or "").strip()
    if not name:
        raise ScheduleError("a schedule needs a name.")
    workflow_key = str(workflow_key or "").strip()
    if not workflow_key:
        raise ScheduleError("say which workflow this runs.")
    from . import projects_archive

    projects_archive.refuse_new_work(connection, project_id, "new schedules")

    # THE WORKFLOW HAS TO EXIST AND BE RUNNABLE. A schedule pointing at nothing fires forever
    # into an error nobody reads, and the check costs one query now against every tick later.
    # Required inputs are part of that check: storing a schedule that cannot start is the
    # same failure, just deferred until the worker ticks.
    spec = _active_spec(connection, project_id, workflow_key)
    from boss_engine.identity import consent as _consent

    input_data = require_schedule_inputs(spec, _consent.strip_identity(input_data))
    # A SCHEDULE RUNS UNATTENDED, SO IT RUNS ON A STANDING GRANT (IDENTITY.md §7.3 3). The person
    # creating it must have one that covers the workflow's agents and permissions, else 409
    # consent_required (scope "schedule"). The link is stored server-side; when the grant ends the
    # schedule pauses with "Renew authority" instead of running on stale authority.
    link = _consent.require_for_schedule(connection, project_id=project_id, spec=spec,
                                         input_data=input_data, name=name) or {}

    policy, catch, cap, gate = _routine(concurrency_policy, catch_up, catch_up_max, activity_gate)
    cron_expr = " ".join(str(cron_expr or "").split()) or None
    if cron_expr or str(cadence or "").strip().lower() == "cron":
        if not cron_expr:
            raise ScheduleError("a cron schedule needs its cron expression (five fields).")
        parse_cron(cron_expr)
        _, _, _, _, tz_name = validate(cadence="daily", minute=0, hour=0, weekday=None,
                                       tz_name=tz_name)
        cadence, minute, hour, weekday = "cron", 0, 0, None
        due = next_cron(cron_expr, tz_name)
    else:
        cadence, minute, hour, weekday, tz_name = validate(
            cadence=cadence, minute=minute, hour=hour, weekday=weekday, tz_name=tz_name)
        due = next_occurrence(cadence=cadence, minute=minute, hour=hour,
                              weekday=weekday, tz_name=tz_name)

    row = connection.execute(
        text(
            """
            INSERT INTO workflow_schedules (
                id, project_id, workflow_key, name, cadence, minute, hour, weekday,
                timezone, input, actor, enabled, next_run_at, start_anyway,
                on_behalf_of, grant_id, cron_expr, concurrency_policy, catch_up,
                catch_up_max, activity_gate
            ) VALUES (
                :id, :p, :k, :name, :cadence, :minute, :hour, :weekday,
                :tz, CAST(:input AS JSONB), :actor, :enabled, :due,
                CAST(:anyway AS JSONB), :obo, :grant, :cron, :policy, :catch, :cap,
                CAST(:gate AS JSONB)
            ) RETURNING *
            """
        ),
        {"id": make_id("wsc"), "p": project_id, "k": workflow_key, "name": name,
         "cadence": cadence, "minute": minute, "hour": hour, "weekday": weekday,
         "tz": tz_name, "input": json.dumps(input_data or {}), "actor": actor,
         "enabled": bool(enabled), "due": due, "anyway": json.dumps(accepted),
         "obo": link.get("on_behalf_of"), "grant": link.get("grant_id"), "cron": cron_expr,
         "policy": policy, "catch": catch, "cap": cap,
         "gate": json.dumps(gate) if gate else None},
    ).mappings().one()
    emit_event(connection, project_id, "workflow.schedule_created", actor,
               "workflow_schedule", row["id"],
               {"workflow_key": workflow_key, "describes": describe(
                   cadence, minute, hour, weekday, tz_name, cron_expr),
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
    from boss_engine.identity import consent as _consent

    data = require_schedule_inputs(spec, _consent.strip_identity(input_data))
    # The grant link lives in the row's on_behalf_of / grant_id columns, which an edit never
    # touches. A schedule saved before those columns keeps its old link in `input._identity`.
    kept_link = None if row.get("on_behalf_of") else connection.execute(
        text("SELECT input->'_identity' FROM workflow_schedules WHERE id = :i"), {"i": schedule_id}).scalar()
    if kept_link:
        data = {**data, _consent.IDENTITY_KEY: kept_link}       # the server's link survives an edit
    zone = row["timezone"]
    due = row["next_run_at"]
    if tz_name is not None:
        _, _, _, _, zone = validate(
            cadence="daily" if row["cadence"] == "cron" else row["cadence"], minute=row["minute"],
            hour=row["hour"],
            weekday=row["weekday"], tz_name=tz_name)
        # A zone change moves the instant. Recompute only then, and only while the schedule
        # is running — the same rule as resume, so editing inputs does not make a paused
        # schedule due, and does not skip the occurrence the operator already had.
        if zone != row["timezone"] and row["enabled"]:
            due = next_for(row, tz_name=zone)
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
    due = next_for(row) if enabled else row["next_run_at"]
    updated = connection.execute(
        text(
            """
            UPDATE workflow_schedules
            SET enabled=:e, next_run_at=:due, consecutive_failures=0, last_error=NULL,
                last_missing=NULL, coalesced_pending=FALSE, updated_at=now()
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


_KEEP: Any = object()


def set_routine(
    connection: Connection,
    *,
    project_id: str,
    schedule_id: str,
    actor: str,
    cron_expr: Any = _KEEP,
    concurrency_policy: Any = _KEEP,
    catch_up: Any = _KEEP,
    catch_up_max: Any = _KEEP,
    activity_gate: Any = _KEEP,
) -> dict[str, Any]:
    """Change a schedule's routine rules (BOSSAPP-877). An argument left out keeps its value; a
    new cron expression moves the next occurrence (while the schedule is running)."""
    row = connection.execute(
        text("SELECT * FROM workflow_schedules WHERE id=:i AND project_id=:p FOR UPDATE"),
        {"i": schedule_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise ScheduleError(f"no schedule '{schedule_id}'.")
    def pick(new: Any, old: Any) -> Any:
        return old if new is _KEEP else new

    policy, catch, cap, gate = _routine(
        pick(concurrency_policy, row.get("concurrency_policy")), pick(catch_up, row.get("catch_up")),
        pick(catch_up_max, row.get("catch_up_max")),
        pick(activity_gate, row.get("activity_gate")))
    cadence, cron, due = row["cadence"], row.get("cron_expr"), row["next_run_at"]
    if cron_expr is not _KEEP:
        cron_new = " ".join(str(cron_expr or "").split()) or None
        if not cron_new:
            raise ScheduleError("a cron expression is five fields; to go back to hourly, daily or "
                                "weekly, make a new schedule.")
        parse_cron(cron_new)
        if cron_new != cron:
            cadence, cron = "cron", cron_new
            if row["enabled"]:
                due = next_cron(cron, row["timezone"])
    updated = connection.execute(
        text(
            """
            UPDATE workflow_schedules
            SET cadence=:cadence, cron_expr=:cron, next_run_at=:due, concurrency_policy=:policy,
                catch_up=:catch, catch_up_max=:cap, activity_gate=CAST(:gate AS JSONB),
                minute = CASE WHEN :cadence = 'cron' THEN 0 ELSE minute END,
                hour = CASE WHEN :cadence = 'cron' THEN 0 ELSE hour END,
                weekday = CASE WHEN :cadence = 'cron' THEN NULL ELSE weekday END,
                updated_at=now()
            WHERE id=:i RETURNING *
            """
        ),
        {"cadence": cadence, "cron": cron, "due": due, "policy": policy, "catch": catch,
         "cap": cap, "gate": json.dumps(gate) if gate else None, "i": schedule_id},
    ).mappings().one()
    emit_event(connection, project_id, "workflow.schedule_routine_updated", actor,
               "workflow_schedule", schedule_id,
               {"cron_expr": cron, "concurrency_policy": policy, "catch_up": catch,
                "catch_up_max": cap, "activity_gate": gate})
    return _row(updated)


def firings(connection: Connection, project_id: str, schedule_id: str,
            limit: int = 50) -> list[dict[str, Any]]:
    """Every firing of a schedule, newest first: started, or why not (BOSSAPP-877)."""
    return [{"id": r["id"], "scheduled_for": r["scheduled_for"].isoformat(),
             "outcome": r["outcome"], "reason": r["reason"], "run_id": r["run_id"],
             "detail": r["detail"] if isinstance(r["detail"], dict) else {},
             "created_at": r["created_at"].isoformat()}
            for r in connection.execute(text(
                "SELECT * FROM workflow_schedule_runs WHERE project_id = :p AND schedule_id = :s "
                "ORDER BY created_at DESC, id LIMIT :n"),
                {"p": project_id, "s": schedule_id, "n": max(1, min(int(limit), 500))}).mappings()]


def _record(connection: Connection, project_id: str, row: Any, scheduled_for: datetime,
            outcome: str, *, reason: Optional[str] = None, run_id: Optional[str] = None,
            detail: Optional[dict[str, Any]] = None) -> None:
    connection.execute(text(
        "INSERT INTO workflow_schedule_runs (id, project_id, schedule_id, scheduled_for, outcome, "
        "reason, run_id, detail) VALUES (:id, :p, :s, :at, :o, :r, :run, CAST(:d AS JSONB))"),
        {"id": make_id("wsr"), "p": project_id, "s": row["id"], "at": scheduled_for, "o": outcome,
         "r": (reason or None) and str(reason)[:500], "run": run_id,
         "d": json.dumps(detail or {}, default=str)})


def plan_firings(row: Any, now: datetime) -> tuple[list[datetime], list[tuple[datetime, str]], datetime]:
    """What this tick does for one due schedule: the occurrences to start, the ones it will not
    start (each with its reason), and the next occurrence after now. Pure.

    catch_up 'one' (the default, and what schedules always did): ONE run for however many
    occurrences were missed. 'all': every missed occurrence, the newest `catch_up_max` of them.
    'none': only an occurrence that is still on time (within ON_TIME_GRACE); missed ones are
    recorded and skipped."""
    following = next_for(row, after=now)
    due_at = row["next_run_at"]
    if due_at is None or due_at > now:
        return [], [], following
    missed = [due_at]
    while len(missed) < _MAX_MISSED_SCAN:
        nxt = next_for(row, after=missed[-1])
        if nxt > now:
            break
        missed.append(nxt)
    catch = row.get("catch_up") or "one"
    if catch == "none":
        latest = missed[-1]
        fires = [latest] if now - latest <= ON_TIME_GRACE else []
        skipped = [(at, "missed") for at in missed if at not in fires]
    elif catch == "all":
        cap = int(row.get("catch_up_max") or 3)
        fires = missed[-cap:]
        skipped = [(at, "catch_up_cap") for at in missed[:-cap]]
    else:
        fires = [missed[0]]
        skipped = [(at, "caught_up_once") for at in missed[1:]]
    return fires, skipped, following


def _active_run(connection: Connection, row: Any) -> Optional[str]:
    """The run this schedule started that is still going, if any."""
    from .workflows import TERMINAL_RUN_STATES

    return connection.execute(text(
        "SELECT r.id FROM workflow_schedule_runs sr JOIN workflow_runs r ON r.id = sr.run_id "
        "WHERE sr.schedule_id = :s AND sr.outcome = 'started' AND r.status <> ALL(:done) "
        "ORDER BY sr.created_at DESC LIMIT 1"),
        {"s": row["id"], "done": sorted(TERMINAL_RUN_STATES)}).scalar()


def _activity(connection: Connection, project_id: str, row: Any, gate: dict[str, Any],
              since: datetime) -> int:
    """How many events of the gate's kinds happened in the project since `since` — not counting
    what this schedule's own runs (and their work items) did."""
    kinds = [f"{k}%" for k in gate.get("kinds") or []]
    return int(connection.execute(text(
        "SELECT count(*) FROM events e WHERE e.project_id = :p AND e.created_at > :since "
        "AND e.event_type LIKE ANY(:kinds) AND NOT EXISTS ("
        "  SELECT 1 FROM workflow_schedule_runs sr JOIN workflow_runs r ON r.id = sr.run_id "
        "  WHERE sr.schedule_id = :s AND (e.entity_id = r.id OR e.entity_id = r.work_item_id))"),
        {"p": project_id, "since": since, "kinds": kinds, "s": row["id"]}).scalar() or 0)


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
    starts one run when it comes back rather than seven (catch_up 'one'; BOSSAPP-877 adds 'all',
    capped, and 'none'). Nobody wants six stale digests.

    THE ROUTINE'S RULES (BOSSAPP-877), in order, each firing it does not start recorded with why
    (workflow_schedule_runs): the catch-up plan (plan_firings); the ACTIVITY GATE — nothing of
    the named kinds happened since the last run → skipped "no_activity"; the CONCURRENCY POLICY
    when its last run is still going — skip_if_active skips ("run_active"), coalesce holds ONE
    firing until that run ends (coalesced_pending; the tick after it ends starts it),
    always_enqueue starts regardless.

    SELECT ... FOR UPDATE SKIP LOCKED because more than one worker may tick at once: two
    workers claiming the same schedule is the same digest sent twice.
    """
    from . import projects_archive

    # AN ARCHIVED PROJECT'S SCHEDULES DO NOT FIRE, and are not advanced either: on restore each
    # one fires once (next_occurrence is computed from now), never once per missed slot.
    if projects_archive.is_archived(connection, project_id):
        return []
    now = now or datetime.now(timezone.utc)
    due_rows = connection.execute(
        text(
            """
            SELECT * FROM workflow_schedules
            WHERE project_id = :p AND enabled AND (next_run_at <= :now OR coalesced_pending)
            ORDER BY next_run_at
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"p": project_id, "now": now, "limit": int(limit)},
    ).mappings().all()

    started: list[dict[str, Any]] = []
    for row in due_rows:
        try:
            fires, skipped, following = plan_firings(row, now)
        except ScheduleError as error:
            # A cron expression that can no longer come round: stop it, saying why.
            connection.execute(text(
                "UPDATE workflow_schedules SET enabled = FALSE, last_error = :e, updated_at = now() "
                "WHERE id = :i"), {"e": str(error)[:500], "i": row["id"]})
            started.append({"schedule_id": row["id"], "name": row["name"], "error": str(error),
                            "disabled": True})
            continue
        pending = bool(row.get("coalesced_pending"))
        if not fires and not skipped and pending:
            # Not due: only the firing held back for a run that was still going.
            fires, following = [now], row["next_run_at"]
        for at, why in skipped:
            _record(connection, project_id, row, at, "suppressed", reason=why)
        if not fires:
            connection.execute(text(
                "UPDATE workflow_schedules SET next_run_at = :due, updated_at = now() WHERE id = :i"),
                {"due": following, "i": row["id"]})
            started.append({"schedule_id": row["id"], "name": row["name"], "skipped": len(skipped),
                            "reason": skipped[-1][1] if skipped else None,
                            "next_run_at": following.isoformat()})
            continue
        held = _hold_back(connection, project_id, row, fires, following, now, pending)
        if held is not None:
            started.append(held)
            continue
        for at in fires:
            started.append(_fire(connection, project_id, row, at, following, now))
    return started


def _hold_back(connection: Connection, project_id: str, row: Any, fires: list[datetime],
               following: datetime, now: datetime, pending: bool) -> Optional[dict[str, Any]]:
    """The activity gate and the concurrency policy. A dict (what the tick reports) when the
    firings are not started; None when they go ahead."""
    def move_on(*, coalesce: bool) -> None:
        connection.execute(text(
            "UPDATE workflow_schedules SET next_run_at = :due, coalesced_pending = :c, "
            "updated_at = now() WHERE id = :i"),
            {"due": following, "c": coalesce, "i": row["id"]})

    gate = row.get("activity_gate") if isinstance(row.get("activity_gate"), dict) else None
    if gate and not pending:
        since = row.get("last_run_at") or row.get("created_at") or now
        seen = _activity(connection, project_id, row, gate, since)
        if seen < int(gate.get("min_events") or 1):
            for at in fires:
                _record(connection, project_id, row, at, "suppressed", reason="no_activity",
                        detail={"since": since, "events": seen, "kinds": gate.get("kinds")})
            move_on(coalesce=False)
            return {"schedule_id": row["id"], "name": row["name"], "skipped": len(fires),
                    "reason": "no_activity", "next_run_at": following.isoformat()}
    policy = row.get("concurrency_policy") or "always_enqueue"
    active = _active_run(connection, row) if policy != "always_enqueue" else None
    if active:
        if policy == "coalesce":
            if not pending:
                for at in fires:
                    _record(connection, project_id, row, at, "coalesced", reason="run_active",
                            detail={"active_run_id": active})
            move_on(coalesce=True)
            return {"schedule_id": row["id"], "name": row["name"], "coalesced": True,
                    "active_run_id": active, "next_run_at": following.isoformat()}
        for at in fires:
            _record(connection, project_id, row, at, "suppressed", reason="run_active",
                    detail={"active_run_id": active})
        move_on(coalesce=False)
        return {"schedule_id": row["id"], "name": row["name"], "skipped": len(fires),
                "reason": "run_active", "active_run_id": active,
                "next_run_at": following.isoformat()}
    if pending:
        connection.execute(text(
            "UPDATE workflow_schedules SET coalesced_pending = FALSE WHERE id = :i"), {"i": row["id"]})
    return None


def _template_context(row: Any, at: datetime) -> dict[str, str]:
    zone = ZoneInfo(row["timezone"])
    last = row.get("last_run_at")
    since = last or row.get("created_at") or at
    return {"fired_at": at.isoformat(), "date": at.astimezone(zone).date().isoformat(),
            "since": since.isoformat(), "last_run_at": last.isoformat() if last else "",
            "schedule_name": str(row["name"])}


def _fire(connection: Connection, project_id: str, row: Any, fired_at: datetime,
          following: datetime, now: datetime) -> dict[str, Any]:
    """Start one occurrence (the schedule's authority and readiness checked first), and record
    what happened as a firing."""
    from .workflows import start_run

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
        # THE SCHEDULE'S AUTHORITY, CHECKED AT EVERY FIRING: its linked standing grant must still
        # be live and its creator's. If not, the schedule PAUSES ("Renew authority").
        from boss_engine.identity import consent as _consent
        from boss_engine.identity import lineage as _lineage

        try:
            schedule_acting = _consent.schedule_acting(connection, row)
        except _consent.AuthorityLost as lost:
            connection.execute(text(
                "UPDATE workflow_schedules SET enabled = FALSE, next_run_at = :due, "
                "last_error = :err, updated_at = now() WHERE id = :i"),
                {"due": following, "i": row["id"],
                 "err": f"Renew authority: the grant this schedule runs under has ended ({lost.reason})."})
            emit_event(connection, project_id, "workflow.schedule_disabled", row["actor"],
                       "workflow_schedule", row["id"], {"name": row["name"], "error": "authority ended"})
            _record(connection, project_id, row, fired_at, "failed", reason="authority_ended")
            return {"schedule_id": row["id"], "name": row["name"], "error": "authority ended",
                    "disabled": True}
        # TEMPLATED INPUTS (BOSSAPP-877): {{fired_at}} {{date}} {{since}} {{last_run_at}}
        # {{schedule_name}} are filled in for THIS firing; nothing else is touched.
        stored = row["input"] if isinstance(row["input"], dict) else {}
        values = render_input(_consent.strip_identity(dict(stored)), _template_context(row, fired_at))
        # CHECKED AT EVERY FIRING, NOT ONCE. A schedule runs unattended, so a newsletter on
        # an install with no mail server would research every Monday and fail at the send
        # every Monday. What the operator accepted when saving the schedule
        # (`start_anyway`) is honoured; anything else missing refuses — see the handler
        # below for what a refusal records.
        from .workflow_readiness import WorkflowNotReadyError, refuse_unless_ready

        try:
            readiness = refuse_unless_ready(
                connection, None, project_id, version_id=str(active),
                input_data={k: v for k, v in values.items() if not str(k).startswith("_identity")},
                start_anyway=[str(k) for k in _json_list(row.get("start_anyway"))])
        except WorkflowNotReadyError as refused:
            _refused(connection, project_id, row, following, refused.readiness)
            missing = [m["key"] for m in refused.readiness["missing"]
                       if m["key"] not in refused.readiness.get("started_anyway", [])]
            _record(connection, project_id, row, fired_at, "refused", reason="not_ready",
                    detail={"missing": missing})
            return {"schedule_id": row["id"], "name": row["name"], "error": "not ready",
                    "disabled": False, "missing": missing}
        with (_lineage.acting(schedule_acting) if schedule_acting is not None else _lineage.nullcontext()):
            run = start_run(
                connection,
                project_id=project_id,
                actor=row["actor"],
                input_data=values,
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
        _record(connection, project_id, row, fired_at, "started", run_id=run.get("id"),
                detail={"duplicate": True} if run.get("duplicate") else None)
        return {"schedule_id": row["id"], "name": row["name"],
                "workflow_key": row["workflow_key"], "run_id": run.get("id"),
                "next_run_at": following.isoformat()}
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
        _record(connection, project_id, row, fired_at, "failed", reason=str(error)[:200])
        return {"schedule_id": row["id"], "name": row["name"],
                "error": str(error)[:200], "disabled": stop}
