"""Step contracts (Build B, P1): what an agent-work step reads, must write, and calls.

A step without a `contract` key behaves exactly as before — every function here is a no-op on
an absent contract, so an already-published workflow is untouched by this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Connection

from . import scopes

_CONTRACT_KEYS = {
    "trigger", "reads", "writes", "calls", "handoff", "human_review", "policy", "llm",
    "decisions", "open_questions",
}
_READ_KEYS = {"kind", "via"}
_WRITE_KEYS = {"kind", "required", "decision_type"}
_CALL_KEYS = {"mcp", "tool"}


@dataclass(frozen=True)
class ReadSpec:
    kind: str
    via: str | None = None


@dataclass(frozen=True)
class WriteSpec:
    kind: str
    required: bool = False
    decision_type: str | None = None


@dataclass(frozen=True)
class CallSpec:
    mcp: str
    tool: str | None = None


@dataclass(frozen=True)
class StepContract:
    trigger: str = ""
    reads: tuple[ReadSpec, ...] = ()
    writes: tuple[WriteSpec, ...] = ()
    calls: tuple[CallSpec, ...] = ()
    handoff: Mapping[str, Any] | None = None
    human_review: str = ""
    policy: Mapping[str, Any] = field(default_factory=dict)
    llm: str = "agent"
    decisions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


def parse_contract(step_id: str, raw: Mapping[str, Any] | None) -> StepContract | None:
    """The step's `contract` block, parsed into a `StepContract`, or None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"step '{step_id}' contract must be a mapping")
    reads = tuple(
        ReadSpec(kind=str(r.get("kind") or "").strip(), via=(str(r["via"]) if r.get("via") else None))
        for r in (raw.get("reads") or [])
    )
    def _required(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"step '{step_id}' contract.writes.required must be true or false, not {value!r}")

    writes = tuple(
        WriteSpec(
            kind=str(w.get("kind") or "").strip(),
            required=_required(w.get("required", False)),
            decision_type=(str(w["decision_type"]) if w.get("decision_type") else None),
        )
        for w in (raw.get("writes") or [])
    )
    calls = tuple(
        CallSpec(mcp=str(c.get("mcp") or "").strip(), tool=(str(c["tool"]) if c.get("tool") else None))
        for c in (raw.get("calls") or [])
    )
    return StepContract(
        trigger=str(raw.get("trigger") or ""),
        reads=reads,
        writes=writes,
        calls=calls,
        handoff=raw.get("handoff") if isinstance(raw.get("handoff"), Mapping) else None,
        human_review=str(raw.get("human_review") or ""),
        policy=dict(raw.get("policy") or {}),
        llm=str(raw.get("llm") or "agent"),
        decisions=tuple(str(d) for d in (raw.get("decisions") or [])),
        open_questions=tuple(str(d) for d in (raw.get("open_questions") or [])),
    )


def contract_shape_problems(step: Mapping[str, Any]) -> list[str]:
    """What is wrong with a step's `contract` block that needs no project to see."""
    step_id = step.get("id", "?")
    raw = step.get("contract")
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        return [f"step '{step_id}' contract must be a mapping"]
    problems: list[str] = []
    unknown = set(raw) - _CONTRACT_KEYS
    if unknown:
        problems.append(f"step '{step_id}' contract has unknown keys {sorted(unknown)}")
    for entry, keys, label in (
        (raw.get("reads"), _READ_KEYS, "reads"),
        (raw.get("writes"), _WRITE_KEYS, "writes"),
        (raw.get("calls"), _CALL_KEYS, "calls"),
    ):
        if entry is None:
            continue
        if not isinstance(entry, list):
            problems.append(f"step '{step_id}' contract.{label} must be a list")
            continue
        for item in entry:
            if not isinstance(item, Mapping):
                problems.append(f"step '{step_id}' contract.{label} entries must be mappings")
                continue
            bad = set(item) - keys
            if bad:
                problems.append(f"step '{step_id}' contract.{label} has unknown keys {sorted(bad)}")
    if raw.get("reads") and isinstance(raw.get("reads"), list):
        for item in raw["reads"]:
            if isinstance(item, Mapping) and not str(item.get("kind") or "").strip():
                problems.append(f"step '{step_id}' contract.reads entries need a kind")
    if raw.get("writes") and isinstance(raw.get("writes"), list):
        for item in raw["writes"]:
            if not isinstance(item, Mapping):
                continue
            if not str(item.get("kind") or "").strip():
                problems.append(f"step '{step_id}' contract.writes entries need a kind")
            if "required" in item and not isinstance(item["required"], bool):
                problems.append(
                    f"step '{step_id}' contract.writes.required must be true or false, "
                    f"not {item['required']!r}")
    if raw.get("calls") and isinstance(raw.get("calls"), list):
        for item in raw["calls"]:
            if isinstance(item, Mapping) and not str(item.get("mcp") or "").strip():
                problems.append(f"step '{step_id}' contract.calls entries need an mcp name")
    return problems


