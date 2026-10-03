"""Talking with agents, end to end through the real API (spec §0.2 A, stream S-converse).

A 1:1 talk, "plan it with <agent>" ending in a card that starts a run with one press, and a
room of three SMEs with an @mention, a debate round and a summary whose next action starts a
run. The model is a scripted stand-in (`agent_converse.make_adapter`), the same seam idea as
`agent_loop.make_chat_adapter` in test_who_runs_a_step.py; everything else is real: Postgres,
the start door, readiness, the budget, receipts and the act rows.
"""
from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"
ACTOR = "ceo"
SEATS = ["pm", "qa", "security_reviewer"]


# ── The stand-in model ───────────────────────────────────────────────────────────────────────


class Model:
    """Answers in a voice keyed off WHO the system prompt says it is, and records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_for: set[str] = set()
        self.summary: dict | None = None
        self.proposal: dict | None = None

    @staticmethod
    def who(system_prompt: str) -> str:
        found = re.search(r"You are (?:\w+, )?the ([^,.\n]+)", system_prompt or "")
        return found.group(1).strip() if found else "someone"

    def create_text_response(self, prompt, system_prompt=None, model=None):
        who = self.who(system_prompt or "")
        self.calls.append({"who": who, "system": system_prompt or "", "prompt": prompt})
        if who in self.fail_for:
            raise RuntimeError("upstream exploded: 500 internal error")
        usage = {"model": "stub-1", "prompt_tokens": 120, "completion_tokens": 40,
                 "total_tokens": 160, "cost_usd_micros": 1500}
        if "Turn this conversation into ONE concrete start" in prompt:
            return "r", json.dumps(self.proposal), usage
        if "close the discussion" in prompt:
            return "r", json.dumps(self.summary), usage
        return "r", f"{who} speaking: considered view number {len(self.calls)}.", usage


@pytest.fixture()
def model(monkeypatch):
    from boss_engine.services import agent_converse

    stub = Model()
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: stub)
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 0)
    return stub


@pytest.fixture()
def seated(client, engine):
    """The three seats exist and are active on the project."""
    have = {r["role_key"] for r in client.get(
        "/v1/roles", params={"project_id": PROJECT}).json()["items"]}
    for key in SEATS:
        if key not in have:
            made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": key,
                                                  "display_name": key.replace("_", " ").title()})
            assert made.status_code == 201, made.text
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET lifecycle = 'active' WHERE project_id = :p"),
                  {"p": PROJECT})
    return SEATS


def _events(response) -> list[dict]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append({"type": lines["event"], **json.loads(lines["data"])})
    return out


def _open(client, kind, seats, **extra):
    made = client.post("/v1/conversations", json={"project_id": PROJECT, "actor": ACTOR,
                                                  "kind": kind, "seats": seats, **extra})
    assert made.status_code == 201, made.text
    return made.json()


def _say(client, conversation_id, message, **extra):
    return _events(client.post(f"/v1/conversations/{conversation_id}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": message, **extra}))


def _acts(engine, conversation_id):
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT * FROM agent_acts WHERE thread_id = :c ORDER BY started_at, act_id"),
            {"c": conversation_id}).mappings()]


def _workflow(client, name: str) -> str:
    """A product workflow with declared inputs: an agent step the project has a model for only
    if one is connected (so Start's readiness check has something real to say)."""
    made = client.post("/v1/delivery-workflows/create",
                       json={"project_id": PROJECT, "name": name, "actor": ACTOR})
    assert made.status_code == 200, made.text
    definition_id = made.json()["definition"]["id"]
    saved = client.post("/v1/delivery-workflows/save", json={
        "project_id": PROJECT, "definition_id": definition_id, "actor": ACTOR,
        "spec": {"entry_step": "work",
                 "input_schema": {"required": ["instructions"],
                                  "properties": {"instructions": {"title": "What to do"},
                                                 "audience": {"title": "Who it is for"}}},
                 "steps": [{"id": "work", "title": "Do the work", "kind": "capability",
                            "next": [], "approval": {"mode": "auto"}, "captain": "qa",
                            "binding": {"agent_work": True, "inputs": {
                                "instructions": {"from_input": "instructions"}}}}]}})
    assert saved.status_code == 200, saved.text
    listed = client.get("/v1/delivery-workflows", params={"project_id": PROJECT}).json()
    return next(w["workflow_key"] for w in listed["items"] if w["id"] == definition_id)


# ── 1:1 ──────────────────────────────────────────────────────────────────────────────────────


def test_a_talk_greets_in_character_streams_and_signs_every_reply(client, engine, model, seated):
    conv = _open(client, "talk", ["qa"], context={"from": "passport"})
    assert conv["seats"] == ["qa"] and conv["status"] == "open" and conv["turns"] == []
    assert conv["seat_info"][0]["role_key"] == "qa"

    hello = _say(client, conv["conversation_id"], "")
    assert [e["type"] for e in hello][:2] == ["turn_start", "token"]
    assert [e["type"] for e in hello][-2:] == ["turn_end", "done"]
    start = hello[0]
    assert (start["role_key"], start["speaker_kind"]) == ("qa", "agent") and start["act_id"]
    said = "".join(e["text"] for e in hello if e["type"] == "token")
    assert said.startswith("QA Lead speaking")
    # The greeting is generated through the composed prompt, never canned, and the prompt
    # always carries the fixed disclosure (§0.1.3).
    from boss_engine.services.agent_world_types import AI_DISCLOSURE

    assert AI_DISCLOSURE in model.calls[0]["system"]
    assert "Greet them" in model.calls[0]["prompt"]

    reply = _say(client, conv["conversation_id"], "What would you test first?")
    end = next(e for e in reply if e["type"] == "turn_end")
    assert end["status"] == "complete" and end["receipt_id"] and end["tokens_out"] == 40
    # The second reply saw the first exchange.
    assert "QA Lead speaking" in model.calls[1]["prompt"]
    assert "What would you test first?" in model.calls[1]["prompt"]

    full = client.get(f"/v1/conversations/{conv['conversation_id']}",
                      params={"project_id": PROJECT, "actor": ACTOR}).json()
    kinds = [(t["speaker_kind"], t["status"]) for t in full["turns"]]
    assert kinds == [("agent", "complete"), ("person", "complete"), ("agent", "complete")]
    assert full["turns"][2]["content"].startswith("QA Lead speaking")

    acts = _acts(engine, conv["conversation_id"])
    assert len(acts) == 2
    for act in acts:
        assert (act["kind"], act["how"], act["handed_by_kind"], act["handed_by_id"]) == (
            "chat", "asked_by_person", "person", ACTOR)
        assert (act["subject_kind"], act["subject_id"]) == ("conversation",
                                                             conv["conversation_id"])
        assert act["status"] == "complete" and act["role_key"] == "qa"
        assert act["tokens_out"] == 40
    # A conversation is one chain of work: the second reply hangs off the first.
    assert acts[1]["parent_act_id"] == acts[0]["act_id"]
    assert acts[1]["root_act_id"] == acts[0]["root_act_id"] == acts[0]["act_id"]
    # Never asked_by_agent from a conversation.
    assert not [a for a in acts if a["how"] == "asked_by_agent"]
    with engine.begin() as c:
        receipts = c.execute(text("SELECT count(*) FROM receipts WHERE effect_kind = "
                                  "'model.converse_turn' AND dedupe_key LIKE 'converse:%'")).scalar()
        consumed = c.execute(text("SELECT count(*) FROM cost_receipts WHERE category = "
                                  "'agent_conversation'")).scalar()
    assert receipts >= 2 and consumed >= 2


def test_a_seat_that_fails_is_explained_and_the_room_moves_on(client, engine, model, seated):
    model.fail_for = {"QA Lead"}
    conv = _open(client, "room", SEATS)
    events = _say(client, conv["conversation_id"], "Is the rollout plan safe?")
    starts = [e["role_key"] for e in events if e["type"] == "turn_start"]
    assert starts == SEATS
    error = next(e for e in events if e["type"] == "error")
    assert error["error"]["headline"] and error["error"]["action"]
    assert error["turn_id"]
    ends = {e["turn_id"]: e["status"] for e in events if e["type"] == "turn_end"}
    assert sorted(ends.values()) == ["complete", "complete", "error"]
    assert events[-1]["type"] == "done"
    failed = next(a for a in _acts(engine, conv["conversation_id"]) if a["role_key"] == "qa")
    assert failed["status"] == "error"


# ── Plan → start ─────────────────────────────────────────────────────────────────────────────


def test_plan_it_with_an_agent_ends_in_a_card_one_press_starts(client, engine, model, seated):
    workflow_key = _workflow(client, "Talked into it")
    conv = _open(client, "plan", ["pm"], context={"from": "palette"})
    assert conv["title"] == "Plan with " + conv["seat_info"][0]["name"]

    greeting = _say(client, conv["conversation_id"], "")
    assert "ask the ONE question" in model.calls[0]["prompt"]
    assert greeting[-1]["type"] == "done"
    _say(client, conv["conversation_id"], "A lead triage page for the support team.")
    # The plan's title becomes what it is about.
    assert client.get(f"/v1/conversations/{conv['conversation_id']}",
                      params={"project_id": PROJECT, "actor": ACTOR}
                      ).json()["title"].startswith("A lead triage page")

    model.proposal = {
        "message": "Here is how I would start it.", "workflow_key": workflow_key,
        "title": "Lead triage page", "directive": "Build a lead triage page.",
        "inputs": {"instructions": "Build a lead triage page.", "audience": "support",
                   "adopt_discovery_run_id": "smuggled", "not_declared": "x"},
        "acceptance_criteria": ["Leads sort by score"], "why": "It is a build.",
        "agents": {"work": "security_reviewer"}}
    proposed = _events(client.post(f"/v1/conversations/{conv['conversation_id']}/propose",
                                   json={"project_id": PROJECT, "actor": ACTOR}))
    card = next(e for e in proposed if e["type"] == "proposal")["proposal"]
    assert card["workflow_key"] == workflow_key and card["status"] == "proposed"
    # Only the inputs the workflow declares; nothing that asserts work already happened.
    assert card["inputs"] == {"instructions": "Build a lead triage page.", "audience": "support"}
    # The card says who will really run each step: the workflow names qa, so qa it is.
    assert card["agents"] == [{"step_id": "work", "step_label": "Do the work", "role_key": "qa"}]
    # No model is connected to this project, and Start will say so: the card already does.
    assert card["readiness"]["ok"] is False and card["readiness"]["problems"]
    said = "".join(e["text"] for e in proposed if e["type"] == "token")
    assert said == "Here is how I would start it."

    start = client.post(f"/v1/conversations/{conv['conversation_id']}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": card["proposal_id"]})
    # The composer's readiness refusal, passed through unchanged.
    assert start.status_code == 409, start.text
    assert start.json()["detail"]["readiness"]["missing"][0]["key"] == "model"

    started = client.post(f"/v1/conversations/{conv['conversation_id']}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": card["proposal_id"],
        "start_anyway": ["model"], "inputs": {"audience": "support leads"}})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    assert run_id.startswith("wfr_")
    after = started.json()["conversation"]
    assert (after["status"], after["started_run_id"]) == ("started", run_id)
    proposal_turn = next(t for t in after["turns"] if t["kind"] == "proposal")
    assert proposal_turn["payload"]["status"] == "started"
    with engine.begin() as c:
        run_input = c.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                              {"r": run_id}).scalar()
    assert run_input["instructions"] == "Build a lead triage page."
    assert run_input["audience"] == "support leads"
    assert "adopt_discovery_run_id" not in run_input
    # One card, one run: pressing again answers with the same run.
    again = client.post(f"/v1/conversations/{conv['conversation_id']}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": card["proposal_id"],
        "start_anyway": ["model"]})
    assert again.status_code == 200 and again.json()["run_id"] == run_id


