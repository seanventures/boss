"""ONE CAP FOR ONE PIECE OF WORK: the attempts BOSS makes by itself on a build lineage.

A lineage is a build and everything BOSS starts for it on its own: the review-repair child, the
CI repair, the checks-failing follow-up and that follow-up's repairs (they all carry the root's
`root_run_id`). Each of those paths used to keep its own count — the repair rounds read the
run's `attempt`, which a follow-up starts again at 1 — so one broken existing test took four
builds (the build, its repair, the follow-up, the follow-up's repair), each failing on the same
gate (ENT-REC-52). The test plan caps the same cause at three attempts in all, then a person.

`attempts` counts the builds of the lineage since a PERSON last started one (the root, or a
person's Retry: `started_by: person`), so a person's Retry is never refused here and starts a
new window. BOSS's bounded retries of a build that died of BOSS's own platform error
(`auto_infra_retry`, capped by build_auto_retries) attempt nothing at the code and are not
counted, nor is a restart of a build an upgrade stopped (`started_by: platform`). Every
automatic path asks `spent` before it starts another build:

  * the quality gate treats the repair rounds as spent (governed: no repair child), and the PR
    opens labelled with no follow-up, its card saying BOSS already tried this (REASON);
  * a follow-up is not started (_spawn_deferred_findings_task);
  * retry_code_task(automatic=True) refuses with RetryNeedsPerson — the backstop for every
    other automatic caller (CI repair, the shepherd, a recovery agent's repair build).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The most builds BOSS makes by itself on one lineage before a person decides.
CAP = 3
#: The checks-failing stop reason (checks_failing.stopped_record) when the cap is reached.
REASON = "attempts_spent"
#: Who started an attempt (finalize_ctx key): "person"; "boss" for BOSS's own attempt at the
#: code; "platform" for BOSS running again a build its own platform stopped (not counted).
STARTED_BY = "started_by"
BOSS, PERSON, PLATFORM = "boss", "person", "platform"
#: The next step a person is handed.
NEXT_STEP = ("Read the failing checks, then press Retry with a note saying what to change, or "
             "merge it with your reason")


def words(n: int = CAP) -> str:
    """The sentence a person reads when BOSS stops on the cap."""
    return (f"BOSS already tried that for this same cause ({n} attempts, the most it makes "
            "without a person)")


def _by_boss(fctx: Mapping[str, Any]) -> Optional[bool]:
    """True: BOSS started it; False: a person did; None: an attempt that attempts nothing at
    the code (a platform retry)."""
    said = str(fctx.get(STARTED_BY) or "")
    if fctx.get("auto_infra_retry") or said == PLATFORM:
        return None
    if said:
        return said == BOSS
    # Before the marker: a follow-up is always BOSS's; anything else reads as a person's.
    return bool(fctx.get("is_followup") or fctx.get("deferred_from_run"))


def attempts(connection: Any, run_id: str) -> int:
    """How many builds the lineage of `run_id` has made, up to and including it, since a person
    last started one. 1 for a build a person started. 0 when it cannot be read (no cap then:
    the other bounds still hold)."""
    try:
        with connection.begin_nested():
            root = connection.execute(text(
                "SELECT COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs "
                "WHERE id = :r"), {"r": run_id}).scalar()
            if not root:
                return 0
            rows = connection.execute(text(
                "SELECT id, finalize_ctx FROM code_task_runs "
                "WHERE (id = :root OR finalize_ctx->>'root_run_id' = :root) "
                "AND created_at <= (SELECT created_at FROM code_task_runs WHERE id = :r) "
                "ORDER BY created_at, id"), {"root": root, "r": run_id}).mappings().all()
    except Exception:  # noqa: BLE001 — an unread lineage is never a reason to stop work
        return 0
    n = 0
    for row in rows:
        fctx = row["finalize_ctx"] or {}
        if isinstance(fctx, str):
            import json

            fctx = json.loads(fctx or "{}")
        by_boss = _by_boss(fctx) if row["id"] != root else False
        if by_boss is None:
            continue
        n = n + 1 if by_boss else 1
    return n


def spent(connection: Any, run_id: str) -> bool:
    """Has BOSS made all the attempts it may make on this lineage by itself?"""
    return attempts(connection, run_id) >= CAP


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


__all__ = ["BOSS", "CAP", "NEXT_STEP", "PERSON", "PLATFORM", "REASON", "STARTED_BY", "attempts",
           "risk_tier", "spent", "words"]
