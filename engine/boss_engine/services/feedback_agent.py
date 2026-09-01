"""Durable, confirmation-gated conversational feedback for code-task lineages."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run, retry_code_task
from .core import emit_event, make_id


_FULL_RERUN_MARKERS = {
    "architecture", "refactor", "redesign", "schema", "migration", "dependency",
    "authentication", "authorization", "across the app", "all pages", "rewrite",
}


def _root_run(run: dict) -> str:
    fctx = run.get("finalize_ctx") or {}
    return str(fctx.get("root_run_id") or run["id"])


def _ensure_thread(connection, run: dict) -> dict:
    root_run_id = _root_run(run)
    row = connection.execute(text(
        "SELECT id,project_id,root_run_id,status,pending_proposal,created_at,updated_at "
        "FROM feedback_threads WHERE root_run_id=:root"
    ), {"root": root_run_id}).mappings().first()
    if row:
        return dict(row)
    thread_id = make_id("fbt")
    connection.execute(text(
        "INSERT INTO feedback_threads (id,project_id,root_run_id,status) "
        "VALUES (:id,:p,:root,'open')"
    ), {"id": thread_id, "p": run["project_id"], "root": root_run_id})
    return {"id": thread_id, "project_id": run["project_id"],
            "root_run_id": root_run_id, "status": "open", "pending_proposal": {}}


def _add_message(
    connection, thread_id: str, *, role: str, kind: str, content: str,
    metadata: Optional[dict] = None, resulting_run_id: Optional[str] = None,
) -> str:
    message_id = make_id("fbm")
    connection.execute(text(
        "INSERT INTO feedback_messages "
        "(id,thread_id,role,kind,content,metadata,resulting_run_id) "
        "VALUES (:id,:thread,:role,:kind,:content,CAST(:meta AS JSONB),:run)"
    ), {"id": message_id, "thread": thread_id, "role": role, "kind": kind,
        "content": content, "meta": json.dumps(metadata or {}), "run": resulting_run_id})
    return message_id


def _is_vague(message: str, *, anchor: str = "", expected: str = "") -> bool:
    words = [word for word in message.strip().split() if word]
    concrete = bool(anchor.strip() or expected.strip())
    intent_words = ("should", "expected", "instead", "when", "after", "before", "change", "add")
    return len(words) < 6 and not concrete and not any(word in message.lower() for word in intent_words)


def _structured_fix_note(
    message: str, *, expected: str, anchor: str, severity: str,
    selected_element: str, classification: str,
) -> str:
    expected_text = expected.strip() or "Resolve the reported behavior while preserving existing behavior."
    scope = "localized change on the existing branch" if classification == "scoped_tweak" else (
        "full governed pipeline rerun across the affected surface"
    )
    fields = [
        ("Observed behavior", message.strip()),
        ("Expected behavior", expected_text),
        ("Anchor URL / route", anchor.strip() or "not supplied"),
        ("Selected element", selected_element.strip() or "not supplied"),
        ("Severity", severity.strip().lower() or "normal"),
        ("Suggested scope", scope),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields)


def submit_feedback(
    engine, run_id: str, *, message: str, expected: str = "", anchor: str = "",
    severity: str = "normal", selected_element: str = "", screenshot_url: str = "",
    workflow_version_id: str = "",
) -> dict:
    message = message.strip()
    if not message:
        raise ValueError("feedback message is required")
    with begin(engine) as connection:
        run = load_code_task_run(connection, run_id)
        if run is None:
            raise ValueError(f"code task run not found: {run_id}")
        thread = _ensure_thread(connection, run)
        metadata = {"anchor": anchor, "expected": expected, "severity": severity,
                    "selected_element": selected_element, "screenshot_url": screenshot_url}
        _add_message(connection, thread["id"], role="operator", kind="feedback",
                     content=message, metadata=metadata)
        if _is_vague(message, anchor=anchor, expected=expected):
            question = (
                "Which element or route is affected, what happened, and what did you expect instead?"
            )
            _add_message(connection, thread["id"], role="agent", kind="clarification",
                         content=question)
            connection.execute(text(
                "UPDATE feedback_threads SET status='awaiting_clarification',updated_at=now() "
                "WHERE id=:id"
            ), {"id": thread["id"]})
            emit_event(connection, run["project_id"], "feedback.clarification_requested",
                       "feedback_agent", "feedback_thread", thread["id"],
                       {"run_id": run_id, "anchor": anchor})
            return {"thread_id": thread["id"], "status": "awaiting_clarification",
                    "question": question, "dispatched": False}

        lowered = message.lower()
        classification = (
            "full_rerun" if any(marker in lowered for marker in _FULL_RERUN_MARKERS)
            else "scoped_tweak"
        )
        fix_note = _structured_fix_note(
            message, expected=expected, anchor=anchor, severity=severity,
            selected_element=selected_element, classification=classification,
        )
        proposal = {
            "source_run_id": run_id, "classification": classification,
            "fix_note": fix_note,
            "profile": "strict" if classification == "full_rerun" else "balanced",
            "repair_retries": 2 if classification == "full_rerun" else 1,
            # THE PATH THE OPERATOR CHOSE FOR THIS CHANGE. Carried on the proposal because the
            # choice is made when the change is described and acted on when it is confirmed —
            # two requests, and nothing else survives between them.
            **({"workflow_version_id": workflow_version_id.strip()}
               if workflow_version_id.strip() else {}),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _add_message(connection, thread["id"], role="agent", kind="proposal",
                     content=fix_note, metadata=proposal)
        connection.execute(text(
            "UPDATE feedback_threads SET status='awaiting_confirmation',"
            "pending_proposal=CAST(:proposal AS JSONB),updated_at=now() WHERE id=:id"
        ), {"proposal": json.dumps(proposal), "id": thread["id"]})
        emit_event(connection, run["project_id"], "feedback.proposed", "feedback_agent",
                   "feedback_thread", thread["id"],
                   {"run_id": run_id, "classification": classification, "anchor": anchor})
    return {"thread_id": thread["id"], "status": "awaiting_confirmation",
            "requires_confirmation": True, "proposal": proposal, "dispatched": False}


def confirm_feedback(
    engine, settings: Settings, jobs, thread_id: str, *, actor: str = "operator",
) -> dict:
    with begin(engine) as connection:
        row = connection.execute(text(
            "SELECT id,project_id,status,pending_proposal FROM feedback_threads "
            "WHERE id=:id FOR UPDATE"
        ), {"id": thread_id}).mappings().first()
        if row is None:
            raise ValueError("feedback thread not found")
        proposal = row["pending_proposal"] or {}
        if isinstance(proposal, str):
            proposal = json.loads(proposal)
        if row["status"] != "awaiting_confirmation" or not proposal:
            raise ValueError("feedback thread has no proposal awaiting confirmation")
        connection.execute(text(
            "UPDATE feedback_threads SET status='dispatching',updated_at=now() WHERE id=:id"
        ), {"id": thread_id})
    try:
        result = retry_code_task(
            engine, settings, jobs, proposal["source_run_id"],
            fix_note=proposal["fix_note"], profile=proposal["profile"],
            repair_retries=int(proposal["repair_retries"]),
            workflow_version_id=str(proposal.get("workflow_version_id") or ""),
        )
    except Exception:
        with begin(engine) as connection:
            connection.execute(text(
                "UPDATE feedback_threads SET status='awaiting_confirmation',updated_at=now() "
                "WHERE id=:id AND status='dispatching'"
            ), {"id": thread_id})
        raise
    with begin(engine) as connection:
        connection.execute(text(
            "UPDATE feedback_messages SET resulting_run_id=:run "
            "WHERE thread_id=:thread AND resulting_run_id IS NULL"
        ), {"run": result["run_id"], "thread": thread_id})
        _add_message(
            connection, thread_id, role="agent", kind="dispatched",
            content=f"Confirmed by {actor}; dispatched attempt {result['attempt']}.",
            metadata={"actor": actor, "proposal": proposal},
            resulting_run_id=result["run_id"],
        )
        connection.execute(text(
            "UPDATE feedback_threads SET status='dispatched',pending_proposal='{}'::jsonb,"
            "updated_at=now() WHERE id=:id"
        ), {"id": thread_id})
        emit_event(connection, row["project_id"], "feedback.retry_dispatched", actor,
                   "feedback_thread", thread_id,
                   {"source_run_id": proposal["source_run_id"],
                    "resulting_run_id": result["run_id"],
                    "classification": proposal["classification"]})
    return {"thread_id": thread_id, "status": "dispatched", "dispatched": True, **result}


def get_feedback_thread(engine, run_id: str) -> dict:
    with begin(engine) as connection:
        run = load_code_task_run(connection, run_id)
        if run is None:
            raise ValueError(f"code task run not found: {run_id}")
        root = _root_run(run)
        thread = connection.execute(text(
            "SELECT id,project_id,root_run_id,status,pending_proposal,created_at,updated_at "
            "FROM feedback_threads WHERE root_run_id=:root"
        ), {"root": root}).mappings().first()
        if thread is None:
            return {"root_run_id": root, "status": "not_started", "messages": []}
        messages = connection.execute(text(
            "SELECT id,role,kind,content,metadata,resulting_run_id,created_at "
            "FROM feedback_messages WHERE thread_id=:id ORDER BY created_at,id"
        ), {"id": thread["id"]}).mappings().all()
    return {**dict(thread), "messages": [dict(message) for message in messages]}


def submit_preview_feedback(engine, preview_id: str, payload: dict) -> dict:
    with begin(engine) as connection:
        row = connection.execute(text(
            "SELECT run_id,status FROM app_previews WHERE id=:id"
        ), {"id": preview_id}).mappings().first()
    if row is None or not row["run_id"]:
        raise ValueError("preview not found")
    if row["status"] != "running":
        raise ValueError("preview is not running")
    return submit_feedback(
        engine, row["run_id"], message=str(payload.get("message") or ""),
        expected=str(payload.get("expected") or ""), anchor=str(payload.get("anchor") or ""),
        severity=str(payload.get("severity") or "normal"),
        selected_element=str(payload.get("selected_element") or ""),
        screenshot_url=str(payload.get("screenshot_url") or ""),
    )
