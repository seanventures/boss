"""Pipeline cost/rigor profiles — the dial that scales effort to the work.

A profile picks how much of the governed pipeline runs and which models run it, so a prototype
runs low-cost and a production change runs strict. AI budget is finite; this turns BOSS's built-in
efficiency into a customer-tunable setting instead of a fixed cost.

Phase 1: profiles + risk-tier inference + per-step cost accounting. The model-routing and
caching/parallel hints are recorded now and applied by the runtime in Phase 2.
"""
from __future__ import annotations

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


def sensitive_markers_in(spec: dict) -> list[str]:
    """Which sensitive markers a task's text matches — the concrete reason it may need review."""
    haystack = " ".join(
        [
            str(spec.get("title", "")),
            str(spec.get("instructions", "")),
            " ".join(spec.get("acceptance_criteria", []) or []),
            str(spec.get("repo", "")),
        ]
    ).lower()
    return [m for m in SENSITIVE_MARKERS if m in haystack]


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
        "model": {
            "default": "low_cost",
            "implement": "strong",
            "review_code": "strong",
            "security_check": "strong",
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
    haystack = " ".join(
        [
            str(spec.get("title", "")),
            str(spec.get("instructions", "")),
            " ".join(spec.get("acceptance_criteria", []) or []),
            str(spec.get("repo", "")),
        ]
    ).lower()
    sensitive = any(m in haystack for m in SENSITIVE_MARKERS)
    explicit = spec.get("risk_tier")
    if isinstance(explicit, int) and 0 <= explicit <= 3:
        tier = explicit
    else:
        tier = 3 if sensitive else 2
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
