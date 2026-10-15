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
