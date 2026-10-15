"""Webhook triggers for workflows over HTTP (#512).

The owner's routes (create, list, rotate, enable/disable, deliveries) and the public door: a
signed POST with no session starts a run; an unsigned or stale one is refused, answered with its
reason and recorded; the secret is shown at creation and rotation and nowhere else; and a trigger
id from one project is a 404 from another.
"""
import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services import workflow_webhooks as hooks
from boss_engine.services import workflows

PROJECT = "boss-dogfood"
BASE = "/v1/delivery-workflows/webhooks"


@pytest.fixture
def workflow_key(engine):
    key = f"hooked_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name="Hooked", description=None,
            actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="ceo",
            spec={"entry_step": "hold",
                  "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                             "binding": {}, "next": []}]})
    return key


def _create(client, key, name=None):
    made = client.post(BASE, json={"project_id": PROJECT, "actor": "ceo",
                                   "workflow_key": key, "name": name or f"Orders {key}"})
    assert made.status_code == 201, made.text
    return made.json()


def _deliver(client, trigger, body: bytes, secret: str, *, ts=None, delivery=None):
    ts = int(time.time()) if ts is None else ts
    headers = {"Content-Type": "application/json", "X-BOSS-Timestamp": str(ts),
               "X-BOSS-Signature": hooks.sign(secret, ts, body)}
    if delivery:
        headers["X-BOSS-Delivery"] = delivery
    return client.post(trigger["path"], content=body, headers=headers)


def test_create_shows_the_secret_once_and_never_again(client, workflow_key):
    made = _create(client, workflow_key)
    assert made["secret"].startswith("whsec_")
    assert made["path"].startswith("/v1/webhooks/inbound/workflow/")
    listed = client.get(BASE, params={"project_id": PROJECT, "workflow_key": workflow_key})
    assert listed.status_code == 200, listed.text
    [row] = listed.json()["webhooks"]
    assert "secret" not in row and made["secret"] not in listed.text
    assert row["secret_last4"] == made["secret"][-4:]
    shown = client.get(f"{BASE}/{made['id']}/deliveries", params={"project_id": PROJECT})
    assert made["secret"] not in shown.text


def test_a_signed_delivery_with_no_session_starts_a_run(client, engine, workflow_key, monkeypatch):
    made = _create(client, workflow_key)
    # The inbound door is exempt from transport auth; the signature is what authenticates it.
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    body = json.dumps({"order": 42}).encode()
    started = _deliver(client, made, body, made["secret"], delivery="evt-1")
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    with engine.begin() as connection:
        run_input = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :i"),
                                       {"i": run_id}).scalar()
    assert run_input["order"] == 42 and run_input["trigger"] == "webhook"

    again = _deliver(client, made, body, made["secret"], delivery="evt-1")
    assert again.status_code == 200 and again.json() == {
        "status": "duplicate", "run_id": run_id, "delivery_id": "evt-1"}

    # A management route still needs a credential under the same setting.
    # (401 with keys configured; 503 on this test install, which has none to check against.)
    assert client.get(BASE, params={"project_id": PROJECT}).status_code in (401, 503)


@pytest.mark.parametrize("case,status,reason", [
    ("bad", 401, "bad_signature"),
    ("stale", 401, "stale_timestamp"),
    ("big", 413, "too_large"),
    ("disabled", 409, "disabled"),
])
def test_a_refused_delivery_is_answered_and_recorded(client, workflow_key, case, status, reason):
    made = _create(client, workflow_key)
    body = b'{"n": 1}'
    secret, ts = made["secret"], None
    if case == "bad":
        secret = "whsec_wrong"
    elif case == "stale":
        ts = int(time.time()) - hooks.TIMESTAMP_TOLERANCE_SECONDS - 60
    elif case == "big":
        body = b'{"x": "' + b"a" * hooks.MAX_BODY_BYTES + b'"}'
    elif case == "disabled":
        off = client.post(f"{BASE}/{made['id']}/enabled",
                          json={"project_id": PROJECT, "actor": "ceo", "enabled": False})
        assert off.status_code == 200 and off.json()["enabled"] is False
    refused = _deliver(client, made, body, secret, ts=ts)
    assert refused.status_code == status, refused.text
    assert refused.json()["reason"] == reason
    shown = client.get(f"{BASE}/{made['id']}/deliveries", params={"project_id": PROJECT}).json()
    assert [(d["status"], d["reason"]) for d in shown["deliveries"]] == [("refused", reason)]


