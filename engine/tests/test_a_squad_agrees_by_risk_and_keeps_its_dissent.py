"""A squad agrees the way the risk asks, and its dissent is never lost (BOSSAPP-908; design
§11.4, §13.3).

* Low risk (convene a panel): the lead decides at once; the decision is the lead's.
* Medium risk (a draft PR): the critic checks. One objection and nobody agreeing sends it back
  to the lead as a recorded 'revise' and into tried-and-failed memory, and the same proposal
  cannot be made again. Agreement with one objection is approved — as the SQUAD's decision,
  the objection kept as dissent on the ledger row. Two objections go to a person.
* Nobody checking within two turns each goes to a person.
* High risk: the squad votes; a tie has no majority and goes to a person recommending HOLD.
* Always-a-person actions are only recommended, whatever the vote.
* A person's answer is recorded as THEIR decision; what they approved is carried out at the
  squad's next round, once.
"""
from __future__ import annotations

from sqlalchemy import text

from boss_engine.services.ambient import board, collab, ledger, squad_memory, squad_turns

from ._ambient_support import eng, project  # noqa: F401
from ._squad_support import (CRITIC, ENGINEER, LEAD, QA, Launchers, ScriptedSquadModel, move,
                             settings, squad_item)

DRAFT = move("proposal", action="open_draft_pr", summary="Bump lodash to 4.17.21",
             verify="CI green")


def _turns(c, item_id, script, launchers=None):
    model = ScriptedSquadModel(script)
    out = []
    for who in (LEAD, ENGINEER, QA, CRITIC):
        if script.get(who):
            out.append(squad_turns.take_turn(c, settings(), item_id, who, model=model,
                                             launchers=launchers))
    return out


def _settle(c, item_id, launchers=None):
    return squad_turns.settle(c, settings(), item_id, end_of_round=True, launchers=launchers)


def test_a_low_risk_proposal_is_the_leads_call_at_once(eng, project):
    launchers = Launchers()
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        turn = _turns(c, item_id, {LEAD: [move("proposal", action="convene_meeting",
                                                summary="Bump only, or bump and escape?")]},
                      launchers)[0]
        decision = ledger.list_decisions(c, project, work_item_id=item_id)[0]
        item = board.get(c, item_id)
    assert turn["settled"]["state"] == "decided"
    assert decision["decided_by"] == LEAD and decision["consensus"]["mode"] == "single"
    assert decision["action"] == "convene_meeting" and item["lifecycle"] == "in_progress"
    assert turn["settled"]["executed"]["kind"] == "meeting" and launchers.convened


def test_one_objection_and_no_agreement_goes_back_to_the_lead_and_cannot_be_repeated(eng,
                                                                                   project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {LEAD: [DRAFT], CRITIC: [move(
            "critique", position="object", summary="Escape /emails too")]})
        settled = _settle(c, item_id)
        row = ledger.get_decision(c, settled["act_id"])
        memory = squad_memory.read(c, item_id)
        again = ScriptedSquadModel({LEAD: [DRAFT, DRAFT]})
        retry = squad_turns.take_turn(c, settings(), item_id, LEAD, model=again)
    assert settled["state"] == "revise" and row["chosen"] == "revise"
    assert row["decided_by"].startswith("squad:") and row["action"] is None
    assert row["consensus"]["dissent"][0]["party"] == CRITIC
    assert any("Escape /emails too" in r for r in memory["tried_and_failed"])
    assert retry["move"] == "pass" and "already tried" in retry["reason"]


def test_agreement_with_one_objection_is_the_squads_decision_with_the_dissent_kept(eng, project):
    launchers = Launchers()
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {
            LEAD: [DRAFT],
            ENGINEER: [move("critique", position="agree", summary="Patch-level, safe")],
            QA: [move("critique", position="concern", summary="No /emails test")],
            CRITIC: [move("critique", position="object", summary="Split the PR")]}, launchers)
        settled = _settle(c, item_id, launchers)
        row = ledger.get_decision(c, settled["act_id"])
    assert settled["state"] == "decided" and row["action"] == "open_draft_pr"
    assert row["decided_by"].startswith("squad:")
    assert row["consensus"]["mode"] == "maker_checker" and row["consensus"]["result"] == "approved"
    assert [d["party"] for d in row["consensus"]["dissent"]] == [CRITIC]
    assert "1 dissent kept on record" in row["rationale"]
    assert settled["executed"]["kind"] == "workflow_run"


def test_two_objections_go_to_a_person_with_both_on_the_card(eng, project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {
            LEAD: [DRAFT],
            ENGINEER: [move("critique", position="object", summary="Breaks the build on node 18")],
            CRITIC: [move("critique", position="object", summary="Split the PR")]})
        settled = _settle(c, item_id)
        item = board.get(c, item_id)
        card = c.execute(text("SELECT recommendation, options FROM decisions WHERE id = :d"),
                         {"d": item["needs_you_decision_id"]}).mappings().one()
    assert settled["state"] == "needs_you" and item["lifecycle"] == "needs_you"
    assert card["recommendation"]["recommended"] == "hold"
    assert {d["party"] for d in card["recommendation"]["dissent"]} == {ENGINEER, CRITIC}
    assert [o["key"] for o in card["options"]] == ["approve", "hold"]


