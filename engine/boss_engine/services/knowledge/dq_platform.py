"""Load NorthPeak Bank's data platform into a project — the DQ case study's Knowledge Graph, the
product way (never a SQL script).

One action, offered by the console (Knowledge Graph › Documents › DQ sample, "Load the NorthPeak
Bank data platform", POST /v1/knowledge/samples/dq) and by asking BOSS (the `load_dq_platform`
tool, which only a person presses), both calling `load_platform` here:

  1. ADDS the `banking-dq` ontology pack to the project (a project holds several packs, so one
     running the regulatory and AML case studies keeps their kinds);
  2. registers the sample's mock connectors — Eagle (the DQ monitor) and Olympus (the profile
     service), served by the `mcp-tools` service (tools/mcp_dq_mock.py); ITSM is the shipped
     `itsm` connector every project already has, which carries the bank's DQ incidents;
  3. writes the platform from tools/dq_sample (`D.nodes()`/`D.edges()`) through the store's own
     primitives (`upsert_node`/`upsert_edge`, trust `observed`, as a system of record's data):
     systems, owners, schemas, tables, columns, feeds, column lineage, CDEs, reports, report
     lines, MDRM codes, the business calendar, known issues and examination findings — and the
     Context Graph's history: Eagle's exception groups, the closed incidents with their
     precedents and resolved patterns, and the one open incident;
  4. installs the DQ workflows (`dq_incident_rca`, `dq_incident_escalation`, `dq_eagle_intake`)
     and schedules the Escalation Agent's daily sweep.

THE RULES ARE NOT HERE. DQ rules and their Warning/Error thresholds come from the bank's rulebook
(NorthPeak Loan DQ Rulebook v3.2, a PDF) through rulebook extraction — never copied in from the
sample's data. Edges to a rule or threshold are written when that node is in the graph (found by
its rule id, `resolve_rule`), and loading again links rules that arrived since. A pipeline that
needs a rule not yet in the graph records a coverage gap that says so.

Idempotent: every write is an upsert keyed by the sample's own ids. The data is synthetic and
deterministic.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
PACK_ID = "banking-dq"
ACTOR_REF = "dq_platform"
RCA_WORKFLOW = "dq_incident_rca"
ESCALATION_WORKFLOW = "dq_incident_escalation"
INTAKE_WORKFLOW = "dq_eagle_intake"
WORKFLOW_KEYS = (RCA_WORKFLOW, ESCALATION_WORKFLOW, INTAKE_WORKFLOW)
#: Kinds the rulebook brings, never this loader.
RULEBOOK_KINDS = frozenset({"data_quality_rule", "dq_threshold"})
#: D.RELATIONSHIPS names that are Cypher keywords, renamed as the pack declares them.
_RESERVED = {"IN": "in_calendar", "OF": "threshold_of", "FROM": "lineage_from",
             "TO": "lineage_to", "VIA": "lineage_via"}
#: When the Escalation Agent sweeps every open incident (bank-local time).
ESCALATION_SCHEDULE = {"cadence": "daily", "hour": 7, "minute": 0, "tz_name": "America/New_York"}
#: When the Pipeline Orchestrator reads Eagle's latest COB and starts an RCA per Error group.
INTAKE_SCHEDULE = {"cadence": "daily", "hour": 6, "minute": 30, "tz_name": "America/New_York"}


def edge_kind(relationship: str) -> str:
    """The pack's edge kind for one of tools/dq_sample's relationship names."""
    return _RESERVED.get(relationship, relationship.lower())


def _dq_port() -> int:
    try:
        return int(os.environ.get("BOSS_MCP_DQ_PORT", "").strip() or 8774)
    except ValueError:
        return 8774


def dq_mcp_servers() -> list[dict[str, Any]]:
    """The connectors the sample registers, at the `mcp-tools` service's address."""
    from ...bootstrap import MCP_TOOLS_HOST

    base = f"http://{MCP_TOOLS_HOST}:{_dq_port()}"
    return [
        {"name": "eagle", "url": f"{base}/eagle/mcp", "risk_tier": 0,
         "description": "DQ sample Eagle DQ monitor: list_exception_groups, get_exception_group, "
                        "get_rule_run, rerun_check (read-only)"},
        {"name": "olympus", "url": f"{base}/olympus/mcp", "risk_tier": 0,
         "description": "DQ sample Olympus profile service: get_exception_profile, "
                        "list_recent_changes (read-only, deterministic)"},
    ]


def node_key(kind: str, key: str) -> str:
    return f"dq:{kind}:{key}"


