"""`boss.agent/v1`: ONE versioned definition of an agent, and the compile step the runtime reads.

WHY (docs/design/AGENT_PLATFORM.md §2.1, §4.1)
---------------------------------------------
An agent used to be a `roles` row plus holdings, with a second catalogue (Agent Studio's
`agent_definitions`/`agent_versions`) that carried prompts, a model policy and limits nobody ran.
This module is the definition both collapse into. A document is validated here (unknown fields
are refused, by path, with the fields that ARE allowed), turned into a frozen `AgentSpec`, and
compiled into a `CompiledAgent`: the grants, the roles-row projection and the work prompt.

PURE. Nothing here touches a database: `versions.py` stores and publishes, `migration.py`
converts the shipped YAMLs. The kernel (P1b) builds its RunSpec from `CompiledAgent` alone.

THE SHAPE
---------
    apiVersion: boss.agent/v1
    kind: Agent
    metadata: {key, name, description, team, reports_to, sponsor, version, labels}
    spec:
      persona            how it speaks (linted: persona never grants)
      mission, outcomes, boundaries {must, must_not}, decision_rights {can_decide, must_escalate}
      prompts {system, job}, instructions [..]          the role prompt
      model {tier: fast|balanced|frontier, need: [..], decision_model: auto|jev}   NEVER a model id
      tools {skills, mcp [{server, tools}], native, exposure, forbidden, abilities,
             recommended_abilities, integrations}
      autonomy {level 0-4, approval {action: mode}, escalate [..]}
      memory {read, write}                               namespaces
      context {..}       P6 placeholder (context policy hooks)
      subagents [..]     P7 placeholder
      triggers [..]      P3 placeholder
      evaluators [..]    P4 placeholder (a shipped eval rubric lands here as kind: rubric)
      output_contract {artifacts, artifact_type, sections, schema, strategy}
      limits {max_steps, max_calls, timeout_seconds, max_retries, run_usd, thread_usd}
      cadence, comms {tone, required_references}, a2a {publish, subscribe}, scopes

Governance (authz, approval, budget, taint, ledger) is implicit and cannot be switched off by a
spec: there is no field for it, so a document that tries is refused as an unknown field.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

API_VERSION = "boss.agent/v1"
KIND = "Agent"

#: Model tiers (services/model_router/tiers.py). A spec names what the work NEEDS, never a model.
TIERS: tuple[str, ...] = ("fast", "balanced", "frontier")
DECISION_MODELS: tuple[str, ...] = ("auto", "jev")
EXPOSURES: tuple[str, ...] = ("scoped", "full")
APPROVAL_MODES: tuple[str, ...] = ("approve", "approve_edit_reject", "notify")
MEMORY_ACCESS: tuple[str, ...] = ("read", "write")

_KEY = r"^[a-z][a-z0-9_]{1,63}$"
_WORD = r"^[a-z][a-z0-9_]*$"
_NEED = r"^[a-z][a-z0-9_\-]*$"
#: A need that is really a model name is refused: a spec asks for a tier and capabilities.
_MODEL_NAMES = re.compile(r"^(claude|gpt|gemini|llama|mistral|mixtral|qwen|deepseek|grok|sonnet|opus|haiku|o[0-9])", re.I)
_SKILL = r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]*\*?$"
_NAMESPACE = r"^[a-z][a-z0-9_]*(:[A-Za-z0-9_.\-()*]+)*$"


class SpecError(ValueError):
    """A document that is not a valid `boss.agent/v1`. `errors` lists every problem as
    {path, message}; str() reads as one operator-facing sentence per problem."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors) or "invalid agent spec")


# ── The schema: one dict, used by the validator below AND published as JSON Schema ─────────────

def _s(**kw: Any) -> dict[str, Any]:
    return {"type": "string", **kw}


def _strs(**item: Any) -> dict[str, Any]:
    return {"type": "array", "items": _s(**item)}


def _obj(props: dict[str, Any], required: tuple[str, ...] = (), extra: Any = False) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": extra}
    if required:
        out["required"] = list(required)
    return out


