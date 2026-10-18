"""THE AUTONOMY METRIC: of the problems a run hit, how many BOSS's agents solved by themselves.

BOSSAPP-571 PRD R15 and §10; spec §12. Baseline 2026-10-17: run wfr_f383649a8b024d97a80c hit two
BOSS-side problems and BOSS solved 0 of them (an outside coding agent wrote both fixes).

A PROBLEM is one failure episode of a step: a cause signature the recovery agent decided on,
from that failure until the step succeeds, is skipped, fails for a DIFFERENT cause (a new
problem), or the run ends. Consecutive decisions on the same signature are one problem.

HOW IT WAS RESOLVED, decided from the run's own events when the problem closes:
    by BOSS            the agent's own action held — retry, clean and retry, fix the app — or a
                       BOSS self-fix was installed and the step got past the cause (verified)
    by a person        a person answered an `ask_person`, retried or skipped the step after the
                       agent gave up
    by an outside agent the step resumed on a BOSS build change that was NOT a self-fix (a
                       person's own coding agent fixed BOSS and installed it)
    open               not resolved when the run ended (or still running)
Approving a decision is governance, not work: it does not count against BOSS. The HANDS-OFF rate
counts only problems BOSS solved with no person event at all, so the cost of approvals shows.

Rate = by BOSS ÷ all problems. A BOSS-SIDE cut counts only the problems the recovery agent placed
on BOSS (wait_for_boss / fix_boss), to match the PRD's success metric (≥ 50 % on the replay set).

Computed on read from `events` (recovery.decided, recovery.resumed, self_fix.*, workflow.step_*),
never stored: one rule, so the run page, the Self-fixes trend and Improve BOSS cannot disagree.
This file is `never`-protected for self-fixes: BOSS does not change how it counts its own wins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import now_utc

BY_BOSS, BY_PERSON, BY_OUTSIDE, OPEN = "boss", "person", "outside_agent", "open"
CLASSES = (BY_BOSS, BY_PERSON, BY_OUTSIDE, OPEN)
_AGENT_ACTIONS = ("retry", "clean_and_retry", "fix_app")
_BOSS_SIDE = ("wait_for_boss", "fix_boss")
_SYSTEM_ACTORS = ("boss", "system", "sys_worker", "sys_self_installer", "boss_maintainer",
                  "auto_approval", "scheduler", "worker")
EVENT_TYPES = ("recovery.decided", "recovery.resumed", "self_fix.verified", "self_fix.approved",
               "workflow.step_retried", "workflow.step_skipped", "workflow.step_succeeded")
WINDOWS = {"7d": 7, "30d": 30, "90d": 90}


def _is_person(actor: Any) -> bool:
    a = str(actor or "")
    return bool(a) and a not in _SYSTEM_ACTORS and not a.startswith(("sys_", "agt_", "role_"))


def _data(e: Mapping[str, Any]) -> dict[str, Any]:
    value = e.get("data") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return value if isinstance(value, dict) else {}


def _at(e: Mapping[str, Any]) -> Optional[str]:
    v = e.get("created_at")
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v else None)


def classify(events: Iterable[Mapping[str, Any]], *, run_ended: bool = True
             ) -> list[dict[str, Any]]:
    """The problems of ONE run from its events (oldest first): [{step_id, signature,
    resolved_by, how, self_fix_id, boss_side, hands_off, started_at, ended_at}]."""
    open_by_step: dict[str, dict[str, Any]] = {}
    done: list[dict[str, Any]] = []

    def close(problem: dict[str, Any], resolved_by: str, how: str, at: Optional[str]) -> None:
        problem.update(resolved_by=resolved_by, how=how, ended_at=at,
                       hands_off=resolved_by == BY_BOSS and not problem.pop("_touched", False))
        problem.pop("_candidate", None)
        done.append(problem)

    def settle(problem: dict[str, Any], at: Optional[str]) -> None:
        by, how = problem.get("_candidate") or (BY_BOSS, "the agent's own action held")
        close(problem, by, how, at)

    for e in events:
        kind = str(e.get("event_type") or "")
        d = _data(e)
        step = str(d.get("step_id") or "")
        if not step:
            continue
        at = _at(e)
        current = open_by_step.get(step)
        if kind == "recovery.decided":
            signature = str(d.get("signature") or "")
            action = str(d.get("action") or "")
            if current is not None and signature and signature != current["signature"]:
                # Got past it: the cause changed. Whatever moved it on resolved it.
                settle(current, at)
                current = None
            if current is None:
                current = {"step_id": step, "signature": signature, "started_at": at,
                           "self_fix_id": None, "boss_side": False, "_touched": False,
                           "_candidate": None}
                open_by_step[step] = current
            if action in _BOSS_SIDE:
                current["boss_side"] = True
                current["_candidate"] = (BY_OUTSIDE, "waiting for a BOSS update")
            elif action == "ask_person":
                current["_candidate"] = (BY_PERSON, "a person answered the agent")
                current["_touched"] = True
            elif action in _AGENT_ACTIONS:
                current["_candidate"] = (BY_BOSS, f"the agent's {action.replace('_', ' ')}")
        elif current is None:
            continue
        elif kind == "recovery.resumed":
            why = str(d.get("why") or "")
            if why == "self_fix_installed" or d.get("self_fix_id"):
                current["self_fix_id"] = d.get("self_fix_id")
                current["_candidate"] = (BY_BOSS, "BOSS fixed itself")
            elif why == "boss_updated":
                current["_candidate"] = (BY_OUTSIDE, "BOSS was updated by hand")
        elif kind == "self_fix.verified":
            current["self_fix_id"] = d.get("self_fix_id") or current["self_fix_id"]
            current["_candidate"] = (BY_BOSS, "BOSS fixed itself")
        elif kind == "self_fix.approved":
            current["_touched"] = True   # governance: not against BOSS, against hands-off
        elif kind == "workflow.step_retried" and _is_person(e.get("actor")):
            current["_candidate"] = (BY_PERSON, "a person retried the step")
            current["_touched"] = True
        elif kind == "workflow.step_skipped":
            open_by_step.pop(step, None)
            current["_touched"] = True
            close(current, BY_PERSON, "a person skipped the step", at)
        elif kind == "workflow.step_succeeded":
            open_by_step.pop(step, None)
            settle(current, at)
    for step, problem in list(open_by_step.items()):
        problem.pop("_touched", None)
        problem.pop("_candidate", None)
        problem.update(resolved_by=OPEN, ended_at=None, hands_off=False,
                       how="not resolved when the run ended" if run_ended else "still open")
        done.append(problem)
    done.sort(key=lambda p: str(p.get("started_at") or ""))
    return done


def counts(problems: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(problems)
    out = {"problems": len(items), "by_boss": 0, "by_person": 0, "by_outside_agent": 0,
           "open": 0, "hands_off": 0}
    key = {BY_BOSS: "by_boss", BY_PERSON: "by_person", BY_OUTSIDE: "by_outside_agent",
           OPEN: "open"}
    for p in items:
        out[key[str(p.get("resolved_by"))]] += 1
        out["hands_off"] += 1 if p.get("hands_off") else 0
    out["rate"] = round(out["by_boss"] / out["problems"], 3) if out["problems"] else None
    out["hands_off_rate"] = round(out["hands_off"] / out["problems"], 3) if out["problems"] \
        else None
    return out


def sentence(c: Mapping[str, Any]) -> str:
    """`3 problems · BOSS solved 2 · you solved 1` — the run header's line."""
    n = int(c.get("problems") or 0)
    if not n:
        return "No problems"
    parts = [f"{n} problem{'s' if n != 1 else ''}", f"BOSS solved {c.get('by_boss', 0)}"]
    if c.get("by_person"):
        parts.append(f"you solved {c['by_person']}")
    if c.get("by_outside_agent"):
        parts.append(f"an outside agent solved {c['by_outside_agent']}")
    if c.get("open"):
        parts.append(f"{c['open']} open")
    return " · ".join(parts)


