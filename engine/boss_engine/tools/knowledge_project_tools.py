"""A knowledge-graph project's setup, as the agent's actions (BOSSAPP-768 project flow).

The console's Knowledge Graph setup and the agent walk the SAME steps through the SAME services:

    create_project(project_type=knowledge_graph)   services/projects.create (KG switched on)
    register_repository(project=…)                 multirepo.project_repos (indexing follows)
    set_project_team(project=…)                    agent_assignment.set_team   ← here
    start_workflow(business_rules_scan, project=…) the workflow start (a button a person presses)
    knowledge_project_setup(project=…)             project_setup.status        ← here

So "create a knowledge-graph project for repo X" is answered by walking the steps
`knowledge_project_setup` names, with the generic `project` argument pointing each tool at the
new project. Nothing here is a second implementation of a console button.
"""
from __future__ import annotations

from .registry import ToolSpec


def _setup(ctx, *, project_id, actor, args):
    from ..services.knowledge import project_setup

    try:
        with ctx.begin() as c:
            found = project_setup.status(c, project_id)
    except LookupError as missing:
        return {"ok": False, "error": str(missing)}
    team = found["team"]
    # The candidates' missions are long; the agent needs who they are and what they would do.
    team = {**team, "candidates": [
        {k: c.get(k) for k in ("role_key", "display_name", "job", "does", "recommended")}
        for c in team["candidates"]]}
    return {"ok": True, **{k: v for k, v in found.items() if k != "team"}, "team": team,
            "href": f"/?project_id={project_id}&area=knowledge&kg_tab=setup"}


def _set_team(ctx, *, project_id, actor, args):
    from ..services import agent_assignment

    roles = args.get("role_keys") or []
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",")]
    with ctx.begin() as c:
        done = agent_assignment.set_team(c, project_id=project_id, role_keys=roles, actor=actor)
    if not done.get("ok"):
        return done
    names = ", ".join(a["display_name"] for a in done["agents"])
    return {**done, "project_id": project_id,
            "note": (f"{names} {'is' if len(done['agents']) == 1 else 'are'} this project's team "
                     "now: every new piece of work in it starts with them, and only they may sign "
                     "off its gates. Undo by setting the team again.")}


KNOWLEDGE_PROJECT_TOOLS: dict[str, dict] = {
    "knowledge_project_setup": {
        "description": (
            "Where a project's KNOWLEDGE GRAPH setup is, step by step — the same view as the "
            "console's Knowledge Graph › Setup: 1 import code (its repositories and their "
            "indexing, with real counts), 2 assign the team (the agents chosen, and every agent "
            "that can join with what it would do: scans, changes, incidents), 3 first scan "
            "(business_rules_scan runs, the areas a scan can read, keyed for its scope), "
            "4 review (rules waiting, approved, rejected), and `next` with the exact tool for "
            "it. Read it after create_project(project_type=knowledge_graph) and after each step; "
            "pass `project` for a project other than this conversation's."),
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
        "handler": _setup, "kind": "read", "permission": "knowledge:read",
    },
    "set_project_team": {
        "description": (
            "STATE a project's team: exactly these BOSS agents work on it — every new piece of "
            "work in the project starts with them and only they sign off its gates (the "
            "console's Knowledge Graph › Setup › Assign the team, and the project's default "
            "team). `role_keys` are agents of that project (knowledge_project_setup lists them "
            "with what each would do; describe_role says more). Replaces the team rather than "
            "adding to it; undo by setting it again. Pass `project` for another project."),
        "parameters": {"type": "object", "properties": {
            "role_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "description": "the agents' role keys, e.g. chief_architect, sre"}},
            "required": ["role_keys"], "additionalProperties": False},
        "handler": _set_team, "kind": "write", "permission": "work_item:manage",
    },
}


def register_knowledge_project_tools(reg) -> None:
    for name, spec in KNOWLEDGE_PROJECT_TOOLS.items():
        reg.register(ToolSpec(
            name=name, description=spec["description"], parameters=spec["parameters"],
            handler=spec["handler"], kind=spec["kind"],
            # Setting a team is undone by setting it again; it starts and spends nothing.
            reversibility="safe", permission=spec["permission"]))
