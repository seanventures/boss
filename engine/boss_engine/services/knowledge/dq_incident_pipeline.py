"""The DQ root-cause analysis and remediation pipeline (`dq_incident_rca`, Instance
A of `kg_investigation_pipeline`; design §5.2), on NorthPeak Bank's data platform.

One DQ Monitor exception group, investigated end to end:

  1 monitoring           Incident Intake Agent       open or attach ONE incident per Error group, grade
                                                its severity; never diagnoses; Warning writes nothing
  2 context_enrichment   Context Hydration     ten fixed graph reads + the Profile Service + Service Desk: footprint,
                                                owners, subscriptions, lineage, regulatory reach,
                                                known issues, exception profile, recent changes;
                                                every missing fact a CoverageGap; no LLM
  3 historical_pattern   Past Incidents     two-tier deterministic match of closed incidents,
                                                grouped by resolved pattern, with how each fix held;
                                                MATCHED_INCIDENT edges; a briefing on the ticket
  4 rca                  Root Cause Analysis Agent (LLM)        candidate root causes with confidence (tier 1 from a
                                                precedent, tier 2 from the evidence), or
                                                InsufficientEvidence — and 2-3-4 run again (at most
                                                two more rounds)
  5 pipeline_orchestrator  Flow Orchestrator route each candidate family to its lane
  6 lane (for each)      Lane Agent (LLM)       confirm or rule out the family's candidate on evidence
  - rule_drafting        Rule Proposal (LLM)    a confirmed false positive becomes a rule proposal in
                                                Rules review (side path "Rule authoring")
  7 remediation_advisor  Fix Planner    the package: fix, target, executor, approver, change
                                                content — PREPARED only
  - human_approval / human_execution            a person approves; a DIFFERENT person carries it out
  8 remediation_fix      (design target)        records what the person did; no agent executes
  10 incident_closure    Close and Learn       re-run the DQ Monitor's check, finalise the cause, close the
                                                ticket, maintain the resolved pattern, write the
                                                Precedent the next investigation finds
  9 escalation           Escalation Watch       (its own daily workflow) ladders every open incident

Deterministic stages are BOSS's own code run as the named role agent ($0, repeatable). The LLM
stages reuse the regulatory instance's governed agent path verbatim (`_role_agent`/`_ask_role`:
the configured model, JSON between markers, one more ask, an unreadable answer is no answer) and
are fed through the Context Budgeter, which trims to the step's token budget and says what it
dropped. Anything an agent cites must be in the list it was offered.

Each stage returns `produced` (the Context Graph writes its contract declares) naming the nodes it
wrote; the engine verifies them and stamps the run's provenance. Exception data, ticket text and
change descriptions are DATA, never instructions.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import banking_regulatory_pipeline as _shared
from . import dq_platform
from .dq_mcp import call as _call
from .dq_services import Section, curate, describe_profile, exception_profile
from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()
_ROLES_DIR = (Path(__file__).resolve().parents[4] / "packages" / "specpack" / "skills"
              / "dq_incident_rca" / "roles")

# The role agents (packages/specpack/roles/dq_*.yaml) each stage runs as.
MONITORING = "dq_monitoring_agent"
ENRICHMENT = "dq_context_enrichment_agent"
HISTORICAL = "dq_historical_pattern_agent"
RCA = "dq_rca_agent"
ORCHESTRATOR = "dq_pipeline_orchestrator"
LANE = "dq_lane_agent"
RULE_DRAFTING = "dq_rule_drafting_agent"
REMEDIATION = "dq_remediation_advisor"
CLOSURE = "dq_incident_closure_agent"
ESCALATION = "dq_escalation_agent"

#: Root-cause families (tools/dq_sample ROOT_CAUSE_FAMILIES) and the lane each routes to.
FAMILIES = {
    "source_processing": "A defect or job-ordering problem in the source system's own processing.",
    "feed_etl": "A feed landed late, partial or transformed wrongly between systems.",
    "reference_data": "Configuration or mapping data (products, GL accounts, codes) is wrong.",
    "manual_entry": "Data captured wrongly or twice by people or front-end channels.",
    "rule_config": "The DQ rule or its threshold is wrong: the data is fine (false positive).",
}
LANE_OF = {"source_processing": "lane_source_processing", "feed_etl": "lane_feed_etl",
           "reference_data": "lane_reference_data", "manual_entry": "lane_manual_entry",
           "rule_config": "rule_drafting"}
INSUFFICIENT = "InsufficientEvidence"
SEVERITIES = ("Low", "Medium", "High", "Critical")
#: Policy (the stages' contracts say the same; DQ pipeline policy v0.2.0).
POLICY_VERSION = "DQ pipeline policy v0.2.0"
HOP_CAP, CHANGE_LOOKBACK_DAYS, LIST_CAP = 3, 14, 25          # CTX-P1/P2/P3
HISTORY_LOOKBACK_DAYS = 730                                   # HP: two years back from the COB
MIN_ROUTE_CONFIDENCE = 0.3                                    # OP-ORC-01
TOKEN_BUDGETS = {"rca": 6000, "lane": 4000, "rule_drafting": 3000, "remediation_advisor": 4000}
EXECUTION_GATE, APPROVAL_GATE = "human_execution", "human_approval"

# The governed agent path, shared with the regulatory instance — module attributes so a test
# swaps `_role_agent` here exactly as it does there.
_role_agent = _shared._role_agent
_ask_role = _shared._ask_role
_attributed = _shared._attributed
_ran_as = _shared._ran_as
_learned = _shared._learned


class PersonMissing(RuntimeError):
    """A stage that needs a person's answer (approval, execution) ran without it."""


# ── small helpers ─────────────────────────────────────────────────────────────────────────────

def _role_prompt(role_file: str, situation: str) -> str:
    return f"{(_ROLES_DIR / role_file).read_text(encoding='utf-8')}\n\n{situation}"


