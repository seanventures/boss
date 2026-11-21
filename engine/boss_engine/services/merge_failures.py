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


def is_local_change(url: Any = None, message: Any = None) -> bool:
    """A change set BOSS keeps on this machine (local_repo: `http://boss-local/local_<p>/…`, a
    LocalMergeConflict's "does not merge cleanly"), not a GitHub pull request."""
    u = str(url or "")
    return "://boss-local/" in u or "/local_" in u or "does not merge cleanly" in str(message or "")


def reason_words(kind: str, message: str, number: Any, url: Any = None) -> str:
    local = is_local_change(url, message)
    pr = (f"Change #{number}" if local else f"PR #{number}") if number else (
        "The change" if local else "The pull request")
    where = "BOSS could not merge it on this machine" if local else "GitHub would not merge it"
    if kind == NOT_MERGEABLE:
        return f"{pr} conflicts with its base branch, so {where}"
    if kind == HEAD_CHANGED:
        return f"{pr} changed while it was being merged"
    if kind == TRANSIENT:
        return ("the repository did not answer just now" if local
                else "GitHub did not answer just now")
    from .software_factory import _github_words

    try:
        return _github_words(RuntimeError(message), doing=f"Couldn't merge {pr}")
    except Exception:  # noqa: BLE001
        return f"Couldn't merge {pr}: {str(message)[:160]}"


def _mark(connection: Any, decision_id: str, reason: str, next_step: str, kind: str, *,
          reopen: bool, handed_to: Optional[str] = None) -> None:
    """Say it on the decision: the summary leads with it, the recommendation carries it, and
    (reopen) the question is unanswered again so it is in Needs you with Merge as the retry."""
    patch = json.dumps({KEY: {"reason": reason, "next": next_step, "kind": kind,
                              **({"handed_to": handed_to} if handed_to else {})}})
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


def repository_of(url: Any) -> str:
    """`owner/name` of a pull request or local change URL (`…/<owner>/<name>/pull/<n>`), or ''."""
    parts = [x for x in str(url or "").split("/") if x]
    if "pull" in parts:
        i = parts.index("pull")
        if i >= 2:
            return f"{parts[i - 2]}/{parts[i - 1]}"
    return ""


def _pr_runs(connection: Any, project_id: str, url: str, decision_id: Optional[str] = None,
             branch_name: Optional[str] = None) -> list[str]:
    """The builds behind a pull request or a LOCAL change set, newest first — ONLY when the
    match is certain, by the most exact evidence first (the first tier that finds any wins):
      1. builds whose pull_request_url IS this URL;
      2. the build its merge question names (`recommendation.run_id`), in this repository;
      3. the local change set of THIS repository with this number or branch;
      4. a `boss/code/<run>` branch's run, in this repository.
    A multi-repository project has a #9 in each repository: a number or a branch alone never
    picks another repository's build. No certain match: [] — the caller keeps the person's card
    with its steps, never withdraws it (northgate-gateway #9, 2026-11-21; review of #264)."""
    repo = repository_of(url).lower()

    def in_repo(ids: list[Any]) -> list[str]:
        ids = [str(i) for i in ids if i]
        if not ids:
            return []
        rows = connection.execute(text(
            "SELECT id, lower(COALESCE(spec->>'owner', '') || '/' || COALESCE(spec->>'repo', '')) "
            "AS repo, pull_request_url FROM code_task_runs WHERE project_id = :p AND id = ANY(:ids) "
            "ORDER BY created_at DESC"), {"p": project_id, "ids": ids}).mappings().all()
        return [str(r["id"]) for r in rows
                if repo and (r["repo"] == repo or repository_of(r["pull_request_url"]).lower() == repo)]

    exact = [str(r) for r in connection.execute(text(
        "SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u "
        "ORDER BY created_at DESC"), {"p": project_id, "u": url}).scalars().all()] if url else []
    if exact:
        return exact
    if decision_id:
        named = in_repo([connection.execute(text(
            "SELECT recommendation->>'run_id' FROM decisions WHERE id = :d AND project_id = :p"),
            {"d": decision_id, "p": project_id}).scalar()])
        if named:
            return named
    if repo:
        number = str(url or "").rstrip("/").rsplit("/", 1)[-1]
        try:
            with connection.begin_nested():
                local = connection.execute(text(
                    "SELECT run_id FROM local_change_sets WHERE project_id = :p AND "
                    "lower(repository) = :r AND (CAST(number AS text) = :n OR branch = :b) "
                    "ORDER BY updated_at DESC"),
                    {"p": project_id, "r": repo, "n": number if number.isdigit() else "",
                     "b": branch_name or ""}).scalars().all()
        except Exception:  # noqa: BLE001 — an install without local change sets
            local = []
        found = connection.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p AND id = ANY(:ids) "
            "ORDER BY created_at DESC"), {"p": project_id, "ids": [str(x) for x in local if x]}
        ).scalars().all() if local else []
        if found:
            return [str(x) for x in found]
    if branch_name and branch_name.startswith("boss/code/"):
        return in_repo([branch_name[len("boss/code/"):]])
    return []


