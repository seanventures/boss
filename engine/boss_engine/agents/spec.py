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
_SKILL = r"^[a-z0-9][a-z0-9_.:\-]*$"
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
                "need": _strs(pattern=_WORD),
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
            "comms": _obj({"tone": _s(), "required_references": _strs()}),
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


def _t(values: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (values or []))


def _dedupe(values: Any) -> tuple[str, ...]:
    out: list[str] = []
    for v in values or []:
        v = str(v)
        if v not in out:
            out.append(v)
    return tuple(out)


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
    return AgentSpec(
        key=meta["key"], name=str(meta.get("name") or ""), description=str(meta.get("description") or ""),
        team=meta.get("team"), reports_to=meta.get("reports_to"), sponsor=meta.get("sponsor"),
        version=meta.get("version"), labels=dict(meta.get("labels") or {}),
        persona=dict(s.get("persona") or {}), mission=str(s.get("mission") or ""),
        outcomes=_t(s.get("outcomes")), must=_t(b.get("must")), must_not=_t(b.get("must_not")),
        can_decide=_t(r.get("can_decide")), must_escalate=_t(r.get("must_escalate")),
        system_prompt=str(p.get("system") or ""), job_prompt=str(p.get("job") or ""),
        instructions=_t(s.get("instructions")),
        model=ModelPolicy(tier=m.get("tier") or "balanced", need=_dedupe(m.get("need")),
                          decision_model=m.get("decision_model") or "auto"),
        tools=ToolScope(
            skills=_dedupe(t.get("skills")),
            mcp=tuple(McpGrant(server=str(x["server"]), tools=_dedupe(x.get("tools"))) for x in t.get("mcp") or []),
            native=_dedupe(t.get("native")), exposure=t.get("exposure") or "scoped",
            forbidden=_dedupe(t.get("forbidden")), abilities=_dedupe(t.get("abilities")),
            recommended_abilities=_dedupe(t.get("recommended_abilities")),
            integrations=_dedupe(t.get("integrations"))),
        autonomy=AutonomyPolicy(level=int(a.get("level", 1)), approval=dict(a.get("approval") or {}),
                                escalate=_t(a.get("escalate"))),
        memory=MemoryPolicy(read=_dedupe(mem.get("read")), write=_dedupe(mem.get("write"))),
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
    return {k: v for k, v in d.items() if v not in (None, "", [], {}, ())}


def dump_spec(spec: AgentSpec, *, include_version: bool = False) -> dict[str, Any]:
    """The canonical document for `spec` (empty fields left out). `load_spec(dump_spec(s)) == s`."""
    meta = _drop_empty({"key": spec.key, "name": spec.name, "description": spec.description,
                        "team": spec.team, "reports_to": spec.reports_to, "sponsor": spec.sponsor,
                        "version": spec.version if include_version else None, "labels": dict(spec.labels)})
    lim = spec.limits
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
        "autonomy": _drop_empty({"level": spec.autonomy.level, "approval": dict(spec.autonomy.approval),
                                 "escalate": list(spec.autonomy.escalate)}),
        "memory": _drop_empty({"read": list(spec.memory.read), "write": list(spec.memory.write)}),
        "context": dict(spec.context), "subagents": [dict(x) for x in spec.subagents],
        "triggers": [dict(x) for x in spec.triggers], "evaluators": [dict(x) for x in spec.evaluators],
        "output_contract": dict(spec.output_contract),
        "limits": _drop_empty({"max_steps": lim.max_steps, "max_calls": lim.max_calls,
                               "timeout_seconds": lim.timeout_seconds, "max_retries": lim.max_retries,
                               "run_usd": lim.run_usd, "thread_usd": lim.thread_usd}),
        "cadence": dict(spec.cadence), "comms": dict(spec.comms), "a2a": dict(spec.a2a),
        "scopes": dict(spec.scopes),
    })
    # level 0 is a real autonomy level: keep it even though _drop_empty would not (0 is kept).
    return {"apiVersion": API_VERSION, "kind": KIND, "metadata": meta, "spec": body}


