"""A PULL REQUEST IS MERGED ONLY WHEN IT WAS MERGED.

THE CASE (HelpDesk AI recording run, 2026-10-16). The Factory said "#229 merged" while GitHub
still had PR #229 open. The merge question had been APPROVED, and the approval recorded
`blocked_manual_merge_required` (decisions._create_merge_gate_block: BOSS does not merge in
this setup, a person merges in GitHub). Every reader took the decision's `approved` status for
"merged" — the Factory's build state, its PR chip, its app rows and the scorecards — and the
deploy step after it shipped the pull request's head with nothing saying it was unmerged.

THE RULE. Merged comes from a real merge and nothing else:
  * a merge sha BOSS recorded — its own merge job (software_factory.merge_pull_request writes a
    `merge_result` artifact and the run's `finalize_ctx.merge`), or
  * GitHub's own state, observed (`observe`) and recorded on the run the same way.
An approval with no merge behind it is `merge_pending` ("approved, not merged"); a manual merge
the gate asked for is said as such (`manual_merge_pending`).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

#: Where a run's merge is recorded: finalize_ctx.merge = {"sha", "at", "source"}.
RECORD_KEY = "merge"
MERGED = "merged"
CLOSED = "closed"
OPEN = "open"
#: The merge was approved, and nothing has merged it yet.
MERGE_PENDING = "merge_pending"
#: The decision's statuses that mean "a person said yes", which is not a merge.
_APPROVED = frozenset({"approved", "executed", "merged"})
_CLOSED = frozenset({"rejected", "cancelled"})
MANUAL_GATE_STATUS = "blocked_manual_merge_required"


def _json(value: Any) -> Any:
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value


def recorded_sha(run: Optional[Mapping[str, Any]],
                 decision: Optional[Mapping[str, Any]] = None) -> str:
    """The merge sha on record for this build, or "" — the run's own record first, then the
    one BOSS's merge job left on the decision (`merged_sha`, from `merged_shas`)."""
    fctx = _json((run or {}).get("finalize_ctx")) or {}
    merge = fctx.get(RECORD_KEY) if isinstance(fctx, Mapping) else None
    if isinstance(merge, Mapping) and str(merge.get("sha") or "").strip():
        return str(merge["sha"]).strip()
    return str((decision or {}).get("merged_sha") or "").strip()


def state(run: Optional[Mapping[str, Any]],
          decision: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """merged | closed | merge_pending | open — None for a build with no pull request."""
    if not run or not (run.get("pull_request_number") or run.get("pull_request_url")):
        return None
    if recorded_sha(run, decision):
        return MERGED
    status = str((decision or {}).get("status") or "")
    if status in _CLOSED:
        return CLOSED
    if status in _APPROVED:
        return MERGE_PENDING
    return OPEN


def merged_shas(connection: Any, project_id: str,
                decision_ids: Iterable[str]) -> dict[str, str]:
    """{decision id: merged sha} for the merges BOSS's merge job performed (its `merge_result`
    artifacts) — one query for any number of decisions."""
    ids = sorted({str(d) for d in decision_ids if d})
    if not ids:
        return {}
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT content->>'decision_id' AS d, content->>'merged_sha' AS sha "
                     "FROM artifacts WHERE project_id = :p AND artifact_type = 'merge_result' "
                     "AND content->>'decision_id' = ANY(:ids) "
                     "AND COALESCE(content->>'merged', 'true') <> 'false' "
                     "AND COALESCE(content->>'merged_sha', '') <> ''"),
                {"p": project_id, "ids": ids}).mappings().all()
    except Exception:  # noqa: BLE001 — unread, nothing is claimed merged
        return {}
    return {str(r["d"]): str(r["sha"]) for r in rows}


def manual_merge_pending(connection: Any, project_id: str, decision_id: Optional[str]) -> bool:
    """Whether the approval of this merge question asked a person to merge by hand."""
    if not decision_id:
        return False
    try:
        with connection.begin_nested():
            return bool(connection.execute(
                text("SELECT 1 FROM artifacts WHERE project_id = :p AND "
                     "artifact_type = 'merge_gate' AND content->>'approved_review_decision_id' "
                     "= :d AND content->>'status' = :s LIMIT 1"),
                {"p": project_id, "d": decision_id, "s": MANUAL_GATE_STATUS}).scalar())
    except Exception:  # noqa: BLE001
        return False


def record(connection: Any, run_id: str, sha: str, *, source: str) -> None:
    """Write a REAL merge onto the build: its sha, when, and who saw it (boss | github)."""
    if not run_id or not sha:
        return
    import json

    from .core import now_utc

    connection.execute(
        text("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
             "jsonb_build_object(CAST(:k AS TEXT), CAST(:m AS JSONB)), updated_at = now() "
             "WHERE id = :i"),
        {"i": run_id, "k": RECORD_KEY,
         "m": json.dumps({"sha": sha, "at": now_utc().isoformat(), "source": source})})
    # A merged pull request answers its own merge question, and any question its failed
    # effects raised (cleared_causes): withdrawn, with an event, never left in Needs you.
    from .cleared_causes import on_pull_request_settled

    on_pull_request_settled(connection, run_id, MERGED)
    # The dossier is re-read on every merge (screen 06): the repository's index is due now.
    try:
        with connection.begin_nested():
            from .knowledge.repo_index import mark_merged

            row = connection.execute(text(
                "SELECT project_id, finalize_ctx->>'repository' AS repo FROM code_task_runs "
                "WHERE id = :i"), {"i": run_id}).mappings().first()
            if row:
                mark_merged(connection, str(row["project_id"]), repo=row["repo"] or None)
    except Exception:  # noqa: BLE001 — a merge is recorded whatever the index does
        pass


