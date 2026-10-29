"""The ``regulatory_trace`` workflow (KG Domain layer v1, A2): for each clause of a document,
search existing rules and code and propose an ``implemented_by`` link, or — when nothing
implements it — a ``gap`` (PRD FR19/FR20, a valid and expected outcome). Every proposal is
``inferred``; nothing here is auto-approved (``domain.propose_implementation``/``propose_gap``).
"""
from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import domain

CAPABILITY = "trace_regulation"
REVIEW_CAPABILITY = "regulatory_trace_review"
#: The decision action of the review gate (decision_actions.WORKFLOW_GATE_ACTIONS).
REVIEW_ACTION = "review_proposed_trace"
DEFAULT_ROLE = "chief_architect"
DEFAULT_BUDGET_USD = 2.00

STOPPED_BY = ("scope_exhausted", "budget")


class RegulatoryTraceRefused(ValueError):
    """The trace cannot start, for a reason a person has to change."""


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


def _candidate_match(connection: Connection, project_id: str, clause_text: str
                     ) -> Optional[str]:
    """The id of a rule or code node whose text search already matches this clause's words,
    else None — a clause with no hit becomes a gap, never a model call (cheaper, and a gap is a
    valid outcome, not an error)."""
    from .store import PostgresKnowledgeStore

    store = PostgresKnowledgeStore()
    try:
        hits = store.text_search(connection, project_id=project_id, query=clause_text, limit=5)
    except ValueError:
        return None
    for hit in hits:
        node_id = hit.get("node_id")
        if node_id:
            return str(node_id)
    return None


def run_step(
    connection: Connection, settings: Any, project_id: str, step: Mapping[str, Any],
    context: Mapping[str, Any], inputs: Mapping[str, Any],
) -> dict[str, Any]:
    """The ``trace_regulation`` workflow step. Without a ``document_id`` there is nothing to
    trace onto, and the step reports zero clauses rather than refusing the run."""
    run_id = str(context.get("run_id") or "")
    trace_id = f"trace_{run_id[-12:]}" if run_id else f"trace_{uuid.uuid4().hex[:16]}"
    document_id = str(inputs.get("document_id") or "").strip()
    implemented = gaps = 0
    if document_id:
        for clause in _clauses_of_document(connection, project_id, document_id):
            target = _candidate_match(connection, project_id, str(clause["text"] or ""))
            if target:
                domain.propose_implementation(
                    connection, project_id=project_id, clause_id=clause["id"], target_id=target,
                    rationale="matched by the trace's text search", confidence=0.6,
                    actor_ref="regulatory_trace", trace_id=trace_id)
                implemented += 1
            else:
                domain.propose_gap(
                    connection, project_id=project_id, clause_id=clause["id"],
                    rationale="no rule or code implements this clause",
                    actor_ref="regulatory_trace", trace_id=trace_id)
                gaps += 1
    return {
        "capability": CAPABILITY, "tool": None, "status": "succeeded", "trace_id": trace_id,
        "clauses_traced": implemented + gaps, "implemented_count": implemented,
        "gap_count": gaps, "spend_usd": 0.0, "stopped_by": "scope_exhausted",
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
