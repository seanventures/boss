"""What each step of ONE workflow run actually read and wrote, from the graph itself (§4.7).

The Pipeline view's "Show the last run" overlays a step's declared contract with what the run
really did. Its first version used the step's input and output KEYS as "reads" and "writes",
which says nothing about the graph: a key named `subject_id` is not a read of a Clause. This
reads the real record instead:

* WRITES — Context Graph rows the engine wrote for this run and step (`context:<kind>:<run>:
  <step>…`, `precedent:<run>:<step>`, `remediation:<run>:<step>`, or `attrs.run_id/step_id`),
  plus the nodes a step's output names as what it opened (`compliance_case_ids`, `precedent_id`).
* READS — the nodes a step's output names as what it consulted (`collected`, `matched`,
  `cited_node_id`), the run's `kg_usage` rows for the step (consulted / cited / shown / reused),
  an outside agent's own `kg_find` calls under the run's token (agent_turns), the step's INPUT
  (the subject, document or case the run bound it to — its whole job is to read them), what a
  node it opened is about (a case's `clause_id`), and the stored Context Graph edges of its
  decision (`consulted` / `about`, context_graph.link_step_decisions).
* CALLS — tool calls recorded for the step: an agent's own (agent_turns), and the MCP calls a
  deterministic step records in its output (`calls: [{mcp, tool, ok}]`). A step with either
  record is marked `calls_recorded`, so a declared call it never made reads "declared, not
  done"; a step with no record at all still reads "not recorded", never "not done".

Read-only, one project. Nothing here is synthesised: a kind absent from the result was not
found in the record.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: An output key that names what a step CONSULTED (a read), checked before the write rule.
_READ_KEY = re.compile(r"cite|citation|collected|matched|consult|evidence|subject|source|read")
#: A node id as the store mints it.
_NODE_ID = re.compile(r"^kgn_[0-9a-f]{8,}$")
#: Text that would put a raw id on screen: never used as an example name.
_RAW_ID = re.compile(r"\b(kgn|wfr|wfd|wfv|act|dec|ext)_[0-9A-Za-z]{6,}")
_EXAMPLES = 3
#: A written node's attrs that name what it is about: read by the step that wrote it.
_SUBJECT_ATTRS = ("clause_id", "document_id", "regulation_id", "subject_id", "alert_id",
                  "incident_id")


def _node_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if _NODE_ID.match(value) else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and _NODE_ID.match(v)]
    return []


def _ids_in_output(output: Any) -> tuple[list[str], list[str]]:
    """(read ids, written ids) a step's output names, by what its keys say they are."""
    reads: list[str] = []
    writes: list[str] = []
    if not isinstance(output, dict):
        return reads, writes
    for key, value in output.items():
        ids = _node_ids(value)
        if not ids:
            continue
        name = str(key).lower()
        if _READ_KEY.search(name):
            reads.extend(ids)
        elif name.endswith("_id") or name.endswith("_ids"):
            writes.extend(ids)
    return reads, writes


def _step_of_key(node_key: str, run_id: str, step_ids: set[str]) -> Optional[str]:
    """The step a run-keyed node belongs to: `…:<run>:<step>[:…]`."""
    marker = f":{run_id}:"
    at = node_key.find(marker)
    if at < 0:
        return None
    step = node_key[at + len(marker):].split(":", 1)[0]
    return step if step in step_ids else None


class _Bucket:
    """Kinds seen for one step on one side (reads or writes): count, examples, decision types."""

    def __init__(self) -> None:
        self.kinds: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.seen: set[tuple[str, str]] = set()

    def add(self, kind: str, *, ref: str, name: str = "", decision_type: str = "",
            count: int = 1) -> None:
        if not kind or (kind, ref) in self.seen:
            return
        self.seen.add((kind, ref))
        entry = self.kinds.setdefault(kind, {"kind": kind, "count": 0, "examples": [],
                                             "decision_types": []})
        entry["count"] += max(1, int(count or 1))
        if decision_type and decision_type not in entry["decision_types"]:
            entry["decision_types"].append(decision_type)
        clean = name.strip()
        if (clean and not _RAW_ID.search(clean) and kind != "agent_decision"
                and clean not in entry["examples"] and len(entry["examples"]) < _EXAMPLES):
            entry["examples"].append(clean[:80])

    def out(self) -> list[dict[str, Any]]:
        return list(self.kinds.values())


def _nodes(connection: Connection, project_id: str, ids: Iterable[str]) -> dict[str, dict]:
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = connection.execute(text(
        "SELECT id, kind, name, attrs FROM kg_nodes WHERE project_id = :p AND id = ANY(:ids)"
    ), {"p": project_id, "ids": wanted}).mappings().all()
    return {str(r["id"]): dict(r) for r in rows}