def conflict_note(number: Any, url: Any, message: Any) -> str:
    """What the build agent is told about a conflict: keep both sides' intent, test, re-ask."""
    local = is_local_change(url, message)
    what = f"Change #{number}" if local else f"PR #{number}"
    files = ""
    said = str(message or "")
    if ": " in said and " conflict" in said:
        files = (" Conflicting files: "
                 + said.split(": ", 1)[1].split(" conflict", 1)[0][:300].rstrip(".") + ".")
    return (f"{what} conflicts with its base branch, so it cannot be merged.{files} Update this "
            "branch from the base branch and resolve every conflict keeping BOTH the base's "
            "changes and this change's intent (never drop either side to make it merge), run "
            "the tests, and open it for merging again.")


def conflict_steps(runs: list[str]) -> str:
    """The exact steps for a person when BOSS could not hand the conflict over itself."""
    where = f"build {runs[0]}" if runs else "the build that made this change"
    return (f"BOSS could not start the build agent on this itself. Open {where}, press Retry "
            "with the note 'Update from the base branch and resolve the conflict, keeping both "
            "sides', and BOSS asks you to merge again when it passes.")


def hand_off_conflict(connection: Any, settings: Any, project_id: str, decision_id: str,
                      message: str = "") -> Optional[str]:
    """A merge question whose merge failed on a conflict and went back to a person: hand it to
    the build agent now (failure_owner's remedy for an open merge-conflict card). The new run's
    id (and the question withdrawn), or None."""
    # ONE HAND-OFF PER QUESTION, WHOEVER SWEEPS: the question is locked (a concurrent sweep
    # holding it skips, doing nothing) and re-read inside the lock — already handed off, nothing.
    row = connection.execute(text(
        "SELECT recommendation FROM decisions WHERE id = :d AND project_id = :p "
        "FOR UPDATE SKIP LOCKED"), {"d": decision_id, "p": project_id}).scalar()
    if row is None:
        return None
    rec = row if isinstance(row, dict) else json.loads(row or "{}")
    failed = rec.get(KEY) if isinstance(rec.get(KEY), dict) else {}
    if failed.get("kind") != NOT_MERGEABLE or failed.get("handed_to"):
        return None
    url = str(rec.get("pull_request_url") or rec.get("url") or "")
    number = rec.get("pull_request_number") or str(url).rstrip("/").rsplit("/", 1)[-1]
    runs = _pr_runs(connection, project_id, url, decision_id, rec.get("branch_name"))
    if not runs:
        return None
    new_run = hand_to_builder(connection, settings, runs[0],
                              conflict_note(number, url, message or failed.get("reason")))
    if not new_run:
        return None
    words = (f"Merge failed: {failed.get('reason') or 'a conflict'}. BOSS handed it to the build "
             f"agent ({new_run}) to update the branch; you will be asked again when it can merge.")
    _mark(connection, decision_id, str(failed.get("reason") or "conflict"),
          f"BOSS handed it to the build agent to update the branch ({new_run}); you will be "
          "asked again when it can merge.", NOT_MERGEABLE, reopen=False, handed_to=new_run)
    _withdraw(connection, project_id, decision_id, words,
              {"kind": "merge_conflict_handed_off", "run_id": new_run})
    return new_run


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
        log.warning("merge conflict hand-off refused for %s: %s", run_id, error)
        return None
    new_run = str(out.get("run_id") or "")
    if not new_run or new_run == run_id or out.get("created") is False:
        # Not a NEW attempt (another run already owns the branch, or nothing started): it is
        # not a hand-off, so the person's card is never withdrawn on it.
        log.warning("merge conflict hand-off for %s started no new build (%s)", run_id, out)
        return None
    return new_run


