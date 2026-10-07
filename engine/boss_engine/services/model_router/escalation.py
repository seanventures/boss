"""(d) Escalation — start at the lowest tier the policy allows, move up on evidence, at the NEXT
boundary (docs/product/MODEL_ROUTER.md, "(d) Escalation").

The ledger counts named signals per step during one unit of work (a build, a workflow run). The
router asks it, at each boundary, how far a step has been pushed up. It never changes a running
session: a resumed repair keeps its model, which is what keeps the prompt cache warm.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .policy import EscalationRule
from .tiers import TIERS, rank, up


@dataclass(frozen=True)
class Escalation:
    tier: str
    signal: str
    count: int
    rule: EscalationRule

    def describe(self) -> str:
        what = {"tests_failed": "tests failed", "no_output": "no output",
                "changes_requested": "reviewers asked for changes",
                "gate_failed": "a gate failed", "final_attempt": "final attempt"}
        times = f" ×{self.count}" if self.count > 1 else ""
        return f"{what.get(self.signal, self.signal)}{times} → {self.tier.capitalize()}"


class EscalationLedger:
    """Signals seen so far in one unit. Thread-safe: the review panel runs seats in threads."""

    def __init__(self, seed: Optional[dict[str, dict[str, int]]] = None):
        self._counts: dict[str, dict[str, int]] = {
            step: dict(signals) for step, signals in (seed or {}).items()}
        self._lock = threading.Lock()

    def record(self, step: str, signal: str, n: int = 1) -> int:
        with self._lock:
            per = self._counts.setdefault(step, {})
            per[signal] = per.get(signal, 0) + max(0, n)
            return per[signal]

    def count(self, step: str, signal: str) -> int:
        with self._lock:
            return self._counts.get(step, {}).get(signal, 0)

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {s: dict(v) for s, v in self._counts.items()}

    def escalate(self, step: str, tier: str, rules: Iterable[EscalationRule],
                 *, steps_alias: Optional[Iterable[str]] = None) -> Optional[Escalation]:
        """The highest tier any rule pushes `step` to from `tier`, or None. A rule names the
        steps it applies to; counts are read for the step and its aliases (implement/solo)."""
        names = {step, *(steps_alias or ())}
        best: Optional[Escalation] = None
        for rule in rules:
            if rule.steps and not (names & set(rule.steps)):
                continue
            seen = max((self.count(name, rule.signal) for name in names), default=0)
            if seen < rule.count:
                continue
            target = up(tier) if rule.to == "up" else rule.to
            if target not in TIERS or rank(target) <= rank(tier):
                continue
            if best is None or rank(target) > rank(best.tier):
                best = Escalation(target, rule.signal, seen, rule)
        return best


#: Gates whose failure means "the reviewers asked for changes"; any other failed gate (tests,
#: mutation, the app not serving) is a failed gate.
REVIEW_GATES = frozenset({"code_review_passed", "security_clean", "consistent",
                          "review_approved"})


def seed_from_lineage(finalize_ctx: Optional[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Signals carried across runs. A review-repair continuation is a NEW run: the rounds
    already spent (`attempt` − 1) count as changes requested when the reviewers blocked, and
    as a failed gate when another gate did; the parent's own ledger (`route_escalations`) is
    carried as it was."""
    fctx = finalize_ctx or {}
    seed: dict[str, dict[str, int]] = {}
    try:
        rounds = max(0, int(fctx.get("attempt") or 1) - 1)
    except (TypeError, ValueError):
        rounds = 0
    if rounds > 0:
        failed = [str(g) for g in (fctx.get("prior_failing_gates")
                                   or fctx.get("failing_gates") or [])]
        per = seed.setdefault("implement", {})
        if not failed or any(g in REVIEW_GATES for g in failed):
            per["changes_requested"] = rounds
        if any(g not in REVIEW_GATES for g in failed):
            per["gate_failed"] = rounds
    carried = fctx.get("route_escalations")
    if isinstance(carried, dict):
        for step, signals in carried.items():
            if isinstance(signals, dict):
                per = seed.setdefault(str(step), {})
                for signal, n in signals.items():
                    try:
                        per[str(signal)] = max(per.get(str(signal), 0), int(n))
                    except (TypeError, ValueError):
                        continue
    return seed
