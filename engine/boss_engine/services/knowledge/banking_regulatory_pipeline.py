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

import contextlib

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
    ``regulatory_trace``'s unreadable judgement: a gap/ruled-out, never an invented verdict.

    ONE MORE ASK, FOR THE ANSWER ONLY. An agent that spends its turns reading the code (the
    Remediation Advisor did, and found the right classes) can end without its judgement; it is
    asked once more, briefly, to answer from what it has, before the step falls back."""
    with tempfile.TemporaryDirectory(prefix="boss-banking-regulatory-") as cwd:
        result = agent.run_role(prompt, Path(cwd), read_only=True, max_turns=MAX_TURNS) or {}
        parsed = _parse_judgement(str(result.get("summary") or ""))
        if parsed is None and str(result.get("summary") or "").strip():
            followup = (f"{prompt}\n\nWHAT YOU FOUND SO FAR:\n{str(result.get('summary'))[:6000]}\n\n"
                        "Answer NOW from what you already know: only the JSON judgement between "
                        f"the two marker lines ({_MARK_OPEN} / {_MARK_CLOSE}), nothing else.")
            result = agent.run_role(followup, Path(cwd), read_only=True, max_turns=2) or {}
            parsed = _parse_judgement(str(result.get("summary") or ""))
    return parsed


def _parse_judgement(body: str) -> Optional[dict[str, Any]]:
    """The JSON object between the marker lines (or the last object in the text), or None."""
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


@contextlib.contextmanager
def _attributed(connection: Connection, project_id: str, run_id: str, step_id: str,
                role_key: str):
    """The role's model calls belong to THIS run, step and role (llm_telemetry), so the run
    report counts them and the run view names who spent. Without it the four calls of a case
    had no run, role or act, and the report read $0.00 (2026-10-31)."""
    from ..llm_telemetry import get_llm_context, set_llm_context

    before = get_llm_context()
    try:
        set_llm_context(db_engine=getattr(connection, "engine", None), project_id=project_id,
                        workflow_run_id=run_id or None, run_id=run_id or None, stage=step_id,
                        role_key=role_key)
    except Exception:  # noqa: BLE001 — attribution never stops the judgement
        pass
    try:
        yield
    finally:
        try:
            from ..llm_telemetry import clear_llm_context
            clear_llm_context()
            if before:
                set_llm_context(**before)
        except Exception:  # noqa: BLE001
            pass


def _ran_as(role_key: str) -> dict[str, str]:
    """Who did the step, for the run view (chain-model.laneAgentOf): the pack's role agent."""
    return {"agent": role_key, "how": "named"}


def _learned(run_id: str, step_id: str, *, kind: str, title: str, statement: str,
             confidence: float) -> list[dict[str, Any]]:
    """What the step established, in record_learning's shape, so the run's 'Learned so far'
    shows it and later runs can recall it (learning_writeback reads `learned`)."""
    if not statement.strip():
        return []
    return [{"learning_kind": kind, "title": title[:160], "statement": statement[:1500],
             "detail": "", "about": {},
             "evidence": [{"type": "log", "ref": f"workflow_run:{run_id}/{step_id}",
                           "excerpt": statement[:500]}],
             "confidence": max(0.0, min(1.0, float(confidence)))}]


def _node_summary(connection: Connection, project_id: str, node_id: str) -> str:
    node = STORE.get_node(connection, project_id=project_id, node_id=node_id)
    if not node:
        return f"[{node_id}] (not found)"
    return f"[{node['kind']}] {node_id}: {node['name']} — {str(node.get('body') or '')[:400]}"


