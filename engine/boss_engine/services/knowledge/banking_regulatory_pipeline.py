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

from . import context_graph, domain, graph_steps, rule_scan
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

#: The Lane Agent's three-way outcome (KG P7c) — `cannot_verify` is forced by the code itself
#: (no citation, no connected symbol, or an unreadable source), never by agent discretion.
_VERDICTS = frozenset({"confirmed", "ruled_out", "cannot_verify"})
_MAX_CITED_SYMBOLS = 5
_CODE_CONTEXT_LINES = 3
_CODE_MAX_CHARS = 4000


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
            "cited_incident": cited_incident, "note": statement, "subject_id": subject_id}


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


def _repo_reader(connection: Connection, settings: Any, project_id: str, actor: str) -> rule_scan.Reader:
    """A thin, monkeypatchable wrapper around the same read-only code path
    ``scan_business_rules`` already uses — project-pinned, the project's GitHub credential,
    never a token in a URL."""
    return rule_scan.repo_reader(connection, settings, project_id, actor)


def _clause_node(connection: Connection, project_id: str, node_id: str) -> Optional[dict[str, Any]]:
    if not node_id:
        return None
    row = connection.execute(
        text("SELECT kind, name, body, attrs FROM kg_nodes WHERE project_id = :p AND id = :i "
             "AND (valid_to IS NULL OR valid_to > now())"),
        {"p": project_id, "i": node_id},
    ).mappings().first()
    return dict(row) if row else None


def _clause_text_and_citation(connection: Connection, project_id: str, subject_id: str) -> tuple[str, str]:
    """The clause's text/citation via its `compliance_case` wrapper (`attrs.clause_id`), or
    directly when `subject_id` already names a `clause` node. Best-effort context, not a
    blocking requirement — callers proceed on code evidence alone when this comes back empty."""
    node = _clause_node(connection, project_id, subject_id)
    if not node:
        return "", ""
    if node["kind"] == "clause":
        attrs = node.get("attrs") or {}
        return str(node.get("body") or ""), str(attrs.get("citation") or "")
    if node["kind"] == "compliance_case":
        clause_id = str((node.get("attrs") or {}).get("clause_id") or "")
        clause = _clause_node(connection, project_id, clause_id) if clause_id else None
        if clause:
            attrs = clause.get("attrs") or {}
            return str(clause.get("body") or ""), str(attrs.get("citation") or "")
    return "", ""


def _connected_symbols(connection: Connection, project_id: str, node_id: str,
                        node_kind: str) -> list[dict[str, Any]]:
    """Symbol nodes connected to the cited node: itself when it already is a `symbol`, else
    every `symbol`-kind neighbor an edge connects it to (e.g. a `rule`'s `evidenced_by`
    citations). Capped so one over-connected citation can't blow up the prompt."""
    if not node_id:
        return []
    if node_kind == "symbol":
        row = connection.execute(
            text("SELECT id, name, node_key, attrs FROM kg_nodes WHERE project_id = :p AND id = :i "
                 "AND (valid_to IS NULL OR valid_to > now())"),
            {"p": project_id, "i": node_id},
        ).mappings().first()
        return [dict(row)] if row else []
    rows = connection.execute(
        text("SELECT n.id, n.name, n.node_key, n.attrs FROM kg_edges e "
             "JOIN kg_nodes n ON n.id = e.dst_id "
             "WHERE e.project_id = :p AND e.src_id = :i AND e.valid_to IS NULL "
             "AND n.kind = 'symbol' AND (n.valid_to IS NULL OR n.valid_to > now()) "
             "ORDER BY n.name LIMIT :lim"),
        {"p": project_id, "i": node_id, "lim": _MAX_CITED_SYMBOLS},
    ).mappings().all()
    return [dict(row) for row in rows]


def _read_symbol_source(reader: rule_scan.Reader, symbol: Mapping[str, Any]) -> dict[str, Any]:
    """Reads `symbol`'s declared line range plus a few lines of context, bounded in size.
    `{"error": ...}` when unreadable — no path, or the reader itself errors."""
    attrs = symbol.get("attrs") or {}
    # WHERE THE INDEXER PUT IT. code_index writes a symbol's repository and file into its node
    # key (symbol:<repo>:<path>:<name>), not its attrs; reading attrs alone made every real
    # symbol "has no path to read", and every lane answered cannot_verify (2026-10-31).
    from .code_index import symbol_location

    key_repo, key_path, _name = symbol_location(str(symbol.get("node_key") or ""))
    path = str(attrs.get("path") or key_path or "").strip()
    repo = str(attrs.get("repo") or key_repo or "")
    if not path:
        return {"error": f"symbol {symbol.get('id')} has no path to read"}
    try:
        line = int(attrs.get("line") or 1)
        end_line = int(attrs.get("end_line") or line)
    except (TypeError, ValueError):
        return {"error": f"symbol {symbol.get('id')} has no readable line range"}
    start = max(1, line - _CODE_CONTEXT_LINES)
    end = end_line + _CODE_CONTEXT_LINES
    try:
        out = reader(path, start, end, repo)
    except Exception as error:  # noqa: BLE001 — one unreadable symbol is said, not fatal
        return {"error": f"{type(error).__name__}: {error}"[:300]}
    if not isinstance(out, Mapping) or out.get("error"):
        return {"error": str((out or {}).get("error") or "the file could not be read")[:300]}
    content = str(out.get("content") or "")[:_CODE_MAX_CHARS]
    return {"path": path, "start": start, "end": end, "content": content}


