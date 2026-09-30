"""GA checks for API paths that grant skills, schedule work, or send test email."""

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.email_adapter import EmailAdapter
from boss_engine.services import workflow_schedules


PROJECT = "boss-dogfood"


@pytest.fixture
def fresh_email_configuration(engine):
    def clear():
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM project_configurations "
                     "WHERE project_id = :project_id AND config_key = 'email_smtp'"),
                {"project_id": PROJECT},
            )
            connection.execute(
                text("DELETE FROM comms_channel_policies "
                     "WHERE project_id = :project_id AND provider = 'email'"),
                {"project_id": PROJECT},
            )

    clear()
    yield
    clear()


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/v1/role-skills/sync", {"project_id": PROJECT}),
        ("GET", "/v1/email/config", {"project_id": PROJECT}),
        ("POST", "/v1/email/config", {
            "project_id": PROJECT, "host": "smtp.example.test",
            "from_address": "boss@example.test",
        }),
        ("POST", "/v1/email/test", {
            "project_id": PROJECT, "to": "founder@example.test", "dedupe_key": "auth-test",
        }),
        ("GET", "/v1/delivery-workflows/schedules", {"project_id": PROJECT}),
        ("POST", "/v1/delivery-workflows/schedules", {
            "project_id": PROJECT, "workflow_key": "missing", "name": "test",
            "cadence": "daily", "actor": "executive_console",
        }),
        ("POST", "/v1/delivery-workflows/schedules/unknown/enabled", {
            "project_id": PROJECT, "enabled": True,
        }),
        ("DELETE", "/v1/delivery-workflows/schedules/unknown", {
            "project_id": PROJECT,
        }),
        ("POST", "/v1/delivery-workflows/schedules/tick", {
            "project_id": PROJECT, "actor": "system",
        }),
    ],
)
def test_project_mutations_and_sensitive_reads_bind_the_authenticated_actor(
    client, monkeypatch, method, path, payload,
):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "release-key")
    monkeypatch.setattr(
        api_main.settings, "api_key_principals", "release-key:release_manager")
    kwargs = {"params": payload} if method in {"GET", "DELETE"} else {"json": payload}
    response = client.request(
        method, path, headers={"Authorization": "Bearer release-key"}, **kwargs)
    assert response.status_code == 403
    assert "release_manager" in response.json()["detail"]


def test_schedule_tick_uses_the_worker_keys_bound_project_member(client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.settings, "api_keys", "ceo-key")
    monkeypatch.setattr(api_main.settings, "api_key_principals", "ceo-key:ceo")
    monkeypatch.setattr(workflow_schedules, "run_due", lambda _connection, **_kwargs: [])
    response = client.post(
        "/v1/delivery-workflows/schedules/tick",
        headers={"Authorization": "Bearer ceo-key"},
        json={"project_id": PROJECT, "actor": "system"},
    )
    assert response.status_code == 200
    assert response.json() == {"started": [], "count": 0}


def test_schedule_creation_and_resume_need_execution_permission(
    client, engine, monkeypatch,
):
    actor = "ga_schedule_manager"
    with engine.begin() as connection:
        api_main.members.upsert_member(
            connection, project_id=PROJECT, member_id=actor,
            display_name="GA schedule manager", member_type="human",
            role_keys=[], permissions=["workflow_manage"], actor="test",
        )
    calls = []
    monkeypatch.setattr(
        workflow_schedules, "create",
        lambda _connection, **kwargs: calls.append(("create", kwargs)) or {"id": "wsc_test"},
    )
    monkeypatch.setattr(
        workflow_schedules, "set_enabled",
        lambda _connection, *_args: calls.append(("enabled", _args)) or {"id": "wsc_test"},
    )
    schedule = {"project_id": PROJECT, "actor": actor, "workflow_key": "sample",
                "name": "Review", "cadence": "daily", "enabled": False}
    assert client.post("/v1/delivery-workflows/schedules", json=schedule).status_code == 201
    assert client.post("/v1/delivery-workflows/schedules", json={
        **schedule, "enabled": True}).status_code == 403
    assert client.post("/v1/delivery-workflows/schedules/wsc_test/enabled", json={
        "project_id": PROJECT, "actor": actor, "enabled": False}).status_code == 200
    assert client.post("/v1/delivery-workflows/schedules/wsc_test/enabled", json={
        "project_id": PROJECT, "actor": actor, "enabled": True}).status_code == 403
    assert [item[0] for item in calls] == ["create", "enabled"]


def _configure_test_email(client):
    config = client.post("/v1/email/config", json={
        "project_id": PROJECT, "actor": "executive_console",
        "host": "smtp.example.test", "from_address": "boss@example.test",
    })
    assert config.status_code == 200