def run_clause_intake(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """Open one ComplianceCase per clause of `document_id` — or, with no document (or none of
    its clauses found), one for the run's subject clause — and record ONE
    AgentDecision(ClauseIntake) saying what was opened. Returns both as `produced`, so the
    step's contract can hold the engine to them."""
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "clause_intake")
    document_id = str(inputs.get("document_id") or "").strip()
    subject_id = str(inputs.get("subject_id") or "").strip()
    actor_ref = "banking_regulatory_pipeline:clause_intake"
    clause_ids: list[str] = []
    if document_id:
        clause_ids = [str(r["id"]) for r in connection.execute(
            text("SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'clause' "
                 "AND valid_to IS NULL AND attrs->>'document_id' = :d ORDER BY name"),
            {"p": project_id, "d": document_id},
        ).mappings().all()]
    case_ids: list[str] = []
    if not clause_ids and subject_id:
        # THE CASE IS ABOUT THE SUBJECT when no document says which clauses changed: a run
        # started on one clause opened nothing, and every later step judged a case nobody opened.
        # A subject that already IS a case is that case.
        subject = STORE.get_node(connection, project_id=project_id, node_id=subject_id)
        if subject and subject["kind"] == "compliance_case":
            case_ids.append(subject_id)
        elif subject:
            clause_ids = [subject_id]
    for clause_id in clause_ids:
        case_ids.append(STORE.upsert_node(
            connection, project_id=project_id, kind="compliance_case",
            node_key=f"case:{clause_id}", name=f"Compliance case: {clause_id}", body="",
            trust_tier="inferred", actor_kind="agent", actor_ref=actor_ref,
            attrs={"clause_id": clause_id, "status": "open"}))
    # IN NAMES, NEVER IDS: a person reads this on the step and in the audit trail.
    def _named(node_id: str) -> str:
        node = STORE.get_node(connection, project_id=project_id, node_id=node_id) if node_id else None
        return str(node["name"]) if node and node.get("name") else "an unnamed node"
    source = (f"the regulation \u201c{_named(document_id)}\u201d" if document_id
              else f"\u201c{_named(subject_id)}\u201d" if subject_id else "no subject")
    statement = (f"Opened {len(case_ids)} compliance case(s) for {source}: "
                 f"{'; '.join(_named(c) for c in (clause_ids or case_ids))}." if case_ids
                 else f"No clause found to open a compliance case for ({source}).")
    decision = context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id or f"adhoc:{document_id or subject_id}",
        decision_type="ClauseIntake", statement=statement,
        policy_version="", workflow_version="", agent_fingerprint="deterministic",
        actor_ref=actor_ref)
    produced: dict[str, Any] = {"agent_decision": {
        "decision_type": "ClauseIntake", "statement": statement, "node_id": decision["id"]}}
    if case_ids:
        produced["compliance_case"] = {"statement": statement, "node_ids": case_ids}
    return {"capability": "clause_intake", "tool": None, "status": "succeeded",
            "compliance_case_ids": case_ids, "produced": produced, "note": statement}


def _prior_decisions(connection: Connection, project_id: str, matched: list) -> str:
    """What BOSS decided on this subject before (precedents Historical Pattern found): the
    Analyst weighs them and says whether they still hold. A rerun used to find the precedent and
    then ignore it."""
    lines = []
    for node_id in matched:
        node = STORE.get_node(connection, project_id=project_id, node_id=str(node_id))
        if node and node["kind"] == "precedent":
            lines.append(f"- {node['name']}: {str(node.get('body') or '')[:600]}")
    if not lines:
        return ""
    return ("PRIOR DECISIONS ON THIS SUBJECT (earlier cases; say whether each still holds):\n"
            + "\n".join(lines[:3]) + "\n\n")


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

    # THE CODE THE INCIDENT HAPPENED IN joins the candidates (graph_steps.code_found_in): a
    # regulation's rule leads to how transactions are classified, the incident to where amounts
    # are applied, and the assessment must see both.
    incident_code = graph_steps.code_found_in(connection, project_id=project_id,
                                              incident_ids=matched)
    known = {c["node_id"] for c in candidates}
    for symbol_id in incident_code:
        node = STORE.get_node(connection, project_id=project_id, node_id=symbol_id)
        if node and symbol_id not in known:
            candidates.append({"node_id": symbol_id, "node": node, "from_incident": True})

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
        listing = "\n".join(
            f"{i}. {c['node_id']}: {_node_summary(connection, project_id, c['node_id'])}"
            + (" (code the incident was found in)" if c.get("from_incident") else "")
            for i, c in enumerate(candidates))
        precedent_text = _prior_decisions(connection, project_id, matched)
        incident_text = precedent_text + (
            f"Historical Pattern correlated incident {cited_incident['incident_id']}: paid down "
            f"{cited_incident['actual_paydown']:.2f} instead of "
            f"{cited_incident['correct_paydown']:.2f}.\n" if cited_incident else "")
        clause_text, clause_citation = _clause_text_and_citation(
            connection, project_id, subject_id, context)
        situation = (
            f"CLAUSE ({clause_citation or 'no citation'}): {clause_text or '(not available)'}\n"
            f"SUBJECT: {_node_summary(connection, project_id, subject_id)}\n\n{incident_text}"
            f"CANDIDATE RULE/CODE NODES:\n{listing or '(none)'}\n\n"
            "Cite a candidate's node_id above, or null for a gap.")
        agent, _engine = _role_agent(connection, settings, project_id)
        with _attributed(connection, project_id, run_id, step_id, "compliance_analyst"):
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
    decision = context_graph.write_agent_decision(
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
            "cited_incident": cited_incident, "note": statement, "subject_id": subject_id,
            "produced": {"agent_decision": {"decision_type": "Assessment", "statement": statement,
                                            "node_id": decision["id"]}},
            "ran_as": _ran_as("compliance_analyst"),
            "learned": _learned(run_id, step_id, kind="decision",
                                title=f"Clause assessed as {status}", statement=statement,
                                confidence=confidence),
            "cited_fact_ids": [i for i in (citation, *matched) if i]}


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
    run_id = _run_id(step, context)
    step_id = str(step.get("id") or "compliance_orchestration")
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, "pipeline_orchestrator"):
        judged = _ask_role(agent, _role_prompt("pipeline_orchestrator.md", situation))
    family = judged.get("family") if judged else None
    family = family if family in FAMILIES else None
    # THE ROUTING IS A DECISION ON THE RECORD (design §5.3 stage 5): which lane the finding went
    # to and why — or that none fitted — not only a list the for_each reads and forgets.
    why = " ".join(str((judged or {}).get("rationale") or "").split())[:600]
    statement = (f"Routed the finding to the {family.replace('_', ' ')} lane"
                 f"{': ' + why if why else '.'}" if family else
                 "No finding family fitted the evidence; no lane was started"
                 f"{': ' + why if why else '.'}")
    decision = context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id, decision_type="Routed",
        statement=statement, policy_version="", workflow_version="",
        agent_fingerprint="deterministic",
        actor_ref="banking_regulatory_pipeline:compliance_orchestration")
    return {"capability": "compliance_orchestration", "tool": None, "status": "succeeded",
            "finding_families": [family] if family else [], "note": statement,
            "produced": {"agent_decision": {"decision_type": "Routed", "statement": statement,
                                            "node_id": decision["id"]}}}


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


