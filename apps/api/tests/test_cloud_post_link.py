"""Post-link BOSS Cloud surface under Community-mounted /v1/cloud/*."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT_ID = "boss-dogfood"


def test_paid_cloud_routes_keep_hosted_operations() -> None:
    """The paid composition retains the existing Cloud API methods."""

    assert type(api_main.commercial).__name__ == "CommercialService"
    assert type(api_main.cloud_product).__name__ == "CloudProductService"
    paths = api_main.app.openapi()["paths"]
    methods = {
        path: set(operations)
        for path, operations in paths.items()
        if path.startswith("/v1/cloud/")
    }
    assert "post" in methods["/v1/cloud/workers/enroll"]
    assert "post" in methods["/v1/cloud/sync"]
    assert "get" in methods["/v1/cloud/entitlements"]
    assert {"get", "post"} <= methods["/v1/cloud/team-invites"]
    assert "get" in methods["/v1/cloud/marketplace"]
    assert {"get", "put"} <= methods["/v1/cloud/retention"]
    assert "post" in methods["/v1/cloud/easy-connect"]


def _blocked_reason(response) -> Optional[str]:
    body = response.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("blocked_reason")
    if isinstance(body.get("blocked_reason"), str):
        return body["blocked_reason"]
    return None


def _link_cloud(client, hosted_org_id: str = "acct_org_cloud_post_link") -> str:
    """Link via commercial service so Community edition tests stay independent of paid mounts."""
    with api_main.engine.begin() as connection:
        edition = api_main.commercial.link_account_connect_org(
            connection,
            PROJECT_ID,
            hosted_org_id=hosted_org_id,
            actor="ceo",
            metadata={"source": "test_cloud_post_link"},
        )
    assert edition.get("account_connect_org")
    return hosted_org_id


def test_unlinked_sync_team_marketplace_fail_closed(client):
    sync = client.post(
        "/v1/cloud/sync",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert sync.status_code == 404, sync.text
    assert _blocked_reason(sync) == "cloud_not_linked"

    create_invite = client.post(
        "/v1/cloud/team-invites",
        json={
            "project_id": PROJECT_ID,
            "email": "ops@example.com",
            "actor": "ceo",
        },
    )
    assert create_invite.status_code == 404, create_invite.text
    assert _blocked_reason(create_invite) == "cloud_not_linked"

    list_invites = client.get(
        "/v1/cloud/team-invites", params={"project_id": PROJECT_ID}
    )
    assert list_invites.status_code == 404, list_invites.text
    assert _blocked_reason(list_invites) == "cloud_not_linked"

    marketplace = client.get(
        "/v1/cloud/marketplace", params={"project_id": PROJECT_ID}
    )
    assert marketplace.status_code == 200, marketplace.text
    mbody = marketplace.json()
    assert mbody["items"] == []
    assert mbody["linked"] is False
    assert mbody["blocked_reason"] == "cloud_not_linked"

    enroll = client.post(
        "/v1/cloud/workers/enroll",
        json={
            "project_id": PROJECT_ID,
            "worker_node_id": "node-unlinked",
            "actor": "ceo",
        },
    )
    assert enroll.status_code == 404, enroll.text
    assert _blocked_reason(enroll) == "cloud_not_linked"

    # Community remains usable
    health = client.get("/health")
    assert health.status_code == 200
    catalog = client.get("/v1/cloud/catalog", params={"project_id": PROJECT_ID})
    assert catalog.status_code == 200
    assert catalog.json()["independence"]["community_runs_without_cloud"] is True


def test_linked_sync_and_entitlements_with_mock_adapter(client, monkeypatch):
    hosted_org_id = _link_cloud(client, "acct_org_post_sync")

    class FakeAdapter:
        def fetch_entitlements(self, *, hosted_org_id: str, project_id: str):
            assert hosted_org_id == "acct_org_post_sync"
            assert project_id == PROJECT_ID
            return [
                {
                    "id": "lic_hosted_team",
                    "capability_key": "team_collaboration",
                    "capabilities": ["team_collaboration"],
                    "status": "active",
                    "seats": 3,
                    "metadata": {"plan": "cloud"},
                }
            ]

    monkeypatch.setattr(
        api_main.commercial, "account_connect_adapter", FakeAdapter()
    )
    monkeypatch.setattr(
        api_main.cloud_product.commercial,
        "account_connect_adapter",
        FakeAdapter(),
    )

    sync = client.post(
        "/v1/cloud/sync",
        json={
            "project_id": PROJECT_ID,
            "hosted_org_id": hosted_org_id,
            "actor": "ceo",
        },
    )
    assert sync.status_code == 200, sync.text
    sbody = sync.json()
    assert sbody["synced_entitlement_count"] == 1
    assert sbody["linked"] is True
    assert sbody["blocked_reason"] is None

    entitlements = client.get(
        "/v1/cloud/entitlements", params={"project_id": PROJECT_ID}
    )
    assert entitlements.status_code == 200, entitlements.text
    ebody = entitlements.json()
    assert ebody["linked"] is True
    assert ebody["hosted_count"] >= 1
    hosted_keys = {
        item["capability_key"]
        for item in ebody["items"]
        if str(item.get("source") or "").startswith("account_connect")
    }
    assert "team_collaboration" in hosted_keys or "account_connect" in hosted_keys


def test_linked_team_invites_with_mock_adapter(client, monkeypatch):
    _link_cloud(client, "acct_org_post_team")

    class FakeAdapter:
        def create_team_invite(
            self,
            *,
            hosted_org_id: str,
            project_id: str,
            email: str,
            display_name: Optional[str],
            role_keys,
            permissions,
            metadata,
        ):
            assert hosted_org_id == "acct_org_post_team"
            return {
                "id": "hosted_invite_1",
                "invite_id": "hosted_invite_1",
                "invite_url": "https://accounts.example/invite/1",
                "status": "pending_acceptance",
            }

    monkeypatch.setattr(
        api_main.commercial, "account_connect_adapter", FakeAdapter()
    )
    monkeypatch.setattr(
        api_main.cloud_product.commercial,
        "account_connect_adapter",
        FakeAdapter(),
    )

    created = client.post(
        "/v1/cloud/team-invites",
        json={
            "project_id": PROJECT_ID,
            "email": "teammate@example.com",
            "display_name": "Teammate",
            "role_keys": ["operator"],
            "permissions": ["command_submit"],
            "actor": "ceo",
        },
    )
    assert created.status_code == 201, created.text
    invite = created.json()
    assert invite["email"] == "teammate@example.com"
    assert invite["receipt_id"]
    assert invite["status"] in {"pending_acceptance", "blocked_configuration"}

    listed = client.get(
        "/v1/cloud/team-invites", params={"project_id": PROJECT_ID}
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert any(item["email"] == "teammate@example.com" for item in items)


def test_linked_marketplace_lists_local_rows(client, engine):
    _link_cloud(client, "acct_org_post_mkt")
    with engine.begin() as connection:
        org = connection.execute(
            text(
                """
                SELECT id FROM account_connect_orgs
                WHERE project_id = :project_id AND status = 'linked'
                ORDER BY linked_at DESC LIMIT 1
                """
            ),
            {"project_id": PROJECT_ID},
        ).mappings().first()
        assert org is not None
        connection.execute(
            text(
                """
                INSERT INTO hosted_marketplace_pack_listings (
                    id, project_id, account_connect_org_id, hosted_org_id,
                    listing_id, pack_id, version, display_name, publisher,
                    category, description, status, price_model,
                    price_usd_micros, required_entitlements, capabilities,
                    source_url, metadata
                )
                VALUES (
                    :id, :project_id, :account_connect_org_id, :hosted_org_id,
                    :listing_id, :pack_id, :version, :display_name, :publisher,
                    :category, :description, 'active', 'free',
                    0, CAST(:required_entitlements AS JSONB),
                    CAST(:capabilities AS JSONB), :source_url,
                    CAST(:metadata AS JSONB)
                )
                """
            ),
            {
                "id": "mktlist_cloud_post_1",
                "project_id": PROJECT_ID,
                "account_connect_org_id": org["id"],
                "hosted_org_id": "acct_org_post_mkt",
                "listing_id": "listing_post_1",
                "pack_id": "pack.private.demo",
                "version": "1.0.0",
                "display_name": "Demo Pack",
                "publisher": "boss-cloud",
                "category": "agents",
                "description": "Post-link marketplace listing",
                "required_entitlements": '["marketplace_private_registry"]',
                "capabilities": '["demo"]',
                "source_url": "https://accounts.example/marketplace/demo",
                "metadata": "{}",
            },
        )

    marketplace = client.get(
        "/v1/cloud/marketplace", params={"project_id": PROJECT_ID}
    )
    assert marketplace.status_code == 200, marketplace.text
    body = marketplace.json()
    assert body["linked"] is True
    assert body["blocked_reason"] is None
    assert any(
        item.get("pack_id") == "pack.private.demo" or item.get("listing_id") == "listing_post_1"
        for item in body["items"]
    )


def test_worker_enroll_receipted_and_get_workers_shows_enrollment(client):
    _link_cloud(client, "acct_org_post_workers")

    enroll = client.post(
        "/v1/cloud/workers/enroll",
        json={
            "project_id": PROJECT_ID,
            "worker_node_id": "local-node-1",
            "display_name": "Lab runner",
            "capabilities": ["shell", "git"],
            "actor": "ceo",
        },
    )
    assert enroll.status_code == 201, enroll.text
    body = enroll.json()
    assert body["worker_node_id"] == "local-node-1"
    assert body["status"] == "enrolled"
    assert body["receipt_id"]
    assert body["managed_fleets_available"] is False
    assert body.get("managed_fleet") is False

    receipts = client.get("/v1/receipts", params={"project_id": PROJECT_ID})
    assert receipts.status_code == 200
    receipt = next(
        item
        for item in receipts.json()["items"]
        if item["id"] == body["receipt_id"]
    )
    assert receipt["status"] == "succeeded"
    assert receipt["effect_kind"] == "cloud.worker_enroll"

    workers = client.get("/v1/cloud/workers", params={"project_id": PROJECT_ID})
    assert workers.status_code == 200, workers.text
    wbody = workers.json()
    assert wbody["available_now"] is False
    assert wbody["status"] == "planned"
    assert wbody["linked"] is True
    enrollments = wbody.get("local_enrollments") or []
    assert any(item["worker_node_id"] == "local-node-1" for item in enrollments)
    assert wbody["enrollment"]["managed_fleets_available"] is False
    assert wbody["enrollment"]["local_worker_enroll_available"] is True

    # Idempotent re-enroll reuses receipt
    again = client.post(
        "/v1/cloud/workers/enroll",
        json={
            "project_id": PROJECT_ID,
            "worker_node_id": "local-node-1",
            "actor": "ceo",
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["receipt_id"] == body["receipt_id"]


def test_linked_sync_fails_closed_without_account_connect_url(client, monkeypatch):
    _link_cloud(client, "acct_org_post_no_url")
    monkeypatch.setattr(api_main.settings, "account_connect_url", None)
    monkeypatch.setattr(api_main.commercial.settings, "account_connect_url", None)
    monkeypatch.setattr(
        api_main.cloud_product.settings, "account_connect_url", None
    )
    monkeypatch.setattr(
        api_main.commercial.account_connect_adapter.settings,
        "account_connect_url",
        None,
    )

    sync = client.post(
        "/v1/cloud/sync",
        json={"project_id": PROJECT_ID, "actor": "ceo"},
    )
    assert sync.status_code == 409, sync.text
    assert _blocked_reason(sync) == "account_connect_url_missing"