def _run_id(step: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return str(step.get("run_id") or (context or {}).get("run_id") or "")


def _attrs(row: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    raw = (row or {}).get("attrs")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return dict(raw or {})


def _outputs(connection: Connection, run_id: str) -> dict[str, dict[str, Any]]:
    """The latest succeeded output of every step of this run (latest round of a loop)."""
    if not run_id:
        return {}
    rows = connection.execute(text(
        "SELECT step_id, output FROM workflow_step_runs WHERE workflow_run_id = :r "
        "AND status = 'succeeded' ORDER BY iteration, created_at, completed_at"),
        {"r": run_id}).mappings().all()
    return {str(r["step_id"]): dict(r["output"]) for r in rows if isinstance(r["output"], dict)}


def _steps(connection: Connection, run_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
    found = _outputs(connection, run_id)
    found.update({k: v for k, v in dict((context or {}).get("steps") or {}).items()
                  if isinstance(v, dict) and k not in found})
    return found


def _round(connection: Connection, run_id: str, step_id: str) -> int:
    """Which round of the evidence loop this is (1 = the first)."""
    if not run_id:
        return 1
    done = connection.execute(text(
        "SELECT count(*) FROM workflow_step_runs WHERE workflow_run_id = :r AND step_id = :s "
        "AND status = 'succeeded'"), {"r": run_id, "s": step_id}).scalar()
    return int(done or 0) + 1


def _node(connection: Connection, project_id: str, node_id: str) -> Optional[dict[str, Any]]:
    if not node_id:
        return None
    row = connection.execute(text(
        "SELECT id, kind, node_key, name, body, attrs, trust_tier FROM kg_nodes "
        "WHERE project_id = :p AND id = :i"), {"p": project_id, "i": node_id}).mappings().first()
    return dict(row) if row else None


def _by_key(connection: Connection, project_id: str, kind: str, key: str
            ) -> Optional[dict[str, Any]]:
    if kind in dq_platform.RULEBOOK_KINDS:
        found = (dq_platform.resolve_rule(connection, project_id, key) if kind ==
                 "data_quality_rule" else dq_platform.resolve_threshold(connection, project_id, key))
        return found
    node_id = dq_platform.node_id_of(connection, project_id, kind, key)
    return _node(connection, project_id, node_id) if node_id else None


def _edges(connection: Connection, project_id: str, node_id: str, *, direction: str = "out",
           kinds: Optional[tuple[str, ...]] = None) -> list[dict[str, Any]]:
    """The live neighbours of a node, with the edge kind and the neighbour's attrs."""
    anchor, other = ("e.src_id", "e.dst_id") if direction == "out" else ("e.dst_id", "e.src_id")
    rows = connection.execute(text(
        f"SELECT n.id, n.kind, n.node_key, n.name, n.body, n.attrs, e.kind AS edge_kind, "
        f"e.attrs AS edge_attrs FROM kg_edges e JOIN kg_nodes n ON n.id = {other} "
        f"WHERE {anchor} = :i AND e.project_id = :p AND e.valid_to IS NULL "
        "AND n.valid_to IS NULL" + (" AND e.kind = ANY(:k)" if kinds else "")
        + " ORDER BY n.node_key"),
        {"i": node_id, "p": project_id, "k": list(kinds or ())}).mappings().all()
    return [dict(r) for r in rows]


def _key_of(row: Mapping[str, Any]) -> str:
    attrs = _attrs(row)
    return str(attrs.get("key") or attrs.get("rule_id") or attrs.get("rule_code")
               or row.get("name") or "")


def _versions(connection: Connection, project_id: str) -> dict[str, Any]:
    try:
        from .graph_versions import versions

        with connection.begin_nested():
            return versions(connection, project_id)
    except Exception:  # noqa: BLE001 — the decision is recorded either way
        return {}


def _decide(connection: Connection, project_id: str, *, run_id: str, step_id: str,
            decision_type: str, statement: str, round_no: int = 1, scope: str = "",
            actor_ref: str = "", extra: Optional[Mapping[str, Any]] = None) -> str:
    """One AgentDecision, keyed by run, step, type, round (and `scope`, e.g. an incident): a
    re-run writes a new decision and keeps the old one (see the stage's trigger)."""
    suffix = (f":r{round_no}" if round_no > 1 else "") + (f":{scope}" if scope else "")
    attrs = {"decision_type": decision_type, "statement": statement[:4000], "step_id": step_id,
             "run_id": run_id, "round": round_no, "policy_version": POLICY_VERSION,
             **_versions(connection, project_id), **dict(extra or {})}
    return STORE.upsert_node(
        connection, project_id=project_id, kind="agent_decision",
        node_key=f"context:agent_decision:{run_id}:{step_id}:{decision_type}{suffix}",
        name=f"{decision_type} ({step_id})", body=statement[:4000], trust_tier="inferred",
        actor_kind="agent", actor_ref=actor_ref or step_id, source="context_graph", attrs=attrs)


def _gaps(connection: Connection, project_id: str, *, run_id: str, step_id: str,
          statements: list[str], round_no: int = 1, actor_ref: str = "") -> list[str]:
    ids = []
    for n, statement in enumerate(dict.fromkeys(statements), start=1):
        ids.append(STORE.upsert_node(
            connection, project_id=project_id, kind="coverage_gap",
            node_key=f"context:coverage_gap:{run_id}:{step_id}:r{round_no}:{n}",
            name=f"Coverage gap ({step_id})", body=statement, trust_tier="inferred",
            actor_kind="agent", actor_ref=actor_ref or step_id, source="context_graph",
            attrs={"statement": statement, "step_id": step_id, "run_id": run_id,
                   "round": round_no}))
    return ids


def _produced(decision_type: str, decision_id: str, statement: str,
              gap_ids: Optional[list[str]] = None, gaps: Optional[list[str]] = None,
              **more: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"agent_decision": {"decision_type": decision_type,
                                              "statement": statement[:2000],
                                              "node_id": decision_id}}
    if gap_ids:
        out["coverage_gap"] = {"node_ids": gap_ids, "statement": "; ".join(gaps or [])[:2000]}
    out.update(more)
    return out


def _edge(connection: Connection, project_id: str, src: str, dst: str, kind: str, actor_ref: str,
          attrs: Optional[dict[str, Any]] = None, actor_kind: str = "agent") -> None:
    if src and dst and src != dst:
        STORE.upsert_edge(connection, project_id=project_id, src_id=src, dst_id=dst, kind=kind,
                          actor_kind=actor_kind, actor_ref=actor_ref, attrs=attrs or {})


def _patch(connection: Connection, project_id: str, node_id: str, patch: Mapping[str, Any]) -> None:
    connection.execute(text(
        "UPDATE kg_nodes SET attrs = COALESCE(attrs, '{}'::jsonb) || CAST(:patch AS JSONB), "
        "last_seen = now() WHERE project_id = :p AND id = :i"),
        {"patch": json.dumps(dict(patch)), "p": project_id, "i": node_id})


def _rule_facts(connection: Connection, project_id: str, rule_id: str) -> dict[str, Any]:
    """A rule's facts from the graph — its node (by rule id), its thresholds and what it points
    at by edge, falling back to the fields the rulebook gives it. Empty when it is not there."""
    rule = dq_platform.resolve_rule(connection, project_id, rule_id)
    if rule is None:
        return {}
    attrs = _attrs(rule)

    def value(level: str) -> Optional[float]:
        found = dq_platform.resolve_threshold(connection, project_id, f"{rule_id}:{level}")
        for source, key in ((_attrs(found), "value"), (attrs, f"{level}_value"),
                            (attrs.get(level) if isinstance(attrs.get(level), dict) else {},
                             "value")):
            raw = (source or {}).get(key)
            if raw not in (None, ""):
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return None

    out = {"node_id": rule["id"], "rule_id": rule_id, "name": rule["name"],
           "title": attrs.get("title") or rule["name"], "severity": attrs.get("severity"),
           "dimension": attrs.get("dimension") or attrs.get("dq_dimension"),
           "check": attrs.get("check") or attrs.get("statement") or rule.get("body"),
           "owner_id": attrs.get("owner_id"), "system_id": attrs.get("system_id"),
           "feed_id": attrs.get("feed_id"), "version": attrs.get("version"),
           "warning_value": value("warning"), "error_value": value("error"),
           "metric": attrs.get("metric") or (
               attrs["warning"].get("metric") if isinstance(attrs.get("warning"), dict) else None),
           "cde_ids": list(attrs.get("cde_ids") or []), "columns": list(attrs.get("columns") or []),
           "tables": list(attrs.get("tables") or []), "report_lines": list(attrs.get("report_lines") or []),
           "related": list(attrs.get("related") or [])}
    for edge in _edges(connection, project_id, rule["id"], direction="out"):
        key = _key_of(edge)
        if edge["edge_kind"] == "checks" and key not in out["cde_ids"]:
            out["cde_ids"].append(key)
        elif edge["edge_kind"] == "applies_to" and edge["kind"] == "database_column" \
                and key not in out["columns"]:
            out["columns"].append(key)
        elif edge["edge_kind"] == "applies_to" and edge["kind"] == "database_table" \
                and key not in out["tables"]:
            out["tables"].append(key)
        elif edge["edge_kind"] == "reaches" and key not in out["report_lines"]:
            out["report_lines"].append(key)
        elif edge["edge_kind"] == "covers" and not out["feed_id"]:
            out["feed_id"] = key
        elif edge["edge_kind"] == "related_to" and key not in out["related"]:
            out["related"].append(key)
    if not out["owner_id"]:
        owners = [e for e in _edges(connection, project_id, rule["id"], direction="in",
                                    kinds=("owns",)) if e["kind"] == "owner"]
        out["owner_id"] = _key_of(owners[0]) if owners else None
    return out


def _grade(measured: float, warning: Optional[float], error: Optional[float]) -> Optional[str]:
    if warning is None or error is None:
        return None
    if measured > error:
        return "Error"
    if measured > warning:
        return "Warning"
    return "Clean"


def _owner(connection: Connection, project_id: str, owner_id: Optional[str]) -> dict[str, Any]:
    """An owner by id, with the team and Service Desk assignment group that work its tickets."""
    node = _by_key(connection, project_id, "owner", str(owner_id or "")) if owner_id else None
    if node is None:
        return {}
    attrs = _attrs(node)
    group = attrs.get("service_desk_group")
    team = attrs.get("team_id")
    if not group and team:
        team_node = _by_key(connection, project_id, "owner", team)
        group = _attrs(team_node).get("service_desk_group") if team_node else None
    return {"owner_id": owner_id, "name": node["name"], "title": attrs.get("title"),
            "type": attrs.get("owner_type"), "team_id": team or owner_id,
            "assignment_group": group, "node_id": node["id"]}


def _incident_node(connection: Connection, project_id: str, incident_id: str
                   ) -> Optional[dict[str, Any]]:
    return _by_key(connection, project_id, "incident", incident_id)


# ── 1. Incident Intake ─────────────────────────────────────────────────────────────────────────────

def _group(connection: Connection, project_id: str, group_id: str,
           calls: list[dict[str, Any]], gaps: list[str]) -> dict[str, Any]:
    payload, gap = _call(connection, project_id, "dq-monitor", "get_exception_group",
                         {"group_id": group_id}, calls)
    if payload is not None:
        return payload
    node = _by_key(connection, project_id, "exception_group", group_id)
    if node is None:
        raise LookupError(
            f"{group_id or 'that exception group'} is neither in the DQ Monitor nor in this project's "
            "graph: load the NorthPeak Bank data platform (Knowledge Graph › Documents › DQ "
            "sample) and start the investigation from one of the DQ Monitor's exception groups.")
    gaps.append(f"The DQ Monitor was not read ({gap}); the exception group as loaded into the graph is used")
    attrs = _attrs(node)
    return {"group_id": group_id, "monitor_rule_id": attrs.get("monitor_rule_id"),
            "rule_id": attrs.get("rule_id"), "rule_version": attrs.get("rule_version"),
            "cob": attrs.get("cob"), "platform": attrs.get("platform"),
            "table": attrs.get("table"), "column": attrs.get("column"),
            "grade": attrs.get("monitor_grade"), "metric": attrs.get("metric"),
            "measured": attrs.get("measured"), "failed": attrs.get("failed"),
            "population": attrs.get("population"),
            "thresholds": attrs.get("monitor_thresholds") or {}, "sample_keys":
            attrs.get("sample_keys") or [], "emitted_at": attrs.get("emitted_at")}


def run_dq_monitoring(connection: Connection, settings: Any, project_id: str,
                      step: Mapping[str, Any], context: Mapping[str, Any],
                      inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "monitoring")
    actor = f"{MONITORING}"
    group_id = str(inputs.get("exception_group_id") or
                   ((context or {}).get("input") or {}).get("exception_group_id") or "").strip()
    if not group_id:
        raise ValueError("start the investigation with an exception_group_id, e.g. "
                         "EXG-20261005-0003 (DQX-0588 v2 on COB 2026-10-05)")
    calls: list[dict[str, Any]] = []
    gaps: list[str] = []
    # 1 Read the group
    group = _group(connection, project_id, group_id, calls, gaps)
    rule_id = str(group.get("rule_id") or "")
    measured = float(group.get("measured") or 0)
    monitor_grade = str(group.get("grade") or "")
    group_node = dq_platform.node_id_of(connection, project_id, "exception_group", group_id)
    label = f"{group.get('monitor_rule_id')} v{group.get('rule_version')} on COB {group.get('cob')}"
    base = {"capability": "dq_monitoring", "tool": None, "status": "succeeded",
            "exception_group_id": group_id, "exception_group_node_id": group_node or None,
            "rule_id": rule_id, "monitor_rule_id": group.get("monitor_rule_id"),
            "rule_version": group.get("rule_version"), "cob": group.get("cob"),
            "platform": group.get("platform"), "column": group.get("column"),
            "table": group.get("table"), "metric": group.get("metric"), "measured": measured,
            "failed": group.get("failed"), "population": group.get("population"),
            "monitor_grade": monitor_grade, "group_label": label, "calls": calls,
            "ran_as": _ran_as(MONITORING)}
    # 2 Resolve the rule (by its rule id: the rulebook brings it)
    rule = _rule_facts(connection, project_id, rule_id)
    if not rule:
        gaps.append(f"rule {rule_id} is not in the graph yet — load the rulebook (NorthPeak Loan "
                    "DQ Rulebook v3.2) so the rule, its thresholds, owner and CDEs can be read")
    # 3 Grade severity: the DQ Monitor grades every run; only Error opens or attaches.
    rulebook_grade = _grade(measured, rule.get("warning_value"), rule.get("error_value")) \
        if rule else None
    if monitor_grade != "Error":
        note = (f"{label}: the DQ Monitor graded it {monitor_grade or 'not at all'}. A Warning goes to the "
                "DQ dashboard; Incident Intake writes nothing and opens no incident.")
        return {**base, "action": "ignore", "continue_pipeline": False,
                "rulebook_grade": rulebook_grade, "note": note, "summary": note, "produced": {}}
    severity = str(rule.get("severity") or "Medium").capitalize()
    if severity not in SEVERITIES:
        severity = "Medium"
    # 4 Set failure reason: one per rule, facts only.
    failure_reason = (f"{rule.get('title') or rule_id} failed for {group.get('failed')} of "
                      f"{group.get('population')} ({group.get('metric')} {measured}"
                      + (f" vs Error above {rule.get('error_value')}" if rule.get('error_value')
                         is not None else "") + ")")
    discrepancy = ""
    if rulebook_grade and rulebook_grade != monitor_grade:
        discrepancy = (f"The DQ Monitor graded Error; against the rulebook's thresholds the measure is "
                       f"{rulebook_grade} (the DQ Monitor's configured thresholds "
                       f"{group.get('thresholds')}).")
    # 5 Tag the exception
    owner = _owner(connection, project_id, rule.get("owner_id"))
    tags = {"cde_ids": rule.get("cde_ids") or [], "column": group.get("column"),
            "platform": group.get("platform"), "dimension": rule.get("dimension")}
    # 6 Match. The incident THIS group already opened (a re-run, or one opened before BOSS was
    # watching: INC-2301 from EXG-20261002-0002) is the same incident, opened — not an attach.
    own = connection.execute(text(
        "SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
        "AND valid_to IS NULL AND attrs->>'raised_from' = :g ORDER BY recorded_at DESC LIMIT 1"),
        {"p": project_id, "g": group_id}).mappings().first()
    if own is not None:
        incident_id = str(_attrs(own).get("incident_id"))
        node_id = str(own["id"])
        statement = (f"{label} already opened {incident_id}; the investigation continues on it "
                     f"(severity {_attrs(own).get('severity') or severity}).")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="IncidentOpened", statement=statement, actor_ref=actor,
                           extra={"graded_severity": severity, "incident_id": incident_id,
                                  "exception_group_id": group_id, "reopened_run": True})
        gap_ids = _gaps(connection, project_id, run_id=run_id, step_id=step_id,
                        statements=gaps, actor_ref=actor)
        return {**base, "action": "open", "continue_pipeline": True, "incident_id": incident_id,
                "subject_id": node_id, "incident_node_id": node_id,
                "severity": _attrs(own).get("severity") or severity,
                "rulebook_grade": rulebook_grade, "failure_reason": failure_reason,
                "discrepancy": discrepancy or None, "owner": owner, "tags": tags,
                "rule_node_id": rule.get("node_id"), "gaps": gaps, "note": statement,
                "summary": statement, "produced": _produced("IncidentOpened", decision,
                                                            statement, gap_ids, gaps)}
    # Otherwise: an open incident on the same rule (the graph first, then Service Desk).
    open_incident = connection.execute(text(
        "SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
        "AND valid_to IS NULL AND attrs->>'rule_id' = :r "
        "AND COALESCE(attrs->>'status', 'open') IN ('open', 'investigating', 'in_progress') "
        "ORDER BY recorded_at DESC LIMIT 1"), {"p": project_id, "r": rule_id}).mappings().first()
    open_id = str(_attrs(open_incident).get("incident_id") or "") if open_incident else ""
    if not open_id:
        listed, gap = _call(connection, project_id, "service-desk", "list_incidents",
                            {"rule_id": rule_id, "status": "open"}, calls)
        if listed is None:
            gaps.append(f"Service Desk was not asked for an open incident on {rule_id}: {gap}")
        else:
            tickets = [t for t in listed.get("incidents") or [] if t.get("domain") == "data_quality"]
            open_id = str(tickets[0]["incident_id"]) if tickets else ""
    # 7 Open or attach
    if open_id:
        action = "attach"
        incident_id = open_id
        ticket, gap = _call(connection, project_id, "service-desk", "attach_to_incident",
                            {"incident_id": incident_id, "source_ref": group_id,
                             "note": f"Attached {label}: {failure_reason}.", "by": "Incident Intake Agent"},
                            calls)
        if ticket is None:
            gaps.append(f"the Service Desk ticket {incident_id} was not updated: {gap}")
        node = _incident_node(connection, project_id, incident_id)
        if node is None:
            node_id = _write_incident(connection, project_id, incident_id, ticket or {}, group,
                                      rule, owner, severity, actor, status=str(
                                          (ticket or {}).get("status") or "investigating"))
        else:
            node_id = node["id"]
            current = str(_attrs(node).get("severity") or "Low")
            groups = list(dict.fromkeys([*(_attrs(node).get("exception_groups") or []), group_id]))
            # Severity only moves up.
            raised = severity if SEVERITIES.index(severity) > SEVERITIES.index(
                current if current in SEVERITIES else "Low") else current
            _patch(connection, project_id, node_id, {"exception_groups": groups,
                                                     "severity": raised})
            severity = raised
    else:
        action = "open"
        ticket, gap = _call(connection, project_id, "service-desk", "create_incident", {
            "title": f"{label}: {rule.get('title') or rule_id}",
            "summary": failure_reason, "assignment_group": owner.get("assignment_group") or "",
            "severity": severity, "source_ref": group_id, "rule_id": rule_id,
            "monitor_rule_id": group.get("monitor_rule_id"), "owner_id": owner.get("owner_id") or "",
            "opened": group.get("cob"), "cde_ids": tags["cde_ids"]}, calls)
        if ticket is None:
            gaps.append(f"no Service Desk ticket was opened ({gap}); the incident is recorded in the "
                        "graph only")
            incident_id = f"DQ-{group_id}"
        else:
            incident_id = str(ticket["incident_id"])
        node_id = _write_incident(connection, project_id, incident_id, ticket or {}, group, rule,
                                  owner, severity, actor, status="open")
    # 8 Link facts (links only, no interpretation)
    if group_node:
        _edge(connection, project_id, node_id, group_node, "raised_from", actor,
              {"link_type": action, "run_id": run_id})
    if action == "open" and rule:
        for past in connection.execute(text(
                "SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
                "AND valid_to IS NULL AND attrs->>'rule_id' = :r AND id <> :i "
                "AND attrs->>'status' = 'closed'"), {"p": project_id, "r": rule_id, "i": node_id}
        ).mappings().all():
            _edge(connection, project_id, node_id, str(past["id"]), "correlated_with", actor,
                  {"linkType": "same_rule", "run_id": run_id})
    # 9 Write trace, hand off
    decision_type = "IncidentOpened" if action == "open" else "IncidentAttached"
    statement = (f"{'Opened' if action == 'open' else 'Attached to'} {incident_id} for {label} "
                 f"({rule_id}), severity {severity}"
                 + (f", assigned to {owner.get('assignment_group')}" if owner else "")
                 + f". Failure reason: {failure_reason}." + (f" {discrepancy}" if discrepancy else ""))
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type=decision_type, statement=statement, actor_ref=actor,
                       extra={"graded_severity": severity, "incident_id": incident_id,
                              "exception_group_id": group_id, "monitor_grade": monitor_grade,
                              "rulebook_grade": rulebook_grade})
    gap_ids = _gaps(connection, project_id, run_id=run_id, step_id=step_id, statements=gaps,
                    actor_ref=actor)
    return {**base, "action": action, "continue_pipeline": action == "open",
            "incident_id": incident_id, "subject_id": node_id, "incident_node_id": node_id,
            "severity": severity, "rulebook_grade": rulebook_grade, "failure_reason": failure_reason,
            "discrepancy": discrepancy or None, "owner": owner, "tags": tags,
            "rule_node_id": rule.get("node_id"), "gaps": gaps,
            "note": statement[:600], "summary": statement[:1200],
            "produced": _produced(decision_type, decision, statement, gap_ids, gaps,
                                  **({"incident": {"node_id": node_id}} if action == "open" else {}))}


def _write_incident(connection: Connection, project_id: str, incident_id: str,
                    ticket: Mapping[str, Any], group: Mapping[str, Any], rule: Mapping[str, Any],
                    owner: Mapping[str, Any], severity: str, actor: str, *, status: str) -> str:
    rule_id = str(group.get("rule_id") or "")
    node_id = STORE.upsert_node(
        connection, project_id=project_id, kind="incident",
        node_key=dq_platform.node_key("incident", incident_id),
        name=str(ticket.get("title") or f"{incident_id}: {rule_id}")[:300],
        body=str(ticket.get("summary") or "")[:2000], trust_tier="inferred", actor_kind="agent",
        actor_ref=actor, source="dq_pipeline",
        attrs={"incident_id": incident_id, "key": incident_id, "status": status,
               "rule_id": rule_id, "monitor_rule_id": group.get("monitor_rule_id"),
               "severity": severity, "opened": group.get("cob"),
               "exception_groups": [group.get("group_id")], "cde_ids": list(rule.get("cde_ids") or []),
               "owner_id": owner.get("owner_id"), "assignment_group": owner.get("assignment_group"),
               "raised_from": group.get("group_id"), "domain": "data_quality"})
    if rule.get("node_id"):
        _edge(connection, project_id, node_id, rule["node_id"], "concerns", actor)
    for cde in rule.get("cde_ids") or []:
        _edge(connection, project_id, node_id,
              dq_platform.node_id_of(connection, project_id, "cde", cde), "concerns", actor)
    if owner.get("node_id"):
        _edge(connection, project_id, node_id, owner["node_id"], "assigned_to", actor)
    return node_id


# ── 2. Context Hydration (deterministic) ────────────────────────────────────────────────────

def run_dq_context_enrichment(connection: Connection, settings: Any, project_id: str,
                              step: Mapping[str, Any], context: Mapping[str, Any],
                              inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "context_enrichment")
    round_no = _round(connection, run_id, step_id)
    steps = _steps(connection, run_id, context)
    mon = steps.get("monitoring") or {}
    rca = steps.get("rca") or {}
    # A re-run after InsufficientEvidence widens its reach (CTX-P1/P2): one more hop, twice
    # the change look-back — and is told what RCA said was missing.
    hop_cap = HOP_CAP + (round_no - 1)
    lookback = CHANGE_LOOKBACK_DAYS * (2 ** (round_no - 1))
    calls: list[dict[str, Any]] = []
    gaps: list[str] = []
    consulted: list[str] = []
    # 1 Load the incident
    incident_node = str(mon.get("incident_node_id") or "")
    if not incident_node:
        raise ValueError("Context Hydration runs after Incident Intake opens an incident (never on "
                         "an attach); this run has none.")
    rule_id = str(mon.get("rule_id") or "")
    column = str(mon.get("column") or "")
    table = str(mon.get("table") or column.rsplit(".", 1)[0])
    cob = str(mon.get("cob") or "")
    platform = str(mon.get("platform") or "")
    # 2 Rule footprint
    rule = _rule_facts(connection, project_id, rule_id)
    if not rule:
        gaps.append(f"rule {rule_id} is not in the graph yet — load the rulebook")
    else:
        consulted.append(rule["node_id"])
    footprint = {"rule": {k: rule.get(k) for k in ("rule_id", "title", "check", "dimension",
                                                     "severity", "warning_value", "error_value",
                                                     "metric", "version")} if rule else None,
                 "cdes": [], "columns": rule.get("columns") or [column],
                 "tables": rule.get("tables") or [table], "feed": rule.get("feed_id"),
                 "related_rules": rule.get("related") or []}
    for cde_id in rule.get("cde_ids") or mon.get("tags", {}).get("cde_ids") or []:
        node = _by_key(connection, project_id, "cde", cde_id)
        if node is None:
            gaps.append(f"CDE {cde_id} is not in the graph")
            continue
        consulted.append(node["id"])
        footprint["cdes"].append({"cde_id": cde_id, "name": node["name"],
                                  "steward_id": _attrs(node).get("steward_id")})
    if not footprint["cdes"]:
        gaps.append(f"no CDE mapping for {rule_id}")
    # 3 Owners
    owners: list[dict[str, Any]] = []
    for role, owner_id in [("rule owner", rule.get("owner_id"))] + [
            ("CDE steward", c["steward_id"]) for c in footprint["cdes"]]:
        found = _owner(connection, project_id, owner_id)
        if found:
            owners.append({"role": role, **{k: found.get(k) for k in (
                "owner_id", "name", "title", "assignment_group")}})
            consulted.append(found["node_id"])
    system = _by_key(connection, project_id, "system", platform) if platform else None
    if system is not None:
        consulted.append(system["id"])
        found = _owner(connection, project_id, _attrs(system).get("owner_id"))
        if found:
            owners.append({"role": f"{system['name']} owner",
                           **{k: found.get(k) for k in ("owner_id", "name", "title",
                                                        "assignment_group")}})
    if not any(o["role"] == "rule owner" for o in owners):
        gaps.append(f"no owner recorded for {rule_id}")
    # 4 Subscriptions: the feeds that load the failing table and the ones that read it.
    feeds_in, feeds_out = [], []
    table_node = _by_key(connection, project_id, "database_table", table) if table else None
    if table_node is not None:
        consulted.append(table_node["id"])
        for e in _edges(connection, project_id, table_node["id"], direction="in",
                        kinds=("feeds", "reads")):
            if e["kind"] != "feed":
                continue
            fa = _attrs(e)
            entry = {"feed_id": _key_of(e), "name": e["name"], "sla": fa.get("sla"),
                     "frequency": fa.get("frequency"), "from": fa.get("source_system"),
                     "to": fa.get("target_system"), "reads": fa.get("reads"),
                     "node_id": e["id"]}
            (feeds_in if e["edge_kind"] == "feeds" else feeds_out).append(entry)
            consulted.append(e["id"])
    # 5 Upstream lineage (hop cap), and the hop a feed implies but nobody registered.
    lineage: list[dict[str, Any]] = []
    frontier, seen, hops = [column], set(), 0
    while frontier and hops < hop_cap:
        nxt = []
        for col in frontier:
            node = _by_key(connection, project_id, "database_column", col)
            if node is None:
                continue
            for hop in _edges(connection, project_id, node["id"], direction="in",
                              kinds=("lineage_to",)):
                if hop["id"] in seen:
                    continue
                seen.add(hop["id"])
                ha = _attrs(hop)
                lineage.append({"lineage_id": _key_of(hop), "from": ha.get("from_column"),
                                "to": ha.get("to"), "feed_id": ha.get("feed_id"),
                                "transform": ha.get("transform"), "hop": hops + 1})
                consulted.append(hop["id"])
                if ha.get("from_column"):
                    nxt.append(str(ha["from_column"]))
        frontier, hops = nxt, hops + 1
    if feeds_in and not lineage:
        for feed in feeds_in:
            sources = list(feed.get("reads") or [])
            ruled = [t for t in sources if any(str(c).startswith(f"{t}.") for c in footprint["columns"])]
            source = (ruled or sources or ["its source"])[0]
            gaps.append(f"lineage {source} -> {table} ({feed['feed_id']}) not registered: "
                        f"{feed['feed_id']} loads {table}, but no column-level hop lands in "
                        f"{column}")
    # Downstream reach of the failing column (what the break flows into).
    downstream: list[dict[str, Any]] = []
    frontier, seen_out, hops = [column], set(), 0
    while frontier and hops < hop_cap:
        nxt = []
        for col in frontier:
            node = _by_key(connection, project_id, "database_column", col)
            if node is None:
                continue
            for hop in _edges(connection, project_id, node["id"], direction="in",
                              kinds=("lineage_from",)):
                if hop["id"] in seen_out:
                    continue
                seen_out.add(hop["id"])
                ha = _attrs(hop)
                downstream.append({"lineage_id": _key_of(hop), "to": ha.get("to"),
                                   "to_kind": ha.get("to_kind"), "feed_id": ha.get("feed_id"),
                                   "hop": hops + 1})
                if ha.get("to_kind") == "database_column" and ha.get("to"):
                    nxt.append(str(ha["to"]))
        frontier, hops = nxt, hops + 1
    # 6 Regulatory reach (CDE -REPORTED_IN-> line; the rule's own REACHES)
    lines: dict[str, dict[str, Any]] = {}
    for cde in footprint["cdes"]:
        node = _by_key(connection, project_id, "cde", cde["cde_id"])
        for e in _edges(connection, project_id, node["id"], direction="out",
                        kinds=("reported_in",)) if node else []:
            lines.setdefault(_key_of(e), {"line_id": _key_of(e), "caption": e["name"],
                                          "via": cde["cde_id"], "node_id": e["id"]})
    for line_id in rule.get("report_lines") or []:
        node = _by_key(connection, project_id, "report_line", line_id)
        if node is not None:
            lines.setdefault(line_id, {"line_id": line_id, "caption": node["name"],
                                       "via": rule_id, "node_id": node["id"]})
    reach = []
    for line in list(lines.values())[:LIST_CAP]:
        la = _attrs(_node(connection, project_id, line["node_id"]))
        consulted.append(line["node_id"])
        reach.append({**{k: v for k, v in line.items() if k != "node_id"},
                      "report_id": la.get("report_id"), "mdrm": la.get("mdrm")})
    if footprint["cdes"] and not reach:
        gaps.append(f"no regulatory route recorded for {', '.join(c['cde_id'] for c in footprint['cdes'])}")
    # 7 Known issues at every level: the rule, its CDEs, feed, system and table.
    anchors = [n for n in [rule.get("node_id"), system["id"] if system else None,
                           table_node["id"] if table_node else None,
                           *[f["node_id"] for f in feeds_in],
                           *[dq_platform.node_id_of(connection, project_id, "cde", c["cde_id"])
                             for c in footprint["cdes"]],
                           dq_platform.node_id_of(connection, project_id, "feed",
                                                  str(rule.get("feed_id") or ""))
                           if rule.get("feed_id") else None] if n]
    issues: dict[str, dict[str, Any]] = {}
    for anchor in dict.fromkeys(anchors):
        for e in _edges(connection, project_id, anchor, direction="in", kinds=("concerns",)):
            if e["kind"] not in ("issue", "examination_finding"):
                continue
            ea = _attrs(e)
            issues.setdefault(_key_of(e), {"id": _key_of(e), "kind": e["kind"],
                                           "title": e["name"], "status": ea.get("status"),
                                           "severity": ea.get("severity"),
                                           "description": ea.get("description"),
                                           "node_id": e["id"]})
    known = list(issues.values())[:LIST_CAP]
    consulted += [i["node_id"] for i in known]
    # 8 Exception profile (Profile Service, the Profile Service)
    profile, gap = exception_profile(connection, project_id, platform=platform, rule_id=rule_id,
                                     measured=mon.get("measured"), calls=calls)
    if profile is None:
        gaps.append(f"no exception profile: {gap}")
    # 9 Recent changes (the Profile Service), the platform's and the rule's and the feed's, in the look-back.
    changes: dict[str, dict[str, Any]] = {}
    for args in ({"platform": platform}, {"rule_id": rule_id},
                 *({"feed_id": f} for f in {rule.get("feed_id"), *[x["feed_id"] for x in feeds_in]}
                   if f)):
        found, gap = _call(connection, project_id, "profile-service", "list_recent_changes",
                           {**args, "until": cob, "lookback_days": lookback}, calls)
        if found is None:
            gaps.append(f"recent changes not read: {gap}")
            break
        for c in found.get("changes") or []:
            changes.setdefault(c["change_id"], c)
    recent = sorted(changes.values(), key=lambda c: (c["day"], c["change_id"]))[:LIST_CAP]
    # 10 Assemble, hand off (ContextAssembled: contextPack and asOf)
    pack = {"exception": {k: mon.get(k) for k in ("exception_group_id", "group_label", "rule_id",
                                                  "column", "platform", "cob", "metric",
                                                  "measured", "failed", "population",
                                                  "monitor_grade", "rulebook_grade",
                                                  "discrepancy", "severity", "incident_id")},
            "footprint": footprint, "owners": owners, "feeds_in": feeds_in,
            "feeds_out": feeds_out, "upstream_lineage": lineage, "downstream": downstream,
            "regulatory_reach": reach, "known_issues": [{k: v for k, v in i.items()
                                                         if k != "node_id"} for i in known],
            "profile": profile, "recent_changes": recent}
    asked = list(rca.get("missing") or []) if round_no > 1 else []
    statement = (f"Context assembled for {mon.get('incident_id')} (round {round_no}, as of COB "
                 f"{cob}): {len(footprint['cdes'])} CDE(s), {len(owners)} owner(s), "
                 f"{len(feeds_in)} feed(s) in, {len(lineage)} upstream lineage hop(s), "
                 f"{len(reach)} report line(s) reached, {len(known)} known issue(s), "
                 f"{'a' if profile else 'no'} profile, {len(recent)} recent change(s); "
                 f"{len(gaps)} coverage gap(s).")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="ContextAssembled", statement=statement, round_no=round_no,
                       actor_ref=ENRICHMENT,
                       extra={"context_pack": pack, "as_of": cob, "hop_cap": hop_cap,
                              "change_lookback_days": lookback, "asked_for": asked})
    gap_ids = _gaps(connection, project_id, run_id=run_id, step_id=step_id, statements=gaps,
                    round_no=round_no, actor_ref=ENRICHMENT)
    return {"capability": "dq_context_enrichment", "tool": None, "status": "succeeded",
            "round": round_no, "incident_id": mon.get("incident_id"),
            "subject_id": incident_node, "context_pack": pack, "as_of": cob,
            "policy": {"hop_cap": hop_cap, "change_lookback_days": lookback, "list_cap": LIST_CAP},
            "gaps": gaps, "consulted_ids": list(dict.fromkeys(consulted)), "calls": calls,
            "asked_for": asked, "note": statement, "summary": statement,
            "ran_as": _ran_as(ENRICHMENT),
            "produced": _produced("ContextAssembled", decision, statement, gap_ids, gaps)}


