"""New work named for an app changes that app: it needs code_task:steer, as Change it does.

PR #156 review A1: with `app=`, POST /v1/projects/{id}/work continues the app's lineage (its
branch, its open change) but was checked only for workflow_run:start. Change it on the same app
needs code_task:steer on its build. A person who may start work but not steer builds is refused
(403) when the work names an app, and still starts plain new work. Real Postgres, enforced.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests.test_new_work_goes_through_the_one_start_door import (  # noqa: F401 — fixtures
    client, project, world,
)

APP_RUN = "crun_steerapp01"


def _the_app(engine, project_id: str) -> None:
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id,project_id,status,phase,title,spec,state,finalize_ctx) "
            "VALUES (:id,:p,'completed','done','Steer App',CAST(:spec AS JSONB),'{}'::jsonb,"
            "CAST(:f AS JSONB)) ON CONFLICT (id) DO UPDATE SET project_id = EXCLUDED.project_id"),
            {"id": APP_RUN, "p": project_id, "f": json.dumps({"attempt": 1}),
             "spec": json.dumps({"instructions": "x", "owner": "acme", "repo": "steer",
                                 "base_branch": "main"})})


def test_a_start_only_person_cannot_build_onto_an_app(world, client, engine, project):  # noqa: F811
    _the_app(engine, project)
    starter = world.person("nw-start-only")
    role = world.custom_role("start-only", ("workflow_run:start", "project:read"))
    world.bind(starter, role, project=project)
    with world.acting_as(world.ctx(starter)):
        refused = client.post(f"/v1/projects/{project}/work",
                              json={"kind": "feature", "app": APP_RUN, "text": "Add a route."})
    assert refused.status_code == 403, refused.text
    assert "code_task:steer" in refused.text


def test_an_owner_is_not_refused_for_steering(world, client, engine, project):  # noqa: F811
    _the_app(engine, project)
    with world.acting_as(world.ctx(world.owner)):
        r = client.post(f"/v1/projects/{project}/work",
                        json={"kind": "feature", "app": APP_RUN, "text": "Add a route."})
    # Consent or readiness may still answer (the start door's own); never the steer refusal.
    assert r.status_code != 403 or "code_task:steer" not in r.text, r.text


# ── review B2: EVERY path that accepts extends_run_id asks for code_task:steer ────────────────

def _start_only(world, project):  # noqa: F811
    person = world.person("nw-start-only-b2")
    world.bind(person, world.custom_role("start-only-b2", ("workflow_run:start", "project:read")),
               project=project)
    return person


def test_the_start_door_with_extends_run_id_needs_steer(world, client, engine, project):  # noqa: F811
    _the_app(engine, project)
    starter = _start_only(world, project)
    body = {"project_id": project, "workflow_key": "minor_feature",
            "input": {"title": "Add a route", "directive": "Add a route.",
                      "extends_run_id": APP_RUN}}
    with world.acting_as(world.ctx(starter)):
        refused = client.post("/v1/delivery-workflows/start", json=body)
        # The same person may still start work that names no app.
        plain = client.post("/v1/delivery-workflows/start", json={
            **body, "input": {"title": "Add a route", "directive": "Add a route."}})
    assert refused.status_code == 403, refused.text
    assert "code_task:steer" in refused.text
    assert not (plain.status_code == 403 and "code_task:steer" in plain.text), plain.text


def test_create_code_task_onto_an_app_needs_steer(world, engine, project):  # noqa: F811
    """The MCP / agent-turn path (create_code_task(extends_run_id=…)) asks in the tool; a
    workflow's own build step (the runner's step authority) was asked at its start door."""
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.tools.builtin import _build_onto_existing_app
    from boss_engine.tools.registry import CURRENT_TOOL_AUTH

    _the_app(engine, project)
    starter = _start_only(world, project)
    with world.acting_as(world.ctx(starter)):
        token = CURRENT_TOOL_AUTH.set(world.ctx(starter))
        try:
            refused = _build_onto_existing_app(None, None, project, starter.id, {}, APP_RUN, {})
            assert refused["ok"] is False and refused["permission"] == "code_task:steer"
            # Inside a workflow step the start door already decided it: no second refusal here
            # (it goes on to the build, which needs a real context — not this test's business).
            marker = runner._IN_STEP_AUTHORITY.set(True)
            try:
                try:
                    _build_onto_existing_app(None, None, project, starter.id, {}, APP_RUN, {})
                except AttributeError:
                    pass                      # got past the check, to ctx.begin() on None
            finally:
                runner._IN_STEP_AUTHORITY.reset(marker)
        finally:
            CURRENT_TOOL_AUTH.reset(token)
