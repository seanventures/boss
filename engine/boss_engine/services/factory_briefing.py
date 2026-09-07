"""The whole factory, in one read: what is stuck, what it means, what usually fixes it.

THREE THINGS BECOME ONE.

BOSS had three agent surfaces. The chat area (`?area=agent`) — a thread rail and a composer that
knows nothing about the system it is attached to. The Metatron Captain (`?area=metatron`) — an
always-on overwatch whose `metatron_sessions` table does not exist in this database, so the page
is a shell over durable state that was never created. And the Task Shepherd — an autopilot that
was switched off after one afternoon in which it opened repair tasks nobody asked about, retried
a build the operator was already retrying by hand, and held the only worker slot for 33 minutes.

The operator's verdict on all three: "I never used Metatron and Agent chat is not good... if
it's not built correctly then we don't have to expose."

What actually works is the agent as it appears on the OTHER screens — Workflows, Infra, Factory,
Monitoring — and what makes it work there is not the model. It is that the screen hands it the
facts before the first word: the app that is down, named, with its error and address. A composer
with no context is the same agent doing worse, because the operator has to type what the page
already knew.

So there is one agent, and this is what gives it the whole factory instead of one screen.

AND THE SHEPHERD'S JUDGEMENT SURVIVES ITS AUTOPILOT.

`task_shepherd.classify()` and its remedy table are the best thing in the three — a taxonomy of
what operational failures MEAN, written from a session where 13 governed runs cost $66.65 and
58% produced nothing, every failure operational rather than a coding fault. That knowledge was
never the problem. Acting on it unattended was. Here it names each failure and proposes the
remedy as a button; nothing runs until somebody presses one.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text as sql

from . import task_shepherd
from .naming import identity_key, project_name

_log = logging.getLogger(__name__)

#: How many of each kind to carry. A briefing is what to look at now, not an inventory — thirty
#: rows of "needs you" is the same as none, because nobody reads to the bottom of it.
PER_KIND = 6


def _app_of(title: str) -> str:
    display, _blurb, _label = project_name(str(title or ""))
    return display if display and display != "Untitled app" else str(title or "")[:60]


def _diagnosis(error: Optional[str], *, gates_failed: Optional[list] = None) -> dict[str, Any]:
    """What this failure IS, and what usually fixes it — the shepherd's table, read-only.

    Returns the remedies as descriptions and action names. Never runs one: the caller renders
    them as buttons, which is the whole difference between this and the autopilot.
    """
    try:
        kind = task_shepherd.classify(error, gates_failed=gates_failed)
    except Exception:  # noqa: BLE001 — an unclassified failure is still worth showing
        return {}
    name = task_shepherd.CLASS_NAMES.get(kind, "")
    remedies = [
        {"id": r.id, "describe": r.describe, "action": r.action,
         "already_automatic": bool(r.automatic)}
        for r in task_shepherd.REMEDIES.get(kind, ())
    ]
    return {"kind": name, "remedies": remedies} if name else {}


def _decisions(connection, project_id: str) -> list[dict[str, Any]]:
    """Approvals with nobody's answer on them. The commonest reason a factory is idle."""
    rows = connection.execute(
        sql("SELECT id, title, status, risk_tier, deadline_at, default_action, created_at "
            "FROM decisions WHERE project_id = :p AND status IN ('pending', 'delegated') "
            "ORDER BY created_at LIMIT :n"),
        {"p": project_id, "n": PER_KIND}).mappings().all()
    out = []
    for row in rows:
        overdue = bool(row["deadline_at"]) and str(row["default_action"] or "hold") == "hold"
        out.append({
            "kind": "decision", "id": row["id"], "title": str(row["title"] or "")[:140],
            "since": str(row["created_at"]),
            # An expired hold applies NOTHING, so the work waits for ever rather than taking a
            # safe default. Saying "overdue" without that is a nag; saying it with that is a
            # reason to act.
            "note": ("past its deadline — the default is 'hold', which applies nothing, so this "
                     "waits until somebody answers it" if overdue else
                     "waiting for an answer"),
            "actions": [{"tool": "decide", "args": {"decision_id": row["id"],
                                                    "action": "approve"},
                         "label": f"Approve — {str(row['title'] or row['id'])[:60]}"}],
        })
    return out


def _stuck_steps(connection, project_id: str) -> list[dict[str, Any]]:
    """Workflow steps that failed, with what the failure means."""
    rows = connection.execute(
        sql("SELECT s.id, s.step_id, s.status, s.last_error, s.output, s.workflow_run_id, "
            "       s.updated_at, r.input "
            "FROM workflow_step_runs s JOIN workflow_runs r ON r.id = s.workflow_run_id "
            "WHERE s.project_id = :p AND s.status IN ('failed', 'deadlettered') "
            "ORDER BY s.updated_at DESC LIMIT :n"),
        {"p": project_id, "n": PER_KIND}).mappings().all()
    out = []
    for row in rows:
        error = str(row["last_error"] or "")
        item = {
            "kind": "workflow_step", "id": row["id"], "step": row["step_id"],
            "run_id": row["workflow_run_id"], "status": row["status"],
            "app": _app_of(str((row["input"] or {}).get("title") or "")),
            "error": error[:300], "since": str(row["updated_at"]),
            "actions": [{"tool": "recheck_workflow_run",
                         "args": {"run_id": row["workflow_run_id"]},
                         "label": "Re-check what stopped it"}],
        }
        diagnosis = _diagnosis(error)
        if diagnosis:
            item["diagnosis"] = diagnosis
        out.append(item)
    return out


