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

#: What each crew member does, in one word (the squad chips).
_DOES = {"product_manager": "Plans", "product_owner": "Plans", "planner": "Plans",
         "architect": "Designs", "software_engineer": "Builds", "engineer": "Builds",
         "developer": "Builds", "frontend_engineer": "Builds", "backend_engineer": "Builds",
         "qa_engineer": "Tests", "qa": "Tests", "tester": "Tests", "reviewer": "Reviews",
         "code_reviewer": "Reviews", "security_engineer": "Secures", "sre": "Watches",
         "site_reliability_engineer": "Watches", "observability_engineer": "Watches",
         "devops_engineer": "Ships", "release_manager": "Ships", "chief_of_staff": "Ranks",
         "engineering_captain": "Leads", "designer": "Designs", "ux_designer": "Designs",
         "technical_writer": "Documents"}


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
    """The crew: the agents who acted in this project most recently (agent_acts), else the
    project's delivery roles, never the captain itself."""
    keys = [str(k) for k in connection.execute(text(
        "SELECT role_key FROM agent_acts WHERE project_id = :p AND role_key IS NOT NULL "
        "GROUP BY role_key ORDER BY max(started_at) DESC NULLS LAST LIMIT 12"),
        {"p": project_id}).scalars().all()]
    if not keys:
        keys = [k for k in ("product_manager", "software_engineer", "qa_engineer", "sre",
                            "chief_of_staff") if k in roster]
    out = []
    for key in keys:
        if key == captain_key or key not in roster:
            continue
        f = face(roster, key)
        out.append({"name": f["name"], "role_key": key,
                    "role": _DOES.get(key) or roster[key]["role"], "avatar": f["avatar"],
                    "agent_id": f.get("agent_id")})
        if len(out) >= limit:
            break
    return out


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
           "squad": squad(connection, project_id, roster, key), "chosen": how,
           # The dock, scoped to the project, talking as its captain (one agent path).
           "ask": {"subject": {"kind": "project", "id": project_id}, "role_key": key,
                   "href": href(project_id, area="project", drawer="captain")},
           "candidates": _candidates(roster)}
    if with_last_said:
        out["last_said"] = last_said(connection, project_id, key)
    return out


def _candidates(roster: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Agents a person may make captain: any active agent, the captain roles first."""
    def rank(k: str) -> tuple:
        return (k != DEFAULT_ROLE, "captain" not in k, k)
    return [{"agent_id": roster[k]["agent_id"], "role_key": k, "name": roster[k]["name"],
             "role": roster[k]["role"], "avatar": roster[k]["avatar"]}
            for k in sorted(roster, key=rank) if roster[k].get("lifecycle") != "retired"][:30]


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
           "default_role", "ensure_default", "last_said", "squad", "view"]
