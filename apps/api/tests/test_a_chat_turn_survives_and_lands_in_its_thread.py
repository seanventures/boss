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
