"""The org chart somebody DECIDED, over the one BOSS observed.

The chart was derived entirely from handoffs — who passed work to whom in the last week. That
is honest, and useful, and completely unchangeable: an operator looking at it could not say
"the QA lead reports to the captain" or "add a security reviewer to this team", because the
chart was a measurement rather than a structure. A measurement nobody can act on is a report.

So: declared edges, stored per project, laid over the observed ones. Where the two disagree is
the most interesting thing on the page — an agent who is supposed to report to the captain and
in practice hands everything to the architect is a fact worth seeing, and averaging the two
would hide it.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from .core import emit_event


class OrgCycle(ValueError):
    """Somebody would report to themselves, through however many hops."""


def get_all(connection, project_id: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        text("SELECT agent_id, reports_to, display_name, sort FROM org_assignments "
             "WHERE project_id = :p ORDER BY sort, agent_id"),
        {"p": project_id},
    ).mappings().all()
    return {r["agent_id"]: dict(r) for r in rows}


def _would_cycle(edges: dict[str, Optional[str]], child: str, parent: Optional[str]) -> bool:
    """Following `parent` upwards, do we arrive back at `child`?

    Checked BEFORE the write rather than repaired after: a cycle in an org chart is not a
    display bug, it is a tree that cannot be laid out, and the renderer would either recurse
    forever or silently drop somebody.
    """
    seen: set[str] = {child}
    cursor = parent
    while cursor:
        if cursor in seen:
            return True
        seen.add(cursor)
        cursor = edges.get(cursor)
    return False


def set_one(
    connection, project_id: str, agent_id: str, *,
    reports_to: Optional[str] = None, display_name: Optional[str] = None,
    sort: int = 0, actor: str = "operator",
) -> dict[str, Any]:
    agent_id = (agent_id or "").strip()
    if not agent_id:
        raise ValueError("an agent id is required")
    parent = (reports_to or "").strip() or None
    if parent == agent_id:
        raise OrgCycle(f"'{agent_id}' cannot report to itself.")

    edges = {k: v["reports_to"] for k, v in get_all(connection, project_id).items()}
    # A PARENT THAT IS NOT IN THE PLAN IS PUT IN IT, AT THE TOP.
    #
    # Team structure's "Reports to" offered roles from the OBSERVED tree when nothing was
    # planned, and nothing here checked the parent was planned. The child was saved reporting to
    # a role the chart did not contain, so it rendered at the top level while its "Reports to"
    # column named a parent — two answers to one question. Placing the parent (top level, where
    # it would render anyway) makes the plan say what the chart shows.
    place_parent = bool(parent) and parent not in edges
    edges[agent_id] = parent
    if place_parent:
        edges[parent] = None
    if parent and _would_cycle(edges, agent_id, parent):
        raise OrgCycle(
            f"'{agent_id}' already sits above '{parent}', so this would make a loop."
        )
    if place_parent:
        connection.execute(
            text("INSERT INTO org_assignments "
                 "  (project_id, agent_id, reports_to, sort, updated_by, updated_at) "
                 "VALUES (:p, :a, NULL, 0, :by, now()) ON CONFLICT (project_id, agent_id) "
                 "DO NOTHING"),
            {"p": project_id, "a": parent, "by": actor},
        )
        emit_event(
            connection, project_id, "org.assignment_set", actor, "role", parent,
            {"reports_to": None, "why": f"placed at the top so '{agent_id}' can report to it"},
        )

    connection.execute(
        text("INSERT INTO org_assignments "
             "  (project_id, agent_id, reports_to, display_name, sort, updated_by, updated_at) "
             "VALUES (:p, :a, :r, :d, :s, :by, now()) "
             "ON CONFLICT (project_id, agent_id) DO UPDATE SET "
             "  reports_to = EXCLUDED.reports_to, "
             "  display_name = COALESCE(EXCLUDED.display_name, org_assignments.display_name), "
             "  sort = EXCLUDED.sort, updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"p": project_id, "a": agent_id, "r": parent,
         "d": (display_name or "").strip() or None, "s": int(sort), "by": actor},
    )
    emit_event(
        connection, project_id, "org.assignment_set", actor, "role", agent_id,
        {"reports_to": parent, "display_name": display_name},
    )
    return {"agent_id": agent_id, "reports_to": parent, "display_name": display_name}


def clear_one(connection, project_id: str, agent_id: str, actor: str = "operator") -> None:
    """Hand this agent back to the observed chart.

    Deleting rather than storing "no parent": a NULL parent is a real statement — this agent is
    a root — and it has to stay distinguishable from "nobody has said".
    """
    deleted = connection.execute(
        text("DELETE FROM org_assignments WHERE project_id = :p AND agent_id = :a"),
        {"p": project_id, "a": agent_id},
    ).rowcount
    if deleted:
        emit_event(
            connection, project_id, "org.assignment_cleared", actor, "role", agent_id,
            {"count": deleted},
        )


def clear_all(connection, project_id: str, actor: str = "operator") -> int:
    """Back to the observed chart entirely. Returns how many statements were dropped."""
    deleted = connection.execute(
        text("DELETE FROM org_assignments WHERE project_id = :p"), {"p": project_id},
    ).rowcount
    if deleted:
        emit_event(
            connection, project_id, "org.structure_cleared", actor, "project", project_id,
            {"count": deleted},
        )
    return deleted


def apply_to(chart: dict[str, Any], declared: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Lay the declared chart over the observed one.

    Both are kept. `edges` still carries what was observed, so the console can draw the two
    differently, and every node says whether its position was decided or measured — which is
    the only way "these disagree" can be seen at all.
    """
    if not declared:
        return {
            **chart,
            "observed_tree": chart.get("tree") or [],
            "declared": {},
            "has_declared": False,
        }

    nodes = [dict(n) for n in (chart.get("nodes") or [])]
    known = {n.get("id") for n in nodes}
    # An agent somebody placed but BOSS has never seen work is still part of the org: it is a
    # seat that has been created and not yet filled, and dropping it would make the editor
    # appear to forget what it was told.
    for agent_id, row in declared.items():
        if agent_id in known:
            continue
        nodes.append({
            "id": agent_id,
            "name": row.get("display_name") or agent_id.replace("_", " ").title(),
            "role": row.get("display_name") or agent_id.replace("_", " ").title(),
            "status": "UNSEEN", "task": "no work observed yet",
            "model": None, "tokens": None, "spend_usd": None, "calls": None,
            "connected": True,
        })

    by_id = {n["id"]: n for n in nodes}
    for agent_id, row in declared.items():
        node = by_id.get(agent_id)
        if node is None:
            continue
        if row.get("display_name"):
            node["name"] = row["display_name"]
        node["reports_to"] = row.get("reports_to")
        node["placed"] = True

    # Depth from the declared edges, so the console can lay it out without re-deriving.
    def depth_of(agent_id: str, guard: int = 0) -> int:
        parent = (declared.get(agent_id) or {}).get("reports_to")
        if not parent or guard > 24:
            return 0
        return 1 + depth_of(parent, guard + 1)

    tree = [
        {**by_id[a], "depth": depth_of(a)}
        for a in declared
        if a in by_id
    ]
    tree.sort(key=lambda n: (n["depth"], n.get("name") or ""))
    return {
        **chart,
        "observed_tree": chart.get("tree") or [],
        "nodes": nodes,
        "tree": tree,
        "declared": {k: dict(v) for k, v in declared.items()},
        "has_declared": True,
        "note": "Declared by your team. Handoffs BOSS observed are drawn underneath.",
    }