# ── 3. Past Incidents (deterministic) ────────────────────────────────────────────────────

def _calendar_note(connection: Connection, project_id: str, cob: str) -> dict[str, Any]:
    if not cob:
        return {}
    day = date.fromisoformat(cob)
    quarter_start = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
    rows = connection.execute(text(
        "SELECT node_key, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'calendar_day' "
        "AND valid_to IS NULL AND attrs->>'day' BETWEEN :a AND :b"),
        {"p": project_id, "a": quarter_start.isoformat(), "b": cob}).mappings().all()
    business = sorted(str(_attrs(r).get("day")) for r in rows if _attrs(r).get("business_day"))
    this = next((_attrs(r) for r in rows if _attrs(r).get("day") == cob), {})
    if not rows:
        return {"cob": cob, "known": False}
    return {"cob": cob, "known": True, "weekday": this.get("weekday"),
            "business_day": bool(this.get("business_day")),
            "business_day_of_quarter": (business.index(cob) + 1) if cob in business else None,
            "quarter": f"Q{(day.month - 1) // 3 + 1}", "holiday": this.get("holiday")}


def run_dq_historical_pattern(connection: Connection, settings: Any, project_id: str,
                              step: Mapping[str, Any], context: Mapping[str, Any],
                              inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "historical_pattern")
    round_no = _round(connection, run_id, step_id)
    steps = _steps(connection, run_id, context)
    mon = steps.get("monitoring") or {}
    enrich = steps.get("context_enrichment") or {}
    calls: list[dict[str, Any]] = []
    actor = HISTORICAL
    # 1 Load the pack
    pack = enrich.get("context_pack") or {}
    incident_node = str(mon.get("incident_node_id") or "")
    rule_id = str(mon.get("rule_id") or "")
    column = str(mon.get("column") or "")
    cob = str(mon.get("cob") or "")
    cdes = {c["cde_id"] for c in (pack.get("footprint") or {}).get("cdes") or []}
    # 2 Sibling rules: the rule's related rules, and the rules on the same CDE or column.
    rule = _rule_facts(connection, project_id, rule_id)
    siblings = set(rule.get("related") or [])
    for row in connection.execute(text(
            "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind IN ('data_quality_rule', "
            "'rule') AND valid_to IS NULL AND (attrs->>'rule_id' IS NOT NULL OR attrs->>'rule_code' "
            "IS NOT NULL)"), {"p": project_id}).scalars().all():
        attrs = row or {}
        other = str(attrs.get("rule_id") or attrs.get("rule_code") or "")
        if other and other != rule_id and (cdes & set(attrs.get("cde_ids") or [])
                                           or column in (attrs.get("columns") or [])):
            siblings.add(other)
    # 3 Reference data: the business calendar and the resolved patterns (remediation templates).
    calendar = _calendar_note(connection, project_id, cob)
    patterns = {}
    for row in connection.execute(text(
            "SELECT id, name, attrs FROM kg_nodes WHERE project_id = :p AND kind = "
            "'resolved_pattern' AND valid_to IS NULL"), {"p": project_id}).mappings().all():
        pa = _attrs(row)
        patterns[str(pa.get("pattern_id") or pa.get("key"))] = {
            "pattern_id": pa.get("pattern_id") or pa.get("key"), "name": row["name"],
            "family": pa.get("family"), "signature": pa.get("signature"),
            "durable_fix": pa.get("durable_fix"), "node_id": row["id"],
            "rule_ids": list(pa.get("rule_ids") or [])}
    # 4 find_past_incidents: two tiers, deterministic, closed only, two years.
    since = (date.fromisoformat(cob) - timedelta(days=HISTORY_LOOKBACK_DAYS)).isoformat() \
        if cob else "0000"
    changes = {c["change_id"]: c for c in pack.get("recent_changes") or []}
    matches = []
    for row in connection.execute(text(
            "SELECT id, name, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
            "AND valid_to IS NULL AND attrs->>'status' = 'closed' AND id <> :i"),
            {"p": project_id, "i": incident_node or ""}).mappings().all():
        ia = _attrs(row)
        if str(ia.get("opened") or "") < since:
            continue
        same_rule = ia.get("rule_id") == rule_id
        shared_cde = sorted(cdes & set(ia.get("cde_ids") or []))
        sibling = ia.get("rule_id") in siblings
        if not (same_rule or sibling or shared_cde):
            continue
        tier = 1 if same_rule else 2
        pattern = patterns.get(str(ia.get("pattern_id") or ""))
        # timingAlignment: a fix that held until a change regressed it, and a recent change on
        # the same rule now — the facts a person would line up, never a verdict.
        regressions = [c for c in changes.values() if rule_id in (c.get("rule_ids") or [])]
        matches.append({
            "incident_id": ia.get("incident_id"), "node_id": row["id"], "title": row["name"],
            "tier": tier, "rule_id": ia.get("rule_id"), "opened": ia.get("opened"),
            "closed": ia.get("closed"), "family": ia.get("root_cause_family"),
            "pattern_id": ia.get("pattern_id"), "pattern": (pattern or {}).get("name"),
            "confirmed_cause": ia.get("confirmed_cause"), "ruled_out": ia.get("ruled_out") or [],
            "fix": ia.get("fix"), "fix_type": ia.get("fix_type"),
            "fix_durability": ia.get("fix_durability"),
            "durability_note": ia.get("durability_note"),
            "same_column": same_rule and column in (rule.get("columns") or [column]),
            "shared_cdes": shared_cde,
            "timing_alignment": ([f"{c['change_id']} ({c['day']}) touches {rule_id}"
                                  for c in regressions]
                                 if ia.get("fix_durability") in ("held_until_regressed",
                                                                 "did_not_hold") else [])})
    matches.sort(key=lambda m: (m["tier"], -(int(str(m["opened"] or "0").replace("-", "") or 0))))
    matches = matches[:LIST_CAP]
    # 5 Write precedents: each match's precedent (only Past Incidents creates them),
    #   MATCHED_INCIDENT from the new incident, and REFERENCES from precedent to pattern.
    precedents, written = [], []
    for m in matches:
        key = f"PRE-{str(m['incident_id'])[4:]}"
        pre = _by_key(connection, project_id, "precedent", key)
        if pre is None:
            pre_id = STORE.upsert_node(
                connection, project_id=project_id, kind="precedent",
                node_key=dq_platform.node_key("precedent", key),
                name=f"{m['incident_id']}: {m['title']}"[:300],
                body=f"Cause: {m['confirmed_cause']} Fix: {m['fix']} Durability: "
                     f"{m['fix_durability']} ({m['durability_note']})",
                trust_tier="inferred", actor_kind="agent", actor_ref=actor, source="dq_pipeline",
                attrs={"precedent_id": key, "incident_id": m["incident_id"],
                       "pattern_id": m["pattern_id"], "domain": "data_quality"})
            _edge(connection, project_id, pre_id, m["node_id"], "from_incident", actor)
            written.append(pre_id)
        else:
            pre_id = pre["id"]
        m["precedent_id"], m["precedent_node_id"] = key, pre_id
        precedents.append(pre_id)
        if incident_node:
            _edge(connection, project_id, incident_node, m["node_id"], "matched_incident", actor,
                  {"tier": m["tier"], "sameColumn": m["same_column"],
                   "timingAlignment": m["timing_alignment"], "run_id": run_id})
        pattern = patterns.get(str(m["pattern_id"] or ""))
        if pattern:
            _edge(connection, project_id, pre_id, pattern["node_id"], "references", actor,
                  {"run_id": run_id})
    by_pattern: dict[str, dict[str, Any]] = {}
    for m in matches:
        pid = str(m["pattern_id"] or "unpatterned")
        group = by_pattern.setdefault(pid, {"pattern_id": m["pattern_id"],
                                            "pattern": m["pattern"], "family": m["family"],
                                            "incidents": [], "durable_fix":
                                            (patterns.get(pid) or {}).get("durable_fix")})
        group["incidents"].append(m["incident_id"])
    for group in by_pattern.values():
        held = [m["fix_durability"] for m in matches if str(m["pattern_id"] or "unpatterned")
                == str(group["pattern_id"] or "unpatterned")]
        group["recurrences"] = len(group["incidents"])
        group["fix_durability"] = held
    # 6 Post briefing on the ticket
    tier1 = [m for m in matches if m["tier"] == 1]
    briefing = (f"Past Incidents: {len(tier1)} closed incident(s) on {rule_id} "
                f"({', '.join(m['incident_id'] for m in tier1) or 'none'}); "
                f"{len(matches) - len(tier1)} on sibling rules or the same CDE. "
                + " ".join(f"{g['pattern_id']}: {g['recurrences']} incident(s), fixes "
                           f"{', '.join(str(x) for x in g['fix_durability'])}."
                           for g in by_pattern.values() if g["pattern_id"]))
    if mon.get("incident_id"):
        posted, gap = _call(connection, project_id, "service-desk", "update_incident",
                            {"incident_id": mon["incident_id"], "note": briefing[:1900],
                             "by": "Past Incidents Agent"}, calls)
    # 7 Hand off
    statement = (f"Matched {len(matches)} closed incident(s) for {mon.get('incident_id')} "
                 f"(round {round_no}): "
                 + ("; ".join(f"{m['incident_id']} tier {m['tier']} "
                              f"({m['pattern_id'] or 'no pattern'}, fix {m['fix_durability']})"
                              for m in matches[:6])
                    or "none — zero matches is a fact, not insufficient evidence")
                 + ".")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="HistoricalPatternMatched", statement=statement,
                       round_no=round_no, actor_ref=actor,
                       extra={"matched": [m["incident_id"] for m in matches],
                              "patterns": list(by_pattern)})
    return {"capability": "dq_historical_pattern", "tool": None, "status": "succeeded",
            "round": round_no, "incident_id": mon.get("incident_id"), "subject_id": incident_node,
            "matches": matches, "matched": [m["node_id"] for m in matches],
            "precedent_ids": precedents, "patterns": list(by_pattern.values()),
            "siblings": sorted(siblings), "calendar": calendar, "calls": calls,
            "briefing": briefing, "note": statement[:600], "summary": statement[:1200],
            "ran_as": _ran_as(HISTORICAL),
            # A precedent it wrote is its write; one already recorded is linked, not rewritten.
            "produced": _produced("HistoricalPatternMatched", decision, statement,
                                  **({"precedent": {"node_ids": written}} if written else {}))}


