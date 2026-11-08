"""A QUESTION WHOSE CAUSE HAS CLEARED IS WITHDRAWN — automatically, with an event.

THE CASE (helpdesk-ai-demo, 2026-11-06). A merge failed with "No GitHub credentials for /" and
raised the blocking decision "GitHub integration is not configured". The merge was fixed and
PR #1 merged — and the decision sat in Needs you, asking for a GitHub token nobody needed to
give, until the operator rejected it by hand. Beside it, a merge question ("Approve code change
… (PR #1)") stayed pending after the pull request had been merged in GitHub. Each was raised by a
FAILURE or a WAIT that later went away; nothing owned "the reason for this question is gone".

THE RULE, IN ONE PLACE. A decision says what would clear it, and this module withdraws it when
that happens. Two kinds of cause, each a registry, so a new one is a line here, not a patch at
the place that happened to notice:

  * AN EFFECT THAT FAILED (`EFFECT_FAILURE_CAUSES`, keyed by the decision's reason code): cleared
    when an effect of the kinds it names SUCCEEDS afterwards in the same project — for the
    same repository when the decision names one — or when the pull request it was about is
    merged. A successful GitHub write proves the GitHub integration is configured.
  * A QUESTION ABOUT A PULL REQUEST (`PULL_REQUEST_QUESTIONS`, keyed by the decision's action):
    cleared when that pull request is merged (recorded by pr_merge) or closed (observed).

  * A DEPLOY REQUEST THAT WAS OVERTAKEN (`DEPLOY_QUESTIONS`, a stack deploy's `deploy_stack`):
    cleared when its target went live with a LATER deploy after it was asked (approving it
    would roll the app back), or — for a workflow's deploy step — when the target already
    serves exactly that commit, healthy (nothing left to ship). helpdesk-ai-demo, 2026-11-06:
    "Deploy …@cfe343f1c6cd" sat in Needs you after 6ba6500 and f3f130c had both gone live on
    the same target, and "Deploy …@f3f130cb7f74" was asked again seven minutes after that very
    commit went live. A person's own redeploy (no workflow run) of the live commit stays.
    When withdrawn, the deployment is closed (`rejected`, failure_reason "Superseded …", its
    ChangeSet and reservation released) and a workflow step parked on it follows the deploy
    that overtook it, so the run moves on instead of waiting forever.

Three ways in, all through `withdraw_cleared`'s one withdrawal (run_decisions._withdraw_with_note:
status `withdrawn`, a `resolution_note` in words and a `decision.withdrawn` event naming the
cause), so none can drift from the others:
  * `on_effect_succeeded` — ReceiptService.mark_succeeded, the moment an effect lands;
  * `on_pull_request_settled` — pr_merge.record (merged) and pr_merge.observe (closed);
  * `withdraw_cleared` — the sweep on the delivery tick and at boot, for causes that cleared
    while nothing was listening (and for decisions raised before this rule).

Only facts BOSS has on record are used: no GitHub call is made here. Never raises from a hook:
a question left open is a small problem, a merge that fails because its bookkeeping did is not.
apps/api/tests/test_a_cleared_cause_withdraws_its_question.py pins it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Mapping, Optional

from sqlalchemy import bindparam, text

log = logging.getLogger(__name__)

#: reason code of a failed-effect decision -> effect-kind prefixes whose later success clears it.
EFFECT_FAILURE_CAUSES: dict[str, tuple[str, ...]] = {
    "missing_github_credentials": ("github.",),
}

#: decision actions that ask to ship one deploy, and are moot once a later one went live.
DEPLOY_QUESTIONS: frozenset[str] = frozenset({"deploy_stack"})

#: decision actions that ask about one pull request, and are moot once it is merged or closed.
PULL_REQUEST_QUESTIONS: frozenset[str] = frozenset({"review_pull_request"})

#: decision actions that say a build is STUCK, and are moot once every build they name moves
#: again (an agent turn after the card was raised — stuck_work.moved_since), finishes, or is
#: cancelled. Laptop, 2026-11-07: "Restart the step, or cancel the build." sat in Needs you while
#: the same build was planning, reviewing its plan and calling tools.
STUCK_QUESTIONS: frozenset[str] = frozenset({"stuck_work_needs_person"})

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _repo_of(rec: Mapping[str, Any]) -> Optional[str]:
    """The repository a decision is about: its own `repository` when it names a real one,
    else the one its pull request's URL names. None: project-wide."""
    repo = str(rec.get("repository") or "").strip()
    if _REPO.match(repo):
        return repo
    from .ci_gate import parse_pr_url

    parsed = parse_pr_url(str(rec.get("pull_request_url") or ""))
    return parsed[0] if parsed else None


