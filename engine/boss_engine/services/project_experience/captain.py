"""The project's captain (contract §7): the agent that takes a project over once it exists.

Default: the project's `engineering_captain` (callsign Helm). Marshal (`chief_of_staff`) ranks
backlogs across projects and is never a project's captain by default. The choice is one row in
`project_captains`; a project with no row reads its default, so every project has a captain
without a write. Chat is the existing Ask BOSS dock with subject {kind: project, id} — one agent
path, no second chat engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import agents, face, href, iso

DEFAULT_ROLE = "engineering_captain"
CROSS_PROJECT = "chief_of_staff"

#: What each crew member does, in one word (the squad chips): THE source for the label AND its
#: tooltip on every surface (screen 04 crew row: Juno Plans, Forge Builds, Vera Tests, Beacon
#: Deploys, Marshal Ranks).
_DOES = {"pm": "Plans", "product_manager": "Plans", "product_owner": "Plans", "planner": "Plans",
         "architect": "Designs", "software_engineer": "Builds", "engineer": "Builds",
         "developer": "Builds", "frontend_engineer": "Builds", "backend_engineer": "Builds",
         "qa_engineer": "Tests", "qa": "Tests", "tester": "Tests", "reviewer": "Reviews",
         "code_reviewer": "Reviews", "security_engineer": "Secures", "sre": "Deploys",
         "site_reliability_engineer": "Deploys", "observability_engineer": "Watches",
         "adversarial_reviewer": "Reviews", "security_reviewer": "Secures",
         "chief_architect": "Designs", "software_engineer_backend": "Builds",
         "devops_engineer": "Ships", "release_manager": "Ships", "chief_of_staff": "Ranks",
         "engineering_captain": "Leads", "designer": "Designs", "ux_designer": "Designs",
         "technical_writer": "Documents"}


#: One line on what a captain-capable role does with a project (the captain picker).
_LINES = {"engineering_captain": "Owns the project end to end: onboards, plans, builds, ships",
          "chief_of_staff": "Ranks work across every project; not a project captain",
          "product_captain": "Leads from the product side: plans and priorities",
          "delivery_captain": "Leads delivery: keeps work moving to production"}


def default_role(roster: Mapping[str, Any]) -> Optional[str]:
    if DEFAULT_ROLE in roster:
        return DEFAULT_ROLE
    captains = sorted(k for k in roster if "captain" in k and k != CROSS_PROJECT)
    return captains[0] if captains else None


def chosen_role(connection: Connection, project_id: str,
                roster: Optional[Mapping[str, Any]] = None) -> tuple[Optional[str], str]:
    """(role_key, chosen | default)."""
    row = connection.execute(text(
        "SELECT role_key FROM project_captains WHERE project_id = :p"), {"p": project_id}).scalar()
    roster = roster if roster is not None else agents(connection, project_id)
    if row and str(row) in roster:
        return str(row), "chosen"
    return default_role(roster), "default"


def _status(connection: Connection, project_id: str) -> str:
    running = int(connection.execute(text(
        "SELECT count(*) FROM workflow_runs r JOIN workflow_definitions d ON "
        "d.id = r.workflow_definition_id WHERE r.project_id = :p AND d.namespace = 'product' "
        "AND r.status IN ('running', 'waiting', 'queued', 'validating', 'blocked', 'paused') "
        "AND COALESCE(r.mode, 'normal') <> 'test'"), {"p": project_id}).scalar() or 0)
    onboarding = connection.execute(text(
        "SELECT 1 FROM project_onboarding_runs WHERE project_id = :p AND state = 'running' "
        "LIMIT 1"), {"p": project_id}).first()
    if onboarding:
        return "Onboarding the project"
    if running:
        return f"Watching {running} build{'s' if running != 1 else ''}"
    return "Ready for work"


def squad(connection: Connection, project_id: str, roster: Mapping[str, Mapping[str, Any]],
          captain_key: Optional[str], limit: int = 5) -> list[dict[str, Any]]:
    """The crew: the project's CHOSEN team when someone chose one (Settings › Edit squad, the
    project_agents table at app "*", in the crew's reading order), else the agents who acted in
    this project most recently (agent_acts), else the project's delivery roles; never the
    captain itself. Editing the squad therefore changes the squad every surface shows (13-18)."""
    chosen = [str(k) for k in connection.execute(text(
        "SELECT role_key FROM project_agents WHERE project_id = :p AND app_key = '*' "
        "ORDER BY role_key"), {"p": project_id}).scalars().all()]
    # Plans · Builds · Tests · Deploys · Ranks (the crew row's order), whatever the role keys.
    order = ("product_manager", "pm", "software_engineer", "backend_engineer", "frontend_engineer",
             "qa_engineer", "qa", "sre", "chief_of_staff")
    keys = sorted(chosen, key=lambda k: (order.index(k) if k in order else len(order), k))
    if not keys:
        keys = [str(k) for k in connection.execute(text(
            "SELECT role_key FROM agent_acts WHERE project_id = :p AND role_key IS NOT NULL "
            "GROUP BY role_key ORDER BY max(started_at) DESC NULLS LAST LIMIT 12"),
            {"p": project_id}).scalars().all()]
    if not keys:
        keys = [k for k in ("product_manager", "software_engineer", "qa_engineer", "sre",
                            "chief_of_staff") if k in roster]
    # Marshal (the Chief of Staff) is not in a project's squad: it ranks across projects and is
    # drawn apart (`ranker`), so "4 in squad" counts this project's own crew.
    room = limit
    out = []
    for key in keys:
        if key == captain_key or key not in roster or key == CROSS_PROJECT:
            continue
        if len(out) >= room:
            break
        f = face(roster, key)
        out.append({"name": f["name"], "role_key": key,
                    "role": _DOES.get(key) or roster[key]["role"], "avatar": f["avatar"],
                    "agent_id": f.get("agent_id")})
    return out


def ranker(roster: Mapping[str, Mapping[str, Any]], captain_key: Optional[str]
           ) -> Optional[dict[str, Any]]:
    """04-34 Marshal (the Chief of Staff), who closes the crew row: it ranks this project's
    backlog against the others. None when the org has no Chief of Staff."""
    if CROSS_PROJECT not in roster or captain_key == CROSS_PROJECT:
        return None
    f = face(roster, CROSS_PROJECT)
    return {"name": f["name"], "role_key": CROSS_PROJECT, "role": _DOES[CROSS_PROJECT],
            "avatar": f["avatar"], "agent_id": f.get("agent_id"),
            "title": "Chief of Staff ranks this project's backlog against the others"}


def last_said(connection: Connection, project_id: str, role_key: Optional[str]
              ) -> Optional[dict[str, Any]]:
    if not role_key:
        return None
    row = connection.execute(text(
        "SELECT act_id, label, why, started_at AS at FROM agent_acts WHERE project_id = :p "
        "AND role_key = :r ORDER BY started_at DESC LIMIT 1"),
        {"p": project_id, "r": role_key}).mappings().first()
    if not row:
        return None
    return {"act_id": row["act_id"], "label": row["label"], "detail": row["why"],
            "at": iso(row["at"]),
            "href": href(project_id, area="agent_ops", act=row["act_id"])}


def _short(text_: Any) -> str:
    """An act's label in words: "Build · <long brief>" → "Build · Finish the visual refresh"."""
    raw = str(text_ or "").strip()
    head, _sep, rest = raw.partition(" · ")
    try:
        from .titles import short_title
    except ImportError:  # pragma: no cover
        return raw[:60]
    if rest:
        return f"{head} · {short_title(rest, limit=5)}"
    return short_title(raw, limit=6) or raw[:60]


