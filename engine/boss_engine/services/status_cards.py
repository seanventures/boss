"""STATUS CARDS AND STORED SUMMARIES (BOSSAPP-883).

BOSS's briefings were computed on demand (factory_briefing, command_center) and never kept:
"where is the checkout epic?" was answered again from scratch each time someone asked, and
what it said yesterday was gone.

A STATUS CARD is a standing question with a stored answer: a title, a scope (the whole project,
or one work item and everything under it), how often to refresh, and whether it is pinned. Its
answer is a DOCUMENT REVISION (``status_card_revisions``): markdown plus the facts it was made
from, numbered, kept. Revisions are written:

* on a schedule — the monitor tick refreshes each card whose ``next_refresh_at`` passed;
* on close — when a work item closes (work_items.mark_done, set_status cancelled), every card
  scoped to it or to one of its ancestors is refreshed, and the item gets its own CLOSING
  summary (a ``closing`` card: what was done, what is left, what it produced);
* on demand — Refresh.

A refresh whose document did not change writes no revision (the clock still moves on), so the
history is the history of change, not of ticks.

DETERMINISTIC. The document is built from BOSS's own rows — status counts, what is blocked or in
review, what closed since the last revision, the item's outputs (work_outputs), and for a
project-scope card the factory briefing's lines. No model writes it. ``prompt`` is kept on the
card (what the person wants to know, shown with it) for a model-written mode that is NOT built:
``mode`` accepts only ``deterministic``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger(__name__)

MODES = ("deterministic",)
KINDS = ("card", "closing")
CLOSED = ("done", "cancelled")
MAX_PER_TICK = 100


class CardError(ValueError):
    """A card BOSS refuses."""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


# ── the document ─────────────────────────────────────────────────────────────────────────────

def _subtree(connection: Connection, project_id: str, root: Optional[str]) -> list[dict]:
    if root:
        sql = ("WITH RECURSIVE down(id) AS (SELECT CAST(:r AS TEXT) UNION SELECT w.id FROM "
               "work_items w JOIN down ON w.parent_id = down.id) SELECT w.id, w.human_key, "
               "w.title, w.status, w.parent_id, w.resolved_at FROM work_items w JOIN down "
               "ON down.id = w.id WHERE w.project_id = :p ORDER BY w.created_at, w.id")
    else:
        sql = ("SELECT id, human_key, title, status, parent_id, resolved_at FROM work_items "
               "WHERE project_id = :p AND NOT COALESCE(ambient, false) "
               "ORDER BY created_at, id LIMIT 2000")
    return [dict(r) for r in connection.execute(text(sql), {"p": project_id, "r": root}).mappings()]


def _key(i: Mapping[str, Any]) -> str:
    return str(i.get("human_key") or i["id"])


def compose(connection: Connection, project_id: str, card: Mapping[str, Any],
            since: Optional[datetime] = None) -> tuple[str, dict[str, Any]]:
    """(markdown, facts) for a card, from BOSS's own rows. Pure reading."""
    from . import work_outputs

    scope = _json(card.get("scope"), {}) or {}
    root = scope.get("work_item_id")
    items = _subtree(connection, project_id, root)
    counts: dict[str, int] = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    blocked = [i for i in items if i["status"] == "blocked"]
    review = [i for i in items if i["status"] == "in_review"]
    open_items = [i for i in items if i["status"] not in CLOSED]
    closed_since = []
    if since is not None:
        for i in items:
            at = i.get("resolved_at")
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if i["status"] in CLOSED and at is not None and at > since:
                closed_since.append(i)
    lines: list[str] = []
    head = next((i for i in items if i["id"] == root), None) if root else None
    if root and head is None:
        lines.append("The work item this card watched no longer exists.")
    if head is not None:
        lines.append(f"**{_key(head)} · {head['title']}** is {head['status'].replace('_', ' ')}.")
    total = len(items)
    done = counts.get("done", 0)
    if total:
        lines.append(f"{done} of {total} done; {len(open_items)} open"
                     + (f", {counts.get('active', 0)} in progress" if counts.get("active") else "")
                     + ".")
    else:
        lines.append("No work items yet.")
    if blocked:
        lines.append("Blocked: " + ", ".join(f"{_key(i)} {i['title']}" for i in blocked[:8]) + ".")
    if review:
        lines.append("In review: " + ", ".join(_key(i) for i in review[:8]) + ".")
    if closed_since:
        lines.append("Closed since last time: " + ", ".join(_key(i) for i in closed_since[:8]) + ".")
    outputs: list[dict[str, Any]] = []
    if head is not None:
        try:
            with connection.begin_nested():
                outputs = work_outputs.outputs_of(connection, head["id"])
        except Exception:  # noqa: BLE001 — a card never fails for want of outputs
            outputs = []
        if outputs:
            lines.append("Outputs: " + "; ".join(
                f"{o['title']}" + (f" ({o['status']})" if o.get("status") else "")
                for o in outputs[:6]) + ".")
    briefing: list[str] = []
    if not root:
        from .factory_briefing import briefing_lines, factory_briefing

        try:
            with connection.begin_nested():
                briefing = briefing_lines(factory_briefing(connection, project_id))[:6]
        except Exception:  # noqa: BLE001
            briefing = []
        if briefing:
            lines.append("Needs a person:")
            lines.extend(f"- {b}" for b in briefing)
    facts = {"counts": counts, "total": total, "blocked": [_key(i) for i in blocked],
             "in_review": [_key(i) for i in review], "closed_since": [_key(i) for i in closed_since],
             "outputs": [{"kind": o["kind"], "title": o["title"], "status": o.get("status")}
                         for o in outputs[:6]],
             "needs_a_person": briefing, "root": root}
    title = str(card.get("title") or "Status")
    return f"### {title}\n\n" + "\n\n".join(lines) + "\n", facts


