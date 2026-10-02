"""Every door that starts a run drops a caller's claim that work already happened.

THE INCIDENT (boss-1, 2026-10-02): a run built on research that had failed. The HTTP start
endpoint stripped caller-supplied `adopt_*` keys; the review of 0fd6c45 found the agent's
`start_workflow` copied them through, that `from_discovery` accepted a completed dossier with no
findings, and that naming both `from_run` and `from_discovery` skipped the `from_run` rule.
`workflows.start_run` now drops every `adopt_*` key itself — so each entry point below is held
to it over HTTP — and every named source goes through `workflow_run_actions.research_to_adopt`.
"""
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"


@pytest.fixture
def published(engine):
    key = f"adopt_{uuid4().hex[:10]}"
    with engine.begin() as c:
        definition = workflows.create_definition(
            c, project_id=PROJECT, workflow_key=key, name="Adopt", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        version = workflows.publish_version(
            c, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "say", "steps": [
                {"id": "say", "kind": "capability",
                 "binding": {"capability": "notify", "inputs": {"message": "on"}}, "next": []},
            ]})
    return key, version["id"]


def _stored(engine, run_id):
    with engine.begin() as c:
        return c.execute(text("SELECT input FROM workflow_runs WHERE id=:r"),
                         {"r": run_id}).scalar()


def _dossier(engine, status, state):
    discovery_id = f"drun_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO discovery_runs (id, project_id, title, status, phase, state) "
            "VALUES (:id, :p, 'research', :s, 'done', CAST(:state AS JSONB))"),
            {"id": discovery_id, "p": PROJECT, "s": status, "state": json.dumps(state)})
    return discovery_id


def test_the_console_start_drops_an_adoption_in_its_input(client, engine, published):
    key, _ = published
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "input": {"directive": "build it", "adopt_discovery_run_id": "drun_dead"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("ignored_input") == ["adopt_discovery_run_id"]
    assert "adopt_discovery_run_id" not in _stored(engine, body["run"]["id"])


def test_the_raw_run_api_drops_one_too_and_says_what_the_run_offers(client, engine, published):
    """POST /v1/workflow-runs reaches `start_run` with the caller's input untouched — the
    central drop is what holds it. Its response carries the live run's actions (Stop), not an
    empty list that would read as "nothing is allowed"."""
    _, version_id = published
    response = client.post("/v1/workflow-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_version_id": version_id,
        "dedupe_key": f"dk_{uuid4().hex[:10]}",
        "input": {"directive": "build it", "adopt_code_task_run_id": "crun_yesterday"}})
    assert response.status_code == 201, response.text
    body = response.json()
    assert "adopt_code_task_run_id" not in _stored(engine, body["id"])
    assert {a["key"] for a in body["actions"]} >= {"stop"}
    assert body["standing"] == "active"


def test_naming_both_sources_is_refused(client, engine, published):
    key, _ = published
    good = _dossier(engine, "completed", {"signal_findings": ["buyers want X"]})
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "from_run": "wfr_anything", "from_discovery": good, "input": {"directive": "x"}})
    assert response.status_code == 400, response.text
    assert "not both" in response.json()["detail"]


def test_research_that_found_nothing_is_not_built_on(client, engine, published):
    key, _ = published
    empty = _dossier(engine, "completed", {})
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "from_discovery": empty, "input": {"directive": "x"}})
    assert response.status_code == 409, response.text
    assert "no findings" in response.json()["detail"]


def test_research_with_findings_still_is(client, engine, published):
    """The behaviour that worked keeps working."""
    key, _ = published
    good = _dossier(engine, "completed", {"signal_findings": ["buyers want X"]})
    response = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "from_discovery": good, "input": {"directive": "x"}})
    assert response.status_code == 200, response.text
    assert _stored(engine, response.json()["run"]["id"])["adopt_discovery_run_id"] == good
