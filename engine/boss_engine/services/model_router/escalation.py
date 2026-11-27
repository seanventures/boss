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

#: Ordered reasoning-effort lever on the SAME model (BOSSAPP-780): raising effort preserves the
#: prompt cache and costs far less than a cold switch to a stronger model.
EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high")


def next_effort(current: Optional[str]) -> Optional[str]:
    """The next higher reasoning effort level on the same model, or None if already at high+."""
    cur = str(current or "").strip().lower()
    if not cur:
        return "medium"
    if cur == "low":
        return "medium"
    if cur == "medium":
        return "high"
    return None


@dataclass(frozen=True)
class CacheSwitchAnalysis:
    """Cost economics of keeping a warm session vs switching cold to a stronger model."""

    warm_context_tokens: int
    current_model: str
    target_model: str
    stay_warm_usd_micros: int
    switch_cold_usd_micros: int
    cache_loss_usd_micros: int
    effort_bump_usd_micros: int
    worth_switching: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "warm_context_tokens": self.warm_context_tokens,
            "current_model": self.current_model,
            "target_model": self.target_model,
            "stay_warm_usd_micros": self.stay_warm_usd_micros,
            "switch_cold_usd_micros": self.switch_cold_usd_micros,
            "cache_loss_usd_micros": self.cache_loss_usd_micros,
            "effort_bump_usd_micros": self.effort_bump_usd_micros,
            "worth_switching": self.worth_switching,
            "reason": self.reason,
        }


def estimate_cache_switch_cost(
    *,
    warm_context_tokens: int,
    current_model: str,
    target_model: str,
    expected_output_tokens: int = 2000,
    warm_fix_attempted: bool = False,
) -> CacheSwitchAnalysis:
    """Price the prompt-cache loss of switching from `current_model` to `target_model` (BOSSAPP-780).

    Staying on the warm session reads `warm_context_tokens` at 0.1x input price; switching cold
    re-ingests the context at 1.25x input price (cache write) on `target_model`. When a warm
    session exists and no same-model warm fix has been tried yet, staying warm (with or without
    an effort bump) avoids discarding the cache unnecessarily.
    """
    from ..openai_adapter import CACHE_READ_MULT, CACHE_WRITE_MULT, price_for_model

    tokens = max(0, int(warm_context_tokens or 0))
    out_tok = max(0, int(expected_output_tokens or 0))
    cur_in, cur_out, _ = price_for_model(current_model or "claude-sonnet-5")
    tgt_in, tgt_out, _ = price_for_model(target_model or "claude-opus-4-6")

    stay_warm = int(round(tokens * cur_in * CACHE_READ_MULT + out_tok * cur_out))
    # Raising effort on the same model preserves cache reads and adds ~40% reasoning output tokens.
    effort_bump = int(round(tokens * cur_in * CACHE_READ_MULT + out_tok * 1.4 * cur_out))
    switch_cold = int(round(tokens * tgt_in * CACHE_WRITE_MULT + out_tok * tgt_out))
    cache_loss = max(0, switch_cold - stay_warm)

    if tokens == 0 or warm_fix_attempted:
        worth = True
        reason = ("warm fix already failed; cold switch justified"
                  if warm_fix_attempted else "no warm cache to lose")
    else:
        worth = False
        reason = (f"warm cache ({tokens:,} tok) loses ~${cache_loss / 1e6:.3f} on cold switch; "
                  "try same-model warm fix first")
    return CacheSwitchAnalysis(
        warm_context_tokens=tokens,
        current_model=current_model or "",
        target_model=target_model or "",
        stay_warm_usd_micros=stay_warm,
        switch_cold_usd_micros=switch_cold,
        cache_loss_usd_micros=cache_loss,
        effort_bump_usd_micros=effort_bump,
        worth_switching=worth,
        reason=reason,
    )


@dataclass(frozen=True)
class EscalationPlan:
    """What the router should do when failure signals exist on a step (BOSSAPP-780)."""

    action: str                      # none | warm_fix | effort_bump | tier_escalation
    tier: str
    effort: Optional[str] = None
    signal: Optional[str] = None
    count: int = 0
    cache_loss_usd_micros: int = 0
    reason: str = ""
    escalation: Optional["Escalation"] = None

    def describe(self) -> str:
        if self.escalation is not None and self.action == "tier_escalation":
            return self.escalation.describe()
        return self.reason