def recent(connection: Connection, project_id: str, role_keys: list[str], limit: int = 5
           ) -> list[dict[str, Any]]:
    """What the captain and its crew did last here (agent_acts), newest first: the drawer's
    "Lately" list before a conversation starts."""
    if not role_keys:
        return []
    rows = connection.execute(text(
        "SELECT act_id, role_key, label, kind, status, started_at, ended_at, code_task_run_id, "
        "workflow_run_id FROM agent_acts WHERE project_id = :p AND role_key = ANY(:keys) "
        "AND COALESCE(label, '') <> '' ORDER BY started_at DESC NULLS LAST LIMIT :n"),
        {"p": project_id, "keys": role_keys, "n": limit * 3}).mappings().all()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        label = _short(r["label"])
        if label in seen:
            continue
        seen.add(label)
        out.append({"act_id": r["act_id"], "role_key": r["role_key"], "label": label,
                    "state": "now" if r["status"] == "active" else
                    "failed" if r["status"] in ("failed", "error") else "done",
                    "at": iso(r["ended_at"] or r["started_at"]),
                    "href": href(project_id, area="agent_ops", act=r["act_id"])})
        if len(out) >= limit:
            break
    return out


def facts(connection: Connection, project_id: str) -> int:
    """How much the captain knows about this project: its knowledge-graph nodes."""
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL"),
                {"p": project_id}).scalar() or 0)
    except Exception:  # noqa: BLE001 — a count is a nicety; the drawer opens without it
        return 0


def starters(connection: Connection, project_id: str) -> list[dict[str, str]]:
    """The drawer's suggestion chips (screen 09): grounded in what is on record — a merge
    waiting on the person names its PR; cost is always answerable; planning is always offered."""
    out = [{"label": "Plan next week", "message": "Plan next week for this project.",
            "icon": "map"}]
    pr = connection.execute(text(
        "SELECT pull_request_number FROM code_task_runs c JOIN decisions d ON d.id = c.decision_id "
        "WHERE c.project_id = :p AND d.status = 'pending' AND c.pull_request_number IS NOT NULL "
        "ORDER BY c.created_at DESC LIMIT 1"), {"p": project_id}).scalar()
    if pr:
        out.append({"label": f"Why is PR #{pr} waiting?",
                    "message": f"Why is PR #{pr} waiting, and what do I need to do?",
                    "icon": "question"})
    else:
        out.append({"label": "What's left to demo?",
                    "message": "What's left before I can demo this project?", "icon": "question"})
    out.append({"label": "What did this cost?",
                "message": "What did this project cost this week, and on what?", "icon": "coins"})
    return out


