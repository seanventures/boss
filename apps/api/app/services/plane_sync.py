"""Plane status sync-back (Phase 2 #115) + closed loop (Phase 4 #117).

As a BOSS code task moves through its lifecycle, mirror it onto the Plane ticket that triggered it:
PATCH the ticket's state and comment the PR link / gate Decision / failure, and close it on merge.
Only runs that a Plane webhook created (``finalize_ctx.source == "plane"``) are synced — everything
else is a no-op — and BOSS's own state PATCHes fire Plane ``issue.updated`` events, which the inbound
receiver ignores (it only acts on ``action == created``), so there's no trigger loop.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# code-task lifecycle event -> Plane state. None = comment only, don't move the ticket.
_EVENT_STATE: dict[str, Optional[str]] = {
    "code_task.started": "In Progress",
    "code_task.pr_opened": "In Progress",
    "code_task.gate_overridden": "In Progress",
    "code_task.completed_no_pr": "Done",
    "code_task.failed": None,                              # leave it In Progress; a human decides
    "software_factory.pull_request_merged": "Done",        # Phase 4: close on merge
}


def comment_for(event_type: str, run: dict[str, Any]) -> Optional[str]:
    """The human-readable Plane comment for a lifecycle event (or None to skip commenting)."""
    fc = run.get("finalize_ctx") or {}
    rid = run.get("id", "")
    pr = run.get("pull_request_url") or fc.get("pull_request_url")
    if event_type == "code_task.started":
        return f"🤖 <b>BOSS picked this up</b> and started a gated code task (<code>{rid}</code>)."
    if event_type == "code_task.pr_opened" and pr:
        return f"🔀 <b>BOSS opened a pull request:</b> <a href=\"{pr}\">{pr}</a>"
    if event_type == "code_task.gate_overridden":
        return ("⚠️ <b>Quality gate overridden</b> by an operator."
                + (f' PR: <a href="{pr}">{pr}</a>' if pr else ""))
    if event_type == "code_task.completed_no_pr":
        return "✅ <b>BOSS completed the task</b> (no PR opened)."
    if event_type == "code_task.failed":
        err = (run.get("error") or "").strip()
        return (f"❌ <b>BOSS run failed.</b> {err[:300]}"
                f"<br>Inspect run <code>{rid}</code> in the BOSS console.")
    if event_type == "software_factory.pull_request_merged":
        return "✅ <b>PR merged — closing.</b>" + (f' <a href="{pr}">{pr}</a>' if pr else "")
    return None


def sync_run(client, run: dict[str, Any], event_type: str) -> dict[str, Any]:
    """Mirror one lifecycle event onto the run's Plane ticket. Best-effort; returns a small report.
    Loop-guarded: only ``source == 'plane'`` runs sync."""
    fc = run.get("finalize_ctx") or {}
    issue_id = fc.get("plane_issue_id")
    project_id = fc.get("plane_project_id")
    if fc.get("source") != "plane" or not issue_id or not project_id:
        return {"synced": False, "reason": "not a plane-triggered run"}
    if not client.enabled():
        return {"synced": False, "reason": "plane client not configured"}

    state = _EVENT_STATE.get(event_type, "__unmapped__")
    if state == "__unmapped__":
        return {"synced": False, "reason": f"event {event_type} not mapped"}

    state_ok = client.set_state(project_id, issue_id, state) if state else None
    comment = comment_for(event_type, run)
    comment_ok = client.add_comment(project_id, issue_id, comment) if comment else None
    logger.info("plane sync %s -> issue %s: state=%s(%s) comment=%s",
                event_type, issue_id, state, state_ok, comment_ok)
    return {"synced": True, "issue_id": issue_id, "event": event_type,
            "state": state, "state_ok": state_ok, "commented": comment_ok}


def on_event(engine, settings, run_id: str, event_type: str) -> dict[str, Any]:
    """Wiring entry point: load the run and sync it. Guarded so a Plane/DB hiccup never breaks the
    caller (the pipeline). Fires at each code-task lifecycle transition."""
    try:
        if event_type not in _EVENT_STATE:
            return {"synced": False, "reason": "unmapped"}
        from .code_task_service import load_code_task_run
        from .db import begin
        from .plane_client import PlaneClient
        with begin(engine) as c:
            run = load_code_task_run(c, run_id)
        if not run:
            return {"synced": False, "reason": "run not found"}
        return sync_run(PlaneClient(settings), run, event_type)
    except Exception as exc:  # noqa: BLE001 — sync must never break the pipeline
        logger.warning("plane sync on_event failed for %s/%s: %s", run_id, event_type, exc)
        return {"synced": False, "reason": str(exc)}
