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
  and an outside agent's own `kg_find` calls under the run's token (agent_turns).
* CALLS — tool calls recorded for the step (agent_turns); a deterministic MCP call that leaves
  no turn is not reported, and the console says so rather than calling it "not done".

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
        "SELECT id, status, created_at, completed_at FROM workflow_runs "
        "WHERE id = :r AND project_id = :p"
    ), {"r": run_id, "p": project_id}).mappings().first()
    if run is None:
        return None

    step_rows = connection.execute(text(
        "SELECT step_id, step_kind, iteration, status, output FROM workflow_step_runs "
        "WHERE workflow_run_id = :r AND project_id = :p ORDER BY created_at ASC, iteration ASC"
    ), {"r": run_id, "p": project_id}).mappings().all()

    status: dict[str, str] = {}
    kinds: dict[str, str] = {}
    out_reads: dict[str, list[str]] = {}
    out_writes: dict[str, list[str]] = {}
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
        r, w = _ids_in_output(output)
        out_reads.setdefault(step, []).extend(r)
        out_writes.setdefault(step, []).extend(w)
    step_ids = set(status)

    reads = {s: _Bucket() for s in step_ids}
    writes = {s: _Bucket() for s in step_ids}
    calls: dict[str, dict[str, int]] = {s: {} for s in step_ids}

    # WRITES the engine keyed to this run and step.
    written = connection.execute(text(
        "SELECT id, kind, name, node_key, attrs FROM kg_nodes WHERE project_id = :p "
        "AND (node_key LIKE :like OR attrs->>'run_id' = :r)"
    ), {"p": project_id, "r": run_id, "like": f"%:{run_id}:%"}).mappings().all()
    for row in written:
        attrs = row["attrs"] if isinstance(row["attrs"], dict) else {}
        step = str(attrs.get("step_id") or "") or _step_of_key(str(row["node_key"]), run_id,
                                                                step_ids) or ""
        if step in writes:
            writes[step].add(str(row["kind"]), ref=str(row["id"]), name=str(row["name"] or ""),
                             decision_type=_decision_type(dict(row)))

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
        calls[step][tool] = calls[step].get(tool, 0) + 1

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
                "calls": [{"tool": t, "count": n} for t, n in calls[step].items()],
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
