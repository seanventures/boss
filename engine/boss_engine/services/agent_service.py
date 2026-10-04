from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from .capability_manifest import build_manifest
from .core import emit_event, make_id
from .intent_router import IntentRouter


LOGGER = logging.getLogger(__name__)


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
        agent_role: str = "",
    ) -> dict[str, Any]:
        # WHOSE CONVERSATION. The key was (project, surface, external_ref) and nothing else, so
        # asking the Program Manager and then asking QA on the same task resolved to ONE thread
        # and the second agent read the first's turns as its own. Normalised the same way the
        # role is matched everywhere else, or "QA" and "qa" would be two conversations.
        agent_role = (agent_role or "").strip().lower()
        if thread_id:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, actor, surface, external_ref, status,
                           agent_role, pending_plan_id, pending_plan_summary,
                           created_at, updated_at
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
            # A THREAD BELONGS TO THE AGENT THAT HAS BEEN SPEAKING IN IT — so asking a
            # DIFFERENT agent falls through to that agent's own thread rather than reading
            # this one's history.
            #
            # IT MUST NOT REFUSE. The first version raised here, and the console does exactly
            # what it forbade: it holds one thread id for the dock and switches the role
            # beside it, so picking "Program Manager" on an open conversation answered "this
            # thread belongs to the general agent" and the operator could not ask anything at
            # all. Separation is about whose history is READ, and starting a fresh thread for
            # the new agent achieves it completely — refusing achieves it by making the
            # feature unusable.
            # ...AND TO THE THING IT IS ABOUT. A held thread id is reused only when it is the
            # conversation this turn is scoped to. In boss-1 the dock held a role thread while
            # the turn was about a Preview step; answering in the held thread would have read
            # `role:Qa` as the subject and dropped the step entirely. A different scope falls
            # through to that scope's own thread — the same rule as a different role.
            same_scope = not external_ref or (row["external_ref"] or "") == external_ref
            if (row["agent_role"] or "") == agent_role:
                if same_scope:
                    return dict(row)

        if external_ref:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, actor, surface, external_ref, status,
                           agent_role, pending_plan_id, pending_plan_summary,
                           created_at, updated_at
                    FROM agent_threads
                    WHERE project_id = :project_id AND surface = :surface
                      AND external_ref = :external_ref AND agent_role = :agent_role
                    """
                ),
                {
                    "project_id": project_id,
                    "surface": surface,
                    "external_ref": external_ref,
                    "agent_role": agent_role,
                },
            ).mappings().first()
            if row:
                # Shared per-task thread (unique per project+surface+external_ref). Any authorized
                # project actor may post — do NOT reject on actor mismatch (that broke task chat when
                # the thread was first created by a different console actor).
                return dict(row)

        # THE CONVERSATION YOU WERE ALREADY HAVING WITH THIS AGENT.
        #
        # Without this, a caller that did not hold a thread id got a NEW thread every turn —
        # so the history was written down and never read back, which is why chat appeared to
        # persist on task threads (they carry an external_ref) and nowhere else. Keyed on who
        # is asking and who is being asked, so switching agent in the console lands in that
        # agent's own conversation rather than the previous one's.
        if not external_ref:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, actor, surface, external_ref, status,
                           agent_role, pending_plan_id, pending_plan_summary,
                           created_at, updated_at
                    FROM agent_threads
                    WHERE project_id = :project_id AND actor = :actor
                      AND surface = :surface AND agent_role = :agent_role
                      AND external_ref IS NULL AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {"project_id": project_id, "actor": actor, "surface": surface,
                 "agent_role": agent_role},
            ).mappings().first()
            if row:
                return dict(row)

        new_id = make_id("athr")
        params = {
            "id": new_id,
            "project_id": project_id,
            "actor": actor,
            "surface": surface,
            "external_ref": external_ref,
            "agent_role": agent_role,
        }
        # A SHARED THREAD THAT ALREADY EXISTS IS USED, NEVER A CRASH.
        #
        # goldeneye's database kept the old 3-column unique index on (project, surface,
        # external_ref) — `CREATE INDEX IF NOT EXISTS` never redefines one (db._converge_indexes
        # now does) — so the lookup above, keyed on the agent too, found nothing, this INSERT hit
        # the stale index, and "Ask Program Manager" failed on every turn with a raw
        # UniqueViolation. Two first turns racing on one subject end the same way. Inside a
        # savepoint so the refusal does not abort the caller's transaction; then the thread the
        # index says exists is read back and used.
        try:
            with connection.begin_nested():
                row = self._insert_thread(connection, params)
        except IntegrityError:
            if not external_ref:
                raise
            row = self._existing_shared_thread(connection, project_id=project_id,
                                               surface=surface, external_ref=external_ref,
                                               agent_role=agent_role)
            if row is None:
                raise
            if (row["agent_role"] or "") != agent_role:
                LOGGER.warning(
                    "agent_threads: a unique index refused %s's own thread on %s/%s; using %s. "
                    "The index predates agent_role in its key — ensure_schema rebuilds it.",
                    agent_role or "the general agent", surface, external_ref, row["id"])
            return dict(row)
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

    @staticmethod
    def _existing_shared_thread(connection: Connection, *, project_id: str, surface: str,
                                external_ref: str, agent_role: str):
        """The thread a unique violation says is already there: this agent's own (a race), else
        the one a stale 3-column index keys on (project, surface, external_ref) — the only
        conversation that index allows on this subject."""
        select = """
            SELECT id, project_id, actor, surface, external_ref, status,
                   agent_role, pending_plan_id, pending_plan_summary,
                   created_at, updated_at
            FROM agent_threads
            WHERE project_id = :project_id AND surface = :surface
              AND external_ref = :external_ref {extra}
            ORDER BY updated_at DESC
            LIMIT 1
        """
        keys = {"project_id": project_id, "surface": surface, "external_ref": external_ref}
        row = connection.execute(
            text(select.format(extra="AND agent_role = :agent_role")),
            {**keys, "agent_role": agent_role},
        ).mappings().first()
        if row is None:
            row = connection.execute(text(select.format(extra="")), keys).mappings().first()
        return row

    @staticmethod
    def _insert_thread(connection: Connection, params: dict[str, Any]):
        return connection.execute(
            text(
                """
                INSERT INTO agent_threads (
                    id, project_id, actor, surface, external_ref, agent_role
                )
                VALUES (:id, :project_id, :actor, :surface, :external_ref, :agent_role)
                RETURNING id, project_id, actor, surface, external_ref, status,
                          agent_role, pending_plan_id, pending_plan_summary,
                          created_at, updated_at
                """
            ),
            params,
        ).mappings().one()

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
        #: WHICH AGENT SAID IT. Recorded nowhere before, so an existing thread could not be
        #: sorted out after the fact — "assistant" says a machine spoke, not which one.
        agent_role: str = "",
    ) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                INSERT INTO agent_messages (
                    id, thread_id, role, content, tool_calls, actions, proposed_actions,
                    agent_role
                )
                VALUES (
                    :id, :thread_id, :role, :content,
                    CAST(:tool_calls AS JSONB), CAST(:actions AS JSONB),
                    CAST(:proposed AS JSONB), :agent_role
                )
                RETURNING id, role, content, tool_calls, actions, proposed_actions,
                          agent_role, created_at
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
                "agent_role": (agent_role or "").strip().lower(),
            },
        ).mappings().one()
        connection.execute(
            text("UPDATE agent_threads SET updated_at = now() WHERE id = :id"),
            {"id": thread_id},
        )
        return self._serialize_message(row)

    @staticmethod
    def _offers(raw: Any) -> list[dict[str, Any]]:
        """`proposed_actions` comes back as a list from JSONB and as text from some drivers."""
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return [o for o in (raw or []) if isinstance(o, dict)]

    def record_action_outcome(
        self,
        connection: Connection,
        *,
        thread_id: str,
        action_id: str,
        tool: str,
        args: dict[str, Any],
        label: str,
        ok: bool,
        result: dict[str, Any],
        error: str,
    ) -> None:
        """Write a pressed button into the conversation, and spend the offer.

        THE BUTTON WAS A DEAD END, AND THIS IS WHY THE AGENT LOOPED.
        ------------------------------------------------------------
        `/v1/engine/agent/action` ran the tool, emitted an event, returned the result — and
        never touched the thread. Every trace of the press lived in React state:

          * Refresh the page and the offers came back, all of them, because the stored
            assistant message still carried its `proposed_actions` untouched. An operator who
            had already published a workflow was shown the same four buttons again.
          * Worse, the NEXT TURN read that same untouched history. The model saw itself offer
            an action and saw no outcome of any kind, concluded the operator had not chosen
            yet, and offered it again. Ten times, if asked ten times.

        The earlier repair loop only fed FAILURES back, from the client. A success was still
        invisible, which is the case the operator actually hit: the write succeeded and the
        agent had no idea, so it proposed it again.

        So the outcome becomes a real message — durable, in the transcript the model reads —
        and the offer is struck from the message that made it.
        """
        payload = {k: v for k, v in (result or {}).items() if k not in ("ok", "tool")}
        detail = json.dumps(payload, default=str)[:600] if payload else ""
        # Addressed to the model as much as to the operator: the next turn reads this and must
        # not re-offer what it says already happened.
        content = (
            f"✓ The operator pressed **{label or tool}**, and `{tool}` ran and SUCCEEDED."
            + (f"\n\nIt returned:\n\n```json\n{detail}\n```" if detail else "")
            + "\n\nThis is done. Do not offer it again — say what changed and what is next."
            if ok else
            f"⚠ The operator pressed **{label or tool}**, and `{tool}` FAILED.\n\n"
            f"```\n{error or 'unknown error'}\n```\n\n"
            "Read that error literally, fix the arguments, and offer a CORRECTED action. Do "
            "not re-offer the same call unchanged."
        )
        # SPEND THE OFFER WHEREVER IT WAS MADE. Matched on the action id when the client sent
        # one, and on the tool name otherwise, so an older console that does not send an id
        # still stops replaying its buttons.
        def _spend(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                o for o in offers
                if not (str(o.get("id") or "") == action_id
                        or (not action_id and str(o.get("tool") or "") == tool))
            ]

        rows = connection.execute(
            text(
                "SELECT id, proposed_actions FROM agent_messages "
                " WHERE thread_id = :t "
                "   AND jsonb_array_length(COALESCE(proposed_actions, '[]'::jsonb)) > 0 "
                " ORDER BY created_at DESC, id DESC"
            ),
            {"t": thread_id},
        ).mappings().all()

        # THE THREE BUTTONS YOU DID NOT PRESS MUST SURVIVE THE ONE YOU DID.
        #
        # The console renders offers from the NEWEST assistant message only — older ones are
        # treated as answered by the turns after them. So appending this outcome with no
        # offers would silently retire every sibling offer as well: press "publish v3" and the
        # two unrelated cleanup buttons disappear, which is a second way for a press to look
        # like it broke something.
        survivors = _spend(self._offers(rows[0]["proposed_actions"]) if rows else [])

        self.append_message(
            connection, thread_id=thread_id, role="assistant", content=content,
            tool_calls=[{"tool": tool, "args": args or {}, "ok": ok, "error": error or ""}],
            # `kind` AS WELL AS `tool`, because both names are already in the table and the
            # console read only the first: a row written here — {id, tool, ok} — made the
            # transcript throw on `a.kind.replaceAll(...)` and took the whole page down with
            # it. The console no longer depends on either being present; writing both stops
            # the two shapes from drifting further apart.
            actions=[{"id": action_id, "tool": tool, "kind": tool, "ok": ok}],
            proposed_actions=survivors,
        )
        for row in rows:
            offers = self._offers(row["proposed_actions"])
            kept = _spend(offers)
            if len(kept) != len(offers):
                connection.execute(
                    text("UPDATE agent_messages SET proposed_actions = CAST(:p AS JSONB) "
                         " WHERE id = :id"),
                    {"p": json.dumps(kept), "id": row["id"]},
                )

    def list_messages(
        self, connection: Connection, thread_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, role, content, tool_calls, actions, proposed_actions,
                       agent_role, created_at
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
        emit_event(
            connection, project_id, "agent.thread_archived", actor,
            "agent_thread", thread_id, {"previous_external_ref": ref},
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
                SELECT t.id, t.status, t.external_ref, t.agent_role, t.created_at, t.updated_at,
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
                    # Which agent this conversation is with. A subject has one thread PER
                    # agent, and the console must open the one the turn will land in.
                    "agent_role": r["agent_role"] or "",
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
        #: "investigate" asks for the full tool loop on a question about a record — the dock's
        #: "Investigate further" / "Do the fix". Otherwise such a question is answered first in
        #: one call from the record (subject_answer.py).
        depth: Optional[str] = None,
        #: WHAT THE DOCK WAS OPENED ABOUT — {act_id} or {workflow_run_id, step_id}. The turn is
        #: signed as work under it, so "Ask about this" shows up in that work's chain.
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        # THE AGENT IS PART OF WHICH CONVERSATION THIS IS. Without it the thread resolved on
        # (project, surface, external_ref) alone, so asking a second agent about the same task
        # handed it the first one's history — and nothing on the messages could tell them
        # apart afterwards either.
        thread = self.start_or_load_thread(
            connection,
            project_id=project_id,
            actor=actor,
            thread_id=thread_id,
            surface=surface,
            external_ref=external_ref,
            agent_role=role or "",
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
        # A QUESTION TO AN AGENT IS SIGNED WORK (agent_acts): one act per turn, asked by this
        # person, under whatever the dock was opened about. BOSS-in-general is not an agent and
        # signs nothing.
        act_id = self._open_chat_act(connection, project_id=project_id, actor=actor, role=role,
                                     thread_id=thread["id"], message=user_content,
                                     context=context)
        # Build exactly once for the turn. The router receives the resolved
        # object and never reaches back into the feature catalog per tool call.
        manifest = build_manifest(connection, self.settings, project_id)
        # A build or a panel this turn starts was started by this turn, for this person
        # (agent_acts.origin): its stages and seats are signed under the turn.
        from . import agent_acts

        with agent_acts.origin(act_id=act_id, actor=actor):
            turn = self.router.route_turn(
                connection,
                project_id=project_id,
                actor=actor,
                messages=messages,
                manifest=manifest,
                confirm_plan_id=confirm_plan_id,
                pending_plan_id=thread.get("pending_plan_id"),
                # A task-scoped thread carries its task_id -> the router builds ON that task's
                # existing app branch (extend the PR) instead of a fresh build on main.
                external_ref=thread.get("external_ref"),
                role=role,
                mode=mode,
                # So the turn can report what it is doing while it does it. See agent_progress:
                # a chat turn is one HTTP request, and an investigation that now runs six or more
                # lookups would otherwise be two minutes of a pulsing dot.
                thread_id=thread["id"],
                depth=depth,
            )
        assistant = self.append_message(
            connection,
            thread_id=thread["id"],
            role="assistant",
            content=turn.reply,
            tool_calls=turn.tool_calls,
            actions=turn.actions,
            proposed_actions=getattr(turn, "proposed_actions", []) or [],
            agent_role=role or "",
        )
        cited = self._close_chat_act(connection, project_id=project_id, act_id=act_id,
                                     message_ids=[written["id"], assistant["id"]],
                                     reply=turn.reply)

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
            # What the dock offers under a first answer: "Investigate further", "Do the fix".
            "next_steps": getattr(turn, "next_steps", []) or [],
            "messages": [*messages, assistant],
            # The signed turn, and the agent's own work it pointed at ([act:…] in the reply).
            "act_id": act_id,
            "cited_acts": cited,
        }

    @staticmethod
    def _open_chat_act(connection: Connection, *, project_id: str, actor: str,
                       role: Optional[str], thread_id: str, message: str,
                       context: Optional[dict[str, Any]]) -> Optional[str]:
        from . import agent_acts

        role_key = agent_acts.resolve_role(role) if role else None
        if not role_key or not connection.execute(
                text("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
                {"p": project_id, "r": role_key}).scalar():
            return None
        return agent_acts.quietly(
            connection, "sign a chat turn", agent_acts.open_chat_act, project_id=project_id,
            actor=actor, role_key=role_key, thread_id=thread_id, message=message,
            context=context)

    @staticmethod
    def _close_chat_act(connection: Connection, *, project_id: str, act_id: Optional[str],
                        message_ids: list[str], reply: str) -> list[str]:
        if not act_id:
            return []
        from . import agent_acts

        def close(c: Connection) -> list[str]:
            agent_acts.close_act(c, act_id, status="complete")
            c.execute(
                text("UPDATE agent_messages m SET act_id = a.act_id, fingerprint = a.fingerprint "
                     "FROM agent_acts a WHERE a.act_id = :a AND m.id = ANY(:ids)"),
                {"a": act_id, "ids": message_ids},
            )
            return agent_acts.cited_acts(c, project_id, reply)

        return agent_acts.quietly(connection, "close a chat turn", close) or []

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
