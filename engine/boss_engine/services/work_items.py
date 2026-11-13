"""ONE CANONICAL WORK ITEM (BOSSAPP-830): the node every piece of work hangs off.

BOSS kept work in five shapes — tasks, work_items, workflow_runs, code_task_runs and the
planning tables — so "what are we working on, what is it waiting for, who owns it" had five
answers. The existing ``work_items`` table (the scored portfolio; the ambient board) becomes the
one node. Everything else POINTS at it:

* the tree — ``parent_id`` (epic → story → task), ``goal_id`` (goals form their own tree);
* who — ``assignee`` (``agent:<role>`` or ``person:<id>``) and ``responsible_person`` (who answers
  for it, and to whom a review that keeps failing escalates — execution_policy, BOSSAPP-831);
* a human key (``WI-12``), labels, and relations (``blocks`` stored once; ``blocked_by`` is the
  same row read the other way, so the two never disagree);
* every run, PRD meeting and planning ticket carries ``work_item_id``. The node is made LAZILY
  where the run is made (:func:`link_run`, called from each creation door), never by a backfill
  migration: a run started for an item joins it; a child run joins its parent's item; anything
  else gets its own item.

STATUS keeps the portfolio's ``active``/``done`` (evaluate_admission, release planning and the
ambient board already read them) and adds ``backlog | todo | blocked | in_review | cancelled``.
``done`` is never written directly: :func:`complete_item` asks the execution policy first
(a stage may hold it at ``in_review``), and only a real done wakes what it was blocking
(:func:`wake_unblocked`): an item whose blockers are now all closed goes back to ``todo`` and its
paused runs resume / its deferred start launches.
"""
from __future__ import annotations

import json
import logging
import re
import weakref
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

log = logging.getLogger(__name__)

STATUSES = ("backlog", "todo", "active", "blocked", "in_review", "done", "cancelled")
CLOSED = ("done", "cancelled")
#: Statuses a person may set by hand. `done` goes through complete_item (the policy decides);
#: `in_review` is the policy's alone.
SETTABLE = ("backlog", "todo", "active", "blocked", "cancelled")
RELATION_KINDS = ("blocks", "blocked_by", "relates_to", "duplicates")
#: The run kinds that carry a work_item_id, and the table each lives in.
RUN_TABLES = {
    "workflow_run": "workflow_runs",
    "code_task": "code_task_runs",
    "meeting": "meeting_runs",
    "roadmap_item": "wp_roadmap_items",
}
_PARTY = re.compile(r"^(agent|person):[A-Za-z0-9_.@:+-]{1,200}$")
EDITABLE = ("title", "description", "parent_id", "goal_id", "assignee", "responsible_person",
            "status", "execution_policy", "due_at", "visibility")


class WorkItemError(ValueError):
    """A request the graph refuses (bad parent, a cycle, an unknown status...)."""


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ── who is reading (BOSSAPP-876: private items) ──────────────────────────────────────────────
# A route says who reads on its connection (view_as); BOSS's own reads say nothing and see
# everything. Every read below applies identity/authz.work_item_visible_sql with these params.
# Kept per Connection OBJECT (weakly), never in `connection.info`: that dict follows the pooled
# DBAPI connection into the next request, which would read as this viewer.
_VIEWERS: "weakref.WeakKeyDictionary[Any, list[str]]" = weakref.WeakKeyDictionary()
PRIVATE_TITLE = "Private item"


def view_as(connection: Connection, actor: Optional[str]) -> list[str]:
    """Read the work graph on this connection as `actor` (no actor: only unrestricted items)."""
    from boss_engine.identity.authz import viewer_parties

    parties = viewer_parties(connection, actor)
    _VIEWERS[connection] = parties
    return parties


def viewing(connection: Connection) -> Optional[list[str]]:
    """The viewer's parties, or None when BOSS itself is reading (everything is visible)."""
    return _VIEWERS.get(connection)


def visible_params(connection: Connection) -> dict[str, Any]:
    parties = viewing(connection)
    return {"wv_all": parties is None, "wv_parties": list(parties or [])}


def visible_sql(connection: Connection, item_col: str) -> str:  # noqa: ARG001 — one call shape
    from boss_engine.identity.authz import work_item_visible_sql

    return work_item_visible_sql(item_col)


