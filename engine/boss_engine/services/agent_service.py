from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .capability_manifest import build_manifest
from .core import emit_event, make_id
from .intent_router import IntentRouter


class AgentThreadNotFoundError(ValueError):
    pass


class AgentThreadOwnershipError(ValueError):
    pass


class AgentService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.router = IntentRouter(settings)

    def start_or_load_thread(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        thread_id: Optional[str] = None,
        surface: str = "chat",
        external_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        if thread_id:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, actor, surface, external_ref, status,
                           pending_plan_id, pending_plan_summary, created_at, updated_at
                    FROM agent_threads WHERE id = :id
                    """
                ),
                {"id": thread_id},
            ).mappings().first()
            if row is None:
                raise AgentThreadNotFoundError("Agent thread not found.")
            # A task thread (external_ref set) is SHARED — any authorized project actor may use it
            # (the endpoint already checked agent_chat). Only a private general thread is actor-owned.
            if row["project_id"] != project_id or (row["actor"] != actor and not row["external_ref"]):
                raise AgentThreadOwnershipError(
                    "Agent thread belongs to a different project or actor."
                )
            if row["status"] != "active":
                raise AgentThreadOwnershipError("Agent thread is closed.")
            return dict(row)

        if external_ref:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, actor, surface, external_ref, status,
                           pending_plan_id, pending_plan_summary, created_at, updated_at
                    FROM agent_threads
                    WHERE project_id = :project_id AND surface = :surface
                      AND external_ref = :external_ref
                    """
                ),
                {
                    "project_id": project_id,
                    "surface": surface,
                    "external_ref": external_ref,
                },
            ).mappings().first()
            if row:
                # Shared per-task thread (unique per project+surface+external_ref). Any authorized
                # project actor may post — do NOT reject on actor mismatch (that broke task chat when
                # the thread was first created by a different console actor).
                return dict(row)

        new_id = make_id("athr")
        row = connection.execute(
            text(
                """
                INSERT INTO agent_threads (
                    id, project_id, actor, surface, external_ref
                )
                VALUES (:id, :project_id, :actor, :surface, :external_ref)
                RETURNING id, project_id, actor, surface, external_ref, status,
                          pending_plan_id, pending_plan_summary, created_at, updated_at
                """
            ),
            {
                "id": new_id,
                "project_id": project_id,
                "actor": actor,
                "surface": surface,
                "external_ref": external_ref,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "agent.thread_started",
            actor,
            "agent_thread",
            new_id,
            {"surface": surface, "external_ref": external_ref},
        )
        return dict(row)

    def append_message(
        self,
        connection: Connection,
        *,
        thread_id: str,
        role: str,
        content: str,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        actions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                INSERT INTO agent_messages (
                    id, thread_id, role, content, tool_calls, actions
                )
                VALUES (
                    :id, :thread_id, :role, :content,
                    CAST(:tool_calls AS JSONB), CAST(:actions AS JSONB)
                )
                RETURNING id, role, content, tool_calls, actions, created_at
                """
            ),
            {
                "id": make_id("amsg"),
                "thread_id": thread_id,
                "role": role,
                "content": content,
                "tool_calls": json.dumps(tool_calls or []),
                "actions": json.dumps(actions or []),
            },
        ).mappings().one()
        connection.execute(
            text("UPDATE agent_threads SET updated_at = now() WHERE id = :id"),
            {"id": thread_id},
        )
        return self._serialize_message(row)

    def list_messages(
        self, connection: Connection, thread_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, role, content, tool_calls, actions, created_at
                FROM agent_messages
                WHERE thread_id = :thread_id
                -- A TURN'S ROWS SHARE ONE TIMESTAMP, SO THE TIEBREAK DECIDES THE TRANSCRIPT.
                --
                -- The question and its answer are written in the same transaction, and
                -- Postgres now() is TRANSACTION START time — so created_at is identical for
                -- both, every time. The tiebreak was `id`, a random hex, which meant the order
                -- of a chat was decided by chance: in a real thread, four of five answers
                -- sorted ABOVE the question they answered, so the operator saw their follow-up
                -- appear before the previous reply.
                --
                -- Ordering by role within a timestamp is deterministic and matches how a turn
                -- actually happens: the question, then any tool work, then the reply. This
                -- also repairs threads already stored.
                ORDER BY created_at,
                         CASE role WHEN 'user' THEN 0
                                   WHEN 'tool' THEN 1
                                   WHEN 'assistant' THEN 2
                                   ELSE 3 END,
                         id
                """
            ),
            {"thread_id": thread_id},
        ).mappings().all()
        return [self._serialize_message(row) for row in rows]

    def list_threads(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        since_days: int = 7,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Recent chat threads for a project+actor (default last 7 days), newest
        first, each with a title (first user message) and message count — for the
        console's conversation history + resume."""
        rows = connection.execute(
            text(
                """
                SELECT t.id, t.status, t.external_ref, t.created_at, t.updated_at,
                       (SELECT m.content FROM agent_messages m
                          WHERE m.thread_id = t.id AND m.role = 'user'
                          ORDER BY m.created_at, m.id LIMIT 1) AS first_user,
                       (SELECT count(*) FROM agent_messages m
                          WHERE m.thread_id = t.id) AS message_count
                FROM agent_threads t
                WHERE t.project_id = :p AND t.surface = 'chat'
                  -- Task threads (external_ref set) are SHARED per task -> visible to every project
                  -- actor; general threads (no external_ref) stay private to their actor.
                  AND (t.actor = :a OR t.external_ref IS NOT NULL)
                  AND t.updated_at >= now() - make_interval(days => :days)
                ORDER BY t.updated_at DESC
                LIMIT :limit
                """
            ),
            {"p": project_id, "a": actor, "days": int(since_days), "limit": int(limit)},
        ).mappings().all()
        threads: list[dict[str, Any]] = []
        for r in rows:
            first = (r["first_user"] or "").strip()
            title = first.splitlines()[0][:80] if first else "New conversation"
            threads.append(
                {
                    "thread_id": r["id"],
                    "external_ref": r["external_ref"],
                    "title": title or "New conversation",
                    "message_count": int(r["message_count"] or 0),
                    "status": r["status"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
            )
        return threads

    def record_turn(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        thread_id: str,
        action_count: int,
        pending_plan_id: Optional[str],
    ) -> None:
        emit_event(
            connection,
            project_id,
            "agent.turn_completed",
            actor,
            "agent_thread",
            thread_id,
            {
                "action_count": action_count,
                "pending_plan_id": pending_plan_id,
            },
        )

    def chat(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        message: str,
        thread_id: Optional[str] = None,
        confirm_plan_id: Optional[str] = None,
        surface: str = "chat",
        external_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        thread = self.start_or_load_thread(
            connection,
            project_id=project_id,
            actor=actor,
            thread_id=thread_id,
            surface=surface,
            external_ref=external_ref,
        )
        user_content = message.strip() or f"Confirm plan {confirm_plan_id}"
        self.append_message(
            connection,
            thread_id=thread["id"],
            role="user",
            content=user_content,
        )
        messages = self.list_messages(connection, thread["id"])
        # Build exactly once for the turn. The router receives the resolved
        # object and never reaches back into the feature catalog per tool call.
        manifest = build_manifest(connection, self.settings, project_id)
        turn = self.router.route_turn(
            connection,
            project_id=project_id,
            actor=actor,
            messages=messages,
            manifest=manifest,
            confirm_plan_id=confirm_plan_id,
            pending_plan_id=thread.get("pending_plan_id"),
            # A task-scoped thread carries its task_id -> the router builds ON that task's existing
            # app branch (extend the PR) instead of a fresh build on main.
            external_ref=thread.get("external_ref"),
        )
        assistant = self.append_message(
            connection,
            thread_id=thread["id"],
            role="assistant",
            content=turn.reply,
            tool_calls=turn.tool_calls,
            actions=turn.actions,
        )

        pending = turn.pending_confirmation
        # A dispatched OR explicitly dismissed plan clears the thread's pending state (a dismissed
        # plan must not keep jamming every future message with "confirm the pending plan").
        dispatched = any(action.get("kind") in ("plan_dispatched", "plan_dismissed")
                         for action in turn.actions)
        if pending:
            connection.execute(
                text(
                    """
                    UPDATE agent_threads
                    SET pending_plan_id = :plan_id,
                        pending_plan_summary = CAST(:summary AS JSONB),
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": thread["id"],
                    "plan_id": pending["plan_id"],
                    "summary": json.dumps(pending),
                },
            )
        elif dispatched:
            connection.execute(
                text(
                    """
                    UPDATE agent_threads
                    SET pending_plan_id = NULL, pending_plan_summary = NULL,
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": thread["id"]},
            )

        active_pending_id = pending["plan_id"] if pending else (
            None if dispatched else thread.get("pending_plan_id")
        )
        self.record_turn(
            connection,
            project_id=project_id,
            actor=actor,
            thread_id=thread["id"],
            action_count=len(turn.actions),
            pending_plan_id=active_pending_id,
        )
        return {
            "thread_id": thread["id"],
            "reply": turn.reply,
            "actions": turn.actions,
            "pending_confirmation": pending,
            # Offers the operator can accept with one click. Nothing here has run yet.
            "proposed_actions": getattr(turn, "proposed_actions", []) or [],
            # The read trail. Collected by the router already and dropped here, which is why
            # the console could never show what an answer was based on.
            "tool_calls": getattr(turn, "tool_calls", []) or [],
            "messages": [*messages, assistant],
        }

    @staticmethod
    def _serialize_message(row) -> dict[str, Any]:
        def value(key: str) -> list[dict[str, Any]]:
            raw = row[key]
            return json.loads(raw) if isinstance(raw, str) else list(raw or [])

        created_at = row["created_at"]
        return {
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "tool_calls": value("tool_calls"),
            "actions": value("actions"),
            "created_at": created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at),
        }
