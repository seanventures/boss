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
from dataclasses import replace

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"
ACTOR = "ceo"
SEATS = ["pm", "qa", "security_reviewer"]


# ── The stand-in model ───────────────────────────────────────────────────────────────────────


class Model:
    from boss_engine.services.model_routing import ModelChoice
    _boss_choice = ModelChoice("ollama", "owned conversation fixture", "approved_local_model_host")

    """Answers in a voice keyed off WHO the system prompt says it is, and records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_for: set[str] = set()
        self.summary: dict | None = None
        self.proposal: dict | None = None
        self.billing: str | None = None  # the adapter's own stamp: 'subscription', 'api', ...

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
                 "total_tokens": 160, "cost_usd_micros": 1500,
                 **({"billing": self.billing} if self.billing else {})}
        if "Turn this conversation into ONE concrete start" in prompt:
            # The start card is the pipeline's decision: the workflow is the chosen action and
            # the card's draft is that action's arguments.
            card = dict(self.proposal or {})
            return "r", json.dumps({"action": card.pop("workflow_key", None), "confidence": 0.9,
                                    "reason": card.get("why") or "", "arguments": card}), usage
        if "close the discussion" in prompt:
            return "r", json.dumps(self.summary), usage
        return "r", f"{who} speaking: considered view number {len(self.calls)}.", usage


@pytest.fixture()
def model(monkeypatch, engine, client):
    from boss_engine.services import agent_converse

    stub = Model()
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: stub)
    monkeypatch.setattr(agent_converse, "model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 0)
    from apps.api.tests.identity_fixtures import IdentityWorld
    from apps.api.tests.test_agent_voice_stream_intent import connect_owned
    from boss_engine.identity import authz, lineage, request_memo
    from boss_engine.identity import P
    from boss_engine.identity.scope import ResolvedScope
    from boss_engine.services.conversation_spend import ConversationAuthority
    with engine.connect() as c:
        row = c.execute(text("SELECT p.workspace_id,w.org_id FROM projects p JOIN workspaces w "
                             "ON w.id=p.workspace_id WHERE p.id=:p"), {"p": PROJECT}).mappings().one()
    world = IdentityWorld(engine, row["org_id"], row["workspace_id"], PROJECT, PROJECT)
    owner = replace(world.person("owned conversation operator"), legacy_member_ids=(ACTOR,))
    world.bind(owner, "owner", project=PROJECT)
    ctx = world.ctx(owner)
    from boss_engine.identity import consent, passports
    from uuid import uuid4
    with engine.begin() as c:
        # These end-to-end fixtures now authenticate as a real person. Give the
        # workflow's actual agent a Passport and an explicit owned standing grant;
        # production consent resolution stays enabled and validates the match.
        passports.upgrade_from_roles(org_id=world.org, db=c, default_sponsor=owner.id)
        agents = c.execute(text("SELECT agent_id FROM roles WHERE project_id=:p AND role_key=ANY(:roles)"),
                           {"p": PROJECT, "roles": SEATS}).scalars().all()
        assert len(agents) == len(SEATS) and all(agents)
        grant_id = "dlg_converse_" + uuid4().hex
        c.execute(text("INSERT INTO identity_delegation_grants(id,org_id,user_principal_id,agent_ids,"
                       "project_ids,status,expires_at) VALUES(:g,:o,:u,:a,:p,'active',now()+interval '1 day')"),
                  {"g":grant_id,"o":world.org,"u":owner.id,"a":agents,"p":[PROJECT]})
        for permission in consent.WORKFLOW_BASE_PERMISSIONS:
            c.execute(text("INSERT INTO identity_delegation_grant_permissions(grant_id,permission_key) "
                           "VALUES(:g,:k)"), {"g":grant_id,"k":permission})
    connect_owned(engine, monkeypatch, PROJECT, paid=True)
    def check():
        request_memo.clear()
        assert authz.check(ctx, P.MEETING_CONVENE, ResolvedScope.in_project(world.org, PROJECT)).allowed
    authority = ConversationAuthority(owner.id, lineage.Acting(owner.id, world.org), check)
    monkeypatch.setattr(engine, "_conversation_test_authority", authority, raising=False)
    with world.acting_as(ctx):
        yield stub


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
    preface = hello[0]
    assert preface["type"] == "intent"
    assert (preface["action"], preface["message"]) == ("turns", "")
    spoken = [event for event in hello if event["type"] != "intent"]
    assert [e["type"] for e in spoken][:2] == ["turn_start", "token"]
    assert [e["type"] for e in spoken][-2:] == ["turn_end", "done"]
    start = spoken[0]
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
    # A whole-response adapter emits each reply once, matching durable transcript bytes.
    assert said == full["turns"][0]["content"]
    assert "".join(e["text"] for e in reply if e["type"] == "token") == full["turns"][2]["content"]

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
    # One llm_calls row per reply, attributed to this project, the seat and its act — the
    # write happens a generator step after the call, on a fresh copy of the request context.
    with engine.begin() as c:
        calls = c.execute(text("SELECT project_id, role, stage, act_id FROM llm_calls "
                               "WHERE act_id = ANY(:a) ORDER BY act_id"),
                          {"a": [a["act_id"] for a in acts]}).all()
    assert sorted(tuple(r) for r in calls) == sorted(
        (PROJECT, "qa", "converse", a["act_id"]) for a in acts)
    with engine.begin() as c:
        receipts = c.execute(text("SELECT count(*) FROM receipts WHERE effect_kind = "
                                  "'model.invoke'")).scalar()
        consumed = c.execute(text("SELECT count(*) FROM cost_receipts WHERE category = "
                                  "'model_gateway'")).scalar()
    assert receipts >= 2 and consumed >= 2


@pytest.mark.parametrize("billing, money", [("subscription", 0), ("api", 1500)])
def test_a_talk_on_a_subscription_costs_no_money(client, engine, model, seated, billing, money):
    """A reply answered on a subscription is worth its list price but costs nothing: the turn,
    its act and the budget all say $0, and the receipt keeps the value as included. The same
    reply on a metered key is money everywhere."""
    model.billing = billing
    with engine.begin() as c:
        c.execute(text("UPDATE entitlement_sources SET entitlement_kind=:k,cost_mode=:k "
                       "WHERE project_id=:p AND source_id='approved_local_model_host'"),
                  {"p": PROJECT, "k": "subscription" if billing == "subscription" else "usage_based_api"})
    conv = _open(client, "talk", ["qa"], context={"from": "passport"})
    reply = _say(client, conv["conversation_id"], "")
    end = next(e for e in reply if e["type"] == "turn_end")
    assert end["status"] == "complete"
    assert end["cost_usd"] == money / 1_000_000

    full = client.get(f"/v1/conversations/{conv['conversation_id']}",
                      params={"project_id": PROJECT, "actor": ACTOR}).json()
    assert full["turns"][0]["cost_usd"] == money / 1_000_000
    (act,) = _acts(engine, conv["conversation_id"])
    assert float(act["cost_usd"]) == money / 1_000_000
    with engine.begin() as c:
        settled = c.execute(text(
            "SELECT actual_usd_micros, equivalent_usd_micros, included_in_subscription "
            "FROM cost_receipts WHERE receipt_id = :r"), {"r": end["receipt_id"]}).one()
        call = c.execute(text("SELECT cost_usd_micros, included_in_subscription FROM llm_calls "
                              "WHERE act_id = :a"), {"a": act["act_id"]}).one()
    included = billing == "subscription"
    assert tuple(settled) == ((0, 1500, True) if included else (1500, None, False))
    # The call's row keeps what it is worth, and says whether that was money.
    assert tuple(call) == (1500, included)


def test_a_conversation_is_named_by_the_agents_callsign(client, engine, seated):
    """"Talk with Vera", not "Talk with QA Lead": the default title names the agent the way the
    console's header does, and seat_info carries the callsign beside the role's name."""
    with engine.begin() as c:
        before = c.execute(text("SELECT spec FROM roles WHERE project_id = :p AND role_key = 'qa'"),
                           {"p": PROJECT}).scalar()
        c.execute(text("UPDATE roles SET spec = jsonb_set(COALESCE(spec::jsonb, '{}'::jsonb), "
                       "'{persona}', COALESCE(spec::jsonb->'persona', '{}'::jsonb) || "
                       "'{\"callsign\": \"Vera\"}'::jsonb) "
                       "WHERE project_id = :p AND role_key = 'qa'"), {"p": PROJECT})
    try:
        talk = _open(client, "talk", ["qa"])
        assert talk["title"] == "Talk with Vera"
        assert talk["seat_info"][0]["callsign"] == "Vera"
        assert talk["seat_info"][0]["name"] != "Vera"
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE roles SET spec = CAST(:s AS JSONB) "
                           "WHERE project_id = :p AND role_key = 'qa'"),
                      {"p": PROJECT, "s": json.dumps(before) if not isinstance(before, str)
                       else before})


def test_a_shipped_agent_with_no_stored_persona_has_one_name_in_every_place(
        client, engine, model, seated):
    """A shipped role whose row carries no persona (written before personas travelled with the
    role) used to be "QA Lead" in the talk's header and title while the console's resolver said
    "Vera". The header (seat_info), the title, the agent's own prompt, a room peer's prompt and
    the identities the console loads now read one persona — and a callsign the operator cleared
    stays cleared."""
    def set_persona(persona):
        with engine.begin() as c:
            c.execute(text(
                "UPDATE roles SET spec = (COALESCE(spec::jsonb, '{}'::jsonb) - 'persona')"
                " || CASE WHEN CAST(:v AS JSONB) IS NULL THEN '{}'::jsonb"
                " ELSE jsonb_build_object('persona', CAST(:v AS JSONB)) END "
                "WHERE project_id = :p AND role_key = 'qa'"),
                {"p": PROJECT, "v": None if persona is None else json.dumps(persona)})

    with engine.begin() as c:
        before = c.execute(text("SELECT spec FROM roles WHERE project_id = :p AND role_key = 'qa'"),
                           {"p": PROJECT}).scalar()
    try:
        set_persona(None)
        talk = _open(client, "talk", ["qa"])
        assert talk["title"] == "Talk with Vera"
        assert talk["seat_info"][0]["callsign"] == "Vera"
        _say(client, talk["conversation_id"], "")
        assert "You are Vera, the " in model.calls[-1]["system"]

        room = _open(client, "room", ["pm", "qa"])
        assert [s.get("callsign") for s in room["seat_info"]][1] == "Vera"
        _say(client, room["conversation_id"], "Who checks the release?")
        pm_prompt = next(c["system"] for c in model.calls if "Also on this panel: " in c["system"]
                         and "(pm)" not in c["system"].split("Also on this panel: ")[1])
        assert "Vera (" in pm_prompt.split("Also on this panel: ")[1]

        identities = client.get("/v1/agents/identities", params={"project_id": PROJECT})
        assert identities.status_code == 200, identities.text
        body = identities.json()
        items = body["items"] if isinstance(body, dict) else body
        assert next(i for i in items if i["role_key"] == "qa")["callsign"] == "Vera"

        # Cleared by the operator: the stored persona has no callsign, and none comes back.
        set_persona({"version": 1, "archetype": None, "tagline": "Show me the test."})
        cleared = _open(client, "talk", ["qa"])
        assert cleared["seat_info"][0]["callsign"] is None
        assert cleared["title"] != "Talk with Vera"
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE roles SET spec = CAST(:s AS JSONB) "
                           "WHERE project_id = :p AND role_key = 'qa'"),
                      {"p": PROJECT, "s": json.dumps(before) if not isinstance(before, str)
                       else before})


def test_an_unknown_seat_outcome_is_explained_and_stops_later_seats(client, engine, model, seated):
    model.fail_for = {"QA Lead"}
    conv = _open(client, "room", SEATS)
    events = _say(client, conv["conversation_id"], "Is the rollout plan safe?")
    starts = [e["role_key"] for e in events if e["type"] == "turn_start"]
    assert starts == SEATS[:2]
    error = next(e for e in events if e["type"] == "error")
    assert error["error"]["headline"] and error["error"]["action"]
    assert error["turn_id"]
    ends = {e["turn_id"]: e["status"] for e in events if e["type"] == "turn_end"}
    assert sorted(ends.values()) == ["complete", "error"]
    assert error["error"]["retry"] is False
    assert events[-1]["type"] == "done"
    failed = next(a for a in _acts(engine, conv["conversation_id"]) if a["role_key"] == "qa")
    assert failed["status"] == "error"


# ── Plan → start ─────────────────────────────────────────────────────────────────────────────


def test_plan_it_with_an_agent_ends_in_a_card_one_press_starts(client, engine, model, seated):
    workflow_key = _workflow(client, "Talked into it")
    conv = _open(client, "plan", ["pm"], context={"from": "palette"})
    # Named as the console names the agent: its callsign when it has one.
    seat = conv["seat_info"][0]
    assert conv["title"] == "Plan with " + (seat.get("callsign") or seat["name"])

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
                                        "message": "Needs a model. Connect one in Repo and models."}
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
        c.execute(text("UPDATE agent_conversations SET streaming_claim = 'flr_other', "
                       "streaming_until = now() + interval '5 minutes' "
                       "WHERE conversation_id = :c"), {"c": cid})
    busy = client.post(f"/v1/conversations/{cid}/turns", json={
        "project_id": PROJECT, "actor": ACTOR, "message": "hi"})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"
    with engine.begin() as c:
        c.execute(text("UPDATE agent_conversations SET streaming_claim = NULL, "
                       "streaming_until = NULL WHERE conversation_id = :c"), {"c": cid})

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


# ── A stream cut off, a floor held, a summary's actions, an agent's scope ──────────────────


def _turn_stream(engine, cid, message, **extra):
    from boss_engine.services import agent_converse

    return agent_converse.take_turn(engine, project_id=PROJECT, conversation_id=cid,
                                    actor=ACTOR, message=message, authority=engine._conversation_test_authority, **extra)


def _reservations(engine, conversation_id):
    with engine.begin() as c:
        return [r[0] for r in c.execute(text(
            "SELECT b.status FROM budget_reservations b JOIN jobs j ON j.id=b.job_id "
            "WHERE j.payload->>'conversation_id'=:r"),
            {"r": conversation_id})]


def test_an_answer_cut_off_mid_stream_is_stopped_settled_and_frees_the_room(
        client, engine, model, seated):
    """Stop, or closing the room mid-answer, closes the stream (GeneratorExit). The turn says
    Stopped, its act closes, its budget comes back, and the very next turn is answered."""
    conv = _open(client, "room", ["pm", "qa"])
    cid = conv["conversation_id"]
    stream = _turn_stream(engine, cid, "Is the plan safe?")
    first = next(stream)
    assert first["type"] == "turn_start" and first["role_key"] == "pm"
    stream.close()

    after = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()
    agent = [t for t in after["turns"] if t["speaker_kind"] == "agent"]
    # The seat speaking is stopped; the seat after it never began.
    assert [(t["role_key"], t["status"]) for t in agent] == [("pm", "error")]
    assert agent[0]["payload"]["headline"] == "Stopped."
    assert [a["status"] for a in _acts(engine, cid)] == ["error"]
    assert _reservations(engine, cid) == []
    assert len(model.calls) == 0

    again = _say(client, cid, "Then tell me now.")
    assert [e["type"] for e in again][-1] == "done"
    assert sorted(e["status"] for e in again if e["type"] == "turn_end") == [
        "complete", "complete"]


def test_a_failure_after_the_answer_settles_its_turn_and_names_it(
        client, engine, model, seated, monkeypatch):
    conv = _open(client, "talk", ["qa"])
    cid = conv["conversation_id"]
    from boss_engine.services.conversation_spend import ConversationCallSpend
    real = ConversationCallSpend._settle_success
    monkeypatch.setattr(ConversationCallSpend, "_settle_success",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger down")))
    events = _say(client, cid, "What would you test?")
    start = next(e for e in events if e["type"] == "turn_start")
    error = next(e for e in events if e["type"] == "error")
    assert error["turn_id"] == start["turn_id"]
    assert next(e for e in events if e["type"] == "turn_end")["status"] == "error"
    assert [a["status"] for a in _acts(engine, cid)] == ["error"]
    assert _reservations(engine, cid) == ["active"]
    assert error["error"]["retry"] is False
    monkeypatch.setattr(ConversationCallSpend, "_settle_success", real)
    assert _say(client, cid, "And now?")[-1]["type"] == "done"


def test_a_turn_left_streaming_by_a_dead_stream_is_swept_by_the_next(
        client, engine, model, seated):
    conv = _open(client, "talk", ["qa"])
    cid = conv["conversation_id"]
    stream = _turn_stream(engine, cid, "First?")
    assert next(stream)["type"] == "turn_start"
    # The worker hung: nothing settled, and its claim lapsed.
    with engine.begin() as c:
        c.execute(text("UPDATE agent_conversations SET streaming_until = now() - interval '1 s' "
                       "WHERE conversation_id = :c"), {"c": cid})
    assert _say(client, cid, "Second?")[-1]["type"] == "done"
    turns = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()["turns"]
    assert [t["status"] for t in turns if t["speaker_kind"] == "agent"] == ["error", "complete"]
    assert [a["status"] for a in _acts(engine, cid)] == ["error", "complete"]
    assert sorted(_reservations(engine, cid)) == ["consumed"]
    # The hung stream, should it wake, finds its turn settled: it records nothing twice.
    rest = list(stream)
    assert [e["type"] for e in rest if e["type"] in ("turn_start", "turn_end")] == []
    turns = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()["turns"]
    assert [t["status"] for t in turns if t["speaker_kind"] == "agent"] == ["error", "complete"]
    assert sorted(_reservations(engine, cid)) == ["consumed"]


def test_two_sends_a_moment_apart_cannot_both_take_the_floor(client, engine, model, seated):
    from boss_engine.services.agent_converse import ConverseError

    conv = _open(client, "room", ["pm", "qa"])
    cid = conv["conversation_id"]
    first = _turn_stream(engine, cid, "One?")
    with pytest.raises(ConverseError) as refused:
        _turn_stream(engine, cid, "Two?")
    assert refused.value.code == "busy"
    events = list(first)
    assert [e["type"] for e in events][-1] == "done"
    # Only the first send was recorded; the floor is free again once it is done.
    turns = client.get(f"/v1/conversations/{cid}",
                       params={"project_id": PROJECT, "actor": ACTOR}).json()["turns"]
    assert [t["content"] for t in turns if t["speaker_kind"] == "person"] == ["One?"]
    assert _say(client, cid, "Three?")[-1]["type"] == "done"


def test_each_summary_card_opens_its_own_decision_and_starting_one_keeps_the_others(
        client, engine, model, seated):
    workflow_key = _workflow(client, "Two summaries")
    conv = _open(client, "room", ["pm", "qa"])
    cid = conv["conversation_id"]
    _say(client, cid, "Ship it?")

    def summed(decision: str) -> str:
        model.summary = {"text": f"About {decision}.", "decisions": [], "next_actions": [
            {"text": decision, "owner_role_key": "pm", "kind": "decision"},
            {"text": "Build it", "kind": "start", "proposal": {
                "workflow_key": workflow_key, "directive": "Build it.",
                "inputs": {"instructions": "Build it."}}}]}
        events = _events(client.post(f"/v1/conversations/{cid}/summary",
                                     json={"project_id": PROJECT, "actor": ACTOR}))
        return next(e for e in events if e["type"] == "summary")["turn_id"]

    older = summed("Choose the date")
    newer = summed("Choose the owner")

    def decide(turn_id):
        r = client.post(f"/v1/conversations/{cid}/decisions", json={
            "project_id": PROJECT, "actor": ACTOR, "turn_id": turn_id, "index": 0})
        assert r.status_code == 200, r.text
        return r.json()

    # The older card opens ITS action, not the latest summary's.
    old_id = decide(older)["decision_id"]
    with engine.begin() as c:
        assert c.execute(text("SELECT question FROM decisions WHERE id = :d"),
                         {"d": old_id}).scalar().startswith("Choose the date")
    new_id = decide(newer)["decision_id"]
    assert new_id != old_id

    # Starting the older card's proposal patches only that proposal: the opened decisions stay
    # opened, and the latest summary stays the latest.
    with engine.begin() as c:
        old_card = c.execute(text("SELECT payload FROM agent_conversation_turns "
                                  "WHERE turn_id = :t"), {"t": older}).scalar()
    started = client.post(f"/v1/conversations/{cid}/start", json={
        "project_id": PROJECT, "actor": ACTOR,
        "proposal_id": old_card["next_actions"][1]["proposal"]["proposal_id"],
        "start_anyway": ["model"]})
    assert started.status_code == 200, started.text
    after = started.json()["conversation"]
    assert after["summary"]["text"] == "About Choose the owner."
    assert after["summary"]["next_actions"][0]["decision_id"] == new_id
    by_turn = {t["turn_id"]: t["payload"] for t in after["turns"]}
    assert by_turn[older]["next_actions"][0]["decision_id"] == old_id
    assert by_turn[older]["next_actions"][1]["proposal"]["status"] == "started"
    assert by_turn[newer]["next_actions"][0]["decision_id"] == new_id
    # Pressing again opens nothing new.
    assert decide(older)["decision_id"] == old_id
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decisions WHERE id = ANY(:d)"),
                         {"d": [old_id, new_id]}).scalar() == 2


def test_an_agent_is_told_what_it_may_use_who_else_is_here_and_who_is_accountable(
        client, engine, model, seated):
    with engine.begin() as c:
        c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id) "
                       "VALUES ('rsk_converse_scope', :p, 'qa', 'release-checklist') "
                       "ON CONFLICT DO NOTHING"), {"p": PROJECT})
    conv = _open(client, "room", ["pm", "qa"])
    _say(client, conv["conversation_id"], "Who checks the release?")
    pm, qa = model.calls[0]["system"], model.calls[1]["system"]
    # The persona compiler's L5 (agent_persona._l5), the same words a work or panel prompt uses.
    granted = re.search(r"Granted for this work: ([^\n]+?)\.(?: |$)", qa)
    assert granted and "release-checklist" in granted.group(1).split(", ")
    assert "Also on this panel: " in pm and "(qa)" in pm.split("Also on this panel: ")[1]
    assert "(pm)" in qa.split("Also on this panel: ")[1]
    assert "(pm)" not in pm.split("Also on this panel: ")[1].split(".")[0]
    assert f"Accountable human: {ACTOR}." in qa and f"Accountable human: {ACTOR}." in pm


def test_a_reply_streams_while_the_model_is_still_writing_it(client, engine, model, seated,
                                                             monkeypatch):
    """The real path, not a replay: the first words a streaming adapter writes reach the
    stream as a `token` event before the adapter has finished (it is still mid-reply when the
    event is read), and the turn settles with the adapter's own usage."""
    from boss_engine.services import agent_converse

    timeline: list[str] = []

    class Streaming(Model):
        def stream_text_response(self, prompt, system_prompt=None, model=None):
            self.calls.append({"who": self.who(system_prompt or ""), "system": system_prompt,
                               "prompt": prompt})
            timeline.append("model:started")
            yield "Show me "
            timeline.append("model:writing")
            yield "the failing case."
            timeline.append("model:finished")
            yield {"model": "stub-1", "prompt_tokens": 10, "completion_tokens": 5,
                   "total_tokens": 15, "cost_usd_micros": 150}

    streaming = Streaming()
    monkeypatch.setattr(agent_converse, "make_adapter", lambda *_a, **_k: streaming)
    monkeypatch.setattr(agent_converse, "REVEAL_SECONDS", 30)  # a replay would hang the test
    conv = _open(client, "talk", ["qa"])
    events = agent_converse.take_turn(engine, project_id=PROJECT,
                                      conversation_id=conv["conversation_id"], actor=ACTOR,
                                      message="What would you test first?", authority=engine._conversation_test_authority)
    said = []
    for event in events:
        if event["type"] == "token":
            timeline.append(f"token:{event['text']}")
            said.append(event["text"])
        elif event["type"] == "turn_end":
            end = event
    assert timeline == ["model:started", "token:Show me ", "model:writing",
                        "token:the failing case.", "model:finished"]
    assert "".join(said) == "Show me the failing case."
    assert end["status"] == "complete" and end["tokens_out"] == 5


