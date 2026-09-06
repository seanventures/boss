"""Is the thing that stopped this step still true?

A failed step's error is a RECORD OF A PAST ATTEMPT, and the console rendered it as a statement
about the present. So after BOSS created the missing deployment target, the Deploy step went on
saying, in red:

    'Lead-Triage Inbox' has no 'staging' deployment target of its own, so there is nowhere to
    ship it.

That had stopped being true. The operator had no way to know — the sentence reads like current
state, the run still said `deadlettered`, and the only clue that anything had changed was that
they had watched BOSS change it. Their words: "not sure why the workflow is not updated with
the latest.. we should ensure the workflow rescans the updated state".

Retrying was always possible; `retry-step` resets the attempt and revives the run. What was
missing is the thing that makes retrying an INFORMED act rather than a guess: knowing whether
the blocker is gone.

So a failure that names a precondition is re-checked against the world, and a step whose
blocker has cleared says so. Only preconditions this module knows how to test are re-checked —
everything else says plainly that it cannot be tested automatically, which is more useful than
a confident answer nobody should trust.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text as sql

from .naming import identity_key

_log = logging.getLogger(__name__)

#: Step statuses whose recorded error might have gone stale. A running step's error is current
#: by definition, and a succeeded step has none.
STALE_CAPABLE = frozenset({"failed", "deadlettered", "cancelled"})


def _target_now_exists(connection, project_id: str, output: dict) -> Optional[dict[str, Any]]:
    """The deploy refused because the app had no target. Does it have one now?"""
    app_name = str(output.get("app_name") or "").strip()
    if not app_name:
        return None
    wanted = identity_key(app_name)
    environment = str(
        (output.get("fix") or {}).get("args", {}).get("environment")
        or output.get("environment") or "staging")
    rows = connection.execute(
        sql("SELECT id, app_name, environment, domains FROM deployment_targets "
            "WHERE project_id = :p AND status = 'active'"),
        {"p": project_id}).mappings().all()
    for row in rows:
        if identity_key(str(row["app_name"] or "")) != wanted:
            continue
        if str(row["environment"] or "") != environment:
            continue
        where = ", ".join(str(d) for d in (row["domains"] or [])) or row["id"]
        return {
            "cleared": True,
            "reason": (f"'{app_name}' now has an active {environment} target ({where}). "
                       "The failure above is a record of the earlier attempt."),
            "resolved_by": row["id"],
        }
    return {
        "cleared": False,
        "reason": (f"'{app_name}' still has no active {environment} deployment target, so this "
                   "would fail again for the same reason."),
    }


#: What a recorded failure can name as its blocker, and how to test each one NOW.
#:
#: Keyed on `output.missing`, which the capability writes when it refuses for a reason that is
#: about the world rather than about the work. Adding a checker is how a new class of failure
#: becomes re-checkable; a failure with no checker is reported as untestable rather than guessed
#: at, because "probably fine now" is the answer that gets a retry burned for nothing.
CHECKERS = {
    "deployment_target": _target_now_exists,
}


def blocker_status(connection, project_id: str, step: dict) -> Optional[dict[str, Any]]:
    """Whether this step's recorded blocker still holds. None when there is nothing to say."""
    if str(step.get("status") or "") not in STALE_CAPABLE:
        return None
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    missing = str(output.get("missing") or "").strip()
    if not missing:
        return None
    checker = CHECKERS.get(missing)
    if not checker:
        return {"cleared": False, "checkable": False,
                "reason": f"this step recorded a missing {missing!r}; BOSS cannot test that "
                          "one automatically, so the failure above may or may not still apply."}
    try:
        result = checker(connection, project_id, output)
    except Exception as exc:  # noqa: BLE001 — a re-check that fails must not hide the failure
        _log.warning("blocker recheck failed for %s: %s", step.get("step_id"), exc)
        return None
    if result is None:
        return None
    return {**result, "checkable": True, "missing": missing}


def recheck_run(connection, *, project_id: str, run_id: str,
                requeue: bool = True) -> dict[str, Any]:
    """Re-test every stale-capable step on a run, and put back the ones that can now proceed.

    The manual refresh the operator asked for, and the operation the first fixing investigation
    said was missing — it had to reconcile a dead-lettered run with hand-written SQL because
    nothing exposed this.

    `requeue=False` answers the question without changing anything, which is what the console's
    read path wants.
    """
    steps = connection.execute(
        sql("SELECT id, step_id, status, output, last_error FROM workflow_step_runs "
            "WHERE workflow_run_id = :r AND project_id = :p ORDER BY created_at"),
        {"r": run_id, "p": project_id}).mappings().all()
    if not steps:
        return {"ok": False, "error": f"no steps found for {run_id} in this project"}

    checked, requeued = [], []
    for row in steps:
        status = blocker_status(connection, project_id, dict(row))
        if not status:
            continue
        entry = {"step_id": row["step_id"], "status": row["status"], **status}
        checked.append(entry)
        if not (status.get("cleared") and requeue):
            continue
        connection.execute(
            sql("UPDATE workflow_step_runs SET status='ready', attempt=0, last_error=NULL, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() WHERE id=:i"),
            {"i": row["id"]})
        requeued.append(row["step_id"])

    if requeued:
        # A re-queued step under a dead run never runs. The run comes back with it — the same
        # pairing `retry-step` makes, for the same reason.
        connection.execute(
            sql("UPDATE workflow_runs SET status='running', blocked_reason=NULL, error=NULL, "
                "updated_at=now() WHERE id=:r AND project_id=:p"),
            {"r": run_id, "p": project_id})

    return {
        "ok": True, "run_id": run_id, "checked": checked, "requeued": requeued,
        "note": (f"{len(requeued)} step(s) put back in the queue; they run on the next tick."
                 if requeued else
                 "Nothing was re-queued — no step's recorded blocker has cleared." if checked
                 else "No step on this run recorded a blocker that can be re-tested."),
    }


__all__ = ["blocker_status", "recheck_run", "CHECKERS", "STALE_CAPABLE"]
