"""An insecure template's lab project builds, and its run can be found (tester round 2, N2).

Seen: applying banking-starter-insecure made the lab project and its workflow run, but the run's
build step stayed `ready` for 15+ minutes with no code task, and recheck found no blocker. The
worker advances workflows for the ONE project it is configured for, and a lab is a project of
its own, so nothing ever ticked it. The run was also in no Workflows or Factory anyone could open.

  * the capability tick advances every project with live workflow work, whichever one the
    worker names, each on its own (one that cannot advance never stops the rest);
  * the source project lists its labs (GET /v1/templates/labs), so the console links to each.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _lab_project(engine, source: str = PROJECT) -> str:
    pid = _uid("proj")
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"), {"p": source}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :n, :s)"),
                   {"i": pid, "w": ws, "n": "Training bank lab (insecure by design)", "s": pid.replace("_", "-")})
        db.execute(text("INSERT INTO project_settings (project_id, key, value, updated_by) "
                        "VALUES (:p, 'template_lab', CAST(:v AS JSONB), 'test')"),
                   {"p": pid, "v": json.dumps({"insecure_by_design": True, "source_project_id": source,
                                               "app_key": "training bank", "app_name": "Training bank",
                                               "template_key": "banking-starter-insecure"})})
    return pid


def _live_run(engine, project_id: str) -> str:
    d, v, r = _uid("wfd"), _uid("wfv"), _uid("wfr")
    spec = {"entry_step": "build", "input_schema": {"directive": "string", "title": "string"},
            "steps": [{"id": "build", "kind": "capability", "next": [],
                       "binding": {"capability": "build", "inputs": {
                           "title": {"from_input": "title"}, "instructions": {"from_input": "directive"}}}}]}
    with engine.begin() as db:
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, namespace, created_by) "
                        "VALUES (:d, :p, :k, 'Fast fix', 'product', 'ceo')"), {"d": d, "p": project_id, "k": d})
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, "
                        "content_hash, created_by) VALUES (:v, :p, :d, 1, CAST(:s AS JSONB), 'h', 'ceo')"),
                   {"v": v, "p": project_id, "d": d, "s": json.dumps(spec)})
        db.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
                        "actor, status, input) VALUES (:r, :p, :d, :v, 'ceo', 'running', '{}'::jsonb)"),
                   {"r": r, "p": project_id, "d": d, "v": v})
    return r


def test_the_tick_reaches_every_project_with_live_work(client, engine):
    from boss_engine.services import workflow_capability_runner as runner

    lab = _lab_project(engine)
    _live_run(engine, lab)
    with engine.begin() as db:
        projects = runner.projects_with_work(db, PROJECT)
    assert projects[0] == PROJECT and lab in projects

    ticked = client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": PROJECT})
    assert ticked.status_code == 200, ticked.text
    assert lab in (ticked.json().get("other_projects") or {}), ticked.json()


def test_a_project_with_nothing_live_is_not_ticked(engine):
    from boss_engine.services import workflow_capability_runner as runner

    quiet = _lab_project(engine)
    with engine.begin() as db:
        assert quiet not in runner.projects_with_work(db, PROJECT)


def test_the_source_project_lists_its_labs(engine):
    from fastapi.testclient import TestClient

    from apps.api.app import main
    from apps.api.tests.identity_fixtures import IdentityWorld

    world = IdentityWorld.create(engine)
    viewer = world.person("lab-viewer")
    world.bind(viewer, "viewer")
    lab = _lab_project(engine, source=world.project_a)
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(viewer)):
        r = client.get("/v1/templates/labs", params={"project_id": world.project_a})
    assert r.status_code == 200, r.text
    hit = [x for x in r.json()["labs"] if x["project_id"] == lab]
    assert hit and hit[0]["name"] == "Training bank lab (insecure by design)"
    assert hit[0]["app_name"] == "Training bank"
