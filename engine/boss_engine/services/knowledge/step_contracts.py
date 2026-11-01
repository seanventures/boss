"""Step contracts (Build B, P1): what an agent-work step reads, must write, and calls.

A step without a `contract` key behaves exactly as before — every function here is a no-op on
an absent contract, so an already-published workflow is untouched by this module.

THE AGENT DESIGN (design §4.1, §4.6) rides on the same block, in two optional keys:

* ``steps`` — the agent's own numbered internal steps, as a consultant draws them:
  ``[{n: int, name: str, reads: [kind], writes: [kind], calls: [{mcp, tool}], explain: str}]``.
  ``n`` and ``name`` are required; each step's ``reads``/``writes``/``calls`` must be a SUBSET
  of the contract's own (a step cannot read what the agent as a whole may not). A step's call
  matches a contract call on ``mcp`` and, when both name one, ``tool``.
* ``design`` — the design record's cards: ``{status, reference, under_revision, decisions:
  [{id, text}], policy: [{id, text}], applied_in: {kg, cg, items: [str]}, open_questions:
  [{id, text}]}``. ``status`` is one of :data:`DESIGN_STATUSES`. The status a contract declares
  is the stage's design status until a person reviews it in BOSS (stage_designs.py), which then
  owns it.

Unknown keys are refused at every level, so a misspelt card never silently disappears.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Connection

from . import scopes

_CONTRACT_KEYS = {
    "trigger", "reads", "writes", "calls", "handoff", "human_review", "policy", "llm",
    "decisions", "open_questions", "steps", "design",
}
_READ_KEYS = {"kind", "via"}
_WRITE_KEYS = {"kind", "required", "decision_type"}
_CALL_KEYS = {"mcp", "tool"}
_STEP_KEYS = {"n", "name", "reads", "writes", "calls", "explain"}
_DESIGN_KEYS = {"status", "reference", "under_revision", "decisions", "policy", "applied_in",
                "open_questions"}
_ID_TEXT_KEYS = {"id", "text"}
_APPLIED_KEYS = {"kg", "cg", "items"}
#: A stage design's review state (design §4.6), in the consultant's legend's order.
DESIGN_STATUSES = ("locked", "in_review", "not_reviewed", "human_step", "shared_service")


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
class AgentStep:
    """One of the agent's numbered internal steps."""
    n: int
    name: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    calls: tuple[CallSpec, ...] = ()
    explain: str = ""


@dataclass(frozen=True)
class DesignNote:
    """A decision, policy or open question with its ID ("A-D30", "OP-ENR-01", "HP-Q1")."""
    id: str
    text: str


@dataclass(frozen=True)
class AgentDesign:
    status: str = ""
    reference: str = ""
    under_revision: str | None = None
    decisions: tuple[DesignNote, ...] = ()
    policy: tuple[DesignNote, ...] = ()
    applied_in: Mapping[str, Any] = field(default_factory=dict)
    open_questions: tuple[DesignNote, ...] = ()


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
    steps: tuple[AgentStep, ...] = ()
    design: AgentDesign | None = None


def _notes(value: Any) -> tuple[DesignNote, ...]:
    return tuple(DesignNote(id=str(v.get("id") or "").strip(), text=str(v.get("text") or "").strip())
                 for v in (value or []) if isinstance(v, Mapping))


def _parse_steps(raw: Any) -> tuple[AgentStep, ...]:
    steps = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        steps.append(AgentStep(
            n=int(item.get("n") or 0), name=str(item.get("name") or "").strip(),
            reads=tuple(str(k).strip() for k in (item.get("reads") or []) if str(k).strip()),
            writes=tuple(str(k).strip() for k in (item.get("writes") or []) if str(k).strip()),
            calls=tuple(CallSpec(mcp=str(c.get("mcp") or "").strip(),
                                 tool=(str(c["tool"]) if c.get("tool") else None))
                        for c in (item.get("calls") or []) if isinstance(c, Mapping)),
            explain=str(item.get("explain") or "").strip(),
        ))
    return tuple(sorted(steps, key=lambda s: s.n))


def _parse_design(raw: Any) -> AgentDesign | None:
    if not isinstance(raw, Mapping):
        return None
    applied = raw.get("applied_in") if isinstance(raw.get("applied_in"), Mapping) else {}
    revision = raw.get("under_revision")
    return AgentDesign(
        status=str(raw.get("status") or "").strip(),
        reference=str(raw.get("reference") or "").strip(),
        under_revision=(str(revision).strip() or None) if revision is not None else None,
        decisions=_notes(raw.get("decisions")),
        policy=_notes(raw.get("policy")),
        applied_in={
            **({"kg": str(applied["kg"])} if applied.get("kg") not in (None, "") else {}),
            **({"cg": str(applied["cg"])} if applied.get("cg") not in (None, "") else {}),
            "items": [str(i) for i in (applied.get("items") or []) if str(i).strip()],
        },
        open_questions=_notes(raw.get("open_questions")),
    )


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
        steps=_parse_steps(raw.get("steps")),
        design=_parse_design(raw.get("design")),
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
    problems.extend(_steps_problems(step_id, raw))
    problems.extend(_design_problems(step_id, raw.get("design")))
    return problems


