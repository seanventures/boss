"""Tiers and the step vocabulary the router speaks (docs/product/MODEL_ROUTER.md, "Tiers").

A tier names what the work NEEDS, not a model: models churn every quarter, the need does not.
"""
from __future__ import annotations

from typing import Iterable, Optional

FAST = "fast"
BALANCED = "balanced"
FRONTIER = "frontier"
TIERS: tuple[str, ...] = (FAST, BALANCED, FRONTIER)

TIER_LABELS = {FAST: "Fast", BALANCED: "Balanced", FRONTIER: "Frontier"}

#: The pipeline profiles' two classes, as tiers. Fast is new: nothing legacy maps to it.
LEGACY_CLASS = {"low_cost": BALANCED, "strong": FRONTIER}


def rank(tier: Optional[str]) -> int:
    try:
        return TIERS.index(str(tier or ""))
    except ValueError:
        return 1  # an unknown tier is read as Balanced, never as Fast


def clamp(tier: str) -> str:
    return tier if tier in TIERS else BALANCED


def up(tier: str, steps: int = 1) -> str:
    return TIERS[min(len(TIERS) - 1, rank(tier) + max(0, steps))]


def down(tier: str, steps: int = 1) -> str:
    return TIERS[max(0, rank(tier) - max(0, steps))]


def highest(tiers: Iterable[Optional[str]]) -> Optional[str]:
    known = [t for t in tiers if t in TIERS]
    return max(known, key=rank) if known else None


def at_least(tier: str, floor: Optional[str]) -> str:
    return tier if not floor or rank(tier) >= rank(floor) else floor


# ── Steps ──────────────────────────────────────────────────────────────────────────────────────
#
# A step is WHERE a model call happens. The router's floors, priors and escalation rules are
# keyed by these names; the governed pipeline's stage names are used as they are.

#: Human names, for floors and "Why this model".
STEP_LABELS: dict[str, str] = {
    "standards": "Standards", "spec": "Spec", "plan": "Plan", "review_plan": "Plan review",
    "write_tests": "Write tests", "implement": "Implement", "solo": "Implement",
    "repair": "Repair", "quality": "Quality pass",
    "review_code": "Adversarial review", "security_check": "Security check",
    "consistency_check": "Consistency check", "check_tests": "Test check",
    "panel_facilitator": "Panel facilitator", "panel_member": "Panelist",
    "research_synthesis": "Research synthesis", "chat": "Chat", "decision": "Decision",
    "agent": "Agent step", "docs": "Docs", "writing": "Writing",
}

#: Steps whose ONLY job is to judge someone else's work — adversarial seats.
REVIEW_STEPS = frozenset({"review_code", "security_check", "consistency_check", "check_tests"})

#: Steps that write the code — what tests_failed / changes_requested escalate.
CODING_STEPS = frozenset({"implement", "solo", "repair", "quality"})

#: Mechanical steps: one tier below the task's own difficulty (never below Fast).
MECHANICAL_STEPS = frozenset({"standards", "docs"})


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step.replace("_", " ").capitalize() if step else "Model call")


#: Which "Who does what" job a step belongs to — a job pin applies to its steps.
def job_for(kind: str, step: str) -> str:
    if kind == "build_stage":
        return "review" if step in REVIEW_STEPS else "build"
    if kind == "decision":
        return "decisions"
    return "chat"
