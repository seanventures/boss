"""The ``regulatory_trace`` workflow (KG Domain layer v1, A2): for each clause of a document,
search existing rules and code, have the governed agent judge the candidates, and propose an
``implemented_by`` link, or — when nothing implements it — a ``gap`` (PRD FR19/FR20, a valid and
expected outcome). Every proposal is ``inferred``; nothing here is auto-approved
(``domain.propose_implementation``/``propose_gap``).
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import domain

CAPABILITY = "trace_regulation"
REVIEW_CAPABILITY = "regulatory_trace_review"
#: The decision action of the review gate (decision_actions.WORKFLOW_GATE_ACTIONS).
REVIEW_ACTION = "review_proposed_trace"
DEFAULT_ROLE = "chief_architect"
DEFAULT_BUDGET_USD = 2.00
#: Candidates offered to the judge for one clause.
TOP_K_CANDIDATES = 5
#: What one judging call reserves before it is sent (the same per-call estimate rule_scan uses).
CALL_ESTIMATE_USD_MICROS = 150_000
MAX_TURNS = 4

STOPPED_BY = ("scope_exhausted", "budget")

_MARK_OPEN = "=== JUDGEMENT ==="
_MARK_CLOSE = "=== END JUDGEMENT ==="

#: Node kinds a judge may point to as "this implements the clause" — never a clause or a
#: document, those are what is being traced, not what traces it.
_CORE_IMPLEMENTATION_KINDS = frozenset({"rule", "symbol"})


class RegulatoryTraceRefused(ValueError):
    """The trace cannot start, for a reason a person has to change."""


def _implementation_kinds(connection: Connection, project_id: str) -> frozenset[str]:
    """Rule and code node kinds, core or pack-granted: an ontology pack may add its own
    implementation kinds (``layer: code``) alongside the core ``rule``/``symbol`` kinds."""
    from .ontology_packs import get_project_pack

    kinds = set(_CORE_IMPLEMENTATION_KINDS)
    pack = get_project_pack(connection, project_id=project_id)
    if pack:
        kinds |= {kind for kind, meta in pack.kind_meta.items() if meta.get("layer") == "code"}
    return frozenset(kinds)


def _clauses_of_document(connection: Connection, project_id: str, document_id: str
                         ) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT id, name AS section, body AS text FROM kg_nodes "
            "WHERE project_id = :p AND kind = 'clause' AND valid_to IS NULL "
            "AND attrs->>'document_id' = :d ORDER BY name"
        ),
        {"p": project_id, "d": document_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _candidates(connection: Connection, project_id: str, clause_id: str, clause_text: str,
                *, limit: int = TOP_K_CANDIDATES) -> list[dict[str, Any]]:
    """Up to `limit` rule/code nodes whose text search matches this clause's words, ranked by
    the search's own order. Never the clause itself, never a clause or document node — only
    kinds this project's ontology marks as implementation (`_implementation_kinds`). A clause
    with no candidate becomes a gap, never a model call (cheaper, and a gap is a valid
    outcome, not an error)."""
    from .store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore()
    try:
        hits = store.text_search(connection, project_id=project_id, query=clause_text,
                                 limit=max(limit, 5))
    except ValueError:
        return []
    ids = [str(hit["node_id"]) for hit in hits if hit.get("node_id")
           and str(hit["node_id"]) != clause_id]
    if not ids:
        return []
    allowed = _implementation_kinds(connection, project_id)
    rows = connection.execute(
        text("SELECT id, kind, name FROM kg_nodes WHERE project_id = :p AND id = ANY(:ids) "
             "AND valid_to IS NULL"),
        {"p": project_id, "ids": ids},
    ).mappings().all()
    by_id = {str(row["id"]): row for row in rows}
    out: list[dict[str, Any]] = []
    for node_id in ids:
        row = by_id.get(node_id)
        if row is not None and row["kind"] in allowed:
            out.append({"node_id": node_id, "kind": str(row["kind"]), "name": str(row["name"] or "")})
        if len(out) >= limit:
            break
    return out


# ── The judge (a governed agent call, never a hard-coded guess) ────────────────────────────────

def _judge_prompt(clause_text: str, candidates: Sequence[Mapping[str, Any]]) -> str:
    listing = "\n".join(f"{i}. [{c['kind']}] {c['name']}" for i, c in enumerate(candidates))
    return (
        "You are judging, for a bank's compliance team, whether a rule or code node already "
        "implements a regulatory clause. Pick the ONE candidate that implements the clause, or "
        "say none of them do (a gap) when none genuinely implements it. Never guess; a gap is a "
        "normal, expected answer.\n\n"
        f"CLAUSE: {clause_text}\n\n"
        f"CANDIDATES:\n{listing}\n\n"
        f"Answer with a JSON object between the two marker lines, and nothing else between "
        f"them:\n{_MARK_OPEN}\n"
        '{"choice": <candidate index, or null for a gap>, "rationale": "why", '
        '"confidence": 0.0}\n'
        f"{_MARK_CLOSE}\n"
        "Confidence is how sure you are the choice (implementation or gap) is correct, 0 to 1."
    )


def _parse_judgement(answer: str, n_candidates: int) -> Optional[dict[str, Any]]:
    body = str(answer or "")
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
    if not isinstance(parsed, dict):
        return None
    choice = parsed.get("choice")
    index: Optional[int] = None
    if isinstance(choice, (int, float)) and not isinstance(choice, bool):
        candidate = int(choice)
        if 0 <= candidate < n_candidates:
            index = candidate
    try:
        confidence = min(1.0, max(0.0, float(parsed.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.5
    rationale = " ".join(str(parsed.get("rationale") or "").split())[:1000]
    return {"index": index, "confidence": confidence, "rationale": rationale or
            ("judged no candidate implements this clause" if index is None else
             "judged this candidate implements the clause")}


def _trace_agent(connection: Connection, settings: Any, project_id: str) -> tuple[Any, str]:
    from ..code_agent import make_code_agent

    engine = str(getattr(settings, "code_review_engine", None)
                 or getattr(settings, "code_agent_engine", None) or "claude_code").lower()
    return make_code_agent(settings, engine=engine, connection=connection,
                           project_id=project_id), engine


def _judge(agent: Any, clause_text: str, candidates: Sequence[Mapping[str, Any]]
          ) -> dict[str, Any]:
    """Run the judging call and return its parsed answer, defaulting to a low-confidence gap
    when the engine answers nothing readable (never a hard-coded verdict)."""
    prompt = _judge_prompt(clause_text, candidates)
    with tempfile.TemporaryDirectory(prefix="boss-regulatory-trace-") as cwd:
        result = agent.run_role(prompt, Path(cwd), read_only=True, max_turns=MAX_TURNS) or {}
    judged = _parse_judgement(str(result.get("summary") or ""), len(candidates))
    if judged is None:
        judged = {"index": None, "confidence": 0.0,
                  "rationale": "the agent's answer could not be read as a judgement"}
    judged["usage"] = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}
    judged["cost_usd_micros"] = result.get("cost_usd_micros")
    judged["cost_unreported"] = bool(result.get("cost_unreported"))
    judged["model"] = result.get("model")
    return judged


def run_step(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``trace_regulation`` workflow step. Without a ``document_id`` there is nothing to
    trace onto, and the step reports zero clauses rather than refusing the run. Every clause
    with at least one candidate is judged by the governed agent (DEFAULT_ROLE), within
    DEFAULT_BUDGET_USD; a clause with no candidates is a gap with zero spend."""
    from .rule_scan import BudgetRefused, Governor

    run_id = str(context.get("run_id") or "")
    trace_id = f"trace_{run_id[-12:]}" if run_id else f"trace_{uuid.uuid4().hex[:16]}"
    document_id = str(inputs.get("document_id") or "").strip()
    implemented = gaps = 0
    spent_usd_micros = 0
    stopped_by = "scope_exhausted"
    budget_usd_micros = int(round(float(inputs.get("budget_usd") or DEFAULT_BUDGET_USD)
                                  * 1_000_000))

    agent: Optional[Any] = None
    governor: Optional[Any] = None
    estimate = CALL_ESTIMATE_USD_MICROS

    if document_id:
        clauses = _clauses_of_document(connection, project_id, document_id)
        for clause in clauses:
            clause_id = str(clause["id"])
            clause_text = str(clause["text"] or "")
            candidates = _candidates(connection, project_id, clause_id, clause_text)
            if not candidates:
                domain.propose_gap(
                    connection, project_id=project_id, clause_id=clause_id,
                    rationale="no rule or code candidate was found for this clause",
                    actor_ref="regulatory_trace", trace_id=trace_id)
                gaps += 1
                continue
            if agent is None:
                agent, engine = _trace_agent(connection, settings, project_id)
                governor = Governor(connection, settings, project_id, engine=engine,
                                    ref=trace_id)
                estimate = int(getattr(governor, "estimate_usd_micros",
                                       CALL_ESTIMATE_USD_MICROS))
            if spent_usd_micros + estimate > budget_usd_micros:
                stopped_by = "budget"
                break
            try:
                held = governor.reserve()
            except BudgetRefused:
                stopped_by = "budget"
                break
            try:
                judged = _judge(agent, clause_text, candidates)
            except Exception as error:  # noqa: BLE001 — said by stopping, not raised
                governor.fail(held, error)
                stopped_by = "budget"
                break
            usage = dict(judged.get("usage") or {})
            if judged.get("model") and not usage.get("model"):
                usage["model"] = judged["model"]
            if not judged.get("cost_unreported") and judged.get("cost_usd_micros") is not None:
                usage["cost_usd_micros"] = int(judged.get("cost_usd_micros") or 0)
            cost = int(governor.settle(held, usage) or 0)
            if judged.get("cost_unreported") and not cost and estimate:
                cost = estimate
            spent_usd_micros += cost

            choice = judged.get("index")
            rationale = judged.get("rationale") or ""
            confidence = float(judged.get("confidence") or 0.0)
            if choice is None:
                domain.propose_gap(
                    connection, project_id=project_id, clause_id=clause_id,
                    rationale=rationale or "the agent judged no candidate implements this clause",
                    actor_ref="regulatory_trace", trace_id=trace_id)
                gaps += 1
            else:
                target = candidates[choice]["node_id"]
                domain.propose_implementation(
                    connection, project_id=project_id, clause_id=clause_id, target_id=target,
                    rationale=rationale or "the agent judged this candidate implements the clause",
                    confidence=confidence, actor_ref="regulatory_trace", trace_id=trace_id)
                implemented += 1
        else:
            stopped_by = "scope_exhausted"
    return {
        "capability": CAPABILITY, "tool": None, "status": "succeeded", "trace_id": trace_id,
        "clauses_traced": implemented + gaps, "implemented_count": implemented,
        "gap_count": gaps, "spend_usd": round(spent_usd_micros / 1_000_000, 6),
        "stopped_by": stopped_by,
        "note": f"Traced {implemented + gaps} clause(s): {implemented} implemented_by, "
                f"{gaps} gap. A person reviews each proposal.",
        "review_link": review_link(trace_id),
    }


def review_link(trace_id: str) -> dict[str, Any]:
    return {"area": "knowledge", "view": "regulatory-trace", "status": "proposed",
            "trace_id": trace_id, "label": "Open regulatory trace review"}


def _trace_id_of(context: Mapping[str, Any]) -> str:
    for produced in (context.get("steps") or {}).values():
        if isinstance(produced, Mapping) and produced.get("trace_id"):
            return str(produced["trace_id"])
    return ""


def has_open_proposals(connection: Connection, project_id: str,
                       context: Optional[Mapping[str, Any]]) -> bool:
    return domain.open_proposals(connection, project_id, _trace_id_of(context or {})) > 0


def not_applicable(context: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    trace_id = _trace_id_of(context or {})
    return {"capability": REVIEW_CAPABILITY, "tool": None, "status": "not_applicable",
            "trace_id": trace_id or None,
            "note": "Nothing to review: the trace proposed no implemented_by or gap finding."}


def review_card(connection: Connection, project_id: str, context: Mapping[str, Any]
               ) -> dict[str, Any]:
    trace_id = _trace_id_of(context)
    return {"trace_id": trace_id or None,
            "proposed_links": domain.open_proposals(connection, project_id, trace_id),
            "link": review_link(trace_id)}