def test_a_proposal_nobody_checks_goes_to_a_person(eng, project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
    run = squad_turns.collaborate(eng, settings(), item_id, rounds=4,
                                  model=ScriptedSquadModel({LEAD: [DRAFT]}),
                                  launchers=Launchers())
    with eng.begin() as c:
        card = c.execute(text(
            "SELECT recommendation FROM decisions WHERE id = :d"),
            {"d": board.get(c, item_id)["needs_you_decision_id"]}).scalar()
    assert run["stopped"]["guard"] == "needs_you" and run["rounds"] == 2
    assert "nobody checked" in card["consensus"]["tie_break"]


def test_a_tied_squad_vote_has_no_majority_and_a_person_is_asked_to_hold(eng, project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {
            LEAD: [move("proposal", action="prod_rollback", summary="Roll web back to 6bf2672")],
            ENGINEER: [move("vote", vote="approve", reason="the release caused it")],
            QA: [move("vote", vote="reject", reason="drops the coupon fix")],
            CRITIC: [move("vote", vote="abstain")]})
        settled = _settle(c, item_id)
        card = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                         {"d": settled["decision_id"]}).scalar()
    assert settled["state"] == "needs_you" and settled["result"] == "tied"
    assert card["recommended"] == "hold" and "no majority" in card["consensus"]["tie_break"]


def test_a_merge_is_only_recommended_even_when_the_squad_approves(eng, project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {
            LEAD: [move("proposal", action="merge", summary="Merge acme/web#41")],
            ENGINEER: [move("vote", vote="approve")], QA: [move("vote", vote="approve")],
            CRITIC: [move("vote", vote="reject", reason="split it")]})
        settled = _settle(c, item_id)
        agent_decisions = [d for d in ledger.list_decisions(c, project, work_item_id=item_id)
                           if d["action"] == "merge"]
    assert settled["state"] == "needs_you" and settled["recommended"] == "approve"
    assert settled["dissent"][0]["party"] == CRITIC and agent_decisions == []


def test_a_persons_approval_is_their_decision_and_is_carried_out_once(eng, project):
    launchers = Launchers()
    with eng.begin() as c:
        # dependency_upgrade runs at A1: starting a workflow needs a person.
        item_id = squad_item(c, project, task_class="dependency_upgrade",
                             fingerprint="fp:dep")["item"]["id"]
        _turns(c, item_id, {
            LEAD: [move("proposal", action="start_workflow", summary="Upgrade lodash",
                        workflow="squad_demo_build")],
            CRITIC: [move("critique", position="agree", summary="Low risk")]})
        asked = _settle(c, item_id, launchers)
        answered = squad_turns.person_answered(c, item_id, actor="u_ops", approve=True,
                                               rationale="Go ahead.")
    assert asked["state"] == "needs_you" and asked["recommended"] == "approve"
    assert answered["decision"]["decided_by"] == "person:u_ops"
    assert answered["decision"]["action"] == "start_workflow"
    assert answered["item"]["lifecycle"] == "claimed"
    first = squad_turns.collaborate(eng, settings(), item_id, rounds=1,
                                    model=ScriptedSquadModel({}), launchers=launchers)
    second = squad_turns.collaborate(eng, settings(), item_id, rounds=1,
                                     model=ScriptedSquadModel({}), launchers=launchers)
    with eng.begin() as c:
        handoffs = [m for m in collab.messages(c, item_id, kinds=("handoff",))
                    if (m["body"] or {}).get("via")]
    assert len(launchers.started) == 1 and len(handoffs) == 1
    assert handoffs[0]["body"]["packet"]["decided"]["decided_by"] == "person:u_ops"
    assert first["results"] == [] and second["results"][0]["status"] == "completed"


def test_a_person_who_holds_it_sends_it_into_tried_and_failed(eng, project):
    with eng.begin() as c:
        item_id = squad_item(c, project)["item"]["id"]
        _turns(c, item_id, {
            LEAD: [move("proposal", action="merge", summary="Merge acme/web#41")],
            ENGINEER: [move("vote", vote="approve")], QA: [move("vote", vote="approve")],
            CRITIC: [move("vote", vote="approve")]})
        _settle(c, item_id)
        answered = squad_turns.person_answered(c, item_id, actor="u_ops", approve=False,
                                               rationale="Wait for the release freeze to end.")
        memory = squad_memory.read(c, item_id)
    assert answered["decision"]["chosen"] == "hold" and answered["decision"]["action"] is None
    assert any("held by person:u_ops" in r for r in memory["tried_and_failed"])
