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
