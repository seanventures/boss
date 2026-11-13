"""THE PERSONAL ATTENTION INBOX, RANKED BY BLOCKED WORK (BOSSAPP-875).

WHY. Decisions and briefings were project-wide: with many agents running, every person saw every
question in creation order, with no "read", no "later", and nothing saying which answer would
unblock the most work. The person is the bottleneck; their queue should put first what holds up
the most.

ONE FEED. The ``inbox_items`` view (db/schema.sql) is every open thing a person may act on: a
pending decision (its ``targets`` are the delegate and the stage participants; none means whoever
may answer), a work item someone answers for that is to do / in progress / blocked / in review,
and a recently failed run of such an item. :func:`inbox` reads it for ONE person and ranks it:

* deadline — overdue first, then due within a day, within three days;
* risk — the decision's risk tier, or the item's priority band;
* blocked work — how many open items wait, directly or transitively, on the entry's work item
  (``work_item_relations`` kind ``blocks``): answering this unblocks that many;
* aimed at you — an entry naming you outranks one for whoever may answer.

``mine`` keeps what names the person plus what names nobody; without it the whole project's feed
is ranked the same way. Read state, snoozes and archives are the person's own
(``inbox_read_state``, ``inbox_snoozes``); a snooze lapses on its own.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event

KINDS = ("decision", "work_item", "failed_run")
MAX_SNOOZE_DAYS = 90


class InboxError(ValueError):
    """Something the person can fix (HTTP 422)."""


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def person_key(actor: str) -> str:
    """The one key a person's read state and snoozes are stored under."""
    raw = str(actor or "").strip()
    if not raw:
        raise InboxError("the inbox is a signed-in person's")
    return raw if ":" in raw else f"person:{raw}"


def blocked_counts(connection: Connection, item_ids: Iterable[str]) -> dict[str, int]:
    """For each item, how many OPEN items wait on it, directly or through a chain of blocks."""
    ids = sorted({i for i in item_ids if i})
    if not ids:
        return {}
    rows = connection.execute(text(
        "WITH RECURSIVE down(root, id, depth) AS ("
        "  SELECT r.from_id, r.to_id, 1 FROM work_item_relations r "
        "  WHERE r.kind = 'blocks' AND r.from_id = ANY(:ids) "
        "  UNION SELECT down.root, r.to_id, down.depth + 1 FROM work_item_relations r "
        "  JOIN down ON r.from_id = down.id WHERE r.kind = 'blocks' AND down.depth < 25) "
        "SELECT down.root, count(DISTINCT down.id) AS n FROM down "
        "JOIN work_items w ON w.id = down.id WHERE w.status NOT IN ('done', 'cancelled') "
        "GROUP BY down.root"), {"ids": ids}).mappings()
    return {r["root"]: int(r["n"]) for r in rows}


def _deadline_points(deadline: Optional[datetime], now: datetime) -> tuple[int, Optional[str]]:
    if deadline is None:
        return 0, None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    left = deadline - now
    if left <= timedelta(0):
        return 40, "overdue"
    if left <= timedelta(days=1):
        return 30, "due today"
    if left <= timedelta(days=3):
        return 15, "due soon"
    return 0, None


def rank(entry: dict[str, Any], *, blocked: int, mine: bool, now: datetime) -> tuple[int, list[str]]:
    """The score and the few words saying why. Pure (tested on its own)."""
    why: list[str] = []
    points, said = _deadline_points(entry.get("deadline_at"), now)
    if said:
        why.append(said)
    risk = int(entry.get("risk_tier") or 0)
    points += 12 * max(0, min(risk, 3))
    if risk >= 2:
        why.append("high risk" if risk >= 3 else "risk")
    if blocked:
        points += 8 * min(blocked, 10)
        why.append(f"blocks {blocked}")
    if mine:
        points += 10
        why.append("for you")
    if entry.get("kind") == "failed_run":
        why.append("failed")
    return points, why


