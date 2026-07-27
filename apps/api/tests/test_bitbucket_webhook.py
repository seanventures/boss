"""BOSS-269 — POST /v1/webhooks/bitbucket end-to-end: fail-closed signature, pullrequest:*
normalization (fulfilled = merged), durable dedupe. Against boss_test."""
import hashlib
import hmac
import json
import uuid

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.app.db import begin

SECRET = "bb-hook-secret"


def _signed(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload():
    return {
        "repository": {"full_name": "acme-ws/shop"},
        "pullrequest": {
            "id": 9, "title": "Fix checkout", "state": "MERGED",
            "source": {"branch": {"name": "boss/code/r1"}},
            "destination": {"branch": {"name": "main"}},
            "links": {"html": {"href": "https://bitbucket.org/acme-ws/shop/pull-requests/9"}},
            "merge_commit": {"hash": "abc123"},
        },
    }


def _post(client, body: bytes, sig: str, delivery: str):
    return client.post("/v1/webhooks/bitbucket", content=body, headers={
        "X-Hub-Signature": sig,
        "X-Event-Key": "pullrequest:fulfilled",
        "X-Request-UUID": delivery,
        "Content-Type": "application/json",
    })


def test_bitbucket_webhook_flow(client, engine, monkeypatch):
    monkeypatch.setattr(api_main.inbound_webhooks.settings,
                        "bitbucket_webhook_secret", SECRET)
    body = json.dumps(_payload()).encode()
    delivery = f"req-{uuid.uuid4().hex[:10]}"

    # fail-closed: wrong signature never lands
    assert _post(client, body, "sha256=deadbeef", delivery).status_code == 401

    # signed delivery lands (202 routed or 204 unrouted repo — never rejected)
    ok = _post(client, body, _signed(body), delivery)
    assert ok.status_code in (202, 204), ok.text

    with begin(engine) as c:
        row = c.execute(
            text("SELECT provider_event, action, status, payload FROM inbound_webhooks "
                 "WHERE source = 'bitbucket' AND delivery_key = :d"),
            {"d": delivery}).mappings().one()
    assert row["provider_event"] == "pullrequest" and row["action"] == "fulfilled"
    assert row["payload"]["pullrequest"]["id"] == 9

    # the emitted event carries the canonical normalized PR (merged=True)
    with begin(engine) as c:
        ev = c.execute(
            text("SELECT data FROM events WHERE data->>'delivery_id' = :d "
                 "ORDER BY created_at DESC LIMIT 1"), {"d": delivery}).scalar_one()
    assert ev["normalized"]["merged"] is True
    assert ev["normalized"]["merge_commit"] == "abc123"

    # durable dedupe: replay is acknowledged, not re-processed
    dup = _post(client, body, _signed(body), delivery)
    assert dup.status_code in (202, 204)
    with begin(engine) as c:
        n = c.execute(text("SELECT count(*) FROM inbound_webhooks WHERE delivery_key = :d"),
                      {"d": delivery}).scalar_one()
    assert n == 1


def test_bitbucket_webhook_fail_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(api_main.inbound_webhooks.settings, "bitbucket_webhook_secret", None)
    # also ensure no generic inbound secret leaks in
    monkeypatch.setattr(api_main.inbound_webhooks.settings, "webhook_inbound_secret", None)
    body = json.dumps(_payload()).encode()
    r = _post(client, body, _signed(body), "req-noconf")
    assert r.status_code == 503                       # not configured -> closed, never accepted
