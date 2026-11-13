"""PAUSE, RESUME OR RESTORE A WHOLE WORK TREE (BOSSAPP-878).

WHY. Cancel cascades to a run's child runs, and pause stops ONE run. A runaway multi-agent
effort — an epic with a dozen stories, each with a build and its child runs — could only be
stopped by cancelling it (losing it) or by pausing run after run, and nothing put it back the
way it was.

A HOLD is one row on a root — a work item (its whole subtree in the canonical graph, BOSSAPP-830,
and every run serving any item in it, with their child runs) or a workflow run (it and its child
runs). :func:`pause_tree` SNAPSHOTS every member as it was (``run_hold_members.prior_status``):

* mode ``pause`` — a running run is paused now (workflows.pause_run, the one pause door);
* mode ``drain`` — running work is left to finish; nothing NEW starts under the hold.

While a hold is active (:func:`held_sql`, the one predicate), under it nothing is claimed by a
worker in pause mode (jobs.claim_job, workflows.claim_step), woken by a finished blocker or started
by a deferred start (work_items.start_or_resume / defer_start), or resumed by hand
(workflows.resume_run refuses a run the hold paused).

:func:`restore_tree` puts it back EXACTLY: it resumes only the runs the hold itself paused that
are still paused, leaves a run that was already paused before the hold paused, and leaves alone a
run another active hold still holds. Both doors take ``dry_run`` and answer with the plan —
what would pause, what would resume — without changing anything.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

MODES = ("pause", "drain")
ROOT_KINDS = ("work_item", "workflow_run")


class HoldError(ValueError):
    """Something the person can fix (HTTP 422); a conflict uses HoldConflict (409)."""


class HoldConflict(HoldError):
    pass


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


# ── the one predicate ───────────────────────────────────────────────────────────────────────

def held_sql(item_col: str, *, mode: Optional[str] = None) -> str:
    """SQL true when the work item `item_col` is under an ACTIVE hold: a member of one, or below
    a held root (so an item added to the tree after the hold is held too). `mode` limits it to
    holds of that mode."""
    mode_sql = f" AND h.mode = '{mode}'" if mode in MODES else ""
    return (
        "EXISTS (WITH RECURSIVE up(id, parent_id, depth) AS ("
        f"  SELECT a.id, a.parent_id, 0 FROM work_items a WHERE a.id = {item_col} "
        "  UNION ALL SELECT p.id, p.parent_id, up.depth + 1 FROM work_items p "
        "  JOIN up ON p.id = up.parent_id WHERE up.depth < 64) "
        "SELECT 1 FROM run_holds h WHERE h.status = 'active'" + mode_sql + " AND ("
        "  (h.root_kind = 'work_item' AND h.root_id IN (SELECT id FROM up)) "
        "  OR EXISTS (SELECT 1 FROM run_hold_members m WHERE m.hold_id = h.id "
        f"    AND m.member_kind = 'work_item' AND m.member_id = {item_col})))")


def run_held_sql(run_col: str, *, mode: Optional[str] = None) -> str:
    """SQL true when the workflow run `run_col` is a member of an active hold, or serves a held
    work item."""
    mode_sql = f" AND h.mode = '{mode}'" if mode in MODES else ""
    return (
        "(EXISTS (SELECT 1 FROM run_hold_members m JOIN run_holds h ON h.id = m.hold_id "
        f"  WHERE h.status = 'active'{mode_sql} AND m.member_kind = 'workflow_run' "
        f"  AND m.member_id = {run_col}) "
        "OR EXISTS (SELECT 1 FROM workflow_runs hr WHERE hr.id = "
        f"  {run_col} AND hr.work_item_id IS NOT NULL AND "
        + held_sql("hr.work_item_id", mode=mode) + "))")


def item_hold(connection: Connection, item_id: str) -> Optional[dict[str, Any]]:
    """The active hold an item is under, if any (for the item's panel)."""
    row = connection.execute(text(
        "WITH RECURSIVE up(id, parent_id, depth) AS (SELECT id, parent_id, 0 FROM work_items "
        "WHERE id = :id UNION ALL SELECT p.id, p.parent_id, up.depth + 1 FROM work_items p "
        "JOIN up ON p.id = up.parent_id WHERE up.depth < 64) "
        "SELECT h.* FROM run_holds h WHERE h.status = 'active' AND ("
        "  (h.root_kind = 'work_item' AND h.root_id IN (SELECT id FROM up)) OR EXISTS ("
        "  SELECT 1 FROM run_hold_members m WHERE m.hold_id = h.id AND m.member_kind = 'work_item' "
        "  AND m.member_id = :id)) ORDER BY h.created_at LIMIT 1"), {"id": item_id}).mappings().first()
    return _public(row) if row else None


def held_items(connection: Connection, ids: Iterable[str], *,
               mode: Optional[str] = None) -> set[str]:
    """Which of these work items are under an active hold (of `mode`, when given)."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return set()
    if not connection.execute(text("SELECT 1 FROM run_holds WHERE status = 'active' LIMIT 1")).first():
        return set()
    return set(connection.execute(text(
        "SELECT w.id FROM work_items w WHERE w.id = ANY(:ids) AND " + held_sql("w.id", mode=mode)),
        {"ids": wanted}).scalars())


def run_is_held(connection: Connection, run_id: str, *, mode: Optional[str] = None) -> bool:
    if not connection.execute(text("SELECT 1 FROM run_holds WHERE status = 'active' LIMIT 1")).first():
        return False
    return bool(connection.execute(text(
        "SELECT 1 WHERE " + run_held_sql(":r", mode=mode)), {"r": run_id}).first())


# ── who is in the tree ──────────────────────────────────────────────────────────────────────

def _members(connection: Connection, project_id: str, root_kind: str,
             root_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(work items, workflow runs) under the root, each with its current status."""
    from .workflows import TERMINAL_RUN_STATES

    if root_kind == "work_item":
        from . import work_items

        work_items._row(connection, project_id, root_id)   # visible to this viewer, in project
        items = [dict(r) for r in connection.execute(text(
            "WITH RECURSIVE down(id, depth) AS (SELECT CAST(:root AS TEXT), 0 UNION ALL "
            "SELECT w.id, down.depth + 1 FROM work_items w JOIN down ON w.parent_id = down.id "
            "WHERE down.depth < 64) SELECT w.id, w.status, w.title, w.human_key FROM work_items w "
            "JOIN down ON down.id = w.id WHERE w.project_id = :p ORDER BY w.created_at"),
            {"root": root_id, "p": project_id}).mappings()]
        seed_sql = "SELECT id FROM workflow_runs WHERE project_id = :p AND work_item_id = ANY(:items)"
        params: dict[str, Any] = {"p": project_id, "items": [i["id"] for i in items]}
    elif root_kind == "workflow_run":
        if connection.execute(text("SELECT 1 FROM workflow_runs WHERE id = :r AND project_id = :p"),
                              {"r": root_id, "p": project_id}).first() is None:
            raise LookupError(f"no workflow run {root_id} in this project")
        items = []
        seed_sql = "SELECT CAST(:root AS TEXT) AS id"
        params = {"p": project_id, "root": root_id}
    else:
        raise HoldError(f"a hold's root is one of {', '.join(ROOT_KINDS)}")
    runs = [dict(r) for r in connection.execute(text(
        "WITH RECURSIVE tree(id, depth) AS (" + seed_sql + ", 0 AS depth "
        "  UNION SELECT r.id, tree.depth + 1 FROM workflow_runs r JOIN tree ON r.parent_run_id = tree.id "
        "  WHERE tree.depth < 32) "
        "SELECT DISTINCT r.id, r.status, r.created_at FROM workflow_runs r JOIN tree ON tree.id = r.id "
        "WHERE r.project_id = :p AND r.status <> ALL(:done) ORDER BY r.created_at"
        if root_kind == "workflow_run" else
        "WITH RECURSIVE tree(id, depth) AS (SELECT id, 0 FROM (" + seed_sql + ") s "
        "  UNION SELECT r.id, tree.depth + 1 FROM workflow_runs r JOIN tree ON r.parent_run_id = tree.id "
        "  WHERE tree.depth < 32) "
        "SELECT DISTINCT r.id, r.status, r.created_at FROM workflow_runs r JOIN tree ON tree.id = r.id "
        "WHERE r.project_id = :p AND r.status <> ALL(:done) ORDER BY r.created_at"),
        {**params, "done": sorted(TERMINAL_RUN_STATES)}).mappings()]
    return items, runs


def _plan_pause(items: list[dict[str, Any]], runs: list[dict[str, Any]],
                mode: str) -> list[dict[str, Any]]:
    plan = [{"member_kind": "work_item", "member_id": i["id"], "prior_status": i["status"],
             "action": "held", "title": i.get("title"), "human_key": i.get("human_key")}
            for i in items]
    for r in runs:
        if r["status"] == "paused":
            action = "already_paused"
        else:
            action = "paused" if mode == "pause" else "left_running"
        plan.append({"member_kind": "workflow_run", "member_id": r["id"],
                     "prior_status": r["status"], "action": action})
    return plan


def _summary(plan: list[dict[str, Any]], key: str = "action") -> dict[str, int]:
    out: dict[str, int] = {}
    for m in plan:
        out[f"{m['member_kind']}:{m[key]}"] = out.get(f"{m['member_kind']}:{m[key]}", 0) + 1
    return out


# ── the two doors ───────────────────────────────────────────────────────────────────────────

def pause_tree(connection: Connection, *, project_id: str, root_kind: str, root_id: str,
               actor: str, mode: str = "pause", reason: str = "",
               dry_run: bool = False) -> dict[str, Any]:
    """Hold the tree under `root`. `dry_run` answers with the plan and changes nothing."""
    from . import workflows as wf

    if mode not in MODES:
        raise HoldError(f"mode is one of {', '.join(MODES)}")
    items, runs = _members(connection, project_id, root_kind, root_id)
    plan = _plan_pause(items, runs, mode)
    if dry_run:
        return {"dry_run": True, "root_kind": root_kind, "root_id": root_id, "mode": mode,
                "members": plan, "summary": _summary(plan)}
    if connection.execute(text(
            "SELECT 1 FROM run_holds WHERE root_kind = :k AND root_id = :r AND status = 'active'"),
            {"k": root_kind, "r": root_id}).first():
        raise HoldConflict("this tree is already on hold; restore it first")
    hold_id = make_id("hold")
    connection.execute(text(
        "INSERT INTO run_holds (id, project_id, root_kind, root_id, mode, reason, created_by) "
        "VALUES (:id, :p, :k, :r, :m, :why, :a)"),
        {"id": hold_id, "p": project_id, "k": root_kind, "r": root_id, "m": mode,
         "why": str(reason or "")[:500], "a": actor})
    for member in plan:
        action = member["action"]
        if action == "paused":
            try:
                with connection.begin_nested():
                    wf.pause_run(connection, project_id, member["member_id"], actor)
            except Exception:  # noqa: BLE001 — it ended between the read and the pause
                action = "left_running"
        connection.execute(text(
            "INSERT INTO run_hold_members (hold_id, member_kind, member_id, prior_status, action) "
            "VALUES (:h, :k, :i, :s, :a) ON CONFLICT DO NOTHING"),
            {"h": hold_id, "k": member["member_kind"], "i": member["member_id"],
             "s": member["prior_status"], "a": action})
        member["action"] = action
    emit_event(connection, project_id, "work_tree.held", actor, root_kind, root_id,
               {"hold_id": hold_id, "mode": mode, "summary": _summary(plan)})
    return {**get_hold(connection, project_id, hold_id), "summary": _summary(plan)}


def restore_tree(connection: Connection, *, project_id: str, hold_id: str, actor: str,
                 dry_run: bool = False) -> dict[str, Any]:
    """Release a hold and put the tree back as it was. `dry_run` answers with the plan."""
    from . import workflows as wf

    hold = _hold(connection, project_id, hold_id, lock=not dry_run)
    if hold["status"] != "active":
        raise HoldConflict("this hold was already restored")
    members = [dict(m) for m in connection.execute(text(
        "SELECT * FROM run_hold_members WHERE hold_id = :h ORDER BY member_kind, member_id"),
        {"h": hold_id}).mappings()]
    plan = []
    for m in members:
        if m["member_kind"] != "workflow_run":
            plan.append({**m, "restored_action": "kept"})
            continue
        now = connection.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                                 {"r": m["member_id"]}).scalar()
        if now is None:
            what = "gone"
        elif m["action"] != "paused":
            what = "left_paused" if m["action"] == "already_paused" else "kept"
        elif now != "paused":
            what = "kept"                       # someone moved it already (cancelled, finished)
        elif _held_elsewhere(connection, hold_id, m["member_id"]):
            what = "still_held"
        else:
            what = "resumed"
        plan.append({**m, "restored_action": what})
    if dry_run:
        return {"dry_run": True, "hold_id": hold_id, "members": plan,
                "summary": _summary(plan, "restored_action")}
    # The hold stops counting BEFORE the resumes, so resume_run's hold check lets them through.
    connection.execute(text(
        "UPDATE run_holds SET status = 'restored', restored_by = :a, restored_at = clock_timestamp() "
        "WHERE id = :h"), {"a": actor, "h": hold_id})
    for m in plan:
        what = m["restored_action"]
        if what == "resumed":
            try:
                with connection.begin_nested():
                    wf.resume_run(connection, project_id, m["member_id"], actor)
            except Exception as error:  # noqa: BLE001 — say it, keep going
                what = f"not_resumed: {str(error)[:120]}"
        connection.execute(text(
            "UPDATE run_hold_members SET restored_action = :w WHERE hold_id = :h "
            "AND member_kind = :k AND member_id = :i"),
            {"w": what[:200], "h": hold_id, "k": m["member_kind"], "i": m["member_id"]})
        m["restored_action"] = what
    # A start that waited only for the hold (work_items.defer_start) goes now; one that also
    # waits on a blocker keeps waiting for it. Paused runs are NOT touched here: only the runs
    # this hold paused were resumed above.
    from . import work_items

    for m in plan:
        if m["member_kind"] == "work_item":
            started = work_items.start_or_resume(connection, project_id=project_id,
                                                 item_id=m["member_id"], actor=actor,
                                                 resume_paused=False)
            if any(s.get("action") == "started" for s in started):
                m["restored_action"] = "started"
    summary = _summary(plan, "restored_action")
    connection.execute(text(
        "UPDATE run_holds SET restore_summary = CAST(:s AS JSONB) WHERE id = :h"),
        {"s": json.dumps(summary), "h": hold_id})
    emit_event(connection, project_id, "work_tree.restored", actor, hold["root_kind"],
               hold["root_id"], {"hold_id": hold_id, "summary": summary})
    return {**get_hold(connection, project_id, hold_id), "summary": summary}


