"""Govern › Limits in plain words (BOSSAPP-807): the caps, what happens at them, and a monthly
limit per project.

Three pieces, each read by the path that already enforces spend:

* ``at_limit`` — the operator's choice for a reached cap, one project setting
  (``budget_at_limit``): ``pause`` (new work waits and asks a person: the behaviour every install
  had before this existed, and the default), ``cheaper`` (work goes on past the cap; the budget
  is red, so auto-lite routes build steps to low-cost models — pipeline_profiles
  .apply_budget_pressure) or ``stop`` (nothing runs, not even $0 work, until the period resets;
  nobody is asked). BudgetGovernor.reserve and build_budget.cap_reached read it.
* ``set_caps`` — the daily cap and burst written directly (they were only reachable through
  Pace), beside the monthly cap's existing write (setup_actions.set_budget).
* A project's MONTHLY LIMIT is a project-scoped budget envelope (``budget_allocations``,
  scope_type ``project``, scope_id = the project, one calendar month). The latest one carries
  into later months until it is changed or removed. BudgetGovernor.reserve refuses work that
  would take the project's month spend past it, the same way a cap refuses.

Nothing here drops or renames data: a limit removed is an envelope closed (status), not deleted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

AT_LIMIT_KNOB = "budget_at_limit"
AT_LIMIT_CHOICES = ("pause", "cheaper", "stop")
DEFAULT_AT_LIMIT = "pause"

#: The envelope shape that is a project's monthly limit: a root envelope on the project itself
#: spanning exactly one calendar month (UTC). Envelopes made any other way keep their own rules.
_MONTH_ENVELOPE = (
    "project_id = :p AND scope_type = 'project' AND scope_id = :p "
    "AND parent_allocation_id IS NULL AND status = 'active' "
    "AND date_trunc('month', period_start AT TIME ZONE 'UTC') = (period_start AT TIME ZONE 'UTC') "
    "AND (period_end AT TIME ZONE 'UTC') = (period_start AT TIME ZONE 'UTC') + interval '1 month'"
)

MAX_LIMIT_USD_MICROS = 1_000_000 * 1_000_000


def at_limit(connection: Any, settings: Any, project_id: str) -> str:
    """The choice for a reached cap: this project's setting, else the install's default."""
    value: Any = None
    try:
        with connection.begin_nested():
            value = connection.execute(
                text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
                {"p": project_id, "k": AT_LIMIT_KNOB}).scalar()
    except Exception:  # noqa: BLE001 — a missing table reads as the default, never an outage
        value = None
    if value in (None, ""):
        value = getattr(settings, AT_LIMIT_KNOB, DEFAULT_AT_LIMIT)
    word = str(value or "").strip().strip('"').lower()
    return word if word in AT_LIMIT_CHOICES else DEFAULT_AT_LIMIT


def at_limit_view(connection: Any, settings: Any, project_id: str) -> dict[str, Any]:
    from . import project_settings as ps

    version, changed_by = ps.version_of(connection, project_id, AT_LIMIT_KNOB)
    own = ps.get_own(connection, project_id)
    return {"value": at_limit(connection, settings, project_id),
            "choices": list(AT_LIMIT_CHOICES),
            "default": DEFAULT_AT_LIMIT,
            "source": "set" if own.get(AT_LIMIT_KNOB) not in (None, "") else "default",
            "version": version, "changed_by": changed_by}


def set_caps(connection: Any, project_id: str, *, daily_cap_usd_micros: Optional[int] = None,
             burst_cap_usd_micros_per_min: Optional[int] = None,
             actor: str = "executive_console") -> dict[str, int]:
    """Write the daily cap and/or the burst cap of the project's budget policy (version + 1),
    release jobs the budget was holding, and record `budget.caps_set` for the Log."""
    from .budget import release_budget_holds
    from .core import emit_event

    changes: dict[str, int] = {}
    if daily_cap_usd_micros is not None:
        if not (10_000 <= int(daily_cap_usd_micros) <= MAX_LIMIT_USD_MICROS):
            raise ValueError("Per day: between $0.01 and $1,000,000.")
        changes["daily_cap_usd_micros"] = int(daily_cap_usd_micros)
    if burst_cap_usd_micros_per_min is not None:
        if not (0 <= int(burst_cap_usd_micros_per_min) <= MAX_LIMIT_USD_MICROS):
            raise ValueError("Burst: between $0 and $1,000,000 a minute.")
        changes["burst_cap_usd_micros_per_min"] = int(burst_cap_usd_micros_per_min)
    if not changes:
        return {}
    before = connection.execute(text(
        "SELECT daily_cap_usd_micros, burst_cap_usd_micros_per_min FROM budget_policies "
        "WHERE scope_type = 'project' AND scope_id = :p ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    if before is None:
        raise ValueError("This project has no budget policy yet. Set a monthly cap first.")
    assignments = ", ".join(f"{column} = :{column}" for column in changes)
    connection.execute(text(
        f"UPDATE budget_policies SET {assignments}, version = version + 1 "
        "WHERE scope_type = 'project' AND scope_id = :p"), {"p": project_id, **changes})
    release_budget_holds(connection, project_id)
    emit_event(connection, project_id, "budget.caps_set", actor, "budget_policy", project_id,
               {"via": "govern", **changes,
                "before": {k: int(before[k] or 0) for k in changes}})
    return changes


def project_limit(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The project's monthly limit (the latest month envelope), or None for no limit."""
    row = connection.execute(text(
        f"SELECT id, amount_usd_micros, period_start, period_end, created_at "
        f"FROM budget_allocations WHERE {_MONTH_ENVELOPE} "
        "ORDER BY period_start DESC, created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    if not row:
        return None
    return {"envelope_id": row["id"], "limit_usd_micros": int(row["amount_usd_micros"]),
            "period_start": row["period_start"].isoformat(),
            "period_end": row["period_end"].isoformat()}


def month_spent(connection: Any, project_id: str, now: Optional[datetime] = None) -> dict[str, int]:
    """This month's money (what the caps count) and the active holds, for one project."""
    from .budget import period_spend

    spend = period_spend(connection, project_id, "month", now)
    return {"actual_usd_micros": int(spend["actual_usd_micros"]),
            "reserved_usd_micros": int(spend["reserved_usd_micros"])}


def set_project_limit(connection: Any, project_id: str, limit_usd_micros: Optional[int], *,
                      actor: str = "executive_console",
                      now: Optional[datetime] = None) -> dict[str, Any]:
    """Create, change or remove a project's monthly limit. None removes it (the envelope is
    closed, never deleted). Returns the limit as `project_limits` reads it."""
    from .budget import budget_period, release_budget_holds
    from .core import emit_event, make_id

    if limit_usd_micros is not None and not (10_000 <= int(limit_usd_micros) <= MAX_LIMIT_USD_MICROS):
        raise ValueError("Monthly limit: between $0.01 and $1,000,000, or no limit.")
    before = project_limit(connection, project_id)
    month = budget_period("month", now)
    if limit_usd_micros is None:
        connection.execute(text(
            f"UPDATE budget_allocations SET status = 'closed' WHERE {_MONTH_ENVELOPE}"),
            {"p": project_id})
    elif before and datetime.fromisoformat(before["period_start"]) == month["start"]:
        connection.execute(text(
            "UPDATE budget_allocations SET amount_usd_micros = :a WHERE id = :i"),
            {"a": int(limit_usd_micros), "i": before["envelope_id"]})
    else:
        # A new month: last month's envelope is closed and this month's carries the limit.
        connection.execute(text(
            f"UPDATE budget_allocations SET status = 'closed' WHERE {_MONTH_ENVELOPE}"),
            {"p": project_id})
        envelope_id = make_id("bua")
        connection.execute(text(
            "INSERT INTO budget_allocations (id, parent_allocation_id, project_id, scope_type, "
            "scope_id, amount_usd_micros, period_start, period_end, status) VALUES "
            "(:i, NULL, :p, 'project', :p, :a, :s, :e, 'active')"),
            {"i": envelope_id, "p": project_id, "a": int(limit_usd_micros),
             "s": month["start"], "e": month["end"]})
        emit_event(connection, project_id, "budget.allocation_created", actor,
                   "budget_allocation", envelope_id,
                   {"parent_allocation_id": None, "scope_type": "project", "scope_id": project_id,
                    "amount_usd_micros": int(limit_usd_micros),
                    "period_start": month["start"].isoformat(),
                    "period_end": month["end"].isoformat(), "via": "govern"})
    release_budget_holds(connection, project_id)
    emit_event(connection, project_id, "budget.project_limit_set", actor, "project", project_id,
               {"from": (before or {}).get("limit_usd_micros"), "to": limit_usd_micros})
    after = project_limit(connection, project_id)
    return {"project_id": project_id,
            "limit_usd_micros": (after or {}).get("limit_usd_micros"),
            "envelope_id": (after or {}).get("envelope_id"),
            "used_usd_micros": month_spent(connection, project_id, now)["actual_usd_micros"]}


def limit_period(connection: Any, project_id: str, snapshot: dict[str, Any],
                 now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """The project's monthly limit as a period BudgetGovernor.cap_refusal can check: this
    month's money and holds against the limit, in the cap's own words. None with no limit."""
    from .budget import budget_period

    limit = project_limit(connection, project_id)
    if not limit:
        return None
    cap = int(limit["limit_usd_micros"])
    month = dict(((snapshot or {}).get("periods") or {}).get("month") or {})
    if not month:
        bounds = budget_period("month", now)
        spent = month_spent(connection, project_id, now)
        month = {"period": "month", "resets_at": bounds["end"].isoformat(),
                 "resets_at_label": bounds["resets_at_label"], **spent}
    committed = int(month.get("actual_usd_micros") or 0) + int(month.get("reserved_usd_micros") or 0)
    return {**month, "period": "month", "limit": "project", "cap_usd_micros": cap,
            "committed_usd_micros": committed,
            "available_usd_micros": max(cap - committed, 0),
            "utilization_ratio": round(committed / cap, 6) if cap else 0.0,
            "whose": "this project's monthly", "cap_name": "project's monthly limit",
            "envelope_id": limit["envelope_id"]}


def reached_period(snapshot: dict[str, Any], extra: Optional[dict[str, Any]] = None
                   ) -> Optional[dict[str, Any]]:
    """The first capped period whose MONEY already reached its cap (for `stop`, which refuses
    even $0 work there), or None."""
    periods = list(((snapshot or {}).get("periods") or {}).values())
    if extra:
        periods.append(extra)
    for period in periods:
        cap = int(period.get("cap_usd_micros") or 0)
        if cap and int(period.get("actual_usd_micros") or 0) >= cap:
            return {**period, "kind": "cap"}
    return None


def project_limits(connection: Any, projects: list[dict[str, Any]],
                   now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """One row per project: its monthly limit (None = no limit) and this month's money."""
    out = []
    for project in projects:
        pid = str(project["id"])
        limit = project_limit(connection, pid)
        out.append({"project_id": pid, "name": project.get("name") or pid,
                    "limit_usd_micros": (limit or {}).get("limit_usd_micros"),
                    "envelope_id": (limit or {}).get("envelope_id"),
                    "used_usd_micros": month_spent(connection, pid, now)["actual_usd_micros"]})
    return out
