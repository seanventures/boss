from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.config import get_settings
from apps.api.app.services.core import emit_event
from apps.api.app.services.event_subscription_service import create_event_subscription
from apps.api.app.services.event_subscription_service import (
    seed_default_event_subscriptions,
)
from apps.api.app.services.jobs import JobLeaseService


PROJECT_ID = "boss-dogfood"
WEBHOOK_SECRET = "test-webhook-secret"


def _create_endpoint(client, *, boundary: str = "internal") -> dict:
    response = client.post(
        "/v1/webhook-endpoints",
        json={
            "project_id": PROJECT_ID,
            "url": "https://receiver.example.test/boss",
            "secret_ref": "env:BOSS_TEST_WEBHOOK_SECRET",
            "description": "Test receiver",
            "data_boundary": boundary,
            "actor": "ceo",
        },
    )
    assert response.status_code == 201
    return response.json()


def _github_payload(event: str, repo: str = "seanheiney/boss") -> dict:
    payload = {
        "action": "opened",
        "repository": {"full_name": repo},
        "sender": {"login": "octocat"},
        "number": 42,
    }
    if event == "pull_request":
        payload["pull_request"] = {"id": 101}
    elif event == "check_suite":
        payload["check_suite"] = {"id": 202}
    else:
        payload["comment"] = {"id": 303}
    return payload


def _signed_headers(raw: bytes, event: str, delivery_id: str) -> dict[str, str]:
    digest = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery_id,
    }


def test_endpoint_crud_manual_queue_dedupe_and_boundary(client, monkeypatch):
    monkeypatch.setenv("BOSS_TEST_WEBHOOK_SECRET", WEBHOOK_SECRET)
    denied = client.post(
        "/v1/webhook-endpoints",
        json={
            "project_id": PROJECT_ID,
            "url": "https://receiver.example.test/denied",
            "secret_ref": "env:BOSS_TEST_WEBHOOK_SECRET",
            "actor": "cto",
        },
    )
    assert denied.status_code == 403
    endpoint = _create_endpoint(client)
    assert client.get(
        "/v1/webhook-endpoints", params={"project_id": PROJECT_ID}
    ).json()["items"] == [endpoint]

    updated = client.patch(
        f"/v1/webhook-endpoints/{endpoint['id']}",
        json={"project_id": PROJECT_ID, "description": "Updated", "enabled": True},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "Updated"

    body = {
        "project_id": PROJECT_ID,
        "event_type": "test.completed",
        "payload": {"ok": True},
        "dedupe_key": "manual:test",
        "max_attempts": 4,
    }
    first = client.post(f"/v1/webhook-endpoints/{endpoint['id']}/deliver", json=body)
    second = client.post(f"/v1/webhook-endpoints/{endpoint['id']}/deliver", json=body)
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["job_id"] == second.json()["job_id"]

    public_endpoint = _create_endpoint(client, boundary="public")
    blocked = client.post(
        f"/v1/webhook-endpoints/{public_endpoint['id']}/deliver",
        json={**body, "dedupe_key": "manual:blocked", "data_boundary": "confidential"},
    )
    assert blocked.status_code == 422

    removed = client.delete(
        f"/v1/webhook-endpoints/{endpoint['id']}",
        params={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert removed.status_code == 204
    assert client.get(
        "/v1/webhook-endpoints", params={"project_id": PROJECT_ID}
    ).json()["items"] == [public_endpoint]


def test_outbound_delivery_signs_exact_body_and_records_receipt(
    client, engine, monkeypatch
):
    monkeypatch.setenv("BOSS_TEST_WEBHOOK_SECRET", WEBHOOK_SECRET)
    endpoint = _create_endpoint(client)
    queued = client.post(
        f"/v1/webhook-endpoints/{endpoint['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "event_type": "test.completed",
            "payload": {"answer": 42},
            "dedupe_key": "manual:signed",
        },
    ).json()
    with engine.begin() as connection:
        leased = JobLeaseService(get_settings()).claim_job(
            connection,
            "worker-webhook",
            ["webhook.outbound.deliver"],
            60,
        )
    assert leased and leased["id"] == queued["job_id"]

    captured = {}

    def fake_post(url, *, content, headers, timeout):
        captured.update(
            {"url": url, "content": content, "headers": headers, "timeout": timeout}
        )
        return httpx.Response(204, text="")

    monkeypatch.setattr(api_main.webhooks, "_post", fake_post)
    denied = client.post(
        f"/v1/webhook-deliveries/{queued['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-webhook",
            "lease_token": "wrong-token",
        },
    )
    assert denied.status_code == 422
    assert captured == {}
    delivered = client.post(
        f"/v1/webhook-deliveries/{queued['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-webhook",
            "lease_token": leased["lease_token"],
        },
    )
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), captured["content"], hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["X-Hub-Signature-256"] == f"sha256={expected}"
    assert captured["headers"]["X-Boss-Delivery"] == queued["id"]

    with engine.begin() as connection:
        receipt = (
            connection.execute(
                text(
                    """
                SELECT status, effect_kind, created_by_job_id FROM receipts
                WHERE id = :receipt_id
                """
                ),
                {"receipt_id": delivered.json()["receipt_id"]},
            )
            .mappings()
            .one()
        )
    assert receipt == {
        "status": "succeeded",
        "effect_kind": "webhook.outbound.deliver",
        "created_by_job_id": leased["id"],
    }

    replay = client.post(
        f"/v1/webhook-deliveries/{queued['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "created_by_job_id": leased["id"],
            "worker_id": "worker-webhook",
            "lease_token": leased["lease_token"],
        },
    )
    assert replay.json()["created"] is False


