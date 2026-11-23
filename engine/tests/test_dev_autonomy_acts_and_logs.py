# ruff: noqa: F811 — pytest fixtures imported from test_workflow_capability_runner
"""Dev autonomy decides development cards itself, with the same follow-on a person's Approve runs,
and logs who decided and why (BOSSAPP-1441).

The operator, 2026-11-23: "for local development I dont need to validate just make the output..
then human can validate it." Production, credentials, spend past the budget and the stops a person
set still wait; everything else is decided by the agent, named, with its undo.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import autonomy_gate, autonomy_policy
from boss_engine.services import workflow_capability_runner as runner

from .test_workflow_capability_runner import (  # noqa: F401
    Ctx, RecordingRegistry, _publish, _start, _steps, eng, project)


@pytest.fixture(autouse=True)
def _jobs_go_with_the_project(eng, project):
    """A decided card's follow-on queues real jobs; they go before the project does."""
    yield
    with eng.begin() as c:
        for table in ("jobs", "autonomy_policies"):
            column = "scope_id" if table == "autonomy_policies" else "project_id"
            c.execute(text(f"DELETE FROM {table} WHERE {column} = :p"), {"p": project})


def _card(c, project_id, rec, *, risk_tier=2, question=None):
    from boss_engine.services.core import create_blocking_decision

    return create_blocking_decision(
        c, project_id, None, question or f"q {uuid.uuid4().hex[:8]}", rec, {}, "test",
        risk_tier=risk_tier)


def _row(c, decision_id):
    row = dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": decision_id}).mappings().one())
    rec = row["recommendation"]
    row["recommendation"] = json.loads(rec) if isinstance(rec, str) else rec
    return row


def _target(c, project_id, environment, *, tier=None, ref="deploy@203.0.113.40:22"):
    target_id = f"dt_{uuid.uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        "target_ref, created_by, status, tier) VALUES (:i, :p, :a, 'o/r', :e, 'ssh_host', :ref, "
        "'test', 'active', :t)"),
        {"i": target_id, "p": project_id, "a": f"app_{target_id}", "e": environment, "ref": ref,
         "t": tier})
    return target_id


def test_a_development_card_is_decided_by_a_named_agent_and_logged(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"action": "decide", "summary": "pick the next step"})
        stamped = _row(c, decision_id)["recommendation"]["autonomy"]
        assert stamped["route"] == "act" and stamped["human_reviewed"] is False
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
        event = c.execute(text(
            "SELECT data FROM events WHERE entity_id = :d AND event_type = :e"),
            {"d": decision_id, "e": autonomy_gate.EVENT}).scalar()
    assert result["decided"] is True
    assert row["status"] == "approved"
    assert row["approved_by"] == "autonomy:dev:product_manager"
    assert "Decided by Product manager under dev autonomy" in row["resolution_note"]
    data = event if isinstance(event, dict) else json.loads(event)
    assert data["undo_kind"] and data["agent"] == "product_manager" and data["evidence"]


def test_an_approved_gate_releases_its_step_the_way_a_persons_answer_does(eng, project,
                                                                         monkeypatch):
    """Same follow-on: the spec_review gate decided by dev autonomy releases its step."""
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([]))
    settings = get_settings()
    with eng.begin() as c:
        _, version = _publish(c, project, "auto_gate", [
            {"id": "g", "kind": "capability",
             "binding": {"capability": "spec_review", "inputs": {}}, "next": []},
        ], "g")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        decision_id = c.execute(text(
            "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r"),
            {"r": run["id"]}).scalar()
        assert autonomy_gate.decide(c, settings, decision_id)["decided"] is True
        runner.tick(c, Ctx(eng), project_id=project)
        steps = _steps(c, run["id"])
    assert steps["g"]["status"] == "succeeded", steps["g"]


def test_production_and_unknown_targets_still_ask(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        prod = _target(c, project, "production")
        unknown = _target(c, project, "eu1")
        local = _target(c, project, "eu1", ref="deploy@localhost:22")
        staging = _target(c, project, "staging", tier="staging")
        answers = {}
        for name, target in (("prod", prod), ("unknown", unknown), ("local", local),
                             ("staging", staging)):
            found = autonomy_gate.route(c, {"project_id": project, "risk_tier": 2,
                                            "recommendation": {"action": "deploy_host",
                                                               "target_id": target}})
            answers[name] = found
        prod_card = _card(c, project, {"action": "deploy_host", "target_id": prod})
        refused = autonomy_gate.decide(c, settings, prod_card)
        still = _row(c, prod_card)
    assert not answers["prod"].acts and answers["prod"].cls == autonomy_gate.ASK_PROD_RELEASE
    assert not answers["unknown"].acts and "could not tell" in answers["unknown"].why
    assert answers["local"].acts and answers["staging"].acts
    assert refused["decided"] is False and still["status"] == "pending"
    assert still["recommendation"]["autonomy"]["route"] == "ask"


def test_a_merge_into_a_branch_that_ships_to_production_asks(eng, project):
    with eng.begin() as c:
        rel = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown"}})
        main = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "main", "repo": "o/unknown"}})
    assert rel.acts
    assert not main.acts and main.cls == autonomy_gate.ASK_PROD_RELEASE


