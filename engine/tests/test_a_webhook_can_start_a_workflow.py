"""A signed webhook starting one of the operator's own workflows (#512).

Every transition of an inbound delivery, against a real database: a good one starts a run with
its payload as the input; each kind of bad one is refused AND recorded where the owner looks; a
redelivery starts nothing new; a rotated secret stops the old one at once; one project never
reaches another's trigger; and the secret is never read back after it was handed over.
"""
import json
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_webhooks as hooks
from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "prj_test"
OTHER = "prj_webhook_other"


@pytest.fixture()
def made():
    engine = create_engine(DATABASE_URL)
    key = f"hook_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "SELECT :o, workspace_id, 'Other', :o FROM projects WHERE id = :p "
            "ON CONFLICT DO NOTHING"), {"o": OTHER, "p": PROJECT})
        definition = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name=key, description=None,
            actor="test", namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=definition["id"], actor="test",
            spec={"entry_step": "hold",
                  "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                             "binding": {}, "next": []}]})
        trigger = hooks.create(connection, project_id=PROJECT, workflow_key=key,
                               name=f"Hook {key}", actor="test")
    yield engine, key, trigger
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workflow_webhook_triggers WHERE workflow_key = :k"),
                           {"k": key})
        connection.execute(
            text("""DELETE FROM workflow_step_runs WHERE workflow_run_id IN (
                      SELECT r.id FROM workflow_runs r
                      JOIN workflow_versions v ON v.id = r.workflow_version_id
                      WHERE v.workflow_definition_id = :i)"""), {"i": definition["id"]})
        connection.execute(
            text("""DELETE FROM workflow_runs WHERE workflow_version_id IN (
                      SELECT id FROM workflow_versions WHERE workflow_definition_id = :i)"""),
            {"i": definition["id"]})
        connection.execute(text("DELETE FROM workflow_definitions WHERE id = :i"),
                           {"i": definition["id"]})


def _token(trigger) -> str:
    return trigger["path"].rsplit("/", 1)[-1]


def _headers(secret: str, body: bytes, *, ts=None, delivery=None, content_type="application/json"):
    ts = int(time.time()) if ts is None else ts
    headers = {"content-type": content_type, "x-boss-timestamp": str(ts),
               "x-boss-signature": hooks.sign(secret, ts, body)}
    if delivery:
        headers["x-boss-delivery"] = delivery
    return headers


def _send(engine, trigger, body: bytes, headers, **kwargs):
    with engine.begin() as connection:
        return hooks.receive(connection, _token(trigger), body, headers, **kwargs)


def _deliveries(engine, trigger):
    with engine.begin() as connection:
        return hooks.deliveries(connection, project_id=PROJECT, trigger_id=trigger["id"])


def test_a_signed_delivery_starts_the_workflow_with_its_payload(made):
    engine, key, trigger = made
    body = json.dumps({"company": "Acme", "plan": "pro"}).encode()
    result = _send(engine, trigger, body, _headers(trigger["secret"], body, delivery="d-1"))
    assert result["status"] == "started", result
    with engine.begin() as connection:
        run = connection.execute(text("SELECT input FROM workflow_runs WHERE id = :i"),
                                 {"i": result["run_id"]}).mappings().one()
    assert run["input"]["company"] == "Acme"
    assert run["input"]["trigger"] == "webhook"
    assert run["input"]["delivery_id"] == "d-1"
    seen = _deliveries(engine, trigger)["deliveries"]
    assert [(d["status"], d["run_id"]) for d in seen] == [("started", result["run_id"])]


def test_a_redelivery_with_the_same_id_starts_no_second_run(made):
    engine, key, trigger = made
    body = b'{"n": 1}'
    first = _send(engine, trigger, body, _headers(trigger["secret"], body, delivery="same"))
    later = int(time.time()) + 5
    second = _send(engine, trigger, body,
                   _headers(trigger["secret"], body, delivery="same", ts=later))
    assert first["status"] == "started"
    assert second == {"status": "duplicate", "run_id": first["run_id"], "delivery_id": "same"}
    with engine.begin() as connection:
        runs = connection.execute(text(
            "SELECT count(*) FROM workflow_runs r JOIN workflow_versions v "
            "ON v.id = r.workflow_version_id JOIN workflow_definitions d "
            "ON d.id = v.workflow_definition_id WHERE d.workflow_key = :k"), {"k": key}).scalar()
    assert runs == 1
    assert [d["status"] for d in _deliveries(engine, trigger)["deliveries"]] == \
        ["duplicate", "started"]


def test_an_identical_replay_without_a_delivery_id_is_one_run(made):
    engine, key, trigger = made
    body = b'{"n": 2}'
    headers = _headers(trigger["secret"], body)
    first = _send(engine, trigger, body, headers)
    second = _send(engine, trigger, body, headers)
    assert first["status"] == "started" and second["status"] == "duplicate"
    assert second["run_id"] == first["run_id"]


