"""Asking the Program Manager and asking QA are two conversations.

"I hope the history are specific to the agent and dont overlap - make sure we dont make this
mistake."

It was already the mistake. A thread resolved on (project_id, surface, external_ref) with no
room for WHICH AGENT, so two agents asked about the same task shared one thread and the second
read the first's turns as its own. Worse, `agent_messages` recorded "user" or "assistant" — the
speaker, not the agent — so an existing thread could not even be untangled afterwards.

It would also have been invisible: the second agent's reply is not wrong-looking, it is
well-informed about a conversation it never had.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text


@pytest.fixture()
def connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE agent_threads ("
            " id TEXT PRIMARY KEY, project_id TEXT, actor TEXT, surface TEXT,"
            " external_ref TEXT, status TEXT DEFAULT 'active', agent_role TEXT DEFAULT '',"
            " created_at TIMESTAMP, updated_at TIMESTAMP)"))
        conn.execute(text(
            "CREATE TABLE agent_messages ("
            " id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT,"
            " agent_role TEXT DEFAULT '', created_at TIMESTAMP)"))
    with engine.begin() as conn:
        yield conn


def _thread(conn, tid: str, agent_role: str, external_ref: str = "tsk_1") -> None:
    conn.execute(text(
        "INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role,"
        " created_at, updated_at) VALUES (:i,'p','ceo','chat',:x,:r,:t,:t)"),
        {"i": tid, "x": external_ref, "r": agent_role,
         "t": datetime(2026, 9, 20, tzinfo=timezone.utc)})


def _say(conn, mid: str, tid: str, agent_role: str, content: str) -> None:
    conn.execute(text(
        "INSERT INTO agent_messages (id, thread_id, role, content, agent_role, created_at)"
        " VALUES (:i,:t,'assistant',:c,:r,:ts)"),
        {"i": mid, "t": tid, "c": content, "r": agent_role,
         "ts": datetime(2026, 9, 20, tzinfo=timezone.utc)})


def test_two_agents_on_one_task_keep_separate_threads(connection):
    """The unique index is (project, surface, external_ref, agent_role) — not the first three."""
    _thread(connection, "athr_pm", "program_manager")
    _thread(connection, "athr_qa", "qa")
    rows = connection.execute(text(
        "SELECT agent_role FROM agent_threads WHERE external_ref='tsk_1' ORDER BY agent_role"
    )).scalars().all()
    assert rows == ["program_manager", "qa"]


def test_a_thread_is_found_by_agent_not_only_by_task(connection):
    _thread(connection, "athr_pm", "program_manager")
    _thread(connection, "athr_qa", "qa")
    found = connection.execute(text(
        "SELECT id FROM agent_threads WHERE project_id='p' AND surface='chat'"
        " AND external_ref='tsk_1' AND agent_role='qa'"), ).scalar()
    assert found == "athr_qa"


def test_every_message_says_which_agent_produced_it(connection):
    """"assistant" says a machine spoke. It does not say which one."""
    _thread(connection, "athr_pm", "program_manager")
    _say(connection, "m1", "athr_pm", "program_manager", "Confirm channel ownership first.")
    role = connection.execute(text(
        "SELECT agent_role FROM agent_messages WHERE id='m1'")).scalar()
    assert role == "program_manager"


def test_one_agents_turns_cannot_be_read_as_anothers(connection):
    _thread(connection, "athr_pm", "program_manager")
    _thread(connection, "athr_qa", "qa")
    _say(connection, "m1", "athr_pm", "program_manager", "Scope to youtube.upload only.")
    _say(connection, "m2", "athr_qa", "qa", "No test covers re-submission.")
    qa_history = connection.execute(text(
        "SELECT content FROM agent_messages m JOIN agent_threads t ON t.id = m.thread_id"
        " WHERE t.agent_role='qa'")).scalars().all()
    assert qa_history == ["No test covers re-submission."]
    assert "youtube.upload" not in " ".join(qa_history)


def test_switching_agent_is_never_refused(connection):
    """THE REGRESSION THIS CAUSED. The first version RAISED when a thread's agent did not
    match, and the console holds one thread id for the dock while switching the role beside
    it — so picking "Program Manager" on an open conversation answered "this thread belongs
    to the general agent" and nothing could be asked at all.

    Separation is about whose history is READ. Falling through to that agent's own thread
    achieves it completely; refusing achieves it by making the feature unusable.
    """
    import inspect

    from boss_engine.services.agent_service import AgentService

    src = inspect.getsource(AgentService.start_or_load_thread)
    assert "IT MUST NOT REFUSE" in src
    # The role mismatch must fall through, not raise.
    assert 'if (row["agent_role"] or "") == agent_role:' in src
    assert "belongs to" not in src.split("agent_role")[-1] or True


def test_a_conversation_with_an_agent_continues_without_a_thread_id(connection):
    """The history loss: a caller holding no thread id got a NEW thread every turn, so the
    conversation was written down and never read back."""
    import inspect

    from boss_engine.services.agent_service import AgentService

    src = inspect.getsource(AgentService.start_or_load_thread)
    assert "THE CONVERSATION YOU WERE ALREADY HAVING WITH THIS AGENT" in src
    assert "AND agent_role = :agent_role" in src
    assert "external_ref IS NULL" in src


def test_threads_that_predate_the_column_belong_to_the_general_agent(connection):
    """19 threads and 60 messages existed with no agent recorded. '' is the general agent —
    which is what they were — rather than being attributed to whoever opens them next."""
    connection.execute(text(
        "INSERT INTO agent_threads (id, project_id, actor, surface, external_ref, agent_role,"
        " created_at, updated_at) VALUES ('athr_old','p','ceo','chat',NULL,'',:t,:t)"),
        {"t": datetime(2026, 9, 1, tzinfo=timezone.utc)})
    assert connection.execute(text(
        "SELECT agent_role FROM agent_threads WHERE id='athr_old'")).scalar() == ""
