"""A squad member's turn is the one agent pipeline's turn, with the board's moves as its choices
(BOSSAPP-908; BOSSAPP-759 "one agent path for every surface").

* The turn goes through AgentService.decide, as the member's role, kept on the item's own
  thread (`work_item:<id>`), with the squad memory, the board and the citable refs in view.
* What a member may do depends on its seat and the board: only the lead proposes; a critic
  critiques the open proposal; votes are offered only when the risk asks for them.
* No herding: a member that has not voted does not see the others' votes in its prompt.
* The caller's check refuses a ghost citation or a repeat and the model is asked once more;
  a move that cannot be made is never posted.
* No model, no move: nothing is posted and the turn says so.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from boss_engine.services import agent_service
from boss_engine.services.ambient import collab, squad_turns

from ._ambient_support import eng, project  # noqa: F401
from ._squad_support import (CRITIC, ENGINEER, LEAD, QA, ScriptedSquadModel, move, settings,
                             squad_item)


def _turn(c, item_id, who, script, **kw):
    return squad_turns.take_turn(c, settings(), item_id, who, model=ScriptedSquadModel(script),
                                 **kw)


def _choices_offered(prompt: str) -> list[str]:
    block = prompt.split("CANDIDATE ACTIONS:", 1)[1]
    return [line[2:].split(":", 1)[0] for line in block.splitlines() if line.startswith("- ")]


def test_a_turn_is_the_members_own_turn_through_agent_service_decide(eng, project, monkeypatch):
    seen = []
    original = agent_service.AgentService.decide

    def spy(self, connection, **kw):
        seen.append(kw)
        return original(self, connection, **kw)

    monkeypatch.setattr(agent_service.AgentService, "decide", spy)
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        turn = _turn(c, item_id, ENGINEER, {ENGINEER: [move(
            "finding", summary="Three call sites use _.template", evidence=["$signal"])]})
        posted = collab.messages(c, item_id, kinds=("finding",))
        thread = c.execute(text(
            "SELECT agent_role FROM agent_threads WHERE project_id = :p AND external_ref = :r"),
            {"p": project, "r": f"work_item:{item_id}"}).scalar()
    call = seen[0]
    assert call["role"] == "software_engineer" and call["external_ref"] == f"work_item:{item_id}"
    assert call["actor"] == ENGINEER
    assert {"squad memory (read first)", "the board (oldest first)", "refs you may cite",
            "your seat"} <= set(call["context"])
    assert turn["status"] == "posted" and turn["move"] == "finding"
    assert posted[0]["sender"] == ENGINEER and posted[0]["body"]["evidence"][0]["kind"] == "signal"
    assert posted[0]["body"]["evidence"][0]["checked"] == "exists"
    assert thread == "software_engineer"


def test_only_the_lead_may_propose_and_votes_are_offered_only_when_the_risk_asks(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        lead_model = ScriptedSquadModel({LEAD: [move(
            "proposal", action="open_draft_pr", summary="Bump lodash to 4.17.21")]})
        squad_turns.take_turn(c, settings(), item_id, LEAD, model=lead_model)
        critic_model = ScriptedSquadModel({CRITIC: [move("pass")]})
        squad_turns.take_turn(c, settings(), item_id, CRITIC, model=critic_model)
        engineer_model = ScriptedSquadModel({ENGINEER: [move("pass")]})
        squad_turns.take_turn(c, settings(), item_id, ENGINEER, model=engineer_model)
    lead_choices = _choices_offered(lead_model.prompts[0][1])
    critic_choices = _choices_offered(critic_model.prompts[0][1])
    assert "proposal" in lead_choices and "resolve" not in lead_choices
    assert "proposal" not in critic_choices and "critique" in critic_choices
    # open_draft_pr is medium risk: maker-checker, so a critique is asked for, not a vote.
    assert "vote" not in critic_choices
    assert "what the autonomy gate allows here" in lead_model.prompts[0][1]
    assert "what the autonomy gate allows here" not in engineer_model.prompts[0][1]


def test_a_member_that_has_not_voted_does_not_see_the_others_votes(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        _turn(c, item_id, LEAD, {LEAD: [move("proposal", action="merge",
                                             summary="Merge acme/web#41")]})
        _turn(c, item_id, ENGINEER, {ENGINEER: [move("vote", vote="reject",
                                                     reason="SECRET-REASON-XYZ")]})
        qa_model = ScriptedSquadModel({QA: [move("vote", vote="approve", reason="tests pass")]})
        squad_turns.take_turn(c, settings(), item_id, QA, model=qa_model)
        votes = collab.messages(c, item_id, kinds=("vote",))
    assert "vote" in _choices_offered(qa_model.prompts[0][1])     # merge: the squad votes
    assert "SECRET-REASON-XYZ" not in qa_model.prompts[0][1]
    assert [v["sender"] for v in votes] == [ENGINEER, QA]


def test_a_ghost_citation_is_refused_and_asked_once_more(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        model = ScriptedSquadModel({QA: [
            move("finding", summary="Coverage is thin", evidence=["alert:al_does_not_exist"]),
            move("finding", summary="Coverage is thin", evidence=["log:coverage report"])]})
        turn = squad_turns.take_turn(c, settings(), item_id, QA, model=model)
        findings = collab.messages(c, item_id, kinds=("finding",))
    assert len(model.prompts) == 2 and "does not exist" in model.prompts[1][1]
    assert turn["status"] == "posted" and len(findings) == 1
    assert findings[0]["body"]["evidence"][0]["ref"] == "coverage report"


def test_a_repeat_is_refused_and_a_member_that_keeps_repeating_posts_nothing(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        same = move("finding", summary="Three call sites use _.template")
        _turn(c, item_id, ENGINEER, {ENGINEER: [same]})
        turn = _turn(c, item_id, ENGINEER, {ENGINEER: [same, same]})
        findings = collab.messages(c, item_id, kinds=("finding",))
    assert len(findings) == 1
    assert turn["move"] == "pass" and turn["status"] == "no_move"
    assert "already posted" in turn["reason"]


def test_no_model_means_no_move_and_nothing_on_the_board(eng, project):
    class Down:
        def create_text_response(self, *_a, **_k):
            raise RuntimeError("no model connected")

    with eng.begin() as c:
        got = squad_item(c, project)
        item_id = got["item"]["id"]
        before = len(collab.messages(c, item_id))
        turn = squad_turns.take_turn(c, settings(), item_id, QA, model=Down())
        after = len(collab.messages(c, item_id))
        event = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p AND event_type = 'ambient.squad_turn'"),
            {"p": project}).scalar()
    assert turn["status"] == "no_move" and turn["move"] == "pass" and before == after
    data = event if isinstance(event, dict) else json.loads(event)
    assert data["member"] == QA and data["status"] == "no_move"


def test_someone_off_the_squad_takes_no_turn(eng, project):
    with eng.begin() as c:
        got = squad_item(c, project)
        try:
            squad_turns.take_turn(c, settings(), got["item"]["id"], "agent:pm",
                                  model=ScriptedSquadModel({}))
        except squad_turns.TurnRefused as refused:
            guard = refused.guard
    assert guard == "not_seated"
