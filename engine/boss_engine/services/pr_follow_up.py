"""ONE PULL REQUEST PER PIECE OF WORK; A FIX AFTER ITS MERGE IS A NAMED FOLLOW-UP.

THE CASE (project Hello World, paramg/boss-sandbox, 2026-11-08). The build opened PR #236 on
`boss/code/<root>`; the person approved it and BOSS merged it (squash). The deploy then failed
and the Pilot's fix and a More tests pass pushed to the same branch. With #236 merged there was
no open pull request for that head, so GitHub's "reuse the open PR" (create_pull_request) found
none and a SECOND pull request, #237 "Hello World REST API + sample · retry 1", was opened:
it re-proposed all of #236's already-merged files (squash left them off main's history), asked
the person to merge the same feature again, and its merge then failed "405" as conflicting.

THE RULE.
  * While a pull request for the work is open, every retry and fix pushes to its branch and
    updates THAT pull request (create_pull_request reuses it): never a second one.
  * After it merged, a fix is a FOLLOW-UP: the branch is put back on its base holding only what
    changed after the merge (GitHubAdapter.restack_after_merge), and the pull request and its
    merge question are named "Fix: <work> after #236" with a one-line reason.
  * A merge question for a pull request that merely repeats a merged one withdraws itself; one
    that carries a real change is relabelled as the follow-up it is (reconsider, on the tick).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

#: What the decision's recommendation carries for a follow-up (the merged PR's number).
FOLLOW_UP_KEY = "follow_up_of"
_FOLLOWUP_LEAD = re.compile(r"^\s*Follow-up:\s*([^—–]+?)\s*[—–]\s*", re.I)
_FIX_TITLE = re.compile(r"^\s*Fix:\s*(.+?)\s+after\s+#(\d+)\s*$", re.I)


def work_name(title: Any) -> str:
    """The work's own name: "Follow-up: … —" leads, "· retry N" tails and an earlier
    "Fix: … after #N" wrapper off."""
    from .project_experience.work import clean_title

    t = str(title or "").strip()
    for _ in range(5):
        before = t
        t = clean_title(t)
        m = _FIX_TITLE.match(t)
        if m:
            t = m.group(1)
        if t == before:
            break
    return t


def follow_up_title(title: Any, merged_number: int) -> str:
    """"Fix: Hello World REST API + sample after #236"."""
    return f"Fix: {work_name(title)} after #{int(merged_number)}"[:200]


def follow_up_reason(title: Any, merged_number: int, paths: Iterable[str] = ()) -> str:
    """One line: why this pull request exists after #N merged."""
    m = _FOLLOWUP_LEAD.match(str(title or ""))
    what = m.group(1).strip() if m else ""
    paths = [p for p in paths if p]
    files = (f" ({len(paths)} file{'s' if len(paths) != 1 else ''}: "
             f"{', '.join(paths[:3])}{', …' if len(paths) > 3 else ''})") if paths else ""
    if what:
        return f"Follow-up fix after #{merged_number} merged: {what}{files}."
    return f"Follow-up fix after #{merged_number} merged: changes made since{files}."


