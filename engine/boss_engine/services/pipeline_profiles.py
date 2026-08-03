"""Pipeline cost/rigor profiles — the dial that scales effort to the work.

A profile picks how much of the governed pipeline runs and which models run it, so a prototype
runs low-cost and a production change runs strict. AI budget is finite; this turns BOSS's built-in
efficiency into a customer-tunable setting instead of a fixed cost.

Phase 1: profiles + risk-tier inference + per-step cost accounting. The model-routing and
caching/parallel hints are recorded now and applied by the runtime in Phase 2.
"""
from __future__ import annotations

import re

from typing import Optional

# Markers that force a change to high risk regardless of the profile floor.
SENSITIVE_MARKERS = (
    "auth", "login", "password", "credential", "payment", "billing", "invoice",
    "secret", "token", "api key", "encrypt", "crypto", "migration", "migrate",
    "schema change", "deploy", "infra", "permission", "access control",
)

# Human-readable names for the markers — so a review gate can explain WHY, in plain terms.
_MARKER_HUMAN = {
    "auth": "authentication", "login": "login / user accounts", "password": "passwords",
    "credential": "credentials", "payment": "payments", "billing": "billing",
    "invoice": "invoicing", "secret": "secrets", "token": "tokens / API keys",
    "api key": "API keys", "encrypt": "encryption", "crypto": "cryptography",
    "migration": "a data migration", "migrate": "a data migration",
    "schema change": "a schema change", "deploy": "deployment", "infra": "infrastructure",
    "permission": "permissions", "access control": "access control",
}


# Text BOSS itself appends to every task. It must NEVER feed the sensitivity classifier: the
# DEPLOYABILITY note contains the word "deployability", which substring-matched the "deploy"
# marker, flagged EVERY task as sensitive, and pinned it to tier >= 2 — overriding even an
# explicit risk_tier from the caller. BOSS was reading its own boilerplate as a risk signal.
_BOSS_BOILERPLATE_STARTS = (
    "deployability (required for any network service)",
    "=== iteration feedback",
)


def _task_haystack(spec: dict) -> str:
    """The task's OWN words, lowercased, with BOSS-appended boilerplate removed."""
    text = " ".join(
        [
            str(spec.get("title", "")),
            str(spec.get("instructions", "")),
            " ".join(spec.get("acceptance_criteria", []) or []),
            str(spec.get("repo", "")),
        ]
    ).lower()
    for marker in _BOSS_BOILERPLATE_STARTS:
        idx = text.find(marker)
        while idx != -1:
            end = text.find("\n\n", idx)
            text = text[:idx] + (text[end:] if end != -1 else "")
            idx = text.find(marker)
    return text


def _matches(marker: str, haystack: str) -> bool:
    """Whole-word match. Substring matching is what turned "deployability" into "deploy",
    and would equally turn "authentic"/"authorised" into "auth" or "tokenizer" into "token"."""
    return re.search(r"\b" + re.escape(marker) + r"\b", haystack) is not None


def sensitive_markers_in(spec: dict) -> list[str]:
    """Which sensitive markers a task's text matches — the concrete reason it may need review."""
    haystack = _task_haystack(spec)
    return [m for m in SENSITIVE_MARKERS if _matches(m, haystack)]


# Phrases that say "modify what is already there" rather than "build something new". A change
# bounded by existing code is inherently lower-risk than a greenfield build.
_INCREMENTAL_MARKERS = (
    "existing", "already", "current", "adjust", "tweak", "update the", "change the",
    "modify", "rename", "fix ", "small", "surgical", "minor", "instead of",
    "keep the", "preserve", "do not restructure", "don't restructure",
)
_GREENFIELD_MARKERS = (
    "build a", "build an", "create a new", "from scratch", "new app", "new service",
    "scaffold", "greenfield", "rewrite",
)


