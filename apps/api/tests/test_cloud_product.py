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
    assert features["managed_workers"]["status"] == "planned"
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
    assert "pending_sessions" in payload["easy_connect"]
    assert payload["managed_workers"]["status"] == "planned"
    assert payload["managed_workers"]["available_now"] is False
    assert "linked_capabilities" in payload


def test_cloud_comparison_and_capabilities(client):
    comparison = client.get(
        "/v1/cloud/comparison", params={"project_id": PROJECT_ID}
    )
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    assert body["project_id"] == PROJECT_ID
    assert len(body["rows"]) >= 8
    keys = {row["capability"] for row in body["rows"]}
    assert any("BYOK" in k or "BYOK" in str(body["rows"]) for k in keys) or any(
        "BYOK" in row.get("capability", "") for row in body["rows"]
    )
    assert body["connect_path"]["methods"] == ["browser", "device"]

    caps = client.get(
        "/v1/cloud/capabilities", params={"project_id": PROJECT_ID}
    )
    assert caps.status_code == 200, caps.text
    cbody = caps.json()
    assert cbody["summary"]["local_count"] >= 1
    assert isinstance(cbody["local_capabilities"], list)
    assert isinstance(cbody["cloud_locked"], list)

    workers = client.get("/v1/cloud/workers", params={"project_id": PROJECT_ID})
    assert workers.status_code == 200, workers.text
    wbody = workers.json()
    assert wbody["status"] == "planned"
    assert wbody["available_now"] is False
    assert wbody["enrollment"]["ready"] is False


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

    # Cloud catalog remains readable while unlinked / unconfigured
    catalog = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert catalog.status_code == 200


def test_cloud_unlink_without_link_is_404(client):
    response = client.post(
        "/v1/cloud/unlink",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "reason": "test_no_link",
        },
    )
    # No active link on default dogfood community → 404
    assert response.status_code == 404, response.text


def test_cloud_easy_connect_rejects_bad_method(client):
    response = client.post(
        "/v1/cloud/easy-connect",
        json={"project_id": PROJECT_ID, "method": "magic", "actor": "ceo"},
    )
    assert response.status_code == 422
