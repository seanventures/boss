"""Outcome tracking and the autonomy metrics (BOSSAPP-912; design §8 "Success metrics", §11.7).

ONE OUTCOME STORE: THE LEDGER (spec §9)
---------------------------------------
Every outcome is written onto the decision it judges — `agent_act_decisions.outcome`, filled
once by `ledger.record_outcome` — never into a parallel proposal_outcomes table. This module
adds the outcomes nobody clicks for:

    ignored    a proposal (propose / ask / convene / comment) nobody acted on for 7 days
    recurred   an item resolved, and the same problem (fingerprint) opened again within 14 days
    merged     the build an agent started was merged and not reverted within 14 days
    reverted   ... or was reverted (which demotes the class at once, policy.review_track_record)

`accepted`, `dismissed` and `overridden` are written where the person acts (roadmap.accept /
reject, ledger.override). The sweep is idempotent: an outcome is set once, so a second pass
finds nothing left to do.

THE METRICS ARE COMPUTED, NEVER ESTIMATED (§11.7)
-------------------------------------------------
`metrics()` reads the ledger, the board, incidents, run scorecards and the roadmap, per project
and (optionally) per task class: acceptance %, noise rate, override rate, revert rate,
decisions/day by level and decider, time to a human decision, MTTM/MTTR, cost per resolved
item and per merged change, and the share of work BOSS started itself. The scorecard endpoint
(read_model.scorecard) carries it under `outcomes`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import ledger
from ._common import now

IGNORED_AFTER = timedelta(days=7)
RECUR_WINDOW = timedelta(days=14)
REVERT_WINDOW = timedelta(days=14)
#: Actions that ASK a person something: unanswered for 7 days, the proposal was ignored.
PROPOSING = ("propose", "ask_question", "convene_meeting", "comment")
#: Actions that make a change a PR can carry.
CHANGING = ("open_draft_pr", "start_workflow", "quarantine_test")
ACCEPTED = ("accepted", "edited", "merged", "succeeded")
NOISE = ("dismissed", "ignored")
BATCH = 500


def sweep(connection: Connection, *, project_id: Optional[str] = None,
          at: Optional[datetime] = None) -> dict[str, int]:
    """Write the outcomes time decides. Returns how many rows got each outcome."""
    moment = at or now()
    done = {"ignored": 0, "recurred": 0, "merged": 0, "reverted": 0}
    for act_id in _ignored(connection, project_id, moment):
        ledger.record_outcome(connection, act_id, outcome="ignored",
                              outcome_ref="no person acted within 7 days")
        done["ignored"] += 1
        _roadmap_ignored(connection, act_id)
    for act_id, ref in _recurred(connection, project_id, moment):
        ledger.record_outcome(connection, act_id, outcome="recurred", outcome_ref=ref)
        done["recurred"] += 1
    for act_id, outcome, ref in _pull_requests(connection, project_id, moment):
        ledger.record_outcome(connection, act_id, outcome=outcome, outcome_ref=ref)
        done[outcome] += 1
    return done


def _ignored(connection: Connection, project_id: Optional[str], moment: datetime) -> list[str]:
    """Non-person proposals older than 7 days, still open, on an item that is still live, with
    no person's decision on that item since (a snooze or a take-over is an answer)."""
    return list(connection.execute(text(
        "SELECT d.act_id FROM agent_act_decisions d LEFT JOIN work_items w "
        "ON w.id = d.work_item_id WHERE d.outcome IS NULL AND d.decided_by NOT LIKE 'person:%' "
        "AND d.action = ANY(:acts) AND d.created_at <= :cut "
        "AND (CAST(:p AS TEXT) IS NULL OR d.project_id = :p) "
        "AND (d.work_item_id IS NULL OR (w.lifecycle IN ('proposed', 'needs_you') "
        "AND COALESCE(w.lease_kind, '') <> 'person')) "
        "AND NOT EXISTS (SELECT 1 FROM agent_act_decisions p WHERE p.work_item_id = "
        "d.work_item_id AND p.decided_by LIKE 'person:%' AND p.created_at >= d.created_at) "
        "ORDER BY d.created_at LIMIT :n"),
        {"acts": list(PROPOSING), "cut": moment - IGNORED_AFTER, "p": project_id,
         "n": BATCH}).scalars())