def content_hash(spec: AgentSpec) -> str:
    """sha256 of the canonical document without metadata.version: the same definition always
    hashes the same, whatever version number it was stored under."""
    canonical = json.dumps(dump_spec(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def role_spec(spec: AgentSpec) -> dict[str, Any]:
    """The `roles.spec` projection, in the keys every existing reader uses (agent_persona L2/L3,
    agent_steps.agent_brief, the console). New keys (`prompts`, `instructions`, `model_policy`,
    `limits`, `memory`, `agent_spec`) are additive."""
    out: dict[str, Any] = {
        "role_id": spec.key, "display_name": spec.display_name, "team": spec.team,
        "mission": spec.mission, "primary_outcomes": list(spec.outcomes),
        "boundaries": {"must": list(spec.must), "must_not": list(spec.must_not)},
        "decision_rights": {"can_decide": list(spec.can_decide), "must_escalate": list(spec.must_escalate)},
        "permissions": _drop_empty({
            "allowed_skills": list(spec.tools.skills), "abilities": list(spec.tools.abilities),
            "recommended_abilities": list(spec.tools.recommended_abilities),
            "forbidden_skills": list(spec.tools.forbidden),
            "mcp_servers": [_drop_empty({"server": g.server, "tools": list(g.tools)}) for g in spec.tools.mcp]}),
        "output_contracts": list(spec.output_contract.get("artifacts") or []),
        "autonomy": {"level": spec.autonomy.level},
        "cadence": dict(spec.cadence),
        "comms_contract": dict(spec.comms),
        "eval_rubric": next((dict(e.get("criteria") or {}) for e in spec.evaluators
                             if e.get("kind") == "rubric"), {}),
        "persona": dict(spec.persona),
        "prompts": _drop_empty({"system": spec.system_prompt, "job": spec.job_prompt}),
        "instructions": list(spec.instructions),
        "model_policy": _drop_empty({"tier": spec.model.tier, "need": list(spec.model.need),
                                     "decision_model": spec.model.decision_model}),
        "limits": dump_spec(spec)["spec"].get("limits", {}),
        "memory": _drop_empty({"read": list(spec.memory.read), "write": list(spec.memory.write)}),
    }
    if spec.description:
        out["description"] = spec.description
    return {k: v for k, v in out.items() if v not in (None, "", [], {}) or k in ("boundaries", "autonomy")}


def compile_prompt(spec: AgentSpec, projection: Optional[Mapping[str, Any]] = None) -> str:
    """The agent-owned part of the work prompt, byte-stable for a given spec: identity, remit and
    voice (agent_persona L1-L3, the one persona compiler), then the role prompt and job."""
    from ..services.agent_persona import compose_agent_prompt, render

    role = {"role_key": spec.key, "display_name": spec.display_name,
            "spec": dict(projection if projection is not None else role_spec(spec))}
    parts = [render(compose_agent_prompt(role, mode="work"), layers=("L1", "L2", "L3"))]
    if spec.system_prompt.strip():
        parts.append("YOUR ROLE PROMPT\n" + spec.system_prompt.strip())
    if spec.instructions:
        parts.append("YOUR STANDING INSTRUCTIONS\n" + "\n".join(f"  - {i}" for i in spec.instructions))
    if spec.job_prompt.strip():
        parts.append("YOUR JOB\n" + spec.job_prompt.strip())
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
    )


def json_schema() -> dict[str, Any]:
    """The JSON Schema for boss.agent/v1 (what `SCHEMA` publishes to editors and the API)."""
    return json.loads(json.dumps(SCHEMA))


__all__ = ["API_VERSION", "KIND", "TIERS", "SCHEMA", "AgentSpec", "AutonomyPolicy", "CompiledAgent", "Limits",
           "McpGrant", "MemoryPolicy", "ModelPolicy", "SpecError", "ToolScope", "compile_prompt", "compile_spec",
           "content_hash", "dump_spec", "json_schema", "load_spec", "role_spec", "validate"]
