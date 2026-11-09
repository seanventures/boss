"""Project › Settings › Notifications (screen 13, 13-25…13-28): WHO is told about what, per
project — Needs you, Incidents, PR ready — each routed to the project's contact points
(Console, Email, Slack…; notify.py's contact points, shared with Observability).

  * Incidents: the project's alert routing tree ROOT (notify.ensure_root): what this row shows
    and edits IS where every alert of the project goes unless a rule says otherwise, so it
    cannot disagree with Observability › Notify.
  * Needs you and PR ready: `project_notification_routes` rows (until a person routes them:
    the console only, nothing is sent). `plan` (the monitor's tick, beside
    notify.plan) enqueues ONE message per pending decision / opened pull request to each routed
    contact point in notify's outbox (idempotent dedupe keys), which delivers it with the same
    retries, rates and receipts as an alert.

Contact point secrets never leave notify.py; this module reads ids, kinds and names only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

EVENTS = (("needs_you", "Needs you", "hand-waving"), ("incidents", "Incidents", "siren"),
          ("pr_ready", "PR ready", "git-merge"))
EVENT_KEYS = tuple(k for k, _l, _i in EVENTS)
KIND_WORDS = {"console": "Console", "email": "Email", "slack_webhook": "Slack",
              "mattermost": "Mattermost", "webhook": "Webhook"}
#: How far back the sweep looks: a route set today does not mail last month's decisions.
LOOKBACK = timedelta(hours=24)


def _contact_points(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    return {str(r["id"]): {"id": r["id"], "kind": r["kind"], "name": r["name"],
                           "word": KIND_WORDS.get(str(r["kind"]), str(r["kind"]).title())}
            for r in connection.execute(text(
                "SELECT id, kind, name FROM contact_points WHERE project_id = :p ORDER BY created_at"),
                {"p": project_id}).mappings()}


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(v) for v in value or []]


def _stored(connection: Connection, project_id: str) -> dict[str, list[str]]:
    return {str(r["event"]): _ids(r["contact_point_ids"]) for r in connection.execute(text(
        "SELECT event, contact_point_ids FROM project_notification_routes WHERE project_id = :p"),
        {"p": project_id}).mappings()}


def route_ids(connection: Connection, project_id: str) -> dict[str, list[str]]:
    """event → contact point ids (the root policy for incidents; stored or default otherwise)."""
    from ..notify import ensure_console, ensure_root

    root = ensure_root(connection, project_id)
    root_ids = _ids(root.get("contact_point_ids"))
    stored = _stored(connection, project_id)
    console = [ensure_console(connection, project_id)]
    return {"needs_you": stored.get("needs_you", console),
            "incidents": root_ids,
            "pr_ready": stored.get("pr_ready", console)}


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    ids = route_ids(connection, project_id)
    cps = _contact_points(connection, project_id)
    routes = []
    for key, label, icon in EVENTS:
        chosen = [cps[i] for i in ids[key] if i in cps]
        # The words a person reads: the channels beyond the console ("Email · Slack"); the
        # console alone reads "Console".
        outside = [c["word"] for c in chosen if c["kind"] != "console"]
        words = " · ".join(dict.fromkeys(outside)) or ("Console" if chosen else "Nobody")
        routes.append({"event": key, "label": label, "icon": icon, "words": words,
                       "contact_point_ids": [c["id"] for c in chosen],
                       "set": {"method": "PUT",
                               "href": f"/v1/projects/{project_id}/notification-routes/{key}",
                               "permission": "observability:manage"}})
    return {"routes": routes, "contact_points": list(cps.values())}


def set_route(connection: Connection, project_id: str, event: str, contact_point_ids: list[str],
              *, actor: str) -> dict[str, Any]:
    """Route one event to these contact points (all of this project's). Incidents writes the
    root policy's contact points (where every alert goes); the others their own row. Until a person routes
    them, Needs you and PR ready show in the console only (no message is sent)."""
    from ..core import emit_event
    from ..notify import ensure_root

    if event not in EVENT_KEYS:
        raise ValueError(f"event must be one of {', '.join(EVENT_KEYS)}")
    cps = _contact_points(connection, project_id)
    wanted = list(dict.fromkeys(str(i) for i in contact_point_ids or []))
    unknown = [i for i in wanted if i not in cps]
    if unknown:
        raise LookupError("Those contact points are not this project's.")
    if event == "incidents":
        root = ensure_root(connection, project_id)
        connection.execute(text(
            "UPDATE notification_policies SET contact_point_ids = CAST(:c AS JSONB) WHERE id = :i"),
            {"c": json.dumps(wanted), "i": root["id"]})
    else:
        connection.execute(text(
            "INSERT INTO project_notification_routes (project_id, event, contact_point_ids, "
            "updated_by) VALUES (:p, :e, CAST(:c AS JSONB), :a) ON CONFLICT (project_id, event) "
            "DO UPDATE SET contact_point_ids = EXCLUDED.contact_point_ids, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()"),
            {"p": project_id, "e": event, "c": json.dumps(wanted), "a": actor})
    emit_event(connection, project_id, "project.notification_route_set", actor, "project",
               project_id, {"event": event, "contact_points": len(wanted)})
    return view(connection, project_id)


def plan(connection: Connection, settings: Any, project_id: str,
         now: Optional[datetime] = None) -> dict[str, int]:
    """Enqueue the Needs-you and PR-ready messages that are due (idempotent: one per decision
    or pull request per contact point, whatever the tick). Console contact points need no
    message (the console already shows them)."""
    from ..decision_actions import waiting_on_operator
    from ..notify import _enqueue
    from .needs import card

    now = now or datetime.now(timezone.utc)
    since = now - LOOKBACK
    ids = route_ids(connection, project_id)
    cps = _contact_points(connection, project_id)
    name = connection.execute(text("SELECT name FROM projects WHERE id = :p"),
                              {"p": project_id}).scalar() or project_id
    out = {"needs_you": 0, "pr_ready": 0}
    targets = {k: [i for i in ids[k] if i in cps and cps[i]["kind"] != "console"]
               for k in ("needs_you", "pr_ready")}
    if targets["needs_you"]:
        for d in waiting_on_operator(connection, project_id):
            created = d.get("created_at")
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and created < since:
                continue
            words = card(connection, project_id, d)
            for cp in targets["needs_you"]:
                if _enqueue(connection, project_id, cp_id=cp, group_key=f"needs:{d['id']}",
                            kind="digest", urgency="low", alert_ids=[], incident_id=None,
                            subject=f"{name}: {words['label']} needs you",
                            body=f"{words['detail']}\n\n{words['action']['label']} in BOSS.",
                            dedupe=f"needs_you:{d['id']}:{cp}", now=now,
                            payload={"event": "needs_you", "decision_id": d["id"]}):
                    out["needs_you"] += 1
    if targets["pr_ready"]:
        for r in connection.execute(text(
                "SELECT DISTINCT ON (pull_request_url) id, title, pull_request_url, "
                "pull_request_number FROM code_task_runs WHERE project_id = :p "
                "AND COALESCE(pull_request_url, '') <> '' AND created_at >= :s "
                "ORDER BY pull_request_url, created_at"),
                {"p": project_id, "s": since}).mappings():
            from .titles import short_title

            label = f"PR #{r['pull_request_number']}" if r["pull_request_number"] else "A pull request"
            for cp in targets["pr_ready"]:
                if _enqueue(connection, project_id, cp_id=cp, group_key=f"pr:{r['pull_request_url']}",
                            kind="digest", urgency="low", alert_ids=[], incident_id=None,
                            subject=f"{name}: {label} is ready — {short_title(r['title'])}",
                            body=f"{label} is open and ready for review: {r['pull_request_url']}",
                            dedupe=f"pr_ready:{r['pull_request_url']}:{cp}", now=now,
                            payload={"event": "pr_ready", "code_task_run_id": r["id"]}):
                    out["pr_ready"] += 1
    return out


def plan_all(engine: Any, settings: Any, now: Optional[datetime] = None) -> dict[str, int]:
    """The tick: every project with a routed (non-console) Needs-you or PR-ready channel."""
    from ...db import begin

    with begin(engine) as c:
        pids = c.execute(text(
            "SELECT DISTINCT project_id FROM project_notification_routes "
            "WHERE jsonb_array_length(contact_point_ids) > 0")).scalars().all()
    total = {"needs_you": 0, "pr_ready": 0}
    for pid in pids:
        with begin(engine) as c:
            got = plan(c, settings, str(pid), now)
        for k in total:
            total[k] += got[k]
    return total


__all__ = ["EVENTS", "plan", "plan_all", "route_ids", "set_route", "view"]