def build_escalation_handoff(
    *,
    failing_tests: str = "",
    diff: str = "",
    review_findings: str = "",
    prior_attempts: int = 0,
    prior_model: str = "",
    signal: str = "",
    max_diff_chars: int = 8000,
    max_test_chars: int = 4000,
    max_findings_chars: int = 4000,
) -> str:
    """Structured evidence packet for an escalated stronger model (BOSSAPP-780).

    Instead of starting cold and blind on the stronger model, hand over the failing tests,
    the current implementation diff, and any review/gate findings from the prior attempt.
    """
    sections: list[str] = []
    meta_bits = []
    if prior_model:
        meta_bits.append(f"previous model: {prior_model}")
    if prior_attempts:
        meta_bits.append(f"prior attempts: {prior_attempts}")
    if signal:
        meta_bits.append(f"signal: {signal}")
    header = "=== ESCALATION EVIDENCE HANDOFF ==="
    if meta_bits:
        header += f"\nContext: {'; '.join(meta_bits)}"
    header += ("\nA warm same-model fix did not converge. Diagnose from the concrete evidence "
               "below and patch ONLY the implementation (never weaken frozen tests).")
    sections.append(header)
    if (failing_tests or "").strip():
        sections.append("### FAILING TESTS / VERIFICATION OUTPUT\n"
                        + failing_tests.strip()[-max_test_chars:])
    if (review_findings or "").strip():
        sections.append("### REVIEW & GATE FINDINGS\n"
                        + review_findings.strip()[:max_findings_chars])
    if (diff or "").strip():
        body = diff.strip()
        if len(body) > max_diff_chars:
            body = body[:max_diff_chars] + "\n... (diff truncated)"
        sections.append("### CURRENT IMPLEMENTATION DIFF\n" + body)
    return "\n\n".join(sections)


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

    def plan_escalation(
        self,
        step: str,
        tier: str,
        rules: Iterable[EscalationRule],
        *,
        steps_alias: Optional[Iterable[str]] = None,
        warm_context_tokens: int = 0,
        warm_fix_attempted: bool = False,
        current_effort: Optional[str] = None,
        current_model: str = "",
        target_model: str = "",
    ) -> EscalationPlan:
        """Cache-aware escalation decision (BOSSAPP-780):
          1. If any failure signal exists below the tier-escalation threshold and effort can be
             raised on the same model, bump effort first (keeps the prompt cache).
          2. If a tier-escalation rule fires while a warm session exists and no same-model warm
             fix has run yet, stay on the same model for a warm fix (and bump effort if possible).
          3. Once a warm fix has already failed (or no warm session exists), escalate tier and
             price the cache loss of the cold switch.
        """
        esc = self.escalate(step, tier, rules, steps_alias=steps_alias)
        names = {step, *(steps_alias or ())}
        if esc is None:
            # Below tier-escalation count: check if a single failure signal warrants an effort bump.
            for sig in ("tests_failed", "changes_requested", "gate_failed"):
                seen = max((self.count(name, sig) for name in names), default=0)
                if seen >= 1:
                    bumped = next_effort(current_effort)
                    if bumped is not None:
                        return EscalationPlan(
                            action="effort_bump", tier=tier, effort=bumped, signal=sig, count=seen,
                            cache_loss_usd_micros=0,
                            reason=f"{sig.replace('_', ' ')} ×{seen}: raise effort to {bumped} "
                                   "before switching models (keeps prompt cache)",
                        )
            return EscalationPlan(action="none", tier=tier)

        analysis = estimate_cache_switch_cost(
            warm_context_tokens=warm_context_tokens,
            current_model=current_model,
            target_model=target_model,
            warm_fix_attempted=warm_fix_attempted,
        )
        if not analysis.worth_switching and esc.signal != "no_output":
            bumped = next_effort(current_effort)
            action = "effort_bump" if bumped else "warm_fix"
            return EscalationPlan(
                action=action,
                tier=tier,
                effort=bumped,
                signal=esc.signal,
                count=esc.count,
                cache_loss_usd_micros=0,
                reason=f"Warm fix first on same model ({analysis.reason})"
                       + (f"; effort → {bumped}" if bumped else ""),
            )
        return EscalationPlan(
            action="tier_escalation",
            tier=esc.tier,
            effort=next_effort(current_effort) or "high",
            signal=esc.signal,
            count=esc.count,
            cache_loss_usd_micros=analysis.cache_loss_usd_micros,
            reason=esc.describe(),
            escalation=esc,
        )


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
