"""A PERSON'S WATCHDOG ON A WORK TREE (BOSSAPP-880).

BOSS's supervisors recover FAILURES and STALLS of runs (run_supervisor, shepherd_watch,
stuck_work). Nothing re-checked work that ended quietly: a subtree where nothing is in flight
any more but items are still open (a false "done", a fake blocker), an epic closed while its
children were not, an item marked blocked and forgotten.

A person attaches a watchdog to a work item (the root of a subtree) with the conditions they
care about:

* ``stopped_unfinished`` — nothing in the subtree is active or in review, yet items are open;
* ``blocked``            — an item in the subtree is blocked;
* ``closed``             — the root was closed (done or cancelled): re-verify it;
* ``stalled``            — open items, and nothing in the subtree changed for ``stall_hours``.

THE SWEEP (``sweep``, on the monitor tick beside the other sweeps) evaluates each enabled
watchdog and computes a FINGERPRINT of what matched (the conditions and the items with their
status). It fires ONCE per fingerprint — a ``work_item.watchdog_fired`` event (the activity log
and the live stream) and one message per contact point through the notify outbox (the
``notifications`` table, retried and receipted like every other message). The same stop is never
told twice; when the subtree moves on (nothing matches), the fingerprint is cleared, so the next
stop is told again. Where it tells: the contact points the watcher chose, else the project's
Needs-you route (Project › Settings › Notifications), which falls back to the console.

The watcher's ``instructions`` ride on the message ("check the tests really ran"), for whoever
reads it. The watchdog wakes no agent by itself: re-opening work stays a person's act.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger(__name__)

CONDITIONS = ("stopped_unfinished", "blocked", "closed", "stalled")
DEFAULT_CONDITIONS = ("stopped_unfinished", "blocked")
IN_FLIGHT = ("active", "in_review")
CLOSED = ("done", "cancelled")
MAX_PER_TICK = 200


class WatchdogError(ValueError):
    """A watchdog BOSS refuses (unknown condition, a contact point not in this project...)."""


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "project_id": row["project_id"], "root_item_id": row["root_item_id"],
            "watcher": row["watcher"], "conditions": _json(row.get("conditions"), []),
            "stall_hours": row.get("stall_hours"), "instructions": row.get("instructions") or "",
            "contact_point_ids": _json(row.get("contact_point_ids"), []),
            "enabled": bool(row.get("enabled")), "fired_count": int(row.get("fired_count") or 0),
            "last_fired_at": _iso(row.get("last_fired_at")),
            "last_fired": _json(row.get("last_fired"), None),
            "created_at": _iso(row.get("created_at"))}


def _conditions(raw: Optional[Iterable[str]]) -> list[str]:
    picked = [str(c).strip() for c in (raw if raw is not None else DEFAULT_CONDITIONS)]
    bad = sorted(set(picked) - set(CONDITIONS))
    if bad:
        raise WatchdogError(f"conditions must be among {', '.join(CONDITIONS)} (not {bad[0]})")
    if not picked:
        raise WatchdogError("watch for at least one condition")
    return [c for c in CONDITIONS if c in picked]


def attach(connection: Connection, *, project_id: str, root_item_id: str, watcher: str,
           conditions: Optional[Iterable[str]] = None, stall_hours: int = 24,
           instructions: str = "", contact_point_ids: Iterable[str] = (),
           enabled: bool = True) -> dict[str, Any]:
    """Attach (or update) the watcher's watchdog on this item's subtree. One per watcher per
    root: attaching again replaces the conditions and re-arms it."""
    if connection.execute(text("SELECT 1 FROM work_items WHERE id = :i AND project_id = :p"),
                          {"i": root_item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {root_item_id} in this project")
    conds = _conditions(conditions)
    hours = int(stall_hours or 24)
    if hours < 1 or hours > 24 * 30:
        raise WatchdogError("stall_hours is between 1 and 720")
    cps = [str(c) for c in contact_point_ids or []]
    if cps:
        known = set(connection.execute(text(
            "SELECT id FROM contact_points WHERE project_id = :p AND id = ANY(:ids)"),
            {"p": project_id, "ids": cps}).scalars())
        missing = [c for c in cps if c not in known]
        if missing:
            raise WatchdogError(f"no contact point {missing[0]} in this project")
    row = connection.execute(text(
        "INSERT INTO work_item_watchdogs (id, project_id, root_item_id, watcher, conditions, "
        " stall_hours, instructions, contact_point_ids, enabled) VALUES (:i, :p, :r, :w, "
        " CAST(:c AS JSONB), :h, :ins, CAST(:cp AS JSONB), :en) "
        "ON CONFLICT (root_item_id, watcher) DO UPDATE SET conditions = EXCLUDED.conditions, "
        " stall_hours = EXCLUDED.stall_hours, instructions = EXCLUDED.instructions, "
        " contact_point_ids = EXCLUDED.contact_point_ids, enabled = EXCLUDED.enabled, "
        " last_fingerprint = NULL, updated_at = now() RETURNING *"),
        {"i": make_id("wdg"), "p": project_id, "r": root_item_id, "w": watcher,
         "c": json.dumps(conds), "h": hours, "ins": str(instructions or "")[:2000],
         "cp": json.dumps(cps), "en": bool(enabled)}).mappings().one()
    emit_event(connection, project_id, "work_item.watchdog_attached", watcher, "work_item",
               root_item_id, {"watchdog_id": row["id"], "conditions": conds})
    return view(row)


def detach(connection: Connection, *, project_id: str, watchdog_id: str, actor: str) -> None:
    row = connection.execute(text(
        "DELETE FROM work_item_watchdogs WHERE id = :i AND project_id = :p RETURNING root_item_id"),
        {"i": watchdog_id, "p": project_id}).first()
    if row is None:
        raise LookupError(f"no watchdog {watchdog_id} in this project")
    emit_event(connection, project_id, "work_item.watchdog_detached", actor, "work_item", row[0],
               {"watchdog_id": watchdog_id})


def of_item(connection: Connection, project_id: str, item_id: str) -> list[dict[str, Any]]:
    return [view(r) for r in connection.execute(text(
        "SELECT * FROM work_item_watchdogs WHERE project_id = :p AND root_item_id = :i "
        "ORDER BY created_at, watcher"), {"p": project_id, "i": item_id}).mappings()]


# ── evaluating ───────────────────────────────────────────────────────────────────────────────

def subtree(connection: Connection, root_item_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "WITH RECURSIVE down(id) AS (SELECT CAST(:r AS TEXT) "
        "  UNION SELECT w.id FROM work_items w JOIN down ON w.parent_id = down.id) "
        "SELECT w.id, w.human_key, w.title, w.status, w.parent_id, w.updated_at "
        "FROM work_items w JOIN down ON down.id = w.id ORDER BY w.created_at, w.id"),
        {"r": root_item_id}).mappings()]


def evaluate(items: list[dict[str, Any]], root_item_id: str, conditions: Iterable[str], *,
             stall_hours: int, now: datetime) -> list[dict[str, Any]]:
    """What matched, as [{condition, items: [{id, human_key, status}]}] — pure, for the tests."""
    wanted = set(conditions)
    open_items = [i for i in items if i["status"] not in CLOSED]
    root = next((i for i in items if i["id"] == root_item_id), None)
    brief = lambda rows: [{"id": r["id"], "human_key": r.get("human_key"),  # noqa: E731
                           "status": r["status"]} for r in rows]
    hits: list[dict[str, Any]] = []
    if "stopped_unfinished" in wanted and open_items \
            and not any(i["status"] in IN_FLIGHT for i in items):
        hits.append({"condition": "stopped_unfinished", "items": brief(open_items)})
    if "blocked" in wanted:
        blocked = [i for i in items if i["status"] == "blocked"]
        if blocked:
            hits.append({"condition": "blocked", "items": brief(blocked)})
    if "closed" in wanted and root is not None and root["status"] in CLOSED:
        hits.append({"condition": "closed", "items": brief([root])})
    if "stalled" in wanted and open_items:
        latest = max((i["updated_at"] for i in items if i.get("updated_at")), default=None)
        if latest is not None:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            if now - latest >= timedelta(hours=int(stall_hours or 24)):
                hits.append({"condition": "stalled", "items": brief(open_items)})
    return hits


def fingerprint(hits: list[dict[str, Any]]) -> str:
    canon = json.dumps([[h["condition"], sorted((i["id"], i["status"]) for i in h["items"])]
                        for h in hits], sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


_WORDS = {"stopped_unfinished": "stopped with work still open",
          "blocked": "has blocked work", "closed": "was closed — check it really is done",
          "stalled": "has not moved"}


def _message(root: Mapping[str, Any], hits: list[dict], instructions: str,
             stall_hours: int) -> tuple[str, str]:
    name = f"{root.get('human_key') or root['id']} · {root.get('title') or ''}".strip(" ·")
    lines = []
    for h in hits:
        what = _WORDS[h["condition"]]
        if h["condition"] == "stalled":
            what = f"has not moved for {stall_hours} h"
        keys = ", ".join(str(i.get("human_key") or i["id"]) for i in h["items"][:8])
        lines.append(f"- {what}: {keys}")
    subject = f"Watchdog: {name} {_WORDS[hits[0]['condition']]}"[:200]
    body = "\n".join(lines) + (f"\n\nYour instructions: {instructions}" if instructions else "")
    return subject, body


def _targets(connection: Connection, project_id: str, row: Mapping[str, Any]) -> list[str]:
    chosen = [str(c) for c in _json(row.get("contact_point_ids"), []) or []]
    if chosen:
        return chosen
    from .project_experience.notify_routes import route_ids

    return route_ids(connection, project_id)["needs_you"]


def check(connection: Connection, row: Mapping[str, Any], *, now: datetime) -> Optional[dict]:
    """Evaluate one watchdog; fire once for a new fingerprint. Returns what fired, or None."""
    from .notify import _enqueue

    items = subtree(connection, row["root_item_id"])
    if not items:
        return None
    hits = evaluate(items, row["root_item_id"], _json(row.get("conditions"), []),
                    stall_hours=int(row.get("stall_hours") or 24), now=now)
    if not hits:
        if row.get("last_fingerprint"):
            connection.execute(text(
                "UPDATE work_item_watchdogs SET last_fingerprint = NULL, updated_at = now() "
                "WHERE id = :i"), {"i": row["id"]})
        return None
    fp = fingerprint(hits)
    if fp == row.get("last_fingerprint"):
        return None
    project_id = str(row["project_id"])
    root = next(i for i in items if i["id"] == row["root_item_id"])
    subject, body = _message(root, hits, row.get("instructions") or "",
                             int(row.get("stall_hours") or 24))
    sent = 0
    for cp in _targets(connection, project_id, row):
        if _enqueue(connection, project_id, cp_id=cp, group_key=f"watchdog:{row['id']}",
                    kind="digest", urgency="low", alert_ids=[], incident_id=None,
                    subject=subject, body=body, dedupe=f"watchdog:{row['id']}:{fp}:{cp}",
                    now=now, payload={"event": "watchdog", "watchdog_id": row["id"],
                                      "work_item_id": row["root_item_id"],
                                      "conditions": [h["condition"] for h in hits]}):
            sent += 1
    fired = {"conditions": [h["condition"] for h in hits], "hits": hits, "fingerprint": fp,
             "messages": sent}
    connection.execute(text(
        "UPDATE work_item_watchdogs SET last_fingerprint = :fp, last_fired_at = :now, "
        "last_fired = CAST(:f AS JSONB), fired_count = fired_count + 1, updated_at = now() "
        "WHERE id = :i"), {"fp": fp, "now": now, "f": json.dumps(fired), "i": row["id"]})
    emit_event(connection, project_id, "work_item.watchdog_fired", "boss", "work_item",
               row["root_item_id"], {"watchdog_id": row["id"], "watcher": row["watcher"],
                                     "conditions": fired["conditions"], "messages": sent,
                                     "subject": subject})
    return fired


def sweep(engine: Any, settings: Any = None, *, now: Optional[datetime] = None) -> dict[str, int]:
    """The tick: every enabled watchdog of a live project, each in its own transaction."""
    from ..db import begin
    from .projects_archive import live_sql

    now = now or datetime.now(timezone.utc)
    with begin(engine) as c:
        ids = list(c.execute(text(
            "SELECT id FROM work_item_watchdogs WHERE enabled AND "
            + live_sql("work_item_watchdogs.project_id") + " ORDER BY updated_at LIMIT :n"),
            {"n": MAX_PER_TICK}).scalars())
    out = {"checked": 0, "fired": 0}
    for wid in ids:
        try:
            with begin(engine) as c:
                row = c.execute(text(
                    "SELECT * FROM work_item_watchdogs WHERE id = :i AND enabled FOR UPDATE "
                    "SKIP LOCKED"), {"i": wid}).mappings().first()
                if row is None:
                    continue
                out["checked"] += 1
                if check(c, dict(row), now=now):
                    out["fired"] += 1
        except Exception:  # noqa: BLE001 — one watchdog never stops the others
            log.exception("watchdog %s failed", wid)
    return out


__all__ = ["CONDITIONS", "WatchdogError", "attach", "check", "detach", "evaluate",
           "fingerprint", "of_item", "subtree", "sweep"]
