"""A MERGE THAT FAILED IS NEVER SILENT, AND NEVER JUST DEAD-LETTERED.

THE CASE (Hello World, paramg/boss-sandbox, 2026-11-09 05:01 UTC). The person approved PR #237;
the merge job got GitHub's "405 Method Not Allowed" (the pull request is not mergeable), raised a
plain RuntimeError, and the worker dead-lettered it after ONE attempt. The decision still read
"approved", so the person believed it merged; PR #237 stayed open and nothing said otherwise.

THE RULE: every failed merge is classified and acted on, in words, where the person looks.
  * head_changed (409: the head moved under the merge) and transient (5xx, timeouts, resets):
    the job retries with backoff (`retryable`) — the same call can succeed next time.
  * not_mergeable (405), after BOSS's own recovery (update the branch from its base, then put a
    squash-merged branch back on its base: software_factory tiers 1 and 1b):
      - SUPERSEDED: every file the PR touches already reads the same on the default branch —
        its change was merged another way. BOSS closes it with a comment ("Superseded by #236,
        already merged"), withdraws the question, and the work reads done.
      - A REAL CONFLICT: retrying the same call can never succeed (why one attempt is right for
        the CALL), so the PR goes to the build agent to update its branch — a new attempt of the
        same lineage, pushing to the same PR — and the person is asked again only when that
        attempt opens it for merging. The question withdraws itself with that in words.
  * anything else (a refused permission, a protected branch): the question reopens with
    "Merge failed: <reason>" and what happens next, so it is in Needs you again.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

NOT_MERGEABLE = "not_mergeable"
HEAD_CHANGED = "head_changed"
TRANSIENT = "transient"
FAILED = "failed"
#: The recommendation key a merge question carries after its merge failed: {reason, next, kind}.
KEY = "merge_failed"
_TRANSIENT_MARKS = ("500 ", "502", "503", "504", "timed out", "timeout", "connecterror",
                    "connection reset", "remotedisconnected", "temporarily")


def classify(message: str) -> str:
    low = str(message or "").lower()
    # "does not merge cleanly" is a LOCAL change set's conflict (local_repo.LocalMergeConflict):
    # the same not-mergeable state as GitHub's 405, and it gets the same recovery (BOSSAPP-980).
    # Checked BEFORE the status-code heuristics: the message embeds branch names like
    # boss/code/crun_<hex>, and hex happily contains "409"/"405" — which would misread a real
    # conflict as a moved head and retry it forever.
    if "does not merge cleanly" in low:
        return NOT_MERGEABLE
    if "409" in low:
        return HEAD_CHANGED
    if "405" in low or "not mergeable" in low or "merge conflict" in low:
        return NOT_MERGEABLE
    if any(m in low for m in _TRANSIENT_MARKS):
        return TRANSIENT
    return FAILED


def reason_words(kind: str, message: str, number: Any) -> str:
    pr = f"PR #{number}" if number else "The pull request"
    if kind == NOT_MERGEABLE:
        return f"{pr} conflicts with its base branch, so GitHub would not merge it"
    if kind == HEAD_CHANGED:
        return f"{pr} changed while it was being merged"
    if kind == TRANSIENT:
        return "GitHub did not answer just now"
    from .software_factory import _github_words

    try:
        return _github_words(RuntimeError(message), doing=f"Couldn't merge {pr}")
    except Exception:  # noqa: BLE001
        return f"Couldn't merge {pr}: {str(message)[:160]}"


def _mark(connection: Any, decision_id: str, reason: str, next_step: str, kind: str, *,
          reopen: bool) -> None:
    """Say it on the decision: the summary leads with it, the recommendation carries it, and
    (reopen) the question is unanswered again so it is in Needs you with Merge as the retry."""
    patch = json.dumps({KEY: {"reason": reason, "next": next_step, "kind": kind}})
    note = f"Merge failed: {reason}. {next_step}"
    connection.execute(text(
        "UPDATE decisions SET recommendation = COALESCE(recommendation, '{}'::jsonb) || "
        "CAST(:patch AS JSONB) || jsonb_build_object('summary', CAST(:note AS TEXT) || ' ' || "
        "COALESCE(recommendation->>'summary', '')), resolution_note = :note, "
        + ("status = 'pending', approved_by = NULL, approved_at = NULL, " if reopen else "")
        + "updated_at = now() WHERE id = :d"), {"patch": patch, "note": note, "d": decision_id})


def _withdraw(connection: Any, project_id: str, decision_id: str, note: str,
              why: dict[str, Any]) -> None:
    """Withdraw a merge question whose approval can no longer act (approved or unanswered)."""
    from .core import emit_event

    changed = connection.execute(text(
        "UPDATE decisions SET status = 'withdrawn', resolution_note = :n, updated_at = now() "
        "WHERE id = :d AND status IN ('pending', 'delegated', 'expired', 'revision_requested', "
        "'approved')"), {"d": decision_id, "n": note[:1000]}).rowcount
    if changed:
        emit_event(connection, project_id, "decision.withdrawn", "boss", "decision",
                   decision_id, {**why, "why": note})


def _pr_runs(connection: Any, project_id: str, url: str) -> list[str]:
    return [str(r) for r in connection.execute(text(
        "SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u "
        "ORDER BY created_at DESC"), {"p": project_id, "u": url}).scalars().all()]


def hand_to_builder(connection: Any, settings: Any, run_id: str, note: str) -> Optional[str]:
    """Start the next attempt of the build's lineage to update its branch (it pushes to the
    same pull request). The new run's id, or None when BOSS may not start it by itself."""
    from .code_task_service import retry_code_task
    from .jobs import JobLeaseService

    engine = getattr(connection, "engine", None)
    if engine is None:
        return None
    try:
        out = retry_code_task(engine, settings, JobLeaseService(settings), run_id,
                              fix_note=note, automatic=True)
    except Exception as error:  # noqa: BLE001 — refused: the person is asked instead
        log.info("merge conflict hand-off refused for %s: %s", run_id, error)
        return None
    return str(out.get("run_id") or "") or None


