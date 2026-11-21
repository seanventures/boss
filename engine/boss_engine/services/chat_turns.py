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

#: How often a running turn's heartbeat moves on its own (a timer), whatever the turn is doing —
#: a tool call that blocks for twenty minutes is still a live turn, never an orphan.
HEARTBEAT_SECONDS = 60

#: The words over a reply that arrived after its turn had been marked stopped.
LATE_PREFIX = ("**Late answer.** This turn was marked as stopped while it was still working; "
               "this is what it came back with.\n\n")

#: The turn this context is answering, for `note_action`: (engine, turn_id).
_TURN: ContextVar[Optional[tuple]] = ContextVar("boss_chat_turn", default=None)

_OWNER: dict[int, str] = {}


def process_owner() -> str:
    """Who runs a turn: host, pid and a token minted once per process. A new process that got
    a dead one's pid has a different token, so it can tell the dead turn is not its own."""
    import os
    import socket
    import uuid

    pid = os.getpid()
    if pid not in _OWNER:
        _OWNER[pid] = f"{socket.gethostname()}:{pid}:{uuid.uuid4().hex[:12]}"
    return _OWNER[pid]

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
    stop = threading.Event()

    def beat() -> None:
        # THE HEARTBEAT IS A TIMER, not only the checkpoints: a tool that blocks for longer
        # than the orphan bound is still this live turn, and must never be swept as dead.
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with _db.begin(engine) as c:
                    c.execute(text("UPDATE agent_chat_turns SET heartbeat_at = now() "
                                   "WHERE id = :t AND status = 'running'"), {"t": turn_id})
            except Exception:  # noqa: BLE001 — the next beat tries again
                LOGGER.info("chat turn %s: heartbeat skipped", turn_id, exc_info=True)

    threading.Thread(target=beat, daemon=True, name=f"chat-turn-beat-{turn_id}").start()
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
        this_turn = _TURN.set((engine, turn_id))
        try:
            yield connection
            if connection.in_transaction():
                connection.commit()
        except BaseException:
            if connection.in_transaction():
                connection.rollback()
            raise
        finally:
            stop.set()
            _TURN.reset(this_turn)
            _CHECKPOINT.reset(armed)
            _db._OPEN_TRANSACTIONS.reset(held)


def note_action(tool: str, result: dict[str, Any]) -> None:
    """A write a tool made inside a running turn, recorded on the turn AT ONCE, on its own
    short transaction (tools/registry.invoke calls this after every successful write tool).

    A checkpoint commits what the turn wrote, so a turn that fails later has still done these
    things: the failure line names them as done, so "ask again" never repeats them."""
    held = _TURN.get()
    if held is None:
        return
    engine, turn_id = held
    ids = [f"{k} {v}" for k, v in (result or {}).items()
           if isinstance(v, str) and v and (k == "id" or k.endswith("_id"))][:3]
    said = next((str(result.get(k)) for k in ("summary", "message", "status")
                 if isinstance(result.get(k), str) and result.get(k)), "")
    entry = {"tool": tool, "ids": ids, "said": said[:200]}
    try:
        from ..db import begin

        with begin(engine) as c:
            c.execute(text("UPDATE agent_chat_turns SET actions = actions || CAST(:a AS JSONB) "
                           "WHERE id = :t"), {"t": turn_id, "a": json.dumps([entry])})
    except Exception:  # noqa: BLE001 — a record of the work never breaks the work
        LOGGER.warning("chat turn %s: could not record %s", turn_id, tool, exc_info=True)


def by_key(connection: Any, *, project_id: str, actor: str, key: str) -> Optional[dict[str, Any]]:
    """The turn a client already started with this idempotency key, as `read` returns it."""
    turn_id = connection.execute(
        text("SELECT id FROM agent_chat_turns WHERE project_id = :p AND actor = :a "
             "AND client_key = :k"), {"p": project_id, "a": actor, "k": key}).scalar()
    return read(connection, turn_id) if turn_id else None


def open_row(connection: Any, *, project_id: str, thread_id: str, actor: str, agent_role: str,
             user_message_id: str, client_key: Optional[str] = None) -> str:
    """The turn's record, written with the operator's message (status running), owned by this
    process (`process_owner`), keyed by the client's idempotency key when it sent one."""
    turn_id = make_id("aturn")
    connection.execute(
        text("INSERT INTO agent_chat_turns (id, project_id, thread_id, actor, agent_role, status, "
             "user_message_id, client_key, owner) "
             "VALUES (:i, :p, :t, :a, :r, 'running', :m, :k, :o)"),
        {"i": turn_id, "p": project_id, "t": thread_id, "a": actor,
         "r": (agent_role or "").strip().lower(), "m": user_message_id,
         "k": (client_key or "").strip()[:200] or None, "o": process_owner()})
    return turn_id


def note_act(connection: Any, turn_id: Optional[str], act_id: Optional[str]) -> None:
    """The signed act this turn opened, so a failure can close it too."""
    if turn_id and act_id:
        connection.execute(text("UPDATE agent_chat_turns SET act_id = :a WHERE id = :t"),
                           {"a": act_id, "t": turn_id})


