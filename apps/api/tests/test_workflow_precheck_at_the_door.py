"""The workflow pre-check at the API (services.workflow_precheck).

* POST /v1/workflows/{id}/precheck answers per step, read-only;
* by default (`report` mode) a start whose pre-check says a step will fail goes ahead, with the
  pre-check in the response, on the run, and as the run's activity line;
* in `enforce` mode it is refused 409 `precheck_failed` with the whole pre-check, and goes ahead
  when the operator accepts the check or skips its step;
* what was accepted or skipped is recorded on the run (its input's `_precheck`, written by the
  server only, and a `workflow.precheck_accepted` event) with who and why;
* a skip of a step that protects a deploy is refused 422 `skip_refused`.
"""
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_precheck, workflows

PROJECT = "boss-dogfood"
STEPS = [
    {"id": "review", "kind": "capability", "next": ["deploy"],
     "binding": {"capability": "review", "inputs": {}}},
    {"id": "deploy", "kind": "capability", "next": ["note"],
     "binding": {"capability": "report", "inputs": {}}},
    {"id": "note", "kind": "capability", "next": [],
     "binding": {"capability": "report", "inputs": {}}},
]


@pytest.fixture
def key(engine):
    key = f"precheck_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Precheck", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "review", "steps": STEPS})
    return key


@pytest.fixture
def failing(monkeypatch):
    """The pre-check says `note` will fail (whatever the install looks like)."""
    real = workflow_precheck.run_checks

    def run_checks(ctx, only_fast=False):
        report = real(ctx, only_fast=only_fast)
        report["summary"]["blocking"] = ["note:sandbox.disk"]
        return report
    monkeypatch.setattr(workflow_precheck, "run_checks", run_checks)
    monkeypatch.setenv(workflow_precheck.GATE_ENV, "enforce")


def _start(client, key, **extra):
    return client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "input": {"directive": "go", "title": "Precheck", **extra.pop("input", {})}, **extra})


def test_the_precheck_endpoint_answers_per_step(client, key):
    r = client.post(f"/v1/workflows/{key}/precheck",
                    json={"project_id": PROJECT, "offline": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkpoint"] == "pre_run" and body["workflow_key"] == key
    for step in body["steps"]:
        for check in step["checks"]:
            assert check["verdict"] in workflow_precheck.VERDICTS
    assert client.post("/v1/workflows/nope/precheck",
                       json={"project_id": PROJECT}).status_code == 404


def test_the_precheck_reads_behaviour_and_lists_fixes_without_applying_them(client, key):
    r = client.post(f"/v1/workflows/{key}/precheck",
                    json={"project_id": PROJECT, "offline": True, "history_days": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"(history)", "(logs)", "(data)"} <= {s["step_id"] for s in body["steps"]}
    assert all(not f["applied"] for f in body["fixes"]) and isinstance(body["explain"], list)
    only = client.post(f"/v1/workflows/{key}/precheck",
                       json={"project_id": PROJECT, "offline": True, "families": ["data"]})
    assert {s["step_id"] for s in only.json()["steps"]} & {"(history)", "(logs)"} == set()


def test_fix_true_is_refused_on_the_read_route_and_taken_on_the_fix_route(client, key):
    r = client.post(f"/v1/workflows/{key}/precheck",
                    json={"project_id": PROJECT, "offline": True, "fix": True})
    assert r.status_code == 422 and "precheck/fix" in r.text
    planned = client.post(f"/v1/workflows/{key}/precheck/fix",
                          json={"project_id": PROJECT, "offline": True, "families": []})
    assert planned.status_code == 200, planned.text
    assert all(not f["applied"] for f in planned.json()["fixes"])   # fix defaults to false


def test_a_start_that_will_fail_is_refused_with_the_precheck(client, key, failing):
    r = _start(client, key)
    assert r.status_code == 409, r.text
    detail = r.json().get("detail") or r.json()
    assert detail["error"] == "precheck_failed"
    assert detail["unaccepted"] == ["note:sandbox.disk"]
    assert detail["precheck"]["steps"] is not None


def test_accepting_starts_and_records_who_and_why(client, engine, key, failing):
    r = _start(client, key, accept=["note:sandbox.disk"], precheck_reason="disk is fine",
               input={"_precheck": {"skip_steps": [{"step_id": "review"}]}})
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    assert r.json()["precheck"]["accepted"] == ["note:sandbox.disk"]
    with engine.begin() as c:
        stored = c.execute(text("SELECT input->'_precheck' FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        event = c.execute(text("SELECT data FROM events WHERE entity_id = :r AND "
                               "event_type = 'workflow.precheck_accepted'"),
                          {"r": run_id}).scalar()
    # The caller's own `_precheck` (a skip of `review`) was dropped; the server's was kept.
    assert stored["skip_steps"] == [] and stored["accepted"] == ["note:sandbox.disk"]
    assert stored["mode"] == "enforce"
    assert stored["by"] == "ceo" and stored["reason"] == "disk is fine"
    assert event["accepted"] == ["note:sandbox.disk"]


def test_skipping_the_failing_step_starts_and_is_recorded(client, engine, key, failing):
    r = _start(client, key, skip_steps=[{"step_id": "note", "reason": "later"}])
    assert r.status_code == 200, r.text
    assert r.json()["precheck"]["skip_steps"] == [{"step_id": "note", "reason": "later"}]


def test_a_skip_of_a_step_that_protects_a_deploy_is_refused(client, key, monkeypatch):
    monkeypatch.setitem(workflow_precheck.CAPABILITY_PRECHECKS, "report",
                        workflow_precheck.NO_CHECKS("test"))
    # `review` verifies work a deploy ships; here the deploy is a report standing in, so make
    # the spec's deploy a real deploy capability for the rule to see.
    monkeypatch.setattr(workflow_precheck, "_DEPLOYS", frozenset({"report"}))
    r = _start(client, key, skip_steps=["review"])
    assert r.status_code == 422, r.text
    assert "unverified" in r.text


def test_report_mode_starts_and_puts_the_precheck_on_the_run(client, engine, key, failing,
                                                             monkeypatch):
    monkeypatch.delenv(workflow_precheck.GATE_ENV)          # the default: report
    r = _start(client, key)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    shown = r.json()["precheck"]
    assert shown["mode"] == "report" and shown["unaccepted"] == ["note:sandbox.disk"]
    assert shown["message"].startswith("Pre-check: 1 will_fail")
    with engine.begin() as c:
        stored = c.execute(text("SELECT input->'_precheck' FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        event = c.execute(text("SELECT data FROM events WHERE entity_id = :r AND "
                               "event_type = 'workflow.precheck_reported'"),
                          {"r": run_id}).scalar()
    assert stored["will_fail"] == ["note:sandbox.disk"] and stored["report"]["steps"] is not None
    assert "note:sandbox.disk" in event["message"]