def _clause_of(connection: Connection, project_id: str, node_id: str) -> tuple[str, str]:
    """A clause's (text, citation) when `node_id` is a clause or a compliance case, else ("", "")."""
    node = _clause_node(connection, project_id, node_id)
    if node and node["kind"] == "compliance_case":
        node = _clause_node(connection, project_id,
                            str((node.get("attrs") or {}).get("clause_id") or ""))
    if node and node["kind"] == "clause":
        attrs = node.get("attrs") or {}
        return (str(attrs.get("text") or node.get("body") or ""),
                str(attrs.get("citation") or ""))
    return "", ""


def _clause_text_and_citation(connection: Connection, project_id: str, subject_id: str,
                              context: Optional[Mapping[str, Any]] = None) -> tuple[str, str]:
    """The clause text this run is judged against, and its citation.

    WHERE THE CLAUSE IS, IN ORDER (2026-10-31, KG P7d): the subject of a case is usually the
    business RULE, not the clause, so the lane said "the clause text itself is not provided".
      1. the clauses of the cases this run's Clause intake opened (the document it started with);
      2. the subject itself, when it is a clause or a compliance case;
      3. a clause that `implemented_by` reaches the subject through (rule -> code), as the
         assessment writes it when it finds an implementation.
    Several clauses are joined, each with its own citation."""
    found: list[tuple[str, str]] = []
    steps = (context or {}).get("steps") or {}
    for case_id in ((steps.get("clause_intake") or {}).get("compliance_case_ids") or []):
        clause = _clause_of(connection, project_id, str(case_id))
        if clause[0] and clause not in found:
            found.append(clause)
    if not found and subject_id:
        clause = _clause_of(connection, project_id, subject_id)
        if clause[0]:
            found.append(clause)
    if not found and subject_id:
        seen, frontier = {subject_id}, [subject_id]
        while frontier and not found:
            node_id = frontier.pop(0)
            for neighbor in STORE.neighbors(connection, project_id=project_id, node_id=node_id,
                                            direction="in"):
                if neighbor.get("edge_kind") != "implemented_by" or neighbor["node_id"] in seen:
                    continue
                seen.add(neighbor["node_id"])
                clause = _clause_of(connection, project_id, neighbor["node_id"])
                if clause[0]:
                    found.append(clause)
                    break
                source = STORE.get_node(connection, project_id=project_id,
                                        node_id=neighbor["node_id"])
                if source and source["kind"] in {"rule", "symbol"}:
                    frontier.append(neighbor["node_id"])
    if not found:
        return "", ""
    text_ = "\n".join(f"[{cite or 'no citation'}] {body}" for body, cite in found)
    citation = "; ".join(dict.fromkeys(cite for _body, cite in found if cite))
    return text_, citation