def _kinds_of(entries: Any) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {str(e.get("kind") or "").strip() for e in entries if isinstance(e, Mapping)}


def _calls_of(entries: Any) -> list[tuple[str, str | None]]:
    if not isinstance(entries, list):
        return []
    return [(str(c.get("mcp") or "").strip(), str(c.get("tool") or "").strip() or None)
            for c in entries if isinstance(c, Mapping)]


def _steps_problems(step_id: str, raw: Mapping[str, Any]) -> list[str]:
    """`contract.steps`: numbered, named, and never reaching past the contract itself."""
    steps = raw.get("steps")
    if steps is None:
        return []
    where = f"step '{step_id}' contract.steps"
    if not isinstance(steps, list):
        return [f"{where} must be a list"]
    problems: list[str] = []
    allowed = {"reads": _kinds_of(raw.get("reads")), "writes": _kinds_of(raw.get("writes"))}
    calls = _calls_of(raw.get("calls"))
    seen: set[int] = set()
    for item in steps:
        if not isinstance(item, Mapping):
            problems.append(f"{where} entries must be mappings")
            continue
        bad = set(item) - _STEP_KEYS
        if bad:
            problems.append(f"{where} has unknown keys {sorted(bad)}")
        n = item.get("n")
        numbered = isinstance(n, int) and not isinstance(n, bool) and n >= 1
        if not numbered:
            problems.append(f"{where} entries need a number n of 1 or more, not {n!r}")
        elif n in seen:
            problems.append(f"{where} numbers step {n} twice")
        else:
            seen.add(n)
        label = f"{where} step {n}" if numbered else where
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            problems.append(f"{label} needs a name")
        if "explain" in item and not isinstance(item["explain"], str):
            problems.append(f"{label} explain must be text")
        for side in ("reads", "writes"):
            value = item.get(side)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(k, str) and k.strip() for k in value):
                problems.append(f"{label} {side} must be a list of kinds")
                continue
            extra = sorted({k.strip() for k in value} - allowed[side])
            if extra:
                problems.append(
                    f"{label} {side} {extra}, which the contract's own {side} do not declare")
        value = item.get("calls")
        if value is None:
            continue
        if not isinstance(value, list):
            problems.append(f"{label} calls must be a list")
            continue
        for call in value:
            if not isinstance(call, Mapping):
                problems.append(f"{label} calls entries must be mappings")
                continue
            bad = set(call) - _CALL_KEYS
            if bad:
                problems.append(f"{label} calls has unknown keys {sorted(bad)}")
            mcp = str(call.get("mcp") or "").strip()
            tool = str(call.get("tool") or "").strip() or None
            if not mcp:
                problems.append(f"{label} calls entries need an mcp name")
                continue
            if not any(m == mcp and (t is None or tool is None or t == tool) for m, t in calls):
                problems.append(
                    f"{label} calls {mcp}{'.' + tool if tool else ''}, which the contract's own "
                    "calls do not declare")
    return problems


def _design_problems(step_id: str, design: Any) -> list[str]:
    """`contract.design`: the review status and the cards, each note with its ID."""
    if design is None:
        return []
    where = f"step '{step_id}' contract.design"
    if not isinstance(design, Mapping):
        return [f"{where} must be a mapping"]
    problems: list[str] = []
    bad = set(design) - _DESIGN_KEYS
    if bad:
        problems.append(f"{where} has unknown keys {sorted(bad)}")
    status = design.get("status")
    if status is not None and status not in DESIGN_STATUSES:
        problems.append(f"{where}.status must be one of {list(DESIGN_STATUSES)}, not {status!r}")
    if "reference" in design and not isinstance(design["reference"], str):
        problems.append(f"{where}.reference must be text")
    if design.get("under_revision") is not None and not isinstance(design["under_revision"], str):
        problems.append(f"{where}.under_revision must be text or null")
    for key in ("decisions", "policy", "open_questions"):
        value = design.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            problems.append(f"{where}.{key} must be a list")
            continue
        for item in value:
            if not isinstance(item, Mapping):
                problems.append(f"{where}.{key} entries must be {{id, text}} mappings")
                continue
            extra = set(item) - _ID_TEXT_KEYS
            if extra:
                problems.append(f"{where}.{key} has unknown keys {sorted(extra)}")
            if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("id", "text")):
                problems.append(f"{where}.{key} entries need an id and a text")
    applied = design.get("applied_in")
    if applied is not None:
        if not isinstance(applied, Mapping):
            problems.append(f"{where}.applied_in must be a mapping")
        else:
            extra = set(applied) - _APPLIED_KEYS
            if extra:
                problems.append(f"{where}.applied_in has unknown keys {sorted(extra)}")
            for key in ("kg", "cg"):
                value = applied.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
                    problems.append(f"{where}.applied_in.{key} must be a version like \"v16\"")
            items = applied.get("items")
            if items is not None and (not isinstance(items, list)
                                      or not all(isinstance(i, str) for i in items)):
                problems.append(f"{where}.applied_in.items must be a list of text")
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
