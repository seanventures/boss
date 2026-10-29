"""Propose, approve and reject Domain-layer trace findings (KG Domain layer v1, A2): a clause
either is ``implemented_by`` a rule or code node, or is a ``gap`` nobody implements. Review is at
the edge, not a new node (SPEC resolution #6): ``regulatory_trace`` writes every proposal
``trust_tier='inferred'``; only a person can flip one to ``approved``, mirroring
``store._assert_can_write``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import now_utc
from .store import PostgresKnowledgeStore

STORE = PostgresKnowledgeStore()

PROPOSAL_EDGE_KINDS = ("implemented_by", "gap")


class TraceRefused(ValueError):
    """A trace proposal or review action cannot complete, for a reason a person has to change."""


def propose_implementation(
    connection: Connection, *, project_id: str, clause_id: str, target_id: str,
    rationale: str, confidence: float, actor_ref: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Clause→rule or clause→code, written ``inferred``; never approved by this call."""
    attrs = {"rationale": str(rationale or ""), "confidence": float(confidence)}
    if trace_id:
        attrs["trace_id"] = trace_id
    edge_id = STORE.upsert_edge(
        connection, project_id=project_id, src_id=clause_id, dst_id=target_id,
        kind="implemented_by", actor_kind="agent", actor_ref=actor_ref or "regulatory_trace",
        attrs=attrs,
    )
    return {"edge_id": edge_id, "kind": "implemented_by"}


def propose_gap(
    connection: Connection, *, project_id: str, clause_id: str, rationale: str,
    actor_ref: Optional[str] = None, trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """A clause nobody implements: a valid, expected outcome, not an error (PRD FR20). The
    sentinel target is the clause itself (SPEC resolution #6: clause → document or a sentinel)."""
    attrs = {"rationale": str(rationale or "")}
    if trace_id:
        attrs["trace_id"] = trace_id
    edge_id = STORE.upsert_edge(
        connection, project_id=project_id, src_id=clause_id, dst_id=clause_id,
        kind="gap", actor_kind="agent", actor_ref=actor_ref or "regulatory_trace",
        attrs=attrs,
    )
    return {"edge_id": edge_id, "kind": "gap"}


def _edge_in_project(connection: Connection, project_id: str, edge_id: str) -> Mapping[str, Any]:
    row = connection.execute(
        text("SELECT id, project_id, kind, trust_tier FROM kg_edges WHERE id = :id"),
        {"id": edge_id},
    ).mappings().first()
    if row is None:
        raise TraceRefused(f"edge not found: {edge_id}")
    if row["project_id"] != project_id:
        raise TraceRefused(f"edge {edge_id} is not in project {project_id}")
    if row["kind"] not in PROPOSAL_EDGE_KINDS:
        raise TraceRefused(f"edge {edge_id} is not a trace proposal (kind={row['kind']!r})")
    return row


def approve_link(
    connection: Connection, *, project_id: str, edge_id: str, actor_kind: str, actor_ref: str,
    note: str = "",
) -> dict[str, Any]:
    if actor_kind != "person":
        raise TraceRefused("only a person may approve a proposed trace link.")
    row = _edge_in_project(connection, project_id, edge_id)
    if row["trust_tier"] == "approved":
        return {"edge_id": edge_id, "trust_tier": "approved", "already": True}
    connection.execute(
        text(
            "UPDATE kg_edges SET trust_tier = 'approved', "
            "attrs = attrs || CAST(:extra AS JSONB) WHERE id = :id"
        ),
        {"id": edge_id, "extra": json.dumps(
            {"reviewed_by": actor_ref, "reviewed_at": now_utc().isoformat(), "note": note})},
    )
    return {"edge_id": edge_id, "trust_tier": "approved", "already": False}


def reject_link(
    connection: Connection, *, project_id: str, edge_id: str, actor_kind: str, actor_ref: str,
    note: str = "",
) -> dict[str, Any]:
    if actor_kind != "person":
        raise TraceRefused("only a person may reject a proposed trace link.")
    _edge_in_project(connection, project_id, edge_id)
    STORE.close_validity(connection, project_id=project_id, valid_to=now_utc(),
                         actor_kind="person", edge_id=edge_id)
    connection.execute(
        text("UPDATE kg_edges SET attrs = attrs || CAST(:extra AS JSONB) WHERE id = :id"),
        {"id": edge_id, "extra": json.dumps({"reviewed_by": actor_ref, "note": note})},
    )
    return {"edge_id": edge_id, "status": "rejected"}


def list_pending(
    connection: Connection, *, project_id: str, trace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    trace_sql = "" if not trace_id else "AND attrs->>'trace_id' = :trace_id"
    rows = connection.execute(
        text(
            f"""
            SELECT id, kind, src_id, dst_id, attrs FROM kg_edges
            WHERE project_id = :project_id AND kind = ANY(:kinds) AND trust_tier = 'inferred'
              AND valid_to IS NULL {trace_sql}
            ORDER BY recorded_at
            """
        ),
        {"project_id": project_id, "kinds": list(PROPOSAL_EDGE_KINDS), "trace_id": trace_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def open_proposals(connection: Connection, project_id: str, trace_id: str) -> int:
    if not trace_id:
        return 0
    return int(connection.execute(
        text(
            "SELECT count(*) FROM kg_edges WHERE project_id = :p AND kind = ANY(:kinds) "
            "AND trust_tier = 'inferred' AND valid_to IS NULL AND attrs->>'trace_id' = :t"
        ),
        {"p": project_id, "kinds": list(PROPOSAL_EDGE_KINDS), "t": trace_id},
    ).scalar() or 0)
