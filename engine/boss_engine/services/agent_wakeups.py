"""Work that starts without anybody typing.

BOSS could only ever act when someone asked. "Keep an eye on this PR and tell me when it is
review-ready" had no answer but the operator remembering to come back, and "every weekday at 9,
summarise the open work" had none at all — `scheduling.py` arbitrates between jobs, it does not
start agent work on a clock.

TWO SHAPES, ONE MECHANISM
-------------------------
* A **monitor** wakes inside an EXISTING conversation, so it keeps that context and history and
  needs no re-briefing each round. It ends on an exit condition the agent evaluates.
* A **schedule** opens a fresh conversation per run, so a daily briefing does not grow one
  thread without limit.

Splitting these into two subsystems would mean two sweepers, two sets of caps and two places
for a runaway loop to hide.

EVERY WAKE SPENDS MODEL BUDGET
------------------------------
So nothing here self-starts: a row exists only because an operator asked for it. Every row
carries a cycle cap, may carry a wall-clock deadline, and stops itself after repeated failures
rather than burning budget against a broken world. The caps are runaway backstops, not targets
— a loop that reaches its cap ran out of rope, it did not succeed.

DEADLINE-PRESERVING INTERVALS
-----------------------------
The next wake is computed from the one that was DUE, not from when the turn happened to
finish. A loop in a conversation the operator is actively using therefore stays on schedule
instead of drifting later every round.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

#: Below this a "monitor" is a busy-loop that spends budget faster than anything changes.
MIN_INTERVAL_SECONDS = 60

#: Kiro Crew's default, and a sensible one: enough rope for an afternoon of five-minute checks.
DEFAULT_MAX_CYCLES = 24

#: A wake that fails this many times running is watching something broken, and the useful act
#: is to stop and say so rather than to keep paying for the same failure.
MAX_CONSECUTIVE_FAILURES = 3

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WakeupError(ValueError):
    """Something the caller can fix, phrased for them."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as error:  # noqa: BLE001
        raise WakeupError(f"unknown timezone {name!r}") from error


def next_scheduled_wake(
    at_time: str, days: list[str], tz_name: str, *, after: Optional[datetime] = None,
) -> datetime:
    """The next moment matching `at_time` on one of `days`, in the operator's own timezone.

    Timezone-aware on purpose: "every weekday at 9" means nine in the morning where the person
    is, and a schedule that drifts by an hour twice a year is one nobody trusts.
    """
    try:
        hour, minute = (int(x) for x in at_time.split(":", 1))
    except Exception as error:  # noqa: BLE001
        raise WakeupError("at_time must look like '09:00'") from error
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise WakeupError("at_time must be a real time of day, e.g. '09:00'")

    wanted = {d.strip().lower()[:3] for d in (days or []) if str(d).strip()}
    for d in wanted:
        if d not in DAYS:
            raise WakeupError(f"{d!r} is not a day; use {', '.join(DAYS)}")

    zone = _tz(tz_name)
    cursor = (after or _now()).astimezone(zone)
    for _ in range(14):
        candidate = cursor.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > cursor and (not wanted or DAYS[candidate.weekday()] in wanted):
            return candidate.astimezone(timezone.utc)
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise WakeupError("no matching day within two weeks — check `days`")