def failed_effect_cause(effect_kind: str, *, repository: Optional[str] = None,
                        pull_request_url: Optional[str] = None,
                        pull_request_number: Optional[int] = None) -> dict[str, Any]:
    """What a decision raised for a failed effect records, so this module can tell when it
    cleared: put these keys in its recommendation."""
    out: dict[str, Any] = {"effect_kind": effect_kind}
    if repository and _REPO.match(str(repository)):
        out["repository"] = str(repository)
    if pull_request_url:
        out["pull_request_url"] = str(pull_request_url)
    if pull_request_number:
        out["pull_request_number"] = int(pull_request_number)
    return out


def _withdraw(connection: Any, decision: Mapping[str, Any], note: str,
              cause: dict[str, Any], actor: str) -> bool:
    from .run_decisions import _withdraw_with_note

    return _withdraw_with_note(
        connection, str(decision["id"]), str(decision["project_id"]), f"Withdrawn: {note}",
        actor=actor, why={"why": note, "cause_cleared": cause, "was": decision.get("status")})


def _open_statuses() -> list[str]:
    from .run_decisions import UNANSWERED

    return list(UNANSWERED)


def _effect_success_after(connection: Any, decision: Mapping[str, Any],
                          prefixes: tuple[str, ...]) -> Optional[dict[str, Any]]:
    """The first effect of these kinds that succeeded after the decision was raised, in its
    project (and its repository, when it names one)."""
    rec = _json(decision.get("recommendation"))
    repo = _repo_of(rec)
    clauses = " OR ".join(f"effect_kind LIKE :k{i}" for i in range(len(prefixes)))
    params: dict[str, Any] = {"p": decision["project_id"], "at": decision["created_at"],
                              "repo": repo}
    params.update({f"k{i}": prefix.replace("%", r"\%").replace("_", r"\_") + "%"
                   for i, prefix in enumerate(prefixes)})
    row = connection.execute(
        text("SELECT id, effect_kind, effect_scope->>'repository' AS repository, updated_at "
             "FROM receipts WHERE project_id = :p AND status = 'succeeded' "
             f"AND ({clauses}) AND updated_at > :at "
             "AND (CAST(:repo AS TEXT) IS NULL OR effect_scope->>'repository' = :repo) "
             "ORDER BY updated_at LIMIT 1"),
        params).mappings().first()
    return dict(row) if row else None


