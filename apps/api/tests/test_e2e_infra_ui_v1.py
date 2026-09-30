"""Isolated E2E: infra honesty + fail-closed create_droplet.

GET /v1/elastic/status, /v1/provider-usage/summary
must return 200 not_configured when empty, with host.connected=false and
no invented metrics. POST /v1/infra/droplets without
BOSS_INFRA_REAL_PROVISION_ALLOWED is blocked and writes a failed Receipt.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app import main as api_main


PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"

INVENTED_METRIC_KEYS = (
    "cpu_percent",
    "memory_used_bytes",
    "network_in_bytes",
    "uptime_seconds",
    "fake_connected",
    "invented",
)


def _assert_no_invented_metrics(payload: dict) -> None:
    blob = json.dumps(payload)
    assert "UndefinedTable" not in blob
    for key in INVENTED_METRIC_KEYS:
        assert key not in payload
    host = payload.get("host") or {}
    if isinstance(host, dict) and "connected" in host:
        assert host["connected"] is False


def test_e2e_elastic_status_empty_is_200_not_configured(client):
    response = client.get("/v1/elastic/status", params={"project_id": PROJECT_ID})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["host"]["connected"] is False
    assert body["instance_counts"]["total"] == 0
    assert body["instance_counts"]["active"] == 0
    assert body.get("infra_real_provision_allowed") is False
    _assert_no_invented_metrics(body)




def test_e2e_provider_usage_summary_empty_is_200_not_configured(client):
    response = client.get(
        "/v1/provider-usage/summary", params={"project_id": PROJECT_ID}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "not_configured"
    assert body.get("providers") == []
    _assert_no_invented_metrics(body)


def test_e2e_status_routes_stay_honest_after_missing_schema(client, engine):
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS provider_usage_events CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS worker_instances CASCADE"))

    elastic = client.get("/v1/elastic/status", params={"project_id": PROJECT_ID})
    usage = client.get("/v1/provider-usage/summary", params={"project_id": PROJECT_ID})

    for response, label in (
        (elastic, "elastic"),
        (usage, "provider-usage"),
    ):
        assert response.status_code == 200, f"{label}: {response.text}"
        body = response.json()
        assert body["status"] == "not_configured", label
        if "host" in body:
            assert body["host"]["connected"] is False, label
        _assert_no_invented_metrics(body)


def test_e2e_create_droplet_http_blocked_without_provision_flag(client, engine):
    previous = api_main.infrastructure.settings.infra_real_provision_allowed
    api_main.infrastructure.settings.infra_real_provision_allowed = False
    try:
        reserve = client.post(
            "/v1/budget/reserve",
            json={
                "project_id": PROJECT_ID,
                "category": "infra",
                "amount_usd_micros": 250_000,
                "reason": "E2E fail-closed droplet",
            },
        )
        assert reserve.status_code == 201, reserve.text
        reservation_id = reserve.json()["reservation_id"]

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO decisions (
                        id, project_id, task_id, question, recommendation, options,
                        risk_tier, cost_summary, deadline_at, default_action, status,
                        approved_by, approved_at
                    )
                    VALUES (
                        'dec_e2e_infra_v1_blocked',
                        :project_id,
                        NULL,
                        'Approve E2E fail-closed droplet',
                        CAST(:recommendation AS JSONB),
                        '[{"label":"Provision","action":"approve"}]'::jsonb,
                        3,
                        CAST(:cost_summary AS JSONB),
                        now() + interval '1 day',
                        'hold',
                        'approved',
                        :actor,
                        now()
                    )
                    """
                ),
                {
                    "project_id": PROJECT_ID,
                    "actor": ACTOR,
                    "recommendation": json.dumps(
                        {
                            "action": "review_infra_provisioning",
                            "reservation_id": reservation_id,
                            "title": "E2E blocked droplet",
                            "region": "nyc3",
                            "size": "s-1vcpu-1gb",
                        }
                    ),
                    "cost_summary": json.dumps({"reservation_id": reservation_id}),
                },
            )

        response = client.post(
            "/v1/infra/droplets",
            json={
                "project_id": PROJECT_ID,
                "decision_id": "dec_e2e_infra_v1_blocked",
                "reservation_id": reservation_id,
                "dedupe_key": "e2e-infra-ui-v1-blocked-droplet",
                "title": "E2E blocked droplet",
                "region": "nyc3",
                "size": "s-1vcpu-1gb",
                "ttl_minutes": 30,
                "tags": ["e2e-infra-ui"],
            },
        )
    finally:
        api_main.infrastructure.settings.infra_real_provision_allowed = previous

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "blocked"
    assert body.get("receipt_id")
    assert "BOSS_INFRA_REAL_PROVISION_ALLOWED" in (body.get("message") or "")

    with engine.begin() as connection:
        receipt = (
            connection.execute(
                text("SELECT status, effect_kind FROM receipts WHERE id = :id"),
                {"id": body["receipt_id"]},
            )
            .mappings()
            .one()
        )
        resource_receipt = (
            connection.execute(
                text(
                    """
                    SELECT status, action, error
                    FROM resource_receipts
                    WHERE receipt_id = :receipt_id
                    """
                ),
                {"receipt_id": body["receipt_id"]},
            )
            .mappings()
            .one()
        )
        reservation = connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :id"),
            {"id": reservation_id},
        ).scalar_one()

    assert receipt["status"] == "failed"
    assert receipt["effect_kind"] == "infra.digitalocean.droplet.create"
    assert resource_receipt["status"] == "failed"
    assert resource_receipt["action"] == "create"
    assert reservation == "released"