_ANY_OBJ: dict[str, Any] = {"type": "object"}
_TEXT_MAP = {"type": "object", "additionalProperties": {"type": ["string", "array", "object", "number",
                                                                 "boolean", "null"]}}

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://boss.dev/schemas/boss.agent.v1.json",
    "title": "BOSS agent definition (boss.agent/v1)",
    **_obj({
        "apiVersion": {"const": API_VERSION},
        "kind": {"const": KIND},
        "metadata": _obj({
            "key": _s(pattern=_KEY),
            "name": _s(maxLength=120),
            "description": _s(maxLength=2000),
            "team": {"type": ["string", "null"]},
            "reports_to": {"type": ["string", "null"]},
            "sponsor": {"type": ["string", "null"]},
            "version": {"type": ["integer", "null"], "minimum": 1},
            "labels": {"type": "object", "additionalProperties": _s()},
            # A PROFILE (a shipped Studio agent such as `cfo`) runs as a role it extends and
            # never projects onto a roles row of its own.
            "extends": _s(pattern=_KEY),
        }, required=("key",)),
        "spec": _obj({
            "persona": {"type": ["object", "null"]},
            "mission": _s(maxLength=4000),
            "outcomes": _strs(),
            "boundaries": _obj({"must": _strs(), "must_not": _strs()}),
            "decision_rights": _obj({"can_decide": _strs(), "must_escalate": _strs()}),
            "prompts": _obj({"system": _s(maxLength=20000), "job": _s(maxLength=20000)}),
            "instructions": _strs(),
            "model": _obj({
                "tier": {"enum": list(TIERS)},
                "need": _strs(pattern=_NEED),
                "decision_model": {"enum": list(DECISION_MODELS)},
            }),
            "tools": _obj({
                "skills": _strs(pattern=_SKILL),
                "mcp": {"type": "array", "items": _obj({
                    "server": _s(minLength=1), "tools": _strs()}, required=("server",))},
                "native": _strs(pattern=_SKILL),
                "exposure": {"enum": list(EXPOSURES)},
                "forbidden": _strs(pattern=_SKILL),
                "abilities": _strs(pattern=_SKILL),
                "recommended_abilities": _strs(pattern=_SKILL),
                "integrations": _strs(),
            }),
            "autonomy": _obj({
                "level": {"type": "integer", "minimum": 0, "maximum": 4},
                "approval": {"type": "object", "additionalProperties": {"enum": list(APPROVAL_MODES)}},
                "escalate": _strs(),
                "upgrade_requires": _strs(),
                "downgrade_triggers": _strs(),
            }),
            "memory": _obj({"read": _strs(pattern=_NAMESPACE), "write": _strs(pattern=_NAMESPACE)}),
            # P6: context policy hooks. The keys are named now so a spec written today still loads.
            "context": _obj({
                "offload_tokens": {"type": "integer", "minimum": 0},
                "summarise_at": {"type": "number", "minimum": 0, "maximum": 1},
                "keep": {"type": "number", "minimum": 0, "maximum": 1},
                "hooks": _strs(pattern=_WORD),
            }),
            # P7: subagents-as-tools.
            "subagents": {"type": "array", "items": _obj({
                "ref": _s(minLength=1), "mode": {"enum": ["isolated", "shared"]},
                "report_tokens": {"type": "integer", "minimum": 0},
                "budget_share": {"type": "number", "minimum": 0, "maximum": 1},
            }, required=("ref",))},
            # P3: what makes an agent a bot.
            "triggers": {"type": "array", "items": _obj({
                "cron": _s(), "event": _ANY_OBJ, "webhook": _ANY_OBJ, "input": _ANY_OBJ,
                "concurrency": _s(pattern=_WORD),
            })},
            # P4: evaluators. A shipped `eval_rubric` arrives as {kind: rubric, criteria}.
            "evaluators": {"type": "array", "items": _obj({
                "kind": {"enum": ["rubric", "online", "dataset"]}, "name": _s(),
                "criteria": _TEXT_MAP, "dataset": _s(),
                "sample": {"type": "number", "minimum": 0, "maximum": 1},
                "weekly_usd_cap": {"type": "number", "minimum": 0},
            }, required=("kind",))},
            "output_contract": _obj({
                "artifacts": _strs(), "artifact_type": _s(), "sections": _strs(), "schema": _ANY_OBJ,
                "strategy": {"enum": ["provider_then_tool", "tool", "provider", "text"]},
            }),
            "limits": _obj({
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 1000},
                "max_calls": {"type": "integer", "minimum": 1, "maximum": 10000},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                "max_retries": {"type": "integer", "minimum": 0, "maximum": 10},
                "run_usd": {"type": "number", "minimum": 0},
                "thread_usd": {"type": "number", "minimum": 0},
            }),
            "cadence": {"type": "object", "additionalProperties": _s()},
            "comms": _obj({"tone": _s(), "required_references": _strs(), "slack_prefix": _s()}),
            "a2a": _obj({"publish": _strs(), "subscribe": _strs()}),
            "scopes": {"type": "object", "additionalProperties": _s()},
        }),
    }, required=("apiVersion", "kind", "metadata", "spec")),
}

_PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
             "array": list, "null": type(None)}

#: Keys a person reaches for to pin a model. Named so the refusal says why, not just "unknown".
_MODEL_ID_KEYS = frozenset({"model", "model_id", "id", "name", "pinned", "provider", "engine"})


def _type_ok(value: Any, kind: Any) -> bool:
    kinds = kind if isinstance(kind, list) else [kind]
    for k in kinds:
        t = _PY_TYPES[k]
        if k in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, t):
            return True
    return False


