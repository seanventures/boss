"""BOSS-102 Python adapter: enqueue-only, honest not_configured, no dest HTTP."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from boss_engine.services.go_webhook_dispatcher import (
    NOT_CONFIGURED_MESSAGE,
    GoWebhookDispatcherClient,
)


def _settings(**overrides):
    values = {
        "go_webhook_dispatcher_url": None,
        "go_webhook_dispatcher_token": None,
        "go_webhook_dispatcher_timeout_seconds": 5.0,
        "go_webhook_dispatcher_dry_run": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _envelope():
    return {
        "event_id": "evt_1",
        "event_type": "code_task.completed",
        "project_id": "boss-dogfood",
        "destinations": [
            {
                "id": "whend_a",
                "url": "https://receiver.example/hook",
                "secret_ref": "env:BOSS_OUTBOUND_EXAMPLE_SECRET",
            }
        ],
    }


def test_unset_url_is_not_configured_not_degraded():
    client = GoWebhookDispatcherClient(_settings())
    status = client.status()
    assert status["status"] == "not_configured"
    assert status["status"] != "degraded"
    assert status["configured"] is False
    assert status["go_runtime_shipped"] is False
    assert status["s3_go_event_dispatcher"] is False
    assert status["community_ga_required"] is False
    assert "unset" in status["message"] or "not_configured" in status["message"]


def test_enqueue_when_unset_skips_without_claiming_success():
    called = {"n": 0}

    def fail_post(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("unset dispatcher must not POST anywhere")

    client = GoWebhookDispatcherClient(_settings(), post=fail_post)
    result = client.enqueue(_envelope())
    assert result["status"] == "not_configured"
    assert result["enqueued"] is False
    assert result["message"] == NOT_CONFIGURED_MESSAGE
    assert called["n"] == 0


def test_enqueue_posts_only_to_dispatcher_never_destination():
    seen = []

    def fake_post(url, *, json, headers, timeout):
        seen.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return httpx.Response(200, json={"event_id": "evt_1", "receipts": [{"status": "dry_run"}]})

    client = GoWebhookDispatcherClient(
        _settings(
            go_webhook_dispatcher_url="http://127.0.0.1:8091",
            go_webhook_dispatcher_token="disp-token",
            go_webhook_dispatcher_dry_run=True,
        ),
        post=fake_post,
    )
    result = client.enqueue(_envelope())
    assert result["status"] == "enqueued"
    assert result["enqueued"] is True
    assert result["dry_run"] is True
    assert result["go_runtime_shipped"] is False
    assert result["s3_go_event_dispatcher"] is False
    assert len(seen) == 1
    assert seen[0]["url"] == "http://127.0.0.1:8091/v1/dispatch"
    assert "receiver.example" not in seen[0]["url"]
    assert seen[0]["json"]["dry_run"] is True
    assert seen[0]["headers"]["X-Boss-Dispatcher-Token"] == "disp-token"


def test_enqueue_failure_is_visible_not_success():
    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    client = GoWebhookDispatcherClient(
        _settings(go_webhook_dispatcher_url="http://127.0.0.1:8091"),
        post=boom,
    )
    result = client.enqueue(_envelope())
    assert result["status"] == "failed"
    assert result["enqueued"] is False
    assert result["go_runtime_shipped"] is False


def test_http_error_does_not_invent_success():
    def fake_post(url, **kwargs):
        return httpx.Response(503, text="sidecar down")

    client = GoWebhookDispatcherClient(
        _settings(go_webhook_dispatcher_url="http://go-dispatcher.internal"),
        post=fake_post,
    )
    result = client.enqueue(_envelope())
    assert result["status"] == "failed"
    assert result["enqueued"] is False
    assert result["http_status"] == 503


def test_adapter_does_not_log_or_return_raw_secret():
    secret = "super-secret-webhook-key-do-not-return"

    def fake_post(url, **kwargs):
        raise httpx.HTTPError(f"upstream echo {secret} sha256=deadbeef")

    client = GoWebhookDispatcherClient(
        _settings(go_webhook_dispatcher_url="http://127.0.0.1:8091"),
        post=fake_post,
    )
    result = client.enqueue(_envelope())
    blob = str(result)
    assert secret not in blob
    assert "sha256=" not in blob
    assert result["status"] == "failed"


def test_configured_status_still_refuses_ga_claim():
    status = GoWebhookDispatcherClient(
        _settings(go_webhook_dispatcher_url="http://127.0.0.1:8091")
    ).status()
    assert status["configured"] is True
    assert status["status"] == "configured"
    assert status["go_runtime_shipped"] is False
    assert status["s3_go_event_dispatcher"] is False
    assert status["community_ga_required"] is False
