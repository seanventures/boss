"""One chat turn, durably: the question is kept whatever happens to the answer.

A console turn used to be ONE database transaction from the operator's message to the reply —
about two and a half minutes on the laptop (2026-11-21). Nothing of it was visible while it ran,
and a failure, a worker restart or Postgres's idle-in-transaction timeout (300 s,
BOSS_API_DB_IDLE_IN_TRANSACTION_SECONDS) rolled the operator's message back with it — while the
console promised "It has not been dropped".

The turn is three steps now (AgentService.chat_durably):

1. ``open_turn`` commits the operator's message and a row here (status ``running``, a
   ``turn_id``) before any model is called.
2. The answer runs on a connection that commits at every step (``committing_between_calls``):
   before each model call and at each progress line, so no transaction sits open while a model
   thinks. ``heartbeat_at`` moves at the same points.
3. The reply lands with status ``done`` and the turn's response in ``result`` (what a console
   that reloaded reads back); a failure lands as status ``failed`` with a plain sentence, and
   the message stays where it was.

A turn whose process died (a restart, a killed worker) stops moving its heartbeat;
``sweep_orphans`` marks it failed after ``ORPHAN_AFTER``, so a console never waits on a turn
nobody is running.
"""
from __future__ import annotations

import contextlib
import json
import logging
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import text

from .core import make_id

LOGGER = logging.getLogger(__name__)

#: A running turn whose heartbeat has not moved for this long is not running: the process that
#: held it stopped. Longer than any single model call or tool a chat turn makes.
ORPHAN_AFTER = "15 minutes"

#: Statuses a turn is still in flight under.
LIVE = ("queued", "running")

#: What the turn connection does at a safe point (commit + heartbeat), while one is in use.
_CHECKPOINT: ContextVar[Optional[Callable[[], None]]] = ContextVar("boss_chat_turn_checkpoint",
                                                                   default=None)


def checkpoint() -> None:
    """A safe point in a running turn: commit what it wrote so far and move its heartbeat.

    Called before every governed model call (model_routing.GovernedChatAdapter) and at every
    progress line (intent_router._progress_reporter). A no-op outside a durable turn, so every
    other caller of those paths is unchanged."""
    hook = _CHECKPOINT.get()
    if hook is not None:
        hook()


@contextlib.contextmanager
def committing_between_calls(engine: Any, turn_id: str) -> Iterator[Any]:
    """A connection for the answer of one turn that never holds a transaction across a model call.

    SQLAlchemy's autobegin opens a transaction at the first statement; `checkpoint` commits it
    (never inside a savepoint, where a commit would break the caller's `begin_nested`) and the
    next statement opens the next one. Registered as an open transaction (db.open_transactions)
    exactly like `db.begin`, so a component that checks whether its caller holds one still sees
    it. Commits on a clean exit; rolls back only the CURRENT step on an error — everything a
    checkpoint already committed stays, the operator's message first of all."""
    from .. import db as _db

    import threading

    owner = threading.get_ident()
    with engine.connect() as connection:
        def hook() -> None:
            # Only on the turn's own thread: a tool fanned out to a worker thread (with this
            # context copied) must never commit a connection the turn is using.
            if threading.get_ident() != owner:
                return
            if not connection.in_transaction() or connection.in_nested_transaction():
                return
            try:
                connection.execute(text("UPDATE agent_chat_turns SET heartbeat_at = now() "
                                        "WHERE id = :t AND status = 'running'"), {"t": turn_id})
                connection.commit()
            except Exception:  # noqa: BLE001 — a step that already broke its transaction
                LOGGER.warning("chat turn %s: checkpoint could not commit", turn_id,
                               exc_info=True)
                connection.rollback()

        held = _db._OPEN_TRANSACTIONS.set(_db._OPEN_TRANSACTIONS.get() + (connection,))
        armed = _CHECKPOINT.set(hook)
        try:
            yield connection
            if connection.in_transaction():
                connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            _CHECKPOINT.reset(armed)
            _db._OPEN_TRANSACTIONS.reset(held)


def open_row(connection: Any, *, project_id: str, thread_id: str, actor: str, agent_role: str,
             user_message_id: str) -> str:
    """The turn's record, written with the operator's message (status running)."""
    turn_id = make_id("aturn")
    connection.execute(
        text("INSERT INTO agent_chat_turns (id, project_id, thread_id, actor, agent_role, status, "
             "user_message_id) VALUES (:i, :p, :t, :a, :r, 'running', :m)"),
        {"i": turn_id, "p": project_id, "t": thread_id, "a": actor,
         "r": (agent_role or "").strip().lower(), "m": user_message_id})
    return turn_id


def note_act(connection: Any, turn_id: Optional[str], act_id: Optional[str]) -> None:
    """The signed act this turn opened, so a failure can close it too."""
    if turn_id and act_id:
        connection.execute(text("UPDATE agent_chat_turns SET act_id = :a WHERE id = :t"),
                           {"a": act_id, "t": turn_id})


def mark_done(connection: Any, turn_id: Optional[str], *, reply_message_id: str,
              result: dict[str, Any]) -> None:
    """The answer landed: written on the turn's own connection, with the reply."""
    if not turn_id:
        return
    # NAMES, NOT FILES: an attached screenshot is hundreds of kilobytes, already stored once in
    # agent_attachments; the response kept for a reloaded console names it.
    result = {**result, "messages": [
        {**m, "attachments": [{k: v for k, v in a.items() if k != "data"}
                              for a in (m.get("attachments") or []) if isinstance(a, dict)]}
        if isinstance(m, dict) and m.get("attachments") else m
        for m in (result.get("messages") or [])]}
    connection.execute(
        text("UPDATE agent_chat_turns SET status = 'done', reply_message_id = :m, "
             "result = CAST(:r AS JSONB), finished_at = now(), heartbeat_at = now() "
             "WHERE id = :t"),
        {"t": turn_id, "m": reply_message_id, "r": json.dumps(result, default=str)})


