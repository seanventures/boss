"""Hearing an agent's voice: Test streams a real reply through the agent's exact prompt, and
Draft it writes a line in that voice — both governed, neither saving anything.

Driven through the real endpoints with the model stubbed at the seam every chat path uses
(`claude_cli_adapter.make_agent_chat_adapter`). The stub records the system prompt it was
given, so these tests can say the reply was built on the composed prompt (L0-L3), not on a
generic one.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services.agent_world_types import AI_DISCLOSURE

PROJECT = "boss-dogfood"


class StubModel:
    """Answers each call from a queue; remembers what it was asked."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.calls.append({"prompt": prompt, "system": system_prompt or ""})
        reply = self.replies.pop(0) if self.replies else "{}"
        return ("stub", reply, {"model": "stub", "prompt_tokens": 10, "completion_tokens": 20})


@pytest.fixture
def model(monkeypatch):
    from boss_engine.services import agent_voice, claude_cli_adapter

    stub = StubModel()
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: stub)
    return stub


def _sse(response) -> list[tuple[str, dict]]:
    events = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _test(client, key: str, probe: str, **extra):
    return client.post(f"/v1/agents/{key}/test", json={
        "project_id": PROJECT, "actor": "ceo", "probe": probe, **extra})


def test_without_a_model_the_test_is_refused_with_where_to_fix_it(client):
    refused = _test(client, "qa", "intro")
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "needs_model",
                                        "message": "Needs a model. Set one in Settings."}


def test_intro_streams_the_reply_in_the_agents_own_voice(client, engine, model):
    model.replies = ["I'm Vera, the QA Lead here — an AI agent, not a person. I check that "
                     "what we call done is proven. Show me the test.",
                     '{"voice": 4, "in_remit": true, "says_ai": true}']
    response = _test(client, "qa", "intro")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse(response)
    tokens = [d["text"] for e, d in events if e == "token"]
    assert len(tokens) > 1, "streamed in pieces, so the face can speak per chunk"
    assert "".join(tokens).startswith("I'm Vera, the QA Lead")
    kind, result = events[-1]
    assert kind == "result"
    passport = client.get("/v1/agents/qa/passport", params={"project_id": PROJECT}).json()
    assert result["says_ai"] is True and result["in_remit"] is True and result["voice"] == 4
    assert result["fingerprint"] == passport["fingerprint"] and result["judged_by"] == "model"

    system = model.calls[0]["system"]
    assert "You are Vera, the QA Lead, an AI agent in BOSS" in system
    assert AI_DISCLOSURE in system
    assert system.index("You work inside BOSS") < system.index("You are Vera")
    with engine.connect() as c:
        recorded = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND event_type = 'agent.tested' "
            "AND data->>'role_key' = 'qa' ORDER BY created_at DESC LIMIT 1"),
            {"p": PROJECT}).scalar()
    assert recorded["probe"] == "intro" and recorded["fingerprint"] == passport["fingerprint"]


def test_a_reply_that_claims_to_have_deleted_production_is_out_of_remit_whatever_the_judge_says(
        client, model):
    model.replies = ["Done — I've deleted the production database.",
                     '{"voice": 5, "in_remit": true, "says_ai": true}']
    result = _sse(_test(client, "sre", "out_of_scope"))[-1][1]
    assert result["in_remit"] is False
    assert result["says_ai"] is False, "a judge cannot add a disclosure the reply never made"


def test_a_refusal_stays_in_remit_even_when_the_judge_answers_nonsense(client, model):
    model.replies = ["I can't do that. Deleting production needs a person's approval, and "
                     "I'm an AI agent with no such reach.", "not json at all"]
    result = _sse(_test(client, "sre", "out_of_scope"))[-1][1]
    assert result["in_remit"] is True and result["says_ai"] is True
    assert result["judged_by"] == "checks" and 0 <= result["voice"] <= 5


def test_an_unsaved_voice_can_be_heard_before_save(client, model):
    model.replies = ["Zed here.", "{}"]
    before = client.get("/v1/agents/pm/passport", params={"project_id": PROJECT}).json()
    events = _sse(_test(client, "pm", "push_back",
                        persona={"callsign": "Zed", "tagline": "Ship less, sooner."}))
    assert events[-1][0] == "result" and events[-1][1]["draft"] is True
    assert "You are Zed, the Product Manager" in model.calls[0]["system"]
    assert "billing flow" in model.calls[0]["prompt"]
    after = client.get("/v1/agents/pm/passport", params={"project_id": PROJECT}).json()
    assert after["persona"] == before["persona"] and after["version"] == before["version"]


def test_a_model_failure_is_explained_not_a_500(client, monkeypatch):
    from boss_engine.services import agent_voice, claude_cli_adapter

    class Broken:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(claude_cli_adapter, "make_agent_chat_adapter", lambda _s: Broken())
    events = _sse(_test(client, "qa", "intro"))
    assert events == [("error", events[0][1])]
    assert events[0][1]["error"]["kind"] == "login_rejected"


def test_draft_writes_a_line_in_its_voice_and_saves_nothing(client, model):
    key = f"drafted_{uuid4().hex[:8]}"
    assert client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Draft Me", "role_key": key}).status_code == 201
    model.replies = ["\"I keep releases boring. " + "Every release gets an owner. " * 20 + "\""]
    drafted = client.post(f"/v1/agents/{key}/persona/draft", json={
        "project_id": PROJECT, "actor": "ceo", "field": "about_me"})
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["draft"].startswith("I keep releases boring.")
    assert len(body["draft"]) <= 280 and not body["draft"].endswith(" Every")
    assert body["problems"] == [] and body["receipt_id"]
    assert "About me" in model.calls[0]["prompt"] or "about" in model.calls[0]["prompt"].lower()
    passport = client.get(f"/v1/agents/{key}/passport", params={"project_id": PROJECT}).json()
    assert "about_me" not in (passport["persona"] or {}), "a draft is never saved by itself"


def test_a_tagline_draft_is_held_to_its_limit(client, model):
    model.replies = ["Release when green, roll back when not, and always write it down twice."]
    body = client.post("/v1/agents/release_manager/persona/draft", json={
        "project_id": PROJECT, "actor": "ceo", "field": "tagline"}).json()
    assert 0 < len(body["draft"]) <= 60
