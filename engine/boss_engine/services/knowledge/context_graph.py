"""The Context Graph (Build B, §4.2): a step's own trace, written by the engine at step end.

``AgentDecision``/``CoverageGap`` are real ``kg_nodes`` rows, in the new ``context`` knowledge
layer — the engine's record of what a step decided, queryable like any other node. ``AgentRun``
and ``AgentToolCall`` are the OPPOSITE: read-only projections over the existing
``agent_acts``/``agent_turns`` trace tables, never duplicated into ``kg_nodes`` (two sources of
truth for the same run is the risk the brief calls out).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .store import PostgresKnowledgeStore

_STORE = PostgresKnowledgeStore()


def write_agent_decision(connection: Connection, project_id: str, *, step_id: str, run_id: str,
                          decision_type: str, statement: str, policy_version: str,
                          workflow_version: str, agent_fingerprint: str, actor_ref: str) -> dict[str, Any]:
    """Persist an ``agent_decision`` node for a step's required write.

    ``node_key`` is content-addressed on (run, step, decision_type) so a retry of the same step
    never creates a duplicate node — the store's existing content-hash upsert handles that.
    """
    node_key = f"context:agent_decision:{run_id}:{step_id}:{decision_type}"
    node_id = _STORE.upsert_node(
        connection, project_id=project_id, kind="agent_decision", node_key=node_key,
        name=f"{decision_type} ({step_id})", body=statement, trust_tier="inferred",
        actor_kind="agent", actor_ref=actor_ref, source="context_graph",
        attrs={
            "decision_type": decision_type,
            "statement": statement,
            "step_id": step_id,
            "run_id": run_id,
            "policy_version": policy_version,
            "workflow_version": workflow_version,
            "agent_fingerprint": agent_fingerprint,
        },
    )
    return {"id": node_id, "kind": "agent_decision"}


def write_coverage_gap(connection: Connection, project_id: str, *, step_id: str, run_id: str,
                        statement: str, actor_ref: str) -> dict[str, Any]:
    """Persist a ``coverage_gap`` node — a step's optional "what I couldn't establish" write."""
    node_key = f"context:coverage_gap:{run_id}:{step_id}"
    node_id = _STORE.upsert_node(
        connection, project_id=project_id, kind="coverage_gap", node_key=node_key,
        name=f"Coverage gap ({step_id})", body=statement, trust_tier="inferred",
        actor_kind="agent", actor_ref=actor_ref, source="context_graph",
        attrs={"statement": statement, "step_id": step_id, "run_id": run_id},
    )
    return {"id": node_id, "kind": "coverage_gap"}


def _statement_of(entry: Any, fallback: str) -> str:
    if isinstance(entry, Mapping):
        return str(entry.get("statement") or entry.get("summary") or entry.get("body") or fallback)
    if entry is None:
        return fallback
    return str(entry)


def persist_contract_writes(connection: Connection, project_id: str, *, run_id: str, step_id: str,
                            contract: Any, produced: Mapping[str, Any],
                            provenance: Mapping[str, Any]) -> set[str]:
    """Write a step's declared context outputs to the Context Graph, BY THE ENGINE — the key
    postcondition (§4.1 step 3): a required write becomes a real `kg_nodes` row rather than
    something the model may or may not have remembered to call a tool for.

    Returns the set of contract write kinds that actually landed as nodes, so the runtime can
    tell whether every required write was satisfied.
    """
    from .step_contracts import WriteSpec, _satisfied

    produced = produced or {}
    actor_ref = str(provenance.get("actor_ref") or f"agent:{step_id}")
    landed: set[str] = set()
    for spec in getattr(contract, "writes", ()):  # WriteSpec
        entry = produced.get(spec.kind)
        if entry is None:
            continue
        statement = _statement_of(entry, f"{spec.kind} produced by {step_id}")
        if spec.kind == "agent_decision":
            decision_type = spec.decision_type or (
                entry.get("decision_type") if isinstance(entry, Mapping) else None) or "Decision"
            write_agent_decision(
                connection, project_id, step_id=step_id, run_id=run_id,
                decision_type=decision_type, statement=statement,
                policy_version=str(provenance.get("policy_version") or ""),
                workflow_version=str(provenance.get("workflow_version") or ""),
                agent_fingerprint=str(provenance.get("agent_fingerprint") or ""),
                actor_ref=actor_ref)
        elif spec.kind == "coverage_gap":
            write_coverage_gap(connection, project_id, step_id=step_id, run_id=run_id,
                               statement=statement, actor_ref=actor_ref)
        else:
            node_key = f"context:{spec.kind}:{run_id}:{step_id}"
            _STORE.upsert_node(
                connection, project_id=project_id, kind=spec.kind, node_key=node_key,
                name=f"{spec.kind} ({step_id})", body=statement, trust_tier="inferred",
                actor_kind="agent", actor_ref=actor_ref, source="context_graph",
                attrs={"statement": statement, "step_id": step_id, "run_id": run_id})
        # Only count it against the contract if it satisfies the spec (kind AND, when named,
        # decision_type): a wrong-typed decision is written for the record but does not satisfy
        # a different required decision.
        if _satisfied(spec, produced):
            landed.add(spec.kind)
    return landed


def project_agent_run(connection: Connection, project_id: str, run_id: str) -> Optional[dict[str, Any]]:
    """``AgentRun``, projected read-only from ``agent_acts`` — never a ``kg_nodes`` row."""
    row = connection.execute(
        text(
            """
            SELECT act_id, role_key, agent_id, fingerprint, kind, status, workflow_run_id,
                   step_id, code_task_run_id, accountable, started_at
            FROM agent_acts
            WHERE project_id = :project_id
              AND (root_act_id = :run_id OR workflow_run_id = :run_id OR code_task_run_id = :run_id
                   OR act_id = :run_id)
            ORDER BY started_at ASC
            LIMIT 1
            """
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def project_agent_tool_calls(connection: Connection, project_id: str, run_id: str,
                              step_id: str | None = None) -> list[dict[str, Any]]:
    """``AgentToolCall``, projected read-only from ``agent_turns`` — never a ``kg_nodes`` row."""
    params: dict[str, Any] = {"project_id": project_id, "run_id": run_id}
    clauses = ["project_id = :project_id",
               "(code_task_run_id = :run_id)",
               "direction IN ('tool_call', 'tool_result')"]
    if step_id:
        clauses.append("stage = :step_id")
        params["step_id"] = step_id
    rows = connection.execute(
        text(
            f"""
            SELECT id, stage, role_key, tool_name, direction, content, tokens, latency_ms, ok, error
            FROM agent_turns
            WHERE {" AND ".join(clauses)}
            ORDER BY turn_index ASC
            """
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]