def hidden_ids(connection: Connection, project_id: str, ids: Iterable[Optional[str]]) -> set[str]:
    """Which of these work items the viewer may NOT read."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted or viewing(connection) is None:
        return set()
    from boss_engine.identity.authz import work_item_visible_sql

    return set(connection.execute(text(
        "SELECT w.id FROM work_items w WHERE w.id = ANY(:ids) AND NOT "
        + work_item_visible_sql("w.id")), {"ids": wanted, **visible_params(connection)}).scalars())


def _row(connection: Connection, project_id: str, item_id: str, *,
         for_update: bool = False) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM work_items WHERE id = :id AND project_id = :p"
        + (" FOR UPDATE" if for_update else "")), {"id": item_id, "p": project_id}).mappings().first()
    # A private item the viewer may not read answers exactly like one that does not exist.
    if row is None or (viewing(connection) is not None
                       and hidden_ids(connection, project_id, [item_id])):
        raise LookupError(f"no work item {item_id} in this project")
    return dict(row)


def _brief(row: Mapping[str, Any], hidden: Iterable[str] = ()) -> dict[str, Any]:
    if row["id"] in hidden:
        # The edge stays (something private blocks this), never what it is.
        return {"id": row["id"], "human_key": None, "title": PRIVATE_TITLE, "status": row["status"],
                "private": True}
    return {"id": row["id"], "human_key": row.get("human_key"), "title": row["title"],
            "status": row["status"]}


def open_blockers(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    """The items that block this one and are not closed yet."""
    rows = connection.execute(text(
        "SELECT w.id, w.project_id, w.human_key, w.title, w.status FROM work_item_relations r "
        "JOIN work_items w ON w.id = r.from_id "
        "WHERE r.to_id = :id AND r.kind = 'blocks' AND w.status <> ALL(:closed) "
        "ORDER BY w.created_at"), {"id": item_id, "closed": list(CLOSED)}).mappings().all()
    hidden = hidden_ids(connection, "", [r["id"] for r in rows])
    return [_brief(r, hidden) for r in rows]


def serialize(connection: Connection, row: Mapping[str, Any], *,
              detail: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"], "project_id": row["project_id"], "human_key": row.get("human_key"),
        "title": row["title"], "description": row.get("description") or "",
        "status": row["status"], "parent_id": row.get("parent_id"),
        "goal_id": row.get("goal_id"), "assignee": row.get("assignee"),
        "responsible_person": row.get("responsible_person"),
        "subject_type": row["subject_type"], "subject_id": row["subject_id"],
        "priority_band": row.get("priority_band"),
        "execution_policy": _json(row.get("execution_policy"), None),
        "execution_state": _json(row.get("execution_state"), None),
        "due_at": _iso(row.get("due_at")), "created_by": row.get("created_by"),
        "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "visibility": row.get("visibility") or "project",
    }
    item["labels"] = [dict(r) for r in connection.execute(text(
        "SELECT l.name, l.color FROM work_item_labels wl JOIN labels l ON l.id = wl.label_id "
        "WHERE wl.work_item_id = :id ORDER BY l.name"), {"id": row["id"]}).mappings()]
    item["blocked_by"] = open_blockers(connection, row["id"])
    if detail:
        item["relations"] = relations(connection, row["id"])
        item["runs"] = runs_of(connection, row["project_id"], row["id"])
        kids = connection.execute(text(
            "SELECT id, human_key, title, status FROM work_items WHERE parent_id = :id "
            "ORDER BY created_at"), {"id": row["id"]}).mappings().all()
        hidden = hidden_ids(connection, row["project_id"], [k["id"] for k in kids])
        item["children"] = [_brief(r) for r in kids if r["id"] not in hidden]
        item["stages"] = stage_log(connection, row["id"])
        item["restricted"] = restricted(connection, row["id"])
        from . import work_holds

        item["hold"] = work_holds.item_hold(connection, row["id"])   # BOSSAPP-878
        item["access"] = access_list(connection, row["id"])
        # THE BREAK-GLASS NOTICE: whoever can read the item sees who read it past its privacy.
        item["break_glass_reads"] = [
            {"actor": r["actor"], "reason": r["reason"], "at": _iso(r["created_at"])}
            for r in connection.execute(text(
                "SELECT actor, reason, created_at FROM work_item_break_glass_reads "
                "WHERE work_item_id = :id ORDER BY created_at DESC LIMIT 20"),
                {"id": row["id"]}).mappings()]
    return item


def get_item(connection: Connection, project_id: str, item_id: str) -> dict[str, Any]:
    return serialize(connection, _row(connection, project_id, item_id), detail=True)


def relations(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    """Every relation touching the item, read from its side (`blocks` / `blocked_by` ...)."""
    out = []
    rows = connection.execute(text(
        "SELECT r.from_id, r.to_id, r.kind, w.id AS other_id, w.human_key, w.title, w.status "
        "FROM work_item_relations r JOIN work_items w "
        "  ON w.id = CASE WHEN r.from_id = :id THEN r.to_id ELSE r.from_id END "
        "WHERE r.from_id = :id OR r.to_id = :id ORDER BY r.created_at"),
        {"id": item_id}).mappings().all()
    hidden = hidden_ids(connection, "", [r["other_id"] for r in rows])
    for r in rows:
        kind = r["kind"]
        if kind == "blocks" and r["to_id"] == item_id:
            kind = "blocked_by"
        out.append({"kind": kind, "item": _brief({"id": r["other_id"], "human_key": r["human_key"],
                                                  "title": r["title"], "status": r["status"]},
                                                 hidden)})
    return out


def runs_of(connection: Connection, project_id: str, item_id: str) -> list[dict[str, Any]]:
    """The runs, panels and planning tickets that serve this item (their work_item_id)."""
    out: list[dict[str, Any]] = []
    queries = {
        "workflow_run": "SELECT id, status, created_at FROM workflow_runs",
        "code_task": "SELECT id, status, created_at FROM code_task_runs",
        "meeting": "SELECT id, status, created_at FROM meeting_runs",
        "roadmap_item": "SELECT id, status, created_at FROM wp_roadmap_items",
    }
    for kind, sql in queries.items():
        try:
            with connection.begin_nested():
                rows = connection.execute(text(
                    sql + " WHERE project_id = :p AND work_item_id = :w ORDER BY created_at"),
                    {"p": project_id, "w": item_id}).mappings().all()
        except Exception:  # noqa: BLE001 — wp_roadmap_items exists only once planning was used
            continue
        out += [{"kind": kind, "id": r["id"], "status": r["status"],
                 "created_at": _iso(r["created_at"])} for r in rows]
    return out


def stage_log(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "created_at": _iso(r["created_at"])} for r in connection.execute(text(
        "SELECT stage_index, stage_kind, round, event, decision_id, participants, actor, comment, "
        "origin, created_at FROM execution_decisions WHERE work_item_id = :w "
        "ORDER BY created_at, id"), {"w": item_id}).mappings()]


def tree(connection: Connection, project_id: str, *, include_closed: bool = True,
         include_ambient: bool = False, label: Optional[str] = None,
         limit: int = 500) -> dict[str, Any]:
    """The project's items as a forest (parent → children), with goals and labels beside it.

    An item whose parent is filtered out (closed, another label) is shown as a root, so a filter
    never hides an open child."""
    rows = connection.execute(text(
        "SELECT w.* FROM work_items w WHERE w.project_id = :p "
        "AND (:amb OR NOT w.ambient) "
        "AND (:closed OR w.status <> ALL(:closed_set)) "
        "AND (CAST(:label AS TEXT) IS NULL OR EXISTS (SELECT 1 FROM work_item_labels wl "
        "     JOIN labels l ON l.id = wl.label_id WHERE wl.work_item_id = w.id AND l.name = :label)) "
        "AND " + visible_sql(connection, "w.id") + " "
        "ORDER BY w.created_at, w.id LIMIT :n"),
        {"p": project_id, "amb": include_ambient, "closed": include_closed,
         "closed_set": list(CLOSED), "label": label,
         "n": max(1, min(int(limit), 2000)), **visible_params(connection)}).mappings().all()
    from . import work_holds

    held = work_holds.held_items(connection, [r["id"] for r in rows])   # BOSSAPP-878
    nodes = {r["id"]: {**serialize(connection, r), "children": [], "on_hold": r["id"] in held}
             for r in rows}
    roots = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"] or "")
        (parent["children"] if parent is not None else roots).append(node)
    counts: dict[str, int] = {}
    for node in nodes.values():
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    return {"items": roots, "count": len(nodes), "by_status": counts,
            "goals": list_goals(connection, project_id), "labels": list_labels(connection, project_id)}


# ── private items, grants and the break-glass read (BOSSAPP-876) ─────────────────────────────

def restricted(connection: Connection, item_id: str) -> bool:
    """Private itself, or under a private item."""
    return bool(connection.execute(text(
        "WITH RECURSIVE up(id, parent_id, depth) AS (SELECT id, parent_id, 0 FROM work_items "
        "WHERE id = :id UNION ALL SELECT p.id, p.parent_id, up.depth + 1 FROM work_items p "
        "JOIN up ON p.id = up.parent_id WHERE up.depth < 64) "
        "SELECT 1 FROM up JOIN work_items w ON w.id = up.id WHERE w.visibility = 'private' LIMIT 1"),
        {"id": item_id}).first())


def access_list(connection: Connection, item_id: str) -> list[dict[str, Any]]:
    return [{"party": r["party"], "granted_by": r["granted_by"], "at": _iso(r["created_at"])}
            for r in connection.execute(text(
                "SELECT party, granted_by, created_at FROM work_item_access_grants "
                "WHERE work_item_id = :id AND revoked_at IS NULL ORDER BY created_at"),
                {"id": item_id}).mappings()]


def _grant(connection: Connection, project_id: str, item_id: str, party: str, actor: str) -> bool:
    return bool(connection.execute(text(
        "INSERT INTO work_item_access_grants (id, project_id, work_item_id, party, granted_by) "
        "VALUES (:id, :p, :w, :party, :a) ON CONFLICT DO NOTHING"),
        {"id": make_id("wig"), "p": project_id, "w": item_id, "party": party, "a": actor}).rowcount)


def set_visibility(connection: Connection, *, project_id: str, item_id: str, visibility: str,
                   actor: str) -> dict[str, Any]:
    """Make an item private (it and everything under it) or open it to the project again. The
    person making it private is granted on it, so nobody locks themselves out."""
    if visibility not in ("project", "private"):
        raise WorkItemError("visibility is project or private")
    row = _row(connection, project_id, item_id, for_update=True)
    if visibility == "private":
        me = actor if ":" in actor else f"person:{actor}"
        _grant(connection, project_id, item_id, me, actor)
    if (row.get("visibility") or "project") != visibility:
        connection.execute(text(
            "UPDATE work_items SET visibility = :v, updated_at = now() WHERE id = :id"),
            {"v": visibility, "id": item_id})
        emit_event(connection, project_id, "work_item.visibility_changed", actor, "work_item",
                   item_id, {"visibility": visibility})
    return get_item(connection, project_id, item_id)


def grant_access(connection: Connection, *, project_id: str, item_id: str, party: str,
                 actor: str) -> list[dict[str, Any]]:
    """Let `party` (person:<id> | agent:<role>) read this item and everything under it."""
    party = _party(party, "party")
    if not party:
        raise WorkItemError("name who gets access (person:<id> or agent:<role>)")
    _row(connection, project_id, item_id, for_update=True)
    if _grant(connection, project_id, item_id, party, actor):
        emit_event(connection, project_id, "work_item.access_granted", actor, "work_item", item_id,
                   {"party": party})
    return access_list(connection, item_id)


def revoke_access(connection: Connection, *, project_id: str, item_id: str, party: str,
                  actor: str) -> list[dict[str, Any]]:
    _row(connection, project_id, item_id, for_update=True)
    gone = connection.execute(text(
        "UPDATE work_item_access_grants SET revoked_at = now(), revoked_by = :a "
        "WHERE work_item_id = :w AND party = :party AND revoked_at IS NULL"),
        {"a": actor, "w": item_id, "party": str(party or "")}).rowcount
    if not gone:
        raise LookupError("no such grant on this item")
    emit_event(connection, project_id, "work_item.access_revoked", actor, "work_item", item_id,
               {"party": party})
    return access_list(connection, item_id)


def break_glass_read(connection: Connection, *, project_id: str, item_id: str, actor: str,
                     reason: str) -> dict[str, Any]:
    """THE ONE WAY PAST AN ITEM'S PRIVACY: a single read of it, with a reason, recorded (a row,
    an event the activity log keeps) and shown on the item to everyone who may read it. The
    caller's own visibility is untouched afterwards — the next read is ordinary again."""
    reason = re.sub(r"\s+", " ", str(reason or "")).strip()
    if len(reason) < 10:
        raise WorkItemError("say why you need to read it (at least 10 characters)")
    saved = _VIEWERS.pop(connection, None)
    try:
        _row(connection, project_id, item_id)
        connection.execute(text(
            "INSERT INTO work_item_break_glass_reads (id, project_id, work_item_id, actor, reason) "
            "VALUES (:id, :p, :w, :a, :r)"),
            {"id": make_id("wbg"), "p": project_id, "w": item_id, "a": actor, "r": reason[:1000]})
        emit_event(connection, project_id, "work_item.break_glass_read", actor, "work_item",
                   item_id, {"reason": reason[:1000], "restricted": restricted(connection, item_id)})
        return {**get_item(connection, project_id, item_id), "break_glass": True}
    finally:
        if saved is not None:
            _VIEWERS[connection] = saved