# ── 4. RCA (role agent, LLM) ─────────────────────────────────────────────────────────────────

def _evidence_catalogue(mon: Mapping[str, Any], enrich: Mapping[str, Any],
                        history: Mapping[str, Any]) -> dict[str, str]:
    """Everything an agent may cite, by a short id: changes, precedents, issues, profile, gaps,
    lineage hops, the exception itself."""
    pack = enrich.get("context_pack") or {}
    out: dict[str, str] = {"EXCEPTION": (
        f"{mon.get('group_label')}: {mon.get('failure_reason')}"
        + (f" {mon.get('discrepancy')}" if mon.get("discrepancy") else ""))}
    for c in pack.get("recent_changes") or []:
        out[c["change_id"]] = (f"{c['day']} on {c['system_id']}: {c['description']} (rules "
                               f"{', '.join(c.get('rule_ids') or []) or '-'}; feeds "
                               f"{', '.join(c.get('feed_ids') or []) or '-'})")
    for m in history.get("matches") or []:
        out[str(m["incident_id"])] = (
            f"tier {m['tier']} {m['rule_id']} {m['opened']}..{m['closed']}: {m['title']}. "
            f"Cause ({m['family']}, {m['pattern_id']}): {m['confirmed_cause']} Ruled out: "
            f"{', '.join(m['ruled_out']) or '-'}. Fix: {m['fix']} Durability: "
            f"{m['fix_durability']} — {m['durability_note']}"
            + (f" Timing: {'; '.join(m['timing_alignment'])}." if m["timing_alignment"] else ""))
    for i in pack.get("known_issues") or []:
        out[str(i["id"])] = f"{i['kind'].replace('_', ' ')} ({i['status']}): {i['title']}. " \
                            f"{i.get('description') or ''}"
    if pack.get("profile"):
        out["PROFILE"] = f"The Profile Service's profile of {mon.get('rule_id')} on {mon.get('platform')}: " \
                         f"{describe_profile(pack['profile'])}"
    for n, gap in enumerate(enrich.get("gaps") or [], start=1):
        out[f"GAP-{n}"] = f"coverage gap: {gap}"
    for hop in pack.get("upstream_lineage") or []:
        out[str(hop["lineage_id"])] = f"lineage {hop['from']} -> {hop['to']} via " \
                                      f"{hop.get('feed_id') or 'in-system'} ({hop.get('transform')})"
    for feed in pack.get("feeds_in") or []:
        out[str(feed["feed_id"])] = f"feed {feed['name']} loads the table; SLA {feed.get('sla')}, " \
                                    f"{feed.get('frequency')}"
    return out


