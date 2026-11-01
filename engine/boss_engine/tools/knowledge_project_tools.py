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


def _add_regulation_document(ctx, *, project_id, actor, args):
    from ..services.knowledge import documents

    clauses = args.get("clauses") or []
    if not isinstance(clauses, list) or not clauses:
        return {"ok": False, "error": (
            "a clause's text must be given, never generated: pass at least one clause as "
            "{section, text} with the operator's exact wording.")}
    try:
        with ctx.begin() as c:
            result = documents.add_document(
                c, project_id=project_id, title=str(args.get("title") or ""),
                source_url=str(args.get("source_url") or ""),
                citation=str(args.get("citation") or ""), clauses=clauses,
                sensitivity=str(args.get("sensitivity") or "public"),
                # The clause text is the operator's own words, never generated (the permission
                # gate already required knowledge:manage): written as the person's act, not an
                # agent's inference.
                actor_kind="person", actor_ref=actor,
            )
    except documents.DocumentRefused as refused:
        return {"ok": False, "error": str(refused)}
    return {"ok": True, **result}


def _set_ontology_pack(ctx, *, project_id, actor, args):
    from ..services.knowledge import ontology_packs

    pack_id = str(args.get("pack_id") or "").strip()
    try:
        with ctx.begin() as c:
            pack = ontology_packs.set_project_pack(
                c, project_id=project_id, pack_id=pack_id, set_by=actor,
            )
            # THE WORKFLOWS THE PACK UNLOCKS: a catalogue entry whose step contracts name this
            # pack's kinds (banking_regulatory_compliance) is skipped by a seed that ran before
            # the pack was set; it is installed now. Idempotent.
            from ..services.workflow_catalogue import seed_unlocked_by_pack

            try:
                with c.begin_nested():
                    seed_unlocked_by_pack(c, project_id, pack.pack_id, actor=actor)
            except Exception:  # noqa: BLE001 — the pack is set either way; boot seeds again
                pass
    except ontology_packs.PackRefused as refused:
        return {"ok": False, "error": str(refused)}
    return {"ok": True, "pack_id": pack.pack_id, "version": pack.version}


def _load_aml_sample(ctx, *, project_id, actor, args):
    from ..services.knowledge import aml_sample

    with ctx.begin() as c:
        return aml_sample.load_sample(c, project_id=project_id, actor=actor)


def _aml_sample_status(ctx, *, project_id, actor, args):
    from ..services.knowledge import aml_sample

    with ctx.begin() as c:
        found = aml_sample.status(c, project_id=project_id)
    return {"ok": True, **found,
            "href": f"/?project_id={project_id}&area=knowledge&kg_tab=documents"}


def _load_dq_platform(ctx, *, project_id, actor, args):
    from ..services.knowledge import dq_platform

    with ctx.begin() as c:
        return dq_platform.load_platform(c, project_id=project_id, actor=actor)


def _dq_platform_status(ctx, *, project_id, actor, args):
    from ..services.knowledge import dq_platform

    with ctx.begin() as c:
        found = dq_platform.status(c, project_id=project_id,
                                   cob=str(args.get("cob") or "") or None)
    return {"ok": True, **found,
            "href": f"/?project_id={project_id}&area=knowledge&kg_tab=documents"}


