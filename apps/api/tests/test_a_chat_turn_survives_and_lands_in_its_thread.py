"""A chat turn keeps the question whatever happens to the answer, and lands where the drawer looks.

Verified on the laptop install, 2026-11-21:

A. A console Helm turn ran inside ONE database transaction (~2.5 min). Nothing was visible while
   it ran, and a failure, a worker restart or Postgres's idle-in-transaction timeout rolled the
   operator's message back — under a console line promising "It has not been dropped". There
   was no turn id to resume after a reload.
B. The drawer created its thread via /v1/agent/threads/ensure with the raw role "helm"; the turn
   resolved "helm" to the captain's key and opened a DIFFERENT thread, so the drawer reopened
   empty.

Now: one role resolution for every door (agent_service.thread_role), and a durable turn
(services/chat_turns.py) — the message and a turn record are committed before any model call,
the answer runs on short transactions, a failure is recorded in words, a dead process is swept.
The model's side is stood in for at the router, the seam every turn goes through.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"
ACTOR = "executive_console"


def _reply(text_: str):
    return SimpleNamespace(reply=text_, tool_calls=[], actions=[], proposed_actions=[],
                           pending_confirmation=None, next_steps=[])


def _stub(monkeypatch, fn) -> None:
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.orchestration_agent.router, "route_turn", fn)


def _captain_key(engine) -> str:
    from boss_engine.services.agent_service import thread_role

    with engine.begin() as c:
        key = thread_role(c, PROJECT, "helm")
    if key == "helm":
        pytest.skip("this seed has no agent answering to 'helm'")
    return key


def _ensure(client, role: str, external_ref: str | None = None) -> str:
    body = {"project_id": PROJECT, "actor": ACTOR, "role": role}
    if external_ref:
        body["external_ref"] = external_ref
    said = client.post("/v1/agent/threads/ensure", json=body)
    assert said.status_code == 200, said.text
    return said.json()["thread_id"]


def _messages(client, thread_id: str) -> dict:
    said = client.get(f"/v1/agent/threads/{thread_id}/messages",
                      params={"project_id": PROJECT, "actor": ACTOR})
    assert said.status_code == 200, said.text
    return said.json()


def _turn(client, turn_id: str) -> dict:
    said = client.get(f"/v1/agent/turns/{turn_id}", params={"project_id": PROJECT, "actor": ACTOR})
    assert said.status_code == 200, said.text
    return said.json()


def _wait_for(client, turn_id: str, seconds: float = 20.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        turn = _turn(client, turn_id)
        if turn["status"] not in ("queued", "running"):
            return turn
        time.sleep(0.1)
    raise AssertionError(f"turn {turn_id} never finished")


# ── B. the drawer and the turn use one thread ───────────────────────────────────────────────


@pytest.mark.parametrize("external_ref", [None, "role:Helm"])
def test_a_turn_to_helm_lands_in_the_thread_ensure_returned(client, engine, monkeypatch,
                                                           external_ref):
    _captain_key(engine)
    _stub(monkeypatch, lambda *_a, **_k: _reply("On it."))
    ensured = _ensure(client, "helm", external_ref)
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "role": "helm", "thread_id": ensured,
        "external_ref": external_ref, "message": "What is stuck?"})
    assert said.status_code == 200, said.text
    assert said.json()["thread_id"] == ensured
    # Reopened, the drawer asks again and gets the same conversation, with the answer in it.
    assert _ensure(client, "helm", external_ref) == ensured
    contents = [m["content"] for m in _messages(client, ensured)["messages"]]
    assert contents == ["What is stuck?", "On it."]


def test_a_split_thread_is_reachable_the_one_with_messages_wins(client, engine):
    """Before the fix the drawer left an empty "helm" thread beside the captain's real one (or
    the real conversation sat under "helm"). Either way the drawer must open the conversation."""
    captain = _captain_key(engine)
    with engine.begin() as c:
        for tid, role in (("athr_split_empty", "helm"), ("athr_split_real", captain)):
            c.execute(text("INSERT INTO agent_threads (id, project_id, actor, surface, "
                           "external_ref, agent_role) VALUES (:i, :p, :a, 'chat', 'role:Helm', :r)"),
                      {"i": tid, "p": PROJECT, "a": ACTOR, "r": role})
        c.execute(text("INSERT INTO agent_messages (id, thread_id, role, content) "
                       "VALUES ('amsg_split_1', 'athr_split_real', 'user', 'earlier question')"))
    assert _ensure(client, "helm", "role:Helm") == "athr_split_real"

    # And the other way round: the conversation was filed under the raw name.
    with engine.begin() as c:
        c.execute(text("UPDATE agent_messages SET thread_id = 'athr_split_empty' "
                       "WHERE id = 'amsg_split_1'"))
    assert _ensure(client, "Helm", "role:Helm") == "athr_split_empty"


# ── A. the question is kept, the turn is durable ────────────────────────────────────────────


def test_a_turn_killed_midway_keeps_the_message_and_ends_failed(client, engine, monkeypatch):
    def dies(*_a, **_k):
        raise RuntimeError("worker lost its model connection")

    _stub(monkeypatch, dies)
    thread = _ensure(client, "")
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
        "message": "Please keep this question"})
    assert said.status_code == 500
    assert said.json()["detail"].startswith("BOSS couldn't finish this turn.")

    read = _messages(client, thread)
    assert [m["role"] for m in read["messages"]] == ["user", "assistant"]
    assert read["messages"][0]["content"] == "Please keep this question"
    assert read["messages"][1]["content"].startswith("That turn did not finish.")
    assert read["turn"] is None and read["working"] is False
    with engine.begin() as c:
        row = c.execute(text("SELECT status, error FROM agent_chat_turns WHERE thread_id = :t"),
                        {"t": thread}).mappings().one()
    assert row["status"] == "failed" and row["error"].startswith("BOSS couldn't finish")
    assert "RuntimeError" not in row["error"]


def test_a_turn_whose_process_died_is_swept_to_failed(client, engine):
    from boss_engine.services import chat_turns

    thread = _ensure(client, "")
    with engine.begin() as c:
        c.execute(text("INSERT INTO agent_messages (id, thread_id, role, content) "
                       "VALUES ('amsg_orphan', :t, 'user', 'asked before the restart')"),
                  {"t": thread})
        turn_id = chat_turns.open_row(c, project_id=PROJECT, thread_id=thread, actor=ACTOR,
                                      agent_role="", user_message_id="amsg_orphan")
        c.execute(text("UPDATE agent_chat_turns SET heartbeat_at = now() - INTERVAL '1 hour' "
                       "WHERE id = :t"), {"t": turn_id})
    turn = _turn(client, turn_id)
    assert turn["status"] == "failed"
    assert "stopped" in turn["error"]
    read = _messages(client, thread)
    assert read["turn"] is None
    assert [m["content"] for m in read["messages"]][0] == "asked before the restart"
    assert read["messages"][-1]["role"] == "assistant"


def test_a_background_turn_is_visible_mid_turn_and_resumes_after_a_reload(client, engine,
                                                                        monkeypatch):
    """202 with a turn id at once; mid-turn the question is already committed (a reload sees it
    and the running turn); the answer lands as `done` with the response; no transaction is held
    across the model call (chat_turns.checkpoint)."""
    from boss_engine.services import chat_turns

    release = threading.Event()
    held: dict = {}

    def slow(connection, **_k):
        chat_turns.checkpoint()                    # what a governed model call does first
        held["in_transaction"] = connection.in_transaction()
        assert release.wait(20)
        return _reply("Answered after the reload.")

    _stub(monkeypatch, slow)
    thread = _ensure(client, "")
    started = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
        "message": "Still there after a reload?", "background": True})
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["thread_id"] == thread and body["status"] == "running"
    turn_id = body["turn_id"]

    # THE RELOAD: a fresh read of the thread finds the question and the running turn.
    read = _messages(client, thread)
    assert [m["content"] for m in read["messages"]] == ["Still there after a reload?"]
    assert read["turn"]["turn_id"] == turn_id and read["working"] is True
    assert _turn(client, turn_id)["status"] == "running"

    release.set()
    done = _wait_for(client, turn_id)
    assert done["status"] == "done"
    assert done["result"]["reply"] == "Answered after the reload."
    assert done["result"]["turn_id"] == turn_id
    assert held["in_transaction"] is False
    assert [m["content"] for m in _messages(client, thread)["messages"]] == [
        "Still there after a reload?", "Answered after the reload."]
    assert _messages(client, thread)["turn"] is None


def test_a_waited_turn_carries_its_turn_id(client, monkeypatch):
    _stub(monkeypatch, lambda *_a, **_k: _reply("Done."))
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "Quick one"})
    assert said.status_code == 200, said.text
    turn_id = said.json()["turn_id"]
    assert turn_id and _turn(client, turn_id)["status"] == "done"


def test_another_actors_turn_is_not_readable(client, monkeypatch):
    _stub(monkeypatch, lambda *_a, **_k: _reply("Mine."))
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "private"})
    turn_id = said.json()["turn_id"]
    other = client.get(f"/v1/agent/turns/{turn_id}",
                       params={"project_id": "northpeak-bank-b", "actor": ACTOR})
    assert other.status_code in (403, 404)


# ── review round: a swept turn that finishes, what a failed turn already did, replays ─────────


def test_a_turn_swept_while_working_then_finishing_is_a_labelled_late_answer(client, engine,
                                                                          monkeypatch):
    release = threading.Event()

    def slow(*_a, **_k):
        assert release.wait(20)
        return _reply("Here is what I found.")

    _stub(monkeypatch, slow)
    thread = _ensure(client, "")
    turn_id = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
        "message": "A long one", "background": True}).json()["turn_id"]
    with engine.begin() as c:
        c.execute(text("UPDATE agent_chat_turns SET heartbeat_at = now() - INTERVAL '1 hour' "
                       "WHERE id = :t"), {"t": turn_id})
    assert _turn(client, turn_id)["status"] == "failed"          # swept
    release.set()
    deadline = time.monotonic() + 20
    contents: list = []
    while time.monotonic() < deadline:
        contents = [m["content"] for m in _messages(client, thread)["messages"]]
        if len(contents) == 3:
            break
        time.sleep(0.1)
    assert contents[0] == "A long one"
    assert contents[1].startswith("That turn did not finish.")
    assert contents[2].startswith("**Late answer.**")
    assert contents[2].endswith("Here is what I found.")
    assert _turn(client, turn_id)["status"] == "failed"          # never flips back to done


def test_the_heartbeat_moves_while_a_tool_blocks(client, engine, monkeypatch):
    from boss_engine.services import chat_turns

    monkeypatch.setattr(chat_turns, "HEARTBEAT_SECONDS", 0.2)
    release = threading.Event()

    def blocks(*_a, **_k):
        assert release.wait(20)
        return _reply("ok")

    _stub(monkeypatch, blocks)
    thread = _ensure(client, "")
    turn_id = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
        "message": "block", "background": True}).json()["turn_id"]
    with engine.begin() as c:
        c.execute(text("UPDATE agent_chat_turns SET heartbeat_at = now() - INTERVAL '14 minutes' "
                       "WHERE id = :t"), {"t": turn_id})
    time.sleep(1.0)
    with engine.begin() as c:
        fresh = c.execute(text("SELECT heartbeat_at > now() - INTERVAL '1 minute' "
                               "FROM agent_chat_turns WHERE id = :t"), {"t": turn_id}).scalar()
    release.set()
    assert fresh is True
    assert _wait_for(client, turn_id)["status"] == "done"


def _writes_then_dies(engine, *, checkpoint: bool):
    from apps.api.app.config import get_settings
    from boss_engine.context import EngineContext
    from boss_engine.services import chat_turns
    from boss_engine.tools import build_default_registry

    def route(*_a, project_id, actor, **_k):
        ctx = EngineContext(settings=get_settings(), engine=engine)
        made = build_default_registry().invoke(
            "remember", ctx, project_id=project_id, actor=actor,
            args={"kind": "project", "body": "the nightly job runs at 02:00"})
        assert made.get("ok") is not False, made
        if checkpoint:
            chat_turns.checkpoint()            # the next model call's commit
        raise RuntimeError("the model connection dropped")

    return route


def test_a_failed_turn_names_what_it_already_did(client, engine, monkeypatch):
    writes_then_dies = _writes_then_dies(engine, checkpoint=True)

    _stub(monkeypatch, writes_then_dies)
    thread = _ensure(client, "")
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread, "message": "Remember this"})
    assert said.status_code == 500
    line = _messages(client, thread)["messages"][-1]["content"]
    assert "already done this, and it stands" in line
    assert "- remember" in line
    assert "ask again only for what is not listed" in line
    assert "Nothing was changed" not in line


def test_the_same_idempotency_key_is_the_same_turn(client, monkeypatch):
    _stub(monkeypatch, lambda *_a, **_k: _reply("Once."))
    thread = _ensure(client, "")
    body = {"project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
            "message": "only once", "idempotency_key": "press-1"}
    first = client.post("/v1/engine/agent/chat", json=body)
    assert first.status_code == 200, first.text
    again = client.post("/v1/engine/agent/chat", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["turn_id"] == first.json()["turn_id"]
    replay = client.post("/v1/engine/agent/chat", json={**body, "background": True})
    # Finished: the replay is that turn's reply, never an empty "running".
    assert replay.status_code == 200 and replay.json()["turn_id"] == first.json()["turn_id"]
    assert replay.json()["reply"] == "Once."
    assert [m["content"] for m in _messages(client, thread)["messages"]] == ["only once", "Once."]


def test_start_up_fails_the_turns_of_a_dead_process_at_once(client, engine):
    import os
    import socket

    from boss_engine.services import chat_turns

    thread = _ensure(client, "")
    with engine.begin() as c:
        c.execute(text("INSERT INTO agent_messages (id, thread_id, role, content) "
                       "VALUES ('amsg_dead', :t, 'user', 'asked of a dead worker')"), {"t": thread})
        dead = chat_turns.open_row(c, project_id=PROJECT, thread_id=thread, actor=ACTOR,
                                   agent_role="", user_message_id="amsg_dead")
        mine = chat_turns.open_row(c, project_id=PROJECT, thread_id=thread, actor=ACTOR,
                                   agent_role="", user_message_id="amsg_dead")
        # The dead process had our pid (a restarted container reuses it) and its own token.
        c.execute(text("UPDATE agent_chat_turns SET owner = :o WHERE id = :t"),
                  {"o": f"{socket.gethostname()}:{os.getpid()}:deadbeef0000", "t": dead})
        failed = chat_turns.sweep_dead_owners(c)
    assert failed == [dead]
    assert _turn(client, dead)["status"] == "failed"
    assert _turn(client, mine)["status"] == "running"


def test_a_write_the_turn_never_committed_is_not_claimed_done(client, engine, monkeypatch):
    _stub(monkeypatch, _writes_then_dies(engine, checkpoint=False))
    thread = _ensure(client, "")
    said = client.post("/v1/engine/agent/chat", json={
        "project_id": PROJECT, "actor": ACTOR, "thread_id": thread, "message": "Remember this"})
    assert said.status_code == 500
    line = _messages(client, thread)["messages"][-1]["content"]
    assert "already done this, and it stands" not in line
    assert "may or may not have been saved" in line and "remember" in line
    with engine.begin() as c:
        [entry] = c.execute(text("SELECT actions FROM agent_chat_turns WHERE thread_id = :t"),
                            {"t": thread}).scalar()
    assert entry["tool"] == "remember" and entry["state"] == "pending"


def test_a_resend_of_a_finished_turn_returns_its_reply(client, monkeypatch):
    _stub(monkeypatch, lambda *_a, **_k: _reply("Finished."))
    thread = _ensure(client, "")
    body = {"project_id": PROJECT, "actor": ACTOR, "thread_id": thread,
            "message": "once", "idempotency_key": "press-done", "background": True}
    turn_id = client.post("/v1/engine/agent/chat", json=body).json()["turn_id"]
    assert _wait_for(client, turn_id)["status"] == "done"
    again = client.post("/v1/engine/agent/chat", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["turn_id"] == turn_id and again.json()["reply"] == "Finished."


def test_a_database_from_the_first_cut_upgrades(engine):
    """A database that applied adcacdc42 (agent_chat_turns without actions/client_key/owner)
    gains them from the current schema."""
    import re
    import subprocess
    import uuid
    from pathlib import Path

    from sqlalchemy import create_engine

    root = Path(__file__).resolve().parents[3]
    old = subprocess.run(["git", "-C", str(root), "show", "adcacdc42:db/schema.sql"],
                         capture_output=True, text=True, check=True).stdout
    new = (root / "db/schema.sql").read_text()

    def turns_block(sql: str) -> str:
        start = sql.index("CREATE TABLE IF NOT EXISTS agent_chat_turns")
        end = sql.index("-- BOSS debugging the way an engineer debugs", start)
        return re.sub(r"REFERENCES projects\(id\) ON DELETE CASCADE", "", sql[start:end])

    name = f"turns_up_{uuid.uuid4().hex[:8]}"
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{name}"'))
    scoped = create_engine(engine.url, connect_args={"options": f"-csearch_path={name}"})
    try:
        with scoped.begin() as c:
            c.exec_driver_sql(turns_block(old))
        with scoped.begin() as c:
            c.exec_driver_sql(turns_block(new))
            cols = set(c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'agent_chat_turns'"),
                {"s": name}).scalars())
            idx = c.execute(text("SELECT 1 FROM pg_indexes WHERE schemaname = :s AND "
                                 "indexname = 'idx_agent_chat_turns_client_key'"),
                            {"s": name}).scalar()
        assert {"actions", "client_key", "owner"} <= cols
        assert idx == 1
    finally:
        scoped.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()
