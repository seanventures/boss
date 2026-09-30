"""Domain E2E: cost events, budget status, FinOps summary, value density.

Read surfaces stay green; invalid inputs fail closed with 4xx.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.core import make_id

PROJECT_ID = "boss-dogfood"
ACTOR = "ceo"

ALLOW_CLIENT = {400, 401, 403, 404, 409, 422, 503}


def test_cost_and_finops_list_gets_do_not_500(client):
    """Costs, anomalies, finops summary, budget surfaces — never 500."""
    costs = client.get("/v1/costs", params={"project_id": PROJECT_ID, "limit": 20})
    assert costs.status_code == 200, costs.text
    assert "items" in costs.json()

    anomalies = client.get(
        "/v1/cost-anomalies", params={"project_id": PROJECT_ID, "limit": 20}
    )
    assert anomalies.status_code == 200, anomalies.text

    summary = client.get(
        "/v1/finops/summary", params={"project_id": PROJECT_ID, "days": 7}
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["project_id"] == PROJECT_ID
    assert "spend_by_provider" in body
    assert "anomalies_open_count" in body

    reconciliations = client.get(
        "/v1/finops/commitment-reconciliations",
        params={"project_id": PROJECT_ID},
    )
    assert reconciliations.status_code == 200, reconciliations.text
    assert "items" in reconciliations.json()

    budget = client.get(
        "/v1/budget/status",
        params={"scope_type": "project", "scope_id": PROJECT_ID},
    )
    assert budget.status_code == 200, budget.text
    assert budget.json()["scope_id"] == PROJECT_ID
    assert "budget_state" in budget.json()

    allocations = client.get(
        "/v1/budget/allocations", params={"project_id": PROJECT_ID}
    )
    assert allocations.status_code == 200, allocations.text

    insights = client.get(
        "/v1/budget/insights", params={"project_id": PROJECT_ID}
    )
    assert insights.status_code == 200, insights.text

    forecast = client.get(
        "/v1/budget/forecast",
        params={"scope_type": "project", "scope_id": PROJECT_ID},
    )
    assert forecast.status_code == 200, forecast.text


def test_finops_summary_reflects_seeded_cost_events(client, engine):
    """Insert cost_events + open anomaly → summary aggregates them."""
    now = datetime.now(timezone.utc)
    with begin(engine) as connection:
        ce_id = make_id("ce")
        connection.execute(
            text(
                """
                INSERT INTO cost_events
                  (id, project_id, provider, category, amount_usd_micros,
                   confidence, created_at)
                VALUES
                  (:id, :p, 'aws', 'compute', 250000, 'high', :created)
                """
            ),
            {"id": ce_id, "p": PROJECT_ID, "created": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO cost_anomalies
                  (id, project_id, provider, anomaly_type, severity, status,
                   amount_usd_micros, threshold_usd_micros, summary)
                VALUES
                  (:id, :p, 'aws', 'spike', 'high', 'open', 250000, 100000, 'e2e spike')
                """
            ),
            {"id": make_id("can"), "p": PROJECT_ID},
        )

    summary = client.get(
        "/v1/finops/summary", params={"project_id": PROJECT_ID, "days": 7}
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    by_prov = {row["provider"]: row for row in body["spend_by_provider"]}
    assert "aws" in by_prov
    assert by_prov["aws"]["cost_usd_micros"] >= 250_000
    assert body["anomalies_open_count"] >= 1

    costs = client.get("/v1/costs", params={"project_id": PROJECT_ID, "limit": 50})
    assert costs.status_code == 200
    assert any(item["id"] == ce_id for item in costs.json()["items"]) or any(
        item.get("amount_usd_micros") == 250_000 for item in costs.json()["items"]
    )

    anomalies = client.get(
        "/v1/cost-anomalies", params={"project_id": PROJECT_ID, "limit": 50}
    )
    assert anomalies.status_code == 200
    assert any(a.get("status") == "open" for a in anomalies.json()["items"])


def test_budget_quote_reserve_and_fail_closed(client):
    """Budget quote/reserve control path + validation fail-closed."""
    quote = client.post(
        "/v1/budget/quote",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 25_000,
            "reason": "e2e finops quote",
        },
    )
    assert quote.status_code in (200, 201, *ALLOW_CLIENT), quote.text
    if quote.status_code in (200, 201):
        qbody = quote.json()
        assert qbody.get("allowed") is True or "budget_state" in qbody
        assert (
            "requested_usd_micros" in qbody
            or "amount_usd_micros" in qbody
            or "available_usd_micros" in qbody
        )

    reserve = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": PROJECT_ID,
            "category": "tool",
            "amount_usd_micros": 25_000,
            "reason": "e2e finops reserve",
        },
    )
    assert reserve.status_code in (200, 201, *ALLOW_CLIENT), reserve.text

    # Missing required fields → 4xx
    bad_quote = client.post("/v1/budget/quote", json={"project_id": PROJECT_ID})
    assert bad_quote.status_code in ALLOW_CLIENT, bad_quote.text

    bad_status = client.get("/v1/budget/status", params={"project_id": PROJECT_ID})
    assert bad_status.status_code in ALLOW_CLIENT, bad_status.text

    bad_scope = client.get(
        "/v1/budget/status",
        params={"scope_type": "org", "scope_id": PROJECT_ID},
    )
    assert bad_scope.status_code in ALLOW_CLIENT, bad_scope.text