def handle(service: Any, connection: Any, project_id: str, decision_id: str, number: int,
           url: str, branch_name: Optional[str], receipt_id: str, message: str) -> dict:
    """What a failed merge becomes. Returns the merge job's result: `retryable` asks the
    worker to retry; status superseded / needs_rebase are handled (the job succeeds); status
    failed is said on the decision and still fails the job."""
    from . import cleared_causes

    github = service.github
    kind = classify(message)
    reason = reason_words(kind, message, number)
    service.receipts.mark_failed(connection, receipt_id, {"message": message, "kind": kind})
    service.events.emit(connection, project_id, "software_factory.pull_request_merge_failed",
                        "release_manager", "receipt", receipt_id,
                        {"message": message, "pull_request_url": url, "kind": kind,
                         "reason": reason, "needs_conflict_resolution": kind == NOT_MERGEABLE})
    base = {"receipt_id": receipt_id, "decision_id": decision_id, "pull_request_url": url,
            "branch_name": branch_name}
    if kind in (HEAD_CHANGED, TRANSIENT):
        _mark(connection, decision_id, reason, "BOSS is trying the merge again.", kind,
              reopen=False)
        return {**base, "status": "failed", "retryable": True,
                "message": f"Merge failed: {reason}. BOSS is trying again."}
    if kind == NOT_MERGEABLE:
        superseded = False
        check = getattr(github, "changes_already_in", None)
        if callable(check):
            try:
                superseded = bool(check(int(number)))
            except Exception:  # noqa: BLE001 — unread: treated as a real conflict
                superseded = False
        if superseded:
            earlier = None
            merged_of = getattr(github, "last_merged_pull_request", None)
            if callable(merged_of) and branch_name:
                try:
                    earlier = merged_of(branch_name)
                except Exception:  # noqa: BLE001
                    earlier = None
            by = (f"Superseded by #{earlier['number']}, already merged"
                  if earlier and int(earlier["number"]) != int(number)
                  else "Superseded: its changes are already on the default branch")
            try:
                github.close_pull_request(int(number), f"{by}. Closed by BOSS: everything this "
                                                       "pull request changes is already merged.")
            except Exception as error:  # noqa: BLE001
                _mark(connection, decision_id, reason, f"{by}; BOSS could not close it "
                      f"({type(error).__name__}). Close it in GitHub.", kind, reopen=True)
                return {**base, "status": "failed", "message": f"Merge failed: {reason}."}
            words = f"{by}. BOSS closed PR #{number}; the work is done."
            _mark(connection, decision_id, f"PR #{number} repeats work already merged",
                  words, "superseded", reopen=False)
            _withdraw(connection, project_id, decision_id, words,
                      {"kind": "superseded_pull_request"})
            for run_id in _pr_runs(connection, project_id, url):
                cleared_causes.on_pull_request_settled(connection, run_id, "closed")
            service.events.emit(connection, project_id, "software_factory.pull_request_superseded",
                                "release_manager", "receipt", receipt_id,
                                {"pull_request_url": url, "words": words})
            return {**base, "status": "superseded", "message": words}
        runs = _pr_runs(connection, project_id, url)
        note = (f"PR #{number} conflicts with its base branch and GitHub will not merge it. "
                "Update this branch from the base branch, resolve every conflict keeping both "
                "the base's changes and this pull request's intent, and run the tests.")
        new_run = hand_to_builder(connection, service.settings, runs[0], note) if runs else None
        if new_run:
            words = (f"Merge failed: {reason}. BOSS handed it to the build agent ({new_run}) "
                     "to update the branch; you will be asked again when it can merge.")
            _mark(connection, decision_id, reason, "BOSS handed it to the build agent to "
                  f"update the branch ({new_run}); you will be asked again when it can merge.",
                  kind, reopen=False)
            _withdraw(connection, project_id, decision_id, words,
                      {"kind": "merge_conflict_handed_off", "run_id": new_run})
            return {**base, "status": "needs_rebase", "message": words}
        _mark(connection, decision_id, reason, "Retry the build to update its branch from the "
              "base branch, then merge again.", kind, reopen=True)
        return {**base, "status": "failed", "message": f"Merge failed: {reason}."}
    _mark(connection, decision_id, reason, "Fix that, then press Merge to try again.", kind,
          reopen=True)
    return {**base, "status": "failed", "message": f"Merge failed: {reason}."}


