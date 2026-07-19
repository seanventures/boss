"""APP-6 (#96) — hybrid frontier + OSS routing for the design axis.

Coherence (taste, hierarchy, on-domain copy) is decided in a few stages; correctness fill-in is not.
Spend frontier tokens where design is *decided* and cheap OSS tokens on the verified fill-in (spec 09
§3.6). This is the **Govern** side of the design axis: coherence becomes an *affordable, metered* cost
rather than "run the whole loop on a frontier model."

- **Design-axis steps** (scaffold theming, the product_design role, the spec/plan taste calls, the
  visual-gate vision judge) → **frontier** tier.
- **Fill-in steps** (implement, repair, write_tests) → **cheap OSS** tier — the bar there is "make the
  frozen design + tests pass," which the existing VAG correctness loop already handles well.
- The design pass is a **distinct budgeted line item**: `design_pass_plan` caps how many frontier design
  passes a task gets and says when to fall back, so a runaway can't burn the whole budget on taste.

Routing is profile-driven and overridable per project/task — this composes with the existing
`route_for_step` precedence (a task's `model_config` still wins over these defaults).
"""
from __future__ import annotations

from typing import Any, Optional

# Where taste/hierarchy/copy are decided — always worth a frontier model.
DESIGN_AXIS_STEPS = frozenset({
    "spec", "plan", "review_plan", "product_design", "scaffold_theme", "visual_judge",
})
# Correctness fill-in against a frozen design + tests — cheap OSS is enough.
FILL_IN_STEPS = frozenset({"implement", "repair", "write_tests"})


def is_design_axis(step: str) -> bool:
    return step in DESIGN_AXIS_STEPS


def resolve_tier(step: str) -> str:
    """The tier a step belongs to: ``frontier`` (design axis), ``oss`` (fill-in), or ``default``."""
    if step in DESIGN_AXIS_STEPS:
        return "frontier"
    if step in FILL_IN_STEPS:
        return "oss"
    return "default"


def resolve_engine(
    step: str, *, frontier_engine: str, oss_engine: str, default_engine: str,
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """Resolve the engine for a step: a per-step override wins, else the step's tier engine. Empty
    tier engines fall back to the default so routing is always a safe no-op when unconfigured."""
    if overrides and step in overrides and overrides[step]:
        return overrides[step]
    tier = resolve_tier(step)
    if tier == "frontier":
        return frontier_engine or default_engine
    if tier == "oss":
        return oss_engine or default_engine
    return default_engine


def design_pass_plan(
    total_budget_usd_micros: int, *, design_fraction: float = 0.4,
    est_design_pass_cost: int = 300_000, max_design_passes: int = 2,
) -> dict[str, Any]:
    """Meter the design pass as a distinct line item: a slice of the task budget reserved for frontier
    design, and a hard cap on how many frontier passes it buys (whichever is smaller)."""
    envelope = int(max(0, total_budget_usd_micros) * max(0.0, min(1.0, design_fraction)))
    affordable = envelope // est_design_pass_cost if est_design_pass_cost > 0 else 0
    passes = max(0, min(max_design_passes, affordable))
    return {
        "design_envelope_usd_micros": envelope,
        "max_design_passes": passes,
        "hard_cap": max_design_passes,
        "est_design_pass_cost": est_design_pass_cost,
        "note": (f"reserve {envelope} micros for up to {passes} frontier design pass(es); "
                 f"fill-in runs on the OSS tier"),
    }


def should_run_design_pass(passes_used: int, design_spent_usd_micros: int, plan: dict[str, Any]) -> dict[str, Any]:
    """Governor decision before another frontier design pass: allow only if under the pass cap AND the
    design envelope can afford another. Otherwise fall back to the OSS tier for the rest of the task."""
    if passes_used >= plan["max_design_passes"]:
        return {"allow": False, "fallback": "oss", "reason": "design-pass cap reached"}
    remaining = plan["design_envelope_usd_micros"] - design_spent_usd_micros
    if remaining < plan["est_design_pass_cost"]:
        return {"allow": False, "fallback": "oss", "reason": "design envelope exhausted"}
    return {"allow": True, "reason": "under cap and within the design envelope",
            "remaining_envelope": remaining}
