"""Autonomy is set per task class, earned from the track record, and some actions always need a
person (BOSSAPP-905; design §4 L5, §5, §9, §11.4).

* Every class starts at its conservative default (A1 propose; CI fix on BOSS PRs, CVE patch and
  the SRE staging mitigation at A2; TODO triage at A0).
* The gate answers per (project, class, action): allowed, needs a person, or observe only — and
  how agents must agree first, by risk.
* Merge, production deploy, external send and payment are never allowed by a level — not at A3,
  not by a squad, not by a policy — and the LEDGER refuses such a decision from anyone but a
  person, so a caller that skipped the gate is still stopped.
* A revert demotes the class at once; 20 decided proposals with ≥ 85% accepted and no revert
  SUGGEST a promotion that a person confirms.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services.ambient import ledger, policy
from boss_engine.services.ambient import task_classes as tc

from ._ambient_support import eng, open_item, project  # noqa: F401


def test_every_class_starts_conservative(eng, project):
    with eng.begin() as c:
        levels = {row["task_class"]: row["level"] for row in policy.matrix(c, project)}
    assert levels["ci_fix_boss_pr"] == "A2" and levels["security_patch"] == "A2"
    assert levels["incident_staging_mitigation"] == "A2"
    assert levels["todo_triage"] == "A0"
    others = {k: v for k, v in levels.items() if k not in {
        "ci_fix_boss_pr", "security_patch", "incident_staging_mitigation", "todo_triage"}}
    assert set(others.values()) == {"A1"}
    assert set(levels) == set(tc.TASK_CLASSES)


def test_the_gate_lets_a_class_act_only_up_to_its_level(eng, project):
    with eng.begin() as c:
        propose = policy.gate(c, project, "doc_drift", "propose")
        draft = policy.gate(c, project, "doc_drift", "open_draft_pr")
        observe = policy.gate(c, project, "todo_triage", "propose")
        staging = policy.gate(c, project, "incident_staging_mitigation", "staging_rollback")
    assert propose["allowed"] and not propose["needs_person"]
    assert propose["consensus_mode"] == "single"
    assert not draft["allowed"] and draft["needs_person"], "A1 may propose the PR, not open it"
    assert not observe["allowed"] and not observe["needs_person"], "A0 only observes"
    assert staging["allowed"] and staging["consensus_mode"] == "maker_checker"


@pytest.mark.parametrize("action", sorted(tc.ALWAYS_A_PERSON))
def test_merge_prod_deploy_send_and_pay_always_need_a_person_even_at_a3(eng, project, action):
    with eng.begin() as c:
        policy.set_level(c, project, "dependency_upgrade", actor="ceo", level="A3",
                         reason="earned")
        verdict = policy.gate(c, project, "dependency_upgrade", action)
        item = open_item(c, project, task_class="dependency_upgrade",
                         fingerprint=f"fp:dep-{action}")["item"]
        for decider in ("agent:software_engineer", "squad:" + item["id"], "policy:auto-merge"):
            with pytest.raises(ledger.GateRefused, match="always needs a person"):
                ledger.record_decision(
                    c, project_id=project, decided_by=decider, question=f"{action}?",
                    chosen="yes", rationale="Green CI.", reversibility="irreversible",
                    evidence=[{"kind": "work_item", "ref": item["id"]}],
                    work_item_id=item["id"], action=action)
        by_person = ledger.record_decision(
            c, project_id=project, decided_by="person:ceo", question=f"{action}?",
            chosen="yes", rationale="Reviewed it.", reversibility="irreversible",
            work_item_id=item["id"], action=action)
    assert verdict["level"] == "A3"
    assert not verdict["allowed"] and verdict["needs_person"]
    assert verdict["consensus_mode"] == "recommend_only"
    assert by_person["decided_by"] == "person:ceo" and by_person["action"] == action


def test_an_agent_decision_beyond_the_level_or_without_agreement_is_refused(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="doc_drift", fingerprint="fp:docs")["item"]
        base = dict(project_id=project, decided_by="agent:docs_writer", question="Open the PR?",
                    chosen="open", rationale="README names a removed env var.",
                    reversibility="reversible", undo={"action": "close_pr"},
                    evidence=[{"kind": "work_item", "ref": item["id"]}], work_item_id=item["id"])
        with pytest.raises(ledger.GateRefused, match="needs A2"):
            ledger.record_decision(c, action="open_draft_pr", **base)
        policy.set_level(c, project, "doc_drift", actor="ceo", level="A2")
        with pytest.raises(ledger.GateRefused, match="maker checker agreement"):
            ledger.record_decision(c, action="open_draft_pr", **base)
        ok = ledger.record_decision(c, action="open_draft_pr", consensus={
            "mode": "maker_checker", "result": "approved", "participants": []}, **base)
    assert ok["autonomy_level"] == "A2"


def test_a_level_never_passes_its_ceiling(eng, project):
    with eng.begin() as c:
        with pytest.raises(ValueError, match="no higher than A1"):
            policy.set_level(c, project, "iac_drift", actor="ceo", level="A2", ceiling="A2")
        policy.set_level(c, project, "flaky_test", actor="ceo", ceiling="A1")
        with pytest.raises(ValueError, match="above this project's ceiling"):
            policy.set_level(c, project, "flaky_test", actor="ceo", level="A2")
        with pytest.raises(ValueError):
            policy.set_level(c, project, "flaky_test", actor="ceo", level="A9")
        with pytest.raises(ValueError, match="unknown task class"):
            policy.gate(c, project, "world_domination", "propose")
        with pytest.raises(ValueError, match="action catalogue is code"):
            policy.gate(c, project, "flaky_test", "rm_rf")


def _decided(c, pid: str, item: dict, outcome: str) -> None:
    row = ledger.record_decision(
        c, project_id=pid, decided_by="agent:software_engineer", question="Bump lodash?",
        chosen="bump", rationale="Patch release.", reversibility="reversible",
        undo={"action": "revert"}, evidence=[{"kind": "work_item", "ref": item["id"]}],
        work_item_id=item["id"], task_class="dependency_upgrade", action="propose")
    ledger.record_outcome(c, row["act_id"], outcome=outcome)


def test_a_revert_demotes_the_class_at_once(eng, project):
    with eng.begin() as c:
        policy.set_level(c, project, "dependency_upgrade", actor="ceo", level="A2")
        item = open_item(c, project, task_class="dependency_upgrade", fingerprint="fp:dep")["item"]
        _decided(c, project, item, "accepted")
        _decided(c, project, item, "reverted")
        now_at = policy.level_for(c, project, "dependency_upgrade")
        change = policy.changes(c, project, task_class="dependency_upgrade")[0]
    assert now_at["level"] == "A1"
    assert change["kind"] == "demoted" and change["actor"] == "boss"
    assert "reverted" in change["reason"]


def test_a_strong_record_suggests_a_promotion_that_a_person_confirms(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="dependency_upgrade", fingerprint="fp:dep")["item"]
        for i in range(policy.PROMOTE_MIN_DECIDED):
            _decided(c, project, item, "accepted" if i < 18 else "dismissed")
        level = policy.level_for(c, project, "dependency_upgrade")["level"]
        suggested = [r for r in policy.matrix(c, project)
                     if r["task_class"] == "dependency_upgrade"][0]["suggested_level"]
        kinds = [ch["kind"] for ch in policy.changes(c, project, task_class="dependency_upgrade")]
        policy.set_level(c, project, "dependency_upgrade", actor="ceo", level="A2",
                         reason="accepted BOSS's suggestion")
        after = policy.level_for(c, project, "dependency_upgrade")
    assert level == "A1", "a promotion is never applied by BOSS itself"
    assert suggested == "A2" and kinds.count("promotion_suggested") == 1
    assert after["level"] == "A2" and after["set_by"] == "ceo"


def test_a_persons_own_decisions_never_count_toward_a_classs_record(eng, project):
    with eng.begin() as c:
        item = open_item(c, project, task_class="dependency_upgrade", fingerprint="fp:dep")["item"]
        row = ledger.record_decision(
            c, project_id=project, decided_by="person:ceo", question="Bump?", chosen="bump",
            rationale="Fine.", reversibility="compensable", work_item_id=item["id"])
        ledger.record_outcome(c, row["act_id"], outcome="reverted")
        record = policy.track_record(c, project, "dependency_upgrade")
        level = policy.level_for(c, project, "dependency_upgrade")["level"]
    assert record["decided"] == 0 and level == "A1"