def _sections(mon: Mapping[str, Any], enrich: Mapping[str, Any], history: Mapping[str, Any],
              catalogue: Mapping[str, str]) -> list[Section]:
    pack = enrich.get("context_pack") or {}
    tier1 = [str(m["incident_id"]) for m in history.get("matches") or [] if m["tier"] == 1]
    tier2 = [str(m["incident_id"]) for m in history.get("matches") or [] if m["tier"] == 2]
    fp = pack.get("footprint") or {}
    cite = lambda ids: [f"[{i}] {catalogue[i]}" for i in ids if i in catalogue]  # noqa: E731
    return [
        Section("Exception", cite(["EXCEPTION"]) + [
            f"Rule: {json.dumps(fp.get('rule'))}",
            f"CDEs: {', '.join(c['cde_id'] + ' ' + c['name'] for c in fp.get('cdes') or []) or '-'}"],
            must=True, priority=100),
        Section("Coverage gaps (what Context Hydration could not establish)",
                cite([k for k in catalogue if k.startswith("GAP-")]), must=True, priority=95),
        Section("Closed incidents on this rule (tier 1 precedents)", cite(tier1), priority=90),
        Section("Recent changes", cite([k for k in catalogue if k.startswith("CHG-")]),
                priority=85),
        Section("Exception profile", cite(["PROFILE"]), priority=80),
        Section("Known issues and examination findings",
                cite([k for k in catalogue if k.startswith(("ISS-", "EF-"))]), priority=70),
        Section("Feeds and upstream lineage",
                cite([k for k in catalogue if k.startswith(("F-", "LIN-"))]), priority=60),
        Section("Regulatory reach", [f"{r['line_id']} ({r.get('mdrm')}): {r['caption']}"
                                     for r in pack.get("regulatory_reach") or []], priority=40),
        Section("Owners", [f"{o['role']}: {o['name']} ({o.get('assignment_group') or '-'})"
                           for o in pack.get("owners") or []], priority=35),
        Section("Closed incidents on sibling rules or the same CDE (tier 2)", cite(tier2),
                priority=30),
        Section("Business calendar", [json.dumps(history.get("calendar") or {})], priority=20),
    ]


def _clean_ids(raw: Any, allowed: Mapping[str, Any]) -> list[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip() in allowed]


def _confidence(raw: Any, default: float = 0.5) -> float:
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return default


def run_dq_rca(connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
               context: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "rca")
    round_no = _round(connection, run_id, step_id)
    steps = _steps(connection, run_id, context)
    mon, enrich = steps.get("monitoring") or {}, steps.get("context_enrichment") or {}
    history = steps.get("historical_pattern") or {}
    catalogue = _evidence_catalogue(mon, enrich, history)
    curated = curate(_sections(mon, enrich, history, catalogue), TOKEN_BUDGETS["rca"])
    precedent_ids = {str(m["incident_id"]): m for m in history.get("matches") or []}
    situation = (
        f"INCIDENT {mon.get('incident_id')} — round {round_no} of at most 3"
        + (f" (earlier rounds said evidence was missing: "
           f"{'; '.join(enrich.get('asked_for') or [])})" if round_no > 1 else "") + "\n"
        f"ROOT-CAUSE FAMILIES: " + "; ".join(f"{k}: {v}" for k, v in FAMILIES.items()) + "\n\n"
        + curated.text + "\n\nEVIDENCE YOU MAY CITE (by the id in brackets): "
        + ", ".join(catalogue))
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, RCA):
        judged = _ask_role(agent, _role_prompt("rca_agent.md", situation))
    candidates: list[dict[str, Any]] = []
    missing: list[str] = []
    rationale = ""
    why = ""
    if judged is None:
        why = "the Root Cause Analysis Agent answered nothing readable"
    else:
        rationale = " ".join(str(judged.get("rationale") or "").split())[:1200]
        missing = [" ".join(str(x).split())[:300] for x in judged.get("missing") or [] if str(x).strip()]
        for raw in judged.get("candidates") or []:
            if not isinstance(raw, Mapping):
                continue
            family = str(raw.get("family") or "").strip()
            evidence = _clean_ids(raw.get("evidence"), catalogue)
            used = _clean_ids(raw.get("precedents"), precedent_ids)
            tier = 1 if used else 2
            if family not in FAMILIES or not evidence:
                continue  # a family it may not name, or a claim that cites nothing it was given
            lifts = raw.get("confidence_lift") if isinstance(raw.get("confidence_lift"), Mapping) else {}
            candidates.append({"family": family,
                               "cause": " ".join(str(raw.get("cause") or "").split())[:600],
                               "confidence": _confidence(raw.get("confidence")), "tier": tier,
                               "evidence": evidence, "precedents": used,
                               "confidence_lift": {p: _confidence(lifts.get(p), 0.15)
                                                   for p in used}})
    said_insufficient = bool(judged) and str(judged.get("verdict") or "") == INSUFFICIENT
    verdict = INSUFFICIENT if (said_insufficient or not candidates) else "Candidates"
    if verdict == INSUFFICIENT and not missing:
        missing = [why or "no candidate cited evidence it was given"]
    candidates.sort(key=lambda c: -c["confidence"])
    incident_node = str(mon.get("incident_node_id") or "")
    for cand in candidates:
        for pid in cand["precedents"]:
            _edge(connection, project_id, incident_node, precedent_ids[pid].get(
                "precedent_node_id") or precedent_ids[pid]["node_id"], "used_precedent", RCA,
                {"confidenceLift": cand["confidence_lift"].get(pid), "family": cand["family"],
                 "run_id": run_id, "round": round_no})
    if verdict == INSUFFICIENT:
        statement = (f"Insufficient evidence for {mon.get('incident_id')} (round {round_no}): "
                     f"missing {'; '.join(missing)}. {rationale}").strip()
    else:
        statement = (f"Candidate root causes for {mon.get('incident_id')} (round {round_no}): "
                     + "; ".join(f"{c['family']} {c['confidence']:.2f} (tier {c['tier']}"
                                 + (f", citing {', '.join(c['precedents'])}" if c['precedents']
                                    else "") + f"): {c['cause']}" for c in candidates)
                     + (f". {rationale}" if rationale else ""))
    decision_type = "InsufficientEvidence" if verdict == INSUFFICIENT else "RootCauseCandidates"
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type=decision_type, statement=statement, round_no=round_no,
                       actor_ref=RCA, extra={"candidates": candidates, "missing": missing,
                                             "curator": curated.report()})
    return {"capability": "dq_rca", "tool": None, "status": "succeeded", "round": round_no,
            "incident_id": mon.get("incident_id"), "subject_id": incident_node,
            "rca_verdict": verdict, "candidates": candidates, "missing": missing,
            "rationale": rationale, "families": [c["family"] for c in candidates],
            "cited_fact_ids": [precedent_ids[p]["node_id"] for c in candidates
                               for p in c["precedents"]],
            "curator": curated.report(), "curator_note": curated.note(),
            "note": statement[:600], "summary": statement[:1200], "ran_as": _ran_as(RCA),
            "learned": _learned(run_id, step_id, kind="decision",
                                title=f"{mon.get('incident_id')}: {decision_type}",
                                statement=statement,
                                confidence=candidates[0]["confidence"] if candidates else 0.3),
            "produced": _produced(decision_type, decision, statement)}


# ── 5. Flow Orchestrator (routing by policy) ─────────────────────────────────────────────

def run_dq_orchestration(connection: Connection, settings: Any, project_id: str,
                         step: Mapping[str, Any], context: Mapping[str, Any],
                         inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "pipeline_orchestrator")
    steps = _steps(connection, run_id, context)
    mon, rca = steps.get("monitoring") or {}, steps.get("rca") or {}
    lanes: list[dict[str, Any]] = []
    held: list[str] = []
    if rca.get("rca_verdict") == INSUFFICIENT:
        statement = (f"No swimlane for {mon.get('incident_id')}: RCA still found insufficient "
                     f"evidence after {rca.get('round') or 1} round(s) "
                     f"({'; '.join(rca.get('missing') or [])}). The incident stays open for a "
                     "person and the Escalation Watch's daily ladder.")
    else:
        by_family: dict[str, list[dict[str, Any]]] = {}
        for cand in rca.get("candidates") or []:
            if cand["confidence"] < MIN_ROUTE_CONFIDENCE:
                held.append(f"{cand['family']} ({cand['confidence']:.2f})")
                continue
            by_family.setdefault(cand["family"], []).append(cand)
        for family, cands in sorted(by_family.items(), key=lambda kv: -kv[1][0]["confidence"]):
            lanes.append({"lane": LANE_OF[family], "family": family,
                          "confidence": cands[0]["confidence"], "candidates": cands,
                          "incident_id": mon.get("incident_id")})
        statement = (f"Routed {mon.get('incident_id')} to "
                     + (", ".join(f"{l['lane']} ({l['confidence']:.2f})" for l in lanes)
                        or "no swimlane")
                     + (f"; below the routing floor ({MIN_ROUTE_CONFIDENCE}): {', '.join(held)}"
                        if held else "") + ".")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="Routed", statement=statement, actor_ref=ORCHESTRATOR,
                       extra={"lanes": [l["lane"] for l in lanes]})
    return {"capability": "dq_orchestration", "tool": None, "status": "succeeded",
            "incident_id": mon.get("incident_id"), "lanes": lanes,
            "insufficient_evidence": rca.get("rca_verdict") == INSUFFICIENT,
            "subject_id": mon.get("incident_node_id"), "note": statement[:600],
            "summary": statement, "ran_as": _ran_as(ORCHESTRATOR),
            "produced": _produced("Routed", decision, statement)}


# ── 6. Lanes (role agent, LLM; one per family) ───────────────────────────────────────────────

_LANE_FOCUS = {
    "source_processing": "the source system's own processing: job ordering, recent platform "
                         "releases, how earlier fixes held, whether the breaks cluster on what "
                         "the change touched",
    "feed_etl": "the feed: when it landed against its SLA, partial loads, recent feed or batch "
                "changes",
    "reference_data": "configuration and mapping data: recent mapping changes and whether the "
                      "break equals one product's balance",
    "manual_entry": "people and channels: onboarding channel changes, duplicates by name and "
                    "birth date",
    "rule_config": "the rule itself: the DQ Monitor's configured thresholds against the rulebook's, "
                   "whether the data sits at its usual baseline, recent rule-pack imports",
}


