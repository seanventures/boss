"""Stage knowledge scopes: which layers of the graph a stage may read, enforced where data is read.

The code graph is large, so a stage's reach is declared, not left to the prompt (operator,
2026-10-21; the earlier stack's PHASE3 "Next" item 5). Every node is in one layer, by kind
(``kg_nodes.knowledge_layer`` is generated from ``kind``):

* ``code``: files, modules, symbols, endpoints, tables, queues, config keys;
* ``lifecycle``: specs, pull requests, decisions, review findings, releases, runs, people;
* ``ops``: services, resources, deployments, incidents, root causes, runbooks, alert rules;
* ``knowledge``: distilled facts and topics.

A :class:`StagePolicy` says which layers a stage reads and how it may reach code:

* ``none``: no code at all (triage, decide);
* ``seeded``: only code reached from the seeds the stage was given (an incident's evidence, the
  plan's or the diff's symbols), at most ``max_hops`` away and ``max_nodes`` in all. Without
  seeds, no code: never a whole-graph scan.

The policy travels with the reader into ``PostgresKnowledgeStore``; every read adds the layer
filter, and a code read outside the policy is refused in words. Escalation is a step change (move
the run to Investigate), never a wider scope inside a step.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

LAYERS = ("code", "lifecycle", "ops", "knowledge")

LAYER_KINDS: dict[str, frozenset[str]] = {
    "code": frozenset({"file", "module", "symbol", "endpoint", "table", "queue", "config_key"}),
    "lifecycle": frozenset({"spec", "decision", "pull_request", "review_finding", "release", "run",
                            "person", "agent", "team"}),
    "ops": frozenset({"service", "resource", "repo", "deployment", "datastore", "external", "incident",
                      "root_cause", "runbook", "alert_rule"}),
    "knowledge": frozenset({"fact", "topic"}),
}


def layer_of(kind: str) -> str:
    """The layer of a node kind. A kind no layer names is knowledge, as the generated column says."""
    for layer, kinds in LAYER_KINDS.items():
        if kind in kinds:
            return layer
    return "knowledge"


def layer_sql_case(column: str = "kind") -> str:
    """The CASE expression behind kg_nodes.knowledge_layer (kept in step with db/schema.sql)."""
    parts = []
    for layer in ("code", "lifecycle", "ops"):
        kinds = ", ".join(f"'{k}'" for k in sorted(LAYER_KINDS[layer]))
        parts.append(f"WHEN {column} IN ({kinds}) THEN '{layer}'")
    return "CASE " + " ".join(parts) + " ELSE 'knowledge' END"


@dataclass(frozen=True)
class StagePolicy:
    stage: str
    layers: frozenset[str]
    code: str = "none"                      # none | seeded
    max_hops: int = 0
    max_nodes: int = 0
    budget_tokens: int = 1200
    tools: frozenset[str] | None = None  # None: every kg tool whose layers the stage reads
    seeds: tuple[str, ...] = field(default=())  # node ids the stage was given

    def __post_init__(self) -> None:
        unknown = set(self.layers) - set(LAYERS)
        if unknown:
            raise ValueError(f"unknown knowledge layers {sorted(unknown)}")
        if self.code not in ("none", "seeded"):
            raise ValueError("code access is none or seeded")
        if self.code == "seeded" and "code" not in self.layers:
            raise ValueError("seeded code access needs the code layer")
        if self.code == "none" and "code" in self.layers:
            object.__setattr__(self, "layers", frozenset(self.layers - {"code"}))
        if self.max_hops < 0 or self.max_nodes < 0 or self.budget_tokens < 0:
            raise ValueError("knowledge bounds must be >= 0")

    @property
    def reads_code(self) -> bool:
        return "code" in self.layers and self.code == "seeded"

    def with_seeds(self, seeds) -> StagePolicy:
        return replace(self, seeds=tuple(dict.fromkeys(str(s) for s in seeds or () if str(s).strip())))

    def refusal(self) -> str:
        opens = "Investigate" if self.stage in ("triage", "decide") else "a step that declares the code layer"
        return f"Code isn't available while {_gerund(self.stage)}; it opens in {opens}."

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "layers": sorted(self.layers), "code": self.code,
                "max_hops": self.max_hops, "max_nodes": self.max_nodes,
                "budget_tokens": self.budget_tokens,
                "tools": sorted(self.tools) if self.tools is not None else None}


def _gerund(stage: str) -> str:
    return {"triage": "triaging", "decide": "deciding", "investigate": "investigating",
            "implement": "implementing", "review": "reviewing"}.get(stage, f"in {stage}")


#: The defaults per stage kind. A workflow step may override any of these in its definition
#: (``knowledge: {layers, tools, budget_tokens, code_seed, max_hops, max_nodes}``).
DEFAULTS: dict[str, StagePolicy] = {
    "triage": StagePolicy("triage", frozenset({"ops", "lifecycle", "knowledge"})),
    "investigate": StagePolicy("investigate", frozenset({"ops", "lifecycle", "knowledge", "code"}),
                               code="seeded", max_hops=2, max_nodes=200),
    "implement": StagePolicy("implement", frozenset({"code", "lifecycle", "knowledge"}),
                             code="seeded", max_hops=2, max_nodes=500),
    "review": StagePolicy("review", frozenset({"code", "lifecycle", "knowledge"}),
                          code="seeded", max_hops=1, max_nodes=300),
    "decide": StagePolicy("decide", frozenset({"lifecycle", "knowledge", "ops"})),
}

#: The stage names BOSS's pipelines and workflow steps use, by the kind whose default they get.
STAGE_KINDS: dict[str, str] = {
    **{name: "triage" for name in ("triage", "alert", "alert_triage", "beacon")},
    **{name: "investigate" for name in ("investigate", "rca", "root_cause", "incident", "diagnose")},
    **{name: "implement" for name in ("implement", "design", "plan", "spec", "write_tests", "solo",
                                      "regression", "research", "build")},
    **{name: "review" for name in ("review", "review_code", "review_plan", "security_check",
                                   "consistency_check", "quality", "standards", "check_tests")},
    **{name: "decide" for name in ("decide", "jev", "typed_decision", "decision", "approve")},
}

#: Only when no stage is known: what a role usually does.
ROLE_FALLBACK: dict[str, str] = {
    "sre": "investigate", "operations_engineer": "investigate",
    "software_engineer": "implement", "backend_engineer": "implement", "frontend_engineer": "implement",
    "adversarial_reviewer": "review", "security_reviewer": "review", "design_critic": "review", "qa": "review",
    "jev": "decide", "metatron": "decide",
}

#: Which kg tools read which layers. A tool outside the stage's layers is not offered.
TOOL_LAYERS: dict[str, frozenset[str]] = {
    "kg_symbol": frozenset({"code"}),
    "kg_callers": frozenset({"code"}),
    "kg_blast_radius": frozenset({"code"}),
    "kg_search": frozenset(LAYERS),
    "kg_why": frozenset(LAYERS),
    "kg_precedent": frozenset({"lifecycle", "knowledge"}),
    "kg_propose_fact": frozenset({"knowledge"}),
    "kg_cypher": frozenset(LAYERS),          # the query itself is checked against the scope (age.py)
}

_DECLARED_KEYS = {"layers", "tools", "budget_tokens", "code_seed", "max_hops", "max_nodes"}


def policy_for(
    *,
    stage: str | None = None,
    step: Mapping[str, Any] | None = None,
    role: str | None = None,
) -> StagePolicy | None:
    """The policy for a step, else a stage name, else (only when no stage is named) a role. None
    when none is known: a person, BOSS itself, or a stage with no default reads unscoped."""
    kind = None
    if step is not None:
        kind = STAGE_KINDS.get(str(step.get("stage") or step.get("kind") or "").strip().lower())
    if kind is None and stage:
        kind = STAGE_KINDS.get(str(stage).strip().lower())
    if kind is None and role and not stage and step is None:
        # Only when nothing names the stage: a named stage BOSS has no default for (a chat turn,
        # "converse") reads unscoped, as before, rather than as whatever the role usually does.
        kind = ROLE_FALLBACK.get(str(role).strip().lower())
    declared = (step or {}).get("knowledge") if step is not None else None
    if kind is None and not declared:
        return None
    base = DEFAULTS[kind] if kind else StagePolicy("custom", frozenset({"lifecycle", "knowledge", "ops"}))
    return apply_declaration(base, declared) if declared else base


def apply_declaration(base: StagePolicy, declared: Mapping[str, Any]) -> StagePolicy:
    """A step's ``knowledge:`` block over its kind's default. Unknown keys are refused."""
    if not isinstance(declared, Mapping):
        raise ValueError("a step's knowledge declaration must be a mapping")
    unknown = set(declared) - _DECLARED_KEYS
    if unknown:
        raise ValueError(f"unknown knowledge keys {sorted(unknown)}")
    changes: dict[str, Any] = {}
    if "layers" in declared:
        changes["layers"] = frozenset(str(x) for x in declared["layers"] or ())
    if "tools" in declared:
        changes["tools"] = None if declared["tools"] is None else frozenset(str(x) for x in declared["tools"])
    for key in ("budget_tokens", "max_hops", "max_nodes"):
        if key in declared:
            value = declared[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            changes[key] = value
    if "code_seed" in declared:
        seed = str(declared["code_seed"] or "none")
        changes["code"] = "none" if seed == "none" else "seeded"
    layers = changes.get("layers", base.layers)
    if "code" not in changes:
        changes["code"] = base.code if "code" in layers else "none"
    if changes["code"] == "seeded" and "code" not in layers:
        changes["code"] = "none"
    return replace(base, **changes)


#: Keyword search only (the knowledge_graph switch's ``keyword``): the layer it never reads. Learned
#: facts and topics are what the graph distilled; the A′ arm has plain records only.
KEYWORD_EXCLUDED_LAYERS = ("knowledge",)

#: The one kg tool keyword search offers. Every other tool walks the graph (callers, blast radius,
#: why, Cypher), looks up learned facts (precedent) or teaches the graph (propose).
KEYWORD_TOOLS = frozenset({"kg_search"})


def keyword_policy(policy: StagePolicy | None) -> StagePolicy | None:
    """A stage's scope under keyword search: its layers minus learned knowledge, and code reached
    from its seeds by zero hops (the seeds themselves), so nothing is found by traversal."""
    if policy is None:
        return None
    return replace(policy, layers=frozenset(policy.layers - set(KEYWORD_EXCLUDED_LAYERS)), max_hops=0)


def offers_tool(policy: StagePolicy | None, tool: str) -> bool:
    if policy is None:
        return True
    if policy.tools is not None and tool not in policy.tools:
        return False
    needs = TOOL_LAYERS.get(tool, frozenset(LAYERS))
    if needs == frozenset({"code"}):
        return policy.reads_code
    return bool(needs & policy.layers)


def current_policy() -> StagePolicy | None:
    """The stage scope of the LLM call in progress (``set_llm_context``), if any."""
    try:
        from ..llm_telemetry import get_llm_context
    except Exception:
        return None
    context = get_llm_context() or {}
    declared = context.get("knowledge_policy")
    if isinstance(declared, StagePolicy):
        return declared
    policy = policy_for(stage=context.get("stage"), role=context.get("role_key"))
    seeds = context.get("knowledge_seeds") or ()
    return policy.with_seeds(seeds) if policy is not None and seeds else policy
