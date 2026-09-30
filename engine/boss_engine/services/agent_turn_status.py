"""Whether a console chat turn is actually still running.

GET /v1/agent/threads/{id}/messages exposes this as `working` so the dock can
stop promising an answer that will never arrive — without declaring a live
turn dead.

Console chat does not write `agent_runs` (that table is the old agent loop /
Metatron path). Mid-turn visibility is `agent_progress`, committed on its own
connection so a poller can see it before the turn's transaction ends. An
investigation is the same work after the HTTP request has returned.
"""

from __future__ import annotations

from sqlalchemy import text as sql


def thread_is_working(connection, project_id: str, thread_id: str) -> bool:
    """True when THIS thread has an in-flight chat turn or investigation."""
    if not (thread_id or "").strip():
        return False
    params = {"t": thread_id.strip(), "p": project_id}
    # Progress after the last committed assistant row is this turn: earlier
    # rows belong to a turn that already answered. The 15-minute bound is so a
    # crashed turn does not stay "working" forever.
    if connection.execute(
        sql(
            """
            SELECT 1 FROM agent_progress p
            WHERE p.thread_id = :t
              AND (p.project_id IS NULL OR p.project_id = :p)
              AND p.created_at > now() - interval '15 minutes'
              AND p.created_at > COALESCE(
                    (SELECT MAX(created_at) FROM agent_messages
                     WHERE thread_id = :t AND role = 'assistant'),
                    TIMESTAMPTZ '-infinity')
            LIMIT 1
            """
        ),
        params,
    ).first():
        return True
    return bool(connection.execute(
        sql(
            """
            SELECT 1 FROM investigations
            WHERE thread_id = :t AND project_id = :p
              AND status IN ('queued', 'running')
            LIMIT 1
            """
        ),
        params,
    ).first())
