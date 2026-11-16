"""From Publish, hand an app to a customer tenant or hand over its papers (BOSSAPP-1259).

No cloud is called. An SBOM is returned only when the publish already recorded one.
"""
from __future__ import annotations

import json
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers.gap_1259 import router, tenant_handoff
from boss_engine.identity import access_of
from boss_engine.identity.declarations import Authenticated
from boss_engine.services.tenant_handoff import TenantHandoffError, handoff_from_publish

IMAGE = "registry.example/billing@sha256:abc123"
RECORDED = {"bomFormat": "CycloneDX", "serialNumber": "urn:uuid:already-on-file"}


def _publish(**extra):
    body = {"app": "billing", "image_reference": IMAGE, "publish_id": "pub_1"}
    body.update(extra)
    return body


def test_handover_without_an_sbom_says_so_and_still_returns_image_and_stub():
    result = handoff_from_publish(_publish(), mode="handover")
    assert result["ok"] is True
    assert result["mode"] == "handover"
    assert result["placed"] is False
    assert result["tenant"] is None
    assert result["cloud_called"] is False
    assert result["image_reference"] == IMAGE
    assert IMAGE in result["terraform_stub"]
    assert result["terraform_stub"].startswith("# Tenant handoff stub.")
    assert "not applied" in result["terraform_stub"].lower()
    assert "sbom" not in result["terraform_stub"].lower()
    assert 'tenant =' not in result["terraform_stub"]
    assert result["sbom"] is None
    assert result["sbom_recorded"] is False
    assert result["sbom_note"] == "No SBOM is recorded."
    dumped = json.dumps(result)
    assert "bomFormat" not in dumped
    assert "components" not in dumped


def test_handover_returns_the_recorded_sbom_unchanged():
    result = handoff_from_publish(_publish(sbom=RECORDED), mode="handover")
    assert result["sbom_recorded"] is True
    assert result["sbom_note"] == "SBOM on file."
    assert result["sbom"] == RECORDED
    assert set(result["sbom"]) == set(RECORDED)
    assert result["image_reference"] == IMAGE
    assert IMAGE in result["terraform_stub"]


def test_a_string_sbom_reference_is_passed_through():
    result = handoff_from_publish(_publish(recorded_sbom="artifacts/sbom-9"), mode="handover")
    assert result["sbom"] == "artifacts/sbom-9"
    assert result["sbom_recorded"] is True


def test_blank_or_empty_sbom_is_not_invented():
    for blank in ("", "   ", {}, [], None):
        result = handoff_from_publish(_publish(sbom=blank), mode="handover")
        assert result["sbom"] is None
        assert result["sbom_recorded"] is False
        assert result["sbom_note"] == "No SBOM is recorded."


def test_packages_on_the_publish_do_not_become_an_sbom():
    result = handoff_from_publish(_publish(packages=["left-pad"]), mode="handover")
    assert result["sbom"] is None
    assert "left-pad" not in json.dumps(result)


def test_place_records_the_tenant_and_does_not_call_a_cloud(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a cloud call was attempted")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    result = handoff_from_publish(_publish(sbom=RECORDED), mode="place", tenant="acme-prod")
    assert result["placed"] is True
    assert result["tenant"] == "acme-prod"
    assert result["cloud_called"] is False
    assert result["image_reference"] == IMAGE
    assert 'tenant = "acme-prod"' in result["terraform_stub"]
    assert result["sbom"] == RECORDED


def test_place_without_an_sbom_still_returns_image_and_stub():
    result = handoff_from_publish(_publish(), mode="place", tenant="acme-prod")
    assert result["sbom_recorded"] is False
    assert result["sbom_note"] == "No SBOM is recorded."
    assert result["image_reference"] == IMAGE
    assert IMAGE in result["terraform_stub"]
    assert result["cloud_called"] is False


def test_repeat_place_is_one_ledger_row():
    ledger: list[dict] = []
    first = handoff_from_publish(_publish(), mode="place", tenant="acme-prod", ledger=ledger)
    second = handoff_from_publish(_publish(), mode="place", tenant="acme-prod", ledger=ledger)
    assert second == first
    assert second["dedupe_key"] == first["dedupe_key"]
    assert len(ledger) == 1
    handoff_from_publish(_publish(), mode="place", tenant="other", ledger=ledger)
    assert len(ledger) == 2


def test_a_different_recorded_sbom_is_a_different_handoff():
    ledger: list[dict] = []
    handoff_from_publish(_publish(sbom="one"), mode="handover", ledger=ledger)
    handoff_from_publish(_publish(sbom="two"), mode="handover", ledger=ledger)
    assert len(ledger) == 2
    assert ledger[0]["dedupe_key"] != ledger[1]["dedupe_key"]


def test_missing_image_is_a_visible_error_and_records_nothing():
    ledger: list[dict] = []
    with pytest.raises(TenantHandoffError, match="no image reference"):
        handoff_from_publish({"app": "billing", "image_reference": "  "}, mode="handover",
                             ledger=ledger)
    assert ledger == []


def test_missing_tenant_image_app_or_mode_is_a_visible_error():
    with pytest.raises(TenantHandoffError, match="Name the tenant"):
        handoff_from_publish(_publish(), mode="place", tenant="  ")
    with pytest.raises(TenantHandoffError, match="Name the app"):
        handoff_from_publish({"app": " ", "image_reference": IMAGE}, mode="handover")
    with pytest.raises(TenantHandoffError, match="place or handover"):
        handoff_from_publish(_publish(), mode="deploy")
    with pytest.raises(TenantHandoffError, match="no image reference"):
        handoff_from_publish({"app": "billing"}, mode="handover")


def test_two_calls_match_and_do_not_invent_spend():
    one = handoff_from_publish(_publish(), mode="handover")
    two = handoff_from_publish(_publish(), mode="handover")
    assert one == two
    assert "spend" not in json.dumps(one).lower()
    assert one["dedupe_key"].startswith("tenant-handoff:")


def test_route_is_declared_and_returns_the_handoff():
    assert isinstance(access_of(tenant_handoff), Authenticated)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    missing = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": IMAGE, "mode": "handover",
    })
    assert missing.status_code == 200
    body = missing.json()
    assert body["image_reference"] == IMAGE
    assert body["sbom_note"] == "No SBOM is recorded."
    assert body["sbom"] is None
    assert IMAGE in body["terraform_stub"]
    assert body["cloud_called"] is False
    again = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": IMAGE, "mode": "handover",
    })
    assert again.json() == body

    recorded = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": IMAGE, "mode": "handover",
        "recorded_sbom": RECORDED,
    })
    assert recorded.status_code == 200
    assert recorded.json()["sbom"] == RECORDED

    placed = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": IMAGE, "mode": "place", "tenant": "acme-prod",
    })
    assert placed.status_code == 200
    assert placed.json()["tenant"] == "acme-prod"
    assert placed.json()["cloud_called"] is False
    assert placed.json()["sbom_note"] == "No SBOM is recorded."

    refused = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": IMAGE, "mode": "place", "tenant": "",
    })
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Name the tenant."

    no_image = client.post("/v1/tenant-handoff", json={
        "app": "billing", "image_reference": "", "mode": "handover",
    })
    assert no_image.status_code == 422
    assert "no image reference" in no_image.json()["detail"]