def _merged_on_record(connection: Any, project_id: str, *, url: Optional[str] = None,
                      decision_id: Optional[str] = None,
                      run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The build whose pull request (by URL, by its merge question, or by run) has a merge on
    record (pr_merge.record), or None."""
    if not (url or decision_id or run_id):
        return None
    row = connection.execute(
        text("SELECT id, pull_request_number, pull_request_url, "
             "finalize_ctx->'merge'->>'sha' AS sha FROM code_task_runs "
             "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
             "AND ((CAST(:u AS TEXT) IS NOT NULL AND pull_request_url = :u) "
             "  OR (CAST(:d AS TEXT) IS NOT NULL AND decision_id = :d) "
             "  OR (CAST(:r AS TEXT) IS NOT NULL AND id = :r)) LIMIT 1"),
        {"p": project_id, "u": url, "d": decision_id, "r": run_id}).mappings().first()
    return dict(row) if row else None


def _pr_label(rec: Mapping[str, Any], build: Optional[Mapping[str, Any]] = None) -> str:
    number = (build or {}).get("pull_request_number") or rec.get("pull_request_number")
    if not number:
        from .ci_gate import parse_pr_url

        parsed = parse_pr_url(str((build or {}).get("pull_request_url")
                                  or rec.get("pull_request_url") or ""))
        number = parsed[1] if parsed else None
    return f"PR #{number}" if number else "its pull request"


def _judge(connection: Any, decision: Mapping[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """(note, cause) when this open decision's cause has cleared, else None. Facts on record only."""
    rec = _json(decision.get("recommendation"))
    reason = str(rec.get("reason_code") or rec.get("reason") or "")
    action = str(rec.get("action") or "")
    project_id = str(decision["project_id"])
    prefixes = EFFECT_FAILURE_CAUSES.get(reason)
    if prefixes:
        done = _effect_success_after(connection, decision, prefixes)
        if done:
            return (f"its cause cleared — {done['effect_kind']} succeeded after it was raised"
                    + (f" on {done['repository']}" if done.get("repository") else "")
                    + ", so nothing is waiting on this answer.",
                    {"kind": "effect_succeeded", "reason_code": reason,
                     "receipt_id": done["id"], "effect_kind": done["effect_kind"]})
        url = str(rec.get("pull_request_url") or "") or None
        merged = _merged_on_record(connection, project_id, url=url)
        if merged:
            return (f"its cause cleared — {_pr_label(rec, merged)} was merged, so nothing is "
                    "waiting on this answer.",
                    {"kind": "pull_request_merged", "reason_code": reason,
                     "code_task_run_id": merged["id"], "sha": merged["sha"]})
    if action in DEPLOY_QUESTIONS:
        overtaken = _overtaken(connection, rec)
        if overtaken:
            short = str(overtaken.get("commit") or "")[:7]
            note = (f"{short} is already live on this target, so there is nothing left to ship."
                    if overtaken["same_commit"] else
                    f"a later deploy ({short}) went live on this target after it was asked; "
                    "approving this one would roll the app back.")
            return (f"superseded — {note}", overtaken)
    if action in STUCK_QUESTIONS:
        moving = _stuck_builds_moving(connection, rec, decision.get("created_at"))
        if moving:
            return moving
    if action in PULL_REQUEST_QUESTIONS:
        merged = _merged_on_record(
            connection, project_id, url=str(rec.get("pull_request_url") or "") or None,
            decision_id=str(decision["id"]), run_id=str(rec.get("run_id") or "") or None)
        if merged:
            return (f"{_pr_label(rec, merged)} was already merged, so there is nothing left "
                    "to approve.",
                    {"kind": "pull_request_merged", "action": action,
                     "code_task_run_id": merged["id"], "sha": merged["sha"]})
    return None


def _stuck_builds_moving(connection: Any, rec: Mapping[str, Any],
                         asked_at: Any) -> Optional[tuple[str, dict[str, Any]]]:
    """(note, cause) when every build a "stuck" card names has moved since it was asked."""
    from . import stuck_work

    ids = [str(i) for i in dict.fromkeys([rec.get("run_id"), *(rec.get("run_ids") or [])]) if i]
    if not ids:
        return None
    titles, states, phase = [], [], ""
    for run_id in ids:
        run = connection.execute(
            text("SELECT id, title, status, phase FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).mappings().first()
        if run is None:
            states.append("gone")
            continue
        titles.append(stuck_work._short_title(run["title"]))  # noqa: SLF001 — one short title
        if run["status"] == "cancelled":
            states.append("cancelled")
        elif run["status"] in stuck_work.ENDED_STATUSES and asked_at is not None and (
                (stuck_work.last_turn_at(connection, run_id) or asked_at) > asked_at):
            # It moved again after the ask, then ended: its own diagnosis (and card, when one
            # is needed) says why it ended; this question about being stuck is moot.
            states.append(f"ended ({run['status']}) after it moved again")
        elif stuck_work.moved_since(connection, run, asked_at):
            states.append("finished" if run["status"] == "completed" else "moving")
            phase = str(run["phase"] or run["status"]).replace("_", " ")
        else:
            return None
    work = titles[0] if len(titles) == 1 else f"{len(titles)} builds"
    said = states[0] if len(set(states)) == 1 else "moving or finished"
    return (f"{work} is moving again (now {phase}) — an agent is working it, so nothing waits "
            "on this answer." if said == "moving" and len(ids) == 1 else
            f"{work} {'are' if len(ids) > 1 else 'is'} {said}, so nothing waits on this answer.",
            {"kind": "stuck_work_moving", "run_ids": ids, "states": states})


def _overtaken(connection: Any, rec: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The healthy deploy that overtook this deploy request, or None (facts on record only)."""
    deployment_id = str(rec.get("deployment_id") or "")
    if not deployment_id:
        return None
    asked = connection.execute(text(
        "SELECT id, target_id, commit_sha, status, created_at, workflow_run_id, rollback_of "
        "FROM stack_deployments WHERE id = :i"), {"i": deployment_id}).mappings().first()
    if not asked or asked["status"] != "awaiting_approval":
        return None
    later = connection.execute(text(
        "SELECT id, commit_sha, finished_at FROM stack_deployments WHERE target_id = :t "
        "AND id <> :i AND status = 'healthy' AND created_at > :at "
        "ORDER BY created_at DESC LIMIT 1"),
        {"t": asked["target_id"], "i": asked["id"], "at": asked["created_at"]}).mappings().first()
    if later:
        return {"kind": "deploy_overtaken", "deployment_id": asked["id"], "by": later["id"],
                "commit": later["commit_sha"], "same_commit": later["commit_sha"] == asked["commit_sha"]}
    if not asked["workflow_run_id"] or asked["rollback_of"]:
        return None
    serving = connection.execute(text(
        "SELECT id, commit_sha FROM stack_deployments WHERE target_id = :t AND id <> :i "
        "AND status = 'healthy' ORDER BY created_at DESC LIMIT 1"),
        {"t": asked["target_id"], "i": asked["id"]}).mappings().first()
    if serving and serving["commit_sha"] == asked["commit_sha"]:
        return {"kind": "deploy_already_live", "deployment_id": asked["id"], "by": serving["id"],
                "commit": serving["commit_sha"], "same_commit": True}
    return None


def _close_overtaken_deploy(connection: Any, cause: Mapping[str, Any], note: str,
                            actor: str) -> None:
    """The withdrawn deploy request's own row: closed in words, its ChangeSet rejected and its
    budget hold released, and a workflow step parked on it moved onto the deploy that is live."""
    deployment_id = str(cause["deployment_id"])
    row = connection.execute(text(
        "UPDATE stack_deployments SET status = 'rejected', finished_at = now(), updated_at = now(), "
        "failure_reason = :r WHERE id = :i AND status = 'awaiting_approval' "
        "RETURNING project_id, changeset_id, reservation_id"),
        {"i": deployment_id, "r": f"Superseded: {note}"[:500]}).mappings().first()
    if not row:
        return
    connection.execute(text(
        "UPDATE changesets SET status = 'rejected', failure_reason = :r, updated_at = now() "
        "WHERE id = :c AND status IN ('pending_approval', 'policy_checked', 'budget_reserved')"),
        {"c": row["changeset_id"], "r": "superseded by a later deploy"})
    if row["reservation_id"]:
        try:
            from ..config import get_settings
            from .budget import BudgetGovernor

            with connection.begin_nested():
                BudgetGovernor(get_settings()).release(
                    connection, row["project_id"], row["reservation_id"],
                    "Stack deployment superseded by a later deploy.")
        except Exception:  # noqa: BLE001 — a held zero-dollar reservation expires on its own
            log.debug("superseded_deploy_release_failed", exc_info=True)
    note_for_step = (f"the deploy it waited for was superseded: {cause['by']} is live "
                     f"({str(cause.get('commit') or '')[:7]})")
    connection.execute(text(
        "UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) || "
        "jsonb_build_object('await', jsonb_build_object('kind', 'stack_deployment', 'id', "
        "CAST(:by AS TEXT)), 'awaited_note', CAST(:note AS TEXT), 'superseded_deployment', "
        "CAST(:was AS TEXT)), updated_at = now() WHERE status = 'waiting' "
        "AND output->'await'->>'kind' = 'stack_deployment' AND output->'await'->>'id' = :was"),
        {"by": cause["by"], "was": deployment_id, "note": note_for_step[:500]})
    from .core import emit_event

    emit_event(connection, row["project_id"], "deployment.stack_superseded", actor,
               "stack_deployment", deployment_id, {"by": cause["by"]})


def _candidates(connection: Any, project_id: Optional[str], *,
                ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    where = ["status IN :open",
             "(recommendation->>'reason_code' IN :reasons OR recommendation->>'action' IN :actions)"]
    params: dict[str, Any] = {"open": _open_statuses(),
                              "reasons": sorted(EFFECT_FAILURE_CAUSES),
                              "actions": sorted(PULL_REQUEST_QUESTIONS | DEPLOY_QUESTIONS
                                                | STUCK_QUESTIONS)}
    binds = [bindparam("open", expanding=True), bindparam("reasons", expanding=True),
             bindparam("actions", expanding=True)]
    if project_id:
        where.append("project_id = :p")
        params["p"] = project_id
    if ids is not None:
        if not ids:
            return []
        where.append("id IN :ids")
        params["ids"] = ids
        binds.append(bindparam("ids", expanding=True))
    rows = connection.execute(
        text("SELECT id, project_id, status, recommendation, created_at FROM decisions WHERE "
             + " AND ".join(where) + " ORDER BY created_at").bindparams(*binds),
        params).mappings().all()
    return [dict(r) for r in rows]


def withdraw_cleared(connection: Any, project_id: Optional[str] = None, *,
                     actor: str = "boss") -> list[str]:
    """THE SWEEP: withdraw every open decision whose cause has cleared. Idempotent and cheap
    (open decisions of the registered causes only); returns the ids it withdrew."""
    out: list[str] = []
    for decision in _candidates(connection, project_id):
        verdict = _judge(connection, decision)
        if verdict and _withdraw(connection, decision, verdict[0], verdict[1], actor):
            out.append(str(decision["id"]))
            if str(verdict[1].get("kind") or "").startswith("deploy_"):
                _close_overtaken_deploy(connection, verdict[1], verdict[0], actor)
    return out


def _guarded(fn, *args, **kwargs) -> list[str]:
    connection = args[0]
    try:
        with connection.begin_nested():
            return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — never fail the effect over its bookkeeping
        log.debug("cleared_causes_hook_failed", exc_info=True)
        return []


def _on_effect_succeeded(connection: Any, receipt_id: str, actor: str) -> list[str]:
    receipt = connection.execute(
        text("SELECT project_id, effect_kind FROM receipts WHERE id = :i AND status = 'succeeded'"),
        {"i": receipt_id}).mappings().first()
    if not receipt:
        return []
    kind = str(receipt["effect_kind"] or "")
    if not any(kind.startswith(p) for prefixes in EFFECT_FAILURE_CAUSES.values() for p in prefixes):
        return []
    return withdraw_cleared(connection, str(receipt["project_id"]), actor=actor)


def on_effect_succeeded(connection: Any, receipt_id: str, *, actor: str = "boss") -> list[str]:
    """An effect landed: withdraw the questions its failure (or a sibling's) raised."""
    if not hasattr(connection, "begin_nested"):
        return []
    return _guarded(_on_effect_succeeded, connection, receipt_id, actor)


def _on_pull_request_settled(connection: Any, run_id: str, state: str, actor: str) -> list[str]:
    from . import pr_merge

    build = connection.execute(
        text("SELECT project_id, pull_request_url, pull_request_number, decision_id "
             "FROM code_task_runs WHERE id = :i"), {"i": run_id}).mappings().first()
    if not build:
        return []
    project_id = str(build["project_id"])
    if state == pr_merge.MERGED:
        return withdraw_cleared(connection, project_id, actor=actor)
    if state != pr_merge.CLOSED:
        return []
    # CLOSED is observed, never recorded: the questions about THIS pull request go now.
    url = build["pull_request_url"]
    ids = [str(i) for i in connection.execute(
        text("SELECT decision_id FROM code_task_runs WHERE project_id = :p "
             "AND decision_id IS NOT NULL AND (id = :i OR (CAST(:u AS TEXT) IS NOT NULL "
             "AND pull_request_url = :u))"),
        {"p": project_id, "i": run_id, "u": url}).scalars().all()]
    out: list[str] = []
    label = f"PR #{build['pull_request_number']}" if build["pull_request_number"] else "Its pull request"
    for decision in _candidates(connection, project_id, ids=ids):
        rec = _json(decision.get("recommendation"))
        if str(rec.get("action") or "") not in PULL_REQUEST_QUESTIONS:
            continue
        if _withdraw(connection, decision,
                     f"{label} was closed without merging, so there is nothing left to approve.",
                     {"kind": "pull_request_closed", "code_task_run_id": run_id}, actor):
            out.append(str(decision["id"]))
    return out


def on_pull_request_settled(connection: Any, run_id: str, state: str, *,
                            actor: str = "boss") -> list[str]:
    """A build's pull request was merged (recorded) or closed (observed): withdraw the open
    questions it made moot."""
    if not run_id or not hasattr(connection, "begin_nested"):
        return []
    return _guarded(_on_pull_request_settled, connection, run_id, state, actor)


def on_deploy_healthy(connection: Any, project_id: str, *, actor: str = "boss") -> list[str]:
    """A stack deploy went live: withdraw the deploy requests it overtook."""
    if not project_id or not hasattr(connection, "begin_nested"):
        return []
    return _guarded(withdraw_cleared, connection, project_id, actor=actor)


# ── a merge question whose pull request or change set is already settled ────────────────────

#: How often one merge question's pull request is read again, and how many are read per sweep:
#: the sweep rides the monitor tick, and GitHub is never asked about the same PR every minute.
RECHECK_EVERY_SECONDS = 15 * 60
RECHECK_PER_SWEEP = 10
_last_read: dict[str, float] = {}
#: A change set in one of these is merged, shipped or closed: its approval asks nothing.
_CHANGE_SET_SETTLED = {"merged": "was merged", "deploying": "was merged", "live": "was merged",
                       "rolled_back": "was rolled back", "failed": "failed",
                       "rejected": "was rejected", "cancelled": "was cancelled"}


def _settled_change_sets(connection: Any, project_id: Optional[str], actor: str) -> list[str]:
    """Approvals of local or multi-repo change sets that are already merged or closed."""
    scope = "AND d.project_id = :p" if project_id else ""
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT d.id, d.project_id, d.status, cs.state, cs.title FROM decisions d "
                     "JOIN repo_change_sets cs ON cs.decision_id = d.id "
                     f"WHERE d.status = ANY(:open) AND cs.state = ANY(:settled) {scope}"),
                {"open": _open_statuses(), "settled": sorted(_CHANGE_SET_SETTLED),
                 **({"p": project_id} if project_id else {})}).mappings().all()
    except Exception:  # noqa: BLE001 — no change sets on this install: nothing to settle
        return []
    out = []
    for r in rows:
        what = f"The change set “{r['title']}”" if r["title"] else "Its change set"
        note = (f"{what} {_CHANGE_SET_SETTLED[str(r['state'])]}, so there is nothing left "
                "to approve.")
        if _withdraw(connection, dict(r), note, {"kind": "change_set_settled",
                                                  "state": r["state"]}, actor):
            out.append(str(r["id"]))
    return out


def _build_of(connection: Any, decision: Mapping[str, Any]) -> Optional[str]:
    """The build (code_task_runs) whose pull request this merge question is about."""
    rec = _json(decision.get("recommendation"))
    return connection.execute(
        text("SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_number IS NOT "
             "NULL AND (id = :r OR decision_id = :d OR (CAST(:u AS TEXT) <> '' AND "
             "pull_request_url = :u)) ORDER BY created_at DESC LIMIT 1"),
        {"p": decision["project_id"], "r": str(rec.get("run_id") or ""), "d": decision["id"],
         "u": str(rec.get("pull_request_url") or "")}).scalar()


def reconsider_merge_questions(connection: Any, settings: Any,
                               project_id: Optional[str] = None, *, actor: str = "boss",
                               now: Optional[float] = None) -> list[str]:
    """A MERGE QUESTION WHOSE PULL REQUEST IS ALREADY MERGED OR CLOSED WITHDRAWS ITSELF.

    Merge questions sat answerable for weeks about pull requests merged or closed by hand in
    GitHub (23 on the live install, 2026-11-08): nothing asked GitHub again after the build
    finished. On the monitor tick, every unanswered merge question (open or expired) has its
    pull request read through the project's own reader (pr_merge.observe, the same read the
    deploy step makes): merged is recorded and closed is observed, and either withdraws the
    question with a reason in words (on_pull_request_settled). A change set's approval is
    withdrawn when its row says merged or closed. Rate-limited (RECHECK_EVERY_SECONDS per
    question, RECHECK_PER_SWEEP per sweep); when GitHub cannot be read the question stands.
    Returns the ids withdrawn."""
    import time

    from . import pr_merge

    out = _settled_change_sets(connection, project_id, actor)
    clock = time.monotonic() if now is None else now
    read = 0
    for decision in _candidates(connection, project_id):
        if read >= RECHECK_PER_SWEEP:
            break
        rec = _json(decision.get("recommendation"))
        if str(rec.get("action") or "") not in PULL_REQUEST_QUESTIONS:
            continue
        did = str(decision["id"])
        last = _last_read.get(did)
        if last is not None and clock - last < RECHECK_EVERY_SECONDS:
            continue
        try:
            with connection.begin_nested():
                build = _build_of(connection, decision)
                if not build:
                    continue
                _last_read[did] = clock
                read += 1
                seen = pr_merge.observe(connection, settings, str(decision["project_id"]),
                                        str(build))
        except Exception:  # noqa: BLE001 — one unreadable question never stops the sweep
            log.exception("reconsider: %s", did)
            continue
        if seen.get("unread"):
            continue        # GitHub could not be asked: the question stands
        status = connection.execute(text("SELECT status FROM decisions WHERE id = :i"),
                                    {"i": did}).scalar()
        if status == "withdrawn":
            out.append(did)
    return out


__all__ = ["DEPLOY_QUESTIONS", "reconsider_merge_questions", "EFFECT_FAILURE_CAUSES", "PULL_REQUEST_QUESTIONS", "STUCK_QUESTIONS",
           "failed_effect_cause", "on_deploy_healthy", "on_effect_succeeded",
           "on_pull_request_settled", "withdraw_cleared"]