def test_a_new_proposal_supersedes_the_last_and_a_card_can_be_dismissed(
        client, model, seated):
    workflow_key = _workflow(client, "Twice proposed")
    conv = _open(client, "plan", ["pm"])
    _say(client, conv["conversation_id"], "A changelog page.")
    model.proposal = {"workflow_key": workflow_key, "title": "Changelog",
                      "directive": "Build a changelog page.", "inputs": {}, "why": "Build."}
    cid = conv["conversation_id"]
    first = next(e for e in _events(client.post(f"/v1/conversations/{cid}/propose", json={
        "project_id": PROJECT, "actor": ACTOR})) if e["type"] == "proposal")["proposal"]
    second = next(e for e in _events(client.post(f"/v1/conversations/{cid}/propose", json={
        "project_id": PROJECT, "actor": ACTOR})) if e["type"] == "proposal")["proposal"]
    turns = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()["turns"]
    status = {t["payload"]["proposal_id"]: t["payload"]["status"]
              for t in turns if t["kind"] == "proposal"}
    assert status == {first["proposal_id"]: "superseded", second["proposal_id"]: "proposed"}
    stale = client.post(f"/v1/conversations/{cid}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": first["proposal_id"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "stale"
    dismissed = client.post(f"/v1/conversations/{cid}/proposals/{second['proposal_id']}/dismiss",
                            json={"project_id": PROJECT, "actor": ACTOR})
    assert dismissed.status_code == 200
    assert next(t for t in dismissed.json()["turns"] if t["kind"] == "proposal"
                and t["payload"]["proposal_id"] == second["proposal_id"]
                )["payload"]["status"] == "dismissed"


# ── The room ─────────────────────────────────────────────────────────────────────────────────


def test_a_room_of_three_smes_takes_turns_answers_a_mention_debates_and_sums_up(
        client, engine, model, seated):
    workflow_key = _workflow(client, "Room start")
    conv = _open(client, "room", SEATS)
    cid = conv["conversation_id"]
    assert conv["lead_role_key"] == "pm"

    # Everyone, in seat order; each seat sees the round so far.
    first = _say(client, cid, "Should we ship the new login flow this week?")
    assert [e["role_key"] for e in first if e["type"] == "turn_start"] == SEATS
    assert [e["type"] for e in first].count("turn_end") == 3
    assert "Product Manager speaking" in model.calls[1]["prompt"]       # qa saw pm
    assert "QA Lead speaking" in model.calls[2]["prompt"]               # security saw qa
    assert "seated in a room with" in model.calls[2]["prompt"]
    acts = _acts(engine, cid)
    assert [(a["kind"], a["how"]) for a in acts] == [("seat", "seated")] * 3

    # @mention: only that seat answers, and it was asked, not seated.
    before = len(model.calls)
    mention = _say(client, cid, "@security_reviewer what about session fixation?")
    assert [e["role_key"] for e in mention if e["type"] == "turn_start"] == [
        "security_reviewer"]
    assert len(model.calls) == before + 1
    assert "addressed you directly" in model.calls[-1]["prompt"]
    assert _acts(engine, cid)[-1]["how"] == "asked_by_person"
    person = [t for t in client.get(f"/v1/conversations/{cid}", params={
        "project_id": PROJECT, "actor": ACTOR}).json()["turns"] if t["speaker_kind"] == "person"]
    assert person[-1]["addressed_to"] == ["security_reviewer"]

    # rounds=2: each seat answers another seat's round-one point.
    debate = _say(client, cid, "Final positions?", to=["pm", "qa"], rounds=2)
    started = [e for e in debate if e["type"] == "turn_start"]
    assert [e["role_key"] for e in started] == ["pm", "qa", "pm", "qa"]
    round_one = {e["role_key"]: e["turn_id"] for e in started[:2]}
    assert started[2]["reply_to_turn_id"] == round_one["qa"]
    assert started[3]["reply_to_turn_id"] == round_one["pm"]
    debate_acts = {a["act_id"]: a for a in _acts(engine, cid)}
    answer = debate_acts[started[3]["act_id"]]
    assert answer["parent_act_id"] == started[0]["act_id"]

    # The lead sums it up; a next action carries a start card, another opens a decision.
    model.summary = {
        "text": "Ship behind a flag after the fixation fix.",
        "decisions": [{"text": "Ship behind a flag", "for": ["pm", "qa"],
                       "against": ["security_reviewer", "not_a_seat"]}],
        "next_actions": [
            {"text": "Fix session fixation", "owner_role_key": "security_reviewer",
             "kind": "start", "proposal": {
                 "workflow_key": workflow_key, "title": "Session fix",
                 "directive": "Rotate the session id on login.",
                 "inputs": {"instructions": "Rotate the session id on login."},
                 "why": "A build."}},
            {"text": "Choose the rollout date", "owner_role_key": "pm", "kind": "decision"},
            {"text": "Start a workflow we do not have", "kind": "start",
             "proposal": {"workflow_key": "nope", "directive": "x"}}]}
    summed = _events(client.post(f"/v1/conversations/{cid}/summary",
                                 json={"project_id": PROJECT, "actor": ACTOR}))
    summary = next(e for e in summed if e["type"] == "summary")["summary"]
    assert summary["by_role_key"] == "pm"
    assert summary["decisions"][0]["against"] == ["security_reviewer"]
    kinds = [a["kind"] for a in summary["next_actions"]]
    assert kinds == ["start", "decision", "note"]       # an unrunnable start is said as a note
    card = summary["next_actions"][0]["proposal"]
    after = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()
    assert after["status"] == "summarized" and after["summary"]["text"] == summary["text"]

    started_run = client.post(f"/v1/conversations/{cid}/start", json={
        "project_id": PROJECT, "actor": ACTOR, "proposal_id": card["proposal_id"],
        "start_anyway": ["model"]})
    assert started_run.status_code == 200, started_run.text
    assert started_run.json()["conversation"]["summary"]["next_actions"][0]["proposal"][
        "status"] == "started"

    decided = client.post(f"/v1/conversations/{cid}/decisions",
                          json={"project_id": PROJECT, "actor": ACTOR, "index": 1})
    assert decided.status_code == 200, decided.text
    decision_id = decided.json()["decision_id"]
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decisions WHERE id = :d"),
                         {"d": decision_id}).scalar() == 1
    # Opening it twice opens it once.
    assert client.post(f"/v1/conversations/{cid}/decisions", json={
        "project_id": PROJECT, "actor": ACTOR, "index": 1}).json()["decision_id"] == decision_id


# ── Refusals ─────────────────────────────────────────────────────────────────────────────────


def test_refusals_say_what_to_do(client, engine, model, seated, monkeypatch):
    from boss_engine.services import agent_converse, budget

    bad = client.post("/v1/conversations", json={"project_id": PROJECT, "actor": ACTOR,
                                                 "kind": "room", "seats": ["pm"]})
    assert bad.status_code == 422 and "2 to 5" in bad.json()["detail"]["message"]
    ghost = client.post("/v1/conversations", json={"project_id": PROJECT, "actor": ACTOR,
                                                   "kind": "talk", "seats": ["nobody_here"]})
    assert ghost.status_code == 422
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET lifecycle = 'retired' WHERE project_id = :p "
                       "AND role_key = 'qa'"), {"p": PROJECT})
    retired = client.post("/v1/conversations", json={"project_id": PROJECT, "actor": ACTOR,
                                                     "kind": "talk", "seats": ["qa"]})
    assert retired.status_code == 422 and "retired" in retired.json()["detail"]["message"]
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET lifecycle = 'active' WHERE project_id = :p"),
                  {"p": PROJECT})

    conv = _open(client, "talk", ["qa"])
    cid = conv["conversation_id"]
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: False)
    refused = client.post(f"/v1/conversations/{cid}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "hi"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == {"code": "needs_model",
                                        "message": "Needs a model. Set one in Settings."}
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)

    monkeypatch.setattr(budget.BudgetGovernor, "quote",
                        lambda self, *_a, **_k: {"allowed": False})
    broke = client.post(f"/v1/conversations/{cid}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "hi"})
    assert broke.status_code == 409 and broke.json()["detail"]["code"] == "budget"
    monkeypatch.undo()
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: model)
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 0)

    # Nothing was said for the refused turns: the transcript holds no person turn.
    turns = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()["turns"]
    assert turns == []

    with engine.begin() as c:
        c.execute(text("INSERT INTO agent_conversation_turns (turn_id, conversation_id, seq, "
                       "speaker_kind, role_key, status) VALUES ('trn_busy', :c, 1, 'agent', "
                       "'qa', 'streaming')"), {"c": cid})
    busy = client.post(f"/v1/conversations/{cid}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "hi"})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"

    other = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": "some-other-project", "actor": ACTOR})
    assert other.status_code in (403, 404)
    missing = client.get("/v1/conversations/cnv_doesnotexist",
                         params={"project_id": PROJECT, "actor": ACTOR})
    assert missing.status_code == 404

    closed = client.post(f"/v1/conversations/{cid}/close",
                         json={"project_id": PROJECT, "actor": ACTOR})
    assert closed.status_code == 200 and closed.json()["status"] == "closed"


def test_presets_name_what_the_project_lacks_and_list_who_can_be_seated(client, seated):
    body = client.get("/v1/conversations/presets",
                      params={"project_id": PROJECT, "actor": ACTOR}).json()
    keys = [p["key"] for p in body["items"]]
    assert keys == ["product_eng_security", "architecture_review", "incident"]
    roles = {r["role_key"] for r in client.get(
        "/v1/roles", params={"project_id": PROJECT}).json()["items"]}
    for preset in body["items"]:
        assert preset["missing"] == [s for s in preset["seats"] if s not in roles]
    assert {"pm", "qa", "security_reviewer"} <= {a["role_key"] for a in body["agents"]}
    listed = client.get("/v1/conversations", params={"project_id": PROJECT, "actor": ACTOR})
    assert listed.status_code == 200 and isinstance(listed.json()["items"], list)
