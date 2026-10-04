"""A catalogue default names an agent without pinning it, and the named agent is the one who builds.

Agent World §0.1 item 4: catalogue workflows name default role agents on their steps — Juno on
the panel, the Frontend or Backend Engineer on the build by app kind, Beacon on readiness —
"with Auto still allowed". Review of S-identity found the naming had made those agents
permanent: delete_agent answered 409 for pm, sre and engineering_captain (named on 20, 9 and 11
stock steps), and pausing Juno failed every stock run at its panel. And the build step named
the Engineering Captain on every template, so the new engineers' grants and voice never reached
a stock run.

A default BOSS put on a step is SOFT (`default_captain`): it never blocks a delete (the
workflows are re-staffed without the agent), and a paused, retired or missing one hands the
step back to Auto with the reason recorded. A name an operator chose holds exactly as before.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import step_agents, workflow_catalogue, workflows

PROJECT = "boss-dogfood"


def _seed(engine) -> None:
    with engine.begin() as c:
        workflow_catalogue.seed(c, PROJECT, actor="test-seed")


def _active(engine, workflow_key: str) -> dict:
    with engine.connect() as c:
        return c.execute(text(
            "SELECT v.spec FROM workflow_definitions d JOIN workflow_versions v "
            "ON v.id = d.active_version_id WHERE d.project_id = :p AND d.workflow_key = :k"),
            {"p": PROJECT, "k": workflow_key}).scalar()


def _active_ids(engine) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text(
            "SELECT workflow_key, active_version_id FROM workflow_definitions "
            "WHERE project_id = :p AND is_catalogue"), {"p": PROJECT}).all())


def _captains(spec: dict) -> dict:
    return {s["id"]: s.get("captain") for s in spec["steps"]}


def _lifecycle(client, key: str, state: str):
    return client.post(f"/v1/agents/{key}/lifecycle", json={
        "project_id": PROJECT, "actor": "ceo", "state": state, "reason": "test"})


def _step(engine, workflow_key: str, step_id: str) -> dict:
    return next(s for s in _active(engine, workflow_key)["steps"] if s["id"] == step_id)


# ── A default never blocks a delete ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["sre", "pm", "engineering_captain", "adversarial_reviewer"])
def test_a_shipped_agent_named_only_by_the_catalogue_can_still_be_deleted(client, engine, key):
    _seed(engine)
    with engine.connect() as c:
        from boss_engine.services import agents

        named = agents.steps_naming(c, PROJECT, key)
    assert named and all(n["soft"] for n in named if n["how"] == "runs it")

    deleted = client.delete(f"/v1/agents/{key}", params={"project_id": PROJECT,
                                                         "actor": "ceo"})
    # pm is also the spec_review approver on four templates: a real name, so it still blocks,
    # and the refusal names only those gates, never a step it merely captains by default.
    if key == "pm":
        assert deleted.status_code == 409, deleted.text
        detail = deleted.json()["detail"]
        assert "approves it" in detail and "runs it" not in detail
        return
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["workflows_restaffed"], "the workflows were re-staffed without it"
    for entry in workflow_catalogue.CATALOGUE:
        assert key not in _captains(_active(engine, entry["key"])).values(), entry["key"]

    # The next boot's seed agrees: the agent stays off, and nothing is published again.
    before = _active_ids(engine)
    with engine.begin() as c:
        workflow_catalogue.seed(c, PROJECT, actor="test-seed")
    assert _active_ids(engine) == before


def test_a_name_an_operator_chose_still_blocks_the_delete(client, engine):
    _seed(engine)
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=f"wf_{uuid4().hex[:8]}", name="Watched",
            description="", actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(c, project_id=PROJECT, definition_id=definition["id"],
                                  spec={"entry_step": "watch", "steps": [
                                      {"id": "watch", "kind": "capability", "captain": "sre",
                                       "binding": {"capability": "monitor", "inputs": {}},
                                       "next": []}]}, actor="ceo")
    refused = client.delete("/v1/agents/sre", params={"project_id": PROJECT, "actor": "ceo"})
    assert refused.status_code == 409, refused.text
    assert "Watched" in refused.json()["detail"]
    assert "Standard Product Delivery" not in refused.json()["detail"]


def test_choosing_a_different_agent_in_the_studio_makes_it_a_real_choice():
    step = {"id": "build", "kind": "capability", "captain": "engineering_captain",
            "default_captain": "engineering_captain",
            "binding": {"capability": "build", "inputs": {}}}
    assert step_agents.is_default_captain(step)
    assert not step_agents.is_default_captain({**step, "captain": "backend_engineer"})
    agent_step = {**step, "binding": {"agent_work": True, "inputs": {}}}
    assert not step_agents.is_default_captain(agent_step), "an agent step's name is a choice"


# ── A paused default hands the step back to Auto ─────────────────────────────────────────────


def test_pausing_the_default_agent_does_not_fail_every_stock_run(client, engine):
    _seed(engine)
    meeting = _step(engine, "standard_product_delivery", "meeting")
    with engine.begin() as c:
        assert step_agents.resolve(c, None, PROJECT, meeting, actor="ceo")["agent"] == "pm"
    try:
        assert _lifecycle(client, "pm", "paused").status_code == 200
        with engine.begin() as c:
            ran = step_agents.resolve(c, None, PROJECT, meeting, actor="ceo")
        assert ran["agent"] == "ceo" and ran["how"] == "run_actor"
        assert ran["default_skipped"] == "pm"
        assert "default agent, is paused, so it ran on Auto" in ran["why"]
    finally:
        _lifecycle(client, "pm", "active")


def test_a_paused_agent_an_operator_named_still_fails_fast(client, engine):
    chosen = {"id": "meeting", "kind": "capability", "captain": "pm",
              "binding": {"capability": "meeting", "inputs": {}}}
    try:
        assert _lifecycle(client, "pm", "paused").status_code == 200
        with engine.begin() as c, pytest.raises(step_agents.StepAgentError, match="paused"):
            step_agents.resolve(c, None, PROJECT, chosen, actor="ceo")
    finally:
        _lifecycle(client, "pm", "active")


def test_a_missing_default_is_not_a_start_requirement():
    from boss_engine.services.workflow_readiness import requirements

    spec = workflow_catalogue.entry_spec(next(
        e for e in workflow_catalogue.CATALOGUE if e["key"] == "standard_product_delivery"))
    needs = {item["key"] for item in requirements(spec)}
    assert "role:sre" not in needs and "role:engineering_captain" not in needs
    assert "role:release_manager" in needs, "the deploy gate's approver is a real need"


# ── The named agent is the one who builds ────────────────────────────────────────────────────


def test_the_build_is_staffed_by_app_kind(engine):
    _seed(engine)
    assert _step(engine, "ux_certification", "build")["captain"] == "frontend_engineer"
    for key in ("backend_service_setup", "data_migration", "third_party_integration"):
        assert _step(engine, key, "build")["captain"] == "backend_engineer", key
    assert _step(engine, "standard_product_delivery", "build")["captain"] == "engineering_captain"
    for entry in workflow_catalogue.CATALOGUE:
        for step in entry["steps"]:
            if step["id"] == "deploy":
                assert step["approval"]["approver_role"] == "release_manager", entry["key"]


def test_a_project_without_the_specialist_gets_the_captain_then_auto():
    entry = next(e for e in workflow_catalogue.CATALOGUE if e["key"] == "ux_certification")
    with_captain = workflow_catalogue._staffed_for(entry, frozenset({"engineering_captain"}))
    build = next(s for s in with_captain["steps"] if s["id"] == "build")
    assert build["captain"] == build["default_captain"] == "engineering_captain"
    bare = workflow_catalogue._staffed_for(entry, frozenset())
    build = next(s for s in bare["steps"] if s["id"] == "build")
    assert "captain" not in build and "default_captain" not in build


def test_a_code_task_started_as_an_agent_records_that_agent(engine):
    from boss_engine.tools.builtin import _building_agent

    with engine.begin() as c:
        agent_id = c.execute(text("SELECT agent_id FROM roles WHERE project_id = :p "
                                  "AND role_key = 'frontend_engineer'"), {"p": PROJECT}).scalar()
        built = _building_agent(c, PROJECT, "frontend_engineer")
        assert built["role_key"] == "frontend_engineer"
        assert built.get("agent_id") == agent_id
        assert _building_agent(c, PROJECT, "ceo") is None, "a person is not an agent"
        assert _building_agent(c, PROJECT, "no_such_agent") is None
