"""ONE CAP FOR ONE CAUSE: the attempts BOSS makes by itself at the same failure of a lineage.

A lineage is a build and everything started for it (the review-repair child, the CI repair, the
checks-failing follow-up and its repairs, a recovery build, an agent's retry: they all carry the
root's `root_run_id`). Each of those paths used to keep its own count — the repair rounds read the
run's `attempt`, which a follow-up starts again at 1 — so one broken existing test took four
builds, each failing on the same gate (ENT-REC-52). The plan caps the same cause at three
attempts in all, then a person.

THE COUNT IS PER CAUSE. Every build that stops on its checks records its cause on its row
(`stop_cause`: cause_gate of its failing gates). `attempts` counts the builds of the window that
stopped on the SAME cause. A different cause is a different problem (a merge conflict, the app
not starting, a CI failure after a green build): it gets its own attempts, so BOSS keeps fixing
on its own. THE WINDOW closes when an attempt goes green (a build that completed with no failing
gate) and when a PERSON starts one (`started_by: person`: only a verified person — the API's
by_person, never an agent's tool call, a recovery build or another caller; those are BOSS's and
never reset it). BOSS's retries of its own platform errors (`started_by: platform`,
`auto_infra_retry`) attempt nothing at the code and are skipped.

A PERSON'S own repair budget is their window: when a verified person retries with
`repair_retries`, the attempt records it (`person_repair_budget`, clamped to MAX_PERSON_BUDGET)
with the person mark, and the window that attempt opens is capped at 1 + that figure when it is
larger than CAP. Any other caller's figure (an agent's tool call) never moves the cap.

Every automatic path asks before it starts another build: the quality gate (no repair child at the
cap; the PR opens as a draft with no follow-up, its card saying BOSS already tried this), the
follow-up spawn, and retry_code_task(automatic=True), which refuses with RetryNeedsPerson.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

#: The most builds BOSS makes by itself at one cause of one lineage before a person decides.
CAP = 3
#: finalize_ctx key: the cause a build stopped on (cause_gate of its failing gates).
STOP_CAUSE = "stop_cause"
#: The checks-failing stop reason (checks_failing.stopped_record) when the cap is reached.
REASON = "attempts_spent"
#: Who started an attempt (finalize_ctx key): "person" (a verified person only); "boss" for
#: everything else; "platform" for BOSS running again a build its platform stopped (skipped).
STARTED_BY = "started_by"
BOSS, PERSON, PLATFORM = "boss", "person", "platform"
#: The next step a person is handed.
NEXT_STEP = ("Read the failing checks, then press Retry with a note saying what to change, or "
             "merge it with your reason")


def words(n: int = CAP) -> str:
    """The sentence a person reads when BOSS stops on the cap."""
    return (f"BOSS already tried that for this same cause ({n} attempts, the most it makes "
            "without a person)")


#: A gate that fails because of what a repair round did, never a cause of its own: a round that
#: could not fix the test wrote nothing, and `code_written` failed beside the same test.
_SYMPTOM_GATES = frozenset({"code_written"})
#: Which failing gate names THE cause when several fail. The floors the later checks stand on
#: come first: a reviewer or the must-haves read a red suite, never the other way round.
_CAUSE_ORDER = ("no_committed_secrets", "template_tests", "iac_verify", "app_serves",
                "written_tests_pass", "existing_tests_intact", "nothing_broke",
                "coder_did_not_edit_tests", "stayed_in_app", "web_package_honest",
                "security_clean", "brief_tests_written", "tests_written_first",
                "tests_strong_enough", "acceptance_met", "code_review_passed", "consistent",
                "input_fidelity", "code_written")


def cause_gate(gates: Iterable[str]) -> str:
    """THE ONE GATE that names why a build stopped on its checks (ENT-REC-52). A signature that
    joined every failing gate read the same broken test as a NEW cause the moment a repair added
    a gate beside it. The cause is the first failing gate in _CAUSE_ORDER (an infrastructure
    check, `iac_*`, with the floors), symptoms last; an unknown gate sorts after the known."""
    named = sorted({str(g) for g in gates if g})
    causes = [g for g in named if g not in _SYMPTOM_GATES] or named
    rank = {g: i for i, g in enumerate(_CAUSE_ORDER)}
    iac = rank["iac_verify"]
    return min(causes, key=lambda g: (rank.get(g, iac if g.startswith("iac_") else len(rank)),
                                      g)) if causes else "review"


def started_by(*, automatic: bool, by_person: bool, platform_retry: bool) -> str:
    """Who an attempt is marked as started by: a PERSON only when the caller verified one."""
    if automatic and platform_retry:
        return PLATFORM
    return PERSON if (by_person and not automatic) else BOSS


#: finalize_ctx key: the repair budget a verified person gave the attempt they started.
PERSON_BUDGET = "person_repair_budget"
#: The most repair rounds a person may give one window (the tools' own bound).
MAX_PERSON_BUDGET = 5


def person_budget(repair_retries: Any, *, person: bool) -> Optional[int]:
    """What a start records as the person's repair budget: theirs, clamped; None otherwise."""
    if not person or not isinstance(repair_retries, int) or isinstance(repair_retries, bool):
        return None
    return max(0, min(MAX_PERSON_BUDGET, repair_retries))


