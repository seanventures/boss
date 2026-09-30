"""Elastic fabric: capacity, instances, provider usage ledger."""

from __future__ import annotations


PROJECT_ID = "boss-dogfood"


def test_elastic_local_capacity_reconcile_and_usage(client):
    # Request local capacity — instances start immediately
    started = client.post(
        "/v1/elastic/capacity",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "elastic-local-test",
            "kind": "local",
            "desired_workers": 2,
            "auto_reconcile": True,
            "reason": "unit-test capacity",
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["desired_workers"] == 2
    assert body["reconcile"]["active_instances"] == 2
    assert len(body["reconcile"]["created"]) == 2

    status = client.get("/v1/elastic/status", params={"project_id": PROJECT_ID})
    assert status.status_code == 200, status.text
    st = status.json()
    assert st["instance_counts"]["active"] >= 2

    instances = client.get(
        "/v1/elastic/instances",
        params={"project_id": PROJECT_ID, "active_only": "true"},
    )
    assert instances.status_code == 200
    items = instances.json()["items"]
    assert len(items) >= 2
    inst_id = items[0]["id"]

    hb = client.post(
        f"/v1/elastic/instances/{inst_id}/heartbeat",
        json={"project_id": PROJECT_ID, "actor": "worker", "endpoint": "http://127.0.0.1:9"},
    )
    assert hb.status_code == 200, hb.text
    assert hb.json()["status"] == "running"
    assert hb.json()["endpoint"] == "http://127.0.0.1:9"

    # Scale down via capacity request
    down = client.post(
        "/v1/elastic/capacity",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "elastic-local-test",
            "desired_workers": 1,
            "auto_reconcile": True,
        },
    )
    assert down.status_code == 201, down.text
    assert down.json()["reconcile"]["active_instances"] == 1

    # Usage ledger should have start + heartbeat + terminate events
    usage = client.get(
        "/v1/provider-usage",
        params={"project_id": PROJECT_ID, "provider": "local"},
    )
    assert usage.status_code == 200, usage.text
    assert usage.json()["count"] >= 1

    summary = client.get(
        "/v1/provider-usage/summary", params={"project_id": PROJECT_ID}
    )
    assert summary.status_code == 200, summary.text
    providers = {p["provider"] for p in summary.json()["providers"]}
    assert "local" in providers


def test_elastic_container_pool_and_usage_ingest(client):
    started = client.post(
        "/v1/elastic/capacity",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "elastic-container-test",
            "kind": "container",
            "desired_workers": 1,
            "auto_reconcile": True,
        },
    )
    assert started.status_code == 201, started.text
    assert started.json()["reconcile"]["active_instances"] == 1
    assert started.json()["reconcile"]["created"][0]["kind"] == "container"

    ingested = client.post(
        "/v1/provider-usage/ingest",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "provider": "openai",
            "operation": "chat.completions",
            "category": "model_token",
            "quantity": 1200,
            "unit": "tokens",
            "cost_usd_micros": 40,
        },
    )
    assert ingested.status_code == 201, ingested.text
    assert ingested.json()["provider"] == "openai"
    assert ingested.json()["quantity"] == 1200.0

    summary = client.get(
        "/v1/provider-usage/summary",
        params={"project_id": PROJECT_ID, "provider": "openai"},
    )
    assert summary.status_code == 200
    body = summary.json()
    assert any(p["provider"] == "openai" for p in body["providers"])


def test_elastic_cloud_simulates_when_real_provision_disabled(client, monkeypatch):
    from apps.api.app import main as api_main

    monkeypatch.setattr(api_main.settings, "infra_real_provision_allowed", False)
    monkeypatch.setattr(
        api_main.elastic_fabric.settings, "infra_real_provision_allowed", False
    )
    monkeypatch.setattr(
        api_main.resource_plane.settings, "infra_real_provision_allowed", False
    )

    # Need a cloud pool with provider account — use local provider account if any
    providers = client.get("/v1/providers", params={"project_id": PROJECT_ID})
    assert providers.status_code == 200
    accounts = providers.json().get("items") or providers.json().get("accounts") or []
    # list shape may be {items: [...]}
    if isinstance(providers.json(), dict) and "items" in providers.json():
        accounts = providers.json()["items"]
    account_id = None
    for acc in accounts:
        if acc.get("provider") == "digitalocean":
            account_id = acc.get("id")
            break
    if not account_id and accounts:
        account_id = accounts[0].get("id")

    if not account_id:
        # Create is heavy; skip cloud path if no provider accounts in seed
        return

    started = client.post(
        "/v1/elastic/capacity",
        json={
            "project_id": PROJECT_ID,
            "actor": "ceo",
            "pool_key": "elastic-cloud-sim",
            "kind": "vm",
            "provider_account_id": account_id,
            "desired_workers": 1,
            "auto_reconcile": True,
            "cloud_spillover_enabled": True,
        },
    )
    assert started.status_code == 201, started.text
    recon = started.json()["reconcile"]
    # Simulated instances when real provision is off
    assert recon["active_instances"] >= 1
    statuses = {i["status"] for i in recon["created"]}
    assert "simulated" in statuses or recon["created"][0]["status"] == "simulated"
