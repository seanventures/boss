"""BOSS-245 / PRV-10 — provider adapter conformance + rollout gates."""

from __future__ import annotations

from boss_engine.services.provider_conformance import (
    CONFORMANCE_CHECKLIST,
    build_provider_conformance_report,
)


def test_conformance_checklist_covers_prv10_dimensions():
    ids = {item["id"] for item in CONFORMANCE_CHECKLIST}
    required = {
        "declared_capabilities",
        "schema_normalization",
        "auth_secret_refs",
        "pagination",
        "retry_backoff_rate_limits",
        "idempotency",
        "dedupe",
        "receipts",
        "tenant_isolation",
        "partial_failure",
        "revocation",
        "stale_health",
        "unsupported_operations",
        "sandbox_replay_fixtures",
        "real_mode_gates",
    }
    assert required.issubset(ids)


def test_build_report_lists_available_and_planned():
    report = build_provider_conformance_report()
    assert report["product"] == "boss_provider_conformance"
    assert report["mode"] == "simulated"
    assert report["summary"]["adapter_count"] >= 5
    keys = {a["provider_key"] for a in report["adapters"]}
    assert "aws" in keys
    assert "azure" in keys
    assert "gcp" in keys
    assert "digitalocean" in keys
    assert "ovhcloud" in keys
    by_key = {a["provider_key"]: a for a in report["adapters"]}
    assert by_key["aws"]["adapter_status"] == "available"
    assert by_key["aws"]["rollout_ready"] is True
    assert by_key["gcp"]["adapter_status"] == "planned"
    assert by_key["gcp"]["rollout_ready"] is False
    # Simulated tests never claim real-mode success.
    for adapter in report["adapters"]:
        tc = adapter["test_connection"]
        assert tc["mode"] == "simulated"
        assert tc["real_mode"] is False
        if adapter["adapter_status"] == "available":
            assert tc["status"] == "passed"
        else:
            assert tc["status"] == "unsupported"


def test_filter_by_provider_key():
    report = build_provider_conformance_report(provider_key="azure")
    assert report["summary"]["adapter_count"] == 1
    assert report["adapters"][0]["provider_key"] == "azure"
    assert report["adapters"][0]["capability_count"] >= 1


def test_unknown_provider_raises():
    try:
        build_provider_conformance_report(provider_key="not-a-cloud")
        assert False, "expected ValueError"
    except ValueError as error:
        assert "Unknown provider" in str(error)


def test_api_provider_conformance(client):
    response = client.get("/v1/provider-conformance")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requirement"] == "PRV-10 / BOSS-245"
    assert body["summary"]["available"] >= 1
    assert body["summary"]["planned"] >= 1
    assert "rollout_ready" in body["summary"]
    aws = next(a for a in body["adapters"] if a["provider_key"] == "aws")
    assert aws["rollout_ready"] is True
    assert aws["test_connection"]["real_mode"] is False
    assert any(c["id"] == "receipts" for c in aws["checklist"])


def test_api_multi_cloud_provider_conformance_alias(client):
    response = client.get("/v1/multi-cloud/provider-conformance")
    assert response.status_code == 200, response.text
    assert response.json()["product"] == "boss_provider_conformance"


def test_api_provider_conformance_filter_and_404(client):
    ok = client.get("/v1/provider-conformance", params={"provider_key": "gcp"})
    assert ok.status_code == 200
    assert ok.json()["adapters"][0]["rollout_ready"] is False
    missing = client.get(
        "/v1/provider-conformance", params={"provider_key": "nope"}
    )
    assert missing.status_code == 404