def contract_assignment_problems(connection: Connection, project_id: str,
                                  step: Mapping[str, Any]) -> list[str]:
    """Whether a step's contract names kinds the project's ontology grants, and MCP servers it
    has registered — a fact about the project, checked at publish/save time."""
    raw = step.get("contract")
    if raw is None or not isinstance(raw, Mapping):
        return []
    step_id = step.get("id", "?")
    from .ontology_packs import effective_node_kinds
    from ..step_agents import project_agents, resolve_mcp

    problems: list[str] = []
    granted = effective_node_kinds(connection, project_id=project_id)
    for item in (raw.get("reads") or []):
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind and kind not in granted:
            problems.append(
                f"step '{step_id}' contract reads kind '{kind}', which is neither a core kind "
                f"nor granted by the project's ontology pack")
    for item in (raw.get("writes") or []):
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind and kind not in granted:
            problems.append(
                f"step '{step_id}' contract writes kind '{kind}', which is neither a core kind "
                f"nor granted by the project's ontology pack")
    wanted = [str(c.get("mcp") or "").strip() for c in (raw.get("calls") or [])
              if isinstance(c, Mapping) and str(c.get("mcp") or "").strip()]
    if wanted:
        found, missing = resolve_mcp(connection, project_id, wanted)
        for name in missing:
            problems.append(f"step '{step_id}' contract calls MCP server '{name}', which this project has not registered")
        captain = str(step.get("captain") or "").strip()
        if captain and found:
            agents = project_agents(connection, project_id)
            agent = next((a for a in agents if a.get("role_key") == captain), None)
            if agent is not None:
                held = {str(n).lower() for n in agent.get("mcp") or []} | {
                    str(i) for i in agent.get("mcp_ids") or []}
                for server in found:
                    if (str(server.get("name") or "").lower() not in held
                            and str(server.get("id") or "") not in held):
                        problems.append(
                            f"step '{step_id}' contract calls MCP server "
                            f"'{server.get('name') or server.get('id')}', which agent '{captain}' "
                            "does not hold — grant it in Agent Studio, or name an agent that does.")
    return problems


def policy_from_contract(base: scopes.StagePolicy, contract: StepContract) -> scopes.StagePolicy:
    """The step's actual `StagePolicy`: `contract.reads` set the layers AND narrow to exactly
    those kinds, `contract.policy` overrides the bounds — closing the gap where a step's
    contract never reached the policy, and where a declared layer silently granted every kind
    inside it rather than only the kinds named."""
    from dataclasses import replace

    kinds = frozenset(r.kind for r in contract.reads) if contract.reads else None
    layers = frozenset(scopes.layer_of(r.kind) for r in contract.reads) if contract.reads else base.layers
    if contract.writes:
        # A contract that declares writes grants the Context Graph layer they land in — without
        # this, a token minted straight from `policy_from_contract` (as the external-agent
        # runner does) could never reach `kg_write_agent_decision`/`kg_write_coverage_gap`.
        layers = layers | {"context"}
    max_hops = contract.policy.get("max_hops", base.max_hops)
    max_nodes = contract.policy.get("list_cap", contract.policy.get("max_nodes", base.max_nodes))
    # A declared code-layer read is honoured only if the base stage can reach code at all
    # (it is seeded): a contract names KINDS, never new seeds, so it can narrow an already-seeded
    # stage's code reach but never grant code to a stage whose stage kind has none.
    code = base.code if "code" in layers and base.code == "seeded" else "none"
    if code == "none":
        layers = layers - {"code"}
    return replace(base, layers=layers, max_hops=max_hops, max_nodes=max_nodes, code=code, kinds=kinds)