def _validate(value: Any, schema: Mapping[str, Any], path: str, errors: list[dict[str, str]]) -> None:
    def err(message: str) -> None:
        errors.append({"path": path or "(document)", "message": message})

    if "const" in schema:
        if value != schema["const"]:
            err(f"must be {schema['const']!r}, not {value!r}.")
        return
    if "enum" in schema:
        if value not in schema["enum"]:
            err(f"must be one of {', '.join(map(str, schema['enum']))}; got {value!r}.")
        return
    kind = schema.get("type")
    if kind is not None and not _type_ok(value, kind):
        names = kind if isinstance(kind, list) else [kind]
        err(f"must be {' or '.join(n if n != 'object' else 'a mapping' for n in names)}, "
            f"not {type(value).__name__}.")
        return
    if isinstance(value, str):
        if "pattern" in schema and not re.match(schema["pattern"], value):
            err(f"{value!r} is not a valid name here (expected {schema['pattern']}).")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            err(f"is {len(value)} characters; the limit is {schema['maxLength']}.")
        if "minLength" in schema and len(value) < schema["minLength"]:
            err("must not be empty.")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            err(f"must be at least {schema['minimum']}.")
        if "maximum" in schema and value > schema["maximum"]:
            err(f"must be at most {schema['maximum']}.")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]", errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if value.get(req) in (None, ""):
                errors.append({"path": f"{path}.{req}" if path else req, "message": "is required."})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            sub = f"{path}.{key}" if path else str(key)
            if key in props:
                _validate(item, props[key], sub, errors)
            elif extra is False:
                known = sorted(props)
                hint = difflib.get_close_matches(str(key), known, n=1)
                why = ""
                if path.endswith("spec.model") and key in _MODEL_ID_KEYS:
                    why = " A spec names a tier (fast, balanced, frontier) and what the work needs, never a model."
                errors.append({"path": sub, "message": (
                    "unknown field." + (f" Did you mean {hint[0]!r}?" if hint else "")
                    + f" Allowed here: {', '.join(known)}." + why)})
            elif isinstance(extra, dict):
                _validate(item, extra, sub, errors)


def validate(doc: Any) -> list[dict[str, str]]:
    """Every problem with `doc` as {path, message}; [] when it is a valid boss.agent/v1."""
    errors: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        return [{"path": "(document)", "message": "an agent spec must be a mapping."}]
    _validate(doc, SCHEMA, "", errors)
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    tools = spec.get("tools") if isinstance(spec.get("tools"), dict) else {}
    granted = set(tools.get("skills") or []) if isinstance(tools.get("skills"), list) else set()
    forbidden = set(tools.get("forbidden") or []) if isinstance(tools.get("forbidden"), list) else set()
    for clash in sorted(granted & forbidden):
        errors.append({"path": "spec.tools.skills",
                       "message": f"{clash!r} is both granted and forbidden; take it out of one list."})
    model = spec.get("model") if isinstance(spec.get("model"), dict) else {}
    for i, need in enumerate(model.get("need") or [] if isinstance(model.get("need"), list) else []):
        if isinstance(need, str) and _MODEL_NAMES.match(need):
            errors.append({"path": f"spec.model.need[{i}]", "message": (
                f"{need!r} names a model. A spec names a tier (fast, balanced, frontier) and what the work "
                "needs (reasoning, code, ...), never a model.")})
    persona = spec.get("persona")
    if isinstance(persona, dict) and persona:
        from ..services.agent_persona import lint_persona

        for problem in lint_persona(persona)[0]:
            errors.append({"path": f"spec.persona.{problem['field']}", "message": problem["message"]})
    return errors


# ── The frozen definition ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelPolicy:
    tier: str = "balanced"
    need: tuple[str, ...] = ()
    decision_model: str = "auto"


@dataclass(frozen=True)
class McpGrant:
    server: str
    tools: tuple[str, ...] = ()  # empty = every tool the server offers


@dataclass(frozen=True)
class ToolScope:
    skills: tuple[str, ...] = ()
    mcp: tuple[McpGrant, ...] = ()
    native: tuple[str, ...] = ()
    exposure: str = "scoped"
    forbidden: tuple[str, ...] = ()
    abilities: tuple[str, ...] = ()
    recommended_abilities: tuple[str, ...] = ()
    integrations: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutonomyPolicy:
    level: int = 1
    approval: Mapping[str, str] = field(default_factory=dict)
    escalate: tuple[str, ...] = ()
    upgrade_requires: tuple[str, ...] = ()
    downgrade_triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryPolicy:
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    max_steps: Optional[int] = None
    max_calls: Optional[int] = None
    timeout_seconds: Optional[int] = None
    max_retries: Optional[int] = None
    run_usd: Optional[float] = None
    thread_usd: Optional[float] = None