def _cap_of(fctx: Mapping[str, Any]) -> int:
    asked = fctx.get(PERSON_BUDGET)
    if isinstance(asked, int) and not isinstance(asked, bool):
        return max(CAP, 1 + max(0, min(MAX_PERSON_BUDGET, asked)))
    return CAP


def _kind(row_id: str, root: str, fctx: Mapping[str, Any]) -> Optional[str]:
    if row_id == root:
        return PERSON                       # the lineage starts a window
    said = str(fctx.get(STARTED_BY) or "")
    if fctx.get("auto_infra_retry") or said == PLATFORM:
        return None
    return PERSON if said == PERSON else BOSS


def standing(connection: Any, run_id: str, cause: Optional[str] = None) -> tuple[int, int]:
    """(attempts, cap) for the window that holds `run_id`: how many of its builds (up to and
    including it) stopped on `cause` (default: the cause `run_id` recorded), and the cap of that
    window (CAP, or the budget of the person who opened it). (0, CAP) when it records no cause
    or the lineage cannot be read (no cap then: the other bounds still hold)."""
    try:
        with connection.begin_nested():
            row = connection.execute(text(
                "SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS root, "
                "finalize_ctx->>'stop_cause' AS cause FROM code_task_runs WHERE id = :r"),
                {"r": run_id}).mappings().first()
            if not row:
                return 0, CAP
            cause = cause or row["cause"]
            if not cause:
                return 0, CAP
            root = row["root"]
            rows = connection.execute(text(
                "SELECT id, status, finalize_ctx FROM code_task_runs "
                "WHERE (id = :root OR finalize_ctx->>'root_run_id' = :root) "
                "AND created_at <= (SELECT created_at FROM code_task_runs WHERE id = :r) "
                "ORDER BY created_at, id"), {"root": root, "r": run_id}).mappings().all()
    except Exception:  # noqa: BLE001 — an unread lineage is never a reason to stop work
        return 0, CAP
    n, cap = 0, CAP
    for r in rows:
        fctx = r["finalize_ctx"] or {}
        if isinstance(fctx, str):
            fctx = json.loads(fctx or "{}")
        kind = _kind(r["id"], root, fctx)
        if kind is None:
            continue
        if kind == PERSON:
            n, cap = 0, _cap_of(fctx)
        stopped = str(fctx.get(STOP_CAUSE) or "")
        if stopped:
            n = n + 1 if stopped == cause else n
        elif str(r["status"] or "") == "completed" and r["id"] != run_id:
            n = 0                           # it went green: the window closes
    return n, cap


def attempts(connection: Any, run_id: str, cause: Optional[str] = None) -> int:
    """How many builds of the window that holds `run_id` stopped on `cause` (standing)."""
    return standing(connection, run_id, cause)[0]


def spent(connection: Any, run_id: str, cause: Optional[str] = None) -> bool:
    """Has BOSS made all the attempts it may make at this cause by itself?"""
    made, cap = standing(connection, run_id, cause)
    return made >= cap


def risk_tier(parent: Optional[Mapping[str, Any]],
              spec: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """The risk tier a continuation of `parent` keeps: the tier asked for (`spec`, else the
    parent's spec), else the tier the parent's run decided (its state). None when none is."""
    for value in ((spec or {}).get("risk_tier"),
                  ((parent or {}).get("spec") or {}).get("risk_tier"),
                  ((parent or {}).get("state") or {}).get("risk_tier")):
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3:
            return value
    return None


__all__ = ["BOSS", "CAP", "NEXT_STEP", "PERSON", "PLATFORM", "REASON", "STARTED_BY", "STOP_CAUSE",
           "MAX_PERSON_BUDGET", "PERSON_BUDGET", "attempts", "cause_gate", "person_budget", "risk_tier",
           "spent", "standing", "started_by", "words"]