def _events(connection: Connection, *, project_id: str, run_ids: Optional[list[str]] = None,
            since: Optional[datetime] = None) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT entity_id AS run_id, event_type, actor, data, created_at FROM events "
        "WHERE project_id = :p AND entity_type = 'workflow_run' AND event_type = ANY(:t) "
        "AND (CAST(:runs AS TEXT[]) IS NULL OR entity_id = ANY(CAST(:runs AS TEXT[]))) "
        "AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR created_at >= CAST(:since AS TIMESTAMPTZ)) "
        "ORDER BY created_at, id"),
        {"p": project_id, "t": list(EVENT_TYPES), "runs": run_ids,
         "since": since}).mappings().all()
    return [dict(r) for r in rows]


def _runs(connection: Connection, project_id: str, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT r.id, r.status, r.created_at, r.completed_at, r.updated_at, "
                "COALESCE(d.name, r.id) AS name FROM workflow_runs r LEFT JOIN "
                "workflow_definitions d ON d.id = r.workflow_definition_id WHERE r.project_id = :p "
                "AND r.id = ANY(:ids)"), {"p": project_id, "ids": run_ids}).mappings().all()
    except Exception:  # noqa: BLE001 — a store without definitions: ids, no names
        rows = connection.execute(text(
            "SELECT id, status, created_at, completed_at, updated_at, id AS name FROM "
            "workflow_runs WHERE project_id = :p AND id = ANY(:ids)"),
            {"p": project_id, "ids": run_ids}).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