# Presentation-layer work: no new behaviour to specify, so a separate test-authoring pass buys
# little and costs a whole cold-start session. Behaviour words below veto this.
_TRIVIAL_MARKERS = (
    "css", "styling", "stylesheet", "colour", "color", "font", "spacing", "padding",
    "margin", "layout tweak", "copy change", "wording", "label", "typo", "rename",
    "placeholder", "icon", "logo", "theme", "dark mode", "responsive",
)
# If any of these appear the change carries real behaviour and must NOT drop to tier 0,
# however small it sounds.
_BEHAVIOUR_MARKERS = (
    "logic", "algorithm", "calculate", "score", "scoring", "validate", "validation",
    "state", "persist", "storage", "api", "endpoint", "database", "query", "concurren",
    "race", "async", "performance", "cache", "generate", "solver", "rule",
)


def _is_trivial_change(haystack: str, spec: dict) -> bool:
    """Presentation-only edits to code that already exists — the one-session lane."""
    if not _is_incremental_change(haystack, spec):
        return False
    if any(m in haystack for m in _BEHAVIOUR_MARKERS):
        return False
    return any(m in haystack for m in _TRIVIAL_MARKERS)


def _is_incremental_change(haystack: str, spec: dict) -> bool:
    """True when the task modifies code that already exists (bounded blast radius).

    Signals, in order of trust:
      1. the run is explicitly extending an existing app (set by the caller/pipeline),
      2. it builds on a branch other than the default (the prior work is already there),
      3. the wording describes modifying existing behaviour and not a greenfield build.
    """
    if spec.get("web_app_build") or spec.get("extended_existing_app"):
        return True
    base = str(spec.get("base_branch", "") or "")
    if base and base not in ("main", "master"):
        return True
    if any(m in haystack for m in _GREENFIELD_MARKERS):
        return False
    return any(m in haystack for m in _INCREMENTAL_MARKERS)


def plan_gate_explanation(markers: list[str], *, plan_ok: bool) -> str:
    """A specific, plain-language explanation of WHY a plan needs human sign-off and WHAT to
    check — so the operator approves with a reason, not blind. Uses the actual matched markers
    + whether the automated plan review flagged concerns."""
    named = sorted({_MARKER_HUMAN.get(m, m) for m in markers})
    if named:
        why = (
            "This task touches " + ", ".join(named) + " — sensitive areas where a wrong "
            "approach can expose user data, accounts, or money, and is costly to unwind once "
            "the code ships. That's why BOSS wants your sign-off on the approach before it "
            "writes any code."
        )
    else:
        why = (
            "This is a high-risk change, so BOSS wants you to confirm the approach before it "
            "writes any code."
        )
    what = (
        " An automated plan review flagged concerns — read the plan review below and decide "
        "whether the approach is sound before approving."
        if not plan_ok
        else " Skim the technical plan below and confirm the approach is right, then approve."
    )
    return why + what

# Each profile clamps the inferred risk tier, hints a model per step (low_cost|strong), and toggles
# the efficiency levers wired in Phase 2.
PROFILES: dict[str, dict] = {
    "lean": {
        "label": "Lean — lowest cost, for prototypes and throwaway work",
        "tier_floor": 0,
        "tier_cap": 1,
        "model": {"default": "low_cost"},
        "caching": True,
        "parallel_checks": True,
    },
    "balanced": {
        "label": "Balanced — the default for normal work",
        "tier_floor": 0,
        "tier_cap": 3,
        # VAG economics with a floor of 0: the CHEAP lane builds, the FRONTIER lane VERIFIES.
        # implement was on `strong` and it is by far the most expensive stage — measured on the
        # same feature: opus 66 turns/877s/$6.22 (and 102 turns/$11.86 on a rebuild) vs sonnet
        # 41 turns/656s/$3.98. The quality bar is held by the verifiers, which stay frontier:
        # a cheap implementation still has to survive review_code, security and consistency.
        "model": {
            "default": "low_cost",
            "review_code": "strong",
            "security_check": "strong",
            "consistency_check": "strong",
        },
        "caching": True,
        "parallel_checks": True,
    },
    "strict": {
        "label": "Strict — production / enterprise grade",
        "tier_floor": 2,
        "tier_cap": 3,
        "model": {"default": "strong"},
        "caching": True,
        "parallel_checks": True,
    },
    # VAG (docs/product/BOSS_VERIFICATION_AMPLIFIED_GENERATION.md): the junior/senior
    # split — cheap models do the groundwork, frontier models write the spec/tests and
    # verify. Classes degrade to lane defaults until the deployment binds them, e.g.
    # BOSS_CODE_AGENT_LOW_COST_ENGINE=native + BOSS_CODE_AGENT_LOW_COST_MODEL=deepseek-chat.
    "vag": {
        "label": "VAG junior/senior — cheap build + test author, frontier design + verification",
        "tier_floor": 2,
        "tier_cap": 3,
        # Economics decision (2026-07-30, measured): the low-cost lane AUTHORS the tests
        # too — the frontier lane still judges them (review_code, consistency, the
        # mutation gate), so the bar stays frontier-graded without paying frontier rates
        # for authoring. Measured on the same task: strong-lane write_tests was 34
        # turns / $4.96; low-cost was 7 turns / $0.42 with the same gate outcome.
        "model": {
            "default": "strong",
            "implement": "low_cost",
            "write_tests": "low_cost",
            "standards": "low_cost",
        },
        "caching": True,
        "parallel_checks": True,
    },
}
DEFAULT_PROFILE = "balanced"