@dataclass(frozen=True)
class AgentSpec:
    """One `boss.agent/v1` document, validated. Build with `load_spec`, never by hand."""

    key: str
    name: str = ""
    description: str = ""
    team: Optional[str] = None
    reports_to: Optional[str] = None
    sponsor: Optional[str] = None
    version: Optional[int] = None  # informational: the store assigns version numbers
    labels: Mapping[str, str] = field(default_factory=dict)
    extends: Optional[str] = None
    persona: Mapping[str, Any] = field(default_factory=dict)
    mission: str = ""
    outcomes: tuple[str, ...] = ()
    must: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    can_decide: tuple[str, ...] = ()
    must_escalate: tuple[str, ...] = ()
    system_prompt: str = ""
    job_prompt: str = ""
    instructions: tuple[str, ...] = ()
    model: ModelPolicy = field(default_factory=ModelPolicy)
    tools: ToolScope = field(default_factory=ToolScope)
    autonomy: AutonomyPolicy = field(default_factory=AutonomyPolicy)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    context: Mapping[str, Any] = field(default_factory=dict)
    subagents: tuple[Mapping[str, Any], ...] = ()
    triggers: tuple[Mapping[str, Any], ...] = ()
    evaluators: tuple[Mapping[str, Any], ...] = ()
    output_contract: Mapping[str, Any] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    cadence: Mapping[str, str] = field(default_factory=dict)
    comms: Mapping[str, Any] = field(default_factory=dict)
    a2a: Mapping[str, Any] = field(default_factory=dict)
    scopes: Mapping[str, str] = field(default_factory=dict)

    @property
    def skills(self) -> tuple[str, ...]:
        return self.tools.skills

    @property
    def display_name(self) -> str:
        return self.name or self.key.replace("_", " ").title()


@dataclass(frozen=True)
class CompiledAgent:
    """What a published version IS to the runtime. P1b's kernel builds its RunSpec from this:
    `prompt` is the system prompt's agent-owned part, `skills`/`mcp`/`native_tools` the tool
    grant, `model` the tier request, `limits`/`autonomy` the policies, `memory`/`context` the
    P6 hooks. `role_spec` is the projection written to `roles.spec` for every reader that still
    reads the row. `content_hash` identifies the definition (the identity seal is keyed to it)."""

    key: str
    display_name: str
    content_hash: str
    prompt: str
    role_spec: Mapping[str, Any]
    skills: tuple[str, ...]
    mcp: tuple[McpGrant, ...]
    native_tools: tuple[str, ...]
    exposure: str
    model: ModelPolicy
    autonomy: AutonomyPolicy
    limits: Limits
    memory: MemoryPolicy
    context: Mapping[str, Any]
    output_contract: Mapping[str, Any]
    reports_to: Optional[str] = None
    #: Set for a profile: the role whose grants it runs under (it projects onto nothing).
    extends: Optional[str] = None
    #: Implicit and not removable by a spec (AGENT_PLATFORM §4.0 principle 3).
    governance: tuple[str, ...] = ("authz", "approval", "budget", "taint", "ledger")


def _t(values: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (values or []))


def _sorted_set(values: Any) -> tuple[str, ...]:
    """Grant-like lists are SETS: order never changes what an agent holds, so it never changes
    the content hash either (a projection read back from role_skills comes back sorted)."""
    return tuple(sorted({str(v) for v in values or [] if str(v).strip()}))


def _display(key: str) -> str:
    return key.replace("_", " ").title()


