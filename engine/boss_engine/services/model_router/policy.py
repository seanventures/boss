"""(b) Policy — what the operator says (docs/product/MODEL_ROUTER.md, "(b) Policy").

A `RoutingPolicy` is data: tiers → ordered targets, floors, headroom rules, the budget response,
escalation rules, an allow-list and the classifier's settings. It is stored per project as ONE
JSON document (`model_routing_policies`) and validated here, in one place, whoever writes it.

Pins are not stored here: a task pin rides the task (`model_config`), an agent pin rides the
agent version (`model_policy`), and a job pin is the "Who does what" row the operator set
(`project_settings`). The router reads each from where it already lives, so nothing is copied
and nothing goes stale.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .tiers import BALANCED, FAST, FRONTIER, TIERS, rank, step_label

MODES = ("fixed", "smart")
HEADROOM_STATES = ("plenty", "tight", "near_limit", "exhausted")
CLASSIFIER_BACKENDS = ("auto", "jev", "rules", "model")
SIGNALS = ("tests_failed", "no_output", "changes_requested", "gate_failed", "final_attempt")

#: Each engine's model per tier, when the operator has not named one. Claude's CLI aliases are
#: stable across releases ("opus" is always the current Opus), which is why they are used rather
#: than dated ids. An engine absent here runs its own default model on every tier.
ENGINE_TIER_MODELS: dict[str, dict[str, Optional[str]]] = {
    "claude_code": {FAST: "haiku", BALANCED: "sonnet", FRONTIER: "opus"},
}

#: OpenRouter's defaults when its catalogue has not been fetched (openrouter_catalog.suggest
#: replaces them with live ids and prices once it has). Cheap where cheap is the point.
OPENROUTER_FALLBACK_MODELS = {
    FAST: "deepseek/deepseek-chat",
    BALANCED: "qwen/qwen3-coder",
    FRONTIER: "anthropic/claude-opus-4.1",
}

LOCAL_ENGINES = frozenset({"ollama", "vllm"})
SUBSCRIPTION_ENGINES = frozenset({"claude_code", "codex"})

#: The build stages a profile's model map can name.
BUILD_STEPS = ("standards", "spec", "plan", "review_plan", "write_tests", "implement",
               "review_code", "security_check", "consistency_check", "check_tests")

DEFAULT_CRITERIA = {
    FAST: "One small, well-specified change or a quick answer: copy, styling, a typo, a "
          "rename, a short question.",
    BALANCED: "Real engineering with a clear goal: a feature, a bug fix that changes "
              "behaviour, a new app, tests, a plan, a research synthesis.",
    FRONTIER: "Judgment where a wrong call is expensive: adversarial or security review, "
              "auth, money, secrets or migrations, architecture, facilitation, hard repairs.",
}


#: Model names by the tier they belong to — for reading what a pin, a profile or a session is
#: really running, and for refusing a non-frontier model as the Frontier tier. Frontier is
#: checked first (gpt-5 before "mini"), so "gpt-5-mini" is not Frontier because of the order
#: below: the fast markers are checked against the name before the frontier ones.
_FAST_MARKERS = ("haiku", "-mini", "mini-", "nano", "flash", "-small", "ministral", "lite",
                 "-fast")
#: A parameter count in the name ("llama-3.1-8b", "qwen2.5-7b-instruct"): small (< 10B) is
#: Fast. Matched as a whole size token, so "gemma-3-27b" is not "7b" and "mixtral-8x7b" (a
#: mixture of experts) is not "7b" either.
_SIZE = re.compile(r"(?:^|[-_/:])(\d+(?:\.\d+)?)b(?=$|[-_:.])")
_FRONTIER_MARKERS = ("opus", "gpt-5", "gemini-2.5-pro", "gemini-3-pro", "grok-4",
                     "claude-sonnet-4.5-thinking")
_BALANCED_MARKERS = ("sonnet", "deepseek", "qwen", "coder", "kimi", "glm", "llama", "mistral",
                     "mixtral", "gemma", "gpt-oss", "gpt-4", "gemini", "codestral", "devstral",
                     "grok-3", "grok-code")


def model_tier(engine: str, model: Optional[str]) -> Optional[str]:
    """The tier a named model belongs to, or None when the name says nothing. An engine's own
    tier aliases (claude_code: haiku/sonnet/opus) are exact; other names go by markers."""
    from ..claude_credential import split_pick

    name = str(split_pick(model)[1] or "").lower()
    if not name:
        return None
    for tier, alias in (ENGINE_TIER_MODELS.get(str(engine or "").lower()) or {}).items():
        if alias and name == alias:
            return tier
    base = name.rsplit("/", 1)[-1]
    size = _SIZE.search(base)
    if any(m in base for m in _FAST_MARKERS) or (size and float(size.group(1)) < 10):
        return FAST
    if any(m in base for m in _FRONTIER_MARKERS) or base.startswith(("o3", "o4")):
        return FRONTIER
    if any(m in base for m in _BALANCED_MARKERS):
        return BALANCED
    return None


class PolicyError(ValueError):
    """A policy document that cannot be routed on. The message names the field."""


@dataclass(frozen=True)
class Target:
    engine: str
    model: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "model": self.model}


@dataclass(frozen=True)
class Floor:
    #: Keys: step (str or list), role, job, kind, task_kind. Every key given must match.
    match: Mapping[str, Any]
    tier: str
    label: str = ""

    def matches(self, *, kind: str, step: str, role: Optional[str], job: str,
                task_kind: Optional[str]) -> bool:
        facts = {"kind": kind, "step": step, "role": role, "job": job, "task_kind": task_kind}
        for key, wanted in self.match.items():
            have = facts.get(key)
            if isinstance(wanted, (list, tuple)):
                if have not in wanted:
                    return False
            elif have != wanted:
                return False
        return True

    def describe(self) -> str:
        return self.label or _floor_label(self.match)

    def to_dict(self) -> dict[str, Any]:
        return {"match": dict(self.match), "tier": self.tier,
                **({"label": self.label} if self.label else {})}


def _floor_label(match: Mapping[str, Any]) -> str:
    if "step" in match and isinstance(match["step"], str):
        return step_label(match["step"])
    if "role" in match:
        return str(match["role"]).replace("_", " ").capitalize()
    if match.get("kind") == "build_stage":
        return "Every build stage"
    if "task_kind" in match:
        return f"{match['task_kind']} tasks"
    return "Matching work"


@dataclass(frozen=True)
class HeadroomRule:
    source: str = "claude_code"
    when: str = "near_limit"          # the state at or below which the rule fires
    tiers: tuple[str, ...] = (FAST, BALANCED)
    to: str = "openrouter"

    def fires(self, state: str) -> bool:
        return HEADROOM_STATES.index(state) >= HEADROOM_STATES.index(self.when)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "when": self.when, "tiers": list(self.tiers), "to": self.to}


@dataclass(frozen=True)
class EscalationRule:
    signal: str
    count: int
    steps: tuple[str, ...]
    to: str = "up"                    # "up" one tier, or a tier name

    def to_dict(self) -> dict[str, Any]:
        return {"signal": self.signal, "count": self.count, "steps": list(self.steps),
                "to": self.to}


DEFAULT_ESCALATION: tuple[EscalationRule, ...] = (
    EscalationRule("tests_failed", 2, ("implement", "solo", "repair"), "up"),
    EscalationRule("no_output", 1, ("write_tests",), FRONTIER),
    EscalationRule("changes_requested", 2, ("implement", "solo", "repair"), "up"),
    EscalationRule("gate_failed", 1, ("implement", "solo", "repair"), "up"),
    EscalationRule("final_attempt", 1, ("implement", "solo", "repair", "write_tests"), FRONTIER),
)


@dataclass(frozen=True)
class ClassifierConfig:
    backend: str = "auto"
    jev_timeout_ms: int = 800
    model_timeout_ms: int = 6000
    #: Below this rules-confidence, the cheap-model classifier is asked (backend auto/model).
    model_below: float = 0.6
    #: The cheap-model fallback is opt-in: it is a paid (or subscription) call per new task.
    model_fallback: bool = False
    criteria: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_CRITERIA))

    def to_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "jev_timeout_ms": self.jev_timeout_ms,
                "model_timeout_ms": self.model_timeout_ms, "model_below": self.model_below,
                "model_fallback": self.model_fallback, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class RoutingPolicy:
    mode: str = "fixed"
    tiers: Mapping[str, tuple[Target, ...]] = field(default_factory=dict)
    floors: tuple[Floor, ...] = ()
    floor_preset: str = "balanced"
    headroom: tuple[HeadroomRule, ...] = ()
    #: Fraction of the weekly window LEFT at which a source turns tight / near limit.
    thresholds: Mapping[str, float] = field(
        default_factory=lambda: {"tight": 0.40, "near_limit": 0.15})
    #: Budget state -> tiers to drop (never below a floor). Green and yellow change nothing.
    budget: Mapping[str, int] = field(default_factory=lambda: {"orange": 1, "red": 1})
    escalation: tuple[EscalationRule, ...] = DEFAULT_ESCALATION
    allowed_sources: Optional[tuple[str, ...]] = None
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    #: engine -> the weekly allowance in API-equivalent USD, set by the operator. Learned
    #: allowances live with the source's observed capacity, not here.
    weekly_allowance_usd: Mapping[str, float] = field(default_factory=dict)
    #: Jobs whose pin is HARD ("Always use, even when limited"). Every other job pin is a
    #: preference: it picks the source and model within the tier, and headroom rules and
    #: exhaustion can still move it (Decision 5).
    hard_pins: tuple[str, ...] = ()
    #: Frontier work may fall back to a local model. Off: Frontier goes subscription →
    #: metered frontier → a refusal in words (Decision 6).
    frontier_allows_local: bool = False
    version: int = 0

    @property
    def smart(self) -> bool:
        return self.mode == "smart"

    def targets(self, tier: str) -> tuple[Target, ...]:
        return tuple(self.tiers.get(tier) or ())

    def floor_for(self, *, kind: str, step: str, role: Optional[str], job: str,
                  task_kind: Optional[str]) -> tuple[Optional[str], list[Floor]]:
        hit = [f for f in self.floors
               if f.matches(kind=kind, step=step, role=role, job=job, task_kind=task_kind)]
        if not hit:
            return None, []
        top = max(hit, key=lambda f: rank(f.tier))
        return top.tier, hit

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tiers": {t: [x.to_dict() for x in self.targets(t)] for t in TIERS},
            "floors": [f.to_dict() for f in self.floors],
            "floor_preset": self.floor_preset,
            "headroom": [h.to_dict() for h in self.headroom],
            "thresholds": dict(self.thresholds),
            "budget": dict(self.budget),
            "escalation": [e.to_dict() for e in self.escalation],
            "allowed_sources": list(self.allowed_sources) if self.allowed_sources else None,
            "classifier": self.classifier.to_dict(),
            "weekly_allowance_usd": dict(self.weekly_allowance_usd),
            "hard_pins": list(self.hard_pins),
            "frontier_allows_local": self.frontier_allows_local,
            "version": self.version,
        }


# ── Parsing ────────────────────────────────────────────────────────────────────────────────────

def _target(value: Any, where: str) -> Target:
    if isinstance(value, str):
        engine, _, model = value.partition(":")
        value = {"engine": engine, "model": model or None}
    if not isinstance(value, Mapping) or not str(value.get("engine") or "").strip():
        raise PolicyError(f"{where}: each target needs an engine.")
    model = value.get("model")
    return Target(str(value["engine"]).strip().lower(),
                  str(model).strip() if model not in (None, "") else None)


def _tier(value: Any, where: str) -> str:
    tier = str(value or "").strip().lower()
    if tier not in TIERS:
        raise PolicyError(f"{where}: tier must be one of {', '.join(TIERS)}.")
    return tier


def from_dict(doc: Mapping[str, Any], *, base: Optional[RoutingPolicy] = None) -> RoutingPolicy:
    """A policy from its JSON document. Missing keys keep `base`'s (or the defaults') values,
    so a partial update is a merge, never a reset."""
    base = base or RoutingPolicy()
    if not isinstance(doc, Mapping):
        raise PolicyError("A routing policy is a JSON object.")
    mode = str(doc.get("mode", base.mode) or "fixed").strip().lower()
    if mode not in MODES:
        raise PolicyError("mode must be fixed or smart.")
    tiers = dict(base.tiers)
    if "tiers" in doc:
        raw = doc["tiers"]
        if not isinstance(raw, Mapping):
            raise PolicyError("tiers is an object of tier -> targets.")
        for name, targets in raw.items():
            tier = _tier(name, "tiers")
            if not isinstance(targets, (list, tuple)):
                targets = [targets]
            parsed = tuple(_target(t, f"tiers.{tier}") for t in targets if t)
            if tier == FRONTIER:
                for t in parsed:
                    found = model_tier(t.engine, t.model)
                    if found in (FAST, BALANCED):
                        raise PolicyError(
                            f"tiers.frontier: {t.model} is a {found} model, not a Frontier one. "
                            "Frontier is for judgment work — choose an Opus-class model.")
            if len(parsed) > 6:
                raise PolicyError(f"tiers.{tier}: at most 6 targets.")
            tiers[tier] = parsed
    floors = base.floors
    if "floors" in doc:
        floors = []
        for i, f in enumerate(doc["floors"] or []):
            if not isinstance(f, Mapping) or not isinstance(f.get("match"), Mapping) \
                    or not f["match"]:
                raise PolicyError(f"floors[{i}]: needs a match (step, role, job, kind…).")
            unknown = set(f["match"]) - {"step", "role", "job", "kind", "task_kind"}
            if unknown:
                raise PolicyError(f"floors[{i}]: cannot match on {', '.join(sorted(unknown))}.")
            floors.append(Floor(dict(f["match"]), _tier(f.get("tier"), f"floors[{i}]"),
                                str(f.get("label") or "")[:60]))
        floors = tuple(floors)
    headroom = base.headroom
    if "headroom" in doc:
        rules = []
        for i, h in enumerate(doc["headroom"] or []):
            when = str(h.get("when") or "near_limit")
            if when not in HEADROOM_STATES[1:]:
                raise PolicyError(f"headroom[{i}]: when is tight, near_limit or exhausted.")
            moved = tuple(_tier(t, f"headroom[{i}]") for t in (h.get("tiers") or (FAST, BALANCED)))
            if FRONTIER in moved and when != "exhausted":
                # Decision 6: frontier work is only moved by exhaustion, never to save headroom.
                raise PolicyError(f"headroom[{i}]: Frontier work keeps the subscription until "
                                  "it is exhausted; move Fast and Balanced only.")
            rules.append(HeadroomRule(str(h.get("source") or "claude_code").lower(), when, moved,
                                      str(h.get("to") or "openrouter").lower()))
        headroom = tuple(rules)
    thresholds = dict(base.thresholds)
    if "thresholds" in doc:
        for key in ("tight", "near_limit"):
            if key in (doc["thresholds"] or {}):
                value = float(doc["thresholds"][key])
                if not 0 < value < 1:
                    raise PolicyError(f"thresholds.{key} is a fraction between 0 and 1.")
                thresholds[key] = value
        if thresholds["near_limit"] >= thresholds["tight"]:
            raise PolicyError("thresholds: near_limit must be below tight.")
    budget = dict(base.budget)
    if "budget" in doc:
        budget = {}
        for state, drop in (doc["budget"] or {}).items():
            if state not in ("yellow", "orange", "red"):
                raise PolicyError("budget: states are yellow, orange and red.")
            drop = _int(drop, f"budget.{state}")
            if not 0 <= drop <= 2:
                raise PolicyError(f"budget.{state} drops 0, 1 or 2 tiers (not {drop}).")
            budget[state] = drop
    escalation = base.escalation
    if "escalation" in doc:
        rules = []
        for i, e in enumerate(doc["escalation"] or []):
            signal = str(e.get("signal") or "")
            if signal not in SIGNALS:
                raise PolicyError(f"escalation[{i}]: signal is one of {', '.join(SIGNALS)}.")
            to = str(e.get("to") or "up")
            if to != "up":
                to = _tier(to, f"escalation[{i}]")
            count = _int(e.get("count", 1), f"escalation[{i}].count")
            if not 1 <= count <= 9:
                raise PolicyError(f"escalation[{i}].count is 1–9 (not {count}).")
            rules.append(EscalationRule(signal, count,
                                        tuple(str(s) for s in (e.get("steps") or ())), to))
        escalation = tuple(rules)
    allowed = base.allowed_sources
    if "allowed_sources" in doc:
        raw = doc["allowed_sources"]
        if isinstance(raw, (list, tuple)) and not raw:
            raise PolicyError("allowed_sources: name at least one source, or clear the list "
                              "(null) to allow every connected source.")
        allowed = tuple(str(s).lower() for s in raw) if raw else None
    classifier = base.classifier
    if "classifier" in doc:
        c = doc["classifier"] or {}
        backend = str(c.get("backend", classifier.backend))
        if backend not in CLASSIFIER_BACKENDS:
            raise PolicyError(f"classifier.backend is one of {', '.join(CLASSIFIER_BACKENDS)}.")
        criteria = dict(classifier.criteria)
        for tier, text in (c.get("criteria") or {}).items():
            text = str(text or "").strip()
            if not text:
                raise PolicyError(f"classifier.criteria.{tier}: say what this tier is for.")
            criteria[_tier(tier, "classifier.criteria")] = text[:600]
        classifier = ClassifierConfig(
            backend=backend,
            jev_timeout_ms=_bounded(c.get("jev_timeout_ms", classifier.jev_timeout_ms),
                                    100, 5000, "classifier.jev_timeout_ms"),
            model_timeout_ms=_bounded(c.get("model_timeout_ms", classifier.model_timeout_ms),
                                      500, 30000, "classifier.model_timeout_ms"),
            model_below=_bounded(c.get("model_below", classifier.model_below), 0.0, 1.0,
                                 "classifier.model_below", number=float),
            model_fallback=bool(c.get("model_fallback", classifier.model_fallback)),
            criteria=criteria)
    allowance = dict(base.weekly_allowance_usd)
    if "weekly_allowance_usd" in doc:
        allowance = {str(k).lower(): float(v) for k, v in (doc["weekly_allowance_usd"] or {}).items()
                     if v not in (None, "") and float(v) > 0}
    hard_pins = base.hard_pins
    if "hard_pins" in doc:
        jobs = ("build", "review", "chat")
        hard_pins = tuple(str(j) for j in (doc["hard_pins"] or ()))
        unknown = [j for j in hard_pins if j not in jobs]
        if unknown:
            raise PolicyError(f"hard_pins: {', '.join(unknown)} is not a job ({', '.join(jobs)}).")
    frontier_local = bool(doc.get("frontier_allows_local", base.frontier_allows_local))
    return RoutingPolicy(
        hard_pins=hard_pins, frontier_allows_local=frontier_local,
        mode=mode, tiers=tiers, floors=floors,
        floor_preset=str(doc.get("floor_preset", base.floor_preset) or "custom")[:40],
        headroom=headroom, thresholds=thresholds, budget=budget, escalation=escalation,
        allowed_sources=allowed, classifier=classifier, weekly_allowance_usd=allowance,
        version=int(doc.get("version", base.version) or 0))


def _int(value: Any, where: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PolicyError(f"{where} is a whole number.") from None
    if number != int(number):
        raise PolicyError(f"{where} is a whole number (not {value}).")
    return int(number)


def _bounded(value: Any, low: float, high: float, where: str, *, number=int) -> Any:
    """A value inside its bounds, or a PolicyError that says so — never a silent clamp."""
    try:
        got = number(float(value)) if number is int else float(value)
    except (TypeError, ValueError):
        raise PolicyError(f"{where} is a number between {low} and {high}.") from None
    if not low <= got <= high:
        raise PolicyError(f"{where} is between {low} and {high} (not {value}).")
    return got


# ── Presets: the pipeline profiles, as floors ─────────────────────────────────────────────────

#: Floors every preset carries: work whose need does not depend on the profile.
BASE_FLOORS: tuple[Floor, ...] = (
    Floor({"step": "panel_facilitator"}, FRONTIER),
    Floor({"step": "research_synthesis"}, BALANCED),
    Floor({"step": ["spec", "plan", "review_plan"]}, BALANCED, "Spec and plan"),
    Floor({"step": "check_tests"}, BALANCED),
)


def _step_tier(profile: Mapping[str, Any], step: str) -> tuple[Optional[str], Optional[Target]]:
    """A profile step's value as (the tier it demands, the explicit pin it names). `strong` is
    Frontier; a pin to a model is that model's tier — `claude_code:opus` is a Frontier floor
    exactly as `strong` is (classified by tier, not by string)."""
    from ..pipeline_profiles import _parse_route

    routing = profile.get("model") or {}
    value = routing.get(step, routing.get("default", "strong"))
    parsed = _parse_route(value)
    if parsed.get("model_class") == "strong":
        return FRONTIER, None
    if parsed.get("model_class") == "low_cost":
        return None, None
    pin = Target(parsed["engine"], parsed.get("model")) if parsed.get("engine") else None
    tier = model_tier(parsed.get("engine") or "", parsed.get("model"))
    return (FRONTIER if tier == FRONTIER else None), pin


def profile_preferences(profile: Optional[Mapping[str, Any]]) -> dict[str, Target]:
    """Steps the profile pins to an explicit source/model: on Smart these are preferences
    within the tier (the floor still wins), never ignored."""
    out: dict[str, Target] = {}
    for step in BUILD_STEPS:
        _tier, pin = _step_tier(profile or {}, step)
        if pin is not None:
            out[step] = pin
    return out


def floors_from_profile(profile: Mapping[str, Any]) -> tuple[Floor, ...]:
    """A profile's Frontier-class steps ARE its floors (Decision 4): `strong`, or a pin to a
    Frontier model. `lean` has none, `balanced` floors the three adversarial seats, `strict`
    every stage, `vag` every stage but the junior ones. A custom profile converts the same way."""
    strong = [s for s in BUILD_STEPS if _step_tier(profile, s)[0] == FRONTIER]
    if len(strong) == len(BUILD_STEPS):
        floors: list[Floor] = [Floor({"kind": "build_stage"}, FRONTIER)]
    else:
        floors = [Floor({"step": s}, FRONTIER) for s in strong]
    every_stage = any(f.match.get("kind") == "build_stage" for f in floors)
    covered = set(BUILD_STEPS) if every_stage else set(strong)

    def redundant(floor: Floor) -> bool:
        steps = floor.match.get("step")
        steps = steps if isinstance(steps, (list, tuple)) else [steps]
        return all(s in covered for s in steps)

    return tuple(floors) + tuple(f for f in BASE_FLOORS if not redundant(f))


def preset_floors(name: str) -> tuple[Floor, ...]:
    from ..pipeline_profiles import PROFILES, resolve_profile

    if name not in PROFILES:
        raise PolicyError(f"Unknown floor preset '{name}'.")
    return floors_from_profile(resolve_profile(name))


# ── Seeding a policy from what an install already has (migration) ─────────────────────────────

def tier_model(engine: str, tier: str) -> Optional[str]:
    if engine == "openrouter":
        return OPENROUTER_FALLBACK_MODELS[tier]
    return (ENGINE_TIER_MODELS.get(engine) or {}).get(tier)


def seed(*, settings: Any, connected: Mapping[str, Mapping[str, Any]], build_engine: str,
         profile: Optional[Mapping[str, Any]], mode: str = "fixed",
         openrouter_models: Optional[Mapping[str, str]] = None) -> RoutingPolicy:
    """The policy an install starts Smart routing with, built from its existing settings so the
    first route on Smart resembles the last one on Fixed (docs: "Backward compatibility")."""
    build = (build_engine or "claude_code").lower()

    def lane(engine_attr: str, model_attr: str, tier: str) -> Target:
        engine = str(getattr(settings, engine_attr, "") or "").strip().lower() or build
        model = str(getattr(settings, model_attr, "") or "").strip() or tier_model(engine, tier)
        return Target(engine, model)

    tiers: dict[str, list[Target]] = {
        FAST: [Target(build, tier_model(build, FAST))],
        BALANCED: [lane("code_agent_low_cost_engine", "code_agent_low_cost_model", BALANCED)],
        FRONTIER: [lane("code_agent_strong_engine", "code_agent_strong_model", FRONTIER)],
    }
    headroom: tuple[HeadroomRule, ...] = ()
    if "openrouter" in connected:
        picks = dict(OPENROUTER_FALLBACK_MODELS)
        picks.update({k: v for k, v in (openrouter_models or {}).items() if v})
        for tier in TIERS:
            if all(t.engine != "openrouter" for t in tiers[tier]):
                tiers[tier].append(Target("openrouter", picks[tier]))
        subs = [e for e in (build, *(t.engine for ts in tiers.values() for t in ts))
                if e in SUBSCRIPTION_ENGINES]
        if subs:
            headroom = (HeadroomRule(subs[0], "near_limit", (FAST, BALANCED), "openrouter"),)
    preset = str((profile or {}).get("name") or "balanced")
    floors = floors_from_profile(profile) if profile else preset_floors("balanced")
    return RoutingPolicy(mode=mode, tiers={k: tuple(v) for k, v in tiers.items()},
                         floors=floors, floor_preset=preset, headroom=headroom)