def _test_layout(connection: Connection, project_id: str, symbol_keys: list[str]) -> str:
    """The repository's language and a real test file near the cited code, for a test name the
    team can use (a Python name was proposed for a Java repository, 2026-10-31)."""
    from .code_index import symbol_location

    for key in symbol_keys:
        repo, path, _name = symbol_location(key)
        if not path or "." not in path:
            continue
        ext = path.rsplit(".", 1)[1]
        rows = connection.execute(
            text("SELECT node_key FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
                 "AND node_key LIKE :like AND (node_key LIKE '%/test/%' OR node_key LIKE '%/tests/%' "
                 "OR node_key LIKE '%test_%' OR node_key LIKE '%Test.%') LIMIT 400"),
            {"p": project_id, "like": f"file:{repo + ':' if repo else ''}%.{ext}"}).scalars().all()
        if not rows:
            return f"REPOSITORY LANGUAGE: .{ext} files (no test file found in the graph)"
        def shared(k: str) -> int:
            other = k.split(":", 2)[-1]
            n = 0
            for a, b in zip(path.split("/"), other.split("/")):
                if a != b:
                    break
                n += 1
            return n
        best = max(rows, key=shared).split(":", 2)[-1]
        return f"REPOSITORY LANGUAGE: .{ext} files. AN EXISTING TEST NEAR THIS CODE: {best}"
    return ""


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
        # AND THE CODE THE INCIDENT HAPPENED IN: a finding is checked where the failure occurred,
        # not only where the regulation's rule points.
        matched = list((steps.get("historical_pattern") or {}).get("matched") or [])
        have = {str(sym.get("id")) for sym in symbols}
        for symbol_id in graph_steps.code_found_in(connection, project_id=project_id,
                                                   incident_ids=matched):
            if symbol_id not in have and len(symbols) < _MAX_CITED_SYMBOLS + 3:
                symbols.extend(_connected_symbols(connection, project_id, symbol_id, "symbol"))
                have.add(symbol_id)
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
                    connection, project_id, subject_id, context)
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
                with _attributed(connection, project_id, run_id, step_id, "lane_agent"):
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
    # SAID IN NAMES, WITH THE REASON. "Confirmed: clause and code both point at kgn_4793…" told a
    # reviewer nothing they could check; the rule's name and what the source showed can be.
    cited = (node or {}).get("name") or citation or "no citation"
    statement = {
        "confirmed": f"Confirmed: {cited} — {reason}",
        "ruled_out": f"Ruled out: {cited} — {reason}",
        "cannot_verify": f"Cannot verify: {cited} — {reason}",
    }[verdict]
    decision = context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id, decision_type=decision_type,
        statement=statement, policy_version="", workflow_version="",
        agent_fingerprint="deterministic", actor_ref=actor_ref)
    return {"capability": "lane_confirmation", "tool": None, "status": "succeeded",
            "verdict": verdict, "confirmed": confirmed, "cited_node_id": citation or None,
            "reason": reason, "note": statement, "ran_as": _ran_as("lane_agent"),
            "produced": {"agent_decision": {"decision_type": decision_type,
                                            "statement": statement, "node_id": decision["id"]}},
            "learned": _learned(run_id, step_id, kind="fact",
                                title=f"Lane verdict: {verdict.replace('_', ' ')}",
                                statement=statement,
                                confidence=0.8 if verdict != "cannot_verify" else 0.4),
            "cited_fact_ids": [citation] if citation else []}


