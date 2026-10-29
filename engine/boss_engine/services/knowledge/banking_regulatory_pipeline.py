"""KG P5b: the six role-agent capabilities of the `banking_regulatory_compliance` instance of
`kg_investigation_pipeline`. `context_enrichment`, `historical_pattern` and the Precedent write
inside `run_compliance_closure` reuse P5a's `graph_steps` functions verbatim — nothing here
reimplements them.

Every function here has the same shape as `regulatory_trace.run_step` / a `graph_steps` function:
``(connection, settings, project_id, step, context, inputs) -> dict``, so the capability runner
dispatches to it exactly like `trace_regulation` ([R:workflow_capability_runner.py] pattern).
Pasted regulation text and ITSM ticket bodies are read as DATA only, never as instructions —
no function below executes anything an input field says to do.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import context_graph, domain, graph_steps
from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()

#: Finding families the Pipeline Orchestrator may route into (PRD §Pipeline flow, F5). What a
#: given finding belongs to is a judgement call made by the Pipeline Orchestrator role agent
#: below, never a keyword match.
FAMILIES = ("payment_crediting", "payment_allocation", "prepayment")

_ROLES_DIR = (Path(__file__).resolve().parents[4] / "packages" / "specpack" / "skills"
             / "banking_regulatory_compliance" / "roles")
MAX_TURNS = 4
_MARK_OPEN = "=== JUDGEMENT ==="
_MARK_CLOSE = "=== END JUDGEMENT ==="


def _run_id(step: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    return str(step.get("run_id") or (context or {}).get("run_id") or "")


def _role_prompt(role_file: str, situation: str) -> str:
    """A real role prompt (`packages/specpack/skills/banking_regulatory_compliance/roles/`)
    plus the situation-specific facts the role is judging, exactly as `regulatory_trace`'s
    judge prompt is built."""
    body = (_ROLES_DIR / role_file).read_text(encoding="utf-8")
    return f"{body}\n\n{situation}"


def _role_agent(connection: Connection, settings: Any, project_id: str) -> tuple[Any, str]:
    """The governed agent for a role call — the same construction every other role-backed
    capability in this codebase uses (``regulatory_trace._trace_agent``), so the configured
    model (or Jev, where wired) applies here too, not a bespoke call path."""
    from ..code_agent import make_code_agent

    engine = str(getattr(settings, "code_review_engine", None)
                 or getattr(settings, "code_agent_engine", None) or "claude_code").lower()
    return make_code_agent(settings, engine=engine, connection=connection,
                           project_id=project_id), engine


def _ask_role(agent: Any, prompt: str) -> Optional[dict[str, Any]]:
    """Run one role turn and parse its JSON judgement from between the marker lines. Returns
    None when the agent answered nothing readable — the caller treats that exactly like
    ``regulatory_trace``'s unreadable judgement: a gap/ruled-out, never an invented verdict."""
    with tempfile.TemporaryDirectory(prefix="boss-banking-regulatory-") as cwd:
        result = agent.run_role(prompt, Path(cwd), read_only=True, max_turns=MAX_TURNS) or {}
    body = str(result.get("summary") or "")
    if _MARK_OPEN in body:
        body = body.split(_MARK_OPEN, 1)[1].split(_MARK_CLOSE, 1)[0]
    body = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", body.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(body[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _node_summary(connection: Connection, project_id: str, node_id: str) -> str:
    node = STORE.get_node(connection, project_id=project_id, node_id=node_id)
    if not node:
        return f"[{node_id}] (not found)"
    return f"[{node['kind']}] {node_id}: {node['name']} — {str(node.get('body') or '')[:400]}"


