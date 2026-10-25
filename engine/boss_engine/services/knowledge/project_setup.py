"""A knowledge-graph project's guided setup: where it is, and what comes next (BOSSAPP-768).

ONE READ FOR THE CONSOLE AND THE AGENT. The Knowledge Graph area's stepper (GET
/v1/knowledge/setup) and the agent's `knowledge_project_setup` tool both return `status`, so
"what's left to set up?" has one answer whether it is asked by a screen or in a sentence. The
steps write through the services that already exist, never a second implementation:

    1 import   the org registry's repositories, added to the project (multirepo.project_repos;
               the `register_repository` tool) and indexed into the code graph by the knowledge
               tick (knowledge/repo_index.py; `code_graph_status`, `index_repository_code`)
    2 team     the BOSS agents on this project: the project's own team (project_agents, app
               "*"), stated by a person (agent_assignment.set_team; `set_project_team`)
    3 scan     a first business-rules scan of one area (the business_rules_scan workflow;
               `start_workflow`), with a budget and a cost preview
    4 review   the proposed rules, approved or rejected by a person in Rules review
    5 done     the Knowledge Graph page

Every step's state is DERIVED from the records those services write, so the setup is resumable by
construction: leave after step 2 and the next visit opens on step 3, from any tab or any agent.
Nothing here is stored about the setup itself.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import repo_index
from .scopes import LEARNED_KINDS

#: The steps, in order (the console's ①–④, then Done).
STEPS: tuple[str, ...] = ("import", "team", "scan", "review")

#: What each agent does in a knowledge-graph project: the recommended team, offered first and
#: preselected when the project has that agent. A product default for the flow, the same for
#: every project — never a gate: any of the project's agents may be chosen. Keys are the shipped
#: role keys (roles.role_key); an agent a project does not have is simply not offered.
TEAM_JOBS: tuple[tuple[str, str, str], ...] = (
    ("chief_architect", "scans",
     "Scans an area of the code for business rules and proposes each one with the lines that "
     "prove it"),
    ("software_engineer", "changes",
     "Makes changes to the code, reading the graph and the approved rules first"),
    ("sre", "incidents",
     "Investigates incidents with the graph: what broke, what calls it, what fixed it before"),
    ("incident_commander", "incidents",
     "Runs an incident: timeline, containment and recovery, with precedent from the graph"),
)
JOB_OF: dict[str, tuple[str, str]] = {k: (job, line) for k, job, line in TEAM_JOBS}

#: The workflow the scan step starts, and its defaults (node-panel's Scan for rules here).
SCAN_WORKFLOW = "business_rules_scan"
SCAN_DEFAULTS = {"max_files": 60, "max_rules": 25, "budget_usd": 2.0}

_LIVE_SCAN = ("queued", "running", "pending", "waiting", "paused", "blocked")


def _project(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, name, project_type, archived_at FROM projects WHERE id = :p"),
        {"p": project_id}).mappings().first()
    return dict(row) if row else None


_WORK_REF = ("coalesce(attrs->'provenance'->>'workflow_run_id', attrs->'provenance'->>'run_id', "
             "attrs->'provenance'->>'thread_id')")


def _work_counts(connection: Connection, project_id: str) -> dict[str, int]:
    """WHAT EACH AGENT DID HERE, counted (KG tester round 5: "Trust 50 · 0 runs" on the Chief
    Architect, who proposed all 41 rules, and on the SRE, who wrote the INC-2088 root cause):
    `roles.successful_runs` grows only with a governance review, so a card also counts each
    conversation the agent held in this project and each run in which it wrote to the graph —
    once each, however often it is seen."""
    rows = connection.execute(text(
        "SELECT r, count(*) AS n FROM ("
        "  SELECT agent_role AS r, 'thread:' || id AS ref FROM agent_threads "
        "   WHERE project_id = :p AND coalesce(agent_role, '') <> '' "
        "  UNION "
        f"  SELECT attrs->'provenance'->>'agent_role', 'run:' || {_WORK_REF} FROM kg_nodes "
        "   WHERE project_id = :p AND coalesce(attrs->'provenance'->>'agent_role', '') <> '' "
        f"     AND {_WORK_REF} IS NOT NULL"
        ") x GROUP BY r"), {"p": project_id}).mappings().all()
    return {str(r["r"]): int(r["n"]) for r in rows}


def _candidates(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """The project's agents, the recommended ones first, each with its Passport's essentials
    (who it is, its mission, trust, runs, and the fingerprint its Passport carries)."""
    rows = connection.execute(text(
        "SELECT role_key, display_name, trust_score, successful_runs, autonomy_level, "
        "       lifecycle, fingerprint, spec->>'mission' AS mission "
        "FROM roles WHERE project_id = :p AND coalesce(lifecycle, 'active') <> 'retired' "
        "ORDER BY display_name"), {"p": project_id}).mappings().all()
    order = {k: i for i, (k, _j, _l) in enumerate(TEAM_JOBS)}
    work = _work_counts(connection, project_id)
    out = []
    for r in rows:
        job = JOB_OF.get(r["role_key"])
        out.append({
            "role_key": r["role_key"], "display_name": r["display_name"],
            "job": job[0] if job else None, "does": job[1] if job else None,
            "recommended": job is not None,
            "mission": r["mission"], "trust": int(r["trust_score"] or 0),
            "runs": int(r["successful_runs"] or 0) + work.get(r["role_key"], 0), "autonomy": int(r["autonomy_level"] or 0),
            "passport": (r["fingerprint"] or "")[:12] or None,
        })
    out.sort(key=lambda c: (order.get(c["role_key"], len(order)), c["display_name"] or ""))
    return out


def _team(connection: Connection, project_id: str) -> dict[str, Any]:
    from ..agent_assignment import PROJECT_DEFAULT, project_team

    rows = connection.execute(text(
        "SELECT role_key, source FROM project_agents WHERE project_id = :p AND app_key = :a "
        "ORDER BY role_key"), {"p": project_id, "a": PROJECT_DEFAULT}).mappings().all()
    chosen = [r["role_key"] for r in rows if r["source"] == "operator"]
    return {"chosen": bool(chosen),
            "role_keys": chosen or project_team(connection, project_id)}


def _scans(connection: Connection, project_id: str) -> dict[str, Any]:
    rows = connection.execute(text(
        "SELECT r.id, r.status, r.created_at, r.completed_at, r.input->'scope' AS scope "
        "FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        "WHERE r.project_id = :p AND d.workflow_key = :k ORDER BY r.created_at DESC LIMIT 5"),
        {"p": project_id, "k": SCAN_WORKFLOW}).mappings().all()
    total = connection.execute(text(
        "SELECT count(*) FROM workflow_runs r JOIN workflow_definitions d "
        "ON d.id = r.workflow_definition_id WHERE r.project_id = :p AND d.workflow_key = :k"),
        {"p": project_id, "k": SCAN_WORKFLOW}).scalar() or 0
    latest = None
    if rows:
        r = rows[0]
        latest = {"run_id": r["id"], "status": r["status"],
                  "at": r["created_at"].isoformat() if r["created_at"] else None,
                  "scope": r["scope"]}
    return {"count": int(total), "latest": latest,
            "running": sum(1 for r in rows if str(r["status"]) in _LIVE_SCAN),
            "completed": sum(1 for r in rows if str(r["status"]) == "completed")}


def _rules(connection: Connection, project_id: str) -> dict[str, int]:
    counts = {"proposed": 0, "approved": 0, "rejected": 0}
    for status, n in connection.execute(text(
            "SELECT coalesce(attrs->>'status', 'proposed'), count(*) FROM kg_nodes "
            "WHERE project_id = :p AND kind = 'rule' AND valid_to IS NULL GROUP BY 1"),
            {"p": project_id}).all():
        if status in counts:
            counts[status] = int(n)
    return counts


def _learnings(connection: Connection, project_id: str) -> int:
    return int(connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL "
        "AND kind = ANY(:k)"), {"p": project_id, "k": sorted(LEARNED_KINDS)}).scalar() or 0)


def _areas(connection: Connection, project_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """The largest indexed areas: what the first scan can be pointed at, keyed exactly as the
    tree keys them (`area:<repo>:<area>`, the scan's scope), largest first. Empty until the view
    rollup is built (view_rollup.rebuild runs at the end of an index)."""
    if connection.execute(text("SELECT to_regclass('kg_view_tree')")).scalar() is None:
        return []
    rows = connection.execute(text(
        "SELECT key, label, repo, class_count, child_count FROM kg_view_tree "
        "WHERE project_id = :p AND level = 'area' ORDER BY class_count DESC, label LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    return [{"key": r["key"], "label": r["label"], "repo": r["repo"],
             "classes": int(r["class_count"] or 0), "packages": int(r["child_count"] or 0)}
            for r in rows]


def _repo_line(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("repo", "branch", "status", "summary", "error", "files", "parsed",
                                  "indexed_at", "graph")}


def status(connection: Connection, project_id: str) -> dict[str, Any]:
    """Where this project's knowledge graph setup is: each step's state (done, working, current,
    waiting) with the real counts behind it, the team and its candidates, and `next` — the first
    step left to do, or None when the graph is set up. Raises LookupError for no such project."""
    project = _project(connection, project_id)
    if project is None:
        raise LookupError(f"There is no project {project_id}.")
    repos = [_repo_line(r) for r in repo_index.status(connection, project_id)["repositories"]]
    indexed = [r for r in repos if r["status"] == "indexed" and ((r.get("graph") or {}).get("nodes") or 0) > 0]
    indexing = [r for r in repos if r["status"] in ("queued", "running", "pending")]
    nodes = sum(((r.get("graph") or {}).get("nodes") or 0) for r in repos)
    # THE ONE ANSWER TO "IS THERE INDEXED CODE?" (KG tester round 4). Import code's Done, the scan
    # gate, the graph's Open button and the console's strip all read this, so the stepper can
    # never say "1 of 1 repositories indexed" beside "Needs indexed code".
    has_code = bool(indexed) or nodes > 0
    team = _team(connection, project_id)
    scans = _scans(connection, project_id)
    rules = _rules(connection, project_id)
    learned = _learnings(connection, project_id)

    done = {
        "import": has_code,
        "team": team["chosen"],
        "scan": scans["count"] > 0,
        # Reviewed: nothing waiting, and something was decided or a scan finished with nothing
        # to propose.
        "review": rules["proposed"] == 0 and (rules["approved"] + rules["rejected"] > 0
                                              or scans["completed"] > 0),
    }
    working = {
        "import": not done["import"] and bool(indexing),
        "team": False,
        "scan": scans["running"] > 0 and rules["proposed"] == 0,
        "review": False,
    }
    # A step that needs an earlier one waits for it: no scan before there is code to scan, and no
    # review before there is a scan.
    # A step whose earlier step is done but is not the first left is READY, never "waiting": with
    # code indexed and the team still open, the first scan can start now (the default team scans).
    # `blocked_by` names the step it really waits for, so the console says the true reason.
    needs = {"scan": "import", "review": "scan"}
    states: dict[str, str] = {}
    blocked: dict[str, Optional[str]] = {}
    current: Optional[str] = None
    for key in STEPS:
        blocked[key] = None
        if done[key]:
            states[key] = "done"
        elif working[key]:
            states[key] = "working"
        elif key in needs and not done[needs[key]]:
            states[key] = "waiting"
            blocked[key] = needs[key]
        elif current is None:
            current = key
            states[key] = "current"
        else:
            states[key] = "ready"
    nxt = next((k for k in STEPS if not done[k]), None)

    summaries = {
        "import": (f"{len(indexed)} of {len(repos)} repositories indexed · {nodes:,} code nodes"
                   if repos else "No repository yet"),
        "team": (f"{len(team['role_keys'])} agents chosen" if team["chosen"]
                 else "Not chosen yet: BOSS's default team works until you choose"),
        "scan": (f"{scans['count']} scan{'s' if scans['count'] != 1 else ''}"
                 + (f" · {scans['running']} running" if scans["running"] else "")
                 if scans["count"] else "No scan yet"),
        "review": (f"{rules['proposed']} waiting · {rules['approved']} approved · "
                   f"{rules['rejected']} rejected"),
    }
    steps = [{"key": k, "state": states[k], "done": done[k], "summary": summaries[k],
              "blocked_by": blocked[k]} for k in STEPS]
    return {
        "project_id": project_id, "name": project["name"],
        "project_type": project.get("project_type") or "app",
        "archived": project.get("archived_at") is not None,
        "has_code": has_code, "has_graph": has_code,
        "steps": steps, "next": nxt, "done_count": sum(1 for k in STEPS if done[k]),
        "repositories": repos, "code_nodes": nodes,
        "team": {**team, "candidates": _candidates(connection, project_id)},
        "scans": scans, "scan_defaults": SCAN_DEFAULTS, "areas": _areas(connection, project_id),
        "rules": rules, "learnings": learned,
        "tools": _agent_steps(nxt),
    }


def _agent_steps(nxt: Optional[str]) -> dict[str, str]:
    """For an agent: the tools that carry out each step (the console's buttons call the same
    services), and what to do next."""
    how = {
        "import": ("list_org_repositories to see what the org's GitHub reaches; allow_repository "
                   "if it is not allowed yet (a person presses it); register_repository to add it "
                   "to the project. Indexing then starts by itself: code_graph_status shows it, "
                   "wait_for waits for it."),
        "team": ("set_project_team with the agents for this project (role keys from "
                 "team.candidates; the recommended ones do the scans, changes and incidents)."),
        "scan": (f"start_workflow {SCAN_WORKFLOW} with input.scope = [an area key from `areas`] "
                 "and a budget_usd (offered as a button; it spends from the knowledge budget)."),
        "review": ("The proposed rules wait in the Knowledge Graph's Rules review for a person "
                   "(owners and admins approve). An agent cannot approve a rule."),
    }
    return {**how, "next": how.get(nxt or "", "The knowledge graph is set up.")}


__all__ = ["STEPS", "TEAM_JOBS", "SCAN_WORKFLOW", "SCAN_DEFAULTS", "status"]