def test_failed_delivery_retries_then_deadletters(client, engine, monkeypatch):
    monkeypatch.setenv("BOSS_TEST_WEBHOOK_SECRET", WEBHOOK_SECRET)
    endpoint = _create_endpoint(client)
    queued = client.post(
        f"/v1/webhook-endpoints/{endpoint['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "event_type": "test.failed",
            "payload": {},
            "dedupe_key": "manual:retry",
            "max_attempts": 2,
        },
    ).json()
    lease_service = JobLeaseService(get_settings())
    attempts = iter(
        [httpx.ReadTimeout("timed out"), httpx.Response(503, text="unavailable")]
    )

    def fail_post(*args, **kwargs):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        api_main.webhooks,
        "_post",
        fail_post,
    )
    with engine.begin() as connection:
        first_lease = lease_service.claim_job(
            connection, "worker-webhook", ["webhook.outbound.deliver"], 60
        )
    failed = client.post(
        f"/v1/webhook-deliveries/{queued['id']}/deliver",
        json={
            "project_id": PROJECT_ID,
            "created_by_job_id": first_lease["id"],
            "worker_id": "worker-webhook",
            "lease_token": first_lease["lease_token"],
        },
    )
    assert failed.json()["status"] == "failed"
    assert "timed out" in failed.json()["message"]
    with engine.begin() as connection:
        retried = lease_service.fail(
            connection,
            "worker-webhook",
            first_lease["id"],
            first_lease["lease_token"],
            {"message": "unavailable"},
            True,
            5,
        )
        state = connection.execute(
            text("SELECT status FROM webhook_deliveries WHERE id = :id"),
            {"id": queued["id"]},
        ).scalar_one()
        connection.execute(
            text(
                "UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": queued["job_id"]},
        )
    assert retried["status"] == "failed_retryable"
    assert state == "retrying"

    with engine.begin() as connection:
        second_lease = lease_service.claim_job(
            connection, "worker-webhook", ["webhook.outbound.deliver"], 60
        )
    assert second_lease["attempt"] == 2
    assert (
        client.post(
            f"/v1/webhook-deliveries/{queued['id']}/deliver",
            json={
                "project_id": PROJECT_ID,
                "created_by_job_id": second_lease["id"],
                "worker_id": "worker-webhook",
                "lease_token": second_lease["lease_token"],
            },
        ).json()["status"]
        == "failed"
    )
    with engine.begin() as connection:
        final = lease_service.fail(
            connection,
            "worker-webhook",
            second_lease["id"],
            second_lease["lease_token"],
            {"message": "unavailable"},
            True,
            10,
        )
        delivery = (
            connection.execute(
                text(
                    "SELECT status, last_error FROM webhook_deliveries WHERE id = :id"
                ),
                {"id": queued["id"]},
            )
            .mappings()
            .one()
        )
    assert final["status"] == "deadlettered"
    assert delivery["status"] == "deadlettered"
    assert delivery["last_error"]["deadletter_id"] == final["deadletter_id"]

    feed = client.get(
        "/v1/deadletters", params={"project_id": PROJECT_ID}
    )
    assert feed.status_code == 200
    surfaced = next(
        item
        for item in feed.json()["items"]
        if item["id"] == final["deadletter_id"]
    )
    assert surfaced["source_kind"] == "job"
    assert surfaced["source_id"] == queued["job_id"]
    assert surfaced["related_entity_type"] == "webhook_delivery"
    assert surfaced["related_entity_id"] == queued["id"]
    assert surfaced["related_status"] == "deadlettered"


def test_webhook_out_subscription_creates_durable_delivery(engine, client, monkeypatch):
    monkeypatch.setenv("BOSS_TEST_WEBHOOK_SECRET", WEBHOOK_SECRET)
    endpoint = _create_endpoint(client)
    with engine.begin() as connection:
        create_event_subscription(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "webhook_out",
            endpoint["id"],
            config={"max_attempts": 5},
        )
        event_id = emit_event(
            connection,
            PROJECT_ID,
            "code_task.pr_opened",
            "software-factory",
            "code_task_run",
            "crun_webhook",
            {"data_boundary": "internal", "pull_request_url": "https://example.test/1"},
        )
        delivery = (
            connection.execute(
                text(
                    """
                SELECT event_id, event_type, status FROM webhook_deliveries
                WHERE endpoint_id = :endpoint_id
                """
                ),
                {"endpoint_id": endpoint["id"]},
            )
            .mappings()
            .one()
        )
        job = (
            connection.execute(
                text(
                    """
                SELECT kind, max_attempts FROM jobs
                WHERE kind = 'webhook.outbound.deliver'
                """
                )
            )
            .mappings()
            .one()
        )
    assert delivery == {
        "event_id": event_id,
        "event_type": "code_task.pr_opened",
        "status": "pending",
    }
    assert job == {"kind": "webhook.outbound.deliver", "max_attempts": 5}