KNOWLEDGE_PROJECT_TOOLS: dict[str, dict] = {
    "load_dq_platform": {
        "description": (
            "LOAD NorthPeak Bank's data platform into this project (the DQ case study): adds the "
            "banking-dq ontology pack beside the project's other packs, registers the Eagle and "
            "Olympus mock connectors, writes the platform (systems, tables, columns, feeds, "
            "lineage, CDEs, reports, MDRM, calendar, issues, findings, Eagle's exception groups, "
            "past incidents and precedents) into the Knowledge Graph, installs dq_incident_rca "
            "and its side workflows, and schedules the daily escalation sweep. The DQ rules come "
            "from the rulebook, never from here. Synthetic data; loading twice changes nothing. "
            "Proposed by the model, but only an operator presses it. Then start dq_incident_rca "
            "with exception_group_id EXG-20261005-0003."),
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
        "handler": _load_dq_platform, "kind": "write", "permission": "knowledge:manage",
        "operator_only": True,
    },
    "dq_platform_status": {
        "description": (
            "Whether this project has NorthPeak Bank's data platform loaded, how many DQ rules "
            "are in the graph, and Eagle's exception groups (optionally one COB) with each one's "
            "incident and latest investigation."),
        "parameters": {"type": "object", "properties": {"cob": {"type": "string"}},
                       "required": [], "additionalProperties": False},
        "handler": _dq_platform_status, "kind": "read", "permission": "knowledge:read",
    },
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
    "add_regulation_document": {
        "description": (
            "Add a regulation document to this project's Knowledge Graph: a title, source URL, "
            "citation, and its clauses (each {section, text}) — the operator's exact pasted "
            "text, never generated. Needs an ontology pack granting 'document' "
            "(set_ontology_pack first, e.g. banking-regulatory); refuses in words otherwise, "
            "and refuses an empty clause list rather than inventing one."),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "source_url": {"type": "string"},
            "citation": {"type": "string"},
            "clauses": {"type": "array", "items": {"type": "object", "properties": {
                "section": {"type": "string"}, "text": {"type": "string"},
                "citation": {"type": "string"}, "url": {"type": "string"}},
                "required": ["section", "text"]}, "minItems": 1}},
            "required": ["title", "clauses"], "additionalProperties": False},
        "handler": _add_regulation_document, "kind": "write", "permission": "knowledge:manage",
        "operator_only": False,
    },
    "set_ontology_pack": {
        "description": (
            "ADD an ontology pack to this project (e.g. banking-regulatory, banking-dq, "
            "banking-aml): what kinds of node and edge, beyond BOSS's core, the project's "
            "Knowledge Graph accepts — a document and its clauses need one first. A project "
            "holds several packs; adding one never removes another's kinds. Proposed by the "
            "model, but only an operator presses it."),
        "parameters": {"type": "object", "properties": {
            "pack_id": {"type": "string"}}, "required": ["pack_id"],
            "additionalProperties": False},
        "handler": _set_ontology_pack, "kind": "write", "permission": "knowledge:manage",
        "operator_only": True,
    },
    "load_aml_sample": {
        "description": (
            "LOAD the AML case study's sample bank into this project (case study 4): sets the "
            "banking-aml ontology pack (it keeps banking-regulatory's kinds), registers the "
            "sample's mock connectors (case-management, kyc, transactions), writes NorthPeak "
            "Lending's synthetic customers, accounts, counterparties, KYC profiles, typologies "
            "and three open alerts into the Knowledge Graph, and installs the "
            "aml_alert_investigation workflow. Synthetic data only; loading twice changes "
            "nothing. Proposed by the model, but only an operator presses it. Then start "
            "aml_alert_investigation with alert_id ALERT-1."),
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
        "handler": _load_aml_sample, "kind": "write", "permission": "knowledge:manage",
        "operator_only": True,
    },
    "aml_sample_status": {
        "description": (
            "Whether this project has the AML sample loaded, its ontology pack, and each sample "
            "alert (ALERT-1..3) with its account, customer, rule and the latest investigation's "
            "case status and the person's determination."),
        "parameters": {"type": "object", "properties": {}, "required": [],
                       "additionalProperties": False},
        "handler": _aml_sample_status, "kind": "read", "permission": "knowledge:read",
    },
}


def register_knowledge_project_tools(reg) -> None:
    for name, spec in KNOWLEDGE_PROJECT_TOOLS.items():
        reg.register(ToolSpec(
            name=name, description=spec["description"], parameters=spec["parameters"],
            handler=spec["handler"], kind=spec["kind"],
            # Setting a team is undone by setting it again; it starts and spends nothing.
            reversibility="safe", permission=spec["permission"],
            operator_only=spec.get("operator_only", False)))
