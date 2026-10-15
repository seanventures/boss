"""Test runs with pinned data and re-running from a step, over HTTP (#512).

The rules are the engine's (services/workflow_test_runs.py, pinned in
engine/tests/test_a_workflow_can_be_test_run_and_rerun_from_a_step.py); these pin the doors:
a test run is started by workflow key with its pins, refuses what it was not told to run, is
left out of the ordinary run list and listed on its own; a re-run is started from a step of a
finished run; and neither door reaches a run or a version of another project.
"""
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


@pytest.fixture
def workflow_key(engine):
    key = f"trapi_{uuid4().hex[:10]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name=key, description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "a", "steps": [
                {"id": "a", "kind": "task", "next": ["b"]},
                {"id": "b", "kind": "task", "next": ["c"]},
                {"id": "c", "kind": "task"},
            ]})
    return key


def _steps(engine, run_id):
    with engine.begin() as c:
        rows = c.execute(text("SELECT step_id, status, output FROM workflow_step_runs "
                              "WHERE workflow_run_id=:r"), {"r": run_id}).mappings().all()
    return {r["step_id"]: dict(r) for r in rows}


def test_a_test_run_starts_with_its_pins_and_refuses_what_it_was_not_told_to_run(
        client, engine, workflow_key):
    response = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "pins": {"a": {"answer": 42}}})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["mode"] == "test" and run["pins"]["a"]["output"] == {"answer": 42}
    steps = _steps(engine, run["id"])
    assert steps["a"]["status"] == "succeeded" and steps["a"]["output"]["pinned"] is True
    assert steps["b"]["status"] == "failed"
    assert steps["b"]["output"]["reason"] == "test_run_refused"

    listed = client.get("/v1/delivery-workflows/runs", params={"project_id": PROJECT})
    assert run["id"] not in {r["id"] for r in listed.json()["runs"]}
    shown = client.get("/v1/delivery-workflows/runs",
                       params={"project_id": PROJECT, "include_test": "true"})
    mine = [r for r in shown.json()["runs"] if r["id"] == run["id"]]
    assert mine and mine[0]["mode"] == "test"
    tests = client.get("/v1/delivery-workflows/test-runs", params={"project_id": PROJECT})
    assert tests.status_code == 200, tests.text
    assert [r["id"] for r in tests.json()["items"]] == [run["id"]]


def test_a_bad_pin_is_refused_with_the_engines_sentence(client, workflow_key):
    response = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "pins": {"nope": {}}})
    assert response.status_code == 422
    assert "no such step" in response.text
    missing = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": "no_such_workflow"})
    assert missing.status_code == 404


def test_a_finished_run_runs_again_from_a_step(client, engine, workflow_key):
    started = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "pins": {"a": {"x": 1}, "b": {"y": 2}, "c": {"z": 3}}}).json()
    assert started["status"] == "completed", started
    response = client.post(f"/v1/delivery-workflows/runs/{started['id']}/rerun", json={
        "project_id": PROJECT, "actor": "ceo", "from_step": "c"})
    assert response.status_code == 201, response.text
    rerun = response.json()
    assert rerun["rerun_of"] == {"run_id": started["id"], "from_step": "c"}
    assert rerun["pins"]["b"]["source"]["run_id"] == started["id"]
    # Nothing before `c` is missing; `c` was the test's own pin and stays one.
    assert _steps(engine, rerun["id"])["c"]["output"]["pinned"] is True

    refused = client.post(f"/v1/delivery-workflows/runs/{started['id']}/rerun", json={
        "project_id": PROJECT, "actor": "ceo", "from_step": "zz"})
    assert refused.status_code == 422


def test_neither_door_reaches_another_projects_run(client, engine, workflow_key):
    started = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "pins": {"a": {}, "b": {}, "c": {}}}).json()
    other = f"p_{uuid4().hex[:10]}"
    with engine.begin() as c:
        workspace = c.execute(text("SELECT workspace_id FROM projects WHERE id=:p"),
                              {"p": PROJECT}).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'other', :i)"), {"i": other, "w": workspace})
    rerun = client.post(f"/v1/delivery-workflows/runs/{started['id']}/rerun", json={
        "project_id": other, "actor": "ceo", "from_step": "c"})
    assert rerun.status_code in (403, 404), rerun.text
    test = client.post("/v1/delivery-workflows/test-runs", json={
        "project_id": other, "actor": "ceo",
        "workflow_version_id": started["workflow_version_id"]})
    assert test.status_code in (403, 404), test.text


