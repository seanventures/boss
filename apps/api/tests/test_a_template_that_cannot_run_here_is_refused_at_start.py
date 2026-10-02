"""Start refuses a workflow this project cannot run, says what is missing and how to fix it,
and starts anyway only for what the operator was shown and accepted.

boss-1, 2026-10-02: every catalogue template but one ends in `deploy`, the install had no
deployment host, and nothing checked — a run paid for research, a panel, a build and a repair,
a person approved the release, and it failed at the last step. The email examples did the same
at an approved send with no SMTP.
"""
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"
#: A channel BOSS cannot post to is missing on every install, so the refusal does not depend
#: on what this test database happens to have connected.
NOWHERE = "slack:#announcements"


@pytest.fixture
def workflow_key(engine):
    key = f"tell_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Tell", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "tell",
                  "input_schema": {"required": ["title"],
                                   "properties": {"title": {"title": "Name"},
                                                  "notify_channel": {"title": "Who to tell"}}},
                  "steps": [{"id": "tell", "kind": "capability",
                             "binding": {"capability": "notify",
                                         "inputs": {"message": {"from_input": "title"},
                                                    "channel": {"from_input":
                                                                "notify_channel"}}},
                             "next": []}]})
    return key


def _start(client, key, **extra):
    return client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": key,
        "input": {"title": "Launch note", "notify_channel": NOWHERE},
        "dedupe_key": f"t:{uuid4().hex}", **extra})


def test_a_start_the_project_cannot_run_is_refused_with_the_checklist(client, workflow_key):
    refused = _start(client, workflow_key)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "cannot run here yet" in detail["message"]
    missing = detail["readiness"]["missing"]
    assert [m["key"] for m in missing] == [f"channel:{NOWHERE}"]
    assert missing[0]["fix"] and missing[0]["needed_by"][0]["step_id"] == "tell"


def test_starting_anyway_is_the_operators_choice_and_is_recorded(client, engine, workflow_key):
    started = _start(client, workflow_key, start_anyway=[f"channel:{NOWHERE}"])
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["readiness"]["started_anyway"] == [f"channel:{NOWHERE}"]
    with engine.begin() as connection:
        event = connection.execute(
            text("SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
                 "AND event_type = 'workflow.started_unready'"),
            {"p": PROJECT, "r": body["run"]["id"]}).scalar()
    assert event["missing"][0]["key"] == f"channel:{NOWHERE}"


def test_accepting_something_else_does_not_wave_this_through(client, workflow_key):
    refused = _start(client, workflow_key, start_anyway=["deployment_target"])
    assert refused.status_code == 409, refused.text


def test_readiness_can_be_asked_before_pressing_start(client, workflow_key):
    asked = client.get(f"/v1/delivery-workflows/{workflow_key}/readiness", params={
        "project_id": PROJECT, "input": json.dumps({"notify_channel": NOWHERE})})
    assert asked.status_code == 200, asked.text
    assert asked.json()["ready"] is False
    assert asked.json()["missing"][0]["label"].startswith("A channel BOSS can post to")
    # No channel given: the notify step will be skipped visibly, and nothing else is needed.
    plain = client.get(f"/v1/delivery-workflows/{workflow_key}/readiness",
                       params={"project_id": PROJECT})
    assert plain.status_code == 200 and plain.json()["ready"] is True


def test_the_low_level_run_door_checks_too(client, engine, workflow_key):
    with engine.begin() as connection:
        version_id = workflows.running_version(connection, PROJECT, workflow_key)["version_id"]
    refused = client.post("/v1/workflow-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_version_id": version_id,
        "input": {"title": "Launch note", "notify_channel": NOWHERE},
        "dedupe_key": f"t:{uuid4().hex}"})
    assert refused.status_code == 409, refused.text


def test_every_catalogue_template_offers_a_form_and_its_own_example(client):
    items = client.get("/v1/delivery-workflows/catalogue").json()["items"]
    assert len(items) >= 12
    for item in items:
        schema = item["input_schema"]
        assert "directive" in schema["required"], item["key"]
        assert schema["properties"]["directive"]["title"], item["key"]
        assert item["example"]["directive"], item["key"]
        assert "spec_review" not in item["gates"], item["key"]
        assert {n["key"] for n in item["needs"]} >= {"model"}, item["key"]


# ── Running it again, and the doors with nobody at the keyboard ──────────────────────────────
# Review of the readiness change: a re-run of a run that was started anyway was refused with no
# way past; Stop and run again stopped the original first and then the start was refused, so the
# operator had no run at all; schedules and event subscriptions could not start anyway.