def _lane_symbol_keys(connection: Connection, project_id: str, lanes: list,
                      citation: str) -> list[str]:
    """Node keys of the code the case is about: the citation's symbols, then any other symbol."""
    keys: list[str] = []
    for row in _connected_symbols(connection, project_id, citation,
                                  ((STORE.get_node(connection, project_id=project_id,
                                                   node_id=citation) or {}).get("kind") or "rule")
                                  ) if citation else []:
        if row.get("node_key"):
            keys.append(str(row["node_key"]))
    return keys


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
    # THE WHOLE EVIDENCE CHAIN, NOT THE CITATION ALONE. Given only the cited node, the advisor
    # proposed changing which transaction types count as repayments while the assessment's gap
    # was a missing amount check (INC-2088: 250.00 paid against 218.60 due). A remediation must
    # close the gap the assessment states, in the code the lane read.
    assessment = steps.get("compliance_assessment") or {}
    clause_text, clause_citation = _clause_text_and_citation(
        connection, project_id, str(assessment.get("subject_id") or citation or ""), context)
    incident = str(assessment.get("cited_incident") or "")
    lanes = [r for r in ((steps.get("lanes") or {}).get("results") or []) if isinstance(r, Mapping)]
    if not lanes and isinstance(steps.get("lane_confirmation"), Mapping):
        lanes = [steps["lane_confirmation"]]
    lane_lines = "\n".join(f"- {r.get('note') or ''}" for r in lanes) or "- (no lane verdict)"
    situation = (
        f"CLAUSE: {clause_text or '(not available)'}"
        f"{f' — {clause_citation}' if clause_citation else ''}\n"
        f"ASSESSMENT (the gap to close): {assessment.get('note') or '(none)'}\n"
        f"INCIDENT: {_node_summary(connection, project_id, incident) if incident else '(none)'}\n"
        f"LANE VERDICTS:\n{lane_lines}\n"
        f"CITED CODE: {_node_summary(connection, project_id, citation) if citation else '(none)'}\n"
        f"FAMILY: {family or '(unknown)'}\n"
        f"{_test_layout(connection, project_id, _lane_symbol_keys(connection, project_id, lanes, citation))}")
    agent, _engine = _role_agent(connection, settings, project_id)
    with _attributed(connection, project_id, run_id, step_id, "remediation_advisor"):
        judged = _ask_role(agent, _role_prompt("remediation_advisor.md", situation))
    gap = " ".join(str(assessment.get("note") or "").split())[:600]
    change_brief = (
        " ".join(str(judged.get("change_brief") or "").split())[:1000] if judged else "") or (
        f"No remediation was proposed (the advisor gave no readable answer). Close the gap the "
        f"assessment found: {gap or 'see the assessment'}")
    test_ref = (str(judged.get("test_ref") or "").strip() if judged else "")
    title = (" ".join(str(judged.get("title") or "").split())[:120] if judged else "") or (
        f"Remediation: {(STORE.get_node(connection, project_id=project_id, node_id=citation) or {}).get('name') or family or 'finding'}"
        if citation else f"Remediation: {family or 'finding'}")
    package_key = f"remediation:{run_id}:{step_id}"
    package_id = STORE.upsert_node(
        connection, project_id=project_id, kind="remediation_package", node_key=package_key,
        name=title, body=change_brief, trust_tier="inferred",
        actor_kind="agent", actor_ref=actor_ref,
        attrs={"change_brief": change_brief, "test_ref": test_ref, "owner_team": owner_team,
              "approver_role": "compliance_officer"})
    if owner_node is not None:
        STORE.upsert_edge(connection, project_id=project_id, src_id=package_id,
                          dst_id=str(owner_node["id"]), kind="owns", actor_kind="agent",
                          actor_ref=actor_ref)
    return {"capability": "remediation_package", "tool": None, "status": "succeeded",
            "remediation_package_id": package_id, "owner_team": owner_team,
            "note": f"{title} — ready for {owner_team}'s approver.",
            "produced": {"remediation_package": {"statement": change_brief,
                                                 "node_id": package_id}},
            "ran_as": _ran_as("remediation_advisor"),
            "learned": _learned(run_id, step_id, kind="decision", title=title,
                                statement=change_brief, confidence=0.6),
            "cited_fact_ids": [citation] if citation else []}


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
    # WHAT THIS CASE FOUND, WHEN NOBODY TYPED IT. The precedent a later run recalls was named
    # "Precedent: " with an empty body whenever the run's inputs had no symptom or cause; the
    # run itself says both: the assessment's finding, and the lanes' verdicts and the remedy.
    steps = (context or {}).get("steps") or {}
    if not symptom.strip():
        symptom = str((steps.get("compliance_assessment") or {}).get("note") or "")[:600]
    if not cause_summary.strip():
        lanes = [r for r in ((steps.get("lanes") or {}).get("results") or [])
                 if isinstance(r, Mapping)]
        parts = [str(r.get("note") or "") for r in lanes if r.get("note")]
        remedy = str((steps.get("remediation_package") or {}).get("note") or "")
        cause_summary = " ".join(p for p in (*parts, f"Remedy: {remedy}" if remedy else "") if p)[:1500]

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