# ── Asking again reopens what was said ───────────────────────────────────────────────────────


def _a_run(engine) -> str:
    """A workflow run to ask about (only its row: the talk's context names it)."""
    import uuid

    tag = uuid.uuid4().hex[:10]
    with engine.begin() as c:
        c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                       "created_by) VALUES (:d, :p, :k, 'Ask about me', 'ceo')"),
                  {"d": f"wfd_{tag}", "p": PROJECT, "k": f"ask_{tag}"})
        c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                       "version, status, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, "
                       "'published', CAST('{}' AS JSONB), :v, 'ceo')"),
                  {"v": f"wfv_{tag}", "p": PROJECT, "d": f"wfd_{tag}"})
        c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                       "workflow_version_id, actor, status) VALUES (:r, :p, :d, :v, 'ceo', "
                       "'running')"),
                  {"r": f"wfr_{tag}", "p": PROJECT, "d": f"wfd_{tag}", "v": f"wfv_{tag}"})
    return f"wfr_{tag}"


def _agent(client) -> str:
    """An agent of this test's own, so no other test's talks are open with it."""
    import uuid

    key = f"historian_{uuid.uuid4().hex[:8]}"
    made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": key,
                                          "display_name": "Historian"})
    assert made.status_code == 201, made.text
    return key