def arm_monitor(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    thread_id: str,
    prompt: str,
    exit_condition: str,
    interval_seconds: int = 300,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    deadline_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Keep one conversation checking something until a condition is met."""
    if not thread_id:
        raise WakeupError("a monitor runs inside a conversation, so it needs a thread")
    if not (prompt or "").strip():
        raise WakeupError("say what to check on each wake")
    if not (exit_condition or "").strip():
        # WITHOUT ONE THIS IS NOT A MONITOR. A loop with no way to finish is a scheduled job
        # wearing a monitor's name, and it will run until its cap and then look like a failure.
        raise WakeupError(
            "a monitor needs an exit condition — the thing that means it can stop "
            "(\"when the PR is approved\", \"when the deploy reports healthy\")")
    interval = max(MIN_INTERVAL_SECONDS, int(interval_seconds or 0))

    existing = connection.execute(
        text("SELECT id FROM agent_wakeups "
             " WHERE project_id=:p AND thread_id=:t AND kind='monitor' AND status='active'"),
        {"p": project_id, "t": thread_id},
    ).scalar()
    if existing:
        # ONE LOOP PER CONVERSATION. Two would interleave wakes in the same thread and double
        # the spend, and nobody asking for "keep watching this" means "twice".
        raise WakeupError(
            f"this conversation is already being monitored ({existing}); stop that first")

    wake_id = make_id("wake")
    connection.execute(
        text(
            """
            INSERT INTO agent_wakeups (
                id, project_id, kind, thread_id, actor, prompt, exit_condition,
                interval_seconds, next_wake_at, max_cycles, deadline_at, created_by)
            VALUES (:id, :p, 'monitor', :t, :a, :pr, :ec, :iv,
                    now() + (:iv || ' seconds')::interval, :mc,
                    CASE WHEN :dl > 0 THEN now() + (:dl || ' seconds')::interval END, :a)
            """
        ),
        {"id": wake_id, "p": project_id, "t": thread_id, "a": actor, "pr": prompt.strip(),
         "ec": exit_condition.strip(), "iv": interval, "mc": max(0, int(max_cycles)),
         "dl": int(deadline_seconds or 0)},
    )
    emit_event(connection, project_id, "wakeup.armed", actor, "agent_wakeup", wake_id,
               {"kind": "monitor", "interval_seconds": interval, "max_cycles": max_cycles})
    return describe(connection, wakeup_id=wake_id)


def schedule(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    prompt: str,
    at_time: str,
    days: Optional[list[str]] = None,
    tz_name: str = "UTC",
    max_cycles: int = 0,
) -> dict[str, Any]:
    """Run something on a clock, in a fresh conversation each time."""
    if not (prompt or "").strip():
        raise WakeupError("say what to do on each run")
    first = next_scheduled_wake(at_time, days or [], tz_name)
    wake_id = make_id("wake")
    connection.execute(
        text(
            """
            INSERT INTO agent_wakeups (
                id, project_id, kind, actor, prompt, at_time, days, timezone,
                next_wake_at, max_cycles, created_by)
            VALUES (:id, :p, 'schedule', :a, :pr, :at, CAST(:d AS JSONB), :tz, :nw, :mc, :a)
            """
        ),
        {"id": wake_id, "p": project_id, "a": actor, "pr": prompt.strip(), "at": at_time,
         "d": json.dumps([str(x).strip().lower()[:3] for x in (days or [])]),
         "tz": tz_name, "nw": first, "mc": max(0, int(max_cycles))},
    )
    emit_event(connection, project_id, "wakeup.scheduled", actor, "agent_wakeup", wake_id,
               {"at_time": at_time, "days": days or [], "timezone": tz_name})
    return describe(connection, wakeup_id=wake_id)


def describe(connection: Connection, *, wakeup_id: str) -> dict[str, Any]:
    row = connection.execute(
        text("SELECT id, project_id, kind, thread_id, actor, prompt, exit_condition, "
             "       interval_seconds, at_time, days, timezone, next_wake_at, cycle, "
             "       max_cycles, deadline_at, status, last_ran_at, last_result "
             "  FROM agent_wakeups WHERE id = :id"),
        {"id": wakeup_id},
    ).mappings().first()
    if row is None:
        raise WakeupError(f"no wakeup {wakeup_id!r}")
    return dict(row)


def list_wakeups(
    connection: Connection, *, project_id: str, active_only: bool = True,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text("SELECT id, kind, thread_id, prompt, exit_condition, interval_seconds, at_time, "
             "       days, timezone, next_wake_at, cycle, max_cycles, status, last_ran_at, "
             "       last_result "
             "  FROM agent_wakeups WHERE project_id = :p "
             "   AND (:active = false OR status = 'active') "
             " ORDER BY next_wake_at"),
        {"p": project_id, "active": active_only},
    ).mappings().all()
    return [dict(r) for r in rows]


def stop(
    connection: Connection, *, project_id: str, wakeup_id: str, actor: str,
    reason: str = "stopped by operator", status: str = "stopped",
) -> dict[str, Any]:
    row = connection.execute(
        text("UPDATE agent_wakeups SET status=:st, last_result=:r, updated_at=now() "
             " WHERE id=:id AND project_id=:p AND status='active' "
             "RETURNING id, kind, status"),
        {"id": wakeup_id, "p": project_id, "st": status, "r": reason},
    ).mappings().first()
    if row is None:
        raise WakeupError(f"no active wakeup {wakeup_id!r} on this project")
    emit_event(connection, project_id, "wakeup.stopped", actor, "agent_wakeup", wakeup_id,
               {"status": status, "reason": reason})
    return dict(row)


def due(connection: Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    """Everything whose time has come. Bounded, so one sweep cannot start fifty turns."""
    rows = connection.execute(
        text("SELECT id, project_id, kind, thread_id, actor, prompt, exit_condition, "
             "       interval_seconds, at_time, days, timezone, cycle, max_cycles, "
             "       deadline_at, next_wake_at "
             "  FROM agent_wakeups "
             " WHERE status='active' AND next_wake_at <= now() "
             " ORDER BY next_wake_at LIMIT :l"),
        {"l": max(1, int(limit))},
    ).mappings().all()
    return [dict(r) for r in rows]


def _exhausted(row: dict[str, Any]) -> Optional[str]:
    """Has this loop run out of rope? Returns the reason, or None."""
    cap = int(row.get("max_cycles") or 0)
    if cap and int(row.get("cycle") or 0) >= cap:
        return (f"reached its {cap}-cycle cap without the exit condition being met — "
                "it ran out of rope rather than finishing")
    deadline = row.get("deadline_at")
    if deadline and deadline <= _now():
        return "its time budget ran out before the exit condition was met"
    return None


def advance(
    connection: Connection,
    *,
    wakeup_id: str,
    ok: bool,
    result: str,
    finished: bool = False,
) -> dict[str, Any]:
    """Record what one wake did, and decide whether there is a next one.

    Separated from actually running the turn so the scheduling rules are testable without a
    model call — which is the only way this gets tested at all.
    """
    row = describe(connection, wakeup_id=wakeup_id)
    cycle = int(row["cycle"]) + 1

    if finished:
        connection.execute(
            text("UPDATE agent_wakeups SET status='completed', cycle=:c, last_ran_at=now(), "
                 "       last_result=:r, consecutive_failures=0, updated_at=now() "
                 " WHERE id=:id"),
            {"c": cycle, "r": result[:2000], "id": wakeup_id})
        return {"status": "completed", "cycle": cycle}

    failures = 0 if ok else int(
        connection.execute(text("SELECT consecutive_failures FROM agent_wakeups WHERE id=:id"),
                           {"id": wakeup_id}).scalar() or 0) + 1
    if failures >= MAX_CONSECUTIVE_FAILURES:
        # WATCHING SOMETHING BROKEN IS NOT MONITORING IT. Stop, and leave the reason where a
        # person will read it, rather than paying for the same failure on a timer.
        connection.execute(
            text("UPDATE agent_wakeups SET status='failed', cycle=:c, last_ran_at=now(), "
                 "       last_result=:r, consecutive_failures=:f, updated_at=now() "
                 " WHERE id=:id"),
            {"c": cycle, "f": failures, "id": wakeup_id,
             "r": f"stopped after {failures} consecutive failures: {result[:500]}"})
        return {"status": "failed", "cycle": cycle, "consecutive_failures": failures}

    spent = _exhausted({**row, "cycle": cycle})
    if spent:
        connection.execute(
            text("UPDATE agent_wakeups SET status='exhausted', cycle=:c, last_ran_at=now(), "
                 "       last_result=:r, updated_at=now() WHERE id=:id"),
            {"c": cycle, "r": spent, "id": wakeup_id})
        return {"status": "exhausted", "cycle": cycle, "reason": spent}

    # DEADLINE-PRESERVING. The next wake is computed from the one that was DUE, not from now:
    # a turn that took four minutes must not push a five-minute loop to nine.
    if row["kind"] == "monitor":
        base = row["next_wake_at"] or _now()
        interval = timedelta(seconds=int(row["interval_seconds"] or MIN_INTERVAL_SECONDS))
        nxt = base + interval
        while nxt <= _now():
            # If the sweeper was down for an hour, catch up to the next future slot rather
            # than firing every missed wake in a burst.
            nxt += interval
    else:
        days = row["days"] or []
        if isinstance(days, str):
            days = json.loads(days)
        nxt = next_scheduled_wake(row["at_time"], days, row["timezone"])

    connection.execute(
        text("UPDATE agent_wakeups SET cycle=:c, next_wake_at=:n, last_ran_at=now(), "
             "       last_result=:r, consecutive_failures=:f, updated_at=now() WHERE id=:id"),
        {"c": cycle, "n": nxt, "r": result[:2000], "f": failures, "id": wakeup_id})
    return {"status": "active", "cycle": cycle, "next_wake_at": nxt}


#: What a wake tells the agent. The exit condition is the part that makes a monitor finite, so
#: it is stated as an instruction with a specific tool to call rather than left implied.
MONITOR_BRIEF = (
    "This is an automatic check, not a message from the operator — cycle {cycle}"
    "{of_cap}. Do the check below, then decide.\n\n"
    "WHAT TO CHECK: {prompt}\n\n"
    "STOP WHEN: {exit_condition}\n\n"
    "If that condition is now met, call `stop_watching` with finished=true and say in one "
    "line what happened. If it is not met, say ONLY what changed since the last cycle — if "
    "nothing changed, say that in one line and nothing more. Do not restate the situation "
    "each cycle; the operator is reading a conversation, not a log."
)

SCHEDULE_BRIEF = (
    "This is a scheduled run, not a message from the operator.\n\n{prompt}\n\n"
    "Answer as if reporting in. Be brief and lead with anything that needs them."
)


def run_due(
    settings: Any, engine: Any, *, limit: int = 5, project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Wake everything that is due, run its turn, and reschedule it.

    THE ONLY PLACE MODEL BUDGET IS SPENT WITHOUT SOMEBODY TYPING, so it is deliberately the
    smallest thing that can work: a low default limit, one turn per wake, each in its own
    transaction, and any failure recorded against that wakeup rather than allowed to abort the
    sweep. A sweep that dies halfway leaves the rest of the loops silently un-run, which is
    indistinguishable from them being stopped.
    """
    from .agent_service import AgentService

    with engine.begin() as connection:
        pending = due(connection, limit=limit)
        if project_id:
            pending = [row for row in pending if row["project_id"] == project_id]

    ran: list[dict[str, Any]] = []
    for row in pending:
        brief = (
            MONITOR_BRIEF.format(
                cycle=int(row["cycle"]) + 1,
                of_cap=(f" of {row['max_cycles']}" if row.get("max_cycles") else ""),
                prompt=row["prompt"], exit_condition=row["exit_condition"] or "")
            if row["kind"] == "monitor"
            else SCHEDULE_BRIEF.format(prompt=row["prompt"])
        )
        try:
            result = AgentService(settings).chat(
                project_id=row["project_id"], actor=row["actor"], message=brief,
                # A MONITOR CONTINUES ITS OWN CONVERSATION; a schedule starts a fresh one, so
                # a daily briefing does not grow one thread forever.
                thread_id=row["thread_id"] if row["kind"] == "monitor" else None,
                surface="wakeup",
            )
            reply = str((result or {}).get("reply") or "")
            # The agent ends a monitor by calling `stop_watching`, which flips the row itself.
            # Re-reading is how this loop learns that happened rather than rescheduling a
            # loop that has already finished.
            with engine.begin() as connection:
                still = describe(connection, wakeup_id=row["id"])
                if still["status"] != "active":
                    ran.append({"id": row["id"], "outcome": still["status"]})
                    continue
                outcome = advance(connection, wakeup_id=row["id"], ok=True, result=reply)
            ran.append({"id": row["id"], "kind": row["kind"], **outcome})
        except Exception as error:  # noqa: BLE001 — one bad loop must not stop the sweep
            with engine.begin() as connection:
                outcome = advance(connection, wakeup_id=row["id"], ok=False,
                                  result=f"{type(error).__name__}: {str(error)[:400]}")
            ran.append({"id": row["id"], "kind": row["kind"], "error": type(error).__name__,
                        **outcome})
    return {"due": len(pending), "ran": ran}
