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
        #: The buttons this reply put above itself. Not writes that ran — offers, which is
        #: why they are not in `actions`. Stored so reopening the thread restores them.
        proposed_actions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                INSERT INTO agent_messages (
                    id, thread_id, role, content, tool_calls, actions, proposed_actions
                )
                VALUES (
                    :id, :thread_id, :role, :content,
                    CAST(:tool_calls AS JSONB), CAST(:actions AS JSONB),
                    CAST(:proposed AS JSONB)
                )
                RETURNING id, role, content, tool_calls, actions, proposed_actions, created_at
                """
            ),
            {
                "id": make_id("amsg"),
                "thread_id": thread_id,
                "role": role,
                "content": content,
                "tool_calls": json.dumps(tool_calls or []),
                "actions": json.dumps(actions or []),
                "proposed": json.dumps(proposed_actions or []),
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
                SELECT id, role, content, tool_calls, actions, proposed_actions, created_at
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
        # NAMES ONLY, and in one query. A restored thread has to show that a file was sent —
        # otherwise "Look at what I've attached." hangs over nothing and no reader can tell
        # whether it arrived — but the DATA is hundreds of kilobytes per image and no reader of
        # a transcript needs it. It is fetched only when a turn actually uses it.
        files: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute(
            text("SELECT message_id, name, kind FROM agent_attachments "
                 "WHERE thread_id = :thread_id ORDER BY created_at"),
            {"thread_id": thread_id},
        ).mappings().all():
            files.setdefault(str(row["message_id"]), []).append(
                {"name": row["name"], "kind": row["kind"]})
        out: list[dict[str, Any]] = []
        for row in rows:
            message = self._serialize_message(row)
            attached = files.get(str(message["id"]))
            if attached:
                message["attachments"] = attached
            out.append(message)
        return out

    def clear_thread(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Start this conversation again, without destroying the one that was there.

        "Clear all" on a role's chat, bounded to that role. It acts on ONE thread id, so it
        cannot reach another agent's conversation by construction — there is no wildcard here
        to get wrong.

        ARCHIVED, NOT DELETED. The thread is closed and its `external_ref` is stamped with its
        own id, which is what actually does the work: the ref is the find-or-create key
        (`role:Engineering Captain`), and freeing it is what lets the next message open a clean
        thread. The messages stay exactly where they were and remain readable by id, so a chat
        cleared by mistake is a lookup rather than a loss.
        """
        row = connection.execute(
            text(
                """
                SELECT id, project_id, actor, external_ref, status
                FROM agent_threads WHERE id = :id FOR UPDATE
                """
            ),
            {"id": thread_id},
        ).mappings().first()
        if row is None:
            raise AgentThreadNotFoundError(f"no thread {thread_id}")
        if row["project_id"] != project_id:
            # The same fence every other thread operation uses: a console actor authorised for
            # one project must not be able to clear another's conversation.
            raise AgentThreadOwnershipError("that conversation belongs to another project")
        if not row["external_ref"] and row["actor"] != actor:
            raise AgentThreadOwnershipError("that conversation belongs to another actor")

        ref = row["external_ref"]
        connection.execute(
            text(
                """
                UPDATE agent_threads
                SET status = 'closed',
                    -- CAST, because psycopg 3 will not infer a bare parameter's type inside a
                    -- CASE: "could not determine data type of parameter $1". Prod runs psycopg
                    -- 3 and this laptop runs psycopg2, which infers happily — so the statement
                    -- passed every local test and 500'd on the first real click.
                    external_ref = CASE WHEN CAST(:ref AS TEXT) IS NULL THEN NULL
                                        ELSE CAST(:ref AS TEXT) || '#cleared-' || id END,
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": thread_id, "ref": ref},
        )
        return {"ok": True, "thread_id": thread_id, "was": ref,
                "note": "closed and archived — the messages are kept, and the next one starts "
                        "a fresh conversation"}


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
                  -- A cleared conversation is closed, and closed means gone from the list.
                  -- Without this, clearing a GENERAL chat archived it correctly and left it
                  -- sitting in the rail — the operator presses Clear and the conversation is
                  -- still there, which reads as the button having done nothing. (A cleared
                  -- ROLE chat disappears either way, because its find-or-create key is
                  -- stamped, so this gap was invisible on the surface it was built for.)
                  AND t.status <> 'closed'
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
        #: Answer as this member of the workforce, rather than as BOSS in general.
        #: See _role_identity in intent_router for what it changes.
        role: Optional[str] = None,
        #: "discuss" thinks about an idea and starts nothing; anything else operates the
        #: factory. See _one_conversation_brief in intent_router.
        mode: str = "operate",
        #: Files the operator attached to THIS message — a screenshot of the error, a log too
        #: long to paste. Each is {name, media_type, kind, data}.
        attachments: Optional[list[dict[str, Any]]] = None,
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
        if not user_content and attachments:
            # A SCREENSHOT IS A QUESTION. Sending one with no words is the most natural thing an
            # operator does — "look at this" — and an empty message body would otherwise read as
            # a blank turn to every later reader of the thread.
            user_content = "Look at what I've attached."
        written = self.append_message(
            connection,
            thread_id=thread["id"],
            role="user",
            content=user_content,
        )
        stored = self._store_attachments(
            connection, project_id=project_id, thread_id=thread["id"],
            message_id=written["id"], attachments=attachments or [])
        messages = self.list_messages(connection, thread["id"])
        if stored:
            # ONLY ON THE NEWEST TURN. The files belong to the message the operator just sent;
            # re-attaching every image in a thread's history to every later turn is how a
            # conversation becomes unaffordable after the third screenshot.
            messages[-1] = {**messages[-1], "attachments": stored}
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
            role=role,
            mode=mode,
            # So the turn can report what it is doing while it does it. See agent_progress:
            # a chat turn is one HTTP request, and an investigation that now runs six or more
            # lookups would otherwise be two minutes of a pulsing dot.
            thread_id=thread["id"],
        )
        assistant = self.append_message(
            connection,
            thread_id=thread["id"],
            role="assistant",
            content=turn.reply,
            tool_calls=turn.tool_calls,
            actions=turn.actions,
            proposed_actions=getattr(turn, "proposed_actions", []) or [],
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

    def _store_attachments(
        self,
        connection: Connection,
        *,
        project_id: str,
        thread_id: str,
        message_id: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Write this message's files, and hand back what the turn should carry."""
        out: list[dict[str, Any]] = []
        for item in attachments:
            data = str(item.get("data") or "")
            if not data:
                continue
            name = str(item.get("name") or "file")[:200]
            media_type = str(item.get("media_type") or "application/octet-stream")
            kind = str(item.get("kind") or ("image" if media_type.startswith("image/") else "text"))
            attachment_id = make_id("aatt")
            connection.execute(
                text(
                    """
                    INSERT INTO agent_attachments (
                        id, project_id, thread_id, message_id, name, media_type, kind,
                        data, bytes
                    ) VALUES (
                        :id, :project_id, :thread_id, :message_id, :name, :media_type, :kind,
                        :data, :bytes
                    )
                    """
                ),
                {"id": attachment_id, "project_id": project_id, "thread_id": thread_id,
                 "message_id": message_id, "name": name, "media_type": media_type,
                 "kind": kind, "data": data, "bytes": len(data)},
            )
            out.append({"id": attachment_id, "name": name, "media_type": media_type,
                        "kind": kind, "data": data})
        return out

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
            "proposed_actions": value("proposed_actions"),
            "created_at": created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else str(created_at),
        }