def test_a_merge_with_failing_checks_is_never_decided_by_autonomy(eng, project):
    red = {"gates": [{"name": "tests", "status": "failed"}]}
    with eng.begin() as c:
        failing = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown",
            "checks_failing": red}})
        green = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "review_pull_request", "pr_base": "rel/rc11", "repo": "o/unknown"}})
    assert not failing.acts and failing.cls == autonomy_gate.EXPLICIT_STOP
    assert "checks are failing" in failing.why
    assert green.acts


def test_a_stop_a_person_set_is_honoured(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"action": "execute_agent_tool",
                                         "tool": "publish_document",
                                         "explicit_stop": "Review the plan first"})
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
    assert result["decided"] is False and row["status"] == "pending"
    assert row["recommendation"]["autonomy"]["class"] == autonomy_gate.EXPLICIT_STOP


def test_credentials_and_spend_still_wait(eng, project):
    with eng.begin() as c:
        secret = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "retry_when_secret_set"}})
        spend = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "increase_budget_or_defer"}})
        over = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "budget.override", "amount_usd": 500}})
        within = autonomy_gate.route(c, {"project_id": project, "recommendation": {
            "action": "budget.override", "amount_usd": 0.5}})
    assert (secret.acts, secret.cls) == (False, autonomy_gate.ASK_CREDENTIAL)
    assert (spend.acts, spend.cls) == (False, autonomy_gate.ASK_SPEND)
    assert not over.acts and within.acts


def test_a_project_that_says_ask_in_development_keeps_its_cards(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        autonomy_policy.save(c, kind=autonomy_policy.PROJECT, scope_id=project,
                             raw={"dev": "ask"}, actor="ceo")
        decision_id = _card(c, project, {"action": "decide"})
        result = autonomy_gate.decide(c, settings, decision_id)
        c.execute(text("DELETE FROM autonomy_policies WHERE scope_id = :p"), {"p": project})
    assert result["decided"] is False


def test_decided_for_you_lists_it_latest_first_with_its_undo(eng, project):
    settings = get_settings()
    with eng.begin() as c:
        first = _card(c, project, {"action": "decide"})
        autonomy_gate.decide(c, settings, first)
        second = _card(c, project, {"action": "resume_code_task", "run_id": "crun_none"})
        autonomy_gate.decide(c, settings, second)
        items = autonomy_gate.decided_for_you(c, project)
        with pytest.raises(autonomy_gate.UndoRefused):
            autonomy_gate.undo(c, settings, first, project_id=project, actor="ceo")
        reviewed = autonomy_gate.mark_reviewed(c, first, project_id=project, actor="ceo")
    ids = [i["decision_id"] for i in items]
    assert set(ids) >= {first, second}
    by_id = {i["decision_id"]: i for i in items}
    assert by_id[first]["undo"] is None and by_id[first]["undo_unavailable"]
    assert by_id[second]["undo"] == "send_back"
    assert reviewed["reviewed"] is True


def test_the_sweep_decides_a_card_written_without_the_sink(eng, project):
    settings = get_settings()
    decision_id = f"dec_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "cost_summary, default_action, status) VALUES (:i, :p, 'direct', "
            "CAST(:r AS JSONB), 2, '{}'::jsonb, 'hold', 'pending')"),
            {"i": decision_id, "p": project, "r": json.dumps({"action": "review_stalled_meeting"})})
    out = autonomy_gate.sweep(eng, settings, project_id=project)
    with eng.begin() as c:
        row = _row(c, decision_id)
    assert out["decided"] >= 1
    assert row["status"] == "approved" and row["approved_by"].startswith("autonomy:dev:")


def test_a_tier_three_card_waits_for_two_people_not_for_autonomy(eng, project):
    """#307 review P1-2: dev autonomy decides through transition(system_policy=True), which skips
    the two-person rule, so a card of a two-person tier is never decided by it."""
    settings = get_settings()
    with eng.begin() as c:
        decision_id = _card(c, project, {"capability": "security_review"}, risk_tier=3)
        result = autonomy_gate.decide(c, settings, decision_id)
        row = _row(c, decision_id)
    assert result["decided"] is False and "two-person" in result["why"]
    assert row["status"] == "pending"


def test_a_deploy_steps_answer_honours_its_author_and_its_own_target(eng, project):
    """#307 review P2: deploy_policy.step_answer — a step set to ask is a person's; the step's
    own target_id decides, not only its environment name."""
    from boss_engine.services import deploy_policy

    with eng.begin() as c:
        prod = _target(c, project, "eu1", tier="prod")
        local = _target(c, project, "eu1", ref="deploy@localhost:22")

        def step(**extra):
            inputs = {"environment": "staging", **extra.pop("inputs", {})}
            return {"id": "d", "kind": "capability",
                    "binding": {"capability": "deploy", "inputs": inputs}, **extra}

        plain = deploy_policy.step_answer(c, project, step())
        manual = deploy_policy.step_answer(c, project, step(approval={"mode": "manual"}))
        to_prod = deploy_policy.step_answer(c, project, step(inputs={"target_id": prod}))
        to_local = deploy_policy.step_answer(c, project, step(inputs={"target_id": local}))
        run_target = deploy_policy.step_answer(c, project, step(), {"target_id": prod})
    assert plain["who"] == "agent"
    assert manual["who"] == "you"
    assert to_prod["who"] == "you" and to_local["who"] == "agent"
    assert run_target["who"] == "you"