def _status(engine, run_id):
    with engine.begin() as connection:
        return connection.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                                  {"r": run_id}).scalar()


def test_a_rerun_repeats_what_was_accepted_for_the_run_it_repeats(client, workflow_key):
    first = _start(client, workflow_key, start_anyway=[f"channel:{NOWHERE}"])
    assert first.status_code == 200, first.text
    again = _start(client, workflow_key, rerun_of=first.json()["run"]["id"])
    assert again.status_code == 200, again.text
    assert again.json()["readiness"]["started_anyway"] == [f"channel:{NOWHERE}"]


def test_stop_and_run_again_refused_leaves_the_original_running(client, engine, workflow_key):
    original = _start(client, workflow_key, start_anyway=[f"channel:{NOWHERE}"]).json()["run"]
    live = _status(engine, original["id"])
    # Something newly missing that nobody accepted: the replacement is refused BEFORE the stop.
    refused = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "input": {"title": "Launch note", "notify_channel": "slack:#elsewhere"},
        "replaces_run": original["id"], "dedupe_key": f"replace:{original['id']}"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["readiness"]["missing"]
    assert _status(engine, original["id"]) == live != "cancelled"
    # The same input as before: what was accepted carries over, the original stops, one starts.
    replaced = client.post("/v1/delivery-workflows/start", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "input": {"title": "Launch note", "notify_channel": NOWHERE},
        "replaces_run": original["id"], "dedupe_key": f"replace:{original['id']}"})
    assert replaced.status_code == 200, replaced.text
    assert _status(engine, original["id"]) == "cancelled"
    assert replaced.json()["run"]["id"] != original["id"]


def test_the_low_level_run_door_can_be_told_to_start_anyway(client, engine, workflow_key):
    with engine.begin() as connection:
        version_id = workflows.running_version(connection, PROJECT, workflow_key)["version_id"]
    started = client.post("/v1/workflow-runs", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_version_id": version_id,
        "input": {"title": "Launch note", "notify_channel": NOWHERE},
        "dedupe_key": f"t:{uuid4().hex}", "start_anyway": [f"channel:{NOWHERE}"]})
    assert started.status_code == 201, started.text
    with engine.begin() as connection:
        noted = connection.execute(
            text("SELECT data FROM events WHERE entity_id = :r "
                 "AND event_type = 'workflow.started_unready'"),
            {"r": started.json()["id"]}).scalar()
    assert noted["started_anyway"] == [f"channel:{NOWHERE}"]


def test_a_schedule_says_what_its_firing_will_refuse_and_stores_start_anyway(client,
                                                                             workflow_key):
    made = client.post("/v1/delivery-workflows/schedules", json={
        "project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
        "name": f"weekly {uuid4().hex[:6]}", "cadence": "weekly", "weekday": 0, "hour": 8,
        "input": {"title": "Launch note", "notify_channel": NOWHERE}})
    assert made.status_code == 201, made.text
    assert made.json()["readiness"]["will_refuse"] == [f"channel:{NOWHERE}"]
    edited = client.post("/v1/delivery-workflows/schedules", json={
        "project_id": PROJECT, "actor": "ceo", "schedule_id": made.json()["id"],
        "input": {"title": "Launch note", "notify_channel": NOWHERE},
        "start_anyway": [f"channel:{NOWHERE}"]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["start_anyway"] == [f"channel:{NOWHERE}"]
    assert edited.json()["readiness"]["will_refuse"] == []
    client.delete(f"/v1/delivery-workflows/schedules/{made.json()['id']}",
                  params={"project_id": PROJECT, "actor": "ceo"})


def test_an_event_subscription_to_a_workflow_can_be_made_and_shows_its_readiness(
        client, workflow_key):
    """The `workflow` sink was accepted by the service but not by the HTTP schemas, so a
    subscription that starts a workflow could not be made or read over the API at all."""
    made = client.post("/v1/event-subscriptions", json={
        "project_id": PROJECT, "actor": "ceo", "event_type": f"probe.x{uuid4().hex[:6]}",
        "sink_kind": "workflow", "sink_ref": workflow_key,
        "config": {"start_anyway": ["deployment_target"]}})
    assert made.status_code == 201, made.text
    assert made.json()["sink_kind"] == "workflow"
    assert made.json()["readiness"]["applies"] is True
    assert made.json()["readiness"]["will_refuse"] == []