def node_id_of(connection: Connection, project_id: str, kind: str, key: str) -> str:
    """The graph id of one of the platform's nodes, or "" when it is not loaded."""
    if kind in RULEBOOK_KINDS:
        found = resolve_rule(connection, project_id, key) if kind == "data_quality_rule" \
            else resolve_threshold(connection, project_id, key)
        return str((found or {}).get("id") or "")
    row = connection.execute(
        text("SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k "
             "AND (valid_to IS NULL OR valid_to > now()) ORDER BY recorded_at DESC LIMIT 1"),
        {"p": project_id, "k": node_key(kind, key)}).scalar()
    return str(row or "")


def resolve_rule(connection: Connection, project_id: str, rule_id: str) -> Optional[dict[str, Any]]:
    """A DQ rule in the graph BY ITS RULE ID ("NPB-DQ-LN-003"), however it arrived: a
    `data_quality_rule` node, or a business `rule` the rulebook extraction proposed with the
    rule's code — a live, not rejected one, the newest first. None when it is not there yet."""
    rule_id = str(rule_id or "").strip()
    if not rule_id:
        return None
    row = connection.execute(text(
        "SELECT id, kind, node_key, name, body, attrs FROM kg_nodes WHERE project_id = :p "
        "AND kind IN ('data_quality_rule', 'rule') AND valid_to IS NULL "
        "AND (attrs->>'rule_id' = :r OR attrs->>'rule_code' = :r OR node_key = :k) "
        "AND COALESCE(attrs->>'status', 'active') NOT IN ('rejected', 'retired', 'superseded') "
        "ORDER BY (kind = 'data_quality_rule') DESC, recorded_at DESC LIMIT 1"),
        {"p": project_id, "r": rule_id, "k": node_key("data_quality_rule", rule_id)}
    ).mappings().first()
    return dict(row) if row else None


def resolve_threshold(connection: Connection, project_id: str, threshold_id: str
                      ) -> Optional[dict[str, Any]]:
    """A rule's threshold ("NPB-DQ-LN-003:error") by its id, or by rule id and level."""
    threshold_id = str(threshold_id or "").strip()
    rule_id, _, level = threshold_id.partition(":")
    row = connection.execute(text(
        "SELECT id, kind, node_key, name, body, attrs FROM kg_nodes WHERE project_id = :p "
        "AND kind = 'dq_threshold' AND valid_to IS NULL AND (attrs->>'threshold_id' = :t "
        "OR node_key = :k OR (attrs->>'rule_id' = :r AND lower(attrs->>'level') = :l)) "
        "ORDER BY recorded_at DESC LIMIT 1"),
        {"p": project_id, "t": threshold_id, "k": node_key("dq_threshold", threshold_id),
         "r": rule_id, "l": level.lower()}).mappings().first()
    return dict(row) if row else None


def _body(node: dict[str, Any]) -> str:
    attrs = node["attrs"]
    for field in ("description", "definition", "statement", "summary", "transform"):
        if attrs.get(field):
            return str(attrs[field])
    return str(node["name"])


