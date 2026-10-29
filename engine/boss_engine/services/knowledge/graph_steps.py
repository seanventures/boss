"""KG P5a: the two `deterministic` graph steps (design: ~/boss-ops design §4.4).

Each runs a FIXED knowledge-graph query — never a model call, $0, repeatable — and writes
through the Context Graph's existing primitives (`context_graph.write_agent_decision` /
`write_coverage_gap`, shipped in Build B).

- Context Enrichment: within a hop cap, collect what is connected to a subject node; what the
  cap couldn't reach becomes a CoverageGap, never a silent omission.
- Historical Pattern: find prior incidents/precedents that share the subject's links, and link
  them with `correlates_with` (a core edge kind) so a later run's Historical Pattern step finds
  what an earlier run's Closure wrote — the learning loop the design calls for.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sqlalchemy.engine import Connection

from . import context_graph
from .store import PostgresKnowledgeStore

_STORE = PostgresKnowledgeStore()

#: Node kinds a correlated match may be: an incident that already happened, or a precedent an
#: earlier investigation's Closure wrote.
_PATTERN_KINDS = frozenset({"incident", "precedent"})

#: Depth `traverse` is extended to when a hop cap misses an expected kind, so the gap can say
#: whether it was never there or just beyond the cap.
_EXTENDED_HOPS = 5


def run_context_enrichment(
    connection: Connection, *, project_id: str, run_id: str, step_id: str, subject_id: str,
    max_hops: int, expected_kinds: Sequence[str] = (),
    actor_ref: str = "graph_steps:context_enrichment",
) -> dict[str, Any]:
    """Collect the rules, code, systems, owners and reports connected to `subject_id` within
    `max_hops`. Writes an AgentDecision(ContextAssembled) always, and a CoverageGap for any
    `expected_kinds` not found within the cap."""
    within = _STORE.traverse(connection, project_id=project_id, start_id=subject_id,
                              max_depth=max_hops)
    within_kinds = _kinds_of(connection, project_id, within)
    missing = [kind for kind in expected_kinds if kind not in within_kinds]

    found_beyond: list[str] = []
    if missing:
        extended = _STORE.traverse(connection, project_id=project_id, start_id=subject_id,
                                    max_depth=max_hops + _EXTENDED_HOPS)
        extended_kinds = _kinds_of(connection, project_id, extended)
        found_beyond = [kind for kind in missing if kind in extended_kinds]

    statement = (
        f"Collected {len(within)} node(s) connected to the subject within {max_hops} hop(s): "
        f"{', '.join(sorted(within_kinds)) or 'none'}."
    )
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id,
        decision_type="ContextAssembled", statement=statement, policy_version="",
        workflow_version="", agent_fingerprint="deterministic", actor_ref=actor_ref)

    if missing:
        detail = "; ".join(
            f"{kind} found beyond the {max_hops}-hop cap" if kind in found_beyond
            else f"no {kind} found" for kind in missing)
        context_graph.write_coverage_gap(
            connection, project_id, step_id=step_id, run_id=run_id,
            statement=f"Expected but missing within the {max_hops}-hop cap: {detail}.",
            actor_ref=actor_ref)

    return {
        "cost_usd_micros": 0,
        "collected": [row["node_id"] for row in within],
        "missing_kinds": missing,
    }


def run_historical_pattern(
    connection: Connection, *, project_id: str, run_id: str, step_id: str, subject_id: str,
    actor_ref: str = "graph_steps:historical_pattern",
) -> dict[str, Any]:
    """Find incidents/precedents that share a link with `subject_id` and correlate them with
    `correlates_with` — including a precedent an earlier run's Closure wrote, so a later run's
    Historical Pattern step finds it (the learning loop)."""
    matches: dict[str, Mapping[str, Any]] = {}
    for neighbor in _STORE.neighbors(connection, project_id=project_id, node_id=subject_id,
                                      direction="out"):
        for candidate in _STORE.neighbors(connection, project_id=project_id,
                                           node_id=neighbor["node_id"], direction="in"):
            if candidate["node_id"] == subject_id:
                continue
            if candidate["kind"] in _PATTERN_KINDS:
                matches[candidate["node_id"]] = candidate

    for match_id in matches:
        _STORE.upsert_edge(
            connection, project_id=project_id, src_id=subject_id, dst_id=match_id,
            kind="correlates_with", actor_kind="agent", actor_ref=actor_ref)

    statement = (
        f"Correlated {len(matches)} prior incident/precedent(s) sharing the subject's links."
        if matches else "No prior incident or precedent shares the subject's links.")
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id,
        decision_type="HistoricalPatternMatched", statement=statement, policy_version="",
        workflow_version="", agent_fingerprint="deterministic", actor_ref=actor_ref)

    return {"cost_usd_micros": 0, "matched": list(matches.keys())}


def run_closure(
    connection: Connection, *, project_id: str, run_id: str, step_id: str, subject_id: str,
    symptom: str, cause_summary: str, fix_summary: str = "",
    actor_ref: str = "graph_steps:closure",
) -> dict[str, Any]:
    """Record this investigation's resolution as a Precedent, through the SAME write
    primitives the deterministic steps use (``context_graph`` / the store's ``upsert_node``) —
    never a one-off insert of its own — so a later run's Historical Pattern step
    (`run_historical_pattern`) finds what this run's Closure wrote.

    The precedent is linked to what the subject was ALREADY connected to (its out-neighbors),
    not to the subject itself: `run_historical_pattern` matches a sibling that shares one of
    those links, so a later investigation sharing any of the same links — on this subject or
    one like it — finds this precedent exactly as it would find a sibling incident."""
    node_key = f"precedent:{run_id}:{step_id}"
    precedent_id = _STORE.upsert_node(
        connection, project_id=project_id, kind="precedent", node_key=node_key,
        name=f"Precedent: {symptom[:150]}", body=cause_summary or symptom,
        trust_tier="inferred", actor_kind="agent", actor_ref=actor_ref,
        attrs={"symptom": symptom, "cause_summary": cause_summary, "fix_summary": fix_summary,
               "run_id": run_id, "step_id": step_id})
    for neighbor in _STORE.neighbors(connection, project_id=project_id, node_id=subject_id,
                                      direction="out"):
        _STORE.upsert_edge(
            connection, project_id=project_id, src_id=precedent_id, dst_id=neighbor["node_id"],
            kind="about", actor_kind="agent", actor_ref=actor_ref)

    statement = f"Recorded this investigation's resolution as a precedent: {symptom[:150]}."
    context_graph.write_agent_decision(
        connection, project_id, step_id=step_id, run_id=run_id,
        decision_type="PrecedentRecorded", statement=statement, policy_version="",
        workflow_version="", agent_fingerprint="deterministic", actor_ref=actor_ref)

    return {"cost_usd_micros": 0, "precedent_id": precedent_id}


def _kinds_of(connection: Connection, project_id: str, rows: Sequence[Mapping[str, Any]]
             ) -> set[str]:
    kinds: set[str] = set()
    for row in rows:
        node = _STORE.get_node(connection, project_id=project_id, node_id=row["node_id"])
        if node:
            kinds.add(str(node["kind"]))
    return kinds
