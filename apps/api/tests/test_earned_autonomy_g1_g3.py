"""BOSS-348 — earned autonomy G1 score, G2 trust gate, G3 value schedule."""

from __future__ import annotations

from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services.autonomy_engine import AutonomyEngine
from boss_engine.services.work_orchestration import WorkOrchestrationService
from boss_engine.services.work_value_scoring import rank_by_value_density


PID = "boss-dogfood"
ROLE = "metatron"


def test_g1_autonomy_score_exists_via_api(client, engine):
    """G1 — score is readable and carries trust + autonomy + risk ceiling."""
    response = client.get(
        f"/v1/roles/{ROLE}/autonomy/score",
        params={"project_id": PID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score_exists"] is True
    assert body["role_key"] == ROLE
    assert body["project_id"] == PID
    assert "trust_score" in body
    assert isinstance(body["trust_score"], int)
    assert "autonomy_level" in body
    assert "max_allowed_risk_tier" in body
    assert body["gate_mode"] == "fail_closed"
    floors = body["trust_risk_tier_floors"]
    assert floors["0"] == 0
    assert int(floors["3"]) >= int(floors["2"]) >= int(floors["1"])

    # Service path agrees with the API.
    settings = get_settings()
    with engine.begin() as connection:
        score = AutonomyEngine(settings).get_score(
            connection, project_id=PID, role_key=ROLE
        )
    assert score["trust_score"] == body["trust_score"]
    assert score["autonomy_level"] == body["autonomy_level"]


def test_g2_trust_risk_tier_gate_fail_closed(client, engine):
    """G2 — low trust cannot execute high risk tiers even with autonomy level."""
    # Drive trust_score well below the tier-2 floor (60) while giving a high
    # autonomy ladder level so the ladder alone would allow the action.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE roles
                SET trust_score = 10, autonomy_level = 4
                WHERE project_id = :p AND role_key = :r
                """
            ),
            {"p": PID, "r": ROLE},
        )

    denied = client.post(
        f"/v1/roles/{ROLE}/autonomy/check",
        json={
            "project_id": PID,
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 2,
            "sandboxed": False,
            "policy_approved": True,
        },
    )
    assert denied.status_code == 200, denied.text
    body = denied.json()
    assert body["allowed"] is False
    assert body["reason"] == "trust_score_below_risk_tier_floor"
    assert body["trust_gate_passed"] is False
    assert body["trust_score"] == 10
    assert body["min_trust_required"] >= 60
    assert body["max_allowed_risk_tier"] == 0

    # Tier 0 stays allowed under low trust (floor = 0) if ladder permits.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE roles
                SET trust_score = 10, autonomy_level = 2
                WHERE project_id = :p AND role_key = :r
                """
            ),
            {"p": PID, "r": ROLE},
        )
    allowed = client.post(
        f"/v1/roles/{ROLE}/autonomy/check",
        json={
            "project_id": PID,
            "actor": "vp_agents",
            "action": "execute",
            "risk_tier": 0,
            "sandboxed": True,
            "policy_approved": False,
        },
    )
    assert allowed.status_code == 200, allowed.text
    ok = allowed.json()
    assert ok["allowed"] is True
    assert ok["trust_gate_passed"] is True
    assert ok["reason"] == "allowed_by_autonomy_level"


def test_g3_schedule_prefers_value_under_budget_pressure(client):
    """G3 — under orange budget, cheap mid-value beats expensive high-priority."""
    candidates = [
        {
            "id": "expensive-high",
            "priority_score": 90,
            "estimated_cost_usd_micros": 9_000_000,
        },
        {
            "id": "cheap-mid",
            "priority_score": 40,
            "estimated_cost_usd_micros": 1_000_000,
        },
        {
            "id": "free-low",
            "priority_score": 10,
            "estimated_cost_usd_micros": 0,
        },
    ]

    green = client.post(
        "/v1/work-orchestration/schedule-preference",
        json={"candidates": candidates, "budget_state": "green"},
    )
    assert green.status_code == 200, green.text
    green_body = green.json()
    assert green_body["ranking_mode"] == "priority_then_value"
    assert green_body["value_preferred"] is False
    assert [item["id"] for item in green_body["items"]][0] == "expensive-high"

    orange = client.post(
        "/v1/work-orchestration/schedule-preference",
        json={"candidates": candidates, "budget_state": "orange"},
    )
    assert orange.status_code == 200, orange.text
    orange_body = orange.json()
    assert orange_body["ranking_mode"] == "value_density"
    assert orange_body["value_preferred"] is True
    order = [item["id"] for item in orange_body["items"]]
    assert order == ["free-low", "cheap-mid", "expensive-high"]

    # Service helper agrees with pure rank_by_value_density under pressure.
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    ranked = service.schedule_preference(candidates, budget_state="red")
    assert [item["id"] for item in ranked["items"]] == order
    assert rank_by_value_density(candidates)[0]["id"] == "free-low"