def _decision_type(row: dict) -> str:
    attrs = row.get("attrs") or {}
    return str(attrs.get("decision_type") or "") if isinstance(attrs, dict) else ""


def run_trace(connection: Connection, project_id: str, run_id: str) -> Optional[dict[str, Any]]:
    """Per step of `run_id`: its last status and the graph kinds it read and wrote. None when
    the run is not this project's."""
    run = connection.execute(text(
        "SELECT r.id, r.status, r.created_at, r.completed_at, r.input, v.spec FROM workflow_runs r "
        "LEFT JOIN workflow_versions v ON v.id = r.workflow_version_id "
        "WHERE r.id = :r AND r.project_id = :p"
    ), {"r": run_id, "p": project_id}).mappings().first()
    if run is None:
        return None
    from .context_graph import bound_run_input

    run_input = run["input"] if isinstance(run["input"], dict) else {}
    templates = {str(s.get("id")): s for s in ((run["spec"] or {}).get("steps") or [])
                 if isinstance(s, dict)} if isinstance(run["spec"], dict) else {}

    step_rows = connection.execute(text(
        "SELECT step_id, step_kind, iteration, status, input, output FROM workflow_step_runs "
        "WHERE workflow_run_id = :r AND project_id = :p ORDER BY created_at ASC, iteration ASC"
    ), {"r": run_id, "p": project_id}).mappings().all()

    status: dict[str, str] = {}
    kinds: dict[str, str] = {}
    out_reads: dict[str, list[str]] = {}
    out_writes: dict[str, list[str]] = {}
    out_calls: dict[str, list[dict[str, Any]]] = {}
    for row in step_rows:
        step = str(row["step_id"])
        status[step] = str(row["status"] or "")  # the latest iteration wins
        kinds[step] = str(row["step_kind"] or "")
        output = row["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                output = {}
        if isinstance(output, dict) and isinstance(output.get("calls"), list):
            # The latest iteration's record wins, like its status.
            out_calls[step] = [c for c in output["calls"]
                               if isinstance(c, dict) and str(c.get("tool") or "")]
        r, w = _ids_in_output(output)
        out_reads.setdefault(step, []).extend(r)
        out_writes.setdefault(step, []).extend(w)
        # THE STEP'S INPUT IS A READ (§4.7 overlay fix): the subject, document or case the run
        # handed this step (its `from_input` bindings, and its own stored input) was read by it —
        # a clause intake's clause is not "declared, not done".
        for value in bound_run_input(templates.get(step) or {}, run_input).values():
            out_reads[step].extend(_node_ids(value))
        step_input = row.get("input") if isinstance(row.get("input"), dict) else {}
        for value in step_input.values():
            out_reads[step].extend(_node_ids(value))
    step_ids = set(status)

    reads = {s: _Bucket() for s in step_ids}
    writes = {s: _Bucket() for s in step_ids}
    calls: dict[str, dict[tuple[str, str], int]] = {s: {} for s in step_ids}
    recorded: set[str] = set(out_calls)
    for step, made in out_calls.items():
        for call in made:
            if call.get("ok") is False:
                continue  # asked for, but the server was absent or did not answer
            key = (str(call.get("mcp") or call.get("server") or ""), str(call["tool"]))
            calls[step][key] = calls[step].get(key, 0) + int(call.get("count") or 1)

    # WRITES the engine keyed to this run and step.
    written = connection.execute(text(
        "SELECT id, kind, name, node_key, attrs FROM kg_nodes WHERE project_id = :p "
        "AND (node_key LIKE :like OR attrs->>'run_id' = :r)"
    ), {"p": project_id, "r": run_id, "like": f"%:{run_id}:%"}).mappings().all()
    written_by: dict[str, set[str]] = {s: set() for s in step_ids}
    for row in written:
        attrs = row["attrs"] if isinstance(row["attrs"], dict) else {}
        step = str(attrs.get("step_id") or "") or _step_of_key(str(row["node_key"]), run_id,
                                                                step_ids) or ""
        if step in writes:
            writes[step].add(str(row["kind"]), ref=str(row["id"]), name=str(row["name"] or ""),
                             decision_type=_decision_type(dict(row)))
            written_by[step].add(str(row["id"]))
            # What a node it opened is ABOUT (a case's clause, an alert's subject) the step read.
            for key in _SUBJECT_ATTRS:
                out_reads.setdefault(step, []).extend(_node_ids(attrs.get(key)))
    # The nodes a step's output names as what it opened are its writes too.
    for step, ids in out_writes.items():
        written_by.setdefault(step, set()).update(ids)
    # The stored Context Graph edges of the run's decisions (context_graph.link_step_decisions):
    # what each step's decision consulted, cited and is about.
    for src_step, dst, kind in connection.execute(text(
        "SELECT d.attrs->>'step_id', e.dst_id, e.kind FROM kg_edges e "
        "JOIN kg_nodes d ON d.id = e.src_id AND d.project_id = e.project_id "
        "WHERE e.project_id = :p AND d.kind = 'agent_decision' AND d.attrs->>'run_id' = :r "
        "AND e.kind IN ('consulted', 'about') AND e.valid_to IS NULL"
    ), {"p": project_id, "r": run_id}).all():
        step = str(src_step or "")
        if step in step_ids and str(dst) not in written_by.get(step, set()):
            out_reads.setdefault(step, []).append(str(dst))

    # What the outputs name, resolved to kinds in one read.
    named = _nodes(connection, project_id,
                   [i for ids in out_reads.values() for i in ids]
                   + [i for ids in out_writes.values() for i in ids])
    for side, by_step in ((reads, out_reads), (writes, out_writes)):
        for step, ids in by_step.items():
            for node_id in ids:
                node = named.get(node_id)
                if node:
                    side[step].add(str(node["kind"]), ref=node_id, name=str(node["name"] or ""),
                                   decision_type=_decision_type(node))

    # READS the run recorded as usage (consulted / cited / shown / reused).
    usage = connection.execute(text(
        "SELECT u.step, u.fact_id, n.kind, n.name, n.attrs FROM kg_usage u "
        "JOIN kg_nodes n ON n.id = u.fact_id AND n.project_id = u.project_id "
        "WHERE u.project_id = :p AND (u.workflow_run_id = :r OR u.run_id = :r)"
    ), {"p": project_id, "r": run_id}).mappings().all()
    for row in usage:
        step = str(row["step"] or "")
        if step in reads:
            reads[step].add(str(row["kind"]), ref=str(row["fact_id"]),
                            name=str(row["name"] or ""), decision_type=_decision_type(dict(row)))

    # An outside agent's own reads and tool calls under the run's token.
    turns = connection.execute(text(
        "SELECT stage, tool_name, content FROM agent_turns WHERE project_id = :p "
        "AND code_task_run_id = :r AND direction = 'tool_call' ORDER BY turn_index ASC"
    ), {"p": project_id, "r": run_id}).mappings().all()
    for i, row in enumerate(turns):
        step = str(row["stage"] or "")
        tool = str(row["tool_name"] or "")
        if step not in step_ids or not tool:
            continue
        if tool in ("kg_find", "kg_get"):
            try:
                body = json.loads(row["content"] or "{}")
            except ValueError:
                body = {}
            kind = str((body or {}).get("kind") or "") if isinstance(body, dict) else ""
            if kind:
                reads[step].add(kind, ref=f"turn:{i}",
                                count=int((body or {}).get("count") or 1))
            continue
        if tool.startswith("kg_"):
            continue  # a graph write: already read back from kg_nodes above
        recorded.add(step)
        calls[step][("", tool)] = calls[step].get(("", tool), 0) + 1

    return {
        "run_id": str(run["id"]),
        "status": str(run["status"] or ""),
        "started_at": run["created_at"].isoformat() if run["created_at"] else None,
        "completed_at": run["completed_at"].isoformat() if run["completed_at"] else None,
        "steps": {
            step: {
                "status": status[step],
                "kind": kinds[step],
                "reads": reads[step].out(),
                "writes": writes[step].out(),
                "calls": [{"tool": t, "server": srv, "count": n}
                          for (srv, t), n in calls[step].items()],
                "calls_recorded": step in recorded,
            }
            for step in status
        },
    }


def role_names(connection: Connection, project_id: str,
               agents: Optional[list[dict[str, Any]]] = None) -> dict[str, dict[str, str]]:
    """Every role a step can run as, by key: its display name and callsign. The project's own
    roles first (an operator's rename wins), then every shipped role, so a stage names its
    agent even on a project that has not installed that role yet."""
    from ..agent_persona import _specpack_root, persona_of, shipped_role
    from ..step_agents import project_agents

    names: dict[str, dict[str, str]] = {}
    try:
        files = sorted((_specpack_root() / "roles").glob("*.yaml"))
    except OSError:
        files = []
    for path in files:
        role = shipped_role(path.stem)
        if role:
            names[role["role_key"]] = {
                "name": str(role.get("display_name") or ""),
                "callsign": str(persona_of(role).get("callsign") or ""),
            }
    for agent in agents if agents is not None else project_agents(connection, project_id):
        key = str(agent.get("role_key") or "")
        if key:
            names[key] = {"name": str(agent.get("name") or ""),
                          "callsign": str(agent.get("callsign") or "")}
    return names