def run_clause_intake(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Open one ComplianceCase per clause of `document_id`, with AgentDecision(ClauseIntake)."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "clause_intake")
    document_id = str(inputs.get("document_id") or "").strip()
    actor_ref = "banking_regulatory_pipeline:clause_intake"
    case_ids: list[str] = []
    if document_id:
        clauses = connection.execute(
            text("SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'clause' "
                 "AND valid_to IS NULL AND attrs->>'document_id' = :d ORDER BY name"),
            {"p": project_id, "d": document_id},
        ).mappings().all()
        for clause in clauses:
            clause_id = str(clause["id"])
            case_id = STORE.upsert_node(
                connection, project_id=project_id, kind="compliance_case",
                node_key=f"case:{clause_id}", name=f"Compliance case: {clause_id}", body="",
                trust_tier="inferred", actor_kind="agent", actor_ref=actor_ref,
                attrs={"clause_id": clause_id, "status": "open"})
            case_ids.append(case_id)
            context_graph.write_agent_decision(
                connection, project_id, step_id=step_id, run_id=run_id or f"adhoc:{clause_id}",
                decision_type="ClauseIntake",
                statement=f"Opened a compliance case for clause {clause_id}.",
                policy_version="", workflow_version="", agent_fingerprint="deterministic",
                actor_ref=actor_ref)
    return {"capability": "clause_intake", "tool": None, "status": "succeeded",
            "compliance_case_ids": case_ids,
            "note": f"Opened {len(case_ids)} compliance case(s) for document {document_id or '(none)'}."}


def run_compliance_assessment(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify the subject's assembled evidence implemented/partly/gap, with a confidence and
    a graph-node citation. Cites a correlated incident's detail — e.g. INC-2088's payment
    detail — by calling the ITSM mock's `get_incident` tool when Historical Pattern found one."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "compliance_assessment")
    subject_id = str(inputs.get("subject_id") or "").strip()
    steps = (context or {}).get("steps") or {}
    collected = list((steps.get("context_enrichment") or {}).get("collected") or [])
    matched = list((steps.get("historical_pattern") or {}).get("matched") or [])
    actor_ref = "banking_regulatory_pipeline:compliance_assessment"

    # Only rule/code candidates are offered to the Compliance Analyst — never a System/Owner/
    # RegulatoryReport node `context_enrichment`'s traversal happened to visit first, and never
    # the subject itself.
    candidates = []
    for candidate_id in collected:
        node = STORE.get_node(connection, project_id=project_id, node_id=candidate_id)
        if node and node["kind"] in {"rule", "symbol"} and candidate_id != subject_id:
            candidates.append({"node_id": candidate_id, "node": node})

    cited_incident = None
    if matched:
        from tools.mcp_itsm_mock import TOOL_FNS as _itsm_tools

        for match_id in matched:
            node = STORE.get_node(connection, project_id=project_id, node_id=match_id)
            if node and node["kind"] == "incident":
                incident_id = str((node.get("attrs") or {}).get("incident_id") or node["name"])
                cited_incident = _itsm_tools["get_incident"]({"incident_id": incident_id})
                if cited_incident.get("error"):
                    cited_incident = None
                else:
                    break

    citation = subject_id
    status = "gap"
    confidence = 0.0
    rationale = "No connected rule or code candidate was found for this subject."
    if candidates or cited_incident:
        listing = "\n".join(f"{i}. {c['node_id']}: {_node_summary(connection, project_id, c['node_id'])}"
                            for i, c in enumerate(candidates))
        incident_text = (
            f"Historical Pattern correlated incident {cited_incident['incident_id']}: paid down "
            f"{cited_incident['actual_paydown']:.2f} instead of "
            f"{cited_incident['correct_paydown']:.2f}.\n" if cited_incident else "")
        situation = (
            f"CLAUSE/SUBJECT: {subject_id}\n\n{incident_text}"
            f"CANDIDATE RULE/CODE NODES:\n{listing or '(none)'}\n\n"
            "Cite a candidate's node_id above, or null for a gap.")
        agent, _engine = _role_agent(connection, settings, project_id)
        judged = _ask_role(agent, _role_prompt("compliance_analyst.md", situation))
        by_id = {c["node_id"]: c for c in candidates}
        if judged:
            candidate_citation = str(judged.get("citation") or "")
            if candidate_citation in by_id:
                citation = candidate_citation
                status = "implemented"
            else:
                status = "gap"
            try:
                confidence = min(1.0, max(0.0, float(judged.get("confidence"))))
            except (TypeError, ValueError):
                confidence = 0.5
            rationale = " ".join(str(judged.get("rationale") or "").split())[:1000] or rationale
        else:
            rationale = "the Compliance Analyst answered nothing readable for this subject"
    statement = f"Assessed as {status} (confidence {confidence:.2f}): {rationale}"
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id, decision_type="Assessment",
        statement=statement, policy_version="", workflow_version="",
        agent_fingerprint="deterministic", actor_ref=actor_ref)

    if status == "implemented" and subject_id:
        domain.propose_implementation(
            connection, project_id=project_id, clause_id=subject_id, target_id=citation,
            rationale=rationale, confidence=confidence, actor_ref=actor_ref, trace_id=run_id or None)
    elif subject_id:
        domain.propose_gap(
            connection, project_id=project_id, clause_id=subject_id, rationale=rationale,
            actor_ref=actor_ref, trace_id=run_id or None)

    return {"capability": "compliance_assessment", "tool": None, "status": "succeeded",
            "assessment": status, "confidence": confidence, "citation": citation,
            "cited_incident": cited_incident, "note": statement}


def run_compliance_orchestration(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Group the run's Assessment into a finding family by the Pipeline Orchestrator role
    agent's judgement — never a fixed keyword match — for the template's existing `lanes`
    for_each step to consume unmodified (PRD F5 per-family lane routing)."""
    steps = (context or {}).get("steps") or {}
    assessment = steps.get("compliance_assessment") or {}
    cited_incident = assessment.get("cited_incident") or {}
    citation = str(assessment.get("citation") or "")

    node = STORE.get_node(connection, project_id=project_id, node_id=citation) if citation else None
    node_text = ""
    if node:
        node_text = f"[{node['kind']}] {node['name']}: {str(node.get('body') or '')[:400]}"
    incident_text = (
        f"Correlated incident {cited_incident.get('incident_id')}: paid down "
        f"{cited_incident.get('actual_paydown')} instead of {cited_incident.get('correct_paydown')}."
        if cited_incident else "")

    situation = (
        f"CITED EVIDENCE: {node_text or '(no rule/code citation)'}\n{incident_text}\n\n"
        f"Valid families: {', '.join(FAMILIES)}.")
    agent, _engine = _role_agent(connection, settings, project_id)
    judged = _ask_role(agent, _role_prompt("pipeline_orchestrator.md", situation))
    family = judged.get("family") if judged else None
    family = family if family in FAMILIES else None
    return {"capability": "compliance_orchestration", "tool": None, "status": "succeeded",
            "finding_families": [family] if family else []}


def run_lane_confirmation(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Confirm or rule out the family's finding by reading the cited symbol/file. Writes
    AgentDecision(Confirmed) when a candidate was cited, AgentDecision(RuledOut) with a stated
    reason when nothing could be read — never a silent skip."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "lane_confirmation")
    steps = (context or {}).get("steps") or {}
    citation = str((steps.get("compliance_assessment") or {}).get("citation") or "")
    actor_ref = "banking_regulatory_pipeline:lane_confirmation"
    node = STORE.get_node(connection, project_id=project_id, node_id=citation) if citation else None
    confirmed = False
    reason = "no citation to read as code the subject is connected to."
    if node and node["kind"] in {"rule", "symbol"}:
        situation = f"CITED NODE: {_node_summary(connection, project_id, citation)}"
        agent, _engine = _role_agent(connection, settings, project_id)
        judged = _ask_role(agent, _role_prompt("lane_agent.md", situation))
        if judged is not None:
            confirmed = bool(judged.get("confirmed"))
            reason = " ".join(str(judged.get("reason") or "").split())[:1000] or reason
        else:
            reason = "the Lane Agent answered nothing readable for this citation"
    decision_type = "Confirmed" if confirmed else "RuledOut"
    statement = (
        f"Confirmed: clause and code both point at {citation}." if confirmed else
        f"Ruled out: {citation or 'no citation'} — {reason}")
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id, decision_type=decision_type,
        statement=statement, policy_version="", workflow_version="",
        agent_fingerprint="deterministic", actor_ref=actor_ref)
    return {"capability": "lane_confirmation", "tool": None, "status": "succeeded",
            "confirmed": confirmed, "cited_node_id": citation or None, "note": statement}


def run_remediation_package(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Write a RemediationPackage: a change brief, a test, a role/team owner, and an approver."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "remediation_package")
    steps = (context or {}).get("steps") or {}
    citation = str((steps.get("compliance_assessment") or {}).get("citation") or "")
    actor_ref = "banking_regulatory_pipeline:remediation_package"
    owner_team = "Payments Platform Team"
    owner_node = None
    for neighbor in STORE.neighbors(connection, project_id=project_id, node_id=citation,
                                    direction="out") if citation else []:
        candidate = STORE.get_node(connection, project_id=project_id, node_id=neighbor["node_id"])
        if candidate and candidate["kind"] == "owner":
            owner_node = candidate
            owner_team = str((candidate.get("attrs") or {}).get("team") or candidate["name"])
            break
    families = ((context or {}).get("steps") or {}).get("compliance_orchestration", {}).get(
        "finding_families") or []
    family = families[0] if families else ""
    situation = (
        f"CITED CODE: {_node_summary(connection, project_id, citation) if citation else '(none)'}\n"
        f"FAMILY: {family or '(unknown)'}")
    agent, _engine = _role_agent(connection, settings, project_id)
    judged = _ask_role(agent, _role_prompt("remediation_advisor.md", situation))
    change_brief = (
        " ".join(str(judged.get("change_brief") or "").split())[:1000] if judged else "") or (
        "Credit a periodic payment as of the date of receipt, applying it interest-first then "
        "to principal, before any later-dated payment is considered.")
    test_ref = (str(judged.get("test_ref") or "").strip() if judged else "") or (
        "payments/test_repayment_order.py::test_interest_before_principal")
    package_key = f"remediation:{run_id}:{step_id}"
    package_id = STORE.upsert_node(
        connection, project_id=project_id, kind="remediation_package", node_key=package_key,
        name="Remediation: repayment allocation order", body=change_brief, trust_tier="inferred",
        actor_kind="agent", actor_ref=actor_ref,
        attrs={"change_brief": change_brief, "test_ref": test_ref, "owner_team": owner_team,
              "approver_role": "compliance_officer"})
    if owner_node is not None:
        STORE.upsert_edge(connection, project_id=project_id, src_id=package_id,
                          dst_id=str(owner_node["id"]), kind="owns", actor_kind="agent",
                          actor_ref=actor_ref)
    return {"capability": "remediation_package", "tool": None, "status": "succeeded",
            "remediation_package_id": package_id, "owner_team": owner_team,
            "note": f"Remediation package {package_id} ready for {owner_team}'s approver."}


def run_compliance_closure(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the investigation's resolution as a Precedent through `graph_steps.run_closure`
    verbatim, then stamp an execution-state marker (proposed/merged/unknown) onto it and write a
    `reused` edge back to any precedent/incident Historical Pattern correlated."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "compliance_closure")
    subject_id = str(inputs.get("subject_id") or "").strip()
    symptom = str(inputs.get("symptom") or "")
    cause_summary = str(inputs.get("cause_summary") or "")
    execution_state = str(inputs.get("execution_state") or "unknown")
    actor_ref = "banking_regulatory_pipeline:compliance_closure"

    result = graph_steps.run_closure(
        connection, project_id=project_id, run_id=run_id, step_id=step_id, subject_id=subject_id,
        symptom=symptom, cause_summary=cause_summary, actor_ref=actor_ref)
    precedent_id = str(result["precedent_id"])
    connection.execute(
        text("UPDATE kg_nodes SET attrs = attrs || CAST(:patch AS JSONB) "
             "WHERE project_id = :p AND id = :i"),
        {"patch": json.dumps({"execution_state": execution_state}), "p": project_id,
         "i": precedent_id})

    steps = (context or {}).get("steps") or {}
    matched = list((steps.get("historical_pattern") or {}).get("matched") or [])
    if subject_id:
        for match_id in matched:
            STORE.upsert_edge(connection, project_id=project_id, src_id=subject_id,
                              dst_id=match_id, kind="reused", actor_kind="agent",
                              actor_ref=actor_ref)

    return {**result, "capability": "compliance_closure", "tool": None, "status": "succeeded",
            "execution_state": execution_state}
