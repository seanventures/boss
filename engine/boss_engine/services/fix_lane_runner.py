"""The runnable half of the fix lane: which stages it skips, what its review panel looks
like, and when it must give up and hand the work to the normal pipeline.

``fix_lane_triage.classify`` decides the lane ONCE, before any code exists, from the
caller's estimate of the change. Everything here is the second half — once the fix lane
has actually written code, these functions gate the pipeline's existing stage code (never
re-implement a stage; only decide whether it runs) and re-check the SAME caps against the
real diff, so a request that looked small enough at triage time but wasn't still stops
before it ships an over-budget change in the lane meant only for small ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import fix_lane_triage

#: The normal pipeline's design chain — never run for a fix-lane request (acceptance
#: criterion #2: red -> green -> verify+review -> PR only).
SKIPPED_STAGES = ("standards", "spec", "plan", "plan_review", "consistency_check", "design")

#: The fix lane's review panel is ONE combined correctness+security seat, not the full
#: adversarial panel (review_code + security_check + regression + consistency_check) —
#: the other seats exist to catch design drift and cross-cutting inconsistency, and the
#: fix lane never runs a design stage for them to drift from.
FIX_LANE_REVIEW_SEATS = frozenset({"review_code"})


def skips_design(lane: str) -> bool:
    """True when ``lane`` never runs the design chain (standards/spec/plan/plan-review)."""
    return lane == fix_lane_triage.LANE_FIX


def review_seats_for(lane: str, default_seats: Iterable[str]) -> frozenset[str]:
    """Which review-panel seats run for ``lane``.

    The fix lane narrows the panel to :data:`FIX_LANE_REVIEW_SEATS`; every other lane keeps
    whatever seats the caller already enabled.
    """
    if lane == fix_lane_triage.LANE_FIX:
        return FIX_LANE_REVIEW_SEATS
    return frozenset(default_seats)


def combined_review_prompt(review_prompt: str, security_prompt: str) -> str:
    """The fix lane's one reviewer judges correctness AND security in a single pass."""
    return f"{review_prompt}\n\n{security_prompt}"


@dataclass(frozen=True)
class EscalationVerdict:
    """Whether a fix-lane run in progress must stop and hand off to the full pipeline."""

    escalated: bool
    reason: Optional[str] = None
    carried_test: Optional[Any] = None


def check_escalation(
    spec: dict,
    *,
    files: int,
    lines: int,
    touched_paths: list[str],
    carried_test: Optional[Any] = None,
) -> EscalationVerdict:
    """Re-run triage against the REAL diff once the fix lane has written it.

    Acceptance criterion #3: a change can pass the pre-flight estimate and still exceed
    >3 files, >150 lines, or touch a protected area once the agent's actual change is in
    hand. This calls the exact same :func:`fix_lane_triage.classify` the pre-flight
    decision used (so "too big" and "protected" can never drift between the two checks)
    and, if it now routes to the full lane, carries ``carried_test`` (the red test the fix
    lane already wrote) forward rather than losing it.
    """
    verdict = fix_lane_triage.classify(
        spec,
        diff_estimate={"files": files, "lines": lines, "touched_paths": touched_paths},
    )
    if verdict.lane == fix_lane_triage.LANE_FULL:
        return EscalationVerdict(escalated=True, reason=verdict.reason, carried_test=carried_test)
    return EscalationVerdict(escalated=False)


def per_step_seconds(stage_timings: Iterable[dict]) -> dict[str, float]:
    """``{stage -> seconds}`` from a run's recorded stage/phase timing entries.

    Acceptance criterion #4 (record per-step seconds: warm start, red, green,
    verify+review, PR) is satisfied by the pipeline's EXISTING ``phase_timings`` /
    ``stage_timings`` instrumentation, which already runs for every lane — this just
    reads it back in the shape a caller wants (seconds, keyed by stage), so nothing new
    needs recording for the fix lane specifically.
    """
    out: dict[str, float] = {}
    for entry in stage_timings or ():
        stage = entry.get("stage") or entry.get("phase")
        if not stage:
            continue
        ms = entry.get("total_ms")
        if ms is None:
            ms = entry.get("duration_ms") or 0
        out[stage] = out.get(stage, 0.0) + (float(ms) / 1000.0)
    return out
