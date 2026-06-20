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