# ── a normal re-run goes through the start door ───────────────────────────────────────────

#: A channel BOSS cannot post to is missing on every install (as in
#: test_a_template_that_cannot_run_here_is_refused_at_start), so the workflow below is not
#: ready here whatever this database has connected.
NOWHERE = "slack:#announcements"


@pytest.fixture
def unready_run(engine):
    """A finished normal run of a workflow that cannot run here now: started past the door (as
    it could have been while it was ready), its one step failed."""
    key = f"unready_{uuid4().hex[:10]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name=key, description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        version = workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "tell",
                  "input_schema": {"properties": {"notify_channel": {"title": "Who to tell"}}},
                  "steps": [{"id": "tell", "kind": "capability",
                             "binding": {"capability": "notify",
                                         "inputs": {"message": "hello",
                                                    "channel": {"from_input":
                                                                "notify_channel"}}},
                             "next": []}]})
        run = workflows.start_run(
            c, project_id=PROJECT, actor="ceo", input_data={"notify_channel": NOWHERE},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
        c.execute(text("UPDATE workflow_step_runs SET status='failed', last_error='no' "
                       "WHERE workflow_run_id=:r"), {"r": run["id"]})
        c.execute(text("UPDATE workflow_runs SET status='failed' WHERE id=:r"),
                  {"r": run["id"]})
    return run["id"]


def _reruns_of(engine, run_id):
    with engine.begin() as c:
        return c.execute(text("SELECT count(*) FROM workflow_runs "
                              "WHERE rerun_of->>'run_id' = :r"), {"r": run_id}).scalar()


def test_a_normal_rerun_of_a_workflow_no_longer_ready_is_refused_by_the_start_door(
        client, engine, unready_run):
    refused = client.post(f"/v1/delivery-workflows/runs/{unready_run}/rerun", json={
        "project_id": PROJECT, "actor": "ceo", "from_step": "tell"})
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    # The door's own words and checklist, exactly as a fresh start is refused.
    assert "cannot run here yet" in detail["message"]
    assert [m["key"] for m in detail["readiness"]["missing"]] == [f"channel:{NOWHERE}"]
    assert _reruns_of(engine, unready_run) == 0
    # Starting anyway is the operator's choice here too, as at the start door.
    anyway = client.post(f"/v1/delivery-workflows/runs/{unready_run}/rerun", json={
        "project_id": PROJECT, "actor": "ceo", "from_step": "tell",
        "start_anyway": [f"channel:{NOWHERE}"]})
    assert anyway.status_code == 201, anyway.text
    assert _reruns_of(engine, unready_run) == 1


def test_a_normal_rerun_of_a_ready_workflow_starts(client, engine, workflow_key):
    with engine.begin() as c:
        version = c.execute(text(
            "SELECT d.active_version_id FROM workflow_definitions d "
            "WHERE d.project_id=:p AND d.workflow_key=:k"),
            {"p": PROJECT, "k": workflow_key}).scalar()
        run = workflows.start_run(c, project_id=PROJECT, actor="ceo", input_data={},
                                  dedupe_key=f"dk_{uuid4().hex[:10]}",
                                  workflow_version_id=version)
        c.execute(text("UPDATE workflow_step_runs SET status='succeeded', output='{\"a\": 1}' "
                       "WHERE workflow_run_id=:r AND step_id='a'"), {"r": run["id"]})
        c.execute(text("UPDATE workflow_step_runs SET status='failed' "
                       "WHERE workflow_run_id=:r AND step_id='b'"), {"r": run["id"]})
        c.execute(text("UPDATE workflow_runs SET status='failed' WHERE id=:r"),
                  {"r": run["id"]})
    started = client.post(f"/v1/delivery-workflows/runs/{run['id']}/rerun", json={
        "project_id": PROJECT, "actor": "ceo", "from_step": "b"})
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["mode"] == "normal" and body["rerun_of"]["from_step"] == "b"
    assert _steps(engine, body["id"])["b"]["status"] == "ready"
