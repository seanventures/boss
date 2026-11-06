"""Outcomes land on the ledger, and the scorecard's metrics add up (BOSSAPP-912; design §8,
§11.7; spec §9 — the ledger's outcome field is the one outcome store).

* A proposal nobody answered for 7 days is IGNORED; a snooze, a take-over or a person's
  decision on the item is an answer. A resolved item whose problem opened again within 14 days
  RECURRED. A change an agent started is REVERTED at once, or MERGED once it held 14 days.
  The sweep is idempotent.
* The metrics are computed from those rows: acceptance %, noise %, override %, revert %,
  decisions by level and decider, time to a human decision, MTTM/MTTR from incidents, cost per
  resolved item and per merged change, the share of work BOSS started — per project and per
  task class — and they ride on the one scorecard endpoint.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from boss_engine.services.ambient import board, collab, ledger, outcomes, read_model, roadmap
from boss_engine.services.ambient import roadmap_demo as demo
from boss_engine.services.ambient._common import now

from ._ambient_support import open_item
from ._learning_support import eng, project  # noqa: F401

DAY = timedelta(days=1)


def _propose(c, pid, item_id, *, decided_by="agent:docs_writer", action="propose",
             task_class=None, **kw):
    return ledger.record_decision(
        c, project_id=pid, decided_by=decided_by, question="Fix the README?",
        chosen="propose_fix", rationale="It names a removed env var.",
        reversibility="reversible", undo={"action": "dismiss"},
        evidence=[{"kind": "work_item", "ref": item_id}], work_item_id=item_id,
        task_class=task_class, action=action, **kw)


def _doc_item(c, pid, fp):
    return open_item(c, pid, fingerprint=fp, task_class="doc_drift")["item"]


def test_a_proposal_nobody_answered_for_seven_days_is_ignored_and_an_answer_is_not(
        eng, project):
    with eng.begin() as c:
        quiet = _doc_item(c, project, "fp:quiet")
        answered = _doc_item(c, project, "fp:answered")
        taken = _doc_item(c, project, "fp:taken")
        a = _propose(c, project, quiet["id"])
        b = _propose(c, project, answered["id"])
        t = _propose(c, project, taken["id"])
        ledger.record_decision(c, project_id=project, decided_by="person:ceo",
                               question="Fix the README?", chosen="later",
                               rationale="After the release.", reversibility="compensable",
                               work_item_id=answered["id"])
        board.take_over(c, taken["id"], actor="ceo")
        early = outcomes.sweep(c, project_id=project, at=now() + 6 * DAY)
        late = outcomes.sweep(c, project_id=project, at=now() + 8 * DAY)
        again = outcomes.sweep(c, project_id=project, at=now() + 9 * DAY)
        rows = {r["act_id"]: r["outcome"] for r in ledger.list_decisions(c, project)}
    assert early["ignored"] == 0 and late["ignored"] == 1 and again["ignored"] == 0
    assert rows[a["act_id"]] == "ignored"
    assert rows[b["act_id"]] is None and rows[t["act_id"]] is None


def test_an_ignored_roadmap_proposal_teaches_its_detector(eng, project):
    with eng.begin() as c:
        for d in (1, 3):
            demo.put_incident(c, project, title="Timeout", cause_key="x.timeout",
                              at=now() - d * DAY)
        roadmap.run(c, project, only=["incident_cause"])
        outcomes.sweep(c, project_id=project, at=now() + 8 * DAY)
        stats = roadmap.detector_stats(c, project)["incident_cause"]
        label = c.execute(text("SELECT outcome FROM learning_examples WHERE project_id = :p "
                               "AND agent = 'roadmap'"), {"p": project}).scalar()
    assert stats["ignored"] == 1 and stats["weight"] < 1.0 and label == "ignored"


def test_a_resolved_problem_that_opens_again_within_two_weeks_recurred(eng, project):
    with eng.begin() as c:
        first = _doc_item(c, project, "fp:again")
        fix = _propose(c, project, first["id"])
        board.finish(c, first["id"], actor="boss", lifecycle="resolved", outcome="fixed")
        _doc_item(c, project, "fp:again")
        other = _doc_item(c, project, "fp:once")
        held = _propose(c, project, other["id"])
        board.finish(c, other["id"], actor="boss", lifecycle="resolved", outcome="fixed")
        done = outcomes.sweep(c, project_id=project)
        rows = {r["act_id"]: r for r in ledger.list_decisions(c, project)}
    assert done["recurred"] == 1
    assert rows[fix["act_id"]]["outcome"] == "recurred"
    assert rows[fix["act_id"]]["outcome_ref"].startswith("work_item:")
    assert rows[held["act_id"]]["outcome"] is None


def _agent_pr(c, pid, fp, unit):
    item = open_item(c, pid, fingerprint=fp, task_class="ci_fix_boss_pr")["item"]
    prop = collab.post(c, item["id"], kind="proposal", sender="agent:software_engineer",
                       body={"action": "open_draft_pr", "summary": "fix the flaky import"})
    collab.post(c, item["id"], kind="critique", sender="agent:qa", reply_to=prop["id"],
                body={"position": "agree", "summary": "small and tested"})
    record = collab.tally(c, item["id"], prop["id"])
    decision = _propose(c, pid, item["id"], decided_by="agent:software_engineer",
                        action="open_draft_pr", task_class="ci_fix_boss_pr",
                        consensus=record, proposal_id=prop["id"])
    board.link(c, item["id"], "code_task", unit)
    return decision


def test_an_agents_change_is_reverted_at_once_or_merged_after_it_held_two_weeks(eng, project):
    with eng.begin() as c:
        kept = demo.put_build(c, project)
        undone = demo.put_build(c, project)
        a = _agent_pr(c, project, "fp:kept", kept)
        b = _agent_pr(c, project, "fp:undone", undone)
        demo.put_scorecard(c, project, kept, cost_usd=4, pr_merged=True, pr_reverted=False,
                           at=now() - DAY)
        demo.put_scorecard(c, project, undone, cost_usd=4, pr_merged=True, pr_reverted=True,
                           at=now() - DAY)
        soon = outcomes.sweep(c, project_id=project)
        later = outcomes.sweep(c, project_id=project, at=now() + 15 * DAY)
        rows = {r["act_id"]: r["outcome"] for r in ledger.list_decisions(c, project)}
        level = c.execute(text("SELECT level FROM ambient_autonomy WHERE project_id = :p AND "
                               "task_class = 'ci_fix_boss_pr'"), {"p": project}).scalar()
    assert soon == {"ignored": 0, "recurred": 0, "merged": 0, "reverted": 1}
    assert later["merged"] == 1
    assert rows[a["act_id"]] == "merged" and rows[b["act_id"]] == "reverted"
    assert level == "A1"                     # a revert demotes the class at once (A2 -> A1)


def test_the_metrics_add_up_from_the_rows(eng, project):
    with eng.begin() as c:
        items = [_doc_item(c, project, f"fp:m{n}") for n in range(4)]
        acts = [_propose(c, project, i["id"]) for i in items]
        ledger.record_outcome(c, acts[0]["act_id"], outcome="accepted")
        ledger.record_outcome(c, acts[1]["act_id"], outcome="dismissed")
        ledger.override(c, acts[2]["act_id"], actor="ceo", chosen="ignore",
                        rationale="Docs are frozen.")
        board.add_cost(c, items[0]["id"], 3_000_000)
        board.finish(c, items[0]["id"], actor="boss", lifecycle="resolved", outcome="fixed")
        demo.put_incident(c, project, title="Down", at=now() - DAY, mitigated_after_min=10,
                          resolved_after_min=40)
        demo.put_incident(c, project, title="Slow", at=now() - DAY, mitigated_after_min=20,
                          resolved_after_min=80)
        demo.put_build(c, project)
        m = outcomes.metrics(c, project, days=7)
        only_docs = outcomes.metrics(c, project, days=7, task_class="doc_drift")
        none = outcomes.metrics(c, project, days=7, task_class="flaky_test")
        card = read_model.scorecard(c, project, days=7)
    assert m["decided"] == 4 and m["with_outcome"] == 3 and m["pending"] == 1
    assert m["acceptance_pct"] == 33.3 and m["noise_pct"] == 33.3
    assert m["override_pct"] == 25.0 and m["revert_pct"] is None
    assert m["decisions_by_decider"] == {"agent": 4, "person": 1}
    assert sum(m["decisions_by_level"].values()) == 5
    assert m["incidents"]["n"] == 2
    assert m["incidents"]["mttm_minutes"]["median"] == 15.0
    assert m["incidents"]["mttr_minutes"]["median"] == 60.0
    assert m["cost"]["per_resolved_item_usd"] == 3.0
    assert m["ambient_share"]["work_items"] == {"boss": 4, "all": 4, "pct": 100.0}
    assert m["ambient_share"]["builds"] == {"boss": 0, "all": 1, "pct": 0.0}
    assert only_docs["decided"] == 4 and none["decided"] == 0
    assert [r["task_class"] for r in m["by_task_class"]] == ["doc_drift"]
    assert card["outcomes"]["acceptance_pct"] == 33.3
    assert "recommendations" in card["outcomes"]


def test_time_to_a_human_decision_counts_needs_you_answers(eng, project):
    with eng.begin() as c:
        item = open_item(c, project)["item"]
        board.needs_you(c, item["id"], actor="boss", question="Roll back?",
                        recommendation={"action": "staging_rollback"})
        c.execute(text("UPDATE work_items SET needs_you_at = now() - interval '30 minutes' "
                       "WHERE id = :w"), {"w": item["id"]})
        board.answered(c, item["id"], actor="ceo")
        m = outcomes.metrics(c, project, days=7)
    assert m["time_to_human_decision_minutes"]["n"] == 1
    assert 29 <= m["time_to_human_decision_minutes"]["median"] <= 31