def _roadmap_ignored(connection: Connection, act_id: str) -> None:
    rec = connection.execute(text(
        "SELECT work_item_id, project_id, detector, fingerprint, kind, evidence_count FROM "
        "ambient_recommendations WHERE proposal_act_id = :a"), {"a": act_id}).mappings().first()
    if rec is None:
        return
    from .roadmap import _feedback

    _feedback(connection, rec["project_id"], dict(rec), "ignored")


def _recurred(connection: Connection, project_id: Optional[str],
              moment: datetime) -> list[tuple[str, str]]:
    """The newest open-outcome agent decision on a RESOLVED item whose fingerprint opened again
    within 14 days of the resolution."""
    rows = connection.execute(text(
        "SELECT DISTINCT ON (old.id) d.act_id, again.id AS again FROM work_items old "
        "JOIN work_items again ON again.project_id = old.project_id AND again.ambient "
        "AND again.fingerprint = old.fingerprint AND again.id <> old.id "
        "AND again.created_at >= old.resolved_at AND again.created_at <= old.resolved_at + :win "
        "JOIN agent_act_decisions d ON d.work_item_id = old.id AND d.outcome IS NULL "
        "AND d.decided_by NOT LIKE 'person:%' "
        "WHERE old.ambient AND old.lifecycle = 'resolved' AND old.resolved_at > :since "
        "AND (CAST(:p AS TEXT) IS NULL OR old.project_id = :p) "
        "ORDER BY old.id, d.created_at DESC LIMIT :n"),
        {"win": RECUR_WINDOW, "since": moment - timedelta(days=90), "p": project_id,
         "n": BATCH}).all()
    return [(act, f"work_item:{again}") for act, again in rows]


def _pull_requests(connection: Connection, project_id: Optional[str],
                   moment: datetime) -> list[tuple[str, str, str]]:
    """A change an agent started (its build / run linked to the item) is judged by the run
    scorecard's late outcomes: reverted at once, merged once it held for 14 days."""
    rows = connection.execute(text(
        "SELECT DISTINCT ON (d.act_id) d.act_id, s.pr_merged, s.pr_reverted, s.unit_id, "
        "COALESCE(s.late_outcomes_at, s.ended_at, s.computed_at) AS judged_at "
        "FROM agent_act_decisions d JOIN work_item_links l ON l.work_item_id = d.work_item_id "
        "AND l.kind IN ('code_task', 'workflow_run') JOIN run_scorecards s ON s.unit_id = l.ref "
        "AND s.unit_kind IN ('build', 'workflow_run') WHERE d.outcome IS NULL "
        "AND d.decided_by NOT LIKE 'person:%' AND d.action = ANY(:acts) "
        "AND (s.pr_reverted OR s.pr_merged) AND (CAST(:p AS TEXT) IS NULL OR d.project_id = :p) "
        "ORDER BY d.act_id, s.pr_reverted DESC LIMIT :n"),
        {"acts": list(CHANGING), "p": project_id, "n": BATCH}).mappings().all()
    out: list[tuple[str, str, str]] = []
    for r in rows:
        if r["pr_reverted"]:
            out.append((r["act_id"], "reverted", f"run:{r['unit_id']}"))
        elif r["pr_merged"] and r["judged_at"] is not None and \
                r["judged_at"] <= moment - REVERT_WINDOW:
            out.append((r["act_id"], "merged", f"run:{r['unit_id']}"))
    return out


# ── metrics ─────────────────────────────────────────────────────────────────────────────────

def _pct(part: int, whole: int) -> Optional[float]:
    return round(100 * part / whole, 1) if whole else None