def load_spec(doc: Any) -> AgentSpec:
    """Validate `doc` (a parsed YAML/JSON mapping) and return the frozen spec. Raises SpecError
    with every problem at once."""
    errors = validate(doc)
    if errors:
        raise SpecError(errors)
    meta = doc["metadata"]
    s = doc["spec"]
    b, r, p = s.get("boundaries") or {}, s.get("decision_rights") or {}, s.get("prompts") or {}
    m, t, a = s.get("model") or {}, s.get("tools") or {}, s.get("autonomy") or {}
    mem, lim = s.get("memory") or {}, s.get("limits") or {}
    mcp: dict[str, set[str]] = {}
    for x in t.get("mcp") or []:
        mcp.setdefault(str(x["server"]), set()).update(str(v) for v in x.get("tools") or [])
    return AgentSpec(
        key=meta["key"], name=str(meta.get("name") or "").strip() or _display(meta["key"]),
        description=str(meta.get("description") or ""),
        team=meta.get("team") or None, reports_to=meta.get("reports_to") or None,
        sponsor=meta.get("sponsor") or None, version=meta.get("version"),
        labels=dict(meta.get("labels") or {}), extends=meta.get("extends") or None,
        persona=dict(s.get("persona") or {}), mission=str(s.get("mission") or ""),
        outcomes=_t(s.get("outcomes")), must=_t(b.get("must")), must_not=_t(b.get("must_not")),
        can_decide=_t(r.get("can_decide")), must_escalate=_t(r.get("must_escalate")),
        system_prompt=str(p.get("system") or ""), job_prompt=str(p.get("job") or ""),
        instructions=_t(s.get("instructions")),
        model=ModelPolicy(tier=m.get("tier") or "balanced", need=_sorted_set(m.get("need")),
                          decision_model=m.get("decision_model") or "auto"),
        tools=ToolScope(
            skills=_sorted_set(t.get("skills")),
            mcp=tuple(McpGrant(server=k, tools=tuple(sorted(v))) for k, v in sorted(mcp.items())),
            native=_sorted_set(t.get("native")), exposure=t.get("exposure") or "scoped",
            forbidden=_sorted_set(t.get("forbidden")), abilities=_sorted_set(t.get("abilities")),
            recommended_abilities=_sorted_set(t.get("recommended_abilities")),
            integrations=_sorted_set(t.get("integrations"))),
        autonomy=AutonomyPolicy(level=int(a.get("level", 1)), approval=dict(a.get("approval") or {}),
                                escalate=_t(a.get("escalate")), upgrade_requires=_t(a.get("upgrade_requires")),
                                downgrade_triggers=_t(a.get("downgrade_triggers"))),
        memory=MemoryPolicy(read=_sorted_set(mem.get("read")), write=_sorted_set(mem.get("write"))),
        context=dict(s.get("context") or {}),
        subagents=tuple(dict(x) for x in s.get("subagents") or []),
        triggers=tuple(dict(x) for x in s.get("triggers") or []),
        evaluators=tuple(dict(x) for x in s.get("evaluators") or []),
        output_contract=dict(s.get("output_contract") or {}),
        limits=Limits(**{k: lim[k] for k in ("max_steps", "max_calls", "timeout_seconds", "max_retries",
                                             "run_usd", "thread_usd") if lim.get(k) is not None}),
        cadence=dict(s.get("cadence") or {}), comms=dict(s.get("comms") or {}),
        a2a=dict(s.get("a2a") or {}), scopes=dict(s.get("scopes") or {}),
    )


def _drop_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {} and v != ()}


def _limits_doc(lim: Limits) -> dict[str, Any]:
    return _drop_empty({"max_steps": lim.max_steps, "max_calls": lim.max_calls,
                        "timeout_seconds": lim.timeout_seconds, "max_retries": lim.max_retries,
                        "run_usd": lim.run_usd, "thread_usd": lim.thread_usd})


def _autonomy_doc(a: AutonomyPolicy) -> dict[str, Any]:
    return {"level": a.level, **_drop_empty({
        "approval": dict(a.approval), "escalate": list(a.escalate),
        "upgrade_requires": list(a.upgrade_requires), "downgrade_triggers": list(a.downgrade_triggers)})}


def dump_spec(spec: AgentSpec, *, include_version: bool = False) -> dict[str, Any]:
    """The canonical document for `spec` (empty fields left out). `load_spec(dump_spec(s)) == s`
    (bar `version`, which is the store's)."""
    meta = _drop_empty({"key": spec.key, "name": spec.name, "description": spec.description,
                        "team": spec.team, "reports_to": spec.reports_to, "sponsor": spec.sponsor,
                        "version": spec.version if include_version else None, "labels": dict(spec.labels),
                        "extends": spec.extends})
    body = _drop_empty({
        "persona": dict(spec.persona), "mission": spec.mission, "outcomes": list(spec.outcomes),
        "boundaries": _drop_empty({"must": list(spec.must), "must_not": list(spec.must_not)}),
        "decision_rights": _drop_empty({"can_decide": list(spec.can_decide),
                                        "must_escalate": list(spec.must_escalate)}),
        "prompts": _drop_empty({"system": spec.system_prompt, "job": spec.job_prompt}),
        "instructions": list(spec.instructions),
        "model": _drop_empty({"tier": spec.model.tier, "need": list(spec.model.need),
                              "decision_model": spec.model.decision_model}),
        "tools": _drop_empty({
            "skills": list(spec.tools.skills),
            "mcp": [_drop_empty({"server": g.server, "tools": list(g.tools)}) for g in spec.tools.mcp],
            "native": list(spec.tools.native), "exposure": spec.tools.exposure,
            "forbidden": list(spec.tools.forbidden), "abilities": list(spec.tools.abilities),
            "recommended_abilities": list(spec.tools.recommended_abilities),
            "integrations": list(spec.tools.integrations)}),
        "autonomy": _autonomy_doc(spec.autonomy),
        "memory": _drop_empty({"read": list(spec.memory.read), "write": list(spec.memory.write)}),
        "context": dict(spec.context), "subagents": [dict(x) for x in spec.subagents],
        "triggers": [dict(x) for x in spec.triggers], "evaluators": [dict(x) for x in spec.evaluators],
        "output_contract": dict(spec.output_contract), "limits": _limits_doc(spec.limits),
        "cadence": dict(spec.cadence), "comms": dict(spec.comms), "a2a": dict(spec.a2a),
        "scopes": dict(spec.scopes),
    })
    return {"apiVersion": API_VERSION, "kind": KIND, "metadata": meta, "spec": body}


