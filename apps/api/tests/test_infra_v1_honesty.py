"""V1 infra honesty: missing-schema empty-200, fail-closed real provision.

Live dogfood 500'd GET /v1/elastic/status, /v1/shared-server/status, and
/v1/provider-usage/summary with UndefinedTable. Status must ensure_tables
and return honest empty — never invent host.connected=true.

Real provider writes stay fail-closed unless BOSS_INFRA_REAL_PROVISION_ALLOWED
is on, and the blocked attempt is receipted.
"""
from __future__ import annotations

from sqlalchemy import text

from apps.api.app import main as api_main
from boss_engine.services.infrastructure import InfrastructureService


PROJECT_ID = "boss-dogfood"


def _reserve_infra(client, reason: str) -> str:
    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "infra",
            "amount_usd_micros": 250_000,
            "reason": reason,
        },
    )
    assert reserve.status_code == 201, reserve.text
    return reserve.json()["reservation_id"]


def test_elastic_status_ensure_tables_honest_empty_when_missing(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS provider_usage_events CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS worker_instances CASCADE"))

    response = client.get("/v1/elastic/status", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_counts"]["total"] == 0
    assert body["instance_counts"]["active"] == 0
    assert body["host"]["connected"] is False
    assert body["status"] == "not_configured"
    assert body["infra_real_provision_allowed"] is False


def test_provider_usage_summary_ensure_tables_honest_empty_when_missing(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS provider_usage_events CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS worker_instances CASCADE"))

    response = client.get(
        "/v1/provider-usage/summary", params={"project_id": PROJECT_ID}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("providers") == []
    assert body.get("status", "not_configured") in {"not_configured", "ok"}


def test_shared_server_status_ensure_tables_never_fakes_host(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS shared_server_deploys CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS shared_server_apps CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS shared_server_package_installs CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS shared_server_nodes CASCADE"))

    response = client.get("/v1/shared-server/status", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_count"] == 0
    assert body["app_count"] == 0
    assert body["host"]["connected"] is False
    assert body["status"] == "not_configured"
    assert body["real_host_effects"] is False


def test_infra_create_droplet_fail_closed_without_real_provision_flag(
    client, engine, monkeypatch
):
    reservation_id = _reserve_infra(client, "Reserve for fail-closed droplet test.")
    request = client.post(
        "/v1/infra/droplets/requests",
        json={
            "project_id": PROJECT_ID,
            "reservation_id": reservation_id,
            "title": "Must not provision without flag",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 30,
            "tags": ["v1-honesty"],
            "actor": "ceo",
        },
    )
    assert request.status_code == 201, request.text
    decision_id = request.json()["decision_id"]

    previous = api_main.decisions.settings.infra_real_provision_allowed
    api_main.decisions.settings.infra_real_provision_allowed = True
    try:
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Approve so create_droplet can run."},
        )
    finally:
        api_main.decisions.settings.infra_real_provision_allowed = previous
    assert approve.status_code == 200, approve.text

    adapter_calls: list[dict] = []

    def _boom(**kwargs):
        adapter_calls.append(kwargs)
        raise AssertionError("provider write must not run when flag is off")

    service = InfrastructureService(api_main.settings)
    monkeypatch.setattr(service.settings, "infra_real_provision_allowed", False)
    monkeypatch.setattr(service.digitalocean, "create_droplet", _boom)

    with engine.begin() as connection:
        result = service.create_droplet(
            connection,
            project_id=PROJECT_ID,
            task_id=None,
            decision_id=decision_id,
            reservation_id=reservation_id,
            dedupe_key="infra-v1-fail-closed-droplet",
            title="Must not provision without flag",
            region="nyc3",
            size="s-1vcpu-1gb",
            ttl_minutes=30,
            tags=["v1-honesty"],
        )

    assert adapter_calls == []
    assert result["status"] == "blocked"
    assert "BOSS_INFRA_REAL_PROVISION_ALLOWED" in result["message"]
    assert result.get("receipt_id")
    assert result.get("resource_receipt_id")

    with engine.begin() as connection:
        receipt = connection.execute(
            text("SELECT status FROM receipts WHERE id = :id"),
            {"id": result["receipt_id"]},
        ).mappings().one()
        resource_receipt = connection.execute(
            text(
                """
                SELECT status, action, error
                FROM resource_receipts
                WHERE id = :id
                """
            ),
            {"id": result["resource_receipt_id"]},
        ).mappings().one()

    assert receipt["status"] == "failed"
    assert resource_receipt["status"] == "failed"
    assert resource_receipt["action"] == "create"
