"""Constitution §5 Community comms dry-run close-the-loop."""

from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT_ID = "boss-dogfood"


def _create_draft(client, *, channel: str, body: str, dedupe_key: str) -> str:
    created = client.post(
        "/v1/comms/outbox",
        json={
            "project_id": PROJECT_ID,
            "channel": channel,
            "subject": "Dry-run subject",
            "body": body,
            "dedupe_key": dedupe_key,
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    return created.json()["id"]


def _upsert_policy(client, provider: str, delivery_mode: str) -> None:
    response = client.post(
        "/v1/comms/policies",
        json={
            "project_id": PROJECT_ID,
            "provider": provider,
            "delivery_mode": delivery_mode,
            "allowed_data_boundaries": ["public", "internal"],
            "actor": "executive_console",
        },
    )
    assert response.status_code == 201, response.text


def test_dry_run_creates_receipt_and_never_calls_adapter(client, engine, monkeypatch):
    _upsert_policy(client, "slack", "draft_only")
    secret_body = "never-leak-this-outbox-body"
    message_id = _create_draft(
        client,
        channel="slack:#ops",
        body=secret_body,
        dedupe_key="outbox-dry-run-draft-1",
    )
    called = {"slack": False, "email": False}

    def fail_slack(channel, subject, body):
        called["slack"] = True
        raise AssertionError("Slack adapter must not run on dry-run.")

    def fail_email(channel, subject, body):
        called["email"] = True
        raise AssertionError("Email adapter must not run on dry-run.")

    monkeypatch.setattr(api_main.comms.slack, "post_message", fail_slack)
    monkeypatch.setattr(api_main.comms.email, "send_message", fail_email)

    response = client.post(
        f"/v1/comms/outbox/{message_id}/dry-run",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "dedupe_key": "outbox-dry-run-receipt-1",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "dry_run"
    assert payload["message_id"] == message_id
    assert payload["receipt_id"]
    assert payload["delivery_mode"] == "dry_run"
    assert payload["provider_send"] is False
    assert payload["message"] == "dry-run — no provider send"
    assert "sent" not in payload["message"].lower()
    assert called == {"slack": False, "email": False}

    with engine.begin() as connection:
        message = connection.execute(
            text(
                """
                SELECT status, receipt_id, body
                FROM outbox_messages
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().one()
        receipt = connection.execute(
            text(
                """
                SELECT effect_kind, status, provider_ref, effect_scope, result
                FROM receipts
                WHERE id = :receipt_id
                """
            ),
            {"receipt_id": payload["receipt_id"]},
        ).mappings().one()
        job_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM jobs
                WHERE kind = 'comms.outbox.deliver'
                  AND payload->>'outbox_message_id' = :message_id
                """
            ),
            {"message_id": message_id},
        ).scalar_one()
        events = connection.execute(
            text(
                """
                SELECT event_type, data
                FROM events
                WHERE event_type = 'outbox.dry_run'
                  AND entity_id = :message_id
                """
            ),
            {"message_id": message_id},
        ).mappings().all()
        reservation_count = connection.execute(
            text(
                """
                SELECT COUNT(*)
                FROM budget_reservations
                WHERE project_id = :project_id
                  AND reason ILIKE '%dry-run%'
                """
            ),
            {"project_id": PROJECT_ID},
        ).scalar_one()

    assert message["status"] == "dry_run"
    assert message["receipt_id"] == payload["receipt_id"]
    assert message["body"] == secret_body
    assert receipt["effect_kind"] == "comms"
    assert receipt["status"] == "succeeded"
    assert receipt["provider_ref"] is None
    assert receipt["effect_scope"]["delivery_mode"] == "dry_run"
    assert receipt["result"]["delivery_mode"] == "dry_run"
    assert receipt["result"]["provider_send"] is False
    assert receipt["result"]["message"] == "dry-run — no provider send"
    assert job_count == 0
    assert reservation_count == 0
    assert len(events) == 1
    event_data = events[0]["data"]
    assert event_data["delivery_mode"] == "dry_run"
    assert event_data["provider_send"] is False
    assert event_data["receipt_id"] == payload["receipt_id"]
    assert secret_body not in str(event_data)
    assert "body" not in event_data


def test_dry_run_does_not_require_real_send_allowed(client, engine, monkeypatch):
    _upsert_policy(client, "email", "draft_only")
    message_id = _create_draft(
        client,
        channel="email:founder@example.com",
        body="Draft-only dry-run body",
        dedupe_key="outbox-dry-run-no-real-send-1",
    )
    previous_allowed = api_main.comms.settings.email_real_send_allowed
    api_main.comms.settings.email_real_send_allowed = False
    called = {"value": False}

    def fail_if_called(channel, subject, body):
        called["value"] = True
        raise AssertionError("Email adapter must not run on dry-run.")

    monkeypatch.setattr(api_main.comms.email, "send_message", fail_if_called)
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/dry-run",
            json={
                "project_id": PROJECT_ID,
                "actor": "ceo",
                "dedupe_key": "outbox-dry-run-no-real-send-receipt-1",
            },
        )
    finally:
        api_main.comms.settings.email_real_send_allowed = previous_allowed

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dry_run"
    assert called["value"] is False


def test_dry_run_rejects_non_draft_message(client):
    message_id = _create_draft(
        client,
        channel="slack:#ops",
        body="Will be queued, not dry-run.",
        dedupe_key="outbox-dry-run-queued-1",
    )
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Queue so dry-run cannot claim this is a draft.",
        },
    )
    assert reserve.status_code == 201
    queued = client.post(
        f"/v1/comms/outbox/{message_id}/queue",
        json={
            "project_id": PROJECT_ID,
            "reservation_id": reserve.json()["reservation_id"],
            "dedupe_key": "outbox-dry-run-queued-job-1",
        },
    )
    assert queued.status_code == 201

    response = client.post(
        f"/v1/comms/outbox/{message_id}/dry-run",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "dedupe_key": "outbox-dry-run-queued-receipt-1",
        },
    )
    assert response.status_code == 422
    assert "draft" in response.json()["detail"]


