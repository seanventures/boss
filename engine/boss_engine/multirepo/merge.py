"""Merge a change set AS A SET (the `change_set.merge` job, after the one approval).

  1. Every PR's head is read again and must equal the SHA the approval covered. If one moved,
     nothing merges: the approval is withdrawn and BOSS asks again.
  2. Merges go in dependency order, each a required-SHA merge (GitHub refuses if the head moved
     in between).
  3. If any merge fails, every merge that already landed is REVERTED — through a revert pull
     request merged under the same approval, newest first — and the set is `failed`. It is never
     left half-merged. A revert that GitHub refuses is said in words, by repository, for a person.

Idempotent: a re-delivered job skips the PRs already merged and carries on.

Two doors, one rule. The worker hands its leased job to `execute_job` (POST
/v1/change-sets/{id}/merge/execute, a worker lease protocol): only the worker principal, only under
the job's live lease, only for the change set and the decision that job was queued for. A person
uses POST /v1/change-sets/{id}/merge, which the API holds for confirmation when a non-human calls
it. Both end in `merge_set`, which re-reads the approval itself and merges nothing without it.
"""
from __future__ import annotations

import logging
from typing import Any

from .common import MultiRepoError, event, jload, sql

log = logging.getLogger(__name__)


def merge_set(db: Any, settings: Any, cs_id: str, *, actor: str = "boss") -> dict[str, Any]:
    from . import change_sets, github, grants

    cs = change_sets._row(db, cs_id, lock=True)  # noqa: SLF001
    if cs["state"] in ("merged", "deploying", "live"):
        return {"status": "merged", "change_set_id": cs_id, "already": True}
    if cs["state"] not in ("approved", "merging"):
        raise MultiRepoError(409, "not_approved", f"This change is {cs['state']}, not approved; nothing was merged.")
    approved = jload(cs.get("approved_shas"), {}) or {}
    _require_the_approval(db, cs, approved)
    lanes = [lane for lane in change_sets._lanes(db, cs_id) if lane["lane_state"] != "unchanged"]  # noqa: SLF001
    lanes.sort(key=lambda x: (x["wave"], x["full_name"]))
    repos = {lane["full_name"]: github.Repo(db, settings, cs["project_id"], cs_id, lane["full_name"]) for lane in lanes}

    # 1. THE HEADS ARE STILL WHAT WAS APPROVED.
    moved = []
    for lane in lanes:
        if lane.get("merged_sha"):
            continue
        head = (repos[lane["full_name"]].pull(lane["pr_number"]).get("head") or {}).get("sha")
        if head != approved.get(lane["full_name"]) or head != lane["head_sha"]:
            moved.append(lane["full_name"])
    if moved:
        change_sets._withdraw_decision(db, cs, "new commits on " + ", ".join(moved))  # noqa: SLF001
        change_sets._set_state(db, cs_id, "building")  # noqa: SLF001
        event(db, cs["project_id"], "change_set.approval_stale", actor, "change_set", cs_id, {"moved": moved})
        return {"status": "stale", "moved": moved, "change_set_id": cs_id}

    change_sets._set_state(db, cs_id, "merging")  # noqa: SLF001
    landed: list[dict[str, Any]] = [lane for lane in lanes if lane.get("merged_sha")]
    title = cs["title"]
    for lane in lanes:
        if lane.get("merged_sha"):
            continue
        answer = repos[lane["full_name"]].merge(lane["pr_number"], approved[lane["full_name"]],
                                                title=f"{title} (#{lane['pr_number']})")
        if answer.status == 200 and isinstance(answer.body, dict) and answer.body.get("merged") is not False:
            sha = str(answer.body.get("sha") or "")
            db.execute(sql("UPDATE change_set_prs SET merged_sha = :s, merged_at = now(), lane_state = 'merged', "
                           "updated_at = now() WHERE id = :i"), {"s": sha, "i": lane["id"]})
            landed.append({**lane, "merged_sha": sha})
            continue
        why = str((answer.body or {}).get("message") if isinstance(answer.body, dict) else "") or f"HTTP {answer.status}"
        db.execute(sql("UPDATE change_set_prs SET error = :e, updated_at = now() WHERE id = :i"),
                   {"e": f"Merge refused: {why}"[:500], "i": lane["id"]})
        reverted, stuck = _revert(db, settings, cs, repos, landed, failed=lane["full_name"], actor=actor)
        sentence = (f"{lane['full_name']}#{lane['pr_number']} could not be merged ({why[:160]}). "
                    + (f"Reverted {', '.join(r.split('/')[-1] for r in reverted)} so nothing is half-merged."
                       if reverted else "Nothing had merged yet.")
                    + (f" REVERT FAILED for {', '.join(stuck)}: revert by hand." if stuck else ""))
        db.execute(sql("UPDATE repo_change_sets SET state = 'failed', error = :e, updated_at = now() WHERE id = :i"),
                   {"e": sentence, "i": cs_id})
        grants.end(db, cs_id)
        event(db, cs["project_id"], "change_set.merge_failed", actor, "change_set", cs_id,
              {"failed": lane["full_name"], "reverted": reverted, "revert_failed": stuck})
        return {"status": "failed", "change_set_id": cs_id, "failed": lane["full_name"], "reverted": reverted,
                "revert_failed": stuck, "message": sentence}

    db.execute(sql("UPDATE repo_change_sets SET state = 'merged', error = NULL, updated_at = now() WHERE id = :i"),
               {"i": cs_id})
    event(db, cs["project_id"], "change_set.merged", actor, "change_set", cs_id,
          {"merged": {lane["full_name"]: lane.get("merged_sha") for lane in landed}})
    from . import release

    release.after_merge(db, settings, cs_id, actor=actor)
    return {"status": "merged", "change_set_id": cs_id,
            "merged": {lane["full_name"]: lane.get("merged_sha") for lane in landed}}


