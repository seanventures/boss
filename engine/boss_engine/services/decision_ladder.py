"""PE-F (BOSSAPP-788): Jev-first decision ladder, pre-step applicability, deadlines with safe defaults.

Unified 5-rung decision ladder:
  1. ``coded_fact``      — deterministic manifest/capability/policy facts
  2. ``jev``             — TypeSafe / Jev fast typed decider (``make_decider``)
  3. ``global_model``    — global decision model second opinion
  4. ``recovery_agent``  — step recovery agent with bounded read tools
  5. ``person``          — ask a person with a concrete recommendation

Pre-step applicability is decided BEFORE launching a step from ``app_profile`` + ``step_requirements``
(never inferred from a failure). Deadlines apply the recommendation on expiry for reversible
decisions and remind/escalate (never expire silently) for irreversible decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

RUNGS: tuple[str, ...] = (
    "coded_fact",
    "jev",
    "global_model",
    "recovery_agent",
    "person",
)

QUESTION_KINDS = frozenset({
    "step_applicable",
    "why_failed",
    "what_next",
})

REVERSIBLE_DEFAULT_ACTIONS = frozenset({
    "apply_recommendation",
    "auto_approve",
    "proceed",
    "retry",
})


@dataclass
class LadderDecision:
    question_kind: str
    answer: Any
    decider: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    recommendation: Optional[str] = None
    reversible: bool = False
    overridden_by: Optional[str] = None
    escalated_rungs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_kind": self.question_kind,
            "answer": self.answer,
            "decider": self.decider,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "recommendation": self.recommendation,
            "reversible": self.reversible,
            "overridden_by": self.overridden_by,
            "escalated_rungs": list(self.escalated_rungs),
        }


def evaluate_step_applicability(
    step_kind: str,
    app_profile: Mapping[str, Any] | None,
    step_requirements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether a workflow step applies BEFORE launching it, from app profile + manifest.

    Example: BOSS's own repo declares ``requires: {"sudo": true}`` for Preview while the sandbox
    host capability has ``sudo: false`` -> returns ``applicable=False``, ``status='not_applicable'``,
    ``decider='coded_fact'`` with concrete evidence before the step ever launches.
    """
    profile = dict(app_profile or {})
    caps = dict(profile.get("capabilities") or {})
    manifest = dict(profile.get("manifest") or {})
    reqs: dict[str, Any] = {}
    if isinstance(manifest.get("requires"), Mapping):
        reqs.update(manifest["requires"])
    step_map = manifest.get("steps") if isinstance(manifest.get("steps"), Mapping) else {}
    step_cfg = step_map.get(step_kind) if isinstance(step_map.get(step_kind), Mapping) else {}
    if isinstance(step_cfg.get("requires"), Mapping):
        reqs.update(step_cfg["requires"])
    if isinstance(step_requirements, Mapping):
        reqs.update(step_requirements)

    missing_evidence: list[str] = []
    for cap_key, needed in sorted(reqs.items()):
        if needed in (False, None, ""):
            continue
        actual = caps.get(cap_key)
        if isinstance(needed, bool):
            if bool(actual) is not needed:
                missing_evidence.append(
                    f"step {step_kind!r} requires {cap_key}={needed} from manifest, "
                    f"but host/profile reports {cap_key}={actual!r}"
                )
        elif actual != needed:
            missing_evidence.append(
                f"step {step_kind!r} requires {cap_key}={needed!r}, "
                f"but host/profile reports {cap_key}={actual!r}"
            )

    if step_cfg.get("disabled") is True:
        missing_evidence.append(f"step {step_kind!r} is explicitly disabled in manifest")

    if missing_evidence:
        return {
            "applicable": False,
            "status": "not_applicable",
            "decider": "coded_fact",
            "before_launch": True,
            "evidence": missing_evidence,
        }

    return {
        "applicable": True,
        "status": "applicable",
        "decider": "coded_fact",
        "before_launch": True,
        "evidence": [f"step {step_kind!r} requirements satisfied by app_profile"],
    }


