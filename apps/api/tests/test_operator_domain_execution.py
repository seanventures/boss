"""Authenticated discovery, typed input, result, and audit for three operator domains.

Core tasks, governance digests, and delivery schedules already exist. This proves
those paths, including the schedule rule that a recurring run cannot be saved
without the inputs its workflow declared, and that editing those inputs is the
same operation rather than a new control plane.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import workflows


PROJECT = "boss-dogfood"
ACTOR = "executive_console"
KEY = "operator-domain-proof-key"


def _auth(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", KEY)
    monkeypatch.setattr(api_main.settings, "api_key_principals", f"{KEY}:{ACTOR}")
    return {"Authorization": f"Bearer {KEY}"}


def _find(catalog: dict, method: str, path: str) -> tuple[str, dict]:
    for domain in catalog["domains"]:
        for route in domain["routes"]:
            if route["method"] == method and route["path"] == path:
                return domain["id"], route
    raise AssertionError(f"{method} {path} is not in the API catalog")


def _body_schema(document: dict, method: str, path: str) -> dict:
    schema = document["paths"][path][method.lower()]["requestBody"]["content"][
        "application/json"]["schema"]
    if "$ref" in schema:
        schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _events_for(client, headers: dict[str, str], entity_id: str) -> list[dict]:
    response = client.get(
        "/v1/events", params={"project_id": PROJECT, "limit": 200}, headers=headers)
    assert response.status_code == 200, response.text
    return [item for item in response.json()["items"] if item["entity_id"] == entity_id]


def test_core_task_is_discovered_typed_written_and_audited(client, monkeypatch):
    headers = _auth(monkeypatch)
    assert client.get("/v1/meta/api-catalog").status_code == 401
    catalog = client.get("/v1/meta/api-catalog", headers=headers)
    assert catalog.status_code == 200, catalog.text
    domain, route = _find(catalog.json(), "POST", "/v1/tasks")
    assert domain == "core"
    assert route["in_openapi"] is True
    schema = _body_schema(client.get("/openapi.json", headers=headers).json(), "POST", "/v1/tasks")
    assert "title" in schema["properties"]
    assert "dedupe_key" in schema["properties"]

    listed = client.get("/v1/tasks", params={"project_id": PROJECT, "limit": 5}, headers=headers)
    assert listed.status_code == 200, listed.text
    assert "items" in listed.json()

    dedupe = f"operator-domain-{uuid.uuid4().hex[:12]}"
    created = client.post("/v1/tasks", headers=headers, json={
        "project_id": PROJECT, "actor": ACTOR, "title": "Plan the community launch",
        "dedupe_key": dedupe,
    })
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    assert task_id.startswith("tsk_")
    audited = _events_for(client, headers, task_id)
    assert any(item["event_type"] == "task.created" for item in audited)


def test_governance_digest_is_discovered_typed_written_and_audited(client, monkeypatch):
    headers = _auth(monkeypatch)
    catalog = client.get("/v1/meta/api-catalog", headers=headers)
    assert catalog.status_code == 200, catalog.text
    domain, route = _find(catalog.json(), "POST", "/v1/decisions/digests")
    assert domain == "core"
    assert route["in_openapi"] is True
    schema = _body_schema(
        client.get("/openapi.json", headers=headers).json(), "POST", "/v1/decisions/digests")
    assert "project_id" in schema["properties"]
    assert "channel" in schema["properties"]

    inbox = client.get(
        "/v1/decisions/inbox", params={"project_id": PROJECT}, headers=headers)
    assert inbox.status_code == 200, inbox.text
    assert isinstance(inbox.json().get("items"), list)

    # No channel: the digest is a local artifact and audit event, not an outbound send.
    created = client.post("/v1/decisions/digests", headers=headers, json={
        "project_id": PROJECT, "actor": ACTOR, "max_items": 5,
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "created"
    assert body["artifact_id"].startswith("art_")
    assert body["outbox_message_id"] is None
    audited = _events_for(client, headers, body["artifact_id"])
    assert any(item["event_type"] == "decision.digest_created" for item in audited)
    assert any(item["event_type"] == "artifact.created" for item in audited)


def test_delivery_schedule_requires_inputs_and_edit_is_audited(client, engine, monkeypatch):
    headers = _auth(monkeypatch)
    catalog = client.get("/v1/meta/api-catalog", headers=headers)
    assert catalog.status_code == 200, catalog.text
    domain, _route = _find(catalog.json(), "POST", "/v1/delivery-workflows/schedules")
    assert domain == "delivery"
    schema = _body_schema(
        client.get("/openapi.json", headers=headers).json(),
        "POST", "/v1/delivery-workflows/schedules")
    assert "input" in schema["properties"]
    assert "schedule_id" in schema["properties"]
    assert "schedule_id" not in schema.get("required", [])

    key = f"sched_inputs_{uuid.uuid4().hex[:8]}"
    definition_id = None
    try:
        with engine.begin() as connection:
            made = workflows.create_definition(
                connection, project_id=PROJECT, workflow_key=key, name=key,
                description=None, actor="test",
                namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
            definition_id = made["id"]
            workflows.publish_version(
                connection, project_id=PROJECT, definition_id=definition_id, actor="test",
                spec={
                    "entry_step": "hold",
                    "input_schema": {
                        "type": "object",
                        "required": ["sectors", "to"],
                        "properties": {
                            "sectors": {"type": "string", "title": "Sectors"},
                            "to": {"type": "string", "title": "To"},
                            "brief": {"type": "string"},
                        },
                    },
                    "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                               "binding": {}, "next": []}],
                })

        listed = client.get(
            "/v1/delivery-workflows/schedules",
            params={"project_id": PROJECT, "actor": ACTOR}, headers=headers)
        assert listed.status_code == 200, listed.text

        base = {
            "project_id": PROJECT, "actor": ACTOR, "workflow_key": key,
            "name": f"{key} Monday", "cadence": "weekly", "weekday": 0,
            "hour": 8, "minute": 0, "timezone": "Europe/London",
        }
        refused = client.post("/v1/delivery-workflows/schedules", headers=headers, json=base)
        assert refused.status_code == 400, refused.text
        assert "sectors" in refused.json()["detail"]
        assert "to" in refused.json()["detail"]

        created = client.post("/v1/delivery-workflows/schedules", headers=headers, json={
            **base, "input": {"sectors": "energy", "to": "reader@example.com", "brief": "short"},
        })
        assert created.status_code == 201, created.text
        schedule = created.json()
        assert schedule["input"]["to"] == "reader@example.com"
        created_events = _events_for(client, headers, schedule["id"])
        created_audit = next(
            item for item in created_events if item["event_type"] == "workflow.schedule_created")
        assert "reader@example.com" not in json.dumps(created_audit["data"])

        edited = client.post("/v1/delivery-workflows/schedules", headers=headers, json={
            **base, "schedule_id": schedule["id"],
            "input": {"sectors": "healthcare", "to": "editor@example.com"},
        })
        assert edited.status_code == 200, edited.text
        assert edited.json()["input"] == {"sectors": "healthcare", "to": "editor@example.com"}
        assert edited.json()["cadence"] == "weekly"
        updated = _events_for(client, headers, schedule["id"])
        edit_audit = next(
            item for item in updated if item["event_type"] == "workflow.schedule_inputs_updated")
        assert edit_audit["data"]["input_keys"] == ["sectors", "to"]
        assert "editor@example.com" not in json.dumps(edit_audit["data"])

        missing_input = client.post("/v1/delivery-workflows/schedules", headers=headers, json={
            **base, "schedule_id": schedule["id"],
        })
        assert missing_input.status_code == 400, missing_input.text

        wrong_workflow = client.post("/v1/delivery-workflows/schedules", headers=headers, json={
            **base, "schedule_id": schedule["id"], "workflow_key": "other",
            "input": {"sectors": "energy", "to": "reader@example.com"},
        })
        assert wrong_workflow.status_code == 400, wrong_workflow.text

        editor = "ga_schedule_editor"
        with engine.begin() as connection:
            api_main.members.upsert_member(
                connection, project_id=PROJECT, member_id=editor,
                display_name="Schedule editor", member_type="human",
                role_keys=[], permissions=["workflow_manage"], actor="test")
        monkeypatch.setattr(
            api_main.settings, "api_key_principals",
            f"{KEY}:{ACTOR},editor-key:{editor}")
        monkeypatch.setattr(api_main.settings, "api_keys", f"{KEY},editor-key")
        denied = client.post(
            "/v1/delivery-workflows/schedules",
            headers={"Authorization": "Bearer editor-key"},
            json={**base, "actor": editor, "schedule_id": schedule["id"],
                  "input": {"sectors": "energy", "to": "reader@example.com"}})
        assert denied.status_code == 403, denied.text
    finally:
        if definition_id:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM workflow_schedules WHERE workflow_key=:k"), {"k": key})
                connection.execute(
                    text("DELETE FROM workflow_definitions WHERE id=:i"), {"i": definition_id})
