"""Who is on this piece of work.

BOSS could not answer that. `roles` says an agent exists in a project; a workflow step names an
`approver_role` and nothing else; `workflow_runs` and `workflow_step_runs` have no role column
at all. The only implicit assignment is `llm_telemetry.STAGE_ROLES`, which maps a pipeline stage
to a role for COST ATTRIBUTION at write time and is never consulted for scheduling or for
permission. So the console listed 26 agents and could not say which of them were on the app in
front of you, and a workflow ran whatever agent its stage happened to imply.

TWO DECISIONS WORTH STATING.

Assignment is keyed on the APP, not on a run. An app gets re-run, retried and re-deployed, and
an assignment that evaporated on the second run would be worse than no assignment — the
operator would set it, watch it work, and find it gone. `app_key` is `identity_key` of the app's
name, the same comparison the rest of BOSS uses to decide two names are one app.

Assignment is ENFORCED, and it therefore ships with defaults. If a project started with nobody
assigned, every run would refuse and the feature would read as breakage rather than governance.
So a new app is seeded with exactly the roles that would have done the work anyway — the
STAGE_ROLES set plus whatever the workflow names as an approver — and enforcement applies on top
of that. Assignment then CHANGES behaviour without breaking the default path, which is the only
version of enforcement worth having.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from sqlalchemy import text as sql

from .core import make_id
from .naming import identity_key

log = logging.getLogger(__name__)

#: Where a row came from. `default` was seeded; `operator` was chosen by a person, and is never
#: removed by re-seeding.
DEFAULT, OPERATOR = "default", "operator"


def default_roles(spec: Optional[dict[str, Any]] = None) -> list[str]:
    """The agents that would have done this work anyway.

    STAGE_ROLES is the map BOSS already uses to attribute every model call to an agent, so it is
    the honest answer to "who runs this" — it is what actually happened, not a guess. The
    workflow's own approver roles are added because a gate naming `release_manager` and a
    project that has not assigned them is precisely the refusal this seeding exists to avoid.
    """
    from .llm_telemetry import STAGE_ROLES, canonical_role

    roles = {canonical_role(r) for r in STAGE_ROLES.values()}
    for step in ((spec or {}).get("steps") or []):
        if not isinstance(step, dict):
            continue
        approver = ((step.get("approval") or {}).get("approver_role") or "").strip()
        if approver:
            roles.add(approver)
    return sorted(r for r in roles if r)


def assigned(connection, project_id: str, app_key: str) -> list[dict[str, Any]]:
    """Everyone on this app, with where each assignment came from."""
    rows = connection.execute(
        sql("SELECT role_key, source, assigned_by, created_at, app_name FROM project_agents "
            "WHERE project_id = :p AND app_key = :a ORDER BY role_key"),
        {"p": project_id, "a": app_key},
    ).mappings().all()
    return [dict(r) for r in rows]


def assign(connection, *, project_id: str, app_name: str, role_keys: Iterable[str],
           actor: str, source: str = OPERATOR) -> dict[str, Any]:
    """Put agents on an app. Idempotent, and it never demotes an operator's choice.

    A re-seed that overwrote `operator` with `default` would quietly undo a decision somebody
    made — the same class of bug as a target picker that forgets what it was told.
    """
    key = identity_key(app_name)
    if not key:
        return {"ok": False, "error": "an assignment needs an app to be about"}
    added: list[str] = []
    for role_key in role_keys:
        role_key = str(role_key or "").strip()
        if not role_key:
            continue
        result = connection.execute(
            sql("""
                INSERT INTO project_agents
                    (id, project_id, app_key, app_name, role_key, source, assigned_by)
                VALUES (:i, :p, :a, :n, :r, :s, :by)
                ON CONFLICT (project_id, app_key, role_key) DO UPDATE
                    SET source = CASE WHEN project_agents.source = 'operator'
                                      THEN 'operator' ELSE EXCLUDED.source END,
                        app_name = EXCLUDED.app_name
                RETURNING (xmax = 0) AS inserted
                """),
            {"i": make_id("pag"), "p": project_id, "a": key, "n": app_name.strip(),
             "r": role_key, "s": source, "by": actor},
        ).mappings().first()
        if result and result["inserted"]:
            added.append(role_key)
    return {"ok": True, "app_key": key, "added": added,
            "assigned": [r["role_key"] for r in assigned(connection, project_id, key)]}


def unassign(connection, *, project_id: str, app_name: str, role_key: str) -> dict[str, Any]:
    """Take an agent off an app.

    Allowed even for a seeded default: a project that genuinely does not want a security
    reviewer should be able to say so, and the refusal it causes later will name the reason.
    """
    key = identity_key(app_name)
    removed = connection.execute(
        sql("DELETE FROM project_agents WHERE project_id = :p AND app_key = :a "
            "AND role_key = :r RETURNING role_key"),
        {"p": project_id, "a": key, "r": role_key},
    ).first()
    return {"ok": bool(removed), "app_key": key, "role_key": role_key,
            "assigned": [r["role_key"] for r in assigned(connection, project_id, key)]}


def ensure_defaults(connection, *, project_id: str, app_name: str, actor: str = "boss",
                    spec: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Seed an app that has nobody on it. Does nothing to one that already has somebody.

    Called when a run starts rather than at project creation, so an app made before this feature
    existed is not stuck permanently unassignable — and so enforcement cannot refuse work on the
    grounds that a table was added after the app was.
    """
    key = identity_key(app_name)
    if not key:
        return {"ok": False, "error": "no app name"}
    existing = assigned(connection, project_id, key)
    if existing:
        return {"ok": True, "seeded": [], "assigned": [r["role_key"] for r in existing]}
    return {**assign(connection, project_id=project_id, app_name=app_name,
                     role_keys=default_roles(spec), actor=actor, source=DEFAULT),
            "seeded": default_roles(spec)}


def may_act(connection, *, project_id: str, app_name: str, role_key: str) -> dict[str, Any]:
    """Is this agent on this app?

    An app with NO assignment at all answers yes. That is deliberate: enforcement should stop
    work that contradicts a decision somebody made, not work on something nobody has decided
    about yet. Refusing by default would turn every pre-existing app into a broken one.
    """
    key = identity_key(app_name)
    role_key = str(role_key or "").strip()
    if not key or not role_key:
        return {"allowed": True, "reason": "nothing to check"}
    rows = assigned(connection, project_id, key)
    if not rows:
        return {"allowed": True, "reason": "no agents are assigned to this app yet"}
    if any(r["role_key"] == role_key for r in rows):
        return {"allowed": True, "reason": "assigned"}
    return {
        "allowed": False,
        "reason": (f"'{role_key}' is not assigned to {app_name}. Assign them on the workflow, "
                   f"or choose one of: {', '.join(r['role_key'] for r in rows[:8])}."),
        "assigned": [r["role_key"] for r in rows],
    }


def apps_for_role(connection, project_id: str, role_key: str) -> list[dict[str, Any]]:
    """Every app this agent is assigned to. The other half of the agent detail page."""
    rows = connection.execute(
        sql("SELECT app_key, app_name, source, created_at FROM project_agents "
            "WHERE project_id = :p AND role_key = :r ORDER BY app_name"),
        {"p": project_id, "r": role_key},
    ).mappings().all()
    return [dict(r) for r in rows]


__all__ = ["assign", "unassign", "assigned", "ensure_defaults", "may_act", "apps_for_role",
           "default_roles", "DEFAULT", "OPERATOR"]