def failure_sentence(error: BaseException) -> tuple[str, str]:
    """(the sentence a person reads, the technical line) for a failed turn."""
    try:
        from .model_failures import explain_turn_failure

        said = explain_turn_failure(error)
        return str(said.get("sentence") or ""), str(said.get("detail") or "")
    except Exception:  # noqa: BLE001 — the reason in plain words still beats silence
        return "BOSS couldn't finish this turn. Try again.", type(error).__name__


def mark_failed(engine: Any, turn_id: str, error: BaseException | str) -> None:
    """A turn that failed says so, in its own short transaction, and keeps the message.

    The thread gets one assistant line saying the turn did not finish and why, so a reopened
    conversation never ends on an unanswered question; the act it opened is closed as an error.
    """
    from ..db import begin

    if isinstance(error, BaseException):
        sentence, technical = failure_sentence(error)
    else:
        sentence, technical = str(error), ""
    try:
        with begin(engine) as connection:
            row = connection.execute(
                text("UPDATE agent_chat_turns SET status = 'failed', error = :e, "
                     "result = CAST(:r AS JSONB), finished_at = now() "
                     "WHERE id = :t AND status IN ('queued', 'running') "
                     "RETURNING thread_id, agent_role, act_id"),
                {"t": turn_id, "e": sentence[:2000],
                 "r": json.dumps({"failure": {"sentence": sentence, "technical": technical}})},
            ).mappings().first()
            if row is None:
                return
            _say_it_failed(connection, row["thread_id"], row["agent_role"], sentence)
            if row["act_id"]:
                from . import agent_acts

                agent_acts.quietly(connection, "close a failed chat turn",
                                   lambda c: agent_acts.close_act(c, row["act_id"], status="error"))
    except Exception:  # noqa: BLE001 — the sweeper finishes what this could not
        LOGGER.exception("could not record chat turn %s as failed", turn_id)


def _say_it_failed(connection: Any, thread_id: str, agent_role: str, sentence: str) -> None:
    connection.execute(
        text("INSERT INTO agent_messages (id, thread_id, role, content, agent_role) "
             "VALUES (:i, :t, 'assistant', :c, :r)"),
        {"i": make_id("amsg"), "t": thread_id, "r": agent_role or "",
         "c": ("That turn did not finish. " + sentence
               + " Your message is kept above; ask again to retry.").strip()})
    connection.execute(text("UPDATE agent_threads SET updated_at = now() WHERE id = :t"),
                       {"t": thread_id})


def sweep_orphans(connection: Any, project_id: Optional[str] = None) -> list[str]:
    """Mark failed every running turn whose heartbeat stopped (its process died). Cheap: one
    indexed UPDATE, run wherever a turn's state is read."""
    rows = connection.execute(
        text("UPDATE agent_chat_turns SET status = 'failed', finished_at = now(), "
             "error = :e WHERE status IN ('queued', 'running') "
             f"AND heartbeat_at < now() - INTERVAL '{ORPHAN_AFTER}' "
             "AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) "
             "RETURNING id, thread_id, agent_role"),
        {"p": project_id,
         "e": "The process running this turn stopped (a restart or a crash) before it "
              "answered. Nothing else was lost; ask again to retry."},
    ).mappings().all()
    for row in rows:
        _say_it_failed(connection, row["thread_id"], row["agent_role"],
                       "The process running it stopped (a restart or a crash) before it "
                       "answered.")
    return [r["id"] for r in rows]


def running_turn(connection: Any, project_id: str, thread_id: str) -> Optional[dict[str, Any]]:
    """The turn still in flight on this thread, after sweeping the dead ones; None when idle."""
    sweep_orphans(connection, project_id)
    row = connection.execute(
        text("SELECT id, status, created_at FROM agent_chat_turns "
             "WHERE thread_id = :t AND project_id = :p AND status IN ('queued', 'running') "
             "ORDER BY created_at DESC LIMIT 1"),
        {"t": thread_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return None
    return {"turn_id": row["id"], "status": row["status"],
            "started_at": row["created_at"].isoformat() if row["created_at"] else None}


def read(connection: Any, turn_id: str) -> Optional[dict[str, Any]]:
    """One turn: {turn_id, thread_id, project_id, actor, status, error, result}."""
    row = connection.execute(
        text("SELECT id, project_id, thread_id, actor, status, error, result, created_at, "
             "finished_at FROM agent_chat_turns WHERE id = :t"),
        {"t": turn_id},
    ).mappings().first()
    if row is None:
        return None
    if row["status"] in LIVE:
        sweep_orphans(connection, row["project_id"])
        row = connection.execute(
            text("SELECT id, project_id, thread_id, actor, status, error, result, created_at, "
                 "finished_at FROM agent_chat_turns WHERE id = :t"),
            {"t": turn_id},
        ).mappings().first()
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return {"turn_id": row["id"], "project_id": row["project_id"], "thread_id": row["thread_id"],
            "actor": row["actor"], "status": row["status"], "error": row["error"],
            "result": result if row["status"] == "done" else None,
            "failure": (result or {}).get("failure") if row["status"] == "failed" else None,
            "started_at": row["created_at"].isoformat() if row["created_at"] else None,
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None}