def load_platform(connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
    from tools.dq_sample import data as D

    from .. import mcp_registry
    from ...bootstrap import builtin_provenance
    from . import ontology_packs

    ontology_packs.set_project_pack(connection, project_id=project_id, pack_id=PACK_ID,
                                    set_by=actor)
    registered = []
    for server in dq_mcp_servers():
        row = mcp_registry.ensure_mcp_server(
            connection, project_id, server["name"], server["url"],
            risk_tier=server["risk_tier"], data_boundary="internal",
            description=server["description"],
            publisher={"id": "boss", "name": "BOSS", "kind": "system"},
            provenance={**builtin_provenance(), "sample": "dq"}, respect_operator=True)
        registered.append({"name": server["name"], "registered": row is not None})

    ds = D.generate()
    ids: dict[tuple[str, str], str] = {}
    counts: dict[str, int] = {}
    for node in ds.nodes():
        kind, key = node["kind"], node["key"]
        if kind in RULEBOOK_KINDS:
            continue
        ids[(kind, key)] = STORE.upsert_node(
            connection, project_id=project_id, kind=kind, node_key=node_key(kind, key),
            name=str(node["name"])[:300], body=_body(node), trust_tier="observed",
            actor_kind="system", actor_ref=ACTOR_REF, source="dq_platform",
            attrs={**node["attrs"], "key": key, "sample": "dq"})
        counts[kind] = counts.get(kind, 0) + 1

    def endpoint(kind: str, key: str) -> str:
        if (kind, key) in ids:
            return ids[(kind, key)]
        if kind in RULEBOOK_KINDS:
            found = node_id_of(connection, project_id, kind, key)
            if found:
                ids[(kind, key)] = found
            return found
        return ""

    edges = waiting = 0
    for src_kind, src_key, rel, dst_kind, dst_key in ds.edges():
        src, dst = endpoint(src_kind, src_key), endpoint(dst_kind, dst_key)
        if not (src and dst):
            # Only a rule or threshold can be missing: it comes with the rulebook.
            waiting += 1
            continue
        STORE.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=dst,
                          kind=edge_kind(rel), actor_kind="system", actor_ref=ACTOR_REF,
                          attrs={"relationship": rel})
        edges += 1
    # The systems agents reach through MCP (Eagle, ITSM, Olympus): the Monitoring and
    # Enrichment agents' designs read them as MCPServer nodes.
    tools_of = {"eagle": ["list_exception_groups", "get_exception_group", "get_rule_run",
                          "rerun_check"],
                "itsm": ["get_incident", "list_incidents", "list_teams", "create_incident",
                         "attach_to_incident", "update_incident"],
                "olympus": ["get_exception_profile", "list_recent_changes"]}
    for system in ds.systems:
        if not system.mcp_server:
            continue
        server = STORE.upsert_node(
            connection, project_id=project_id, kind="mcp_server",
            node_key=node_key("mcp_server", system.mcp_server), name=system.mcp_server,
            body=f"{system.name} reached by agents through MCP.", trust_tier="observed",
            actor_kind="system", actor_ref=ACTOR_REF, source="dq_platform",
            attrs={"key": system.mcp_server, "name": system.mcp_server,
                   "system_id": system.system_id, "tools": tools_of.get(system.mcp_server, []),
                   "sample": "dq"})
        counts["mcp_server"] = counts.get("mcp_server", 0) + 1
        STORE.upsert_edge(connection, project_id=project_id, src_id=ids[("system", system.system_id)],
                          dst_id=server, kind="exposes", actor_kind="system", actor_ref=ACTOR_REF,
                          attrs={"relationship": "EXPOSES"})
    # A table the code graph indexes is the same table: say so, rather than two islands.
    for table in ds.tables:
        if not table.in_code_graph:
            continue
        code = connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'table' "
            "AND valid_to IS NULL AND (name = :n OR node_key LIKE :suffix) LIMIT 1"),
            {"p": project_id, "n": table.key, "suffix": f"%:{table.key}"}).scalar()
        if code and ("database_table", table.key) in ids:
            STORE.upsert_edge(connection, project_id=project_id,
                              src_id=ids[("database_table", table.key)], dst_id=str(code),
                              kind="references", actor_kind="system", actor_ref=ACTOR_REF,
                              attrs={"relationship": "SAME_TABLE_AS_CODE"})

    rules_present = sum(1 for r in ds.rules if resolve_rule(connection, project_id, r.rule_id))
    installed = _install_workflows(connection, project_id, actor)
    schedule = _ensure_schedule(connection, project_id, actor, ESCALATION_WORKFLOW,
                                "Escalation Agent — daily sweep of open incidents",
                                ESCALATION_SCHEDULE)
    groups = len(ds.exception_groups)
    note = (f"Loaded {D.BANK}'s data platform: {counts.get('system', 0)} systems, "
            f"{counts.get('database_table', 0)} tables, {counts.get('database_column', 0)} "
            f"columns, {counts.get('feed', 0)} feeds, {counts.get('cde', 0)} CDEs, "
            f"{counts.get('report_line', 0)} report lines, {groups} Eagle exception groups and "
            f"{counts.get('incident', 0)} incidents. ")
    if rules_present < len(ds.rules):
        note += (f"{rules_present} of {len(ds.rules)} DQ rules are in the graph: load the "
                 "rulebook (NorthPeak Loan DQ Rulebook v3.2) to bring the rest, then load the "
                 "platform again to link them.")
    else:
        note += "Every DQ rule in the rulebook is linked."
    return {"ok": True, "pack_id": PACK_ID, "connectors": registered, "counts": counts,
            "edges": edges, "edges_waiting_for_rules": waiting,
            "rules_in_graph": rules_present, "rules_in_rulebook": len(ds.rules),
            "workflows": installed, "escalation_schedule": schedule, "note": note}


def _install_workflows(connection: Connection, project_id: str, actor: str) -> list[dict]:
    from ..workflow_catalogue import seed

    result = seed(connection, project_id, actor=actor, only=frozenset(WORKFLOW_KEYS))
    return [i for i in result.get("installed") or [] if i.get("key") in WORKFLOW_KEYS]


