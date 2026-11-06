"""An ambient work item has one live copy and one owner, and a person always wins (BOSSAPP-908;
design §2.2 pattern 7, §4 L4, §6 rule 1).

* One live item per fingerprint: a second sensor raising the same problem JOINS the live item
  (its signals link to it) — and the database refuses a second live row outright.
* The item is an ordinary portfolio work item (scored by work_orchestration, status 'active'
  while live, 'done' after), so admission and ranking read it unchanged.
* An agent works it only under a lease with a fencing token; an expired, released or taken-over
  lease fails `assert_lease` with LeaseLost. A person's take-over cancels the agent's lease at
  once — the Codex "keeps firing after I took over" bug cannot happen.
* Needs you is one Decisions row (Command Center's queue) linked to the item.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.ambient import board
from boss_engine.services.work_orchestration import WorkOrchestrationService

from ._ambient_support import eng, open_item, project, signal  # noqa: F401


def test_the_same_problem_raised_twice_joins_the_one_live_item(eng, project):
    with eng.begin() as c:
        first = open_item(c, project)
        second_signal = signal(c, project, fingerprint_value="fp:other-sensor",
                               source="doctor", kind="doctor.5xx", source_ref="x")
        joined = open_item(c, project, signal_ids=[second_signal["id"]])
        links = board.links(c, first["item"]["id"])
        status = c.execute(text("SELECT status, work_item_id FROM signals WHERE id = :s"),
                           {"s": second_signal["id"]}).one()
    assert first["outcome"] == "opened" and joined["outcome"] == "joined"
    assert joined["item"]["id"] == first["item"]["id"]
    assert len([l for l in links if l["kind"] == "signal"]) == 2
    assert status.status == "triaged" and status.work_item_id == first["item"]["id"]


def test_the_database_refuses_a_second_live_item_for_a_fingerprint(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        with pytest.raises(Exception):
            with c.begin_nested():
                c.execute(text(
                    "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, "
                    "ambient, fingerprint, lifecycle) VALUES ('work_dup', :p, 'ambient', "
                    "'work_dup', 'dup', true, :f, 'proposed')"),
                    {"p": project, "f": item["fingerprint"]})


def test_an_ambient_item_is_a_scored_portfolio_item_the_admission_reads(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, scores={"urgency": 90, "customer_impact": 80})["item"]
        ranked = WorkOrchestrationService(get_settings()).list_work_items(c, project)
        scored = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                "event_type = 'work.priority_scored' AND entity_id = :w"),
                           {"p": project, "w": item["id"]}).scalar()
    assert item["status"] == "active" and item["lifecycle"] == "proposed"
    assert item["subject_type"] == "ambient" and item["subject_id"] == item["id"]
    assert item["subject_ref"] == "app:order-api" and item["autonomy_level"] == "A1"
    assert item["urgency"] == 90 and item["id"] in [w["id"] for w in ranked]
    assert scored == 1


def test_an_agent_works_only_under_its_lease_and_its_token_fences_every_effect(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        held = board.claim(c, item["id"], owner="agent:sre", minutes=30)
        token = held["lease_token"]
        assert "lease_token" not in held["item"], "the token is the holder's alone"
        board.assert_lease(c, item["id"], owner="agent:sre", token=token)
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item["id"], owner="agent:sre", token="forged")
        with pytest.raises(board.Conflict):
            board.claim(c, item["id"], owner="agent:qa")
        started = board.start(c, item["id"], owner="agent:sre", token=token)
        assert started["lifecycle"] == "in_progress" and started["lease_owner"] == "agent:sre"
        board.renew(c, item["id"], owner="agent:sre", token=token, minutes=10)
        released = board.release(c, item["id"], owner="agent:sre", token=token)
        assert released["lifecycle"] == "proposed" and released["lease_owner"] is None
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item["id"], owner="agent:sre", token=token)


def test_an_expired_lease_goes_back_to_proposed_for_the_next_claim(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        token = board.claim(c, item["id"], owner="agent:sre")["lease_token"]
        c.execute(text("UPDATE work_items SET lease_expires_at = now() - interval '1 second' "
                       "WHERE id = :id"), {"id": item["id"]})
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item["id"], owner="agent:sre", token=token)
        assert item["id"] in board.expire_leases(c, project_id=project)
        assert board.get(c, item["id"])["lifecycle"] == "proposed"
        assert board.claim(c, item["id"], owner="agent:qa")["item"]["lease_owner"] == "agent:qa"


def test_a_person_taking_over_cancels_the_agents_lease_at_once_and_for_good(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        token = board.claim(c, item["id"], owner="agent:sre")["lease_token"]
        taken = board.take_over(c, item["id"], actor="ceo", reason="I'll handle this one")
        with pytest.raises(board.LeaseLost):
            board.assert_lease(c, item["id"], owner="agent:sre", token=token)
        with pytest.raises(board.Conflict):
            board.claim(c, item["id"], owner="agent:sre")
        handoff = c.execute(text("SELECT sender, body FROM work_item_messages WHERE "
                                 "work_item_id = :w AND kind = 'handoff'"),
                            {"w": item["id"]}).one()
    assert taken["lease_owner"] == "person:ceo" and taken["lease_kind"] == "person"
    assert taken["lease_expires_at"] is None
    assert handoff.sender == "boss" and handoff.body["to"] == "person:ceo"
    assert handoff.body["from"] == "agent:sre"


def test_a_persons_action_on_the_subject_takes_over_every_live_item_on_it(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, subject_ref="pr:acme/shop#41")["item"]
        board.claim(c, item["id"], owner="agent:software_engineer")
        other = open_item(c, project, fingerprint="fp:unrelated", subject_ref="app:helpdesk")
        taken = board.on_person_action(c, project_id=project, subject_ref="pr:acme/shop#41",
                                       actor="ceo", what="pushed a commit to the branch")
        untouched = board.get(c, other["item"]["id"])
    assert taken == [item["id"]]
    assert board_owner(eng, item["id"]) == "person:ceo"
    assert untouched["lease_owner"] is None


def board_owner(eng, item_id: str) -> str:
    with eng.begin() as c:
        return board.get(c, item_id)["lease_owner"]


def test_needs_you_is_one_decision_in_the_one_queue_and_an_answer_moves_the_item_on(
        eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        token = board.claim(c, item["id"], owner="agent:sre")["lease_token"]
        with pytest.raises(board.LeaseLost):
            board.needs_you(c, item["id"], actor="agent:sre", question="Roll back?",
                            recommendation={"action": "prod_rollback"})
        asked = board.needs_you(c, item["id"], actor="agent:sre", token=token,
                                question="Roll back order-api to 6bf2672?",
                                recommendation={"action": "prod_rollback"},
                                options=[{"key": "rollback", "label": "Roll back"},
                                         {"key": "wait", "label": "Wait"}],
                                cost_of_waiting="customers see 5xx until it is fixed")
        decision = c.execute(text("SELECT question, status, options, recommendation FROM "
                                  "decisions WHERE id = :d"),
                             {"d": asked["needs_you_decision_id"]}).one()
        answered = board.answered(c, item["id"], actor="ceo")
    assert asked["lifecycle"] == "needs_you" and asked["status"] == "active"
    assert decision.status == "pending" and decision.question.startswith("Roll back order-api")
    assert [o["key"] for o in decision.options] == ["rollback", "wait"]
    assert decision.recommendation["work_item_id"] == item["id"]
    assert answered["lifecycle"] == "claimed", "the agent still holds a live lease: back to it"
    assert answered["answered_at"] is not None


def test_ending_an_item_closes_its_signals_releases_the_lease_and_leaves_the_portfolio(
        eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        token = board.claim(c, item["id"], owner="agent:sre")["lease_token"]
        board.start(c, item["id"], owner="agent:sre", token=token)
        with pytest.raises(board.LeaseLost):
            board.finish(c, item["id"], actor="agent:sre", lifecycle="resolved")
        done = board.finish(c, item["id"], actor="agent:sre", token=token, lifecycle="resolved",
                            outcome="recovered in 4m")
        statuses = c.execute(text("SELECT DISTINCT status FROM signals WHERE work_item_id = :w"),
                             {"w": item["id"]}).scalars().all()
        with pytest.raises(board.Conflict):
            board.take_over(c, item["id"], actor="ceo")
        reopened = open_item(c, project)
    assert done["lifecycle"] == "resolved" and done["status"] == "done"
    assert done["resolved_by"] == "agent:sre" and done["lease_owner"] is None
    assert statuses == ["done"]
    assert reopened["outcome"] == "opened" and reopened["item"]["id"] != item["id"], \
        "after it ends, the same problem may open a new item"


def test_a_dismissal_says_why_and_an_item_cannot_jump_its_lifecycle(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        with pytest.raises(ValueError):
            board.dismiss(c, item["id"], actor="ceo", reason="  ")
        with pytest.raises(board.Conflict, match="from proposed to in_progress"):
            board._transition(c, item, "in_progress", actor="boss")
        gone = board.dismiss(c, item["id"], actor="ceo", reason="known flake, ignore")
    assert gone["lifecycle"] == "dismissed" and gone["outcome"].startswith("dismissed: known")