def canonical_json(spec: AgentSpec) -> str:
    return json.dumps(dump_spec(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(spec: AgentSpec) -> str:
    """sha256 of the canonical document without metadata.version: the same definition always
    hashes the same, whatever version number it was stored under."""
    return "sha256:" + hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


# ── The roles-row projection, and its inverse ──────────────────────────────────────────────────
#
# `role_spec` writes these keys of `roles.spec` (every other key of a row is left as it is);
# `doc_from_role` reads them back. Publishing a spec and reading the row back gives the same
# document — tests/test_agent_spec.py proves it — which is what makes "has the row drifted from
# its active version" a hash comparison.

#: roles.spec keys a published spec owns.
ROLE_SPEC_KEYS: tuple[str, ...] = (
    "display_name", "description", "team", "mission", "primary_outcomes", "boundaries", "decision_rights",
    "output_contracts", "output_contract", "autonomy", "cadence", "comms_contract", "eval_rubric",
    "persona", "prompts", "standing_instructions", "model_policy", "limits", "memory", "agent_spec")
#: roles.spec.permissions keys a published spec owns. `allowed_skills` is NOT one: it is the
#: shipped "may be given" list the boot seed reads, and the grant is role_skills.
ROLE_PERMISSION_KEYS: tuple[str, ...] = ("abilities", "recommended_abilities", "forbidden_skills", "mcp_servers")


def role_spec(spec: AgentSpec) -> dict[str, Any]:
    """The `roles.spec` projection, in the keys every existing reader uses (agent_persona L2/L3,
    agent_steps.agent_brief, the console). New keys are additive."""
    rubric = next((dict(e.get("criteria") or {}) for e in spec.evaluators if e.get("kind") == "rubric"), {})
    out: dict[str, Any] = {
        "display_name": spec.display_name, "description": spec.description, "team": spec.team,
        "mission": spec.mission, "primary_outcomes": list(spec.outcomes),
        "boundaries": {"must": list(spec.must), "must_not": list(spec.must_not)},
        "decision_rights": _drop_empty({"can_decide": list(spec.can_decide),
                                        "must_escalate": list(spec.must_escalate)}),
        "permissions": _drop_empty({
            "abilities": list(spec.tools.abilities),
            "recommended_abilities": list(spec.tools.recommended_abilities),
            "forbidden_skills": list(spec.tools.forbidden),
            "mcp_servers": [_drop_empty({"server": g.server, "tools": list(g.tools)}) for g in spec.tools.mcp]}),
        "output_contracts": list(spec.output_contract.get("artifacts") or []),
        "output_contract": dict(spec.output_contract),
        "autonomy": _autonomy_doc(spec.autonomy),
        "cadence": dict(spec.cadence), "comms_contract": dict(spec.comms), "eval_rubric": rubric,
        "persona": dict(spec.persona),
        "prompts": _drop_empty({"system": spec.system_prompt, "job": spec.job_prompt}),
        "standing_instructions": list(spec.instructions),
        "model_policy": _drop_empty({"tier": spec.model.tier, "need": list(spec.model.need),
                                     "decision_model": spec.model.decision_model}),
        "limits": _limits_doc(spec.limits),
        "memory": _drop_empty({"read": list(spec.memory.read), "write": list(spec.memory.write)}),
    }
    return {k: v for k, v in out.items() if (v not in (None, "") and v != [] and v != {})
            or k in ("boundaries", "autonomy")}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value or [] if isinstance(v, (str, int, float)) and str(v).strip()] \
        if isinstance(value, (list, tuple)) else []


def _str_map(value: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items() if isinstance(v, (str, int, float))} \
        if isinstance(value, dict) else {}


def _pick(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: value[k] for k in keys if isinstance(value, dict) and k in value}


def doc_from_role(row: Mapping[str, Any], *, skills: Any = (), mcp: Any = (),
                  base: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """A `boss.agent/v1` document for a roles row and what it holds — the inverse of
    `role_spec` for the owned keys. Fields a row cannot carry (native tools, triggers,
    subagents, context, a2a, scopes, labels, sponsor) come from `base` (the agent's active
    version), so reading a row never loses them. Anything malformed in the row is dropped, not
    guessed: a legacy row always yields a document that validates (persona aside, see migration).

    `skills` are the role_skills ids (baseline excluded by the caller); `mcp` is [{server, tools}].
    """
    base = dict(base or {})
    bmeta = dict(base.get("metadata") or {})
    bspec = dict(base.get("spec") or {})
    rs = row.get("spec") if isinstance(row.get("spec"), dict) else {}
    key = str(row.get("role_key") or bmeta.get("key") or "")
    perms = rs.get("permissions") if isinstance(rs.get("permissions"), dict) else {}
    bounds = rs.get("boundaries") if isinstance(rs.get("boundaries"), dict) else {}
    rights = rs.get("decision_rights") if isinstance(rs.get("decision_rights"), dict) else {}
    prompts = rs.get("prompts") if isinstance(rs.get("prompts"), dict) else {}
    model = rs.get("model_policy") if isinstance(rs.get("model_policy"), dict) else {}
    tier = model.get("tier") if model.get("tier") in TIERS else {"low_cost": "balanced", "strong": "frontier"}.get(
        str(model.get("class") or model.get("model_class") or ""), None)
    need = [n for n in _str_list(model.get("need") or model.get("preferred_capabilities"))
            if re.match(_NEED, n) and not _MODEL_NAMES.match(n)]
    autonomy = rs.get("autonomy") if isinstance(rs.get("autonomy"), dict) else {}
    level = autonomy.get("level")
    oc = rs.get("output_contract") if isinstance(rs.get("output_contract"), dict) else {}
    oc = _pick(oc, ("artifacts", "artifact_type", "sections", "schema", "strategy"))
    artifacts = _str_list(rs.get("output_contracts"))
    if artifacts or "artifacts" in oc:
        oc["artifacts"] = artifacts
    comms = rs.get("comms_contract") if isinstance(rs.get("comms_contract"), dict) else {}
    rubric = rs.get("eval_rubric") if isinstance(rs.get("eval_rubric"), dict) else {}
    evaluators = [e for e in bspec.get("evaluators") or [] if e.get("kind") != "rubric"]
    if rubric:
        evaluators = [{"kind": "rubric", "criteria": {str(k): v for k, v in rubric.items()}}] + evaluators
    limits = rs.get("limits") if isinstance(rs.get("limits"), dict) else {}
    memory = rs.get("memory") if isinstance(rs.get("memory"), dict) else {}
    btools = dict(bspec.get("tools") or {})
    tools = {
        **_pick(btools, ("native", "exposure", "integrations")),
        "skills": sorted({str(s) for s in skills or []}),
        "mcp": [{"server": str(m["server"]), "tools": sorted(str(t) for t in m.get("tools") or [])}
                for m in sorted(mcp or [], key=lambda m: str(m["server"]))],
        "forbidden": _str_list(perms.get("forbidden_skills")),
        "abilities": _str_list(perms.get("abilities")),
        "recommended_abilities": _str_list(perms.get("recommended_abilities")),
    }
    # Skill ids a schema would refuse (an odd hand-written key) stay held but are not described.
    tools = {k: ([s for s in v if re.match(_SKILL, s)] if k in ("skills", "forbidden", "abilities",
                                                               "recommended_abilities") else v)
             for k, v in tools.items()}
    persona = rs.get("persona") if isinstance(rs.get("persona"), dict) else {}
    team = rs.get("team") if isinstance(rs.get("team"), str) else None
    display = str(row.get("display_name") or rs.get("display_name") or "")
    doc = {
        "apiVersion": API_VERSION, "kind": KIND,
        "metadata": {**_pick(bmeta, ("sponsor", "labels", "extends")), "key": key, "name": display,
                     "description": str(rs.get("description") or ""), "team": team,
                     "reports_to": row.get("reports_to") or None},
        "spec": {
            **_pick(bspec, ("context", "subagents", "triggers", "a2a", "scopes")),
            "persona": persona, "mission": str(rs.get("mission") or ""),
            "outcomes": _str_list(rs.get("primary_outcomes")),
            "boundaries": {"must": _str_list(bounds.get("must")), "must_not": _str_list(bounds.get("must_not"))},
            "decision_rights": {"can_decide": _str_list(rights.get("can_decide")),
                                "must_escalate": _str_list(rights.get("must_escalate"))},
            "prompts": {k: str(prompts[k]) for k in ("system", "job") if isinstance(prompts.get(k), str)},
            "instructions": _str_list(rs.get("standing_instructions")),
            "model": {**({"tier": tier} if tier else {}), "need": need,
                      **({"decision_model": model["decision_model"]}
                         if model.get("decision_model") in DECISION_MODELS else {})},
            "tools": tools,
            "autonomy": {**({"level": level} if isinstance(level, int) and not isinstance(level, bool)
                            and 0 <= level <= 4 else {}),
                         **{k: autonomy[k] for k in ("approval",) if isinstance(autonomy.get(k), dict)
                            and all(v in APPROVAL_MODES for v in autonomy[k].values())},
                         **{k: _str_list(autonomy.get(k)) for k in ("escalate", "upgrade_requires",
                                                                   "downgrade_triggers")}},
            "memory": {k: [n for n in _str_list(memory.get(k)) if re.match(_NAMESPACE, n)] for k in MEMORY_ACCESS},
            "evaluators": evaluators,
            "output_contract": oc,
            "limits": {k: v for k, v in limits.items() if k in ("max_steps", "max_calls", "timeout_seconds",
                                                               "max_retries", "run_usd", "thread_usd")
                       and isinstance(v, (int, float)) and not isinstance(v, bool)},
            "cadence": _str_map(rs.get("cadence")),
            "comms": {**({"tone": str(comms["tone"])} if isinstance(comms.get("tone"), str) else {}),
                      **({"slack_prefix": str(comms["slack_prefix"])}
                         if isinstance(comms.get("slack_prefix"), str) else {}),
                      "required_references": _str_list(comms.get("required_references"))},
        },
    }
    # The canonical shape: what a stored version's document looks like.
    try:
        return dump_spec(load_spec(_strip(doc)))
    except SpecError:
        return _strip(doc)


def _strip(doc: Any) -> Any:
    if isinstance(doc, dict):
        out = {k: _strip(v) for k, v in doc.items()}
        return {k: v for k, v in out.items() if v is not None and v != "" and v != [] and v != {}}
    if isinstance(doc, list):
        return [_strip(v) for v in doc]
    return doc


def prompt_sections(projection: Mapping[str, Any]) -> list[str]:
    """The role prompt, standing instructions and job, as prompt text. ONE reading for the
    compiled prompt and the live work prompt (agent_steps.agent_brief), so what a version says
    is what an agent step is told."""
    prompts = projection.get("prompts") if isinstance(projection.get("prompts"), dict) else {}
    out: list[str] = []
    system = str(prompts.get("system") or "").strip()
    if system:
        out.append("YOUR ROLE PROMPT\n" + system)
    instructions = _str_list(projection.get("standing_instructions"))
    if instructions:
        out.append("YOUR STANDING INSTRUCTIONS\n" + "\n".join(f"  - {i}" for i in instructions))
    job = str(prompts.get("job") or "").strip()
    if job:
        out.append("YOUR JOB\n" + job)
    return out


def compile_prompt(spec: AgentSpec, projection: Optional[Mapping[str, Any]] = None) -> str:
    """The agent-owned part of the work prompt, byte-stable for a given spec: identity, remit and
    voice (agent_persona L1-L3, the one persona compiler), then the role prompt and job."""
    from ..services.agent_persona import compose_agent_prompt, render

    projection = dict(projection if projection is not None else role_spec(spec))
    role = {"role_key": spec.extends or spec.key, "display_name": spec.display_name, "spec": projection}
    parts = [render(compose_agent_prompt(role, mode="work"), layers=("L1", "L2", "L3"))]
    parts.extend(prompt_sections(projection))
    return "\n\n".join(p for p in parts if p)


def compile_spec(spec: AgentSpec) -> CompiledAgent:
    """Compile a validated spec. Pure and deterministic: the same spec compiles to the same bytes."""
    projection = role_spec(spec)
    digest = content_hash(spec)
    projection["agent_spec"] = {"api_version": API_VERSION, "content_hash": digest}
    return CompiledAgent(
        key=spec.key, display_name=spec.display_name, content_hash=digest,
        prompt=compile_prompt(spec, projection), role_spec=projection,
        skills=tuple(s for s in spec.tools.skills if s not in spec.tools.forbidden),
        mcp=spec.tools.mcp, native_tools=spec.tools.native, exposure=spec.tools.exposure,
        model=spec.model, autonomy=spec.autonomy, limits=spec.limits, memory=spec.memory,
        context=dict(spec.context), output_contract=dict(spec.output_contract), reports_to=spec.reports_to,
        extends=spec.extends,
    )


def compiled_as_dict(compiled: CompiledAgent) -> dict[str, Any]:
    """JSON for a CompiledAgent (what a version row stores beside its prompt)."""
    from dataclasses import asdict

    out = asdict(compiled)
    out["mcp"] = [{"server": g.server, "tools": list(g.tools)} for g in compiled.mcp]
    return json.loads(json.dumps(out, default=list))


def json_schema() -> dict[str, Any]:
    """The JSON Schema for boss.agent/v1 (what `SCHEMA` publishes to editors and the API)."""
    return json.loads(json.dumps(SCHEMA))


__all__ = ["API_VERSION", "KIND", "TIERS", "SCHEMA", "ROLE_SPEC_KEYS", "ROLE_PERMISSION_KEYS", "AgentSpec",
           "AutonomyPolicy", "CompiledAgent", "Limits", "McpGrant", "MemoryPolicy", "ModelPolicy", "SpecError",
           "ToolScope", "canonical_json", "compile_prompt", "compile_spec", "compiled_as_dict", "content_hash",
           "doc_from_role", "dump_spec", "json_schema", "load_spec", "prompt_sections", "role_spec", "validate"]