def resolve_profile(name: Optional[str]) -> dict:
    """Return the named profile (with its name), falling back to the default."""
    key = (name or DEFAULT_PROFILE).lower().strip()
    if key not in PROFILES:
        key = DEFAULT_PROFILE
    return {"name": key, **PROFILES[key]}


def infer_risk_tier(spec: dict, profile: dict) -> int:
    """Pick a risk tier for this task, then clamp it to the profile's range.

    Order: an explicit `risk_tier` on the task wins; otherwise infer — sensitive surfaces
    (auth/payments/secrets/migrations/deploys) are high, everything else is standard.
    """
    haystack = _task_haystack(spec)
    sensitive = any(_matches(m, haystack) for m in SENSITIVE_MARKERS)
    explicit = spec.get("risk_tier")
    if isinstance(explicit, int) and 0 <= explicit <= 3:
        tier = explicit
    elif sensitive:
        tier = 3
    else:
        # Rigor must track the SIZE of the change, not only its sensitivity. This used to be
        # `tier = 2` for everything non-sensitive, so inference could never return below 2 and
        # EVERY task — a one-line CSS tweak included — ran the full 10-session pipeline
        # (design + write_tests + implement + a 4-check review panel, ~39 min). That is the
        # single reason a small feature cost 49 minutes against ~3 for a standard coding tool.
        #
        # A change to code that ALREADY EXISTS is bounded by that code: the blast radius is
        # small, the reviewers have a working baseline to compare against, and the tests that
        # guard it are already written. A NEW build has no such floor, so it keeps tier 2.
        if _is_trivial_change(haystack, spec):
            # One session: the implementer writes the code AND its tests, like a developer
            # using a normal coding tool. Safe only because `existing_tests_intact` and the
            # authoritative full-suite run are floors that hold at every tier — a trivial
            # change still cannot weaken an existing test or ship a red suite.
            tier = 0
        elif _is_incremental_change(haystack, spec):
            tier = 1
        else:
            tier = 2
    clamped = max(int(profile.get("tier_floor", 0)), min(int(profile.get("tier_cap", 3)), tier))
    # Safety override: a sensitive change never runs below standard rigor, even in a low-cost
    # profile — cost caps must not silently de-risk auth/payments/secrets/migrations/deploys.
    if sensitive:
        clamped = max(clamped, 2)
    return clamped


def model_for_step(profile: dict, step: str) -> str:
    """Which model class (low_cost|strong) a step should use under this profile."""
    routing = profile.get("model", {}) or {}
    return routing.get(step, routing.get("default", "strong"))


# --- Per-step (engine, model) routing — VAG Milestone B2. ---------------------------- #
# Adversary steps run on the review lane (settings.code_review_engine); everything else
# on the build lane. Matches the pre-existing agent/reviewer split in the governed service.
REVIEW_LANE_STEPS = frozenset({"review_code", "security_check", "consistency_check", "check_tests"})