def resolve_deadline_expiry(
    decision_row: Mapping[str, Any],
) -> dict[str, Any]:
    """PE-F deadline policy replacing silent hold-then-expire.

    - Reversible decisions (``reversible=True`` or ``default_action`` in ``REVERSIBLE_DEFAULT_ACTIONS``):
      apply the recommendation on expiry and record what was applied.
    - Irreversible decisions (``hold`` / ``reversible=False``):
      never silently expire; remind and escalate so the decision remains open and answerable.
    """
    default_action = str(decision_row.get("default_action") or "hold").strip().lower()
    rec = decision_row.get("recommendation")
    rec_dict = rec if isinstance(rec, Mapping) else {}
    explicit_rev = decision_row.get("reversible")
    if explicit_rev is None:
        explicit_rev = rec_dict.get("reversible")

    is_reversible = (
        bool(explicit_rev)
        if explicit_rev is not None
        else default_action in REVERSIBLE_DEFAULT_ACTIONS
    )

    if is_reversible:
        applied_action = (
            str(rec_dict.get("action") or default_action)
            if default_action == "hold"
            else default_action
        )
        return {
            "outcome": "apply_recommendation",
            "new_status": "approved",
            "applied_action": applied_action,
            "escalated": False,
            "event": "decision.default_applied",
            "note": (
                f"Deadline reached on reversible decision; applied recommended action "
                f"{applied_action!r} automatically."
            ),
        }

    return {
        "outcome": "remind_and_escalate",
        "new_status": "pending",
        "applied_action": None,
        "escalated": True,
        "event": "decision.overdue",
        "note": (
            "Deadline reached on irreversible decision; never auto-defaulted or expired. "
            "Kept open, reminded, and escalated to owner."
        ),
    }


class DecisionLadder:
    """Walks coded_fact -> jev -> global_model -> recovery_agent -> person, recording decider & overrides."""

    def __init__(self, *, confidence_threshold: float = 0.75) -> None:
        self.confidence_threshold = float(confidence_threshold)
        self._history: list[LadderDecision] = []

    def decide(
        self,
        question_kind: str,
        *,
        context: Mapping[str, Any],
        coded_check: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
        jev_decider: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
        global_decider: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
        recovery_fn: Optional[Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]] = None,
        recommendation: Optional[str] = None,
        reversible: bool = False,
    ) -> LadderDecision:
        if question_kind not in QUESTION_KINDS:
            raise ValueError(f"unknown question_kind {question_kind!r}; expected one of {sorted(QUESTION_KINDS)}")

        escalated: list[str] = []
        rungs = (
            ("coded_fact", coded_check),
            ("jev", jev_decider),
            ("global_model", global_decider),
            ("recovery_agent", recovery_fn),
        )
        best_recommendation = recommendation
        collected_evidence: list[str] = []

        for rung_name, fn in rungs:
            if fn is None:
                continue
            res = fn(context)
            if not res:
                escalated.append(rung_name)
                continue
            conf = float(res.get("confidence", 1.0 if rung_name == "coded_fact" else 0.0))
            ev = [str(e) for e in (res.get("evidence") or []) if str(e).strip()]
            collected_evidence.extend(ev)
            ans = res.get("answer")
            if ans is not None and (rung_name == "coded_fact" or conf >= self.confidence_threshold):
                dec = LadderDecision(
                    question_kind=question_kind,
                    answer=ans,
                    decider=rung_name,
                    confidence=conf,
                    evidence=collected_evidence,
                    recommendation=str(res.get("recommendation") or best_recommendation or ans),
                    reversible=reversible,
                    escalated_rungs=escalated,
                )
                self._history.append(dec)
                return dec
            if ans is not None and not best_recommendation:
                best_recommendation = str(ans)
            escalated.append(rung_name)

        dec = LadderDecision(
            question_kind=question_kind,
            answer="ask_person",
            decider="person",
            confidence=0.0,
            evidence=collected_evidence,
            recommendation=best_recommendation or "review evidence and choose next step",
            reversible=reversible,
            escalated_rungs=escalated,
        )
        self._history.append(dec)
        return dec

    def record_override(self, decision: LadderDecision, *, by: str) -> None:
        decision.overridden_by = str(by or "person")

    def override_rate(self, *, decider: Optional[str] = None) -> float:
        items = [d for d in self._history if decider is None or d.decider == decider]
        if not items:
            return 0.0
        overridden = sum(1 for d in items if d.overridden_by)
        return overridden / len(items)

    def metrics(self) -> dict[str, Any]:
        by_decider: dict[str, int] = {}
        for d in self._history:
            by_decider[d.decider] = by_decider.get(d.decider, 0) + 1
        return {
            "total": len(self._history),
            "by_decider": by_decider,
            "override_rate": self.override_rate(),
        }


__all__ = [
    "DecisionLadder",
    "LadderDecision",
    "QUESTION_KINDS",
    "REVERSIBLE_DEFAULT_ACTIONS",
    "RUNGS",
    "evaluate_step_applicability",
    "resolve_deadline_expiry",
]
