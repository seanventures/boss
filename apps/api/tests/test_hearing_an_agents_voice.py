"""Hearing an agent's voice: Test streams a real reply through the agent's exact prompt, and
Draft it writes a line in that voice — both governed, neither saving anything.

Driven through the real endpoints with the model stubbed at the seam every chat path uses
(`model_routing.project_chat_adapter`). The stub records the system prompt it was
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
    from boss_engine.services import agent_voice, model_routing

    stub = StubModel()
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: stub)
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
    from boss_engine.services import agent_voice, model_routing

    class Broken:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: Broken())
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


# ── A voice call is spend the budget sees ───────────────────────────────────────────────────


class PricedModel(StubModel):
    """Each call reports what it cost, the way the Claude CLI does."""

    def __init__(self, *replies: str, cost: int = 1234):
        super().__init__(*replies)
        self.cost = cost

    def create_text_response(self, prompt, system_prompt=None, model=None):
        tag, reply, usage = super().create_text_response(prompt, system_prompt, model)
        return tag, reply, {**usage, "cost_usd_micros": self.cost}


@pytest.fixture
def priced(monkeypatch):
    from boss_engine.services import agent_voice, model_routing

    stub = PricedModel()
    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: stub)
    return stub


def _spend(engine, receipt_id: str) -> dict:
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT r.status AS receipt_status, r.effect_kind, c.actual_usd_micros, "
            "c.reservation_id, b.status AS hold FROM receipts r "
            "LEFT JOIN cost_receipts c ON c.receipt_id = r.id "
            "LEFT JOIN budget_reservations b ON b.id = c.reservation_id "
            "WHERE r.id = :r"), {"r": receipt_id}).mappings().first()
    return dict(row or {})


def test_a_test_settles_both_its_calls_against_a_real_receipt(client, engine, priced):
    priced.replies = ["I'm Vera, an AI agent. Show me the test.",
                      '{"voice": 4, "in_remit": true, "says_ai": true}']
    result = _sse(_test(client, "qa", "intro"))[-1][1]
    assert result["receipt_id"].startswith("rcpt"), "the receipt, not the reservation"
    spent = _spend(engine, result["receipt_id"])
    assert spent["receipt_status"] == "succeeded"
    assert spent["effect_kind"] == "model.agent_voice_test"
    assert spent["actual_usd_micros"] == 2 * 1234, "the reply AND the judge are paid for"
    assert spent["hold"] == "consumed"


def test_a_draft_settles_against_a_real_receipt(client, engine, priced):
    priced.replies = ["Release when green."]
    body = client.post("/v1/agents/release_manager/persona/draft", json={
        "project_id": PROJECT, "actor": "ceo", "field": "tagline"}).json()
    spent = _spend(engine, body["receipt_id"])
    assert spent["receipt_status"] == "succeeded" and spent["actual_usd_micros"] == 1234
    assert spent["hold"] == "consumed"


def test_a_failed_draft_releases_its_hold_and_records_the_failure(client, engine, monkeypatch):
    from boss_engine.services import agent_voice, model_routing

    class Broken:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(agent_voice, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: Broken())
    assert client.post("/v1/agents/qa/persona/draft", json={
        "project_id": PROJECT, "actor": "ceo", "field": "tagline"}).status_code == 502
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT status FROM budget_reservations WHERE project_id = :p "
            "AND reason = 'Draft tagline for qa' ORDER BY created_at DESC LIMIT 1"),
            {"p": PROJECT}).scalar()
    assert row == "released"


def test_a_client_that_drops_mid_stream_still_pays_for_what_it_heard(client, engine, priced,
                                                                    monkeypatch):
    from apps.api.app import main as api_main

    captured = {}

    def keep(content, **kwargs):
        captured["stream"] = content
        return content

    monkeypatch.setattr(api_main, "StreamingResponse", keep)
    priced.replies = ["I'm Vera, an AI agent, and I check that what we call done is "
                      "proven by a test someone can rerun.", "{}"]
    stream = api_main.agent_voice_test_endpoint("qa", api_main.AgentVoiceTestBody(
        project_id=PROJECT, actor="ceo", probe="intro"))
    first = next(stream)
    assert first.startswith("event: token")
    stream.close()  # what the server does when the client goes away
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT b.status, c.actual_usd_micros FROM budget_reservations b "
            "JOIN cost_receipts c ON c.reservation_id = b.id WHERE b.project_id = :p "
            "AND b.reason = 'Test intro for qa' ORDER BY b.created_at DESC LIMIT 1"),
            {"p": PROJECT}).first()
    assert row is not None, "the hold was left on the budget"
    assert row[0] == "consumed" and row[1] == 1234
