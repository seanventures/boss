"""The Context Graph (Build B, §4.2): a step's own trace, written by the engine at step end.

``AgentDecision``/``CoverageGap`` are real ``kg_nodes`` rows, in the new ``context`` knowledge
layer — the engine's record of what a step decided, queryable like any other node. ``AgentRun``
and ``AgentToolCall`` are the OPPOSITE: read-only projections over the existing
``agent_acts``/``agent_turns`` trace tables, never duplicated into ``kg_nodes`` (two sources of
truth for the same run is the risk the brief calls out).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .graph_versions import versions
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
    # Which Knowledge Graph and Context Graph versions this decision was made against (§4.2).
    graph = versions(connection, project_id)
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
            **graph,
        },
    )
    # A retry with the same content keeps the stored attrs, so the versions are stamped apart.
    connection.execute(
        text("UPDATE kg_nodes SET attrs = COALESCE(attrs, '{}'::jsonb) || CAST(:patch AS JSONB) "
             "WHERE project_id = :p AND id = :i AND NOT (COALESCE(attrs, '{}'::jsonb) ? 'kg_version')"),
        {"patch": json.dumps(graph), "p": project_id, "i": node_id})
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


_PROVENANCE_KEYS = ("policy_version", "workflow_version", "agent_fingerprint")


def stamp_provenance(connection: Connection, project_id: str, node_id: str, *, run_id: str,
                     step_id: str, provenance: Mapping[str, Any]) -> None:
    """Record on a node WHICH run and step wrote it, under which policy/workflow version and by
    which agent — the "what this step wrote" a console view reads back. A step's own write is
    made before the engine knows the run's versions (and the store's upsert keeps attrs when the
    content is unchanged), so the engine stamps them afterwards. An empty value never overwrites
    one already recorded."""
    patch: dict[str, Any] = {"run_id": run_id, "step_id": step_id}
    for key in _PROVENANCE_KEYS:
        value = str(provenance.get(key) or "")
        if value:
            patch[key] = value
    connection.execute(
        text("UPDATE kg_nodes SET attrs = COALESCE(attrs, '{}'::jsonb) || CAST(:patch AS JSONB) "
             "WHERE project_id = :p AND id = :i"),
        {"patch": json.dumps(patch), "p": project_id, "i": node_id})


def _named_nodes(entry: Any) -> list[str]:
    """The node ids a produced entry says the step ALREADY wrote itself (`node_id`/`node_ids`)."""
    if not isinstance(entry, Mapping):
        return []
    ids = [str(i) for i in (entry.get("node_ids") or []) if i]
    if entry.get("node_id"):
        ids.insert(0, str(entry["node_id"]))
    return list(dict.fromkeys(ids))


def _verified(connection: Connection, project_id: str, node_ids: list[str], kind: str
              ) -> list[dict[str, Any]]:
    """The named nodes that really exist in this project with this kind — a step's say-so that
    it wrote something is checked against the rows, never trusted."""
    if not node_ids:
        return []
    rows = connection.execute(
        text("SELECT id, attrs FROM kg_nodes WHERE project_id = :p AND kind = :k "
             "AND id = ANY(:ids)"),
        {"p": project_id, "k": kind, "ids": node_ids},
    ).mappings().all()
    return [dict(r) for r in rows]


def persist_contract_writes(connection: Connection, project_id: str, *, run_id: str, step_id: str,
                            contract: Any, produced: Mapping[str, Any],
                            provenance: Mapping[str, Any]) -> set[str]:
    """Write a step's declared context outputs to the Context Graph, BY THE ENGINE — the key
    postcondition (§4.1 step 3): a required write becomes a real `kg_nodes` row rather than
    something the model may or may not have remembered to call a tool for.

    A produced entry that names `node_id`/`node_ids` is a node the step's own code already wrote
    (a ComplianceCase, a RemediationPackage, a Precedent): the engine checks those rows exist
    with the declared kind (and, for a decision, an accepted decision_type) and stamps the run's
    provenance on them instead of writing a copy. Any other entry is written here. Every node
    that lands carries run, step, policy/workflow version and agent fingerprint.

    Returns the set of contract write kinds that actually landed as nodes, so the runtime can
    tell whether every required write was satisfied.
    """
    from .step_contracts import _satisfied, decision_types

    produced = produced or {}
    actor_ref = str(provenance.get("actor_ref") or f"agent:{step_id}")
    landed: set[str] = set()
    for spec in getattr(contract, "writes", ()):  # WriteSpec
        entry = produced.get(spec.kind)
        if entry is None:
            continue
        allowed = decision_types(spec)
        named = _named_nodes(entry)
        if named:
            rows = _verified(connection, project_id, named, spec.kind)
            if spec.kind == "agent_decision" and allowed:
                rows = [r for r in rows if (r.get("attrs") or {}).get("decision_type") in allowed]
            for row in rows:
                stamp_provenance(connection, project_id, str(row["id"]), run_id=run_id,
                                 step_id=step_id, provenance=provenance)
            if rows and _satisfied(spec, produced):
                landed.add(spec.kind)
            continue
        statement = _statement_of(entry, f"{spec.kind} produced by {step_id}")
        if spec.kind == "agent_decision":
            produced_type = entry.get("decision_type") if isinstance(entry, Mapping) else None
            if allowed:
                decision_type = produced_type if produced_type in allowed else allowed[0]
            else:
                decision_type = produced_type or "Decision"
            written = write_agent_decision(
                connection, project_id, step_id=step_id, run_id=run_id,
                decision_type=decision_type, statement=statement,
                policy_version=str(provenance.get("policy_version") or ""),
                workflow_version=str(provenance.get("workflow_version") or ""),
                agent_fingerprint=str(provenance.get("agent_fingerprint") or ""),
                actor_ref=actor_ref)
        elif spec.kind == "coverage_gap":
            written = write_coverage_gap(connection, project_id, step_id=step_id, run_id=run_id,
                                         statement=statement, actor_ref=actor_ref)
        else:
            node_key = f"context:{spec.kind}:{run_id}:{step_id}"
            written = {"id": _STORE.upsert_node(
                connection, project_id=project_id, kind=spec.kind, node_key=node_key,
                name=f"{spec.kind} ({step_id})", body=statement, trust_tier="inferred",
                actor_kind="agent", actor_ref=actor_ref, source="context_graph",
                attrs={"statement": statement, "step_id": step_id, "run_id": run_id})}
        stamp_provenance(connection, project_id, str(written["id"]), run_id=run_id,
                         step_id=step_id, provenance=provenance)
        # Only count it against the contract if it satisfies the spec (kind AND, when named,
        # decision_type): a wrong-typed decision is written for the record but does not satisfy
        # a different required decision.
        if _satisfied(spec, produced):
            landed.add(spec.kind)
    return landed


# ── STORED CONTEXT GRAPH EDGES (§4.2): a decision linked to what its step read ───────────────
#
# A step's AgentDecision is linked, by real `kg_edges` rows the engine writes at step end, to:
#   * what the step CONSULTED — the node ids its output names under a read key (`collected`,
#     `matched`, `consulted_ids`, `evidence`, `source…`): edge `consulted` (attrs.how = "read");
#   * what it CITED — `cited_fact_ids`, `cited_node_id`, `citation`: edge `consulted` with
#     attrs.how = "cited" (the canvas's existing "cited" line; one edge per pair, cited wins);
#   * what it is ABOUT — its subject (`subject_id`), the case or incident it opened or worked on
#     (`*_case_ids`, `incident_id`, a produced `*_case`/incident node): edge `about`.
# The run's own input ids that the step was bound to (`from_input`) count as consulted (the
# document a clause intake read) or about (the subject). Every edge carries the run and step.
# Idempotent: the store's upsert is unique per (src, dst, kind).

_CITE_KEY = re.compile(r"cite|citation")
_ABOUT_KEY = re.compile(r"subject|_case_id|_case_ids|^case_id|^case_ids|incident_id|alert_id")
_CONSULT_KEY = re.compile(r"collected|matched|consult|evidence|source|read|clause|document")
_NODE_ID = re.compile(r"^kgn_[0-9a-f]{8,}$")
_ABOUT_KINDS = ("incident", "alert")


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if _NODE_ID.match(value) else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and _NODE_ID.match(v)]
    return []


def classify_step_ids(output: Any, *, bound_input: Optional[Mapping[str, Any]] = None
                      ) -> dict[str, list[str]]:
    """{"cited": [...], "consulted": [...], "about": [...]} — the node ids a step's output (and
    the run input it was bound to) names, by what each key says it is. Written nodes
    (`produced.<kind>.node_ids`) are returned under "produced" with their kind for the caller."""
    out: dict[str, list[str]] = {"cited": [], "consulted": [], "about": [], "produced": []}

    def take(key: str, value: Any) -> None:
        ids = _ids(value)
        if not ids:
            return
        name = str(key).lower()
        if _CITE_KEY.search(name):
            out["cited"].extend(ids)
        elif _ABOUT_KEY.search(name):
            out["about"].extend(ids)
        elif _CONSULT_KEY.search(name):
            out["consulted"].extend(ids)

    if isinstance(output, Mapping):
        for key, value in output.items():
            take(key, value)
        produced = output.get("produced")
        if isinstance(produced, Mapping):
            for kind, entry in produced.items():
                if not isinstance(entry, Mapping):
                    continue
                for key, value in entry.items():
                    if key in ("node_id", "node_ids"):
                        if kind != "agent_decision":
                            out["produced"].extend(_ids(value))
                    else:
                        take(key, value)
    for key, value in (bound_input or {}).items():
        ids = _ids(value)
        if not ids:
            continue
        (out["about"] if _ABOUT_KEY.search(str(key).lower()) else out["consulted"]).extend(ids)
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def bound_run_input(step_template: Mapping[str, Any], run_input: Mapping[str, Any]) -> dict[str, Any]:
    """The run-input values a step was bound to (`binding.inputs.<name>.from_input`), by name."""
    inputs = ((step_template or {}).get("binding") or {}).get("inputs") or {}
    bound: dict[str, Any] = {}
    if not isinstance(inputs, Mapping):
        return bound
    for name, spec in inputs.items():
        if isinstance(spec, Mapping) and spec.get("from_input"):
            value = (run_input or {}).get(str(spec["from_input"]))
            if value is not None:
                bound[str(name)] = value
    return bound


def link_step_decisions(connection: Connection, project_id: str, *, run_id: str, step_id: str,
                        output: Any, bound_input: Optional[Mapping[str, Any]] = None) -> int:
    """Write the edges from this run step's AgentDecision(s) to what it consulted, cited and is
    about. Returns how many edges were written or refreshed (0 when the step wrote no decision)."""
    decisions = [str(r[0]) for r in connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'agent_decision' "
        "AND valid_to IS NULL AND attrs->>'run_id' = :r AND attrs->>'step_id' = :s"),
        {"p": project_id, "r": run_id, "s": step_id}).all()]
    if not decisions:
        return 0
    found = classify_step_ids(output, bound_input=bound_input)
    wanted = {i for ids in found.values() for i in ids}
    if not wanted:
        return 0
    kinds = {str(r[0]): str(r[1]) for r in connection.execute(text(
        "SELECT id, kind FROM kg_nodes WHERE project_id = :p AND id = ANY(:i)"),
        {"p": project_id, "i": sorted(wanted)}).all()}

    def is_about(node_id: str) -> bool:
        kind = kinds.get(node_id, "")
        return kind.endswith("_case") or kind in _ABOUT_KINDS

    plan: dict[tuple[str, str], dict[str, Any]] = {}
    for node_id in found["consulted"]:
        plan[(node_id, "about" if is_about(node_id) else "consulted")] = {"how": "read"}
    for node_id in found["cited"]:
        plan[(node_id, "consulted")] = {"how": "cited"}
    for node_id in found["about"] + [i for i in found["produced"] if is_about(i)]:
        plan[(node_id, "about")] = {}
    existing = {(str(s), str(d), str(k), str(h or "")) for s, d, k, h in connection.execute(text(
        "SELECT src_id, dst_id, kind, attrs->>'how' FROM kg_edges WHERE project_id = :p "
        "AND src_id = ANY(:s)"), {"p": project_id, "s": decisions}).all()}
    written = 0
    for decision in decisions:
        for (node_id, kind), extra in plan.items():
            if node_id == decision or node_id not in kinds or kinds[node_id] == "agent_decision":
                continue
            if (decision, node_id, kind, str(extra.get("how") or "")) in existing:
                continue  # already linked: a re-run or the backfill writes nothing new
            _STORE.upsert_edge(
                connection, project_id=project_id, src_id=decision, dst_id=node_id, kind=kind,
                actor_kind="system", actor_ref="context_graph",
                attrs={"run_id": run_id, "step_id": step_id, "source": "context_graph", **extra})
            written += 1
    return written


def backfill_decision_edges(connection: Connection, project_id: Optional[str] = None) -> dict[str, int]:
    """Write the stored edges for every workflow step that already ran (§4.2 backfill): one pass
    over the succeeded step runs that have an AgentDecision, idempotent — a second pass writes
    nothing new. ``project_id`` limits it to one project."""
    clauses = ["s.status = 'succeeded'"]
    params: dict[str, Any] = {}
    if project_id:
        clauses.append("s.project_id = :p")
        params["p"] = project_id
    rows = connection.execute(text(
        "SELECT s.project_id, s.workflow_run_id, s.step_id, s.output, r.input, v.spec "
        "FROM workflow_step_runs s JOIN workflow_runs r ON r.id = s.workflow_run_id "
        "LEFT JOIN workflow_versions v ON v.id = r.workflow_version_id "
        f"WHERE {' AND '.join(clauses)} AND EXISTS (SELECT 1 FROM kg_nodes n "
        "WHERE n.project_id = s.project_id AND n.kind = 'agent_decision' "
        "AND n.attrs->>'run_id' = s.workflow_run_id AND n.attrs->>'step_id' = s.step_id "
        # Only decisions not linked yet: a second pass reads nothing to do.
        "AND NOT EXISTS (SELECT 1 FROM kg_edges e WHERE e.src_id = n.id "
        "AND e.attrs->>'source' = 'context_graph')) "
        "ORDER BY s.created_at ASC"), params).mappings().all()
    before = connection.execute(text(
        "SELECT count(*) FROM kg_edges WHERE attrs->>'source' = 'context_graph'"
        + (" AND project_id = :p" if project_id else "")), params).scalar_one()
    steps = 0
    for row in rows:
        output = row["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                output = {}
        spec = row["spec"] if isinstance(row["spec"], Mapping) else {}
        template = next((s for s in spec.get("steps") or []
                         if isinstance(s, Mapping) and s.get("id") == row["step_id"]), {})
        bound = bound_run_input(template, row["input"] if isinstance(row["input"], Mapping) else {})
        with connection.begin_nested():
            link_step_decisions(connection, str(row["project_id"]), run_id=str(row["workflow_run_id"]),
                                step_id=str(row["step_id"]), output=output, bound_input=bound)
        steps += 1
    after = connection.execute(text(
        "SELECT count(*) FROM kg_edges WHERE attrs->>'source' = 'context_graph'"
        + (" AND project_id = :p" if project_id else "")), params).scalar_one()
    return {"steps": steps, "edges_added": int(after) - int(before)}


def step_writes(connection: Connection, project_id: str, *, run_id: str,
                step_id: Optional[str] = None) -> list[dict[str, Any]]:
    """What a run's steps wrote, with provenance — the rows a console "what this step wrote"
    view lists. Read from the stamped `kg_nodes` rows, never from a step's self-reported output."""
    clauses = ["project_id = :p", "attrs->>'run_id' = :r", "valid_to IS NULL"]
    params: dict[str, Any] = {"p": project_id, "r": run_id}
    if step_id:
        clauses.append("attrs->>'step_id' = :s")
        params["s"] = step_id
    rows = connection.execute(text(
        f"SELECT id, kind, name, body, attrs FROM kg_nodes WHERE {' AND '.join(clauses)} "
        "ORDER BY recorded_at ASC"
    ), params).mappings().all()
    out = []
    for row in rows:
        attrs = row["attrs"] or {}
        out.append({
            "id": row["id"], "kind": row["kind"], "name": row["name"],
            "statement": attrs.get("statement") or row["body"],
            "decision_type": attrs.get("decision_type"),
            "step_id": attrs.get("step_id"), "run_id": attrs.get("run_id"),
            **{key: attrs.get(key) for key in _PROVENANCE_KEYS},
        })
    return out


