"""Metatron cannot start a new conversation (BOSS_ACTOR no longer forks one).

Three new BOSS_ACTOR names in the same project all continued the project's earlier thread —
"corrects my prior answer" on what should have been a first message. The key moved to
(project, actor, surface, role) resolved against the VERIFIED PRINCIPAL under identity
enforcement (identity_guard._bind_actor overwrites whatever `actor` is claimed), which is right
for identity but left no way to say "start over" on purpose.

`new_thread=True` is that way: it skips every lookup `start_or_load_thread` would otherwise use
to resume a conversation — including a passed `thread_id` — and creates a fresh row instead. A
private thread needs nothing else: the new row's later `updated_at` makes it the one the very
next plain call finds. A shared per-task thread occupies a unique slot
(project, surface, external_ref, agent_role), so its old row is freed first (its `external_ref`
is renamed, the same move `AgentService.clear_thread` already makes for "Clear").
"""
from __future__ import annotations

from datetime import datetime

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.services import agent_service
from boss_engine.services.agent_service import AgentService

TABLE = """
CREATE TABLE agent_threads (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, actor TEXT NOT NULL,
  surface TEXT NOT NULL DEFAULT 'chat', external_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active', pending_plan_id TEXT, pending_plan_summary TEXT,
  agent_role TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

UNIQUE_SHARED_INDEX = (
    "CREATE UNIQUE INDEX idx_agent_threads_surface_external "
    "ON agent_threads(project_id, surface, external_ref, agent_role) "
    "WHERE external_ref IS NOT NULL"
)


def _service() -> AgentService:
    return object.__new__(AgentService)  # the thread lookup needs no router


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(agent_service, "emit_event", lambda *a, **k: None)
    eng = create_engine("sqlite+pysqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text(TABLE))
        c.execute(text(UNIQUE_SHARED_INDEX))
    return eng


def test_new_thread_does_not_reuse_the_actors_open_conversation(engine):
    with engine.begin() as c:
        first = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="")
    with engine.begin() as c:
        # Without new_thread, the actor's existing active thread is resumed (the baseline).
        resumed = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="")
    assert resumed["id"] == first["id"]

    with engine.begin() as c:
        fresh = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="",
            new_thread=True)
    assert fresh["id"] != first["id"]

    with engine.begin() as c:
        # The old conversation is still there, readable by id -- "start over", not "erase".
        still_there = c.execute(
            text("SELECT status FROM agent_threads WHERE id = :id"), {"id": first["id"]}
        ).scalar_one()
    assert still_there == "active"


def test_new_thread_becomes_the_one_the_next_plain_call_finds(engine):
    with engine.begin() as c:
        first = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="")
        # Backdated, so ordering by updated_at is unambiguous regardless of clock resolution --
        # the point under test is which thread is picked, not when either was made.
        c.execute(text("UPDATE agent_threads SET updated_at = :t WHERE id = :id"),
                 {"t": datetime(2000, 1, 1), "id": first["id"]})
        fresh = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="",
            new_thread=True)
    assert fresh["id"] != first["id"]
    with engine.begin() as c:
        # Metatron's very next message, with no thread_id and no new_thread -- it must land
        # in the conversation /new just started, not the one before it.
        again = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="")
    assert again["id"] == fresh["id"]


def test_new_thread_ignores_a_held_thread_id(engine):
    """A caller holding a stale thread id (the Metatron client's in-memory cache) must not be
    able to defeat /new by passing it along."""
    with engine.begin() as c:
        held = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="")
    with engine.begin() as c:
        fresh = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat", agent_role="",
            thread_id=held["id"], new_thread=True)
    assert fresh["id"] != held["id"]


def test_new_thread_frees_a_shared_task_threads_slot_for_a_fresh_one(engine):
    """A task thread occupies a unique (project, surface, external_ref, agent_role) slot, so
    /new on it must free the old row before inserting the new one, or the INSERT 500s."""
    with engine.begin() as c:
        old = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="tsk_1", agent_role="qa")
    with engine.begin() as c:
        fresh = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="tsk_1", agent_role="qa", new_thread=True)
    assert fresh["id"] != old["id"]
    assert fresh["external_ref"] == "tsk_1"
    with engine.begin() as c:
        freed = c.execute(
            text("SELECT external_ref, status FROM agent_threads WHERE id = :id"),
            {"id": old["id"]},
        ).mappings().one()
    assert freed["external_ref"] != "tsk_1"
    assert freed["status"] == "active"  # archived by renaming, not closed -- same as clear_thread


def test_agent_chat_request_defaults_new_thread_to_false():
    """A turn that says nothing about it must behave exactly as before this fix: resume, never
    fork, the way every existing caller of AgentChatRequest already relies on."""
    from boss_engine.schemas import AgentChatRequest

    request = AgentChatRequest(project_id="boss-dogfood", actor="ceo", message="status?")
    assert request.new_thread is False

    asked = AgentChatRequest(project_id="boss-dogfood", actor="ceo", message="status?",
                             new_thread=True)
    assert asked.new_thread is True


def test_new_thread_on_a_task_is_found_by_any_project_actor_afterwards(engine):
    """The freed slot's replacement is the shared thread every actor resolves to next -- the
    normal task-thread rule, unaffected by who happened to ask for /new."""
    with engine.begin() as c:
        _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="tsk_1", agent_role="qa")
        fresh = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="tsk_1", agent_role="qa", new_thread=True)
    with engine.begin() as c:
        found_by_other_actor = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="someone_else", surface="chat",
            external_ref="tsk_1", agent_role="qa")
    assert found_by_other_actor["id"] == fresh["id"]