def test_invalid_inbound_signature_is_auth_exempt_and_audited(
    client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    monkeypatch.setattr(settings, "webhook_inbound_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    raw = json.dumps(_github_payload("pull_request"), separators=(",", ":")).encode()
    response = client.post(
        "/v1/webhooks/inbound/github",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-rejected",
        },
    )
    assert response.status_code == 401
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM inbound_webhooks")
            ).scalar_one()
            == 0
        )
        rejected = connection.execute(
            text(
                """
                SELECT data FROM events
                WHERE event_type = 'webhook.inbound.rejected'
                """
            )
        ).scalar_one()
    assert rejected == {
        "source": "github",
        "delivery_id": "delivery-rejected",
        "reason": "invalid_signature",
    }


def test_inbound_webhook_is_inert_when_secret_is_not_configured(
    client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    monkeypatch.setattr(settings, "webhook_inbound_secret", "")
    monkeypatch.setattr(settings, "webhook_inbound_secrets_json", "")
    with engine.begin() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM inbound_webhooks")
        ).scalar_one()

    response = client.post(
        "/v1/webhooks/inbound/github",
        content=b"not-json-and-must-not-be-parsed",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "delivery-unconfigured",
            "X-GitHub-Event": "issues",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Webhook source is not configured."
    with engine.begin() as connection:
        after = connection.execute(
            text("SELECT count(*) FROM inbound_webhooks")
        ).scalar_one()
    assert after == before


@pytest.mark.parametrize(
    "github_event", ["pull_request", "check_suite", "issue_comment"]
)
def test_valid_github_events_normalize_dispatch_and_dedupe(
    github_event, client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    monkeypatch.setattr(settings, "webhook_inbound_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(
        settings,
        "webhook_project_map_json",
        json.dumps({"seanheiney/boss": PROJECT_ID}),
    )
    with engine.begin() as connection:
        seed_default_event_subscriptions(connection, PROJECT_ID)
    raw = json.dumps(_github_payload(github_event), separators=(",", ":")).encode()
    delivery_id = f"delivery-{github_event}"
    headers = _signed_headers(raw, github_event, delivery_id)

    first = client.post("/v1/webhooks/inbound/github", content=raw, headers=headers)
    duplicate = client.post("/v1/webhooks/inbound/github", content=raw, headers=headers)
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["event_id"] == first.json()["event_id"]

    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM inbound_webhooks WHERE delivery_key = :key"),
                {"key": delivery_id},
            ).scalar_one()
            == 1
        )
        event = connection.execute(
            text("SELECT data FROM events WHERE id = :id"),
            {"id": first.json()["event_id"]},
        ).scalar_one()
        expected_kind = (
            "github.pr_review" if github_event == "pull_request" else "github.triage"
        )
        jobs = connection.execute(
            text(
                """
                SELECT count(*) FROM jobs
                WHERE kind = :kind
                  AND payload->>'event_id' = :event_id
                """
            ),
            {"kind": expected_kind, "event_id": first.json()["event_id"]},
        ).scalar_one()
    assert event["normalized"]["entity_type"] == github_event
    assert jobs == 1


def test_unmapped_repository_is_accepted_but_not_dispatched(
    client, engine, monkeypatch
):
    settings = api_main.inbound_webhooks.settings
    monkeypatch.setattr(settings, "webhook_inbound_secret", WEBHOOK_SECRET)
    monkeypatch.setattr(settings, "webhook_project_map_json", "{}")
    raw = json.dumps(
        _github_payload("pull_request", repo="other/repository"),
        separators=(",", ":"),
    ).encode()
    response = client.post(
        "/v1/webhooks/inbound/github",
        content=raw,
        headers=_signed_headers(raw, "pull_request", "delivery-unmapped"),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "unrouted"
    with engine.begin() as connection:
        event_type = connection.execute(
            text("SELECT event_type FROM events WHERE id = :id"),
            {"id": response.json()["event_id"]},
        ).scalar_one()
        jobs = connection.execute(
            text(
                """
                SELECT count(*) FROM jobs
                WHERE payload->>'event_id' = :event_id
                """
            ),
            {"event_id": response.json()["event_id"]},
        ).scalar_one()
    assert event_type == "webhook.inbound.unrouted"
    assert jobs == 0


def test_webhook_service_has_no_budget_entitlement_or_commercial_imports():
    module_path = Path(__file__).parents[1] / "app" / "services" / "webhooks.py"
    text_source = module_path.read_text(encoding="utf-8")
    for forbidden in ("budget", "entitlements", "commercial"):
        assert f"import {forbidden}" not in text_source
        assert f"from .{forbidden}" not in text_source