def read_contract_writes(connection: Connection, project_id: str, *, run_id: str, step_id: str,
                          contract: Any) -> dict[str, Any]:
    """What a step's contract declares as writes, read back from the real ``kg_nodes`` rows —
    never from an agent's self-reported HTTP body. A write that was never actually made through
    the Context Graph tools is simply absent from the result, never synthesized."""
    from .step_contracts import decision_types

    produced: dict[str, Any] = {}
    for spec in getattr(contract, "writes", ()):  # WriteSpec
        if spec.kind == "agent_decision":
            types = decision_types(spec)
            if len(types) == 1:
                node_key = f"context:agent_decision:{run_id}:{step_id}:{types[0]}"
            else:
                row = connection.execute(text(
                    "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'agent_decision' "
                    "AND node_key LIKE :prefix ORDER BY recorded_at ASC LIMIT 1"
                ), {"p": project_id, "prefix": f"context:agent_decision:{run_id}:{step_id}:%"}).first()
                if row is not None:
                    attrs = row[0] or {}
                    produced["agent_decision"] = {
                        "decision_type": attrs.get("decision_type"), "statement": attrs.get("statement")}
                continue
            row = connection.execute(text(
                "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'agent_decision' "
                "AND node_key = :nk"
            ), {"p": project_id, "nk": node_key}).first()
            if row is not None:
                attrs = row[0] or {}
                produced["agent_decision"] = {
                    "decision_type": attrs.get("decision_type"), "statement": attrs.get("statement")}
        elif spec.kind == "coverage_gap":
            node_key = f"context:coverage_gap:{run_id}:{step_id}"
            row = connection.execute(text(
                "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'coverage_gap' "
                "AND node_key = :nk"
            ), {"p": project_id, "nk": node_key}).first()
            if row is not None:
                attrs = row[0] or {}
                produced["coverage_gap"] = {"statement": attrs.get("statement")}
        else:
            node_key = f"context:{spec.kind}:{run_id}:{step_id}"
            row = connection.execute(text(
                "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = :k AND node_key = :nk"
            ), {"p": project_id, "k": spec.kind, "nk": node_key}).first()
            if row is not None:
                attrs = row[0] or {}
                produced[spec.kind] = {"statement": attrs.get("statement")}
    return produced