def handle(service: Any, connection: Any, project_id: str, decision_id: str, number: int,
           url: str, branch_name: Optional[str], receipt_id: str, message: str) -> dict:
    """What a failed merge becomes. Returns the merge job's result: `retryable` asks the
    worker to retry; status superseded / needs_rebase are handled (the job succeeds); status
    failed is said on the decision and still fails the job."""
    from . import cleared_causes

    github = service.github
    kind = classify(message)
    reason = reason_words(kind, message, number, url)
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
        runs = _pr_runs(connection, project_id, url, decision_id, branch_name)
        note = conflict_note(number, url, message)
        new_run = hand_to_builder(connection, service.settings, runs[0], note) if runs else None
        if new_run:
            words = (f"Merge failed: {reason}. BOSS handed it to the build agent ({new_run}) "
                     "to update the branch; you will be asked again when it can merge.")
            _mark(connection, decision_id, reason, "BOSS handed it to the build agent to "
                  f"update the branch ({new_run}); you will be asked again when it can merge.",
                  kind, reopen=False, handed_to=new_run)
            _withdraw(connection, project_id, decision_id, words,
                      {"kind": "merge_conflict_handed_off", "run_id": new_run})
            return {**base, "status": "needs_rebase", "message": words}
        _mark(connection, decision_id, reason, conflict_steps(runs), kind, reopen=True)
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
    reason = reason_words(kind, message, payload.get("pull_request_number"),
                          payload.get("pull_request_url"))
    _mark(connection, decision_id, reason,
          "The merge job stopped after its last attempt (dead-lettered). "
          "Press Merge to try again.", kind, reopen=True)
    from .core import emit_event

    emit_event(connection, str(row["project_id"]), "decision.merge_job_deadlettered", "boss",
               "decision", decision_id,
               {"job_id": str(job_row.get("id") or ""), "reason": reason})


__all__ = ["FAILED", "HEAD_CHANGED", "KEY", "NOT_MERGEABLE", "TRANSIENT", "classify",
           "conflict_note", "conflict_steps", "hand_off_conflict", "hand_to_builder",
           "recover_orphaned_conflicts",
           "is_local_change", "handle", "on_job_deadlettered", "reason_words"]


#: The marker the owner loop's stale-card cleanup left on the cards it withdrew before #270
#: (failure_owner._withdraw_own_card, `decision.withdrawn` by agent:shepherd with this why).
#: ONLY those are orphans: a card a person, a cancelled run, cleared_causes or a stale-build
#: sweep withdrew was withdrawn on purpose and is never revived.
ORPHAN_ACTOR = "agent:shepherd"
ORPHAN_WHY = "BOSS's agent fixes it itself"
#: What a builder told to resolve a conflict was told (conflict_note): an existing later attempt
#: is linked as the hand-off only when it was given this.
CONFLICT_NOTE_MARK = "conflicts with its base branch"