def record_for_pull_request(connection: Any, project_id: str, pull_request_url: str, sha: str,
                            *, source: str) -> None:
    """Write a real merge onto EVERY build attempt that carries this pull request (a lineage's
    attempts share one PR, and the branch names only the root)."""
    if not pull_request_url or not sha:
        return
    ids = [str(r) for r in connection.execute(
        text("SELECT id FROM code_task_runs WHERE project_id = :p AND pull_request_url = :u"),
        {"p": project_id, "u": pull_request_url}).scalars().all()]
    for run_id in ids:
        record(connection, run_id, sha, source=source)


def observe(connection: Any, settings: Any, project_id: str, run_id: str, *,
            reader: Any = None) -> dict[str, Any]:
    """This build's pull request as GitHub has it now, recorded on the run when it merged.

    Returns {"state", "number", "sha"?, "manual"?, "unread"?}: `state` is merged, closed,
    merge_pending or open; `unread` says why GitHub could not be asked (the recorded state
    stands). Never raises."""
    try:
        with connection.begin_nested():
            run = connection.execute(
                text("SELECT id, pull_request_number, pull_request_url, finalize_ctx, "
                     "decision_id, spec->>'repo' AS repo FROM code_task_runs WHERE id = :i"),
                {"i": run_id}).mappings().first()
    except Exception as error:  # noqa: BLE001
        return {"state": None, "unread": str(error)[:200]}
    if not run or not run["pull_request_number"]:
        return {"state": None}
    run = dict(run)
    decision: dict[str, Any] = {}
    if run.get("decision_id"):
        try:
            with connection.begin_nested():
                status = connection.execute(
                    text("SELECT status FROM decisions WHERE id = :d"),
                    {"d": run["decision_id"]}).scalar()
            decision = {"status": status,
                        "merged_sha": merged_shas(connection, project_id,
                                                  [run["decision_id"]]).get(
                                                      str(run["decision_id"]), "")}
        except Exception:  # noqa: BLE001
            decision = {}
    number = int(run["pull_request_number"])
    out: dict[str, Any] = {"number": number}
    if not recorded_sha(run, decision):
        try:
            if reader is None:
                from . import ci_gate

                repo = (ci_gate.parse_pr_url(str(run.get("pull_request_url") or "")) or
                        (None, None))[0] or run.get("repo")
                reader = ci_gate.reader_for_project(connection, settings, project_id, repo)
            pr = reader.pull_request(number) or {}
            if pr.get("merged") or pr.get("merged_at"):
                sha = str(pr.get("merge_commit_sha") or (pr.get("head") or {}).get("sha")
                          or "merged")
                record(connection, run_id, sha, source="github")
                run["finalize_ctx"] = {**(_json(run.get("finalize_ctx")) or {}),
                                       RECORD_KEY: {"sha": sha}}
            elif str(pr.get("state") or "") == "closed":
                out["github"] = CLOSED
                from .cleared_causes import on_pull_request_settled

                on_pull_request_settled(connection, run_id, CLOSED)
            elif pr:
                out["github"] = OPEN
        except Exception as error:  # noqa: BLE001 — GitHub unreachable: the record stands
            out["unread"] = f"{type(error).__name__}: {error}"[:200]
    st = state(run, decision)
    if out.get("github") == CLOSED and st != MERGED:
        st = CLOSED
    out["state"] = st
    sha = recorded_sha(run, decision)
    if sha:
        out["sha"] = sha
    if st == MERGE_PENDING:
        out["manual"] = manual_merge_pending(connection, project_id, run.get("decision_id"))
    return out


def sentence(observed: Mapping[str, Any]) -> str:
    """What the deploy step says about the merge, plainly, when it is not merged; "" if it is."""
    n = observed.get("number")
    pr = f"PR #{n}" if n else "The pull request"
    st = observed.get("state")
    if st == MERGED or st is None:
        return ""
    if st == MERGE_PENDING and observed.get("manual"):
        why = ("its merge was approved, but BOSS does not merge here: a MANUAL MERGE IS "
               "PENDING in GitHub")
    elif st == MERGE_PENDING:
        why = "its merge was approved and has not happened yet"
    elif st == CLOSED:
        why = "it was closed without merging"
    else:
        why = "it is still open"
    unread = " (GitHub could not be read just now; this is BOSS's record)" \
        if observed.get("unread") else ""
    return (f"{pr} is NOT merged — {why}{unread}. This deploys the pull request's head, "
            "not the merged default branch.")


__all__ = ["CLOSED", "MERGED", "MERGE_PENDING", "OPEN", "RECORD_KEY", "manual_merge_pending",
           "merged_shas", "observe", "record", "record_for_pull_request", "recorded_sha", "sentence", "state"]