def test_rotate_returns_a_new_secret_and_the_old_one_stops(client, workflow_key):
    made = _create(client, workflow_key)
    rotated = client.post(f"{BASE}/{made['id']}/rotate",
                          json={"project_id": PROJECT, "actor": "ceo"})
    assert rotated.status_code == 200, rotated.text
    new_secret = rotated.json()["secret"]
    assert new_secret != made["secret"]
    body = b'{"n": 2}'
    assert _deliver(client, made, body, made["secret"]).status_code == 401
    assert _deliver(client, made, body, new_secret).status_code == 202


def test_an_unknown_url_is_a_plain_404(client):
    response = client.post("/v1/webhooks/inbound/workflow/not-a-real-token", content=b"{}",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 404


def test_another_projects_trigger_is_not_found(client, engine, workflow_key):
    made = _create(client, workflow_key)
    other = f"prj_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "SELECT :o, workspace_id, 'Other', :o FROM projects WHERE id = :p"),
            {"o": other, "p": PROJECT})
    for response in (
        client.post(f"{BASE}/{made['id']}/rotate", json={"project_id": other, "actor": "ceo"}),
        client.post(f"{BASE}/{made['id']}/enabled",
                    json={"project_id": other, "actor": "ceo", "enabled": False}),
        client.get(f"{BASE}/{made['id']}/deliveries", params={"project_id": other}),
    ):
        assert response.status_code in (403, 404), response.text
        assert made["secret"][-8:] not in response.text
    mine = client.get(BASE, params={"project_id": PROJECT, "workflow_key": workflow_key})
    assert mine.json()["webhooks"][0]["enabled"]


def _owner(engine, permissions):
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO project_members (id, project_id, member_id, display_name, permissions)
            VALUES (:id, :p, 'hook_owner', 'Hook owner', CAST(:perms AS JSONB))
            ON CONFLICT (project_id, member_id) DO UPDATE SET permissions = EXCLUDED.permissions
        """), {"id": f"pm_{uuid4().hex[:12]}", "p": PROJECT, "perms": json.dumps(permissions)})


def test_a_creator_who_lost_the_right_to_run_is_refused_then_restored(client, engine, workflow_key):
    _owner(engine, ["workflow_manage", "workflow_execute"])
    made = client.post(BASE, json={"project_id": PROJECT, "actor": "hook_owner",
                                   "workflow_key": workflow_key, "name": f"Owned {workflow_key}"})
    assert made.status_code == 201, made.text
    made = made.json()
    _owner(engine, ["workflow_manage"])
    body = b'{"n": 3}'
    refused = _deliver(client, made, body, made["secret"], delivery="own-1")
    assert refused.status_code == 403, refused.text
    assert refused.json()["reason"] == "actor_not_authorized"
    shown = client.get(f"{BASE}/{made['id']}/deliveries", params={"project_id": PROJECT}).json()
    assert [(d["status"], d["reason"], d["run_id"]) for d in shown["deliveries"]] == \
        [("refused", "actor_not_authorized", None)]
    assert shown["trigger"]["enabled"] is True
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM events WHERE entity_id = :i "
            "AND event_type = 'workflow.webhook_refused_authority'"), {"i": made["id"]}).scalar() == 1

    _owner(engine, ["workflow_manage", "workflow_execute"])
    assert _deliver(client, made, body, made["secret"], delivery="own-1").status_code == 202