def recover_orphaned_conflicts(engine: Any, settings: Any, *, project_id: Optional[str] = None,
                               limit: int = 3, reach_days: int = 14) -> list[dict[str, Any]]:
    """A MERGE CONFLICT THE OWNER LOOP ORPHANED IS PICKED UP AGAIN (northgate-gateway #9).

    Only a merge question the owner loop's stale-card cleanup withdrew (its `decision.withdrawn`
    event, ORPHAN_ACTOR / ORPHAN_WHY), whose merge failed on a conflict, with no builder linked
    (`merge_failed.handed_to`), on a change still OPEN (a closed PR or change set is a person's
    "no"), whose build lineage was not cancelled. Each is locked (FOR UPDATE SKIP LOCKED: a
    concurrent sweep does nothing) and re-checked inside the lock; then an existing later
    attempt that was told the conflict note is linked, or the conflict is handed to the build
    agent, or — when it cannot start — the question is reopened with the exact steps."""
    from .core import emit_event

    done: list[dict[str, Any]] = []
    with engine.begin() as c:
        ids = c.execute(text(
            "SELECT d.id FROM decisions d WHERE d.status = 'withdrawn' "
            "AND d.recommendation->'merge_failed'->>'kind' = :k "
            "AND d.recommendation->'merge_failed'->>'handed_to' IS NULL "
            "AND d.updated_at > now() - make_interval(days => :days) "
            "AND (CAST(:p AS text) IS NULL OR d.project_id = :p) "
            "AND EXISTS (SELECT 1 FROM events e WHERE e.project_id = d.project_id "
            "  AND e.entity_id = d.id AND e.event_type = 'decision.withdrawn' "
            "  AND e.actor = :who AND e.data->>'why' = :why) "
            "ORDER BY d.updated_at DESC LIMIT :n"),
            {"k": NOT_MERGEABLE, "days": reach_days, "p": project_id, "n": limit,
             "who": ORPHAN_ACTOR, "why": ORPHAN_WHY}).scalars().all()
    for decision_id in ids:
        with engine.begin() as c:
            row = c.execute(text(
                "SELECT id, project_id, recommendation FROM decisions WHERE id = :d "
                "AND status = 'withdrawn' "
                "AND recommendation->'merge_failed'->>'handed_to' IS NULL "
                "FOR UPDATE SKIP LOCKED"), {"d": decision_id}).mappings().first()
            if row is None:
                continue                      # another sweep has it, or it was handed off
            pid = str(row["project_id"])
            rec = row["recommendation"] if isinstance(row["recommendation"], dict) else \
                json.loads(row["recommendation"] or "{}")
            url = str(rec.get("pull_request_url") or "")
            runs = _pr_runs(c, pid, url, decision_id, rec.get("branch_name"))
            if not change_open(c, pid, url, settings=settings) or _lineage_stopped(c, runs):
                continue
            later = c.execute(text(
                "SELECT n.id FROM code_task_runs n, code_task_runs o WHERE o.id = :newest AND "
                "n.project_id = o.project_id AND COALESCE(n.finalize_ctx->>'root_run_id', n.id) = "
                "COALESCE(o.finalize_ctx->>'root_run_id', o.id) AND n.created_at > o.created_at "
                "AND n.id <> ALL(:runs) AND (n.finalize_ctx->'feedback_history'->-1->>'fix_note') "
                "LIKE :mark ORDER BY n.created_at DESC LIMIT 1"),
                {"runs": runs or [""], "newest": (runs or [""])[0],
                 "mark": f"%{CONFLICT_NOTE_MARK}%"}).scalar()
            if later:
                c.execute(text(
                    "UPDATE decisions SET recommendation = jsonb_set(recommendation, "
                    "'{merge_failed,handed_to}', to_jsonb(CAST(:r AS text))) WHERE id = :d"),
                    {"r": later, "d": decision_id})
                done.append({"decision_id": decision_id, "linked": str(later)})
                continue
            new_run = _hand_off_locked(c, settings, pid, decision_id, rec, runs)
            if new_run is None:
                failed = rec.get(KEY) or {}
                _mark(c, decision_id, str(failed.get("reason") or "a conflict"),
                      conflict_steps(runs), NOT_MERGEABLE, reopen=True)
            emit_event(c, pid, "merge.conflict_orphan_recovered", "boss", "decision",
                       decision_id, {"handed_to": new_run, "reopened": new_run is None})
        done.append({"decision_id": decision_id, "handed_to": new_run,
                     "reopened": new_run is None})
    return done