def _unhealthy(connection, project_id: str) -> list[dict[str, Any]]:
    """Apps that are deployed and not answering. The only item here an outage looks like."""
    rows = connection.execute(
        sql("SELECT t.id, t.app_name, t.environment, t.domains, "
            "       (SELECT s.ok FROM deployment_health_samples s "
            "         WHERE s.deployment_target_id = t.id "
            "         ORDER BY s.checked_at DESC LIMIT 1) AS last_ok, "
            "       (SELECT s.error FROM deployment_health_samples s "
            "         WHERE s.deployment_target_id = t.id "
            "         ORDER BY s.checked_at DESC LIMIT 1) AS last_error "
            "FROM deployment_targets t "
            "WHERE t.project_id = :p AND t.status = 'active' ORDER BY t.app_name"),
        {"p": project_id}).mappings().all()
    out = []
    for row in rows:
        if row["last_ok"] is not False:          # healthy, or never probed — not an outage
            continue
        where = ", ".join(str(d) for d in (row["domains"] or [])) or row["id"]
        item = {
            "kind": "unhealthy_app", "id": row["id"],
            "app": row["app_name"], "environment": row["environment"], "at": where,
            "error": str(row["last_error"] or "")[:300],
            "actions": [{"tool": "http_probe", "args": {"url": f"http://{where}/"},
                         "label": f"Probe {row['app_name']} now"}],
        }
        diagnosis = _diagnosis(str(row["last_error"] or ""))
        if diagnosis:
            item["diagnosis"] = diagnosis
        out.append(item)
    return out[:PER_KIND]


def _stalled_builds(connection, project_id: str) -> list[dict[str, Any]]:
    """Code tasks that have been running long enough to be worth a look."""
    rows = connection.execute(
        sql("SELECT id, title, status, phase, updated_at, "
            "       EXTRACT(EPOCH FROM (now() - updated_at)) AS quiet_seconds "
            "FROM code_task_runs WHERE project_id = :p "
            "  AND status IN ('running', 'in_progress') "
            "  AND updated_at < now() - INTERVAL '20 minutes' "
            "ORDER BY updated_at LIMIT :n"),
        {"p": project_id, "n": PER_KIND}).mappings().all()
    return [{
        "kind": "quiet_build", "id": row["id"], "app": _app_of(str(row["title"] or "")),
        "phase": row["phase"], "quiet_minutes": int((row["quiet_seconds"] or 0) // 60),
        # NOT "stalled". A build that has been quiet for twenty minutes is usually a build that
        # is working — the last one called stalled was cancelled and was fine.
        "note": "quiet for a while; that is often normal for this phase",
        "actions": [{"tool": "diagnose_run", "args": {"run_id": row["id"]},
                     "label": f"Diagnose {_app_of(str(row['title'] or ''))}"}],
    } for row in rows]


def factory_briefing(connection, project_id: str) -> dict[str, Any]:
    """Everything that wants a person, across the whole factory.

    Each section is independently guarded: a briefing that fails because one table is missing
    tells the operator nothing about the four that are fine.
    """
    sections: dict[str, list] = {}
    for name, reader in (("decisions", _decisions), ("workflow_steps", _stuck_steps),
                         ("unhealthy", _unhealthy), ("quiet_builds", _stalled_builds)):
        try:
            # Bounded HERE as well as in each query. The LIMIT is the efficient half; this is
            # the guarantee — a briefing is what to look at now, and thirty rows of "needs you"
            # is the same as none because nobody reads to the bottom of it.
            sections[name] = list(reader(connection, project_id))[:PER_KIND]
        except Exception as exc:  # noqa: BLE001
            _log.warning("briefing section %s failed: %s", name, exc)
            sections[name] = []
    items = [item for group in sections.values() for item in group]
    return {
        "project_id": project_id,
        "items": items,
        "counts": {name: len(group) for name, group in sections.items()},
        "total": len(items),
        "quiet": not items,
    }


def briefing_lines(briefing: dict[str, Any]) -> list[str]:
    """The briefing as sentences — what the agent is handed before the first question.

    The reason the other screens' agents answer well is that the screen tells them the facts
    first. This is that, for the whole factory.
    """
    lines: list[str] = []
    for item in briefing.get("items") or []:
        kind = item.get("kind")
        if kind == "decision":
            lines.append(f"Decision {item['id']} is unanswered: {item['title']} — {item['note']}.")
        elif kind == "workflow_step":
            what = (item.get("diagnosis") or {}).get("kind")
            lines.append(
                f"{item['app']}: workflow step '{item['step']}' is {item['status']}"
                + (f" ({what})" if what else "") + f" — {item['error']}")
        elif kind == "unhealthy_app":
            lines.append(
                f"{item['app']} ({item['environment']}) is not answering at {item['at']}"
                + (f" — {item['error']}" if item.get("error") else "."))
        elif kind == "quiet_build":
            lines.append(
                f"{item['app']} has been quiet for {item['quiet_minutes']} minutes in "
                f"{item.get('phase') or 'its current phase'}.")
    return lines


__all__ = ["factory_briefing", "briefing_lines", "PER_KIND"]
