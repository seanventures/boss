"""The decision ledger is append-only, cites only real evidence, and an override is a new row
(BOSSAPP-906; design §11.2).

A decision is an act of kind 'decision' (so every act reader sees who decided, when, at what
cost) with its 1:1 detail row. The database refuses any edit or delete of a decision; the one
later write fills an empty outcome, once. A person's override is a new row pointing at the row
it overrides, which then reads 'overridden'. Evidence that does not exist, an over-long
rationale and a reversible decision without its undo are refused before anything is written.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import collab, ledger
from boss_engine.services.ambient._common import EvidenceError

from ._ambient_support import eng, open_item, project  # noqa: F401


def _decide(c, pid: str, item: dict, **kw) -> dict:
    args = dict(project_id=pid, decided_by="agent:chief_of_staff",
                question="Is this worth an incident squad?", chosen="open_incident",
                options=["open_incident", "ignore"], rationale="5xx above 5% for 6 minutes. "
                "Customers reported checkout failures.", reversibility="reversible",
                undo={"action": "dismiss"}, evidence=[{"kind": "work_item", "ref": item["id"]}],
                confidence=0.86, work_item_id=item["id"], action="propose")
    args.update(kw)
    return ledger.record_decision(c, **args)


def test_a_decision_is_an_act_with_its_detail_and_lands_on_the_items_board(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        row = _decide(c, project, item, cost_usd_micros=42_000)
        act = c.execute(text("SELECT kind, status, role_key, how, accountable, subject_id, "
                             "cost_usd, ended_at FROM agent_acts WHERE act_id = :a"),
                        {"a": row["act_id"]}).one()
        board_msg = c.execute(text("SELECT kind, sender, body FROM work_item_messages WHERE "
                                   "work_item_id = :w AND kind = 'decision'"),
                              {"w": item["id"]}).one()
        cost = c.execute(text("SELECT cost_usd_micros FROM work_items WHERE id = :w"),
                         {"w": item["id"]}).scalar()
    assert act.kind == "decision" and act.status == "complete" and act.ended_at is not None
    assert act.role_key == "chief_of_staff" and act.how == "board"
    assert act.accountable == "boss" and act.subject_id == item["id"]
    assert float(act.cost_usd) == pytest.approx(0.042)
    assert row["chosen"] == "open_incident" and row["rejected"] == [{"option": "ignore"}]
    assert row["autonomy_level"] == "A1" and row["task_class"] == "incident_response"
    assert row["evidence"][0]["checked"] == "exists"
    assert row["edge_label"] == "From the work board" and row["decider_kind"] == "agent"
    assert board_msg.kind == "decision" and board_msg.body["act_id"] == row["act_id"]
    assert cost == 42_000


def test_the_database_refuses_to_edit_or_delete_a_decision(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        row = _decide(c, project, item)
        with pytest.raises(Exception, match="append-only"):
            with c.begin_nested():
                c.execute(text("UPDATE agent_act_decisions SET rationale = 'rewritten' "
                               "WHERE act_id = :a"), {"a": row["act_id"]})
        with pytest.raises(Exception, match="append-only"):
            with c.begin_nested():
                c.execute(text("DELETE FROM agent_act_decisions WHERE act_id = :a"),
                          {"a": row["act_id"]})
        with pytest.raises(Exception, match="append-only"):
            with c.begin_nested():
                c.execute(text("UPDATE agent_act_decisions SET outcome = 'accepted', "
                               "chosen = 'ignore' WHERE act_id = :a"), {"a": row["act_id"]})


def test_an_outcome_is_filled_once(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        row = _decide(c, project, item)
        filled = ledger.record_outcome(c, row["act_id"], outcome="accepted", outcome_ref="x")
        with pytest.raises(ledger.LedgerConflict):
            ledger.record_outcome(c, row["act_id"], outcome="reverted")
    assert filled["outcome"] == "accepted" and filled["outcome_at"] is not None


def test_a_persons_override_is_a_new_row_pointing_at_the_one_it_overrides(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        row = _decide(c, project, item)
        over = ledger.override(c, row["act_id"], actor="ceo", chosen="ignore",
                               rationale="Known synthetic traffic from the load test.")
        original = ledger.get_decision(c, row["act_id"])
        with pytest.raises(ledger.LedgerConflict):
            ledger.override(c, row["act_id"], actor="ceo", chosen="open_incident",
                            rationale="Changed my mind.")
        with pytest.raises(ValueError, match="only a person"):
            _decide(c, project, item, overrides_act_id=over["act_id"])
        act = c.execute(text("SELECT role_key, handed_by_kind, handed_by_id, accountable, how, "
                             "parent_act_id FROM agent_acts WHERE act_id = :a"),
                        {"a": over["act_id"]}).one()
    assert over["overrides_act_id"] == row["act_id"] and over["decided_by"] == "person:ceo"
    assert over["question"] == row["question"] and over["chosen"] == "ignore"
    assert original["outcome"] == "overridden" and original["outcome_ref"] == over["act_id"]
    assert act.role_key is None and act.handed_by_kind == "person" and act.accountable == "ceo"
    assert act.how == "approved" and act.parent_act_id == row["act_id"]


def test_evidence_must_exist_in_this_project(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        with pytest.raises(EvidenceError, match="does not exist"):
            _decide(c, project, item, evidence=[{"kind": "signal", "ref": "sig_ghost"}])
        with pytest.raises(EvidenceError, match="unknown evidence kind"):
            _decide(c, project, item, evidence=[{"kind": "vibes", "ref": "strong"}])
        with pytest.raises(EvidenceError, match="at least one"):
            _decide(c, project, item, evidence=[])
        ok = _decide(c, project, item, evidence=[{"kind": "commit", "ref": "6bf2673"},
                                                 {"kind": "work_item", "ref": item["id"]}])
    assert [e["checked"] for e in ok["evidence"]] == ["shape", "exists"]


def test_a_rationale_is_at_most_three_sentences_and_a_reversible_decision_names_its_undo(
        eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        with pytest.raises(ValueError, match="at most 3 sentences"):
            _decide(c, project, item, rationale="One. Two. Three. Four.")
        with pytest.raises(ValueError, match="undo"):
            _decide(c, project, item, undo=None)
        three = _decide(c, project, item, rationale="One. Two. Three")
    assert ledger.sentences("One. Two. Three") == 3 and three["rationale"] == "One. Two. Three"


def test_a_decision_on_a_proposal_carries_the_consensus_that_settled_it(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="flaky_test",
                         fingerprint="fp:flaky-cart-test")["item"]
        c.execute(text("INSERT INTO ambient_autonomy (project_id, task_class, level, ceiling, "
                       "set_by) VALUES (:p, 'flaky_test', 'A2', 'A3', 'ceo')"), {"p": project})
        proposal = collab.post(c, item["id"], kind="proposal", sender="agent:qa",
                               body={"action": "start_workflow",
                                     "summary": "Quarantine test_cart behind a label"})
        collab.post(c, item["id"], kind="critique", sender="agent:software_engineer",
                    reply_to=proposal["id"], body={"position": "agree", "summary": "Safe."})
        record = collab.tally(c, item["id"], proposal["id"])
        row = ledger.record_decision(
            c, project_id=project, decided_by="agent:qa", question="Quarantine test_cart?",
            chosen="quarantine", options=["quarantine", "leave"], rationale="Failed 6 of 40.",
            reversibility="reversible", undo={"action": "unquarantine"},
            evidence=[{"kind": "message", "ref": proposal["id"]}], work_item_id=item["id"],
            action="start_workflow", consensus=record, proposal_id=proposal["id"])
    assert record["mode"] == "maker_checker" and record["result"] == "approved"
    assert row["consensus"]["participants"] == ["agent:qa", "agent:software_engineer"]
    assert row["proposal_id"] == proposal["id"] and row["autonomy_level"] == "A2"