def _hand_off_locked(c: Any, settings: Any, project_id: str, decision_id: str,
                     rec: dict[str, Any], runs: list[str]) -> Optional[str]:
    """The hand-off for a question this transaction already holds locked."""
    if not runs:
        return None
    failed = rec.get(KEY) or {}
    url = str(rec.get("pull_request_url") or "")
    number = rec.get("pull_request_number") or url.rstrip("/").rsplit("/", 1)[-1]
    new_run = hand_to_builder(c, settings, runs[0],
                              conflict_note(number, url, failed.get("reason")))
    if not new_run:
        return None
    _mark(c, decision_id, str(failed.get("reason") or "conflict"),
          f"BOSS handed it to the build agent to update the branch ({new_run}); you will be "
          "asked again when it can merge.", NOT_MERGEABLE, reopen=False, handed_to=new_run)
    return new_run


def _lineage_stopped(c: Any, runs: list[str]) -> bool:
    """A person (or BOSS) cancelled the change's build lineage or its workflow run: on purpose."""
    if not runs:
        return False
    newest = c.execute(text(
        "SELECT n.status, n.finalize_ctx->>'workflow_run_id' AS wfr FROM code_task_runs n, "
        "code_task_runs o WHERE o.id = :r AND n.project_id = o.project_id AND "
        "COALESCE(n.finalize_ctx->>'root_run_id', n.id) = COALESCE(o.finalize_ctx->>'root_run_id', "
        "o.id) ORDER BY n.created_at DESC LIMIT 1"), {"r": runs[0]}).mappings().first()
    if newest is None:
        return False
    if str(newest["status"]) in ("cancelled", "rejected"):
        return True
    if newest["wfr"]:
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :w"),
                           {"w": newest["wfr"]}).scalar()
        return str(status or "") in ("cancelled", "rejected")
    return False


#: How a GitHub pull request's state is read: (connection, project_id, owner/repo, number,
#: settings) -> "open" | "merged" | "closed" | None (unknown). One seam; tests replace it.
def _read_github_pr_state(c: Any, project_id: str, owner_repo: str, number: int,
                          settings: Any) -> Optional[str]:
    from .software_factory import SoftwareFactoryService

    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    owner, repo = owner_repo.split("/", 1)
    factory = SoftwareFactoryService(settings, owner=owner, repo=repo)
    try:
        factory.bind_project_github(c, project_id, owner=owner, repo=repo)
    except Exception:  # noqa: BLE001 — the env-default adapter may still answer
        log.debug("orphan check could not bind the project's GitHub", exc_info=True)
    try:
        settled = factory._pull_request_settled(int(number))  # noqa: SLF001 — the one read
        if settled is not None:
            return str(settled.get("state") or "") or None
        read = getattr(factory.github, "pull_request_state", None)
        if callable(read):
            return str((read(int(number)) or {}).get("state") or "") or None
    except Exception:  # noqa: BLE001 — unknown
        log.info("orphan check could not read %s#%s", owner_repo, number, exc_info=True)
    return None


PR_STATE_READER = _read_github_pr_state


def _still_open(engine: Any, project_id: str, url: str, settings: Any = None) -> bool:
    with engine.begin() as c:
        return change_open(c, project_id, url, settings=settings)


def change_open(c: Any, project_id: str, url: str, *, settings: Any = None) -> bool:
    """Is the change still waiting to merge? A local change set says so itself; a GitHub pull
    request is READ (merged or closed — a person closing it without merging is a "no" — is not
    open, and a state BOSS cannot read is not assumed open); a build of it with a merged sha
    settles it either way."""
    repo = repository_of(url)
    number = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    merged = c.execute(text(
        "SELECT 1 FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u AND "
        "finalize_ctx->'merge'->>'sha' IS NOT NULL LIMIT 1"), {"p": project_id, "u": url}
    ).scalar()
    if not url or merged:
        return False
    if is_local_change(url) and repo and number.isdigit():
        try:
            with c.begin_nested():
                state = c.execute(text(
                    "SELECT state FROM local_change_sets WHERE project_id = :p AND "
                    "lower(repository) = lower(:r) AND number = :n"),
                    {"p": project_id, "r": repo, "n": int(number)}).scalar()
            if state is not None:
                return state == "open"
        except Exception:  # noqa: BLE001 — no local change sets here
            pass
        return False
    if not repo or not number.isdigit():
        return False
    return PR_STATE_READER(c, project_id, repo, int(number), settings) == "open"
