"""Plane status sync-back (Phase 2 #115) + closed loop (Phase 4 #117).

As a BOSS code task moves through its lifecycle, mirror it onto the Plane ticket that triggered it:
PATCH the ticket's state and comment the PR link / gate Decision / failure, and close it on merge.
Only runs that a Plane webhook created (``finalize_ctx.source == "plane"``) are synced — everything
else is a no-op — and BOSS's own state PATCHes fire Plane ``issue.updated`` events, which the inbound
receiver ignores (it only acts on ``action == created``), so there's no trigger loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Optional

from sqlalchemy import text

from .core import create_blocking_decision, emit_event
from .receipts import ReceiptService

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


def _recorded_effect(
    connection: Any,
    run: dict[str, Any],
    event_type: str,
    operation: str,
    payload: dict[str, Any],
    send: Callable[[], bool],
    *,
    enabled: bool,
) -> tuple[bool, str]:
    """Record each Plane write once and escalate an uncertain or failed outcome."""
    project_id = str(run.get("project_id") or "")
    context = run["finalize_ctx"]
    plane_project_id = str(context["plane_project_id"])
    issue_id = str(context["plane_issue_id"])
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]
    dedupe_key = f"plane-sync:{run['id']}:{event_type}:{operation}:{digest}"
    receipts = ReceiptService()
    # The caller may have an open run-creation transaction. Commit the receipt on
    # a separate connection BEFORE sending to Plane, so a process crash after a
    # provider response cannot erase the only record of an uncertain effect.
    with connection.engine.begin() as receipt_connection:
        scope = receipt_connection.execute(
            text(
                "SELECT p.workspace_id, w.org_id FROM projects p "
                "JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=:project_id"
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not scope:
            raise ValueError("Plane sync run has no BOSS project scope.")
        receipt, created = receipts.create_or_reuse(
            receipt_connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            f"plane.issue.{operation}",
            {"plane_project_id": plane_project_id, "issue_id": issue_id},
            payload,
        )
    receipt_id = receipt["id"]
    if not created:
        # A failed or in-progress comment may have reached Plane before its response was
        # lost. Replaying it could duplicate a public comment; a decision resolves it.
        return receipt["status"] == "succeeded", receipt_id

    reason = "Plane client is not configured" if not enabled else "Plane API returned no success"
    ok = False
    if enabled:
        try:
            ok = bool(send())
        except Exception as error:  # noqa: BLE001 — the receipt still needs a terminal state
            reason = f"Plane adapter raised {type(error).__name__}"
    if ok:
        with connection.engine.begin() as outcome_connection:
            receipts.mark_succeeded(
                outcome_connection, receipt_id, issue_id,
                {"operation": operation, "event": event_type, "issue_id": issue_id},
            )
            try:
                with outcome_connection.begin_nested():
                    emit_event(
                        outcome_connection, project_id, "plane.sync_succeeded", "plane_sync",
                        "receipt", receipt_id, {"event": event_type, "operation": operation},
                    )
            except Exception:  # noqa: BLE001 — keep the committed effect outcome
                logger.exception("plane sync success event failed for receipt %s", receipt_id)
        return True, receipt_id

    with connection.engine.begin() as outcome_connection:
        receipts.mark_failed(outcome_connection, receipt_id, {"reason": reason})
        try:
            with outcome_connection.begin_nested():
                emit_event(
                    outcome_connection, project_id, "plane.sync_failed", "plane_sync", "receipt",
                    receipt_id, {"event": event_type, "operation": operation, "reason": reason},
                )
                create_blocking_decision(
                    outcome_connection, project_id, run.get("task_id"),
                    f"Reconcile Plane {operation} for code task {run['id']}",
                    {
                        "action": "review_plane_sync_failure", "receipt_id": receipt_id,
                        "run_id": run["id"], "event": event_type,
                        "plane_project_id": plane_project_id, "plane_issue_id": issue_id,
                        "operation": operation,
                    },
                    {}, "plane_sync_failed",
                )
        except Exception:  # noqa: BLE001 — preserve the failed receipt if escalation fails
            logger.exception("plane sync decision escalation failed for receipt %s", receipt_id)
    logger.warning("plane sync %s/%s failed; receipt %s", run["id"], operation, receipt_id)
    return False, receipt_id


def sync_run(
    client, run: dict[str, Any], event_type: str, *, connection: Any = None
) -> dict[str, Any]:
    """Mirror one lifecycle event onto the run's Plane ticket. Best-effort; returns a small report.
    Loop-guarded: only ``source == 'plane'`` runs sync."""
    fc = run.get("finalize_ctx") or {}
    issue_id = fc.get("plane_issue_id")
    project_id = fc.get("plane_project_id")
    if fc.get("source") != "plane" or not issue_id or not project_id:
        return {"synced": False, "reason": "not a plane-triggered run"}
    try:
        enabled = bool(client.enabled())
    except Exception:  # noqa: BLE001 — a failed credential lookup is still a failed effect
        logger.exception("plane sync could not check client availability")
        enabled = False
    if not enabled and connection is None:
        return {"synced": False, "reason": "plane client not configured"}

    state = _EVENT_STATE.get(event_type, "__unmapped__")
    if state == "__unmapped__":
        return {"synced": False, "reason": f"event {event_type} not mapped"}

    receipt_ids: dict[str, str] = {}
    if state and connection is not None:
        state_ok, receipt_ids["state"] = _recorded_effect(
            connection, run, event_type, "state.update",
            {"state": state},
            lambda: client.set_state(project_id, issue_id, state), enabled=enabled,
        )
    else:
        state_ok = client.set_state(project_id, issue_id, state) if state else None
    comment = comment_for(event_type, run)
    if comment and connection is not None:
        comment_ok, receipt_ids["comment"] = _recorded_effect(
            connection, run, event_type, "comment.create",
            {"comment_html": comment},
            lambda: client.add_comment(project_id, issue_id, comment), enabled=enabled,
        )
    else:
        comment_ok = client.add_comment(project_id, issue_id, comment) if comment else None
    logger.info("plane sync %s -> issue %s: state=%s(%s) comment=%s",
                event_type, issue_id, state, state_ok, comment_ok)
    return {"synced": enabled and state_ok is not False and comment_ok is not False,
            "issue_id": issue_id, "event": event_type, "state": state,
            "state_ok": state_ok, "commented": comment_ok,
            **({"receipt_ids": receipt_ids} if connection is not None else {})}


def on_event(engine, settings, run_id: str, event_type: str) -> dict[str, Any]:
    """Wiring entry point: load the run and sync it. Guarded so a Plane/DB hiccup never breaks the
    caller (the pipeline). Fires at each code-task lifecycle transition."""
    try:
        if event_type not in _EVENT_STATE:
            return {"synced": False, "reason": "unmapped"}
        from .code_task_service import load_code_task_run
        from ..db import begin
        from .plane_client import PlaneClient
        with begin(engine) as c:
            run = load_code_task_run(c, run_id)
            if not run:
                return {"synced": False, "reason": "run not found"}
            client = PlaneClient(settings)
            client._connection = c
            return sync_run(client, run, event_type, connection=c)
    except Exception as exc:  # noqa: BLE001 — sync must never break the pipeline
        logger.warning("plane sync on_event failed for %s/%s: %s", run_id, event_type, exc)
        return {"synced": False, "reason": str(exc)}
