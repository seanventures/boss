"""BOSSAPP-1295: a month's spend, its forecast and its runs, for the org, one team or one project.

/v1/budget/forecast forecasts one project's governed cap from its burn rate. A preview team of
10-20 people with one monthly allowance asks a different question: "this month, across what my
team works on, how much have we spent, where will we land, which runs cost what, and is there a
cap?" This module answers it, from the same ledger every scoped budget reads (``llm_calls``,
MONEY only: a subscription or local call is $0, included_spend.LLM_MONEY_SQL).

    scope    org      every project of the org
             team     the projects an identity group works in (scoped_budgets.team_projects_sql)
             project  one project

FORECAST = spent so far + (spent so far / days elapsed, at least one) x days left. A plain
run-rate: a person can redo it on paper. The cap is the scope's active MONTHLY scoped budget,
when one exists (Govern › Limits).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from .included_spend import LLM_MONEY_SQL
from .scoped_budgets import ScopedBudgetError, team_projects, team_projects_sql

SCOPES = ("org", "team", "project")
RUN_LIMIT = 25

_MONEY = (LLM_MONEY_SQL.replace("included_in_subscription", "l.included_in_subscription")
          .replace("cost_usd_micros", "l.cost_usd_micros"))


def scope_projects(connection: Any, org_id: str, scope_type: str, scope_id: str) -> list[str]:
    """The project ids a scope covers, every one of them in `org_id`."""
    scope_type = str(scope_type or "org").strip().lower()
    if scope_type not in SCOPES:
        raise ScopedBudgetError(f"scope is one of {', '.join(SCOPES)}")
    in_org = connection.execute(text(
        "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o ORDER BY p.id"), {"o": org_id}).scalars().all()
    in_org = [str(p) for p in in_org]
    if scope_type == "org":
        return in_org
    if scope_type == "project":
        if scope_id not in in_org:
            raise ScopedBudgetError("No such project in this org.")
        return [scope_id]
    found = connection.execute(text(
        "SELECT 1 FROM identity_groups WHERE id = :g AND org_id = :o"),
        {"g": scope_id, "o": org_id}).first()
    if not found:
        raise ScopedBudgetError("No such team in this org.")
    allowed = set(in_org)
    return [p for p in team_projects(connection, scope_id) if p in allowed]


def scope_label(connection: Any, scope_type: str, scope_id: str) -> str:
    if scope_type == "org":
        return "Whole org"
    if scope_type == "team":
        name = connection.execute(text("SELECT name FROM identity_groups WHERE id = :g"),
                                  {"g": scope_id}).scalar()
        return f"Team {name or scope_id}"
    name = connection.execute(text("SELECT name FROM projects WHERE id = :p"),
                              {"p": scope_id}).scalar()
    return f"Project {name or scope_id}"


def _cap(connection: Any, org_id: str, scope_type: str, scope_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, cap_usd_micros, warn_pct, hard_stop FROM scoped_budget_policies "
        "WHERE org_id = :o AND status = 'active' AND period = 'month' AND scope_type = :t "
        "AND scope_id = :s ORDER BY created_at DESC LIMIT 1"),
        {"o": org_id, "t": scope_type, "s": org_id if scope_type == "org" else scope_id}
    ).mappings().first()
    return dict(row) if row else None


def _project_cap(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The project's own monthly cap (budget_policies, the one /v1/budget reads), if one is set."""
    row = connection.execute(text(
        "SELECT id, monthly_cap_usd_micros AS cap_usd_micros, soft_cap_pct AS warn_pct "
        "FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    if not row or not int(row["cap_usd_micros"] or 0):
        return None
    return {"id": row["id"], "cap_usd_micros": int(row["cap_usd_micros"]),
            "warn_pct": float(row["warn_pct"] or 0.8), "hard_stop": None}


def state_of(spent: int, forecast: int, cap: Optional[int], warn_pct: float) -> str:
    """at_cap | over_forecast | near | on_track | no_cap — the badge's one word."""
    if not cap:
        return "no_cap"
    if spent >= cap:
        return "at_cap"
    if forecast > cap:
        return "over_forecast"
    if spent >= warn_pct * cap:
        return "near"
    return "on_track"


def month_forecast(connection: Any, org_id: str, scope_type: str = "org", scope_id: str = "",
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """This month for one scope: spent, run-rate, forecast, cap, days, projects and runs."""
    from .budget import budget_period
    from .core import now_utc

    now = now or now_utc()
    scope_type = str(scope_type or "org").strip().lower()
    scope_id = org_id if scope_type == "org" else str(scope_id or "").strip()
    projects = scope_projects(connection, org_id, scope_type, scope_id)
    bounds = budget_period("month", now)
    start, end = bounds["start"], bounds["end"]
    days_total = (end - start).total_seconds() / 86400
    elapsed = max(0.0, (now - start).total_seconds() / 86400)
    left = max(0.0, days_total - elapsed)
    params = {"ids": projects, "start": start, "end": end}
    where = "l.project_id = ANY(:ids) AND l.created_at >= :start AND l.created_at < :end"

    spent = 0
    daily: list[dict[str, Any]] = []
    by_project: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    run_count = run_total = 0
    if projects:
        spent = int(connection.execute(text(
            f"SELECT COALESCE(SUM({_MONEY}), 0) FROM llm_calls l WHERE {where}"), params).scalar() or 0)
        daily = [{"day": str(r["day"]), "money_usd_micros": int(r["money"] or 0)}
                 for r in connection.execute(text(
                     f"SELECT to_char(date_trunc('day', l.created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') "
                     f"AS day, SUM({_MONEY}) AS money FROM llm_calls l WHERE {where} "
                     "GROUP BY 1 ORDER BY 1"), params).mappings()]
        by_project = [{"id": r["id"], "name": r["name"] or r["id"], "money_usd_micros": int(r["money"] or 0)}
                      for r in connection.execute(text(
                          f"SELECT l.project_id AS id, MAX(p.name) AS name, SUM({_MONEY}) AS money "
                          f"FROM llm_calls l LEFT JOIN projects p ON p.id = l.project_id WHERE {where} "
                          "GROUP BY l.project_id ORDER BY money DESC"), params).mappings()]
        # A RUN is a build (code_task_run_id) or an agent run (run_id); unattributed calls are
        # not a run. Its cost is the money of its calls in this month.
        run_sql = (f"SELECT COALESCE(l.code_task_run_id, l.run_id) AS run_id, "
                   "CASE WHEN l.code_task_run_id IS NOT NULL THEN 'build' ELSE 'agent' END AS kind, "
                   f"MAX(l.project_id) AS project_id, SUM({_MONEY}) AS money, COUNT(*) AS calls, "
                   "MIN(l.created_at) AS started_at FROM llm_calls l "
                   f"WHERE {where} AND COALESCE(l.code_task_run_id, l.run_id) IS NOT NULL "
                   "GROUP BY 1, 2")
        totals = connection.execute(text(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(x.money), 0) AS money FROM ({run_sql}) x"),
            params).mappings().first()
        run_count, run_total = int(totals["n"] or 0), int(totals["money"] or 0)
        rows = connection.execute(text(
            f"SELECT x.*, c.title FROM ({run_sql}) x LEFT JOIN code_task_runs c ON c.id = x.run_id "
            "ORDER BY x.money DESC, x.started_at DESC LIMIT :lim"), {**params, "lim": RUN_LIMIT}).mappings()
        for r in rows:
            started = r["started_at"]
            runs.append({"run_id": r["run_id"], "kind": r["kind"], "project_id": r["project_id"],
                         "title": r["title"] or None, "money_usd_micros": int(r["money"] or 0),
                         "calls": int(r["calls"] or 0),
                         "started_at": started.isoformat() if isinstance(started, datetime) else None})

    rate = spent / max(elapsed, 1.0)
    forecast = int(round(spent + rate * left))
    policy = _cap(connection, org_id, scope_type, scope_id)
    cap_source = "scoped_budget" if policy else None
    if policy is None and scope_type == "project":
        # A project with no scoped monthly budget still has its own monthly cap (Govern ›
        # Limits, /v1/budget): the forecast is read against it.
        policy = _project_cap(connection, scope_id)
        cap_source = "project_budget" if policy else None
    cap = int(policy["cap_usd_micros"]) if policy else None
    warn = float(policy["warn_pct"]) if policy else 0.8
    return {
        "scope_type": scope_type, "scope_id": scope_id,
        "label": scope_label(connection, scope_type, scope_id),
        "period": "month", "period_start": start.isoformat(), "period_end": end.isoformat(),
        "days_total": round(days_total, 2), "days_elapsed": round(elapsed, 2), "days_left": round(left, 2),
        "spent_usd_micros": spent,
        "run_rate_usd_micros_per_day": int(round(rate)),
        "forecast_usd_micros": forecast,
        "cap_usd_micros": cap, "warn_pct": warn,
        "hard_stop": (bool(policy["hard_stop"]) if policy and policy.get("hard_stop") is not None
                      else None),
        "policy_id": policy["id"] if policy else None, "cap_source": cap_source,
        "state": state_of(spent, forecast, cap, warn),
        "projects": projects,
        "by_project": by_project, "daily": daily,
        "run_count": run_count,
        "avg_run_usd_micros": int(round(run_total / run_count)) if run_count else 0,
        "runs": runs,
    }


__all__ = ["SCOPES", "month_forecast", "scope_projects", "scope_label", "state_of",
           "team_projects_sql"]