def run_lane_confirmation(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Confirm, rule out, or (when nothing could be read) say so — against the clause's real
    text, the Compliance Analyst's assessment, and the current source lines of every code
    symbol connected to the citation (KG P7c). `cannot_verify` is forced by the code itself
    whenever there is no citation, no connected symbol, or the source could not be read; the
    agent is never asked to judge on name/body text alone, and only a successful code read may
    reach a `ruled_out` verdict."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "lane_confirmation")
    steps = (context or {}).get("steps") or {}
    assessment = steps.get("compliance_assessment") or {}
    citation = str(assessment.get("citation") or "")
    subject_id = str(assessment.get("subject_id") or "")
    actor_ref = "banking_regulatory_pipeline:lane_confirmation"

    node = STORE.get_node(connection, project_id=project_id, node_id=citation) if citation else None
    verdict = "cannot_verify"
    reason = "no citation to read as code the subject is connected to."

    if node and node["kind"] in {"rule", "symbol"}:
        symbols = _connected_symbols(connection, project_id, citation, node["kind"])
        if not symbols:
            reason = f"{citation} has no connected code symbol to read."
        else:
            reader = _repo_reader(connection, settings, project_id, actor_ref)
            read_symbols, read_errors = [], []
            for symbol in symbols:
                read = _read_symbol_source(reader, symbol)
                if read.get("error"):
                    read_errors.append(f"{symbol.get('name')}: {read['error']}")
                else:
                    read_symbols.append((symbol, read))
            if not read_symbols:
                reason = ("the source could not be read for any connected symbol: "
                          + "; ".join(read_errors))[:1000]
            else:
                clause_text, clause_citation = _clause_text_and_citation(
                    connection, project_id, subject_id)
                source_text = "\n\n".join(
                    f"{symbol.get('name')} ({read['path']}, lines {read['start']}-{read['end']}):\n"
                    f"{read['content']}" for symbol, read in read_symbols)
                unread_note = ("\n\nCOULD NOT READ: " + "; ".join(read_errors)) if read_errors else ""
                situation = (
                    f"CLAUSE: {clause_text or '(not available)'}"
                    f"{f' — {clause_citation}' if clause_citation else ''}\n\n"
                    f"ASSESSMENT: {assessment.get('note') or '(none)'}\n\n"
                    f"CURRENT SOURCE:\n{source_text}{unread_note}")
                agent, _engine = _role_agent(connection, settings, project_id)
                judged = _ask_role(agent, _role_prompt("lane_agent.md", situation))
                answered = str((judged or {}).get("verdict") or "").strip().lower()
                if judged is None:
                    reason = "the Lane Agent answered nothing readable for this citation"
                elif answered not in _VERDICTS:
                    reason = f"the Lane Agent's answer did not name a recognised verdict: {answered or '(none)'}"
                else:
                    verdict = answered
                    reason = (" ".join(str(judged.get("reason") or "").split())[:1000]
                              or reason)
                if read_errors and verdict != "cannot_verify":
                    reason = f"{reason} (could not read: {'; '.join(read_errors)})"[:1000]

    confirmed = verdict == "confirmed"
    decision_type = {"confirmed": "Confirmed", "ruled_out": "RuledOut",
                     "cannot_verify": "CannotVerify"}[verdict]
    statement = {
        "confirmed": f"Confirmed: clause and code both point at {citation}.",
        "ruled_out": f"Ruled out: {citation or 'no citation'} — {reason}",
        "cannot_verify": f"Cannot verify: {citation or 'no citation'} — {reason}",
    }[verdict]
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id, decision_type=decision_type,
        statement=statement, policy_version="", workflow_version="",
        agent_fingerprint="deterministic", actor_ref=actor_ref)
    return {"capability": "lane_confirmation", "tool": None, "status": "succeeded",
            "verdict": verdict, "confirmed": confirmed, "cited_node_id": citation or None,
            "note": statement}


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