#: The decision states that still stand behind a merge (`approved`; `executed` once acted on).
_STANDING = ("approved", "executed")


def _require_the_approval(db: Any, cs: dict[str, Any], approved: dict[str, Any]) -> None:
    """The set's state alone is not the approval: its ONE decision must still be approved, and the
    SHAs it pinned must be on the set. A withdrawn or expired decision merges nothing."""
    status = None
    if cs.get("decision_id"):
        status = db.execute(sql("SELECT status FROM decisions WHERE id = :d AND project_id = :p"),
                            {"d": cs["decision_id"], "p": cs["project_id"]}).scalar()
    if not approved or status not in _STANDING:
        raise MultiRepoError(409, "not_approved",
                             "This change has no standing approval (" + (status or "no decision")
                             + "); nothing was merged.")


def execute_job(db: Any, settings: Any, cs_id: str, *, job_id: str, worker_id: str,
                lease_token: str) -> dict[str, Any]:
    """The worker's leased `change_set.merge` job. Fenced by the job's live lease, and the job must
    be the merge of THIS set under the decision the set still carries — a job queued for an older
    approval never merges the newer content. Then `merge_set`, which re-checks the approval."""
    from ..services.jobs import JobLeaseService, LeaseNotFoundError
    from . import change_sets

    try:
        job = JobLeaseService(settings).require_active_lease(db, worker_id, job_id, lease_token)
    except LeaseNotFoundError as exc:
        raise MultiRepoError(409, "lease_lost", f"This merge job's lease is no longer held: {exc}") from exc
    payload = job.get("payload")
    payload = payload if isinstance(payload, dict) else (jload(payload, {}) or {})
    if job.get("kind") != change_sets.MERGE_JOB or str(payload.get("change_set_id") or "") != cs_id:
        raise MultiRepoError(409, "wrong_job", "This job is not the merge of this change.")
    cs = change_sets._row(db, cs_id, lock=True)  # noqa: SLF001
    if cs["project_id"] != job.get("project_id"):
        raise MultiRepoError(409, "wrong_job", "This job is not the merge of this change.")
    if cs["state"] not in ("merged", "deploying", "live") and payload.get("decision_id") != cs.get("decision_id"):
        raise MultiRepoError(409, "stale_decision", "This merge was queued for an older approval; nothing was merged.")
    return {**merge_set(db, settings, cs_id, actor="boss"), "project_id": cs["project_id"]}


def _revert(db: Any, settings: Any, cs: dict[str, Any], repos: dict[str, Any], landed: list[dict[str, Any]], *,
            failed: str, actor: str) -> tuple[list[str], list[str]]:
    """Undo every landed merge, newest first, through revert PRs merged under the same approval."""
    reverted, stuck = [], []
    for lane in reversed(landed):
        repo = repos[lane["full_name"]]
        base = lane.get("base_branch") or "main"
        branch = f"boss/revert-{cs['id'][-10:]}-{lane['full_name'].split('/')[-1]}"
        done = repo.revert_merge(
            merged_sha=lane["merged_sha"], base=base, branch=branch,
            title=f"Revert “{cs['title']}” in {lane['full_name']}",
            body=(f"BOSS reverts this repository's part of change {cs['id']}: {failed} could not be merged, "
                  "and a change across several repositories is never left half-merged."))
        if done.get("ok"):
            db.execute(sql("UPDATE change_set_prs SET lane_state = 'reverted', revert_pr_number = :n, revert_sha = :s, "
                           "updated_at = now() WHERE id = :i"),
                       {"n": done.get("pr_number"), "s": done.get("sha"), "i": lane["id"]})
            reverted.append(lane["full_name"])
        else:
            db.execute(sql("UPDATE change_set_prs SET error = :e, updated_at = now() WHERE id = :i"),
                       {"e": f"Revert failed: {done.get('reason')}"[:500], "i": lane["id"]})
            stuck.append(lane["full_name"])
            log.error("change set %s: revert of %s failed: %s", cs["id"], lane["full_name"], done.get("reason"))
    if reverted:
        event(db, cs["project_id"], "change_set.reverted", actor, "change_set", cs["id"], {"reverted": reverted})
    return reverted, stuck