def _ensure_schedule(connection: Connection, project_id: str, actor: str, workflow_key: str,
                     name: str, when: dict[str, Any]) -> dict[str, Any]:
    """The workflow's schedule, created once (an operator's edit or delete of it is kept: a
    schedule with this name that exists, enabled or not, is left alone)."""
    from .. import workflow_schedules

    existing = connection.execute(text(
        "SELECT id, enabled FROM workflow_schedules WHERE project_id = :p AND workflow_key = :k "
        "AND name = :n LIMIT 1"), {"p": project_id, "k": workflow_key, "n": name}
    ).mappings().first()
    if existing:
        return {"id": existing["id"], "enabled": bool(existing["enabled"]), "created": False}
    try:
        with connection.begin_nested():
            made = workflow_schedules.create(
                connection, project_id=project_id, workflow_key=workflow_key, name=name,
                cadence=when["cadence"], minute=when["minute"], hour=when["hour"],
                tz_name=when["tz_name"], actor=actor, input_data={})
        return {"id": made.get("id"), "enabled": bool(made.get("enabled", True)), "created": True,
                "describes": made.get("describes")}
    except Exception as refused:  # noqa: BLE001 — said, never fatal to the load
        return {"created": False, "error": str(refused)[:300]}


def schedule_intake(connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
    """THE SCHEDULED TRIGGER: every morning the Pipeline Orchestrator reads Eagle's latest close
    of business and starts an RCA for each exception group Eagle graded Error. A person turns it
    on (it starts model-backed runs); turning it off is the schedule's own switch."""
    return _ensure_schedule(connection, project_id, actor, INTAKE_WORKFLOW,
                            "Pipeline Orchestrator — investigate Eagle's Error groups each COB",
                            INTAKE_SCHEDULE)


def status(connection: Connection, *, project_id: str, cob: Optional[str] = None
           ) -> dict[str, Any]:
    """Whether the platform is loaded, how many rules are in the graph, and Eagle's exception
    groups by COB, each with what the pipeline made of it (incident, latest run)."""
    from tools.dq_sample import data as D

    from . import ontology_packs

    ds = D.generate()
    loaded = bool(node_id_of(connection, project_id, "system", "S-EGL"))
    rules_present = sum(1 for r in ds.rules if resolve_rule(connection, project_id, r.rule_id)) \
        if loaded else 0
    groups = []
    for g in ds.exception_groups:
        if cob and g.cob != cob:
            continue
        incident = connection.execute(text(
            "SELECT n.attrs FROM kg_nodes n JOIN kg_edges e ON e.src_id = n.id "
            "JOIN kg_nodes x ON x.id = e.dst_id WHERE n.project_id = :p AND n.kind = 'incident' "
            "AND e.kind = 'raised_from' AND x.node_key = :k AND n.valid_to IS NULL "
            "ORDER BY n.recorded_at DESC LIMIT 1"),
            {"p": project_id, "k": node_key("exception_group", g.group_id)}).scalar() \
            if loaded else None
        run = connection.execute(text(
            "SELECT r.id, r.status FROM workflow_runs r JOIN workflow_versions v "
            "ON v.id = r.workflow_version_id JOIN workflow_definitions d "
            "ON d.id = v.workflow_definition_id WHERE r.project_id = :p AND d.workflow_key = :k "
            "AND r.input->>'exception_group_id' = :g ORDER BY r.created_at DESC LIMIT 1"),
            {"p": project_id, "k": RCA_WORKFLOW, "g": g.group_id}).mappings().first() \
            if loaded else None
        attrs = incident or {}
        groups.append({"group_id": g.group_id, "eagle_rule_id": g.eagle_rule_id,
                       "rule_id": g.rule_id, "rule_version": g.rule_version, "cob": g.cob,
                       "grade": g.eagle_grade, "column": g.column, "failed": g.failed,
                       "population": g.population,
                       "incident_id": attrs.get("incident_id"),
                       "incident_status": attrs.get("status"),
                       "run_id": (run or {}).get("id"), "run_status": (run or {}).get("status")})
    pack = ontology_packs.get_project_pack(connection, project_id=project_id)
    return {"loaded": loaded, "has_pack": bool(pack and pack.includes(PACK_ID)),
            "rules_in_graph": rules_present, "rules_in_rulebook": len(ds.rules),
            "workflow_key": RCA_WORKFLOW, "cobs": sorted({g.cob for g in ds.exception_groups}),
            "running_example": D.RUNNING_EXAMPLE, "groups": groups}
