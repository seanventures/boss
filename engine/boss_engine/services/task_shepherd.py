"""Task Shepherd — the bounded core.

Design: docs/product/TASK_SHEPHERD_DESIGN.md

This module is the half that must NEVER be left to a model's judgement: the failure taxonomy,
the remedy ledger, the termination conditions, and the cost arithmetic. A model chooses among
what this module says is permissible, and must cite evidence; it cannot invent an action or
repeat a spent one.

Written from the 2026-08-03/04 session, where 13 governed runs cost $66.65 and 58% of that
produced nothing. Every one of those failures was operational rather than a coding failure —
a reclaimed lease, a provider spend cap, a merge needing a rebase, an approval needing a
human. All recoverable by someone who knew what the error meant. This encodes that knowing.

The functions here are pure and side-effect free so they can be tested without a database or
a model. Persistence lives in the service layer above.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Failure classes
# --------------------------------------------------------------------------- #
# Numbered to match the design doc's remedy table, so a reasoning record, a ledger row and
# the doc all refer to the same thing.
SPEND_CAP = 1
TURN_CAP = 2
STALE_BASE = 3
MERGE_CONFLICT = 4
APPROVAL_REQUIRED = 5
GATES_FAILED = 6
NO_CODE = 7
DUPLICATE_EXECUTION = 8
INFRASTRUCTURE = 9
UNKNOWN = 10

CLASS_NAMES = {
    SPEND_CAP: "provider spend/rate cap",
    TURN_CAP: "turn cap exhausted",
    STALE_BASE: "stale merge base",
    MERGE_CONFLICT: "true merge conflict",
    APPROVAL_REQUIRED: "approval required",
    GATES_FAILED: "quality gates failed",
    NO_CODE: "no code produced",
    DUPLICATE_EXECUTION: "duplicate execution",
    INFRASTRUCTURE: "infrastructure",
    UNKNOWN: "unknown",
}

# Exact phrases the engine itself writes. Deliberately NOT loose tokens: a bare "timeout"
# matched the sentence "review found a missing timeout on the fetch call" during the session,
# which would have classified a genuine code finding as a platform fault. Reviewer prose is
# not a status code.
_SIGNATURES: list[tuple[int, tuple[str, ...]]] = [
    (SPEND_CAP, ("spend limit", "usage limit", "rate limit exceeded", "quota exceeded",
                 "too many requests")),
    (TURN_CAP, ("error_max_turns", "max_turns_exhausted", "ran out of turns")),
    (STALE_BASE, ("405", "method not allowed", "not mergeable", "base branch was modified")),
    (MERGE_CONFLICT, ("merge conflict", "conflicting", "needs_conflict_resolution")),
    (APPROVAL_REQUIRED, ("not allowed to approve", "projectmemberpermissionerror",
                         "awaiting_plan_approval", "awaiting_gate_override")),
    (NO_CODE, ("code_written", "no test files", "produced no test files")),
    (DUPLICATE_EXECUTION, ("lease reassigned", "runfenced", "owned by another execution",
                           "duplicate concurrent")),
    (INFRASTRUCTURE, ("connection refused", "connection reset", "executable is not allowlisted",
                      "infrastructure error", "timed out after", "lease expired",
                      "budget reservation denied",
                      # SIGTERM. During the session this was a sibling run's process killer
                      # reaching outside its own sandbox; it is a platform fault, never the
                      # task's, and re-running the task unchanged is the correct remedy.
                      "exited 143", "error_during_execution")),
    # The run did all the work and the PR step itself failed. Distinct from a stale base:
    # there is nothing to rebase, the call simply did not succeed. Two runs ended here.
    (STALE_BASE, ("pr not created",)),
]


def classify(error: Optional[str], *, gates_failed: Optional[list[str]] = None,
             has_pr: bool = False) -> int:
    """Map observed evidence onto a failure class.

    Order matters: the most specific and most actionable signatures win. UNKNOWN is a real
    answer, not a fallback for "probably infra" — every expensive mistake of the session came
    from treating an unclassified failure as a familiar one.
    """
    text = (error or "").lower()
    if gates_failed and has_pr:
        # The PR shipped with findings attached; the follow-up task already exists. There is
        # nothing for a shepherd to remedy here.
        return GATES_FAILED
    for cls, phrases in _SIGNATURES:
        if any(p in text for p in phrases):
            return cls
    return UNKNOWN


# --------------------------------------------------------------------------- #
# Remedies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Remedy:
    id: str
    describe: str
    est_cost_usd: float
    # An action the shepherd may take, or None when the remedy IS escalation.
    action: Optional[str] = None
    # True when the engine already handles this without a shepherd. Kept in the table so the
    # ledger stays honest about what it actually owns rather than claiming credit.
    automatic: bool = False


ESCALATE = Remedy("escalate", "ask the operator one decision-shaped question", 0.0, None)

REMEDIES: dict[int, tuple[Remedy, ...]] = {
    SPEND_CAP: (
        Remedy("wait_resume", "wait for the cap to reset, resume from the checkpoint", 3.0,
               "retry_code_task"),
        Remedy("reissue_lean", "re-issue on the lean profile (no adversarial panel)", 3.0,
               "retry_code_task"),
        ESCALATE,
    ),
    TURN_CAP: (
        Remedy("raise_turns", "retry on the same branch with a raised turn cap", 3.0,
               "retry_code_task"),
        Remedy("split_task", "split the task; the scope exceeds one agent session", 6.0,
               "create_code_task"),
        ESCALATE,
    ),
    STALE_BASE: (
        Remedy("update_branch", "merge the base into the head branch and retry", 0.0,
               None, automatic=True),
        Remedy("rebase_via_agent", "have BOSS rebase the branch onto the new base", 5.0,
               "extend_code_task"),
        ESCALATE,
    ),
    MERGE_CONFLICT: (
        Remedy("resolve_via_agent", "have BOSS resolve the conflicts on the branch", 5.0,
               "extend_code_task"),
        ESCALATE,
    ),
    # No automated remedy exists or should. Tier 2 requires the ceo role, and an agent
    # approving its own merge into main is what a governance product exists to prevent.
    APPROVAL_REQUIRED: (ESCALATE,),
    GATES_FAILED: (
        Remedy("already_shipped", "PR shipped with findings; follow-up task exists", 0.0,
               None, automatic=True),
    ),
    NO_CODE: (
        Remedy("rerun_implement", "re-run implement on the checkpointed branch", 3.0,
               "retry_code_task"),
        Remedy("rescope", "re-scope the task; the brief was not buildable as written", 6.0,
               "extend_code_task"),
        ESCALATE,
    ),
    DUPLICATE_EXECUTION: (
        Remedy("fencing", "fencing rejects the stale writer", 0.0, None, automatic=True),
        Remedy("reissue_clean", "re-issue once, resuming from the checkpoint", 3.0,
               "retry_code_task"),
        ESCALATE,
    ),
    INFRASTRUCTURE: (
        Remedy("substrate_fallback", "re-run on the local substrate", 3.0, "retry_code_task"),
        ESCALATE,
    ),
    UNKNOWN: (ESCALATE,),
}


# --------------------------------------------------------------------------- #
# Termination
# --------------------------------------------------------------------------- #
@dataclass
class ShepherdState:
    """Everything needed to decide whether to act again. Deliberately small and explicit."""
    task_ref: str
    budget_usd: float
    spent_usd: float = 0.0
    used_remedies: set[tuple[int, str]] = field(default_factory=set)
    # (failure_class, remedy_id) -> how many times this pair was followed by the SAME failure
    attempt_history: list[tuple[int, str, str]] = field(default_factory=list)
    elapsed_minutes: float = 0.0
    deadline_minutes: float = 240.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)


@dataclass(frozen=True)
class Verdict:
    """What the shepherd should do next, and why. `stop` is a first-class success."""
    action: str                 # "act" | "stop"
    remedy: Optional[Remedy] = None
    reason: str = ""
    stopped_because: Optional[str] = None   # success|ledger|loop|budget|human|deadline


def next_step(state: ShepherdState, failure_class: int, *, succeeded: bool = False,
              failure_signature: str = "") -> Verdict:
    """The single decision point. Cheap, deterministic, and checked BEFORE any spend.

    Order is not arbitrary: success first (never act on a finished task), then the bounds that
    cost nothing to evaluate, and only then the ledger. A model is consulted afterwards to
    choose among what this returns as permissible — never to decide whether to continue.
    """
    if succeeded:
        return Verdict("stop", None, "the task reached its terminal condition", "success")

    if state.elapsed_minutes >= state.deadline_minutes:
        return Verdict("stop", None,
                       f"wall-clock deadline reached ({state.deadline_minutes:.0f} min)",
                       "deadline")

    remedies = REMEDIES.get(failure_class, (ESCALATE,))

    # Loop detection: the SAME remedy followed by the SAME failure, twice. One repeat is
    # coincidence; the second is proof the remedy does not work here. During the session a run
    # executed three identical passes and nothing was counting.
    for cls, rid, sig in state.attempt_history:
        if cls == failure_class and sig == failure_signature:
            repeats = sum(1 for c, r, s in state.attempt_history
                          if c == cls and r == rid and s == sig)
            if repeats >= 2:
                return Verdict("stop", None,
                               f"remedy {rid!r} produced the same failure twice; it does not "
                               "work here", "loop")

    for remedy in remedies:
        if (failure_class, remedy.id) in state.used_remedies:
            continue
        if remedy.automatic:
            # The engine handles it; the shepherd records it and moves to the next remedy
            # rather than claiming work it did not do.
            continue
        if remedy is ESCALATE or remedy.action is None:
            return Verdict("stop", ESCALATE,
                           "no automated remedy remains for this class", "human")
        if remedy.est_cost_usd > state.remaining_usd:
            return Verdict("stop", None,
                           f"next remedy needs ${remedy.est_cost_usd:.2f} but only "
                           f"${state.remaining_usd:.2f} remains; not starting what it "
                           "cannot finish", "budget")
        return Verdict("act", remedy, remedy.describe)

    return Verdict("stop", None, "every remedy for this class has been spent", "ledger")


def needs_operator_confirmation(remedy: Remedy, state: ShepherdState,
                                threshold: float = 0.4) -> bool:
    """Spend that is large relative to the budget is the operator's call, not the shepherd's.

    Not a safety rail against runaway cost — the ledger and budget already bound that. This is
    about a single expensive action deserving a human's assent even when it is affordable.
    """
    if state.budget_usd <= 0:
        return True
    return remedy.est_cost_usd > threshold * state.budget_usd


def escalation(task_ref: str, failure_class: int, evidence: dict[str, Any],
               options: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Build a decision-shaped question.

    Never a log dump. During the session the operator was repeatedly handed "here are five
    defects, what would you like to do?" — which is not a question, it is a transfer of work.
    Each option states what it costs.
    """
    remedies = REMEDIES.get(failure_class, (ESCALATE,))
    auto = [
        {"label": r.describe, "action": r.action, "est_cost_usd": r.est_cost_usd}
        for r in remedies if r.action and not r.automatic
    ]
    return {
        "task_ref": task_ref,
        "failure_class": failure_class,
        "question": f"{CLASS_NAMES.get(failure_class, 'unknown')} — how should this proceed?",
        "options": options or (auto + [{"label": "I'll handle it", "action": None,
                                        "est_cost_usd": 0.0}]),
        "evidence": evidence,
    }