def find_nodes(connection: Connection, project_id: str, *, kind: str,
               query: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """KG P6a read-within-scope: real ``kg_nodes`` rows of one ``kind`` the caller's contract
    grants, at most ``limit`` of them — ``limit <= 0`` means literally zero rows, same as every
    other reader of a policy's ``max_nodes``. Never trusts a self-reported body; the kind is
    checked against the token's policy by the caller before we are reached."""
    if int(limit) <= 0:
        return []
    clauses = ["project_id = :p", "kind = :k"]
    params: dict[str, Any] = {"p": project_id, "k": kind, "lim": int(limit)}
    if query:
        clauses.append("(node_key ILIKE :q OR COALESCE(attrs->>'statement','') ILIKE :q)")
        params["q"] = f"%{query}%"
    rows = connection.execute(text(
        f"SELECT node_key, kind, attrs FROM kg_nodes WHERE {' AND '.join(clauses)} "
        "ORDER BY recorded_at DESC LIMIT :lim"
    ), params).mappings().all()
    return [{"node_key": r["node_key"], "kind": r["kind"], "attrs": r["attrs"] or {}} for r in rows]


def get_node(connection: Connection, project_id: str, *, node_key: str,
             kinds: Any = None) -> Optional[dict[str, Any]]:
    """One ``kg_nodes`` row by ``node_key``, but only if its ``kind`` is one the caller's
    contract grants (``kinds``) — otherwise ``None``, so a token cannot read outside its scope."""
    row = connection.execute(text(
        "SELECT node_key, kind, attrs FROM kg_nodes WHERE project_id = :p AND node_key = :nk"
    ), {"p": project_id, "nk": node_key}).mappings().first()
    if row is None:
        return None
    if kinds is not None and row["kind"] not in kinds:
        return None
    return {"node_key": row["node_key"], "kind": row["kind"], "attrs": row["attrs"] or {}}


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
