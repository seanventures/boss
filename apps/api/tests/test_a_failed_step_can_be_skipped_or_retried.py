"""The operator's two ways past a failed workflow step, over HTTP: retry it, or skip and proceed.

On boss-1 (2026-10-02) a delivery workflow's research step failed and the runner recorded it as
succeeded, so the run walked on into a panel, an auto-approved spec and a build. A failed step
now STOPS its run for every kind of step, and the product owner asked for the other half: "there
should be a button to skip and proceed." These are that button's endpoint and its sibling's,
with the same project authority as every other run action (`workflow_execute`).
"""
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


@pytest.fixture
def failed_run(engine):
    """A real published workflow — research → notify, plus an approval gate on another branch —
    started, with research failed the way the runner now leaves it."""
    key = f"skip_{uuid4().hex[:10]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name="Skip", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        version = workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "research", "steps": [
                {"id": "research", "kind": "capability",
                 "binding": {"capability": "research", "inputs": {"seed": "x"}},
                 "next": ["notify"]},
                {"id": "notify", "kind": "capability",
                 "binding": {"capability": "notify", "inputs": {"message": "on"}},
                 "next": ["approve"]},
                {"id": "approve", "kind": "capability",
                 "binding": {"capability": "spec_review", "inputs": {}}, "next": []},
            ]})
        run = workflows.start_run(
            c, project_id=PROJECT, actor="ceo", input_data={},
            dedupe_key=f"dk_{uuid4().hex[:10]}", workflow_version_id=version["id"])
        c.execute(text(
            "UPDATE workflow_step_runs SET status='failed', "
            "last_error='the discovery it started finished as ''cancelled''' "
            "WHERE workflow_run_id=:r AND step_id='research'"), {"r": run["id"]})
        c.execute(text("UPDATE workflow_runs SET status='failed', error='research died' "
                       "WHERE id=:r"), {"r": run["id"]})
    return run["id"]


def _step(engine, run_id, step_id):
    with engine.begin() as c:
        return c.execute(text(
            "SELECT status, output FROM workflow_step_runs "
            "WHERE workflow_run_id=:r AND step_id=:s"), {"r": run_id, "s": step_id}
        ).mappings().first()


def _events(engine, run_id, event_type):
    with engine.begin() as c:
        return c.execute(text(
            "SELECT actor, data FROM events WHERE entity_id=:r AND event_type=:t"),
            {"r": run_id, "t": event_type}).mappings().all()


def test_skip_and_proceed_marks_the_step_skipped_with_who_and_why_and_the_run_goes_on(
        client, engine, failed_run):
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research",
        "reason": "the directive is specific enough"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "skipped"

    research = _step(engine, failed_run, "research")
    assert research["status"] == "skipped", "a skipped step must never read as succeeded"
    assert research["output"]["skip_reason"] == "the directive is specific enough"
    assert research["output"]["skipped_from"] == "failed"
    assert _step(engine, failed_run, "notify")["status"] == "ready", "the run did not proceed"
    events = _events(engine, failed_run, "workflow.step_skipped")
    assert len(events) == 1
    assert events[0]["data"]["reason"] == "the directive is specific enough"
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                           {"r": failed_run}).scalar()
    assert status == "running"


def test_skip_needs_a_reason(client, engine, failed_run):
    """Without one, "why did this run go on without research?" has no answer in the record."""
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research", "reason": "  "})
    assert response.status_code == 422, response.text
    assert _step(engine, failed_run, "research")["status"] == "failed"


def test_skip_refuses_a_step_that_did_not_fail(client, engine, failed_run):
    """`notify` has not run. Skipping a live or pending step would abandon work, not route
    around a failure."""
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "notify", "reason": "no"})
    assert response.status_code == 409, response.text


def test_skip_refuses_an_approval_gate(client, engine, failed_run):
    """A gate that "failed" is a person who said no; a skip would turn that into a yes."""
    with engine.begin() as c:
        c.execute(text("UPDATE workflow_step_runs SET status='failed', "
                       "last_error='a person rejected this step' "
                       "WHERE workflow_run_id=:r AND step_id='approve'"), {"r": failed_run})
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "approve", "reason": "ship it"})
    assert response.status_code == 409, response.text
    assert "approval" in response.json()["detail"]


def test_an_unknown_step_is_not_found(client, failed_run):
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/skip-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "Do research", "reason": "r"})
    assert response.status_code == 404, response.text


def test_retry_requeues_the_failed_step_revives_the_run_and_is_recorded(
        client, engine, failed_run):
    """Retry is one implementation shared with the agent's tool; from the console it now
    leaves the same `workflow.step_retried` trace the agent's retry always did."""
    response = client.post(f"/v1/delivery-workflows/runs/{failed_run}/retry-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    assert _step(engine, failed_run, "research")["status"] == "ready"
    with engine.begin() as c:
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                           {"r": failed_run}).scalar()
    assert status == "running"
    assert len(_events(engine, failed_run, "workflow.step_retried")) == 1

    again = client.post(f"/v1/delivery-workflows/runs/{failed_run}/retry-step", json={
        "project_id": PROJECT, "actor": "ceo", "step_id": "research"})
    assert again.status_code == 409, "re-queueing a ready step would run it twice"