def view(connection: Connection, project_id: str, *, roster: Optional[Mapping[str, Any]] = None,
         with_last_said: bool = True) -> dict[str, Any]:
    """§2 / §7 captain: {agent_id, name, role_key, avatar, status, squad, chosen, ask, last_said}."""
    roster = roster if roster is not None else agents(connection, project_id)
    key, how = chosen_role(connection, project_id, roster)
    if key is None:
        return {"agent_id": None, "name": None, "role_key": None, "avatar": None,
                "status": "No captain agent in this project", "squad": [], "chosen": "none",
                "ask": None, "last_said": None, "candidates": _candidates(roster)}
    f = face(roster, key)
    out = {"agent_id": f.get("agent_id") or key, "name": f["name"], "role_key": key,
           "role": roster[key]["role"], "avatar": f["avatar"],
           "status": _status(connection, project_id),
           "squad": squad(connection, project_id, roster, key), "ranker": ranker(roster, key),
           "chosen": how,
           # The dock, scoped to the project, talking as its captain (one agent path).
           "ask": {"subject": {"kind": "project", "id": project_id}, "role_key": key,
                   "href": href(project_id, area="project", drawer="captain")},
           "candidates": _candidates(roster)}
    if with_last_said:
        # THE CAPTAIN DRAWER (screen 09): scope, what it knows, what it did lately, starters.
        out["last_said"] = last_said(connection, project_id, key)
        name = connection.execute(text("SELECT name FROM projects WHERE id = :p"),
                                  {"p": project_id}).scalar()
        out["scope"] = {"label": f"{name or project_id} only", "project_id": project_id}
        out["facts"] = facts(connection, project_id)
        out["recent"] = recent(connection, project_id,
                               [key] + [m["role_key"] for m in out["squad"]])
        out["starters"] = starters(connection, project_id)
    return out


def _candidates(roster: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Agents a person may make captain: any active agent, the captain roles first."""
    def rank(k: str) -> tuple:
        return (k != DEFAULT_ROLE, "captain" not in k, k)
    return [{"agent_id": roster[k]["agent_id"], "role_key": k, "name": roster[k]["name"],
             "role": roster[k]["role"], "avatar": roster[k]["avatar"],
             # THE PICKER (PX-13): captain roles first, the default one recommended, each with
             # one line on what it does here.
             "captain_capable": "captain" in k and k != CROSS_PROJECT,
             "recommended": k == DEFAULT_ROLE,
             "line": _LINES.get(k) or (f"{_DOES[k]} on this project" if k in _DOES else
                                       "Can lead this project")}
            for k in sorted(roster, key=rank) if roster[k].get("lifecycle") != "retired"][:60]


class CaptainRefused(ValueError):
    """The named agent is not one of this project's active agents."""


def assign(connection: Connection, project_id: str, agent: str, *, actor: str) -> dict[str, Any]:
    """Make `agent` (an agent id or a role key of THIS project) the project's captain."""
    from ..ambient._common import emit

    wanted = str(agent or "").strip()
    roster = agents(connection, project_id)
    key = next((k for k, a in roster.items()
                if wanted and (a["agent_id"] == wanted or k == wanted.lower())), None)
    if key is None:
        raise CaptainRefused(f"{wanted or 'That agent'} is not an agent of this project.")
    if roster[key].get("lifecycle") == "retired":
        raise CaptainRefused(f"{roster[key]['name']} is retired; pick an active agent.")
    before, _how = chosen_role(connection, project_id, roster)
    connection.execute(text(
        "INSERT INTO project_captains (project_id, role_key, assigned_by, assigned_at) "
        "VALUES (:p, :r, :a, :t) ON CONFLICT (project_id) DO UPDATE SET role_key = EXCLUDED.role_key, "
        "assigned_by = EXCLUDED.assigned_by, assigned_at = EXCLUDED.assigned_at"),
        {"p": project_id, "r": key, "a": actor, "t": datetime.now(timezone.utc)})
    emit(connection, project_id, "project.captain_changed", actor, "project", project_id,
         {"from": before, "to": key})
    return view(connection, project_id, roster=roster)


def ensure_default(connection: Connection, project_id: str, *, actor: str = "boss"
                   ) -> Optional[str]:
    """On project creation: record the default captain (idempotent; never overrides a choice)."""
    roster = agents(connection, project_id)
    key = default_role(roster)
    if key is None:
        return None
    connection.execute(text(
        "INSERT INTO project_captains (project_id, role_key, assigned_by) VALUES (:p, :r, :a) "
        "ON CONFLICT (project_id) DO NOTHING"), {"p": project_id, "r": key, "a": actor})
    return key


__all__ = ["CROSS_PROJECT", "CaptainRefused", "DEFAULT_ROLE", "assign", "chosen_role",
           "default_role", "ensure_default", "facts", "last_said", "recent", "squad", "starters",
           "view"]