def required_write_kinds(contract: StepContract) -> frozenset[str]:
    return frozenset(w.kind for w in contract.writes if w.required)


def unsatisfied_required(contract: StepContract, produced: Mapping[str, Any]) -> list[str]:
    """The required write kinds a step's `produced` output does NOT satisfy — kind AND, when the
    contract names one, `decision_type`. Shared by the synchronous `enforce_writes` and the
    workflow runtime's requeue path so both judge "did the required write land" the same way."""
    produced = produced or {}
    missing = [spec.kind for spec in _required_write_specs(contract) if not _satisfied(spec, produced)]
    # Stable, de-duplicated: a kind named by two required specs reports once.
    seen: dict[str, None] = {}
    for kind in missing:
        seen.setdefault(kind, None)
    return list(seen)


def _required_write_specs(contract: StepContract) -> tuple[WriteSpec, ...]:
    return tuple(w for w in contract.writes if w.required)


def _satisfied(spec: WriteSpec, produced: Mapping[str, Any]) -> bool:
    """A required write is satisfied by its kind AND, when the contract names one, its
    decision_type — a wrong-typed AgentDecision must not satisfy a different required decision."""
    if spec.kind not in produced:
        return False
    if spec.decision_type is None:
        return True
    entry = produced[spec.kind]
    produced_type = entry.get("decision_type") if isinstance(entry, Mapping) else None
    return produced_type in decision_types(spec)


def decision_types(spec: WriteSpec) -> tuple[str, ...]:
    """The decision types a write accepts. `Confirmed|RuledOut|CannotVerify` names a step whose
    one decision is any of several outcomes (a lane's verdict, a gate's answer); a single name
    is the common case. Empty when the contract names none."""
    if not spec.decision_type:
        return ()
    return tuple(t.strip() for t in spec.decision_type.split("|") if t.strip())


class ContractWriteError(Exception):
    """A required write never landed, even after the one repair round."""


def enforce_writes(connection: Connection, project_id: str, *, step: Mapping[str, Any],
                    contract: StepContract, outcome: Mapping[str, Any], run_again) -> dict[str, Any]:
    """After the agent's turn: every required write must have landed in `outcome['produced']`.

    Missing one triggers exactly one repair round (`run_again`, called with an instruction
    naming the missing kind); still missing after that, the step fails loudly rather than
    completing on chat text alone (the AG-1 miss).
    """
    step_id = step.get("id", "?")
    specs = _required_write_specs(contract)
    if not specs:
        return dict(outcome)

    def missing_specs(produced: Mapping[str, Any]) -> tuple[WriteSpec, ...]:
        return tuple(spec for spec in specs if not _satisfied(spec, produced))

    produced = dict(outcome.get("produced") or {})
    missing = missing_specs(produced)
    if not missing:
        return dict(outcome)

    missing_kinds = sorted({spec.kind for spec in missing})
    extra_instruction = (
        f"step '{step_id}' is missing required write(s) {missing_kinds}; produce them before finishing.")
    repaired = run_again(extra_instruction) or {}
    # The repair round's output is MERGED with the first round's, not swapped in for it: a step
    # that produced one required kind on the first pass and a different one on repair must not
    # fail for having "lost" the first.
    produced.update(repaired.get("produced") or {})
    outcome = {**outcome, **repaired, "produced": produced}
    missing = missing_specs(produced)
    if missing:
        raise ContractWriteError(
            f"step '{step_id}' never produced required write(s) {sorted({s.kind for s in missing})} "
            "after one repair round")
    return dict(outcome)