# ── cards ────────────────────────────────────────────────────────────────────────────────────

def _view(connection: Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    latest = connection.execute(text(
        "SELECT revision, document, facts, trigger, created_at FROM status_card_revisions "
        "WHERE card_id = :c ORDER BY revision DESC LIMIT 1"), {"c": row["id"]}).mappings().first()
    return {"id": row["id"], "project_id": row["project_id"], "title": row["title"],
            "kind": row["kind"], "scope": _json(row.get("scope"), {}),
            "prompt": row.get("prompt") or "", "mode": row["mode"],
            "refresh_minutes": int(row.get("refresh_minutes") or 0), "pinned": bool(row["pinned"]),
            "next_refresh_at": _iso(row.get("next_refresh_at")),
            "revision": int(latest["revision"]) if latest else 0,
            "document": latest["document"] if latest else "",
            "facts": _json(latest["facts"], {}) if latest else {},
            "trigger": latest["trigger"] if latest else None,
            "refreshed_at": _iso(latest["created_at"]) if latest else None,
            "created_by": row.get("created_by"), "created_at": _iso(row.get("created_at"))}


def create(connection: Connection, *, project_id: str, title: str, actor: str,
           work_item_id: Optional[str] = None, prompt: str = "", refresh_minutes: int = 60,
           pinned: bool = True, mode: str = "deterministic", kind: str = "card") -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise CardError("a card needs a title")
    if mode not in MODES:
        raise CardError("only deterministic cards are built: a model-written card is not "
                        "available on this install")
    minutes = int(refresh_minutes or 0)
    if minutes < 0 or minutes > 7 * 24 * 60:
        raise CardError("refresh_minutes is between 0 (on close and on demand only) and 10080")
    if work_item_id and connection.execute(text(
            "SELECT 1 FROM work_items WHERE id = :w AND project_id = :p"),
            {"w": work_item_id, "p": project_id}).first() is None:
        raise LookupError(f"no work item {work_item_id} in this project")
    row = connection.execute(text(
        "INSERT INTO status_cards (id, project_id, title, kind, scope, prompt, mode, "
        " refresh_minutes, pinned, created_by) VALUES (:i, :p, :t, :k, CAST(:s AS JSONB), :pr, "
        " :m, :r, :pin, :a) RETURNING *"),
        {"i": make_id("scard"), "p": project_id, "t": title[:200], "k": kind,
         "s": json.dumps({"work_item_id": work_item_id} if work_item_id else {}),
         "pr": str(prompt or "")[:2000], "m": mode, "r": minutes, "pin": bool(pinned),
         "a": actor}).mappings().one()
    emit_event(connection, project_id, "status_card.created", actor, "status_card", row["id"],
               {"work_item_id": work_item_id, "kind": kind})
    refresh(connection, project_id=project_id, card_id=row["id"], trigger="created", actor=actor)
    return get(connection, project_id, row["id"])


def get(connection: Connection, project_id: str, card_id: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM status_cards WHERE id = :i AND project_id = :p"),
        {"i": card_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no status card {card_id} in this project")
    return _view(connection, row)


def list_cards(connection: Connection, project_id: str, *, work_item_id: Optional[str] = None,
               pinned_only: bool = False) -> list[dict[str, Any]]:
    rows = connection.execute(text(
        "SELECT * FROM status_cards WHERE project_id = :p "
        "AND (CAST(:w AS TEXT) IS NULL OR scope->>'work_item_id' = :w) "
        "AND (NOT :pinned OR pinned) ORDER BY pinned DESC, created_at, id"),
        {"p": project_id, "w": work_item_id, "pinned": bool(pinned_only)}).mappings().all()
    return [_view(connection, r) for r in rows]


def revisions(connection: Connection, project_id: str, card_id: str) -> list[dict[str, Any]]:
    get(connection, project_id, card_id)
    return [{**dict(r), "facts": _json(r["facts"], {}), "created_at": _iso(r["created_at"])}
            for r in connection.execute(text(
                "SELECT revision, document, facts, trigger, created_at FROM status_card_revisions "
                "WHERE card_id = :c ORDER BY revision DESC LIMIT 50"), {"c": card_id}).mappings()]


def delete(connection: Connection, *, project_id: str, card_id: str, actor: str) -> None:
    if connection.execute(text(
            "DELETE FROM status_cards WHERE id = :i AND project_id = :p RETURNING id"),
            {"i": card_id, "p": project_id}).first() is None:
        raise LookupError(f"no status card {card_id} in this project")
    emit_event(connection, project_id, "status_card.deleted", actor, "status_card", card_id, {})


def refresh(connection: Connection, *, project_id: str, card_id: str, trigger: str = "manual",
            actor: str = "boss", now: Optional[datetime] = None) -> Optional[int]:
    """Recompose the card; write a revision only when the document changed. Returns the new
    revision number, or None when nothing changed."""
    now = now or datetime.now(timezone.utc)
    card = connection.execute(text(
        "SELECT * FROM status_cards WHERE id = :i AND project_id = :p FOR UPDATE"),
        {"i": card_id, "p": project_id}).mappings().first()
    if card is None:
        raise LookupError(f"no status card {card_id} in this project")
    last = connection.execute(text(
        "SELECT revision, document, created_at FROM status_card_revisions WHERE card_id = :c "
        "ORDER BY revision DESC LIMIT 1"), {"c": card_id}).mappings().first()
    since = last["created_at"] if last else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    document, facts = compose(connection, project_id, card, since)
    minutes = int(card.get("refresh_minutes") or 0)
    connection.execute(text(
        "UPDATE status_cards SET next_refresh_at = :n, updated_at = now() WHERE id = :i"),
        {"n": now + timedelta(minutes=minutes) if minutes else None, "i": card_id})
    if last is not None and _digest(last["document"]) == _digest(_without_since(document)) \
            and not facts["closed_since"]:
        return None
    revision = int(last["revision"]) + 1 if last else 1
    connection.execute(text(
        "INSERT INTO status_card_revisions (card_id, revision, project_id, document, facts, "
        " trigger, created_by) VALUES (:c, :r, :p, :d, CAST(:f AS JSONB), :t, :a)"),
        {"c": card_id, "r": revision, "p": project_id, "d": document,
         "f": json.dumps(facts, default=str), "t": trigger, "a": actor})
    emit_event(connection, project_id, "status_card.refreshed", actor, "status_card", card_id,
               {"revision": revision, "trigger": trigger})
    return revision


def _without_since(document: str) -> str:
    return "\n\n".join(p for p in document.split("\n\n")
                       if not p.startswith("Closed since last time:"))


def _digest(document: Any) -> str:
    return hashlib.sha256(_without_since(str(document or "")).encode()).hexdigest()


# ── when an item closes, and the tick ───────────────────────────────────────────────────────

def on_item_closed(connection: Connection, *, project_id: str, item_id: str,
                   actor: str = "boss") -> dict[str, Any]:
    """Refresh every card scoped to the item or an ancestor, and keep the item's own closing
    summary (one ``closing`` card per item, refreshed if it closes again)."""
    chain = list(connection.execute(text(
        "WITH RECURSIVE up(id, parent_id) AS (SELECT id, parent_id FROM work_items WHERE id = :i "
        "  UNION SELECT w.id, w.parent_id FROM work_items w JOIN up ON w.id = up.parent_id) "
        "SELECT id FROM up"), {"i": item_id}).scalars())
    refreshed = 0
    for cid in connection.execute(text(
            "SELECT id FROM status_cards WHERE project_id = :p AND kind = 'card' "
            "AND scope->>'work_item_id' = ANY(:chain)"), {"p": project_id, "chain": chain}).scalars():
        if refresh(connection, project_id=project_id, card_id=cid, trigger="close", actor=actor):
            refreshed += 1
    closing = connection.execute(text(
        "SELECT id FROM status_cards WHERE project_id = :p AND kind = 'closing' "
        "AND scope->>'work_item_id' = :i"), {"p": project_id, "i": item_id}).scalar()
    if closing:
        refresh(connection, project_id=project_id, card_id=closing, trigger="close", actor=actor)
    else:
        title = connection.execute(text("SELECT COALESCE(human_key || ' · ', '') || title "
                                        "FROM work_items WHERE id = :i"), {"i": item_id}).scalar()
        closing = create(connection, project_id=project_id, title=f"Closing summary: {title}",
                         actor=actor, work_item_id=item_id, refresh_minutes=0, pinned=False,
                         kind="closing")["id"]
    return {"refreshed": refreshed, "closing_card_id": closing}


def sweep(engine: Any, settings: Any = None, *, now: Optional[datetime] = None) -> dict[str, int]:
    """The tick: cards whose refresh time passed, each in its own transaction."""
    from ..db import begin
    from .projects_archive import live_sql

    now = now or datetime.now(timezone.utc)
    with begin(engine) as c:
        due = [dict(r) for r in c.execute(text(
            "SELECT id, project_id FROM status_cards WHERE refresh_minutes > 0 AND "
            "next_refresh_at IS NOT NULL AND next_refresh_at <= :now AND "
            + live_sql("status_cards.project_id") + " ORDER BY next_refresh_at LIMIT :n"),
            {"now": now, "n": MAX_PER_TICK}).mappings()]
    out = {"due": len(due), "revised": 0}
    for card in due:
        try:
            with begin(engine) as c:
                if refresh(c, project_id=card["project_id"], card_id=card["id"],
                           trigger="schedule", now=now):
                    out["revised"] += 1
        except Exception:  # noqa: BLE001 — one card never stops the others
            log.exception("status card %s not refreshed", card["id"])
    return out


__all__ = ["CardError", "compose", "create", "delete", "get", "list_cards", "on_item_closed",
           "refresh", "revisions", "sweep"]