def test_asking_a_step_again_reopens_that_talk_with_everything_said(client, engine, model,
                                                                     seated):
    """The operator asked a step's agent, closed the sheet, pressed Ask again — and got an empty
    talk: every Ask made a new conversation. Asking again reopens the talk about that step;
    another step gets its own; a plain "Talk" reopens the latest; only New talk starts over."""
    agent = _agent(client)
    run = _a_run(engine)
    step = {"from": "run_step", "workflow_run_id": run, "step_id": "brief"}
    first = _open(client, "talk", [agent], context=step, resume=True)
    assert first["resumed"] is False and first["turns"] == []
    _say(client, first["conversation_id"], "What did this step make?")

    again = _open(client, "talk", [agent], context=step, resume=True)
    assert again["conversation_id"] == first["conversation_id"] and again["resumed"] is True
    assert [(t["speaker_kind"], t["status"]) for t in again["turns"]] == [
        ("person", "complete"), ("agent", "complete")]
    assert again["turns"][0]["content"] == "What did this step make?"
    assert "speaking: considered view" in again["turns"][1]["content"]

    # Another step of the same run is another subject: its own talk.
    other = _open(client, "talk", [agent], resume=True,
                  context={**step, "step_id": "publish"})
    assert other["conversation_id"] != first["conversation_id"] and other["turns"] == []

    # A door that is not about a step (the Passport's Talk) reopens the talk that has
    # something in it — an empty one opened since never hides it.
    plain = _open(client, "talk", [agent], context={"from": "passport"}, resume=True)
    assert plain["conversation_id"] == first["conversation_id"]
    assert len(plain["turns"]) == 2

    # New talk is the one door that starts over; without `resume` the API makes a new one too.
    fresh = _open(client, "talk", [agent], context=step)
    assert fresh["conversation_id"] != first["conversation_id"] and fresh["resumed"] is False

    # A closed talk is not reopened.
    client.post(f"/v1/conversations/{first['conversation_id']}/close",
                json={"project_id": PROJECT, "actor": ACTOR})
    with engine.begin() as c:
        c.execute(text("UPDATE agent_conversations SET status = 'closed' "
                       "WHERE conversation_id = :c"), {"c": fresh["conversation_id"]})
    after = _open(client, "talk", [agent], context=step, resume=True)
    assert after["conversation_id"] not in {first["conversation_id"], fresh["conversation_id"]}
