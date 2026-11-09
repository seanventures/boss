"""Loop and duplicate guards for self-started work, enforced in code (design §6, §9; BOSSAPP-908).

WHY IN CODE AND NOT IN A PROMPT
-------------------------------
BOSS has had duplicate runs (four duplicate-action incidents, 2026-08..10) and runaway retries,
and the industry's worst ambient failures were loops where every single call was inside its
limit while the whole run was unbounded (the ~$47k multi-agent retry loop, design §2.2 pattern
4). A model asked nicely not to loop will, eventually, loop. So every rule below is a check the
board runs before it opens or starts ambient work, and a refusal is recorded and evented
(`ambient.guard_refused`) so the scorecard can count it — never a silent drop.

THE RULES
---------
1. ONE LIVE ITEM PER FINGERPRINT — the database itself (uq_work_items_live_fingerprint).
2. CAUSAL DEPTH — a signal raised by an ambient run is one level deeper than that run's item;
   past `max_spawn_depth` (default 2) it is suppressed and reported, never worked.
3. COOLDOWN — after an item for a fingerprint is dismissed or fails, the fingerprint waits
   `cooldown_hours`, doubling for each such ending in the last 30 days (24h, 48h, 96h ...).
4. DAILY ATTEMPT CAP — at most `daily_attempt_cap` new ambient items per class per project/day.
5. CONCURRENCY — at most `max_concurrent` ambient items in progress per project.
6. CIRCUIT BREAKER — when at least `breaker_min_runs` ambient items ended in the last hour and
   `breaker_failure_ratio` of them failed, the project's ambient work STOPS (policy.stop, which
   cancels every agent lease) and `ambient.breaker_tripped` is emitted for paging. A person
   resumes it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import policy
from ._common import emit, iso, now

LIVE = ("proposed", "claimed", "in_progress", "needs_you")
COOLDOWN_LOOKBACK_DAYS = 30
BREAKER_WINDOW = timedelta(hours=1)


def _refuse(connection: Connection, project_id: str, guard: str, reason: str,
            **detail: Any) -> dict[str, Any]:
    emit(connection, project_id, "ambient.guard_refused", "boss", "project", project_id,
         {"guard": guard, "reason": reason, **{k: v for k, v in detail.items()
                                               if v is not None}})
    return {"ok": False, "guard": guard, "reason": reason, **detail}


def cooldown_until(connection: Connection, project_id: str, fingerprint: str,
                   cooldown_hours: int) -> Optional[Any]:
    rows = connection.execute(text(
        "SELECT resolved_at FROM work_items WHERE project_id = :p AND fingerprint = :f "
        "AND lifecycle IN ('dismissed', 'failed') AND resolved_at > now() - make_interval("
        "days => :d) ORDER BY resolved_at DESC"),
        {"p": project_id, "f": fingerprint, "d": COOLDOWN_LOOKBACK_DAYS}).scalars().all()
    if not rows or cooldown_hours <= 0:
        return None
    hours = cooldown_hours * (2 ** (len(rows) - 1))
    until = rows[0] + timedelta(hours=hours)
    return until if until > now() else None


def admit_new_item(connection: Connection, *, project_id: str, fingerprint: str,
                   task_class: str, causal_depth: int = 0) -> dict[str, Any]:
    """May a NEW ambient item open for this fingerprint now? {ok} or {ok: False, guard, reason}."""
    stopped = policy.stop_state(connection, project_id)
    if stopped["stopped"]:
        return _refuse(connection, project_id, "stopped",
                       f"ambient work is stopped ({stopped['scope']})")
    limits = policy.settings_for(connection, project_id)
    breaker = check_breaker(connection, project_id)
    if breaker["tripped"]:
        return _refuse(connection, project_id, "breaker", breaker["reason"])
    if int(causal_depth) > int(limits["max_spawn_depth"]):
        return _refuse(connection, project_id, "causal_depth",
                       f"raised at causal depth {causal_depth}, above the limit of "
                       f"{limits['max_spawn_depth']}", fingerprint=fingerprint)
    until = cooldown_until(connection, project_id, fingerprint, int(limits["cooldown_hours"]))
    if until is not None:
        return _refuse(connection, project_id, "cooldown",
                       f"this problem was dismissed or failed recently; it waits until "
                       f"{iso(until)}", fingerprint=fingerprint, until=iso(until))
    opened_today = int(connection.execute(text(
        "SELECT count(*) FROM work_items WHERE project_id = :p AND ambient AND task_class = :c "
        "AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"),
        {"p": project_id, "c": task_class}).scalar() or 0)
    if opened_today >= int(limits["daily_attempt_cap"]):
        return _refuse(connection, project_id, "attempt_cap",
                       f"{opened_today} {task_class} items already opened today (cap "
                       f"{limits['daily_attempt_cap']})", task_class=task_class)
    return {"ok": True}


#: Work that never waits for a concurrency slot behind backlog items: an incident is urgent
#: (HelpDesk AI 2026-11-07: two security patches in progress would have held INC-2's squad).
URGENT_CLASSES = frozenset({"incident_response", "incident_staging_mitigation"})


def admit_start(connection: Connection, *, project_id: str,
                task_class: Optional[str] = None) -> dict[str, Any]:
    """May one more ambient item move to in_progress here now (stop, breaker, concurrency)?
    An incident (URGENT_CLASSES) is held only by the stop and the breaker."""
    stopped = policy.stop_state(connection, project_id)
    if stopped["stopped"]:
        return _refuse(connection, project_id, "stopped",
                       f"ambient work is stopped ({stopped['scope']})")
    breaker = check_breaker(connection, project_id)
    if breaker["tripped"]:
        return _refuse(connection, project_id, "breaker", breaker["reason"])
    if task_class in URGENT_CLASSES:
        return {"ok": True}
    limit = int(policy.settings_for(connection, project_id)["max_concurrent"])
    running = int(connection.execute(text(
        "SELECT count(*) FROM work_items WHERE project_id = :p AND ambient AND "
        "lifecycle = 'in_progress'"), {"p": project_id}).scalar() or 0)
    if running >= limit:
        return _refuse(connection, project_id, "concurrency",
                       f"{running} ambient items already in progress (limit {limit})")
    return {"ok": True}


def check_breaker(connection: Connection, project_id: str, *,
                  actor: str = "boss") -> dict[str, Any]:
    """Trip the project's ambient breaker when too many ambient items failed in the last hour."""
    limits = policy.settings_for(connection, project_id)
    # A person who resumed after a trip starts a fresh window: the failures that tripped it are
    # not counted again, or the breaker would re-trip on the very next sweep.
    since = now() - BREAKER_WINDOW
    resumed = limits.get("resumed_at")
    if resumed:
        resumed_at = datetime.fromisoformat(str(resumed))
        since = max(since, resumed_at)
    row = connection.execute(text(
        "SELECT count(*) FILTER (WHERE lifecycle = 'failed') AS failed, "
        "count(*) FILTER (WHERE lifecycle IN ('failed', 'resolved')) AS ended "
        "FROM work_items WHERE project_id = :p AND ambient AND resolved_at > :since"),
        {"p": project_id, "since": since}).mappings().one()
    failed, ended = int(row["failed"] or 0), int(row["ended"] or 0)
    ratio = (failed / ended) if ended else 0.0
    tripped = ended >= int(limits["breaker_min_runs"]) and ratio >= float(
        limits["breaker_failure_ratio"])
    reason = (f"circuit breaker: {failed} of {ended} ambient items failed in the last hour"
              if tripped else "")
    if tripped and not policy.is_stopped(connection, project_id):
        policy.stop(connection, project_id=project_id, actor=actor, reason=reason)
        emit(connection, project_id, "ambient.breaker_tripped", actor, "project", project_id,
             {"failed": failed, "ended": ended, "ratio": round(ratio, 3),
              "threshold": float(limits["breaker_failure_ratio"])})
    return {"tripped": tripped, "failed": failed, "ended": ended, "ratio": round(ratio, 3),
            "reason": reason}
