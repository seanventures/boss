"""Agents agree on the shared board, with the consensus mode chosen by risk (BOSSAPP-908; design
§11.4, §13).

* Messages are typed and validated; votes and critiques answer a proposal on the same item; one
  vote per voter; findings cite evidence that exists; the board's own moves (claim, decision,
  resolve) cannot be forged by a post.
* No herding: an agent reading the board before it votes does not see the others' votes.
* Low risk: the owner decides. Medium: a critic who is not the proposer must agree, two
  objections go to a person. High: a squad vote (quorum ≥ 3, ≥ 2/3) AND a person. Irreversible:
  the squad only recommends.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import collab, policy

from ._ambient_support import eng, open_item, project  # noqa: F401


def _proposal(c, item_id: str, action: str, sender: str = "agent:sre") -> dict:
    return collab.post(c, item_id, kind="proposal", sender=sender,
                       body={"action": action, "summary": f"{action} order-api",
                             "verify": "5xx < 1% within 5m"})


def test_messages_are_typed_and_validated(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        with pytest.raises(ValueError, match="needs summary"):
            collab.post(c, item["id"], kind="finding", sender="agent:sre", body={})
        with pytest.raises(Exception, match="does not exist"):
            collab.post(c, item["id"], kind="finding", sender="agent:sre",
                        body={"summary": "x", "evidence": [{"kind": "alert", "ref": "al_none"}]})
        with pytest.raises(ValueError, match="action catalogue"):
            collab.post(c, item["id"], kind="proposal", sender="agent:sre",
                        body={"action": "drop_database", "summary": "x"})
        with pytest.raises(ValueError, match="replies to a proposal"):
            collab.post(c, item["id"], kind="vote", sender="agent:qa", body={"vote": "approve"})
        with pytest.raises(ValueError, match="written by the board"):
            collab.post(c, item["id"], kind="claim", sender="agent:sre", body={})
        with pytest.raises(ValueError, match="a party is"):
            collab.post(c, item["id"], kind="finding", sender="mallory", body={"summary": "x"})
        hyp = collab.post(c, item["id"], kind="hypothesis", sender="agent:investigator",
                          body={"summary": "N+1 in total() for 15+ items", "confidence": 0.72,
                                "evidence": [{"kind": "commit", "ref": "6bf2673"}]})
        squad = c.execute(text("SELECT squad FROM work_items WHERE id = :w"),
                          {"w": item["id"]}).scalar()
    assert hyp["body"]["confidence"] == 0.72 and hyp["body"]["evidence"][0]["checked"] == "shape"
    assert "agent:investigator" in squad


def test_one_vote_per_voter_and_no_agent_sees_the_votes_before_casting_its_own(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        prop = _proposal(c, item["id"], "prod_rollback")
        collab.post(c, item["id"], kind="vote", sender="agent:investigator",
                    reply_to=prop["id"], body={"vote": "approve"})
        with pytest.raises(ValueError, match="already voted"):
            collab.post(c, item["id"], kind="vote", sender="agent:investigator",
                        reply_to=prop["id"], body={"vote": "reject"})
        unvoted_view = collab.messages(c, item["id"], viewer="agent:reviewer")
        collab.post(c, item["id"], kind="vote", sender="agent:reviewer", reply_to=prop["id"],
                    body={"vote": "reject", "reason": "drops the coupon fix"})
        voted_view = collab.messages(c, item["id"], viewer="agent:reviewer")
        person_view = collab.messages(c, item["id"], viewer="person:ceo")
    assert [m["kind"] for m in unvoted_view if m["kind"] == "vote"] == []
    assert len([m for m in voted_view if m["kind"] == "vote"]) == 2
    assert len([m for m in person_view if m["kind"] == "vote"]) == 2


def test_low_risk_the_owner_decides_alone(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="doc_drift", fingerprint="fp:docs")["item"]
        prop = _proposal(c, item["id"], "propose", sender="agent:docs_writer")
        record = collab.tally(c, item["id"], prop["id"])
    assert record["mode"] == "single" and record["result"] == "approved"
    assert not record["needs_person"]


def test_medium_risk_needs_a_critic_who_is_not_the_proposer(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="incident_staging_mitigation",
                         fingerprint="fp:staging")["item"]
        prop = _proposal(c, item["id"], "staging_rollback")
        collab.post(c, item["id"], kind="critique", sender="agent:sre", reply_to=prop["id"],
                    body={"position": "agree", "summary": "I agree with myself"})
        alone = collab.tally(c, item["id"], prop["id"])
        collab.post(c, item["id"], kind="critique", sender="agent:adversarial_reviewer",
                    reply_to=prop["id"], body={"position": "agree", "summary": "Safe on staging"})
        checked = collab.tally(c, item["id"], prop["id"])
    assert alone["mode"] == "maker_checker" and alone["result"] == "pending", \
        "the proposer agreeing with itself is not a check"
    assert checked["result"] == "approved" and not checked["needs_person"]
    assert checked["participants"] == ["agent:sre", "agent:adversarial_reviewer"]


def test_medium_risk_two_objections_or_one_unsafe_go_to_a_person(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="incident_staging_mitigation",
                         fingerprint="fp:staging")["item"]
        prop = _proposal(c, item["id"], "staging_restart")
        collab.post(c, item["id"], kind="critique", sender="agent:qa", reply_to=prop["id"],
                    body={"position": "object", "summary": "restart loses the repro"})
        one = collab.tally(c, item["id"], prop["id"])
        collab.post(c, item["id"], kind="vote", sender="agent:adversarial_reviewer",
                    reply_to=prop["id"], body={"vote": "reject", "reason": "same"})
        two = collab.tally(c, item["id"], prop["id"])
        prop2 = _proposal(c, item["id"], "staging_rollback")
        collab.post(c, item["id"], kind="critique", sender="agent:qa", reply_to=prop2["id"],
                    body={"position": "unsafe", "summary": "the migration is not reversible"})
        unsafe = collab.tally(c, item["id"], prop2["id"])
    assert one["result"] == "rejected" and one["dissent"][0]["party"] == "agent:qa"
    assert two["result"] == "needs_person" and two["needs_person"]
    assert unsafe["result"] == "needs_person"


def test_high_risk_is_a_squad_vote_and_a_person_with_dissent_recorded(eng, project):
    with eng.begin() as c:
        policy.set_level(c, project, "incident_response", actor="ceo", level="A3")
        item = open_item(c, project)["item"]
        prop = _proposal(c, item["id"], "prod_rollback")
        for voter, vote in (("agent:investigator", "approve"), ("agent:software_engineer",
                                                                "approve"),
                            ("agent:adversarial_reviewer", "reject")):
            collab.post(c, item["id"], kind="vote", sender=voter, reply_to=prop["id"],
                        body={"vote": vote, "reason": "drops the coupon fix"
                              if vote == "reject" else ""})
        record = collab.tally(c, item["id"], prop["id"])
    assert record["mode"] == "squad_vote_and_person"
    assert record["result"] == "approved" and record["needs_person"]
    assert record["tally"]["quorum"] >= 3 and record["tally"]["approve"] == 2
    assert record["dissent"] == [{"party": "agent:adversarial_reviewer", "position": "object",
                                  "reason": "drops the coupon fix"}]


def test_too_few_votes_on_a_high_risk_action_is_no_quorum(eng, project):
    with eng.begin() as c:
        policy.set_level(c, project, "incident_response", actor="ceo", level="A3")
        item = open_item(c, project)["item"]
        prop = _proposal(c, item["id"], "prod_restart")
        collab.post(c, item["id"], kind="vote", sender="agent:investigator",
                    reply_to=prop["id"], body={"vote": "approve"})
        record = collab.tally(c, item["id"], prop["id"])
    assert record["result"] == "no_quorum" and record["needs_person"]


def test_an_irreversible_action_is_only_ever_recommended(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="dependency_upgrade", fingerprint="fp:dep")["item"]
        prop = _proposal(c, item["id"], "merge", sender="agent:software_engineer")
        record = collab.tally(c, item["id"], prop["id"])
    assert record["mode"] == "recommend_only" and record["needs_person"]
    assert collab.satisfies(record, "recommend_only") is False


def test_a_closed_board_takes_no_new_posts(eng, project):
    from boss_engine.services.ambient import board

    with eng.begin() as c:
        item = open_item(c, project)["item"]
        board.dismiss(c, item["id"], actor="ceo", reason="duplicate of INC-31")
        with pytest.raises(ValueError, match="board is closed"):
            collab.post(c, item["id"], kind="finding", sender="agent:sre",
                        body={"summary": "late"})
        kinds = [m["kind"] for m in collab.messages(c, item["id"])]
    assert kinds[-1] == "resolve"