@pytest.mark.parametrize("case,reason", [
    ("bad_signature", "bad_signature"),
    ("stale", "stale_timestamp"),
    ("future", "stale_timestamp"),
    ("unsigned", "missing_signature"),
    ("text_plain", "unsupported_content_type"),
    ("not_an_object", "invalid_json"),
])
def test_a_bad_delivery_is_refused_and_recorded(made, case, reason):
    engine, key, trigger = made
    body = b'[1, 2]' if case == "not_an_object" else b'{"n": 3}'
    secret = trigger["secret"]
    if case == "bad_signature":
        headers = _headers("whsec_not_the_secret", body)
    elif case == "stale":
        headers = _headers(secret, body, ts=int(time.time()) - hooks.TIMESTAMP_TOLERANCE_SECONDS - 5)
    elif case == "future":
        headers = _headers(secret, body, ts=int(time.time()) + hooks.TIMESTAMP_TOLERANCE_SECONDS + 5)
    elif case == "unsigned":
        headers = {"content-type": "application/json"}
    elif case == "text_plain":
        headers = _headers(secret, body, content_type="text/plain")
    else:
        headers = _headers(secret, body)
    result = _send(engine, trigger, body, headers)
    assert result["status"] == "refused" and result["reason"] == reason, result
    seen = _deliveries(engine, trigger)["deliveries"]
    assert [(d["status"], d["reason"], d["run_id"]) for d in seen] == [("refused", reason, None)]


def test_a_body_over_the_limit_is_refused_and_recorded(made):
    engine, key, trigger = made
    body = b'{"x": "' + b"a" * hooks.MAX_BODY_BYTES + b'"}'
    result = _send(engine, trigger, body, _headers(trigger["secret"], body))
    assert result["reason"] == "too_large"
    assert _deliveries(engine, trigger)["deliveries"][0]["reason"] == "too_large"


def test_a_disabled_trigger_refuses_and_records_then_starts_once_enabled(made):
    engine, key, trigger = made
    with engine.begin() as connection:
        off = hooks.set_enabled(connection, project_id=PROJECT, trigger_id=trigger["id"],
                                enabled=False, actor="test")
    assert off["enabled"] is False
    body = b'{"n": 4}'
    refused = _send(engine, trigger, body, _headers(trigger["secret"], body, delivery="x"))
    assert refused["reason"] == "disabled"
    with engine.begin() as connection:
        hooks.set_enabled(connection, project_id=PROJECT, trigger_id=trigger["id"],
                          enabled=True, actor="test")
    started = _send(engine, trigger, body, _headers(trigger["secret"], body, delivery="x"))
    assert started["status"] == "started", "a refused delivery must not burn its dedupe key"


def test_rotating_the_secret_stops_the_old_one_at_once(made):
    engine, key, trigger = made
    with engine.begin() as connection:
        rotated = hooks.rotate(connection, project_id=PROJECT, trigger_id=trigger["id"],
                               actor="test")
    assert rotated["secret"] != trigger["secret"]
    body = b'{"n": 5}'
    old = _send(engine, trigger, body, _headers(trigger["secret"], body))
    assert old["reason"] == "bad_signature"
    new = _send(engine, trigger, body, _headers(rotated["secret"], body))
    assert new["status"] == "started"


def test_the_secret_is_never_read_back_or_stored_in_the_row(made):
    engine, key, trigger = made
    secret = trigger["secret"]
    assert secret.startswith("whsec_") and len(secret) > 40
    with engine.begin() as connection:
        listed = hooks.list_triggers(connection, PROJECT, key)
        shown = hooks.deliveries(connection, project_id=PROJECT, trigger_id=trigger["id"])
        row = connection.execute(text("SELECT * FROM workflow_webhook_triggers WHERE id = :i"),
                                 {"i": trigger["id"]}).mappings().one()
        events = connection.execute(text(
            "SELECT data::text FROM events WHERE entity_id = :i"), {"i": trigger["id"]}).scalars().all()
    assert all("secret" not in t for t in listed)
    assert "secret" not in shown["trigger"]
    assert listed[0]["secret_last4"] == secret[-4:]
    assert secret not in json.dumps({k: str(v) for k, v in row.items()})
    assert all(secret not in e for e in events)


def test_another_project_cannot_reach_the_trigger(made):
    engine, key, trigger = made
    with engine.begin() as connection:
        assert hooks.list_triggers(connection, OTHER) == []
        for call in (
            lambda: hooks.deliveries(connection, project_id=OTHER, trigger_id=trigger["id"]),
            lambda: hooks.rotate(connection, project_id=OTHER, trigger_id=trigger["id"],
                                 actor="test"),
            lambda: hooks.set_enabled(connection, project_id=OTHER, trigger_id=trigger["id"],
                                      enabled=False, actor="test"),
        ):
            with pytest.raises(hooks.WebhookTriggerNotFound):
                call()
        # And it cannot point a trigger of its own at this project's workflow.
        with pytest.raises(hooks.WebhookTriggerError):
            hooks.create(connection, project_id=OTHER, workflow_key=key, name="x", actor="test")


def test_an_unknown_url_is_not_found_and_says_nothing_more(made):
    engine, _key, _trigger = made
    with engine.begin() as connection:
        assert hooks.receive(connection, "no-such-token", b"{}", {}) == {"status": "not_found"}


def test_a_flood_of_refusals_is_counted_not_itemised(made):
    engine, key, trigger = made
    body = b'{"n": 6}'
    for _ in range(hooks.REFUSED_RECORD_LIMIT + 3):
        _send(engine, trigger, body, _headers("whsec_wrong", body))
    shown = _deliveries(engine, trigger)
    assert len(shown["deliveries"]) == hooks.REFUSED_RECORD_LIMIT
    assert shown["trigger"]["refused_unrecorded"] == 3