def on_job_deadlettered(connection: Any, job_row: dict[str, Any], error: dict[str, Any]) -> None:
    """The merge job died for good (jobs._deadletter): say so ON the decision it was executing.

    BOSSAPP-982 (helpdesk-laptop, dec_4fe0…/job_bb61…, 2026-11-10): POST /v1/decisions/{id}/
    approve answered 200 "approved", the merge job dead-lettered, and the decision slid back
    with nothing current on it saying why — the person saw their approval vanish. Whatever path
    the job died on — including one that never reached handle() — the decision now carries
    "Merge failed: …" and is answerable again, so Merge is a visible retry, never silence.

    A decision handle() already spoke for (pending, with its merge_failed note) is left as it
    is; one still APPROVED carries a promise the dead job can no longer keep, so it reopens
    even over an earlier, now stale, "BOSS is trying the merge again" note."""
    payload = job_row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    decision_id = str(payload.get("decision_id") or "")
    if not decision_id:
        return
    row = connection.execute(text(
        "SELECT project_id, status, recommendation FROM decisions WHERE id = :d"),
        {"d": decision_id}).mappings().first()
    if row is None:
        return
    rec = row["recommendation"]
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = {}
    status = str(row["status"] or "")
    already_said = isinstance(rec, dict) and bool(rec.get(KEY))
    if status != "approved" and not (status == "pending" and not already_said):
        return
    message = str((error or {}).get("message") or "the merge job failed")
    kind = classify(message)
    reason = reason_words(kind, message, payload.get("pull_request_number"))
    _mark(connection, decision_id, reason,
          "The merge job stopped after its last attempt (dead-lettered). "
          "Press Merge to try again.", kind, reopen=True)
    from .core import emit_event

    emit_event(connection, str(row["project_id"]), "decision.merge_job_deadlettered", "boss",
               "decision", decision_id,
               {"job_id": str(job_row.get("id") or ""), "reason": reason})


__all__ = ["FAILED", "HEAD_CHANGED", "KEY", "NOT_MERGEABLE", "TRANSIENT", "classify",
           "hand_to_builder", "handle", "on_job_deadlettered", "reason_words"]