def _held_elsewhere(connection: Connection, hold_id: str, run_id: str) -> bool:
    return bool(connection.execute(text(
        "SELECT 1 FROM run_hold_members m JOIN run_holds h ON h.id = m.hold_id "
        "WHERE h.status = 'active' AND h.id <> :h AND m.member_kind = 'workflow_run' "
        "AND m.member_id = :r"), {"h": hold_id, "r": run_id}).first())


# ── reading ─────────────────────────────────────────────────────────────────────────────────

def _hold(connection: Connection, project_id: str, hold_id: str, *, lock: bool = False) -> dict:
    row = connection.execute(text(
        "SELECT * FROM run_holds WHERE id = :h AND project_id = :p" + (" FOR UPDATE" if lock else "")),
        {"h": hold_id, "p": project_id}).mappings().first()
    if row is None:
        raise LookupError(f"no hold {hold_id} in this project")
    return dict(row)


def _public(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "project_id": row["project_id"], "root_kind": row["root_kind"],
            "root_id": row["root_id"], "mode": row["mode"], "status": row["status"],
            "reason": row["reason"], "created_by": row["created_by"],
            "created_at": _iso(row["created_at"]), "restored_by": row.get("restored_by"),
            "restored_at": _iso(row.get("restored_at"))}


def get_hold(connection: Connection, project_id: str, hold_id: str) -> dict[str, Any]:
    hold = _public(_hold(connection, project_id, hold_id))
    hold["members"] = [dict(m) for m in connection.execute(text(
        "SELECT member_kind, member_id, prior_status, action, restored_action "
        "FROM run_hold_members WHERE hold_id = :h ORDER BY member_kind, member_id"),
        {"h": hold_id}).mappings()]
    return hold


def list_holds(connection: Connection, project_id: str, *, active_only: bool = True) -> list[dict]:
    return [_public(r) for r in connection.execute(text(
        "SELECT * FROM run_holds WHERE project_id = :p AND (NOT :a OR status = 'active') "
        "ORDER BY created_at DESC LIMIT 200"), {"p": project_id, "a": active_only}).mappings()]


__all__ = ["pause_tree", "restore_tree", "get_hold", "list_holds", "item_hold", "held_items",
           "held_sql", "run_held_sql", "run_is_held", "HoldError", "HoldConflict"]
