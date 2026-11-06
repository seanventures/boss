"""The incident squad mitigates first, within its autonomy (BOSSAPP-910; design §12.5, §11.4).

* Staging: rolling back is A2, maker-checker. The reviewer agrees, the lead rolls back on its own
  and the ledger row carries the critic's agreement and the undo.
* Production: a squad vote AND a person. The squad's recommendation, with its dissent, becomes
  ONE Needs-you card; no agent ever records a production rollback (the ledger refuses it).
* The person's answer on the card resumes the squad: approve → roll back, start the fix, verify,
  close; reject → the person holds it; request changes → fix only, no rollback.
* A critic who objects stops a staging rollback and the squad asks a person.
* Recovery is verified: not recovered → the rollback's outcome is failed and the squad escalates;
  still waiting → the squad lets go and the tick finishes once the monitor has a verdict.
* No earlier healthy release → no rollback; the fix goes forward.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import incident_squad as squad
from boss_engine.services import incident_squad_demo as demo
from boss_engine.services.ambient import board, collab, ledger
from boss_engine.services.decisions import DecisionService

from ._sre_squad_support import add_person, eng, project  # noqa: F401


def _route(c, pid, env="production", mind=None, effects=None):
    prepared = demo.prepare(c, pid, environment=env)
    out = squad.route_incident(c, project_id=pid, item_id=prepared["item_id"],
                               mind=mind or demo.demo_mind(env),
                               effects=effects or demo.FixtureEffects())
    return prepared, out


def _decisions(c, pid, item_id, question=None):
    rows = ledger.list_decisions(c, pid, work_item_id=item_id)
    return [d for d in rows if question is None or d["question"] == question]


class _Recovery(demo.FixtureEffects):
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)

    def check_recovery(self, connection, *, project_id, ctx):
        status = self.verdicts.pop(0) if self.verdicts else "recovered"
        if status == "recovered":
            return super().check_recovery(connection, project_id=project_id, ctx=ctx)
        return {"status": status, "word": f"Still {status}: 5.9% of 900 requests",
                "evidence": [{"kind": "incident", "ref": ctx.incident_id}]}


def test_staging_rollback_is_automatic_once_a_critic_agrees(eng, project):
    with eng.begin() as c:
        prepared, out = _route(c, project, env="staging")
        rollback = _decisions(c, project, prepared["item_id"], squad.Q_MITIGATE)
        needs = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p"),
                          {"p": project}).scalar()
        item = board.get(c, prepared["item_id"])
    assert out["stage"] == "resolved"
    assert len(rollback) == 1
    r = rollback[0]
    assert (r["decided_by"], r["action"], r["chosen"]) == (
        "agent:sre", "staging_rollback", f"Roll back to {demo.PREVIOUS[:7]}")
    assert r["consensus"]["mode"] == "maker_checker" and r["consensus"]["result"] == "approved"
    assert r["undo"] == {"action": "redeploy", "commit": demo.SERVING}
    assert r["outcome"] == "succeeded"
    assert needs == 0
    assert item["lifecycle"] == "resolved" and item["resolved_by"] == "agent:sre"


def test_production_rollback_is_one_needs_you_card_with_the_squads_dissent(eng, project):
    with eng.begin() as c:
        prepared, out = _route(c, project)
        item = board.get(c, prepared["item_id"])
        card = c.execute(text("SELECT question, options, recommendation FROM decisions "
                              "WHERE project_id = :p"), {"p": project}).mappings().all()
        rows = _decisions(c, project, prepared["item_id"], squad.Q_MITIGATE)
    assert out["stage"] == "needs_you" and item["lifecycle"] == "needs_you"
    assert len(card) == 1
    assert [o["key"] for o in card[0]["options"]] == ["approve", "fix_only", "hold"]
    rec = card[0]["recommendation"]
    assert rec["action"] == squad.NEEDS_YOU_ACTION and rec["mitigation"] == "prod_rollback"
    assert rec["votes"] == "2/3" and "coupon" in rec["dissent"][0].lower()
    assert [(d["decided_by"], d["chosen"], d["action"]) for d in rows] == [
        (f"squad:{prepared['item_id']}", "Recommend rollback", "propose")]
    consensus = rows[0]["consensus"]
    assert consensus["mode"] == "squad_vote_and_person" and consensus["needs_person"]
    assert consensus["result"] == "approved"
    assert [d["party"] for d in consensus["dissent"]] == ["agent:adversarial_reviewer"]


def test_no_agent_may_record_a_production_rollback(eng, project):
    with eng.begin() as c:
        prepared, _ = _route(c, project)
        with pytest.raises(ledger.GateRefused):
            ledger.record_decision(
                c, project_id=project, decided_by="agent:sre", question="Roll back prod?",
                chosen="rollback", rationale="Fast.", reversibility="reversible",
                undo={"action": "redeploy"}, work_item_id=prepared["item_id"],
                action="prod_rollback", evidence=[{"kind": "commit", "ref": demo.SERVING}])


def test_approving_the_card_resumes_the_squad_to_recovery(eng, project):
    with eng.begin() as c:
        prepared, out = _route(c, project)
        add_person(c, project)
        DecisionService(get_settings()).transition(c, out["decision_id"], action="approve",
                                                   actor="ceo", note=None)
        item = board.get(c, prepared["item_id"])
        rows = _decisions(c, project, prepared["item_id"])
        links = {l["kind"] for l in board.links(c, prepared["item_id"])}
        inc = c.execute(text("SELECT status, lifecycle, mitigated_at FROM incidents "
                             "WHERE id = :i"), {"i": prepared["incident_id"]}).mappings().one()
    by_q = {d["question"]: d for d in rows}
    person = [d for d in rows if d["decided_by"] == "person:ceo"]
    assert [(d["chosen"], d["action"]) for d in person] == [
        ("Roll back + fix", "prod_rollback"), ("Start fix build", "start_workflow")]
    assert person[0]["outcome"] == "succeeded"
    squad_rec = next(d for d in rows if d["decided_by"].startswith("squad:"))
    assert squad_rec["outcome"] == "accepted"
    assert by_q[squad.Q_VERIFY]["chosen"] == "Recovered"
    assert item["lifecycle"] == "resolved"
    assert "pull_request" in links
    assert inc["status"] == "resolved" and inc["lifecycle"] == "resolved"
    assert inc["mitigated_at"] is not None


def test_rejecting_the_card_hands_the_incident_to_the_person(eng, project):
    with eng.begin() as c:
        prepared, out = _route(c, project)
        add_person(c, project)
        DecisionService(get_settings()).transition(c, out["decision_id"], action="reject",
                                                   actor="ceo", note="I'll hotfix it")
        item = board.get(c, prepared["item_id"])
        rows = _decisions(c, project, prepared["item_id"], squad.Q_MITIGATE)
    assert item["lease_kind"] == "person" and item["lease_owner"] == "person:ceo"
    assert [d["chosen"] for d in rows if d["decided_by"] == "person:ceo"] == ["Hold"]
    assert not any(d.get("action") == "prod_rollback" for d in rows)
    assert next(d for d in rows if d["decided_by"].startswith("squad:"))["outcome"] == \
        "dismissed"


def test_fix_only_skips_the_rollback_and_still_fixes(eng, project):
    with eng.begin() as c:
        prepared, _ = _route(c, project)
        out = squad.resume(c, project_id=project, item_id=prepared["item_id"],
                           answer="fix_only", actor="ceo", mind=demo.demo_mind(),
                           effects=demo.FixtureEffects())
        rows = _decisions(c, project, prepared["item_id"])
    assert out["stage"] == "resolved"
    assert not any(d.get("action") == "prod_rollback" for d in rows)
    assert [d["chosen"] for d in rows if d["decided_by"] == "person:ceo"] == [
        "Fix only", "Start fix build"]


def test_a_critic_who_objects_stops_a_staging_rollback(eng, project):
    mind = squad.ScriptedMind({("agent:adversarial_reviewer", "critique"): {
        "key": "object", "reason": "The coupon change ships in the same release."}})
    with eng.begin() as c:
        prepared, out = _route(c, project, env="staging", mind=mind)
        rows = _decisions(c, project, prepared["item_id"], squad.Q_MITIGATE)
    assert out["stage"] == "needs_you"
    assert [d["decided_by"].split(":")[0] for d in rows] == ["squad"]
    assert rows[0]["consensus"]["result"] == "rejected"


def test_a_rollback_that_does_not_recover_is_escalated(eng, project):
    with eng.begin() as c:
        prepared, out = _route(c, project, env="staging", effects=_Recovery("failing"))
        mitigation = [d for d in _decisions(c, project, prepared["item_id"], squad.Q_MITIGATE)
                      if d.get("action") == "staging_rollback"][0]
        escalations = collab.messages(c, prepared["item_id"], kinds=("escalate",))
    assert out["stage"] == "escalated"
    assert mitigation["outcome"] == "failed"
    assert escalations and "did not recover" in escalations[0]["body"]["reason"]


def test_while_recovery_is_unknown_the_squad_lets_go_and_the_tick_finishes(eng, project):
    effects = _Recovery("waiting", "waiting")
    with eng.begin() as c:
        prepared, out = _route(c, project, env="staging", effects=effects)
        waiting = board.get(c, prepared["item_id"])
        first = squad.tick(c, project_id=project, mind=demo.demo_mind("staging"),
                           effects=effects)
        second = squad.tick(c, project_id=project, mind=demo.demo_mind("staging"),
                            effects=effects)
        item = board.get(c, prepared["item_id"])
        watching = [m for m in collab.messages(c, prepared["item_id"], kinds=("finding",))
                    if m["body"]["summary"] == squad.WATCHING]
    assert out["stage"] == "verifying"
    assert waiting["lifecycle"] == "proposed" and waiting["lease_owner"] is None
    assert [r["stage"] for r in first] == ["verifying"]
    assert [r["stage"] for r in second] == ["resolved"]
    assert item["lifecycle"] == "resolved"
    assert len(watching) == 1


def test_without_an_earlier_healthy_release_the_fix_goes_forward(eng, project):
    with eng.begin() as c:
        prepared = demo.prepare(c, project, environment="staging")
        c.execute(text("UPDATE incidents SET finalize_ctx = finalize_ctx #- "
                       "'{deploy,previous_commit_sha}' WHERE id = :i"),
                  {"i": prepared["incident_id"]})
        out = squad.route_incident(c, project_id=project, item_id=prepared["item_id"],
                                   mind=demo.demo_mind("staging"),
                                   effects=demo.FixtureEffects())
        rows = _decisions(c, project, prepared["item_id"])
    by_q = {d["question"]: d for d in rows}
    assert out["stage"] == "resolved"
    assert by_q[squad.Q_MITIGATE]["chosen"] == "Fix forward"
    assert by_q[squad.Q_FIX]["decided_by"] == "agent:backend_engineer"
    assert by_q[squad.Q_FIX]["consensus"]["result"] == "approved"


def test_the_rule_mind_and_a_spent_model_budget_never_call_a_model(eng, project):
    mind = squad.AgentMind(settings=None, budget=0)
    with eng.begin() as c:
        prepared, out = _route(c, project, env="staging", mind=mind)
        cause = _decisions(c, project, prepared["item_id"], squad.Q_CAUSE)[0]
    assert mind.spent == 0
    assert out["stage"] == "resolved"
    assert "budget" in cause["rationale"]