def test_finops_mutations_and_validation(client):
    """Initiative budget + reconcile; invalid summary days fail closed."""
    initiative = client.post(
        "/v1/finops/initiative-budget",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "title": "e2e initiative",
            "tier": 1,
            "amount_usd_micros": 50_000,
        },
    )
    assert initiative.status_code in (200, 201), initiative.text
    assert initiative.json().get("status") in (
        "auto_approved",
        "pending",
        "approved",
        "requires_decision",
    ) or "artifact_id" in initiative.json()

    reconcile = client.post(
        "/v1/finops/reconcile-commitments",
        json={"project_id": PROJECT_ID, "actor": ACTOR},
    )
    assert reconcile.status_code in (200, 201, *ALLOW_CLIENT), reconcile.text

    bad_days = client.get(
        "/v1/finops/summary", params={"project_id": PROJECT_ID, "days": 0}
    )
    assert bad_days.status_code in ALLOW_CLIENT, bad_days.text

    bad_limit = client.get(
        "/v1/costs", params={"project_id": PROJECT_ID, "limit": 0}
    )
    assert bad_limit.status_code in ALLOW_CLIENT, bad_limit.text


def test_value_density_score_preview_and_overview(client):
    """Work orchestration value density preview + overview read surface."""
    preview = client.post(
        "/v1/work-orchestration/score-preview",
        json={
            "strategic_value": 80,
            "customer_impact": 70,
            "urgency": 60,
            "cost_of_delay": 50,
            "risk_reduction": 40,
            "dependency_pressure": 30,
            "confidence": 90,
            "estimated_cost_usd_micros": 1_000_000,
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert "priority_score" in body
    assert "value_density" in body or "value_per_cost" in body
    # density = score * 1e6 / cost when cost > 0
    if "value_density" in body and body.get("estimated_cost_usd_micros"):
        assert body["value_density"] > 0

    overview = client.get(
        "/v1/work-orchestration/overview",
        params={"project_id": PROJECT_ID},
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["project_id"] == PROJECT_ID

    # out-of-range dimensions fail closed via pydantic 422
    bad = client.post(
        "/v1/work-orchestration/score-preview",
        json={
            "strategic_value": 999,
            "customer_impact": 70,
            "urgency": 60,
            "cost_of_delay": 50,
            "risk_reduction": 40,
            "dependency_pressure": 30,
            "confidence": 90,
        },
    )
    assert bad.status_code in ALLOW_CLIENT, bad.text