def inbox(connection: Connection, project_id: str, *, actor: str, mine: bool = True,
          include_snoozed: bool = False, include_archived: bool = False,
          kinds: Optional[Iterable[str]] = None, limit: int = 100,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """One person's ranked feed. Hidden work (BOSSAPP-876) is filtered by the item's own read."""
    from boss_engine.identity.authz import viewer_parties

    now = now or datetime.now(timezone.utc)
    who = person_key(actor)
    parties = set(viewer_parties(connection, actor)) | {who}
    wanted = [k for k in (kinds or KINDS) if k in KINDS]
    rows = [dict(r) for r in connection.execute(text(
        "SELECT i.*, rs.read_at, sn.kind AS snooze_kind, sn.until AS snoozed_until "
        "FROM inbox_items i "
        "LEFT JOIN inbox_read_state rs ON rs.project_id = i.project_id AND rs.person = :who "
        "  AND rs.item_key = i.item_key "
        "LEFT JOIN inbox_snoozes sn ON sn.project_id = i.project_id AND sn.person = :who "
        "  AND sn.item_key = i.item_key "
        "WHERE i.project_id = :p AND i.kind = ANY(:kinds)"),
        {"p": project_id, "who": who, "kinds": wanted}).mappings()]
    rows = _visible(connection, project_id, rows)
    counts = blocked_counts(connection, (r.get("work_item_id") for r in rows))
    out, snoozed, archived = [], 0, 0
    for r in rows:
        targets = [str(t) for t in (_json(r.get("targets"), []) or []) if t]
        named = bool(parties & ({*targets} | {f"person:{t}" for t in targets if ":" not in t}))
        if mine and targets and not named:
            continue
        is_archived = r.get("snooze_kind") == "archive"
        until = r.get("snoozed_until")
        is_snoozed = r.get("snooze_kind") == "snooze" and until is not None and until > now
        archived += is_archived
        snoozed += is_snoozed
        if (is_archived and not include_archived) or (is_snoozed and not include_snoozed):
            continue
        blocked = counts.get(r.get("work_item_id") or "", 0)
        score, why = rank(r, blocked=blocked, mine=named, now=now)
        out.append({
            "item_key": r["item_key"], "kind": r["kind"], "ref_id": r["ref_id"], "title": r["title"],
            "work_item_id": r.get("work_item_id"), "targets": targets, "for_you": named,
            "risk_tier": int(r.get("risk_tier") or 0), "deadline_at": _iso(r.get("deadline_at")),
            "created_at": _iso(r.get("created_at")), "blocked_count": blocked, "score": score,
            "why": why, "read": r.get("read_at") is not None,
            "snoozed_until": _iso(until) if is_snoozed else None, "archived": is_archived,
        })
    out.sort(key=lambda e: (-e["score"], e["read"], e["created_at"] or ""))
    return {"items": out[:max(1, min(int(limit), 500))], "count": len(out),
            "unread": sum(1 for e in out if not e["read"]), "snoozed": snoozed,
            "archived": archived, "person": who}


def _visible(connection: Connection, project_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entries whose work item the viewer may not see (BOSSAPP-876's predicate, when the
    connection carries a viewer). Before 876 there is nothing hidden."""
    from . import work_items

    hidden = getattr(work_items, "hidden_ids", None)
    if hidden is None:
        return rows
    gone = hidden(connection, project_id, [r.get("work_item_id") for r in rows])
    return [r for r in rows if r.get("work_item_id") not in gone]


def mark_read(connection: Connection, project_id: str, *, actor: str, item_keys: Iterable[str],
              read: bool = True) -> dict[str, Any]:
    who = person_key(actor)
    keys = sorted({str(k) for k in item_keys if k})[:500]
    if not keys:
        raise InboxError("name the inbox items to mark")
    if read:
        for key in keys:
            connection.execute(text(
                "INSERT INTO inbox_read_state (project_id, person, item_key) VALUES (:p, :w, :k) "
                "ON CONFLICT (project_id, person, item_key) DO UPDATE SET read_at = now()"),
                {"p": project_id, "w": who, "k": key})
    else:
        connection.execute(text(
            "DELETE FROM inbox_read_state WHERE project_id = :p AND person = :w AND item_key = ANY(:k)"),
            {"p": project_id, "w": who, "k": keys})
    return {"marked": keys, "read": read}


def snooze(connection: Connection, project_id: str, *, actor: str, item_key: str,
           minutes: Optional[int] = None, archive: bool = False, clear: bool = False,
           now: Optional[datetime] = None) -> dict[str, Any]:
    """Snooze for `minutes`, archive (until unarchived), or clear either."""
    who = person_key(actor)
    key = str(item_key or "").strip()
    if not key or key.split(":", 1)[0] not in KINDS:
        raise InboxError("item_key is <kind>:<id> (decision, work_item, failed_run)")
    if clear:
        connection.execute(text(
            "DELETE FROM inbox_snoozes WHERE project_id = :p AND person = :w AND item_key = :k"),
            {"p": project_id, "w": who, "k": key})
        return {"item_key": key, "cleared": True}
    if archive:
        kind, until = "archive", None
    else:
        if not minutes or int(minutes) < 1 or int(minutes) > MAX_SNOOZE_DAYS * 24 * 60:
            raise InboxError(f"snooze for 1 minute to {MAX_SNOOZE_DAYS} days")
        kind, until = "snooze", (now or datetime.now(timezone.utc)) + timedelta(minutes=int(minutes))
    connection.execute(text(
        "INSERT INTO inbox_snoozes (project_id, person, item_key, kind, until) "
        "VALUES (:p, :w, :k, :kind, :u) ON CONFLICT (project_id, person, item_key) "
        "DO UPDATE SET kind = :kind, until = :u, created_at = now()"),
        {"p": project_id, "w": who, "k": key, "kind": kind, "u": until})
    emit_event(connection, project_id, f"inbox.{kind}d", who, "inbox_item", key,
               {"until": _iso(until)})
    return {"item_key": key, "kind": kind, "until": _iso(until)}


__all__ = ["inbox", "rank", "blocked_counts", "mark_read", "snooze", "person_key", "InboxError"]