def _rates(r: Any) -> dict[str, Any]:
    decided, judged = int(r["decided"] or 0), int(r["judged"] or 0)
    accepted, noise = int(r["accepted"] or 0), int(r["noise"] or 0)
    overridden, reverted = int(r["overridden"] or 0), int(r["reverted"] or 0)
    merged = int(r["merged"] or 0)
    return {"decided": decided, "with_outcome": judged, "pending": decided - judged,
            "accepted": accepted, "dismissed_or_ignored": noise, "overridden": overridden,
            "reverted": reverted, "merged": merged,
            "acceptance_pct": _pct(accepted, judged), "noise_pct": _pct(noise, judged),
            "override_pct": _pct(overridden, decided),
            "revert_pct": _pct(reverted, merged + reverted)}


_RATES_SQL = (
    "count(*) AS decided, count(outcome) AS judged, "
    "count(*) FILTER (WHERE outcome = ANY(:acc)) AS accepted, "
    "count(*) FILTER (WHERE outcome = ANY(:noise)) AS noise, "
    "count(*) FILTER (WHERE outcome = 'overridden') AS overridden, "
    "count(*) FILTER (WHERE outcome IN ('reverted', 'incident_caused')) AS reverted, "
    "count(*) FILTER (WHERE outcome = 'merged') AS merged")


def metrics(connection: Connection, project_id: str, *, days: int = 7,
            task_class: Optional[str] = None) -> dict[str, Any]:
    """Design §8's success metrics for one project (optionally one task class)."""
    from .read_model import _summary as spread

    days = max(1, min(int(days), 365))
    args = {"p": project_id, "d": days, "c": task_class, "acc": list(ACCEPTED),
            "noise": list(NOISE)}
    scope = ("d.project_id = :p AND d.created_at > now() - make_interval(days => :d) "
             "AND (CAST(:c AS TEXT) IS NULL OR d.task_class = :c)")
    overall = connection.execute(text(
        f"SELECT {_RATES_SQL} FROM agent_act_decisions d WHERE {scope} "
        "AND d.decided_by NOT LIKE 'person:%'"), args).mappings().one()
    by_class = [{"task_class": r["task_class"], **_rates(r)} for r in connection.execute(text(
        f"SELECT d.task_class, {_RATES_SQL} FROM agent_act_decisions d WHERE {scope} "
        "AND d.decided_by NOT LIKE 'person:%' GROUP BY d.task_class ORDER BY d.task_class"),
        args).mappings()]
    levels = connection.execute(text(
        "SELECT COALESCE(autonomy_level, '-') AS level, split_part(decided_by, ':', 1) AS who, "
        f"count(*) AS n FROM agent_act_decisions d WHERE {scope} GROUP BY 1, 2"),
        args).mappings().all()
    by_level: dict[str, int] = {}
    by_decider: dict[str, int] = {}
    for r in levels:
        by_level[r["level"]] = by_level.get(r["level"], 0) + int(r["n"])
        by_decider[r["who"]] = by_decider.get(r["who"], 0) + int(r["n"])

    waits = [float(x) for x in connection.execute(text(
        "SELECT EXTRACT(EPOCH FROM (answered_at - needs_you_at)) / 60 FROM work_items WHERE "
        "project_id = :p AND ambient AND answered_at IS NOT NULL AND needs_you_at IS NOT NULL "
        "AND answered_at > now() - make_interval(days => :d) "
        "AND (CAST(:c AS TEXT) IS NULL OR task_class = :c)"), args).scalars()]
    waits += [float(x) for x in connection.execute(text(
        "SELECT EXTRACT(EPOCH FROM (decided_at - created_at)) / 60 FROM ambient_recommendations "
        "WHERE project_id = :p AND decided_at > now() - make_interval(days => :d) "
        "AND (CAST(:c AS TEXT) IS NULL OR :c = 'roadmap_item')"), args).scalars()]

    incidents = _incident_times(connection, project_id, days)
    resolved = connection.execute(text(
        "SELECT count(*) FILTER (WHERE lifecycle = 'resolved') AS resolved, "
        "COALESCE(sum(cost_usd_micros), 0) AS cost FROM work_items WHERE project_id = :p AND "
        "ambient AND created_at > now() - make_interval(days => :d) "
        "AND (CAST(:c AS TEXT) IS NULL OR task_class = :c)"), args).mappings().one()
    n_resolved, spent = int(resolved["resolved"] or 0), int(resolved["cost"] or 0)
    merged = int(overall["merged"] or 0)
    return {
        "days": days, "task_class": task_class,
        **_rates(overall),
        "decisions_by_level": by_level, "decisions_by_decider": by_decider,
        "decisions_per_day": round(sum(by_level.values()) / days, 2),
        "time_to_human_decision_minutes": spread(waits),
        "incidents": incidents,
        "cost": {"spent_usd": round(spent / 1e6, 4),
                 "per_resolved_item_usd": round(spent / 1e6 / n_resolved, 4)
                 if n_resolved else None,
                 "per_merged_change_usd": round(spent / 1e6 / merged, 4) if merged else None},
        "ambient_share": _ambient_share(connection, project_id, days),
        "by_task_class": by_class,
    }