def run_dq_lane(connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
                context: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "lane")
    item = dict((context or {}).get("item") or inputs.get("item") or {})
    family = str(item.get("family") or "")
    if family not in FAMILIES:
        raise ValueError(f"a swimlane runs one root-cause family; got {family or 'none'}")
    steps = _steps(connection, run_id, context)
    mon, enrich = steps.get("monitoring") or {}, steps.get("context_enrichment") or {}
    history = steps.get("historical_pattern") or {}
    catalogue = _evidence_catalogue(mon, enrich, history)
    sections = _sections(mon, enrich, history, catalogue)
    sections.insert(1, Section("Candidates for this swimlane", [
        f"{c['family']} {c['confidence']:.2f} (tier {c['tier']}): {c['cause']} — cites "
        f"{', '.join(c['evidence'])}" for c in item.get("candidates") or []], must=True,
        priority=99))
    if family == "rule_config":
        rule = _rule_facts(connection, project_id, str(mon.get("rule_id") or ""))
        sections.insert(2, Section("Thresholds", [
            f"Rulebook thresholds: Warning above {rule.get('warning_value')}, Error above "
            f"{rule.get('error_value')} ({rule.get('metric')})" if rule else
            "The rulebook's thresholds are not in the graph.",
            f"The DQ Monitor measured {mon.get('measured')} and graded {mon.get('monitor_grade')}; "
            f"against the rulebook that is {mon.get('rulebook_grade') or 'unknown'}. "
            f"{mon.get('discrepancy') or ''}"], must=True, priority=98))
    curated = curate(sections, TOKEN_BUDGETS["lane"])
    situation = (f"CAUSE CHECK: {item.get('lane')} — family {family}: {FAMILIES[family]}\n"
                 f"LOOK AT: {_LANE_FOCUS[family]}\nINCIDENT {mon.get('incident_id')}\n\n"
                 + curated.text + "\n\nEVIDENCE YOU MAY CITE: " + ", ".join(catalogue))
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, LANE):
        judged = _ask_role(agent, _role_prompt("lane_agent.md", situation))
    verdict, cause, evidence, rationale = "CannotVerify", "", [], ""
    if judged is None:
        rationale = "the Lane Agent answered nothing readable"
    else:
        said = str(judged.get("verdict") or "")
        evidence = _clean_ids(judged.get("evidence"), catalogue)
        cause = " ".join(str(judged.get("cause") or "").split())[:800]
        rationale = " ".join(str(judged.get("rationale") or "").split())[:1000]
        if said in ("Confirmed", "RuledOut") and evidence:
            verdict = said
        elif said in ("Confirmed", "RuledOut"):
            rationale = f"{said} without citing evidence it was given is not a verdict. {rationale}"
    statement = (f"{item.get('lane')} on {mon.get('incident_id')}: {verdict}"
                 + (f" — {cause}" if cause else "") + (f" (cites {', '.join(evidence)})"
                                                       if evidence else "")
                 + (f". {rationale}" if rationale else ""))
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type=verdict, statement=statement, actor_ref=LANE,
                       scope=str(item.get("lane") or family),
                       extra={"lane": item.get("lane"), "family": family, "evidence": evidence})
    cited = [m["node_id"] for m in history.get("matches") or [] if m["incident_id"] in evidence]
    return {"capability": "dq_lane", "tool": None, "status": "succeeded",
            "lane": item.get("lane"), "family": family, "lane_verdict": verdict, "cause": cause,
            "evidence": evidence, "rationale": rationale,
            "confidence": item.get("confidence"),
            "false_positive": family == "rule_config" and verdict == "Confirmed",
            "cited_fact_ids": cited, "curator": curated.report(),
            "subject_id": mon.get("incident_node_id"), "note": statement[:600],
            "summary": statement[:1200], "ran_as": _ran_as(LANE),
            "produced": _produced(verdict, decision, statement)}


def _lane_results(steps: Mapping[str, Any]) -> list[dict[str, Any]]:
    lanes = steps.get("lanes") or {}
    results = lanes.get("results") if isinstance(lanes, Mapping) else None
    out = []
    for r in results or []:
        r = r.get("output") if isinstance(r, Mapping) and isinstance(r.get("output"), Mapping) else r
        if isinstance(r, Mapping) and r.get("lane"):
            out.append(dict(r))
    return out


# ── Rule Proposal (side path: Rule authoring) ────────────────────────────────────────────────

def run_dq_rule_drafting(connection: Connection, settings: Any, project_id: str,
                         step: Mapping[str, Any], context: Mapping[str, Any],
                         inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "rule_drafting")
    steps = _steps(connection, run_id, context)
    mon = steps.get("monitoring") or {}
    confirmed = [r for r in _lane_results(steps) if r.get("false_positive")]
    if not confirmed:
        statement = (f"No swimlane confirmed a false positive on {mon.get('incident_id')}: nothing "
                     "to draft.")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="NothingToDraft", statement=statement,
                           actor_ref=RULE_DRAFTING)
        return {"capability": "dq_rule_drafting", "tool": None, "status": "succeeded",
                "drafted": False, "note": statement, "summary": statement,
                "ran_as": _ran_as(RULE_DRAFTING),
                "produced": _produced("NothingToDraft", decision, statement)}
    rule_id = str(mon.get("rule_id") or "")
    rule = _rule_facts(connection, project_id, rule_id)
    enrich, history = steps.get("context_enrichment") or {}, steps.get("historical_pattern") or {}
    catalogue = _evidence_catalogue(mon, enrich, history)
    lane = confirmed[0]
    curated = curate([
        Section("The confirmed false positive", [lane["note"]], must=True, priority=100),
        Section("The rule", [
            f"{rule_id} v{rule.get('version')}: {rule.get('title')} — {rule.get('check')}",
            f"Rulebook thresholds: Warning above {rule.get('warning_value')}, Error above "
            f"{rule.get('error_value')} ({rule.get('metric')})"] if rule else
            [f"{rule_id} is not in the graph yet."], must=True, priority=95),
        Section("What the DQ Monitor did", [f"The DQ Monitor measured {mon.get('measured')} against its configured "
                                   f"thresholds and graded {mon.get('monitor_grade')}. "
                                   f"{mon.get('discrepancy') or ''}"], priority=90),
        Section("Evidence", [f"[{k}] {v}" for k, v in catalogue.items()], priority=50),
    ], TOKEN_BUDGETS["rule_drafting"])
    situation = (f"INCIDENT {mon.get('incident_id')} on {rule_id}\n\n{curated.text}\n\n"
                 f"EVIDENCE YOU MAY CITE: {', '.join(catalogue)}")
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, RULE_DRAFTING):
        judged = _ask_role(agent, _role_prompt("rule_drafting_agent.md", situation))
    proposal = " ".join(str((judged or {}).get("proposal") or "").split())[:1800]
    evidence = _clean_ids((judged or {}).get("evidence"), catalogue)
    thresholds = (judged or {}).get("thresholds") if isinstance((judged or {}).get("thresholds"),
                                                                 Mapping) else {}
    if not proposal or not evidence:
        statement = (f"The Rule Proposal Agent drafted nothing it could stand behind for "
                     f"{rule_id} ({'no proposal' if not proposal else 'no evidence cited'}); a "
                     "person opens the rule authoring case.")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="NothingToDraft", statement=statement,
                           actor_ref=RULE_DRAFTING)
        return {"capability": "dq_rule_drafting", "tool": None, "status": "succeeded",
                "drafted": False, "note": statement, "summary": statement,
                "ran_as": _ran_as(RULE_DRAFTING),
                "produced": _produced("NothingToDraft", decision, statement)}
    title = f"{rule_id}: {str((judged or {}).get('title') or 'threshold correction')}"[:200]
    # INTO RULES REVIEW: a proposed rule, untrusted until a person approves it — the same queue
    # every proposed rule waits in.
    from .rules import _content_key, _node_key

    scope = {"repo": None, "areas": [], "packages": [], "symbols": []}
    content_key = _content_key(scope, proposal)
    proposed_rule = STORE.upsert_node(
        connection, project_id=project_id, kind="rule", node_key=_node_key(content_key, 1),
        name=title, body=proposal, trust_tier="inferred", actor_kind="agent",
        actor_ref=RULE_DRAFTING, source="rules",
        attrs={"statement": proposal, "rationale": str((judged or {}).get("rationale") or "")[:2000],
               "category": "limit", "scope": scope, "status": "proposed",
               "confidence": _confidence((judged or {}).get("confidence"), 0.6),
               "provenance": {"actor_kind": "agent", "agent_role": RULE_DRAFTING,
                              "agent_name": "Rule Proposal Agent", "run_id": run_id,
                              "workflow_run_id": run_id, "step": step_id},
               "review": None, "version": 1, "previous_id": None, "content_key": content_key,
               "rule_code": rule_id, "corrects_dq_rule": rule_id, "thresholds": dict(thresholds),
               "source": "dq_rule_drafting"})
    proposal_node = STORE.upsert_node(
        connection, project_id=project_id, kind="rule_proposal",
        node_key=f"context:rule_proposal:{run_id}:{rule_id}", name=title,
        body=proposal, trust_tier="inferred", actor_kind="agent", actor_ref=RULE_DRAFTING,
        source="dq_pipeline",
        attrs={"rule_id": rule_id, "proposal": proposal, "thresholds": dict(thresholds),
               "status": "in_rules_review", "rule_node_id": proposed_rule,
               "incident_id": mon.get("incident_id"), "evidence": evidence, "run_id": run_id})
    if rule.get("node_id"):
        _edge(connection, project_id, proposal_node, rule["node_id"], "proposes_change_to",
              RULE_DRAFTING)
        _edge(connection, project_id, proposed_rule, rule["node_id"], "evidenced_by",
              RULE_DRAFTING, {"why": "the false positive it corrects"})
    _edge(connection, project_id, proposal_node, proposed_rule, "references", RULE_DRAFTING)
    statement = (f"Drafted a rule proposal for {rule_id} from the false positive on "
                 f"{mon.get('incident_id')}: {proposal} It waits in Rules review for a person.")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="RuleProposed", statement=statement, actor_ref=RULE_DRAFTING,
                       extra={"rule_proposal_id": proposal_node, "rule_node_id": proposed_rule})
    return {"capability": "dq_rule_drafting", "tool": None, "status": "succeeded",
            "drafted": True, "rule_id": rule_id, "proposal": proposal,
            "thresholds": dict(thresholds), "rule_proposal_node_id": proposal_node,
            "proposed_rule_id": proposed_rule, "evidence": evidence,
            "subject_id": mon.get("incident_node_id"), "curator": curated.report(),
            "note": statement[:600], "summary": statement[:1200],
            "ran_as": _ran_as(RULE_DRAFTING),
            "produced": _produced("RuleProposed", decision, statement,
                                  rule_proposal={"node_id": proposal_node})}


# ── 7. Fix Planner (role agent, LLM): the package, prepared only ─────────────────────

