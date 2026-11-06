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

#: decision actions that ask about one pull request, and are moot once it is merged or closed.
PULL_REQUEST_QUESTIONS: frozenset[str] = frozenset({"review_pull_request"})

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


def _candidates(connection: Any, project_id: Optional[str], *,
                ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    where = ["status IN :open",
             "(recommendation->>'reason_code' IN :reasons OR recommendation->>'action' IN :actions)"]
    params: dict[str, Any] = {"open": _open_statuses(),
                              "reasons": sorted(EFFECT_FAILURE_CAUSES),
                              "actions": sorted(PULL_REQUEST_QUESTIONS)}
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


__all__ = ["EFFECT_FAILURE_CAUSES", "PULL_REQUEST_QUESTIONS", "failed_effect_cause",
           "on_effect_succeeded", "on_pull_request_settled", "withdraw_cleared"]