def mark_done(connection: Any, turn_id: Optional[str], *, reply_message_id: str,
              result: dict[str, Any]) -> bool:
    """The answer landed: written on the turn's own connection, with the reply. Only a turn
    still in flight becomes done; one already marked failed (swept while it worked) keeps its
    status, and its reply is labelled a late answer. Returns True for a late answer."""
    if not turn_id:
        return False
    # NAMES, NOT FILES: an attached screenshot is hundreds of kilobytes, already stored once in
    # agent_attachments; the response kept for a reloaded console names it.
    result = {**result, "messages": [
        {**m, "attachments": [{k: v for k, v in a.items() if k != "data"}
                              for a in (m.get("attachments") or []) if isinstance(a, dict)]}
        if isinstance(m, dict) and m.get("attachments") else m
        for m in (result.get("messages") or [])]}
    landed = connection.execute(
        text("UPDATE agent_chat_turns SET status = 'done', reply_message_id = :m, "
             "result = CAST(:r AS JSONB), finished_at = now(), heartbeat_at = now() "
             "WHERE id = :t AND status IN ('queued', 'running') RETURNING id"),
        {"t": turn_id, "m": reply_message_id, "r": json.dumps(result, default=str)}).first()
    if landed is not None:
        return False
    connection.execute(
        text("UPDATE agent_messages SET content = :p || content WHERE id = :m"),
        {"p": LATE_PREFIX, "m": reply_message_id})
    connection.execute(
        text("UPDATE agent_chat_turns SET reply_message_id = :m WHERE id = :t"),
        {"t": turn_id, "m": reply_message_id})
    return True


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
                     "RETURNING thread_id, agent_role, act_id, actions"),
                {"t": turn_id, "e": sentence[:2000],
                 "r": json.dumps({"failure": {"sentence": sentence, "technical": technical}})},
            ).mappings().first()
            if row is None:
                return
            _say_it_failed(connection, row["thread_id"], row["agent_role"], sentence,
                           row["actions"])
            _close_act(connection, row["act_id"])
    except Exception:  # noqa: BLE001 — the sweeper finishes what this could not
        LOGGER.exception("could not record chat turn %s as failed", turn_id)


def _close_act(connection: Any, act_id: Optional[str]) -> None:
    if not act_id:
        return
    from . import agent_acts

    agent_acts.quietly(connection, "close a failed chat turn",
                       lambda c: agent_acts.close_act(c, act_id, status="error"))


def failure_words(sentence: str, actions: Any) -> str:
    """The line a failed turn leaves in its thread. What it ALREADY DID stands — a checkpoint
    committed it — so it is named, and a retry is offered only for the rest; "ask again" over
    a turn that had started a build would start a second one."""
    done = [a for a in (json.loads(actions) if isinstance(actions, str) else (actions or []))
            if isinstance(a, dict) and a.get("tool")]
    head = f"That turn did not finish. {sentence}".strip()
    if not done:
        return f"{head} Nothing was changed. Your message is kept above; ask again to retry."
    lines = [f"- {a['tool']}" + (f" ({', '.join(a.get('ids') or [])})" if a.get("ids") else "")
             + (f": {a['said']}" if a.get("said") else "") for a in done[:12]]
    return "\n".join([
        head, "", "Before it stopped it had already done this, and it stands:", *lines, "",
        "Your message is kept above. Do not ask for these again; ask again only for what is "
        "not listed."])


def _say_it_failed(connection: Any, thread_id: str, agent_role: str, sentence: str,
                   actions: Any = None) -> None:
    connection.execute(
        text("INSERT INTO agent_messages (id, thread_id, role, content, agent_role) "
             "VALUES (:i, :t, 'assistant', :c, :r)"),
        {"i": make_id("amsg"), "t": thread_id, "r": agent_role or "",
         "c": failure_words(sentence, actions)})
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
             "RETURNING id, thread_id, agent_role, act_id, actions"),
        {"p": project_id, "e": STOPPED},
    ).mappings().all()
    return _after_sweep(connection, rows)


#: What a turn whose process died says.
STOPPED = "The process running this turn stopped (a restart or a crash) before it answered."


def _after_sweep(connection: Any, rows: Any) -> list[str]:
    for row in rows:
        _say_it_failed(connection, row["thread_id"], row["agent_role"], STOPPED, row["actions"])
        _close_act(connection, row["act_id"])
    return [r["id"] for r in rows]


def sweep_dead_owners(connection: Any) -> list[str]:
    """AT START-UP: fail at once every running turn a dead process on THIS host owned, instead
    of waiting out the heartbeat bound. Dead means its pid is gone, or its pid is ours with
    another token (we reused it). A live process's turns, and other hosts', are left alone."""
    import os

    me = process_owner()
    host, my_pid, _token = me.split(":", 2)
    dead: list[str] = []
    for turn_id, owner in connection.execute(text(
            "SELECT id, owner FROM agent_chat_turns WHERE status IN ('queued', 'running') "
            "AND owner LIKE :h"), {"h": f"{host}:%"}).all():
        parts = str(owner or "").split(":")
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        pid = int(parts[1])
        if str(pid) == my_pid:
            if owner != me:
                dead.append(turn_id)
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead.append(turn_id)
        except PermissionError:
            pass                                   # alive, someone else's
    if not dead:
        return []
    rows = connection.execute(
        text("UPDATE agent_chat_turns SET status = 'failed', finished_at = now(), error = :e "
             "WHERE id = ANY(:ids) AND status IN ('queued', 'running') "
             "RETURNING id, thread_id, agent_role, act_id, actions"),
        {"ids": dead, "e": STOPPED}).mappings().all()
    return _after_sweep(connection, rows)


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