_ENDED = ("completed", "failed", "deadlettered", "cancelled", "compensated")


def run_report(connection: Connection, *, project_id: str, run_id: str) -> dict[str, Any]:
    """One run: its problems, the counts, the rate and the header sentence."""
    events = _events(connection, project_id=project_id, run_ids=[run_id])
    run = _runs(connection, project_id, [run_id]).get(run_id) or {}
    problems = classify(events, run_ended=str(run.get("status") or "") in _ENDED)
    c = counts(problems)
    return {"run_id": run_id, "counts": {k: c[k] for k in ("problems", "by_boss", "by_person",
                                                          "by_outside_agent", "open")},
            "rate": c["rate"], "hands_off_rate": c["hands_off_rate"], "sentence": sentence(c),
            "boss_side": counts(p for p in problems if p.get("boss_side")),
            "problems": problems}


def _week(value: Any) -> str:
    d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    start = d.date() - timedelta(days=d.weekday())
    return start.isoformat()


def report(connection: Connection, *, project_id: str, window: str = "30d") -> dict[str, Any]:
    """The project over a window: the totals, the BOSS-side cut, a weekly trend, every run."""
    days = WINDOWS.get(window, 30)
    since = now_utc() - timedelta(days=days)
    events = _events(connection, project_id=project_id, since=since)
    by_run: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_run.setdefault(str(e["run_id"]), []).append(e)
    runs = _runs(connection, project_id, list(by_run))
    all_problems: list[dict[str, Any]] = []
    run_rows = []
    for run_id, evs in by_run.items():
        run = runs.get(run_id) or {}
        problems = classify(evs, run_ended=str(run.get("status") or "") in _ENDED)
        if not problems:
            continue
        for p in problems:
            p["run_id"] = run_id
        all_problems.extend(problems)
        c = counts(problems)
        ended = run.get("completed_at") or run.get("updated_at")
        run_rows.append({"run_id": run_id, "name": str(run.get("name") or run_id),
                         **{k: c[k] for k in ("problems", "by_boss", "by_person",
                                              "by_outside_agent", "open")},
                         "rate": c["rate"], "sentence": sentence(c),
                         "ended_at": ended.isoformat() if isinstance(ended, datetime) else ended})
    run_rows.sort(key=lambda r: str(r.get("ended_at") or ""), reverse=True)
    weeks: dict[str, list[dict[str, Any]]] = {}
    for p in all_problems:
        when = p.get("ended_at") or p.get("started_at")
        if when:
            weeks.setdefault(_week(when), []).append(p)
    trend = []
    cursor = since.date() - timedelta(days=since.weekday())
    while cursor <= now_utc().date():
        key = cursor.isoformat()
        c = counts(weeks.get(key, []))
        trend.append({"week": key, **{k: c[k] for k in ("problems", "by_boss", "by_person",
                                                        "by_outside_agent", "open")},
                      "rate": c["rate"]})
        cursor += timedelta(days=7)
    total = counts(all_problems)
    side = counts(p for p in all_problems if p.get("boss_side"))
    keys = ("problems", "by_boss", "by_person", "by_outside_agent", "open")
    return {"window": window if window in WINDOWS else "30d",
            "totals": {**{k: total[k] for k in keys}, "rate": total["rate"],
                       "hands_off_rate": total["hands_off_rate"]},
            "boss_side": {**{k: side[k] for k in keys}, "rate": side["rate"]},
            "trend": trend, "runs": run_rows[:100]}


__all__ = ["BY_BOSS", "BY_OUTSIDE", "BY_PERSON", "CLASSES", "OPEN", "classify", "counts",
           "report", "run_report", "sentence"]