def test_real_send_allowed_without_creds_still_blocks(client, engine):
    _upsert_policy(client, "slack", "real_send_allowed")
    message_id = _create_draft(
        client,
        channel="slack:#ops",
        body="Real path must stay fail-closed without creds.",
        dedupe_key="outbox-real-send-no-creds-1",
    )
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "comms",
            "amount_usd_micros": 50_000,
            "reason": "Attempt real Slack send without webhook.",
        },
    )
    assert reserve.status_code == 201
    reservation_id = reserve.json()["reservation_id"]

    previous_allowed = api_main.comms.settings.slack_real_send_allowed
    previous_webhook = api_main.comms.settings.slack_webhook_url
    api_main.comms.settings.slack_real_send_allowed = True
    api_main.comms.settings.slack_webhook_url = None
    try:
        response = client.post(
            f"/v1/comms/outbox/{message_id}/deliver",
            json={
                "project_id": PROJECT_ID,
                "reservation_id": reservation_id,
                "dedupe_key": "outbox-real-send-no-creds-delivery-1",
            },
        )
    finally:
        api_main.comms.settings.slack_real_send_allowed = previous_allowed
        api_main.comms.settings.slack_webhook_url = previous_webhook

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "blocked"
    assert payload["decision_id"]
    assert payload["receipt_id"]
    assert "webhook" in (payload.get("message") or "").lower()

    with engine.begin() as connection:
        outbox_status = connection.execute(
            text("SELECT status FROM outbox_messages WHERE id = :message_id"),
            {"message_id": message_id},
        ).scalar_one()
        reservation_status = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :reservation_id"),
            {"reservation_id": reservation_id},
        ).scalar_one()

    assert outbox_status == "blocked"
    assert reservation_status == "released"
