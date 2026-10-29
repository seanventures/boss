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
    writes = tuple(
        WriteSpec(
            kind=str(w.get("kind") or "").strip(),
            required=bool(w.get("required", False)),
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
            if isinstance(item, Mapping) and not str(item.get("kind") or "").strip():
                problems.append(f"step '{step_id}' contract.writes entries need a kind")
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
    from ..step_agents import resolve_mcp

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
        _, missing = resolve_mcp(connection, project_id, wanted)
        for name in missing:
            problems.append(f"step '{step_id}' contract calls MCP server '{name}', which this project has not registered")
    return problems


def policy_from_contract(base: scopes.StagePolicy, contract: StepContract) -> scopes.StagePolicy:
    """The step's actual `StagePolicy`: `contract.reads` set the layers, `contract.policy`
    overrides the bounds — closing the gap where a step's contract never reached the policy."""
    from dataclasses import replace

    if contract.reads:
        layers = frozenset(scopes.layer_of(r.kind) for r in contract.reads)
        kinds = frozenset(r.kind for r in contract.reads)
    else:
        layers = base.layers
        kinds = None
    max_hops = contract.policy.get("max_hops", base.max_hops)
    max_nodes = contract.policy.get("list_cap", contract.policy.get("max_nodes", base.max_nodes))
    code = "seeded" if "code" in layers else "none"
    return replace(base, layers=layers, max_hops=max_hops, max_nodes=max_nodes, code=code, kinds=kinds)


def required_write_kinds(contract: StepContract) -> frozenset[str]:
    return frozenset(w.kind for w in contract.writes if w.required)


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
    required = required_write_kinds(contract)
    if not required:
        return dict(outcome)

    def missing_kinds(result: Mapping[str, Any]) -> frozenset[str]:
        produced = result.get("produced") or {}
        return required - frozenset(produced.keys())

    missing = missing_kinds(outcome)
    if not missing:
        return dict(outcome)

    extra_instruction = (
        f"step '{step_id}' is missing required write(s) {sorted(missing)}; produce them before finishing.")
    outcome = run_again(extra_instruction) or {}
    missing = missing_kinds(outcome)
    if missing:
        raise ContractWriteError(
            f"step '{step_id}' never produced required write(s) {sorted(missing)} after one repair round")
    return dict(outcome)