def _incident_times(connection: Connection, project_id: str, days: int) -> dict[str, Any]:
    """MTTM (first seen → mitigated) and MTTR (first seen → resolved), and how many incidents
    agents resolved with no person's hand except approvals."""
    from .read_model import _summary as spread

    rows = connection.execute(text(
        "SELECT i.id, EXTRACT(EPOCH FROM (i.mitigated_at - i.first_seen)) / 60 AS mttm, "
        "EXTRACT(EPOCH FROM (i.resolved_at - i.first_seen)) / 60 AS mttr, "
        "EXISTS (SELECT 1 FROM work_item_links l JOIN work_items w ON w.id = l.work_item_id "
        "WHERE l.kind = 'incident' AND l.ref = i.id AND w.lifecycle = 'resolved' AND "
        "COALESCE(w.resolved_by, '') NOT LIKE 'person:%') AS by_agents "
        "FROM incidents i WHERE i.project_id = :p AND i.first_seen > now() - "
        "make_interval(days => :d)"), {"p": project_id, "d": days}).mappings().all()
    mttm = [float(r["mttm"]) for r in rows if r["mttm"] is not None and r["mttm"] >= 0]
    mttr = [float(r["mttr"]) for r in rows if r["mttr"] is not None and r["mttr"] >= 0]
    resolved = len(mttr)
    by_agents = sum(1 for r in rows if r["by_agents"])
    return {"n": len(rows), "mttm_minutes": spread(mttm), "mttr_minutes": spread(mttr),
            "agent_resolved_pct": _pct(by_agents, resolved)}


def _ambient_share(connection: Connection, project_id: str, days: int) -> dict[str, Any]:
    """The share of work BOSS started itself: work items it opened (ambient, or accepted from
    its roadmap) of all work items, and builds started from an ambient item of all builds."""
    items = connection.execute(text(
        "SELECT count(*) AS all_items, count(*) FILTER (WHERE ambient OR source = 'roadmap') "
        "AS boss FROM work_items WHERE project_id = :p AND created_at > now() - "
        "make_interval(days => :d) AND (ambient IS NOT TRUE OR task_class <> 'roadmap_item')"),
        {"p": project_id, "d": days}).mappings().one()
    builds = connection.execute(text(
        "SELECT count(*) AS all_builds, count(*) FILTER (WHERE EXISTS (SELECT 1 FROM "
        "work_item_links l WHERE l.kind = 'code_task' AND l.ref = r.id)) AS boss FROM "
        "code_task_runs r WHERE r.project_id = :p AND r.created_at > now() - "
        "make_interval(days => :d)"), {"p": project_id, "d": days}).mappings().one()
    return {"work_items": {"boss": int(items["boss"]), "all": int(items["all_items"]),
                           "pct": _pct(int(items["boss"]), int(items["all_items"]))},
            "builds": {"boss": int(builds["boss"]), "all": int(builds["all_builds"]),
                       "pct": _pct(int(builds["boss"]), int(builds["all_builds"]))}}


__all__ = ["sweep", "metrics", "IGNORED_AFTER", "RECUR_WINDOW", "REVERT_WINDOW"]
