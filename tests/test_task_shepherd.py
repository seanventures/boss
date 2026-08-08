"""Task Shepherd core tests, written from the incident it exists to prevent.

Every case below is a real situation from the 2026-08-03/04 session, with the real error
string where one was recorded. The point of this module is the BOUNDS — a shepherd that
cannot loop, cannot overspend, and cannot guess. Run directly:

    python tests/test_task_shepherd.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))

from boss_engine.services.task_shepherd import (  # noqa: E402
    APPROVAL_REQUIRED, DUPLICATE_EXECUTION, ESCALATE, GATES_FAILED, INFRASTRUCTURE,
    MERGE_CONFLICT, NO_CODE, REMEDIES, SPEND_CAP, STALE_BASE, TURN_CAP, UNKNOWN,
    ShepherdState, classify, escalation, needs_operator_confirmation, next_step,
)

_R: list[tuple[bool, str, str]] = []


def check(name: str, got, want, note: str = "") -> None:
    _R.append((got == want, name, f"got={got!r} want={want!r} {note}".strip()))


def check_true(name: str, got, note: str = "") -> None:
    check(name, bool(got), True, note)


# --------------------------------------------------------------------------- #
def t_classify_real_errors() -> None:
    """The exact strings BOSS wrote during the session."""
    cases = [
        ("claude code agent exited 1: success — You've hit your monthly spend limit", SPEND_CAP),
        ("rate limit exceeded (429)", SPEND_CAP),
        ("claude code agent exited 1: error_max_turns", TURN_CAP),
        ("Client error '405 Method Not Allowed' for url 'https://api.github.com/...'", STALE_BASE),
        ("needs_conflict_resolution", MERGE_CONFLICT),
        ("ProjectMemberPermissionError: Actor 'mcp' is not allowed to approve a Tier-2 decision.",
         APPROVAL_REQUIRED),
        ("Test author produced no test files even after escalating", NO_CODE),
        ("run crun_x is owned by another execution (rejected at implement)", DUPLICATE_EXECUTION),
        ("Executable is not allowlisted: [ [substrate=local_subprocess]", INFRASTRUCTURE),
        ("job timed out after 3600s", INFRASTRUCTURE),
        ("Budget reservation denied: denied", INFRASTRUCTURE),
    ]
    for err, want in cases:
        check(f"classify: {err[:44]}", classify(err), want)

    # UNKNOWN is a real answer. Every expensive mistake of the session came from treating an
    # unclassified failure as a familiar one.
    check("classify: an unrecognised error is UNKNOWN, not guessed",
          classify("the flux capacitor desynchronised"), UNKNOWN)
    check("classify: empty error is UNKNOWN", classify(""), UNKNOWN)
    check("classify: None is UNKNOWN", classify(None), UNKNOWN)

    # Reviewer PROSE must never be read as a platform fault — the loose-token bug that a test
    # caught earlier in the session.
    check("classify: a finding mentioning 'timeout' is NOT infrastructure",
          classify("review found a missing timeout on the fetch call") == INFRASTRUCTURE, False)
    check("classify: a finding mentioning rate limiting is NOT a spend cap",
          classify("the client has no rate limiting and no quota handling") == SPEND_CAP, False)

    # A shipped PR with failing gates is not a shepherd problem.
    check("classify: gates failed on a shipped PR",
          classify("", gates_failed=["code_review_passed"], has_pr=True), GATES_FAILED)


# --------------------------------------------------------------------------- #
def t_never_loops() -> None:
    """crun_b4b6383d ran the same pipeline three times, 15 sessions, $6.83, no PR."""
    s = ShepherdState("crun_x", budget_usd=25.0)
    v1 = next_step(s, SPEND_CAP, failure_signature="spend limit")
    check("first failure: a remedy is chosen", v1.action, "act")
    s.used_remedies.add((SPEND_CAP, v1.remedy.id))
    s.attempt_history.append((SPEND_CAP, v1.remedy.id, "spend limit"))
    s.spent_usd += v1.remedy.prior_cost_usd

    # Same remedy, same failure, a second time -> stop. This is the bound the session lacked.
    s.attempt_history.append((SPEND_CAP, v1.remedy.id, "spend limit"))
    v2 = next_step(s, SPEND_CAP, failure_signature="spend limit")
    check("a remedy that produced the same failure twice stops the shepherd",
          v2.stopped_because, "loop")
    check_true("and it says which remedy did not work", v1.remedy.id in v2.reason)

    # A DIFFERENT failure after the same remedy is progress, not a loop.
    s2 = ShepherdState("crun_y", budget_usd=25.0)
    s2.attempt_history.append((SPEND_CAP, "wait_resume", "spend limit"))
    s2.used_remedies.add((SPEND_CAP, "wait_resume"))
    v3 = next_step(s2, SPEND_CAP, failure_signature="rate limit exceeded")
    check("a different failure after a remedy is not a loop", v3.action, "act")


# --------------------------------------------------------------------------- #
def t_never_overspends() -> None:
    s = ShepherdState("crun_x", budget_usd=25.0, spent_usd=23.0)
    v = next_step(s, TURN_CAP)
    check("does not start a remedy it cannot finish", v.stopped_because, "budget")
    check_true("and says how much was needed vs left", "$3.00" in v.reason and "$2.00" in v.reason)

    # Exhausting the ledger is distinct from exhausting the budget, and must say so.
    s2 = ShepherdState("crun_x", budget_usd=100.0)
    for r in REMEDIES[TURN_CAP]:
        if r.action:
            s2.used_remedies.add((TURN_CAP, r.id))
    v2 = next_step(s2, TURN_CAP)
    check("with every remedy spent, it escalates rather than inventing one",
          v2.stopped_because, "human")

    # A large single spend asks first, even when affordable.
    big = [r for r in REMEDIES[TURN_CAP] if r.prior_cost_usd >= 6.0][0]
    check("a remedy over 40% of budget asks the operator first",
          needs_operator_confirmation(big, ShepherdState("crun_x", budget_usd=10.0)), True)
    check("a modest remedy does not",
          needs_operator_confirmation(big, ShepherdState("crun_x", budget_usd=100.0)), False)


# --------------------------------------------------------------------------- #
def t_governance_is_absolute() -> None:
    """BOSS refused `mcp` a Tier-2 approval and was right to."""
    s = ShepherdState("crun_x", budget_usd=1000.0)
    v = next_step(s, APPROVAL_REQUIRED)
    check("an approval requirement always stops the shepherd", v.action, "stop")
    check("and it stops for a human, not for budget", v.stopped_because, "human")
    check("there is no automated remedy for approval, at any budget",
          [r for r in REMEDIES[APPROVAL_REQUIRED] if r.action], [])

    # Unknown failures must never be guessed at.
    v2 = next_step(ShepherdState("crun_x", budget_usd=1000.0), UNKNOWN)
    check("an unknown failure escalates immediately", v2.stopped_because, "human")


# --------------------------------------------------------------------------- #
def t_does_not_claim_automatic_work() -> None:
    """Rows the engine already handles must not be reported as shepherd remedies."""
    s = ShepherdState("crun_x", budget_usd=25.0)
    v = next_step(s, STALE_BASE)
    check("skips the automatic remedy and offers the real one",
          v.remedy.id if v.remedy else None, "rebase_via_agent")
    check_true("the automatic remedy is still declared, for honesty",
               any(r.automatic for r in REMEDIES[STALE_BASE]))

    # Gates failed on a shipped PR: nothing to do at all.
    v2 = next_step(ShepherdState("crun_x", budget_usd=25.0), GATES_FAILED)
    check("a shipped PR with findings needs no remedy", v2.action, "stop")
    check("and it is not reported as an escalation-worthy problem",
          v2.stopped_because, "ledger")


# --------------------------------------------------------------------------- #
def t_stops_on_success_and_deadline() -> None:
    s = ShepherdState("crun_x", budget_usd=25.0)
    check("a succeeded task is never acted on again",
          next_step(s, SPEND_CAP, succeeded=True).stopped_because, "success")
    s2 = ShepherdState("crun_x", budget_usd=25.0, shepherd_minutes=241, deadline_minutes=240)
    check("a wait-and-resume remedy cannot idle forever",
          next_step(s2, SPEND_CAP).stopped_because, "deadline")


# --------------------------------------------------------------------------- #
def t_escalation_is_a_question() -> None:
    """The operator was handed 'five defects, what would you like to do?' — not a question."""
    e = escalation("crun_x", MERGE_CONFLICT, {"pr": 67, "conflicting_paths": ["tests/"]})
    check_true("it asks something answerable", e["question"].endswith("?"))
    check_true("it carries the evidence", e["evidence"]["pr"] == 67)
    check_true("every option is concrete", all("label" in o for o in e["options"]))
    check_true("every option states its cost", all("est_cost_usd" in o for o in e["options"]))
    check_true("and says whether that cost is measured or only a cold-start prior",
               all("estimate_source" in o for o in e["options"]))
    check_true("there is always an 'I'll handle it' option",
               any(o["action"] is None for o in e["options"]))
    check_true("the automated option is offered with its price",
               any(o["action"] == "extend_code_task" and o["est_cost_usd"] > 0
                   for o in e["options"]))
    # Measured estimates must override the prior — an enterprise repo's real cost can be an
    # order of magnitude off a constant derived from one small sample.
    e2 = escalation("crun_x", MERGE_CONFLICT, {}, costs={"resolve_via_agent": 47.50})
    opt = [o for o in e2["options"] if o["action"] == "extend_code_task"][0]
    check("a measured estimate replaces the prior", opt["est_cost_usd"], 47.50)
    check("and is labelled as measured", opt["estimate_source"], "measured")


# --------------------------------------------------------------------------- #
def t_bounded_by_construction() -> None:
    """Termination must be a property of the ledger, not of a model's judgement."""
    total = 0
    for cls, remedies in REMEDIES.items():
        actionable = [r for r in remedies if r.action and not r.automatic]
        total += len(actionable)
        check_true(f"class {cls} has a finite remedy list", len(remedies) <= 4)
        check_true(f"class {cls} ends in escalation or is fully automatic",
                   remedies[-1] is ESCALATE or all(r.automatic for r in remedies))
    check_true("the whole system is bounded by well under 30 actions", total <= 30,
               f"total actionable remedies = {total}")

    # Drive one class to exhaustion and prove it terminates.
    s = ShepherdState("crun_x", budget_usd=1000.0)
    steps = 0
    while steps < 50:
        v = next_step(s, NO_CODE, failure_signature=f"sig{steps}")
        if v.action == "stop":
            break
        s.used_remedies.add((NO_CODE, v.remedy.id))
        s.spent_usd += v.remedy.prior_cost_usd
        steps += 1
    check_true("a class always terminates, regardless of budget", steps < 50,
               f"terminated after {steps} remedies")


# --------------------------------------------------------------------------- #
def main() -> int:
    for fn in (t_classify_real_errors, t_never_loops, t_never_overspends,
               t_governance_is_absolute, t_does_not_claim_automatic_work,
               t_stops_on_success_and_deadline, t_escalation_is_a_question,
               t_bounded_by_construction):
        try:
            fn()
        except Exception as exc:
            _R.append((False, f"{fn.__name__} raised", f"{type(exc).__name__}: {exc}"))
    bad = [r for r in _R if not r[0]]
    for ok, name, detail in _R:
        if not ok:
            print(f"  FAIL  {name}\n        {detail}")
    print(f"\n  {len(_R) - len(bad)}/{len(_R)} shepherd assertions passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
