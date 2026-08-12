"""BOSS Cloud catalog + Easy Connect independence tests."""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"


def test_cloud_catalog_community_independent(client):
    response = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] == PROJECT_ID
    assert payload["independence"]["community_runs_without_cloud"] is True
    assert payload["independence"]["account_connect_required_for_local_work"] is False
    assert payload["independence"]["easy_connect_available"] is True
    features = {item["key"]: item for item in payload["cloud_features"]}
    assert features["durable_control_plane"]["community_local"] is True
    assert features["durable_control_plane"]["available_now"] is True
    assert features["managed_workers"]["community_local"] is False
    assert "packaging" in payload
    assert "community" in payload["packaging"]


def test_cloud_status_independent_without_account_connect(client):
    response = client.get("/v1/cloud/status", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["independent"] is True
    assert payload["edition"]["edition_id"] in {
        "community",
        "cloud",
        "enterprise_cloud",
        "enterprise_self_hosted",
    }
    assert "easy_connect" in payload
    assert set(payload["easy_connect"]["methods"]) == {"browser", "device"}


def test_cloud_easy_connect_device_fail_closed_without_url(client, monkeypatch):
    # Ensure Account Connect URL unset for independence path
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "account_connect_url", None)
    monkeypatch.setattr(
        api_main.cloud_product.settings, "account_connect_url", None
    )
    monkeypatch.setattr(
        api_main.commercial.settings, "account_connect_url", None
    )
    monkeypatch.setattr(
        api_main.commercial.account_connect_adapter.settings,
        "account_connect_url",
        None,
    )

    started = client.post(
        "/v1/cloud/easy-connect",
        json={
            "project_id": PROJECT_ID,
            "method": "device",
            "actor": "ceo",
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["method"] == "device"
    assert body["status"] == "blocked"
    assert body.get("user_code") in (None, "")
    assert "ACCOUNT_CONNECT" in (body.get("message") or "").upper() or "not configured" in (
        body.get("message") or ""
    ).lower()

    # Core Community work still available
    health = client.get("/health")
    assert health.status_code == 200
    providers = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert providers.status_code == 200


def test_cloud_easy_connect_rejects_bad_method(client):
    response = client.post(
        "/v1/cloud/easy-connect",
        json={"project_id": PROJECT_ID, "method": "magic", "actor": "ceo"},
    )
    assert response.status_code == 422