def prepare_branch(github: Any, branch_name: str, *, base: Optional[str] = None,
                   paths: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    """Before a pull request is opened from `branch_name`: None when it continues an OPEN pull
    request (or the host cannot tell), else, when an earlier pull request from this branch
    merged, {"after": its number, "restacked", "paths", ...} — the branch put back on its base.
    Never raises: a host that cannot answer leaves the pull request as it would have been."""
    opened = getattr(github, "open_pull_request_for", None)
    merged_of = getattr(github, "last_merged_pull_request", None)
    if not callable(opened) or not callable(merged_of) or not branch_name:
        return None
    try:
        if opened(branch_name):
            return None
        merged = merged_of(branch_name)
    except Exception:  # noqa: BLE001 — unread: the pull request opens as before
        log.debug("pr_follow_up_unread", exc_info=True)
        return None
    if not merged:
        return None
    out: dict[str, Any] = {"after": int(merged["number"]), "merged_url": merged.get("url"),
                           "merged_head_sha": merged.get("head_sha"), "restacked": False}
    restack = getattr(github, "restack_after_merge", None)
    if callable(restack):
        try:
            out.update(restack(branch_name, str(merged.get("head_sha") or ""), base=base,
                               number=int(merged["number"])))
        except Exception as error:  # noqa: BLE001 — the follow-up still opens, named
            out["why"] = f"{type(error).__name__}: {error}"[:200]
    out["paths"] = sorted(set(out.get("paths") or []) | {p for p in paths if p})
    return out


def merged_before(connection: Any, decision: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The earlier merge question of the SAME work branch whose pull request is merged on
    record (a different pull request than this one's), or None: {decision_id, number, url,
    head_sha}."""
    from sqlalchemy import text

    from .pr_merge import _json

    rec = _json(decision.get("recommendation")) or {}
    branch = str(rec.get("branch_name") or "")
    url = str(rec.get("pull_request_url") or "")
    if not branch or not url:
        return None
    row = connection.execute(text(
        "SELECT d.id, d.recommendation->>'pull_request_url' AS url, "
        "d.recommendation->>'head_sha' AS head_sha, c.pull_request_number AS number "
        "FROM decisions d JOIN code_task_runs c ON c.decision_id = d.id "
        "WHERE d.project_id = :p AND d.id <> :d "
        "AND d.recommendation->>'action' = 'review_pull_request' "
        "AND d.recommendation->>'branch_name' = :b "
        "AND COALESCE(d.recommendation->>'pull_request_url', '') NOT IN ('', :u) "
        "AND (COALESCE(c.finalize_ctx->'merge'->>'sha', '') <> '' OR EXISTS ("
        "  SELECT 1 FROM artifacts a WHERE a.project_id = d.project_id "
        "  AND a.artifact_type = 'merge_result' AND a.content->>'decision_id' = d.id "
        "  AND COALESCE(a.content->>'merged', 'true') <> 'false')) "
        "AND d.created_at < :at ORDER BY d.created_at DESC LIMIT 1"),
        {"p": decision["project_id"], "d": decision["id"], "b": branch, "u": url,
         "at": decision["created_at"]}).mappings().first()
    if not row:
        return None
    from .ci_gate import parse_pr_url

    number = row["number"] or ((parse_pr_url(str(row["url"] or "")) or (None, None))[1])
    return {"decision_id": str(row["id"]), "number": int(number) if number else None,
            "url": row["url"], "head_sha": row["head_sha"]}


def relabel(connection: Any, decision: Mapping[str, Any], merged_number: int,
            reason: str, *, actor: str = "boss") -> bool:
    """Name an existing merge question as the follow-up it is: its title and question say
    "Fix: <work> after #N", its recommendation carries the reason. Idempotent."""
    import json

    from sqlalchemy import text

    from .core import emit_event
    from .pr_merge import _json

    rec = _json(decision.get("recommendation")) or {}
    if rec.get(FOLLOW_UP_KEY):
        return False
    title = follow_up_title(rec.get("title") or "", merged_number)
    number = rec.get("pull_request_number")
    if not number:
        from .ci_gate import parse_pr_url

        number = (parse_pr_url(str(rec.get("pull_request_url") or "")) or (None, None))[1]
    question = (f"Approve follow-up fix after #{merged_number}: {work_name(title)}"
                + (f" (PR #{number})" if number else ""))
    patch = {"title": title, FOLLOW_UP_KEY: int(merged_number), "follow_up_reason": reason,
             "summary": f"{reason} {str(rec.get('summary') or '')}".strip()}
    connection.execute(text(
        "UPDATE decisions SET question = :q, recommendation = COALESCE(recommendation, "
        "'{}'::jsonb) || CAST(:patch AS JSONB), updated_at = now() WHERE id = :d"),
        {"q": question, "patch": json.dumps(patch), "d": decision["id"]})
    emit_event(connection, str(decision["project_id"]), "decision.relabelled_follow_up", actor,
               "decision", str(decision["id"]),
               {FOLLOW_UP_KEY: int(merged_number), "reason": reason, "title": title})
    return True


__all__ = ["FOLLOW_UP_KEY", "follow_up_reason", "follow_up_title", "merged_before",
           "prepare_branch", "relabel", "work_name"]