def _parse_route(value) -> dict:
    """Parse one routing value into {"engine", "model", "model_class"} (each Optional).

    Accepted forms:
      "low_cost" | "strong"                      -> class routing (today's vocabulary)
      "engine:model" e.g. "native:deepseek-chat" -> explicit pair
      "engine:"      e.g. "claude_code:"         -> engine with its default model
      {"engine": ..., "model": ...}              -> dict form (JSONB-native)
      any other bare string                      -> model name on the step's lane engine
      anything else                              -> safe fallback to class "strong"
    """
    if isinstance(value, dict):
        eng = str(value.get("engine") or "").strip().lower() or None
        mdl = str(value.get("model") or "").strip() or None
        if eng or mdl:
            return {"engine": eng, "model": mdl, "model_class": None}
        return {"engine": None, "model": None, "model_class": "strong"}
    if isinstance(value, str):
        v = value.strip()
        if v in ("low_cost", "strong"):
            return {"engine": None, "model": None, "model_class": v}
        if ":" in v:
            eng, _, mdl = v.partition(":")
            eng = eng.strip().lower() or None
            mdl = mdl.strip() or None
            if eng or mdl:
                return {"engine": eng, "model": mdl, "model_class": None}
        elif v:
            return {"engine": None, "model": v, "model_class": None}
    return {"engine": None, "model": None, "model_class": "strong"}


def route_for_step(profile: dict, step: str, model_config: Optional[dict] = None) -> dict:
    """Resolve a step's route: {"engine", "model", "model_class", "lane"}.

    Precedence: spec model_config steps[step] > model_config default > profile model[step]
    > profile model default > class "strong". engine/model may be None — the caller
    materializes classes via settings and falls back to the lane's default engine.
    """
    mc = model_config or {}
    value = (mc.get("steps") or {}).get(step)
    if value is None:
        value = mc.get("default")
    if value is None:
        routing = profile.get("model", {}) or {}
        value = routing.get(step, routing.get("default", "strong"))
    route = _parse_route(value)
    route["lane"] = "review" if step in REVIEW_LANE_STEPS else "build"
    return route


def apply_budget_pressure(route: dict, step: str, budget_state: str) -> tuple[dict, bool]:
    """Auto-lite mode, actually enforced (BOSS-280). At ORANGE/RED budget state, class-routed
    BUILD-lane "strong" steps degrade to "low_cost". Three invariants:
      - verification NEVER degrades (review-lane steps keep their route — the VAG principle:
        saving money by weakening the checker is how false passes happen);
      - an EXPLICIT operator pin (engine/model named in model_config) is never overridden;
      - green/yellow states change nothing.
    Returns (route, degraded)."""
    if budget_state not in ("orange", "red"):
        return route, False
    if route.get("lane") == "review" or step in REVIEW_LANE_STEPS:
        return route, False
    if route.get("model_class") == "strong" and not route.get("engine") and not route.get("model"):
        return {**route, "model_class": "low_cost"}, True
    return route, False


def materialize_route(
    route: dict, *, build_engine: str, review_engine: str,
    strong_engine: str = "", low_cost_engine: str = "",
    strong_model: str = "", low_cost_model: str = "",
) -> tuple[str, Optional[str]]:
    """Turn a route_for_step result into a concrete (engine_name, model).

    Explicit engine wins; else the class's bound engine (BOSS_CODE_AGENT_*_ENGINE); else
    the step's lane default. Model: explicit wins; else the class model; None = the
    engine's own default. With no class bindings and only low_cost/strong values this is
    EXACTLY the pre-B2 behavior (lane engine + strong/low-cost model)."""
    cls = route.get("model_class")
    class_engine = ((strong_engine if cls == "strong" else
                     low_cost_engine if cls == "low_cost" else "") or "").lower() or None
    class_model = (strong_model if cls == "strong" else
                   low_cost_model if cls == "low_cost" else "") or None
    lane_default = review_engine if route.get("lane") == "review" else build_engine
    engine_name = route.get("engine") or class_engine or lane_default
    model = route.get("model") or class_model
    return engine_name, model