def test_email_test_records_one_budgeted_receipt_and_reuses_it(
    client, engine, monkeypatch, fresh_email_configuration,
):
    _configure_test_email(client)
    policy = client.post("/v1/comms/policies", json={
        "project_id": PROJECT, "provider": "email",
        "delivery_mode": "real_send_allowed",
        "allowed_data_boundaries": ["internal"], "actor": "executive_console",
    })
    assert policy.status_code == 201
    sends = []
    monkeypatch.setattr(
        EmailAdapter, "send_message",
        lambda _self, channel, subject, body: (
            sends.append((channel, subject, body)) or "smtp:founder@example.test"),
    )
    missing_key = client.post("/v1/email/test", json={
        "project_id": PROJECT, "to": "founder@example.test"})
    assert missing_key.status_code == 422
    assert not sends
    request = {"project_id": PROJECT, "to": "founder@example.test",
               "dedupe_key": "ga-email-success", "actor": "executive_console"}
    first = client.post("/v1/email/test", json=request)
    assert first.status_code == 200
    assert first.json()["sent"] is True
    assert first.json()["receipt_id"]
    assert first.json()["outbox_message_id"]
    assert len(sends) == 1

    replay = client.post("/v1/email/test", json=request)
    assert replay.status_code == 200
    assert replay.json()["status"] == "reused"
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    assert len(sends) == 1

    collision = client.post("/v1/email/test", json={
        **request, "to": "someone-else@example.test"})
    assert collision.status_code == 409
    assert len(sends) == 1

    with engine.begin() as connection:
        receipt = connection.execute(text(
            "SELECT effect_kind, status, provider_ref FROM receipts WHERE id = :id"),
            {"id": first.json()["receipt_id"]},
        ).mappings().one()
        outbox = connection.execute(text(
            "SELECT status, receipt_id FROM outbox_messages WHERE id = :id"),
            {"id": first.json()["outbox_message_id"]},
        ).mappings().one()
        costs = connection.execute(text(
            "SELECT b.status, b.amount_usd_micros FROM cost_receipts c "
            "JOIN budget_reservations b ON b.id = c.reservation_id "
            "WHERE c.receipt_id = :id"),
            {"id": first.json()["receipt_id"]},
        ).mappings().all()
    assert receipt == {"effect_kind": "email.message.send", "status": "succeeded",
                       "provider_ref": "smtp:founder@example.test"}
    assert outbox == {"status": "acked", "receipt_id": first.json()["receipt_id"]}
    assert len(costs) == 1
    assert costs[0]["status"] == "consumed"
    assert costs[0]["amount_usd_micros"] == api_main.settings.comms_message_budget_usd_micros


def test_email_test_without_channel_policy_blocks_and_releases_budget(
    client, engine, monkeypatch, fresh_email_configuration,
):
    _configure_test_email(client)
    monkeypatch.setattr(
        EmailAdapter, "send_message",
        lambda *_args: pytest.fail("SMTP must not run without a channel policy"),
    )
    request = {
        "project_id": PROJECT, "to": "founder@example.test",
        "dedupe_key": "ga-email-policy-block", "actor": "executive_console",
    }
    result = client.post("/v1/email/test", json=request)
    assert result.status_code == 409
    assert result.json()["sent"] is False
    assert result.json()["decision_id"]
    assert result.json()["receipt_id"]
    with engine.begin() as connection:
        receipt = connection.execute(text(
            "SELECT status, effect_kind FROM receipts WHERE id = :id"),
            {"id": result.json()["receipt_id"]},
        ).mappings().one()
        reservation = connection.execute(text(
            "SELECT status FROM budget_reservations "
            "WHERE project_id = :project_id AND reason = :reason "
            "ORDER BY created_at DESC LIMIT 1"),
            {"project_id": PROJECT,
             "reason": "Send one SMTP configuration test message"},
        ).scalar_one()
    assert receipt == {"status": "failed", "effect_kind": "email.message.send"}
    assert reservation == "released"

    retry = client.post("/v1/email/test", json=request)
    assert retry.status_code == 409
    assert retry.json()["receipt_id"] == result.json()["receipt_id"]
    assert "new dedupe_key" in retry.json()["detail"]
    with engine.begin() as connection:
        reservations = connection.execute(text(
            "SELECT count(*) FROM budget_reservations "
            "WHERE project_id = :project_id AND reason = :reason"),
            {"project_id": PROJECT,
             "reason": "Send one SMTP configuration test message"},
        ).scalar_one()
    assert reservations == 1