# ── creating ─────────────────────────────────────────────────────────────────────────────────

def _party(value: Any, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    value = str(value).strip()
    if not _PARTY.match(value):
        raise WorkItemError(f"{field} must be agent:<role> or person:<id>")
    return value


def _status(value: Any, allowed: Iterable[str] = STATUSES) -> str:
    value = str(value or "").strip().lower()
    allowed = tuple(allowed)
    if value not in allowed:
        raise WorkItemError(f"status must be one of {', '.join(allowed)}")
    return value


def next_human_key(connection: Connection, project_id: str) -> str:
    row = connection.execute(text(
        "INSERT INTO work_item_keys (project_id) VALUES (:p) "
        "ON CONFLICT (project_id) DO UPDATE SET next_number = work_item_keys.next_number + 1 "
        "RETURNING prefix, next_number"), {"p": project_id}).mappings().one()
    # A fresh row returns next_number 1 (it is used); an update returns the incremented value.
    return f"{row['prefix']}-{row['next_number']}"


def _check_parent(connection: Connection, project_id: str, item_id: Optional[str],
                  parent_id: Optional[str]) -> Optional[str]:
    if not parent_id:
        return None
    _row(connection, project_id, parent_id)
    if item_id:
        # Walking up from the new parent must never reach the item: no cycles in the tree.
        hit = connection.execute(text(
            "WITH RECURSIVE up(id, parent_id) AS ("
            "  SELECT id, parent_id FROM work_items WHERE id = :parent "
            "  UNION SELECT w.id, w.parent_id FROM work_items w JOIN up ON w.id = up.parent_id) "
            "SELECT 1 FROM up WHERE id = :item LIMIT 1"), {"parent": parent_id, "item": item_id}).first()
        if hit:
            raise WorkItemError("an item cannot be placed under itself or its own descendant")
    return parent_id


def _check_goal(connection: Connection, project_id: str, goal_id: Optional[str]) -> Optional[str]:
    if not goal_id:
        return None
    if connection.execute(text("SELECT 1 FROM goals WHERE id = :g AND project_id = :p"),
                          {"g": goal_id, "p": project_id}).first() is None:
        raise LookupError(f"no goal {goal_id} in this project")
    return goal_id


def create_item(connection: Connection, settings: Any = None, *, project_id: str, title: str,
                description: str = "", parent_id: Optional[str] = None,
                goal_id: Optional[str] = None, assignee: Optional[str] = None,
                responsible_person: Optional[str] = None, status: str = "todo",
                labels: Iterable[str] = (), execution_policy: Optional[Mapping[str, Any]] = None,
                subject_type: str = "item", subject_id: Optional[str] = None,
                actor: str = "system", visibility: str = "project") -> dict[str, Any]:
    """Make a canonical item. It joins the scored portfolio through upsert_work_item (the same
    score, the same `work.priority_scored` event), then gains its key, place and owners."""
    if visibility not in ("project", "private"):
        raise WorkItemError("visibility is project or private")
    from ..config import get_settings
    from .execution_policy import normalize_policy
    from .work_orchestration import WorkOrchestrationService

    title = str(title or "").strip()
    if not title:
        raise WorkItemError("a work item needs a title")
    status = _status(status, SETTABLE)
    assignee = _party(assignee, "assignee")
    responsible_person = _party(responsible_person, "responsible_person")
    if responsible_person and not responsible_person.startswith("person:"):
        raise WorkItemError("responsible_person must be a person (person:<id>)")
    _check_parent(connection, project_id, None, parent_id)
    _check_goal(connection, project_id, goal_id)
    policy = normalize_policy(execution_policy) if execution_policy else None
    placeholder = subject_id or make_id("wi")
    created = WorkOrchestrationService(settings or get_settings()).upsert_work_item(
        connection, project_id=project_id, subject_type=subject_type, subject_id=placeholder,
        title=title[:300], description=str(description or "")[:8000], status=status,
        owner_actor=assignee, actor=actor)
    item_id = created["id"]
    connection.execute(text(
        "UPDATE work_items SET subject_id = CASE WHEN :own THEN id ELSE subject_id END, "
        "human_key = COALESCE(human_key, :key), parent_id = :parent, goal_id = :goal, "
        "assignee = :assignee, responsible_person = :resp, "
        "execution_policy = CAST(:policy AS JSONB), updated_at = now() WHERE id = :id"),
        {"own": subject_id is None, "key": next_human_key(connection, project_id),
         "parent": parent_id, "goal": goal_id, "assignee": assignee, "resp": responsible_person,
         "policy": json.dumps(policy) if policy else None, "id": item_id})
    if visibility == "private":
        # Born private: never readable by the project for a moment, and its maker holds a grant.
        connection.execute(text("UPDATE work_items SET visibility = 'private' WHERE id = :id"),
                           {"id": item_id})
        _grant(connection, project_id, item_id, actor if ":" in actor else f"person:{actor}", actor)
    if labels:
        add_labels(connection, project_id, item_id, labels, actor=actor)
    emit_event(connection, project_id, "work_item.created", actor, "work_item", item_id,
               {"subject_type": subject_type, "parent_id": parent_id, "status": status,
                **({"visibility": "private"} if visibility == "private" else {})})
    return get_item(connection, project_id, item_id)


def update_item(connection: Connection, settings: Any = None, *, project_id: str, item_id: str,
                fields: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Edit an item. `status: done` is a completion (the policy may hold it in review)."""
    from .execution_policy import normalize_policy

    unknown = sorted(set(fields) - set(EDITABLE))
    if unknown:
        raise WorkItemError(f"cannot edit {', '.join(unknown)}")
    row = _row(connection, project_id, item_id, for_update=True)
    sets: dict[str, Any] = {}
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            raise WorkItemError("a work item needs a title")
        sets["title"] = title[:300]
    if "description" in fields:
        sets["description"] = str(fields["description"] or "")[:8000]
    if "parent_id" in fields:
        sets["parent_id"] = _check_parent(connection, project_id, item_id, fields["parent_id"] or None)
    if "goal_id" in fields:
        sets["goal_id"] = _check_goal(connection, project_id, fields["goal_id"] or None)
    if "assignee" in fields:
        sets["assignee"] = _party(fields["assignee"], "assignee")
        sets["owner_actor"] = sets["assignee"]
    if "responsible_person" in fields:
        resp = _party(fields["responsible_person"], "responsible_person")
        if resp and not resp.startswith("person:"):
            raise WorkItemError("responsible_person must be a person (person:<id>)")
        sets["responsible_person"] = resp
    if "due_at" in fields:
        sets["due_at"] = fields["due_at"] or None
    if "execution_policy" in fields:
        policy = fields["execution_policy"]
        sets["execution_policy"] = json.dumps(normalize_policy(policy)) if policy else None
    if sets:
        assign = ", ".join(
            f"{k} = CAST(:{k} AS JSONB)" if k == "execution_policy" else f"{k} = :{k}"
            for k in sets)
        connection.execute(text(f"UPDATE work_items SET {assign}, updated_at = now() WHERE id = :id"),
                           {**sets, "id": item_id})
        emit_event(connection, project_id, "work_item.updated", actor, "work_item", item_id,
                   {"fields": sorted(sets)})
    if "visibility" in fields and fields["visibility"]:
        set_visibility(connection, project_id=project_id, item_id=item_id,
                       visibility=str(fields["visibility"]), actor=actor)
    if "status" in fields and fields["status"] != row["status"]:
        target = _status(fields["status"])
        if target == "done":
            complete_item(connection, settings, project_id=project_id, item_id=item_id,
                          actor=actor, origin=f"person:{actor}" if ":" not in actor else actor)
        else:
            set_status(connection, settings, project_id=project_id, item_id=item_id,
                       status=target, actor=actor)
    return get_item(connection, project_id, item_id)


def set_status(connection: Connection, settings: Any = None, *, project_id: str, item_id: str,
               status: str, actor: str) -> dict[str, Any]:
    """A hand-set status (never done, never in_review). Re-opening a closed item is allowed."""
    status = _status(status, SETTABLE)
    row = _row(connection, project_id, item_id, for_update=True)
    if row["status"] == "in_review" and status != "cancelled":
        raise WorkItemError("an item in review moves when its stage is answered (or cancel it)")
    connection.execute(text(
        "UPDATE work_items SET status = :s, updated_at = now(), "
        "resolved_at = CASE WHEN :s = 'cancelled' THEN now() ELSE NULL END, "
        "resolved_by = CASE WHEN :s = 'cancelled' THEN :a ELSE NULL END WHERE id = :id"),
        {"s": status, "a": actor, "id": item_id})
    emit_event(connection, project_id, "work_item.status_changed", actor, "work_item", item_id,
               {"from": row["status"], "to": status})
    if status == "cancelled":
        # A cancelled blocker no longer blocks: what it held may go.
        wake_unblocked(connection, settings, project_id=project_id, item_id=item_id, actor=actor)
    return get_item(connection, project_id, item_id)


def delete_item(connection: Connection, *, project_id: str, item_id: str, actor: str) -> None:
    row = _row(connection, project_id, item_id, for_update=True)
    if row.get("ambient"):
        raise WorkItemError("an always-on item is ended from the agents' board, not deleted")
    for table in ("workflow_runs", "code_task_runs", "meeting_runs"):
        connection.execute(text(f"UPDATE {table} SET work_item_id = NULL WHERE work_item_id = :w"),
                           {"w": item_id})
    connection.execute(text("DELETE FROM work_items WHERE id = :id"), {"id": item_id})
    emit_event(connection, project_id, "work_item.deleted", actor, "work_item", item_id,
               {"title": row["title"], "human_key": row.get("human_key")})


# ── completion and the blocker wake ──────────────────────────────────────────────────────────

def complete_item(connection: Connection, settings: Any = None, *, project_id: str,
                  item_id: str, actor: str, origin: Optional[str] = None) -> dict[str, Any]:
    """THE done door. The execution policy decides: no stages (or all passed) → done now, and
    what it blocked wakes; otherwise the item goes to in_review and a stage decision is raised."""
    from .execution_policy import apply_transition

    verdict = apply_transition(connection, settings, project_id=project_id,
                               work_item_id=item_id, actor=actor, origin=origin)
    if verdict["status"] == "done":
        verdict["woke"] = mark_done(connection, settings, project_id=project_id,
                                    item_id=item_id, actor=actor)
    return verdict


def mark_done(connection: Connection, settings: Any = None, *, project_id: str, item_id: str,
              actor: str) -> list[dict[str, Any]]:
    """Write done (the policy already said yes) and wake what this item was blocking."""
    connection.execute(text(
        "UPDATE work_items SET status = 'done', resolved_at = now(), resolved_by = :a, "
        "updated_at = now() WHERE id = :id"), {"a": actor, "id": item_id})
    emit_event(connection, project_id, "work_item.completed", actor, "work_item", item_id, {})
    return wake_unblocked(connection, settings, project_id=project_id, item_id=item_id,
                          actor=actor)


def wake_unblocked(connection: Connection, settings: Any = None, *, project_id: str,
                   item_id: str, actor: str) -> list[dict[str, Any]]:
    """THE BLOCKER-RESOLUTION HOOK. Every item this one blocked whose blockers are now ALL
    closed is unblocked: `blocked` → `todo`, and its runs start or resume."""
    woke = []
    blocked = connection.execute(text(
        "SELECT w.id, w.status FROM work_item_relations r JOIN work_items w ON w.id = r.to_id "
        "WHERE r.from_id = :id AND r.kind = 'blocks' ORDER BY w.created_at"),
        {"id": item_id}).mappings().all()
    for item in blocked:
        if item["status"] in CLOSED or open_blockers(connection, item["id"]):
            continue
        if item["status"] == "blocked":
            connection.execute(text(
                "UPDATE work_items SET status = 'todo', updated_at = now() WHERE id = :id"),
                {"id": item["id"]})
        started = start_or_resume(connection, settings, project_id=project_id,
                                  item_id=item["id"], actor=actor)
        emit_event(connection, project_id, "work_item.unblocked", actor, "work_item", item["id"],
                   {"by": item_id, "runs": started})
        woke.append({"id": item["id"], "runs": started})
    return woke


def start_or_resume(connection: Connection, settings: Any = None, *, project_id: str,
                    item_id: str, actor: str, resume_paused: bool = True) -> list[dict[str, Any]]:
    """Resume the item's paused workflow runs, and launch the start that waited on its
    blockers (execution_state.deferred_start). Each in its own savepoint: one run that cannot
    move never stops the others."""
    from . import work_holds
    from . import workflows as wf

    out: list[dict[str, Any]] = []
    # A WORK TREE ON HOLD (BOSSAPP-878) wakes nothing: restoring the tree does.
    if work_holds.held_items(connection, [item_id]):
        return [{"kind": "hold", "action": "held"}]
    for run_id in (connection.execute(text(
            "SELECT id FROM workflow_runs WHERE project_id = :p AND work_item_id = :w "
            "AND status = 'paused' ORDER BY created_at"),
            {"p": project_id, "w": item_id}).scalars().all() if resume_paused else []):
        try:
            with connection.begin_nested():
                wf.resume_run(connection, project_id, run_id, actor)
            out.append({"kind": "workflow_run", "id": run_id, "action": "resumed"})
        except Exception as error:  # noqa: BLE001
            out.append({"kind": "workflow_run", "id": run_id, "action": "not_resumed",
                        "why": str(error)[:200]})
    state = _json(connection.execute(text(
        "SELECT execution_state FROM work_items WHERE id = :id"), {"id": item_id}).scalar(), {}) or {}
    deferred = state.get("deferred_start")
    if open_blockers(connection, item_id):
        deferred = None                          # still waits on a blocker: its wake starts it
    if isinstance(deferred, dict) and deferred.get("workflow_version_id"):
        try:
            with connection.begin_nested():
                run = wf.start_run(
                    connection, project_id=project_id, actor=actor,
                    input_data={**(deferred.get("input") or {}), "work_item_id": item_id},
                    dedupe_key=f"work_item:{item_id}:{make_id('start')}",
                    workflow_version_id=str(deferred["workflow_version_id"]))
            out.append({"kind": "workflow_run", "id": run.get("id"), "action": "started"})
            connection.execute(text(
                "UPDATE work_items SET execution_state = execution_state - 'deferred_start' "
                "WHERE id = :id"), {"id": item_id})
        except Exception as error:  # noqa: BLE001
            out.append({"kind": "workflow_run", "action": "not_started", "why": str(error)[:200]})
    return out


def defer_start(connection: Connection, *, project_id: str, item_id: str,
                workflow_version_id: str, input_data: Optional[Mapping[str, Any]] = None,
                actor: str) -> dict[str, Any]:
    """Start a workflow for this item now, or — while it has open blockers, or its work tree is
    on hold (BOSSAPP-878) — once they close / the tree is restored."""
    from . import work_holds
    from . import workflows as wf

    _row(connection, project_id, item_id, for_update=True)
    if work_holds.held_items(connection, [item_id]):
        connection.execute(text(
            "UPDATE work_items SET execution_state = COALESCE(execution_state, '{}'::jsonb) || "
            "jsonb_build_object('deferred_start', CAST(:d AS JSONB)), updated_at = now() "
            "WHERE id = :id"),
            {"d": json.dumps({"workflow_version_id": workflow_version_id,
                              "input": dict(input_data or {}), "by": actor}), "id": item_id})
        emit_event(connection, project_id, "work_item.start_deferred", actor, "work_item",
                   item_id, {"workflow_version_id": workflow_version_id, "held": True})
        return {"status": "deferred", "held": True, "blocked_by": open_blockers(connection, item_id)}
    if open_blockers(connection, item_id):
        connection.execute(text(
            "UPDATE work_items SET status = 'blocked', execution_state = "
            "COALESCE(execution_state, '{}'::jsonb) || jsonb_build_object('deferred_start', "
            "CAST(:d AS JSONB)), updated_at = now() WHERE id = :id"),
            {"d": json.dumps({"workflow_version_id": workflow_version_id,
                              "input": dict(input_data or {}), "by": actor}), "id": item_id})
        emit_event(connection, project_id, "work_item.start_deferred", actor, "work_item",
                   item_id, {"workflow_version_id": workflow_version_id})
        return {"status": "deferred", "blocked_by": open_blockers(connection, item_id)}
    run = wf.start_run(connection, project_id=project_id, actor=actor,
                       input_data={**dict(input_data or {}), "work_item_id": item_id},
                       dedupe_key=f"work_item:{item_id}:{make_id('start')}",
                       workflow_version_id=workflow_version_id)
    connection.execute(text(
        "UPDATE work_items SET status = 'active', updated_at = now() WHERE id = :id "
        "AND status IN ('backlog', 'todo')"), {"id": item_id})
    return {"status": "started", "run_id": run.get("id")}


# ── lazy linking from the creation doors ─────────────────────────────────────────────────────

def _is_postgres(connection: Any) -> bool:
    dialect = getattr(connection, "dialect", None) or getattr(
        getattr(connection, "bind", None), "dialect", None)
    return getattr(dialect, "name", "") == "postgresql"


def _item_of(connection: Connection, kind: str, run_id: Any) -> Optional[str]:
    table = RUN_TABLES.get(kind)
    if not table or not run_id:
        return None
    with connection.begin_nested():
        return connection.execute(text(f"SELECT work_item_id FROM {table} WHERE id = :id"),
                                  {"id": str(run_id)}).scalar()


def link_run(connection: Connection, *, kind: str, run_id: str, project_id: str, title: str,
             explicit_id: Optional[str] = None,
             inherit: Iterable[tuple[str, Any]] = (), actor: str = "boss",
             settings: Any = None) -> Optional[str]:
    """Give a just-created run/panel/ticket its work item. NEVER raises: a creation door must
    not fail because the graph could not be written (the run simply has no item).

    * `explicit_id` — the run was started FOR an item (input `work_item_id`): it joins it, and
      is that item's primary run when the item has none yet (so the run's done is the item's).
    * `inherit` — (kind, id) of the run this one was spawned by (a workflow's build, a child
      run): the first that has an item lends it. A spawned run never completes the item.
    * otherwise a new item is made with this run as its subject (and so its primary)."""
    table = RUN_TABLES.get(kind)
    if table is None or not run_id or not _is_postgres(connection):
        return None
    try:
        with connection.begin_nested():
            item_id: Optional[str] = None
            if explicit_id:
                row = connection.execute(text(
                    "SELECT id, execution_state FROM work_items WHERE id = :id AND project_id = :p "
                    "FOR UPDATE"), {"id": str(explicit_id), "p": project_id}).mappings().first()
                if row is not None:
                    item_id = row["id"]
                    state = _json(row["execution_state"], {}) or {}
                    if not state.get("primary_run"):
                        connection.execute(text(
                            "UPDATE work_items SET execution_state = COALESCE(execution_state, "
                            "'{}'::jsonb) || jsonb_build_object('primary_run', CAST(:r AS TEXT)), "
                            "status = CASE WHEN status IN ('backlog', 'todo') THEN 'active' "
                            "ELSE status END, updated_at = now() WHERE id = :id"),
                            {"r": f"{kind}:{run_id}", "id": item_id})
            if item_id is None:
                for parent_kind, parent_id in inherit:
                    item_id = _item_of(connection, parent_kind, parent_id)
                    if item_id:
                        break
            if item_id is None:
                created = create_item(connection, settings, project_id=project_id,
                                      title=str(title or kind).strip()[:300] or kind,
                                      subject_type=kind, subject_id=str(run_id),
                                      status="active", actor=actor)
                item_id = created["id"]
            connection.execute(text(f"UPDATE {table} SET work_item_id = :w WHERE id = :id"),
                               {"w": item_id, "id": str(run_id)})
            return item_id
    except Exception:  # noqa: BLE001
        log.warning("work item not linked for %s %s", kind, run_id, exc_info=True)
        return None


def is_primary(item: Mapping[str, Any], kind: str, run_id: str) -> bool:
    """Whether this run's done is the item's done (its subject, or the run it was started for)."""
    if item.get("subject_type") == kind and str(item.get("subject_id")) == str(run_id):
        return True
    state = _json(item.get("execution_state"), {}) or {}
    return state.get("primary_run") == f"{kind}:{run_id}"


def on_run_completed(connection: Connection, settings: Any = None, *, kind: str, run_id: str,
                     actor: str = "boss") -> Optional[dict[str, Any]]:
    """A run reached its done (a workflow completed, a build passed its gates and completed, a
    panel wrote its PRD). If it is its item's primary run, the item completes through the
    policy. NEVER raises: the run's own completion is already written and must stand."""
    if not _is_postgres(connection):
        return None
    try:
        with connection.begin_nested():
            item_id = _item_of(connection, kind, run_id)
            if not item_id:
                return None
            row = connection.execute(text("SELECT * FROM work_items WHERE id = :id FOR UPDATE"),
                                     {"id": item_id}).mappings().first()
            if row is None or row["status"] in CLOSED or row["status"] == "in_review":
                return None
            if row.get("ambient") or not is_primary(row, kind, run_id):
                return None
            return complete_item(connection, settings, project_id=row["project_id"],
                                 item_id=item_id, actor=actor, origin=f"{kind}:{run_id}")
    except Exception:  # noqa: BLE001
        log.warning("work item not completed for %s %s", kind, run_id, exc_info=True)
        return None


# ── relations ────────────────────────────────────────────────────────────────────────────────

def add_relation(connection: Connection, *, project_id: str, item_id: str, other_id: str,
                 kind: str, actor: str) -> list[dict[str, Any]]:
    """`item blocks other`, `item blocked_by other`, `relates_to`, `duplicates`."""
    if kind not in RELATION_KINDS:
        raise WorkItemError(f"relation must be one of {', '.join(RELATION_KINDS)}")
    if item_id == other_id:
        raise WorkItemError("an item cannot relate to itself")
    _row(connection, project_id, item_id)
    _row(connection, project_id, other_id)
    src, dst, stored = item_id, other_id, kind
    if kind == "blocked_by":
        src, dst, stored = other_id, item_id, "blocks"
    if stored == "blocks":
        # No blocking cycles: if dst already (transitively) blocks src, this edge closes a loop.
        loop = connection.execute(text(
            "WITH RECURSIVE down(id) AS (SELECT CAST(:dst AS TEXT) UNION "
            "  SELECT r.to_id FROM work_item_relations r JOIN down ON r.from_id = down.id "
            "  WHERE r.kind = 'blocks') SELECT 1 FROM down WHERE id = :src LIMIT 1"),
            {"dst": dst, "src": src}).first()
        if loop:
            raise WorkItemError("that would make a blocking cycle")
    connection.execute(text(
        "INSERT INTO work_item_relations (project_id, from_id, to_id, kind, created_by) "
        "VALUES (:p, :f, :t, :k, :a) ON CONFLICT DO NOTHING"),
        {"p": project_id, "f": src, "t": dst, "k": stored, "a": actor})
    if stored == "blocks":
        blocker_status = _row(connection, project_id, src)["status"]
        if blocker_status not in CLOSED:
            connection.execute(text(
                "UPDATE work_items SET status = 'blocked', updated_at = now() "
                "WHERE id = :id AND status IN ('backlog', 'todo')"), {"id": dst})
    emit_event(connection, project_id, "work_item.related", actor, "work_item", item_id,
               {"kind": kind, "other": other_id})
    return relations(connection, item_id)


def remove_relation(connection: Connection, settings: Any = None, *, project_id: str,
                    item_id: str, other_id: str, kind: str, actor: str) -> list[dict[str, Any]]:
    if kind not in RELATION_KINDS:
        raise WorkItemError(f"relation must be one of {', '.join(RELATION_KINDS)}")
    src, dst, stored = item_id, other_id, kind
    if kind == "blocked_by":
        src, dst, stored = other_id, item_id, "blocks"
    gone = connection.execute(text(
        "DELETE FROM work_item_relations WHERE project_id = :p AND from_id = :f AND to_id = :t "
        "AND kind = :k"), {"p": project_id, "f": src, "t": dst, "k": stored}).rowcount
    if not gone:
        raise LookupError("no such relation")
    if stored == "blocks" and not open_blockers(connection, dst):
        status = _row(connection, project_id, dst)["status"]
        if status == "blocked":
            connection.execute(text(
                "UPDATE work_items SET status = 'todo', updated_at = now() WHERE id = :id"),
                {"id": dst})
            start_or_resume(connection, settings, project_id=project_id, item_id=dst, actor=actor)
    emit_event(connection, project_id, "work_item.unrelated", actor, "work_item", item_id,
               {"kind": kind, "other": other_id})
    return relations(connection, item_id)


# ── labels ───────────────────────────────────────────────────────────────────────────────────

def _label_name(name: Any) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    if not name or len(name) > 40:
        raise WorkItemError("a label is 1-40 characters")
    return name


def list_labels(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text(
        "SELECT l.id, l.name, l.color, count(wl.work_item_id) AS items FROM labels l "
        "LEFT JOIN work_item_labels wl ON wl.label_id = l.id WHERE l.project_id = :p "
        "GROUP BY l.id ORDER BY l.name"), {"p": project_id}).mappings()]


def ensure_label(connection: Connection, project_id: str, name: str,
                 color: Optional[str] = None) -> str:
    name = _label_name(name)
    return connection.execute(text(
        "INSERT INTO labels (id, project_id, name, color) VALUES (:id, :p, :n, COALESCE(:c, 'gray')) "
        "ON CONFLICT (project_id, name) DO UPDATE SET color = COALESCE(:c, labels.color) "
        "RETURNING id"), {"id": make_id("lbl"), "p": project_id, "n": name, "c": color}).scalar()


def add_labels(connection: Connection, project_id: str, item_id: str, names: Iterable[str], *,
               actor: str) -> list[dict[str, Any]]:
    _row(connection, project_id, item_id)
    for name in names:
        label_id = ensure_label(connection, project_id, name)
        connection.execute(text(
            "INSERT INTO work_item_labels (work_item_id, label_id) VALUES (:w, :l) "
            "ON CONFLICT DO NOTHING"), {"w": item_id, "l": label_id})
    return serialize(connection, _row(connection, project_id, item_id))["labels"]


def remove_label(connection: Connection, project_id: str, item_id: str, name: str, *,
                 actor: str) -> list[dict[str, Any]]:
    _row(connection, project_id, item_id)
    connection.execute(text(
        "DELETE FROM work_item_labels WHERE work_item_id = :w AND label_id IN "
        "(SELECT id FROM labels WHERE project_id = :p AND name = :n)"),
        {"w": item_id, "p": project_id, "n": _label_name(name)})
    return serialize(connection, _row(connection, project_id, item_id))["labels"]


# ── goals ────────────────────────────────────────────────────────────────────────────────────

def list_goals(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    return [{**dict(r), "target_date": _iso(r["target_date"]), "created_at": _iso(r["created_at"])}
            for r in connection.execute(text(
                "SELECT g.id, g.parent_id, g.title, g.description, g.status, g.owner, "
                "g.target_date, g.created_at, (SELECT count(*) FROM work_items w "
                "WHERE w.goal_id = g.id) AS items FROM goals g WHERE g.project_id = :p "
                "ORDER BY g.created_at"), {"p": project_id}).mappings()]


def create_goal(connection: Connection, *, project_id: str, title: str, description: str = "",
                parent_id: Optional[str] = None, owner: Optional[str] = None,
                target_date: Optional[str] = None, actor: str = "system") -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise WorkItemError("a goal needs a title")
    if parent_id:
        _check_goal(connection, project_id, parent_id)
    goal_id = make_id("goal")
    connection.execute(text(
        "INSERT INTO goals (id, project_id, parent_id, title, description, owner, target_date, "
        "created_by) VALUES (:id, :p, :parent, :t, :d, :o, CAST(:td AS DATE), :a)"),
        {"id": goal_id, "p": project_id, "parent": parent_id, "t": title[:300],
         "d": str(description or "")[:4000], "o": owner, "td": target_date or None, "a": actor})
    emit_event(connection, project_id, "goal.created", actor, "goal", goal_id, {"title": title})
    return next(g for g in list_goals(connection, project_id) if g["id"] == goal_id)


__all__ = [
    "STATUSES", "CLOSED", "RELATION_KINDS", "WorkItemError", "get_item", "tree", "create_item",
    "update_item", "set_status", "delete_item", "complete_item", "mark_done", "wake_unblocked",
    "start_or_resume", "defer_start", "link_run", "on_run_completed", "add_relation",
    "remove_relation", "add_labels", "remove_label", "list_labels", "ensure_label",
    "create_goal", "list_goals", "open_blockers", "relations", "runs_of", "view_as", "viewing",
    "visible_sql", "visible_params", "hidden_ids", "restricted", "access_list", "set_visibility",
    "grant_access", "revoke_access", "break_glass_read",
]
