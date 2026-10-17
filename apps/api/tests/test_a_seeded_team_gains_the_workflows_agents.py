"""A TEAM NOBODY CHOSE GAINS THE AGENTS THE RUNNING WORKFLOW NAMES; A CHOSEN ONE IS LEFT ALONE.

HelpDesk AI's team was seeded on 12 Oct by an older run with BOSS's default software team. A
run of the D1 workflow on 16 Oct found "a team exists", added none of its own agents, and its
deploy was refused: "'release_manager' is not assigned to HelpDesk AI…". The operator had
chosen nothing; BOSS refused a decision nobody made.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import agent_assignment

from apps.api.tests.test_a_stack_deploy_step_is_approved_once_by_its_deployment import (  # noqa: F401 - fixtures
    HOME,
    _build,
    _deploy_run,
    _step,
    _tick,
    stack,
)


def _team(engine, app: str) -> dict[str, str]:
    with engine.begin() as c:
        return {r["role_key"]: r["source"] for r in agent_assignment.assigned(
            c, HOME, agent_assignment.app_key(app))}


def test_a_default_team_from_an_older_run_gains_the_deploy_approver_at_the_gate(
        client, engine, stack):
    app = f"Shop {uuid4().hex[:6]}"  # its own app: a team row must not leak into other tests
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'),
                         title=app)
    # An older run seeded the app with BOSS's software team AFTER this run started (so the
    # run-start seeding cannot be what saves it): only the gate's top-up can.
    with engine.begin() as c:
        c.execute(text("DELETE FROM project_agents WHERE project_id = :p AND app_key = :a"),
                  {"p": HOME, "a": agent_assignment.app_key(app)})
        agent_assignment.assign(c, project_id=HOME, app_name=app,
                                role_keys=["software_engineer", "qa"], actor="boss",
                                source=agent_assignment.DEFAULT)
    _tick(engine)

    step = _step(engine, run_id)
    assert "not assigned" not in (step["last_error"] or ""), step
    team = _team(engine, app)
    assert team.get("release_manager") == agent_assignment.DEFAULT
    assert {"software_engineer", "qa"} <= set(team)


def test_a_team_a_person_chose_is_never_topped_up(client, engine, stack):
    app = f"Shop {uuid4().hex[:6]}"
    with engine.begin() as c:
        agent_assignment.assign(c, project_id=HOME, app_name=app, role_keys=["pm"],
                                actor="ceo")
    run_id = _deploy_run(engine, _build(engine, '{"owner": "acme", "repo": "shop"}'),
                         title=app)
    _tick(engine)

    step = _step(engine, run_id)
    assert "release_manager" in (step["last_error"] or "") and "not assigned" in step["last_error"]
    assert "release_manager" not in _team(engine, app)