def run_dq_remediation_package(connection: Connection, settings: Any, project_id: str,
                               step: Mapping[str, Any], context: Mapping[str, Any],
                               inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "remediation_advisor")
    steps = _steps(connection, run_id, context)
    mon = steps.get("monitoring") or {}
    enrich, history = steps.get("context_enrichment") or {}, steps.get("historical_pattern") or {}
    confirmed = sorted([r for r in _lane_results(steps) if r.get("lane_verdict") == "Confirmed"],
                       key=lambda r: -float(r.get("confidence") or 0))
    base = {"capability": "dq_remediation_package", "tool": None, "status": "succeeded",
            "incident_id": mon.get("incident_id"), "subject_id": mon.get("incident_node_id"),
            "ran_as": _ran_as(REMEDIATION)}
    if not confirmed:
        statement = (f"No confirmed cause for {mon.get('incident_id')}, so no remediation is "
                     "prepared; the incident stays open.")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="NoPackage", statement=statement, actor_ref=REMEDIATION)
        return {**base, "package_ready": False, "note": statement, "summary": statement,
                "produced": _produced("NoPackage", decision, statement)}
    lane = confirmed[0]
    pack = enrich.get("context_pack") or {}
    catalogue = _evidence_catalogue(mon, enrich, history)
    teams = connection.execute(text(
        "SELECT name, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'owner' "
        "AND valid_to IS NULL AND attrs->>'owner_type' = 'team' ORDER BY node_key"),
        {"p": project_id}).mappings().all()
    groups = {str(_attrs(t).get("service_desk_group")): t["name"] for t in teams if _attrs(t).get("service_desk_group")}
    people = {str(_attrs(r).get("owner_id")): f"{r['name']} ({_attrs(r).get('title')})"
              for r in connection.execute(text(
                  "SELECT name, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'owner' "
                  "AND valid_to IS NULL AND attrs->>'owner_type' = 'person' ORDER BY node_key"),
                  {"p": project_id}).mappings().all()}
    pattern_fixes = [f"{g['pattern_id']}: durable fix — {g.get('durable_fix')}; earlier fixes "
                     f"{', '.join(str(x) for x in g.get('fix_durability') or [])}"
                     for g in history.get("patterns") or [] if g.get("family") == lane["family"]]
    curated = curate([
        Section("Confirmed cause", [lane["note"]], must=True, priority=100),
        Section("Remediation templates (resolved patterns of this family)", pattern_fixes,
                priority=90),
        Section("Precedent fixes and how they held",
                [f"[{m['incident_id']}] {catalogue.get(str(m['incident_id']))}"
                 for m in history.get("matches") or [] if m["family"] == lane["family"]],
                priority=85),
        Section("Owners", [f"{o['role']}: {o['name']} — {o.get('owner_id')} "
                           f"({o.get('assignment_group') or '-'})" for o in pack.get("owners") or []],
                must=True, priority=80),
        Section("Recent changes", [f"[{k}] {v}" for k, v in catalogue.items()
                                   if k.startswith("CHG-")], priority=70),
        Section("Regulatory reach", [f"{r['line_id']}: {r['caption']}"
                                     for r in pack.get("regulatory_reach") or []], priority=40),
    ], TOKEN_BUDGETS["remediation_advisor"])
    situation = (f"INCIDENT {mon.get('incident_id')} — {mon.get('group_label')} on "
                 f"{mon.get('platform')}\n\n{curated.text}\n\nASSIGNMENT GROUPS (executor): "
                 + "; ".join(f"{g} = {n}" for g, n in groups.items())
                 + "\nPEOPLE (approver): " + "; ".join(f"{k} = {v}" for k, v in people.items())
                 + f"\n\nEVIDENCE YOU MAY CITE: {', '.join(catalogue)}")
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, REMEDIATION):
        judged = _ask_role(agent, _role_prompt("remediation_advisor.md", situation))
    judged = judged or {}
    executor = str((judged.get("executor") or {}).get("assignment_group") or "") \
        if isinstance(judged.get("executor"), Mapping) else str(judged.get("executor") or "")
    approver = str((judged.get("approver") or {}).get("owner_id") or "") \
        if isinstance(judged.get("approver"), Mapping) else str(judged.get("approver") or "")
    fix = " ".join(str(judged.get("fix") or "").split())[:1200]
    content = [" ".join(str(x).split())[:500] for x in judged.get("change_content") or []
               if str(x).strip()][:12]
    evidence = _clean_ids(judged.get("evidence"), catalogue)
    problems = []
    if not fix or not content:
        problems.append("no fix or change content")
    if executor not in groups:
        problems.append(f"an executor that is not an assignment group ({executor or 'none'})")
    if approver not in people:
        problems.append(f"an approver who is not one of the bank's people ({approver or 'none'})")
    if not evidence:
        problems.append("no evidence cited")
    if problems:
        statement = (f"The Fix Planner's package for {mon.get('incident_id')} was not "
                     f"usable ({'; '.join(problems)}); nothing is prepared and a person "
                     "writes the fix.")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="NoPackage", statement=statement, actor_ref=REMEDIATION)
        return {**base, "package_ready": False, "problems": problems, "note": statement,
                "summary": statement, "produced": _produced("NoPackage", decision, statement)}
    target = judged.get("target") if isinstance(judged.get("target"), Mapping) else \
        {"system": str(judged.get("target") or mon.get("platform"))}
    package = {"incident_id": mon.get("incident_id"), "family": lane["family"],
               "cause": lane.get("cause"), "fix": fix, "target": dict(target),
               "executor": {"assignment_group": executor, "team": groups[executor]},
               "approver": {"owner_id": approver, "name": people[approver]},
               "change_content": content,
               "validation": " ".join(str(judged.get("validation") or "").split())[:600],
               "rollback": " ".join(str(judged.get("rollback") or "").split())[:600],
               "evidence": evidence, "status": "prepared", "prepare_only": True}
    package_id = STORE.upsert_node(
        connection, project_id=project_id, kind="remediation_package",
        node_key=f"context:remediation_package:{run_id}:{mon.get('incident_id')}",
        name=f"Remediation for {mon.get('incident_id')}: {fix[:120]}", body=fix,
        trust_tier="inferred", actor_kind="agent", actor_ref=REMEDIATION, source="dq_pipeline",
        attrs={**package, "run_id": run_id})
    if mon.get("incident_node_id"):
        _edge(connection, project_id, package_id, mon["incident_node_id"], "packaged_for",
              REMEDIATION)
    statement = (f"Prepared the remediation for {mon.get('incident_id')} ({lane['family']}): "
                 f"{fix} Executor {groups[executor]} ({executor}); approver {people[approver]}. "
                 f"{len(content)} change step(s). Prepared only: a person approves and a "
                 "different person carries it out.")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="RemediationPrepared", statement=statement,
                       actor_ref=REMEDIATION, extra={"remediation_package_id": package_id})
    return {**base, "package_ready": True, "package": package,
            "remediation_package_id": package_id, "curator": curated.report(),
            "note": statement[:600], "summary": statement[:1200],
            "produced": _produced("RemediationPrepared", decision, statement,
                                  remediation_package={"node_id": package_id})}


# ── 8. Fix (design target: records what the person did) ──────────────────────────

def _gate(connection: Connection, run_id: str, step_id: str) -> Optional[dict[str, Any]]:
    if not run_id:
        return None
    row = connection.execute(text(
        "SELECT d.id AS decision_id, d.status, d.approved_by, d.resolution_note "
        "FROM workflow_step_runs s LEFT JOIN decisions d ON d.id = s.decision_id "
        "WHERE s.workflow_run_id = :r AND s.step_id = :s ORDER BY s.iteration DESC, "
        "s.created_at DESC LIMIT 1"), {"r": run_id, "s": step_id}).mappings().first()
    return dict(row) if row else None


def run_dq_remediation_fix(connection: Connection, settings: Any, project_id: str,
                           step: Mapping[str, Any], context: Mapping[str, Any],
                           inputs: Mapping[str, Any]) -> dict[str, Any]:
    from ..workflows import _approver_name

    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "remediation_fix")
    steps = _steps(connection, run_id, context)
    mon, advisor = steps.get("monitoring") or {}, steps.get("remediation_advisor") or {}
    approval, execution = _gate(connection, run_id, APPROVAL_GATE), _gate(connection, run_id,
                                                                          EXECUTION_GATE)
    if not approval or approval.get("status") != "approved":
        raise PersonMissing("no person has approved the remediation yet (Human approval). "
                            "Under the prepare-only cap no agent executes a fix.")
    if not execution or execution.get("status") != "approved":
        raise PersonMissing("no person has carried out the approved fix yet (Human execution). "
                            "Under the prepare-only cap no agent executes a fix.")
    approved_by = _approver_name(connection, approval.get("approved_by"))
    executed_by = _approver_name(connection, execution.get("approved_by"))
    note = " ".join(str(execution.get("resolution_note") or "").split())
    change_ref = next((w.strip(".,;:") for w in note.split() if w.upper().startswith("CHG-")),
                      "") or (note[:60] if note else "")
    package_id = str(advisor.get("remediation_package_id") or "")
    if package_id:
        _patch(connection, project_id, package_id, {
            "status": "executed", "approved_by": approved_by, "executed_by": executed_by,
            "change_ref": change_ref, "execution_note": note[:600]})
    calls: list[dict[str, Any]] = []
    if mon.get("incident_id"):
        _call(connection, project_id, "service-desk", "update_incident",
              {"incident_id": mon["incident_id"], "status": "in_progress",
               "note": f"Fix approved by {approved_by}, carried out by {executed_by}"
                       + (f" under {change_ref}" if change_ref else "") + f". {note}"[:1800],
               "by": "Fix"}, calls)
    statement = (f"The approved fix for {mon.get('incident_id')} was carried out by "
                 f"{executed_by}" + (f" under {change_ref}" if change_ref else "")
                 + f", after {approved_by} approved it. No agent executed anything "
                   "(prepare-only cap).")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="FixRecorded", statement=statement,
                       actor_ref="dq_incident_pipeline:remediation_fix",
                       extra={"approved_by": approved_by, "executed_by": executed_by,
                              "change_ref": change_ref})
    return {"capability": "dq_remediation_fix", "tool": None, "status": "succeeded",
            "incident_id": mon.get("incident_id"), "approved_by": approved_by,
            "executed_by": executed_by, "change_ref": change_ref, "calls": calls,
            "subject_id": mon.get("incident_node_id"), "note": statement, "summary": statement,
            "produced": _produced("FixRecorded", decision, statement)}


# ── 10. Close and Learn ─────────────────────────────────────────────────────────────────────

def run_dq_incident_closure(connection: Connection, settings: Any, project_id: str,
                            step: Mapping[str, Any], context: Mapping[str, Any],
                            inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "incident_closure")
    steps = _steps(connection, run_id, context)
    mon = steps.get("monitoring") or {}
    fix = steps.get("remediation_fix") or {}
    advisor = steps.get("remediation_advisor") or {}
    history = steps.get("historical_pattern") or {}
    rca = steps.get("rca") or {}
    incident_id = str(mon.get("incident_id") or "")
    incident_node = str(mon.get("incident_node_id") or "")
    rule_id = str(mon.get("rule_id") or "")
    calls: list[dict[str, Any]] = []
    actor = CLOSURE
    base = {"capability": "dq_incident_closure", "tool": None, "status": "succeeded",
            "incident_id": incident_id, "subject_id": incident_node, "calls": calls,
            "ran_as": _ran_as(CLOSURE)}
    if not fix.get("executed_by") or not advisor.get("package_ready"):
        why = ("RCA found insufficient evidence" if rca.get("rca_verdict") == INSUFFICIENT
               else "no cause was confirmed" if not advisor.get("package_ready")
               else "no fix was carried out")
        statement = (f"{incident_id} stays open: {why}. The Escalation Watch ladders it daily "
                     "until a person resolves it.")
        _call(connection, project_id, "service-desk", "update_incident",
              {"incident_id": incident_id, "note": statement, "by": "Close and Learn Agent"},
              calls)
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="LeftOpen", statement=statement, actor_ref=actor)
        return {**base, "closed": False, "note": statement, "summary": statement,
                "produced": _produced("LeftOpen", decision, statement)}
    # 1 Re-run the check
    rerun, gap = _call(connection, project_id, "dq-monitor", "rerun_check",
                       {"rule_id": rule_id, "cob": mon.get("cob"),
                        "change_ref": fix.get("change_ref") or "recorded fix"}, calls)
    grade = (rerun or {}).get("grade")
    if rerun is None or grade == "Error":
        statement = (f"{incident_id} is not closed: "
                     + (f"The DQ Monitor's re-run of {rule_id} still grades Error ({rerun.get('measured')})"
                        if rerun else f"the check could not be re-run ({gap})")
                     + ". The fix is not confirmed.")
        decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                           decision_type="FixNotConfirmed", statement=statement, actor_ref=actor)
        return {**base, "closed": False, "rerun": rerun, "note": statement, "summary": statement,
                "produced": _produced("FixNotConfirmed", decision, statement)}
    # 2 Finalise the confirmed cause
    package = advisor.get("package") or {}
    family = str(package.get("family") or "")
    cause = str(package.get("cause") or "")
    used = [p for c in rca.get("candidates") or [] if c.get("family") == family
            for p in c.get("precedents") or []]
    matched = {str(m["incident_id"]): m for m in history.get("matches") or []}
    pattern_id = next((matched[p]["pattern_id"] for p in used if p in matched
                       and matched[p].get("pattern_id")), None)
    pattern_node = dq_platform.node_id_of(connection, project_id, "resolved_pattern",
                                          str(pattern_id)) if pattern_id else ""
    # 3 Maintain resolved patterns: an existing pattern is seen once more; a new cause starts one.
    if pattern_node:
        pa = _attrs(_node(connection, project_id, pattern_node))
        seen = list(dict.fromkeys([*(pa.get("incidents") or []), incident_id]))
        _patch(connection, project_id, pattern_node, {
            "recurrences": int(pa.get("recurrences") or len(pa.get("rule_ids") or []) or 0) + 1,
            "incidents": seen, "last_seen_incident": incident_id})
    else:
        pattern_id = f"RPAT-{family.upper().replace('_', '-')}-{rule_id.rsplit('-', 2)[-2]}-" \
                     f"{rule_id.rsplit('-', 1)[-1]}"
        pattern_node = STORE.upsert_node(
            connection, project_id=project_id, kind="resolved_pattern",
            node_key=dq_platform.node_key("resolved_pattern", pattern_id),
            name=f"{family.replace('_', ' ')} on {rule_id}: {cause[:120]}", body=cause,
            trust_tier="inferred", actor_kind="agent", actor_ref=actor, source="dq_pipeline",
            attrs={"pattern_id": pattern_id, "key": pattern_id, "family": family,
                   "signature": cause, "durable_fix": package.get("fix"), "rule_ids": [rule_id],
                   "recurrences": 1, "incidents": [incident_id]})
    rule_node = (dq_platform.resolve_rule(connection, project_id, rule_id) or {}).get("id")
    if rule_node:
        _edge(connection, project_id, pattern_node, rule_node, "seen_on", actor)
    _edge(connection, project_id, incident_node, pattern_node, "caused_by", actor,
          {"run_id": run_id})
    # 4 Close the incident (graph and ticket)
    closed_on = str(mon.get("cob") or "")
    _patch(connection, project_id, incident_node, {
        "status": "closed", "closed_run": run_id, "root_cause_family": family,
        "pattern_id": pattern_id, "confirmed_cause": cause, "fix": package.get("fix"),
        "fix_type": "change", "change_ref": fix.get("change_ref"),
        "approved_by": fix.get("approved_by"), "executed_by": fix.get("executed_by"),
        "fix_durability": "pending", "closed": closed_on})
    _call(connection, project_id, "service-desk", "update_incident",
          {"incident_id": incident_id, "status": "resolved", "confirmed_cause": cause,
           "root_cause_family": family, "fix": package.get("fix"),
           "note": f"Re-run graded {grade}; closed with cause {family}.",
           "by": "Close and Learn Agent"}, calls)
    # 5 Write the Precedent the next investigation's Past Incidents finds
    precedent_key = f"PRE-{incident_id[4:] if incident_id.startswith('INC-') else incident_id}"
    recurrence = len([m for m in matched.values() if m.get("pattern_id") == pattern_id]) + 1
    precedent_id = STORE.upsert_node(
        connection, project_id=project_id, kind="precedent",
        node_key=dq_platform.node_key("precedent", precedent_key),
        name=f"{incident_id}: {mon.get('group_label')} — {family}"[:300],
        body=(f"Cause: {cause} Fix: {package.get('fix')} (approved by {fix.get('approved_by')}, "
              f"carried out by {fix.get('executed_by')} under {fix.get('change_ref')}). Re-run "
              f"graded {grade}. Occurrence {recurrence} of {pattern_id}."),
        trust_tier="observed", actor_kind="system", actor_ref=actor, source="dq_pipeline",
        attrs={"precedent_id": precedent_key, "incident_id": incident_id, "key": precedent_key,
               "pattern_id": pattern_id, "family": family, "rule_id": rule_id,
               "recurrence": recurrence, "fix_durability": "pending", "domain": "data_quality",
               "used_precedents": used, "run_id": run_id})
    _edge(connection, project_id, precedent_id, incident_node, "from_incident", actor,
          actor_kind="system")
    _edge(connection, project_id, precedent_id, pattern_node, "matches", actor,
          actor_kind="system")
    statement = (f"Closed {incident_id}: the DQ Monitor's re-run of {rule_id} grades {grade}; confirmed "
                 f"cause {family} ({pattern_id}); fix by {fix.get('executed_by')} under "
                 f"{fix.get('change_ref')}. Wrote precedent {precedent_key} (occurrence "
                 f"{recurrence} of the pattern) for the next investigation.")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="IncidentClosed", statement=statement, actor_ref=actor,
                       extra={"pattern_id": pattern_id, "precedent_id": precedent_key})
    return {**base, "closed": True, "rerun": rerun, "family": family, "pattern_id": pattern_id,
            "precedent_id": precedent_key, "precedent_node_id": precedent_id,
            "note": statement, "summary": statement,
            "produced": _produced("IncidentClosed", decision, statement,
                                  precedent={"node_id": precedent_id})}


