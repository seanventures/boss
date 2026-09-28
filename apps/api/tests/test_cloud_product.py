"""BOSS Cloud catalog + Easy Connect independence tests."""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"


def test_shared_community_cloud_service_runs_without_hosted_url(engine):
    from sqlalchemy import text

    from boss_engine.config import get_settings
    from boss_engine.services.community_cloud import CommunityCloudService
    from boss_engine.services.community_edition import CommunityEditionService

    settings = get_settings().model_copy(
        update={"boss_edition": "community", "account_connect_url": None}
    )
    edition = CommunityEditionService(settings)
    cloud = CommunityCloudService(settings, edition)
    with engine.begin() as connection:
        status = edition.edition_status(connection, PROJECT_ID)
        assert status["edition_id"] == "community"
        assert status["status"] == "active"
        assert edition.list_license_entitlements(connection, PROJECT_ID)
        assert cloud.catalog(connection, PROJECT_ID)["independence"][
            "community_runs_without_cloud"
        ] is True
        assert cloud.status(connection, PROJECT_ID)["independent"] is True

        started = cloud.easy_connect(
            connection,
            project_id=PROJECT_ID,
            method="device",
            actor="ceo",
        )
        assert started["status"] == "blocked"
        assert started["receipt_id"]
        receipt = connection.execute(
            text("SELECT status, effect_kind FROM receipts WHERE id = :id"),
            {"id": started["receipt_id"]},
        ).mappings().one()
        assert receipt["status"] == "failed"
        assert receipt["effect_kind"] == "account_connect.auth_session"


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


def test_cloud_retention_unlinked_community_default(client, monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "receipt_retention_days", None)
    monkeypatch.setattr(
        api_main.cloud_product.settings, "receipt_retention_days", None
    )

    response = client.get("/v1/cloud/retention", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project_id"] == PROJECT_ID
    assert body["linked"] is False
    assert body["tier"] == "community_local"
    assert body["indefinite"] is True
    assert body["retention_days"] is None
    assert body["support_sla"] is None
    assert body["local_receipts"]["available_now"] is True
    assert body["configurable"] is False

    catalog = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert catalog.status_code == 200
    features = {item["key"]: item for item in catalog.json()["cloud_features"]}
    retention_feature = features["evidence_retention_support"]
    assert retention_feature["status"] == "partial"
    assert retention_feature["available_now"] is True
    assert retention_feature["community_local"] is True
    assert retention_feature["retention"]["tier"] == "community_local"

    # Unlinked PUT is denied (no silent drop)
    denied = client.put(
        "/v1/cloud/retention",
        json={
            "project_id": PROJECT_ID,
            "retention_days": 365,
            "reason": "should_fail_unlinked",
            "actor": "ceo",
        },
    )
    assert denied.status_code == 404, denied.text


def test_cloud_retention_env_days_when_unlinked(client, monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "receipt_retention_days", 90)
    monkeypatch.setattr(
        api_main.cloud_product.settings, "receipt_retention_days", 90
    )
    response = client.get("/v1/cloud/retention", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "community_local"
    assert body["linked"] is False
    assert body["retention_days"] == 90
    assert body["indefinite"] is False
    assert body["source"] == "BOSS_RECEIPT_RETENTION_DAYS"


def test_cloud_retention_get_put_when_linked(client):
    link = client.post(
        "/v1/account-connect/link",
        json={
            "project_id": PROJECT_ID,
            "hosted_org_id": "acct_org_retention_test",
            "connection_ref": "acctconn:retention",
            "marketplace_identity": {"publisher": "seanheiney"},
            "metadata": {"source": "retention_test"},
            "actor": "ceo",
        },
    )
    assert link.status_code == 200, link.text

    got = client.get("/v1/cloud/retention", params={"project_id": PROJECT_ID})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["linked"] is True
    assert body["tier"] == "cloud_linked"
    assert body["support_sla"] == "design_partner"
    assert body["retention_days"] == 365
    assert body["configurable"] is True
    assert body["bounds"]["min_days"] == 30
    assert body["bounds"]["max_days"] == 2555

    updated = client.put(
        "/v1/cloud/retention",
        json={
            "project_id": PROJECT_ID,
            "retention_days": 730,
            "reason": "design_partner_sla_extension",
            "actor": "ceo",
        },
    )
    assert updated.status_code == 200, updated.text
    up = updated.json()
    assert up["retention_days"] == 730
    assert up["tier"] == "cloud_linked"
    assert up["support_sla"] == "design_partner"
    assert up["receipt_id"]
    assert up["reason"] == "design_partner_sla_extension"
    assert up["updated"] is True

    again = client.get("/v1/cloud/retention", params={"project_id": PROJECT_ID})
    assert again.status_code == 200
    assert again.json()["retention_days"] == 730
    assert again.json()["source"] == "account_connect_org.metadata"

    catalog = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    features = {item["key"]: item for item in catalog.json()["cloud_features"]}
    assert features["evidence_retention_support"]["retention"]["tier"] == "cloud_linked"
    assert features["evidence_retention_support"]["retention"]["retention_days"] == 730
    assert features["evidence_retention_support"]["available_now"] is True

    bad = client.put(
        "/v1/cloud/retention",
        json={
            "project_id": PROJECT_ID,
            "retention_days": 7,
            "reason": "too_short",
            "actor": "ceo",
        },
    )
    assert bad.status_code == 422, bad.text
