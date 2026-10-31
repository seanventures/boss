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

from sqlalchemy import text
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

    # Always asked, even when the graph already has a match: a new ticket touching the
    # subject's code/systems since the last run would otherwise go unseen.
    matches.update(_itsm_matches(connection, project_id=project_id, subject_id=subject_id,
                                  actor_ref=actor_ref))

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


def _itsm_matches(connection: Connection, *, project_id: str, subject_id: str, actor_ref: str
                  ) -> dict[str, Mapping[str, Any]]:
    """Ask the project's REGISTERED `itsm` MCP server (bootstrap.BUILTIN_MCP_SERVERS seeds one
    on every project, pointed at `tools/mcp_itsm_mock.py` — the step's `contract.calls` source)
    for a past ticket that names one of the subject's connected code symbols/systems, and write
    it in as an `incident` node — never a bespoke insert, through the same `_STORE.upsert_node`
    every other write here uses — so the NEXT run's graph-only pass above finds it like a
    sibling. Goes through `mcp_registry.call_tool_unattended` like any other registry server's
    tool (no person is present during a workflow step to ask), never a direct import of the
    mock — a re-pointed or disabled `itsm` server must be able to refuse this call exactly as
    it would refuse any other road to a registered MCP tool."""
    from .. import mcp_registry
    from ...config import get_settings

    server = mcp_registry.find_mcp_server(connection, project_id, "itsm")
    if server is None or not server.get("enabled", True):
        return {}

    neighbor_names = [
        str(n.get("name") or "").strip().lower()
        for n in _STORE.neighbors(connection, project_id=project_id, node_id=subject_id,
                                  direction="out")
    ]
    neighbor_names = [n for n in neighbor_names if n]
    if not neighbor_names:
        return {}

    try:
        result = mcp_registry.call_tool_unattended(get_settings(), server, "list_incidents", {})
        incidents = (result or {}).get("incidents") or []
    except Exception:
        return {}

    found: dict[str, Mapping[str, Any]] = {}
    for incident in incidents:
        incident_id = str(incident.get("incident_id") or "").strip()
        if not incident_id:
            continue
        # The ticket's text and its affected components (what a real ITSM ticket records as
        # the code or service the incident touched).
        components = " ".join(str(c) for c in (incident.get("affected_components") or []))
        haystack = f"{incident.get('title', '')} {incident.get('summary', '')} {components}".lower()
        if not any(name in haystack for name in neighbor_names):
            continue
        node_id = _STORE.upsert_node(
            connection, project_id=project_id, kind="incident",
            node_key=f"incident:{incident_id}", name=incident_id,
            body=str(incident.get("summary") or ""), trust_tier="observed",
            actor_kind="agent", actor_ref=actor_ref, attrs={"incident_id": incident_id})
        found[node_id] = {"node_id": node_id, "kind": "incident"}
        # WHERE THE INCIDENT HAPPENED, IN THE CODE. A ticket names the components it touched
        # (LoanPaymentAllocationRule); without an edge to that code the assessment and the lanes
        # only ever saw the regulation's own rule, and the remediation said, rightly, that the
        # allocation code "was never cited or read" (2026-10-31).
        for component in incident.get("affected_components") or []:
            for symbol_id in incident_code(connection, project_id=project_id,
                                           component=str(component)):
                _STORE.upsert_edge(
                    connection, project_id=project_id, src_id=node_id, dst_id=symbol_id,
                    kind="found_in", actor_kind="agent", actor_ref=actor_ref)

    return found


_TEST_PATH = ("/test/", "/tests/", "/e2e", "test_", "_test.", "Test.java", "Tests.java")


def incident_code(connection: Connection, *, project_id: str, component: str,
                  limit: int = 3) -> list[str]:
    """The code symbols a ticket's affected component names: an exact class or method name
    (`LoanPaymentAllocationRule`, `LoanTransactionType.isRepaymentType`), never test code."""
    name = component.strip()
    if not name:
        return []
    rows = connection.execute(
        text("SELECT id, node_key FROM kg_nodes WHERE project_id = :p AND kind = 'symbol' "
             "AND (name = :n OR name LIKE :suffix) AND (valid_to IS NULL OR valid_to > now()) "
             "ORDER BY length(name) LIMIT 40"),
        {"p": project_id, "n": name, "suffix": f"%.{name}"},
    ).mappings().all()
    keep = [r["id"] for r in rows
            if not any(t in str(r["node_key"] or "") for t in _TEST_PATH)]
    return keep[:limit]


def code_found_in(connection: Connection, *, project_id: str, incident_ids: Sequence[str]
                  ) -> list[str]:
    """The symbols the given incidents were found in (their `found_in` edges)."""
    out: list[str] = []
    for incident_id in incident_ids:
        for neighbor in _STORE.neighbors(connection, project_id=project_id, node_id=incident_id,
                                         direction="out", kind="found_in"):
            if neighbor.get("kind") == "symbol" and neighbor["node_id"] not in out:
                out.append(neighbor["node_id"])
    return out


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