# ── 9. Escalation (its own daily workflow) ───────────────────────────────────────────────────

#: The escalation ladder (OP-ESC-01): business days an incident has been open → who is told.
LADDER = ((1, "the owner's assignment group"), (3, "the data steward of the CDE"),
          (5, "the Chief Data Officer"))


def _business_days(connection: Connection, project_id: str, start: str, end: str) -> int:
    if not start or not end or end <= start:
        return 0
    days = connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'calendar_day' "
        "AND valid_to IS NULL AND attrs->>'day' > :a AND attrs->>'day' <= :b "
        "AND (attrs->>'business_day')::boolean"), {"p": project_id, "a": start, "b": end}).scalar()
    if days:
        return int(days)
    d, n = date.fromisoformat(start), 0
    while d < date.fromisoformat(end):
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def run_dq_escalation(connection: Connection, settings: Any, project_id: str,
                      step: Mapping[str, Any], context: Mapping[str, Any],
                      inputs: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "escalation")
    as_of = str(inputs.get("as_of") or ((context or {}).get("input") or {}).get("as_of")
                or date.today().isoformat())
    calls: list[dict[str, Any]] = []
    rows = connection.execute(text(
        "SELECT id, name, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
        "AND valid_to IS NULL AND attrs->>'domain' = 'data_quality' "
        "AND COALESCE(attrs->>'status', 'open') IN ('open', 'investigating', 'in_progress') "
        "ORDER BY node_key"), {"p": project_id}).mappings().all()
    if not rows:
        rows = connection.execute(text(
            "SELECT id, name, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'incident' "
            "AND valid_to IS NULL AND node_key LIKE 'dq:incident:%' "
            "AND COALESCE(attrs->>'status', 'open') IN ('open', 'investigating', 'in_progress') "
            "ORDER BY node_key"), {"p": project_id}).mappings().all()
    escalated, steady, decisions = [], [], []
    for row in rows:
        attrs = _attrs(row)
        incident_id = str(attrs.get("incident_id") or attrs.get("key") or row["name"])
        opened = str(attrs.get("opened") or "")
        age = _business_days(connection, project_id, opened, as_of)
        rung = max([n for n, (days, _who) in enumerate(LADDER, start=1) if age >= days],
                   default=0)
        reach = bool(connection.execute(text(
            "SELECT 1 FROM kg_edges e JOIN kg_nodes c ON c.id = e.dst_id JOIN kg_edges r "
            "ON r.src_id = c.id AND r.kind = 'reported_in' WHERE e.src_id = :i "
            "AND e.kind = 'concerns' AND c.kind = 'cde' LIMIT 1"), {"i": row["id"]}).scalar())
        # Regulatory reach climbs one rung sooner (a report line depends on the data).
        if reach and age >= 2 and rung < len(LADDER):
            rung = max(rung, 2 if age < 5 else 3)
        current = int(attrs.get("escalation_level") or 0)
        if rung <= current:
            steady.append({"incident_id": incident_id, "level": current, "age": age})
            continue
        who = LADDER[rung - 1][1]
        if rung >= 2:
            target = connection.execute(text(
                "SELECT o.id FROM kg_edges e JOIN kg_nodes c ON c.id = e.dst_id "
                "JOIN kg_edges s ON s.dst_id = c.id AND s.kind = 'stewards' "
                "JOIN kg_nodes o ON o.id = s.src_id WHERE e.src_id = :i AND e.kind = 'concerns' "
                "AND c.kind = 'cde' LIMIT 1"), {"i": row["id"]}).scalar() if rung == 2 else \
                dq_platform.node_id_of(connection, project_id, "owner", "P-DWHITFIELD")
        else:
            target = connection.execute(text(
                "SELECT dst_id FROM kg_edges WHERE src_id = :i AND kind = 'assigned_to' LIMIT 1"),
                {"i": row["id"]}).scalar()
        if target:
            _edge(connection, project_id, row["id"], str(target), "escalated_to", ESCALATION,
                  {"level": rung, "as_of": as_of})
        _patch(connection, project_id, row["id"], {"escalation_level": rung,
                                                   "escalated_on": as_of})
        _call(connection, project_id, "service-desk", "update_incident",
              {"incident_id": incident_id, "escalation_level": rung,
               "note": f"Escalated to level {rung} ({who}): open {age} business day(s) as of "
                       f"{as_of}" + (", with regulatory reach" if reach else "") + ".",
               "by": "Escalation Watch"}, calls)
        statement = (f"Escalated {incident_id} to level {rung} ({who}): open {age} business "
                     f"day(s) as of {as_of}" + (", regulatory reach" if reach else "") + ".")
        decisions.append(_decide(connection, project_id, run_id=run_id, step_id=step_id,
                                 decision_type="Escalated", statement=statement, scope=incident_id,
                                 actor_ref=ESCALATION, extra={"incident_id": incident_id,
                                                              "level": rung, "age": age}))
        escalated.append({"incident_id": incident_id, "level": rung, "to": who, "age": age,
                          "regulatory_reach": reach})
    statement = (f"Escalation sweep as of {as_of}: {len(rows)} open incident(s); escalated "
                 f"{len(escalated)}" + (f" ({', '.join(e['incident_id'] + ' L' + str(e['level']) for e in escalated)})"
                                        if escalated else "")
                 + f"; {len(steady)} already at their rung.")
    sweep = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                    decision_type="EscalationSweep", statement=statement, actor_ref=ESCALATION)
    return {"capability": "dq_escalation", "tool": None, "status": "succeeded", "as_of": as_of,
            "open_incidents": len(rows), "escalated": escalated, "steady": steady,
            "calls": calls, "note": statement, "summary": statement,
            "ran_as": _ran_as(ESCALATION),
            "produced": {"agent_decision": {"decision_type": "EscalationSweep",
                                            "statement": statement,
                                            "node_ids": [sweep, *decisions]}}}


# ── The scheduled trigger: the DQ Monitor's Error groups → one RCA run each ───────────────────────────

def run_dq_monitor_intake(connection: Connection, settings: Any, project_id: str,
                        step: Mapping[str, Any], context: Mapping[str, Any],
                        inputs: Mapping[str, Any]) -> dict[str, Any]:
    import uuid

    from .. import workflows

    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "monitor_intake")
    calls: list[dict[str, Any]] = []
    cob = str(inputs.get("cob") or ((context or {}).get("input") or {}).get("cob") or "").strip()
    listed, gap = _call(connection, project_id, "dq-monitor", "list_exception_groups",
                        {"cob": cob} if cob else {}, calls)
    if listed is None:
        raise RuntimeError(f"The DQ Monitor could not be read: {gap}")
    if not cob:
        cob = (listed.get("cobs") or [""])[-1]
        listed, gap = _call(connection, project_id, "dq-monitor", "list_exception_groups",
                            {"cob": cob}, calls)
    errors = [g for g in (listed or {}).get("groups") or [] if g.get("grade") == "Error"]
    version = connection.execute(text(
        "SELECT v.id FROM workflow_versions v JOIN workflow_definitions d "
        "ON d.id = v.workflow_definition_id WHERE d.project_id = :p AND d.workflow_key = :k "
        "ORDER BY v.version DESC LIMIT 1"), {"p": project_id, "k": dq_platform.RCA_WORKFLOW}
    ).scalar()
    # The investigations start on the authority of whoever started (or scheduled) this intake.
    actor = str(connection.execute(text("SELECT actor FROM workflow_runs WHERE id = :r"),
                                   {"r": run_id}).scalar() or "boss")
    started, skipped = [], []
    for g in errors:
        existing = connection.execute(text(
            "SELECT r.id FROM workflow_runs r JOIN workflow_versions v ON v.id = "
            "r.workflow_version_id JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
            "WHERE r.project_id = :p AND d.workflow_key = :k AND r.input->>'exception_group_id' = :g "
            "LIMIT 1"), {"p": project_id, "k": dq_platform.RCA_WORKFLOW, "g": g["group_id"]}
        ).scalar()
        if existing or not version:
            skipped.append({"group_id": g["group_id"], "run_id": existing,
                            "why": "already investigated" if existing else
                            "the RCA workflow is not installed"})
            continue
        run = workflows.start_run(connection, project_id=project_id, actor=actor,
                                  input_data={"exception_group_id": g["group_id"]},
                                  dedupe_key=f"dq_intake_{g['group_id']}_{uuid.uuid4().hex[:6]}",
                                  workflow_version_id=version)
        started.append({"group_id": g["group_id"], "run_id": run["id"]})
    statement = (f"DQ Monitor, COB {cob}: {len(errors)} exception group(s) graded Error; started "
                 f"{len(started)} investigation(s)"
                 + (f", {len(skipped)} already under investigation" if skipped else "") + ".")
    decision = _decide(connection, project_id, run_id=run_id, step_id=step_id,
                       decision_type="IntakeRouted", statement=statement, actor_ref=ORCHESTRATOR)
    return {"capability": "dq_monitor_intake", "tool": None, "status": "succeeded", "cob": cob,
            "started": started, "skipped": skipped, "calls": calls, "note": statement,
            "summary": statement, "ran_as": _ran_as(ORCHESTRATOR),
            "produced": _produced("IntakeRouted", decision, statement)}


# ── Shared services as stages (drawn in the pipeline; runnable on their own) ─────────────────

def run_context_curator(connection: Connection, *, project_id: str, run_id: str, step_id: str,
                        **_ignored: Any) -> dict[str, Any]:
    """What the Context Budgeter kept and dropped for each LLM step of this run."""
    steps = _outputs(connection, run_id)
    report = {sid: out.get("curator") for sid, out in steps.items() if out.get("curator")}
    note = "; ".join(f"{sid}: used {r['used_tokens']}/{r['budget_tokens']} tokens, dropped "
                     f"{sum(d['items'] for d in r['dropped'])} item(s)"
                     for sid, r in report.items()) or "no LLM step has run yet"
    return {"cost_usd_micros": 0, "status": "succeeded", "report": report,
            "note": f"Context Budgeter: {note}.", "produced": {}}


def run_profile_service(connection: Connection, *, project_id: str, run_id: str, step_id: str,
                        platform: str = "", rule_id: str = "", measured: Any = None,
                        **_ignored: Any) -> dict[str, Any]:
    """The exception profile of one rule on its platform (the Profile Service), deterministic."""
    calls: list[dict[str, Any]] = []
    profile, gap = exception_profile(connection, project_id, platform=str(platform),
                                     rule_id=str(rule_id),
                                     measured=float(measured) if measured not in (None, "") else None,
                                     calls=calls)
    return {"cost_usd_micros": 0, "status": "succeeded", "profile": profile, "gap": gap or None,
            "calls": calls, "note": describe_profile(profile) if profile else gap,
            "produced": {}}
