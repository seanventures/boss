"""BOSS-353 integration: admission + work-item read models expose value density."""

from __future__ import annotations

import pytest

from apps.api.app.config import get_settings
from apps.api.app.services.work_orchestration import WorkOrchestrationService


def test_admission_includes_value_density_fields(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    with engine.begin() as connection:
        work = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="value-density-admission",
            title="Value density admission",
            strategic_value=90,
            customer_impact=90,
            urgency=80,
            cost_of_delay=80,
            risk_reduction=70,
            dependency_pressure=60,
            confidence=90,
            actor="product_manager",
        )
        assert "priority_score" in work
        assert work["priority_score"] == work["effective_score"]

        result = service.evaluate_admission(
            connection,
            project_id=settings.default_project_id,
            work_item_id=work["id"],
            role_key="metatron",
            requested_autonomy_level=1,
            estimated_cost_usd_micros=4_000_000,
            actor="chief_operator",
        )

    assert result["decision"] == "admit"
    assert result["priority_score"] == result["effective_score"]
    assert result["value_density"] == pytest.approx(
        result["priority_score"] * 1_000_000 / 4_000_000
    )
    assert result["value_per_cost"] == result["value_density"]
    assert result["value_per_cost_micros"] == round(result["value_density"])
    assert result["cost_floor_applied"] is False
    assert result["estimated_cost_usd_micros"] == 4_000_000


def test_service_score_preview_and_rank_helpers(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    preview = service.score_preview(
        strategic_value=100,
        customer_impact=100,
        urgency=100,
        cost_of_delay=100,
        risk_reduction=100,
        dependency_pressure=100,
        confidence=100,
        estimated_cost_usd_micros=1_000_000,
    )
    assert preview["priority_score"] == 100.0
    assert preview["value_density"] == pytest.approx(100.0)

    ranked = service.rank_by_value_density(
        [
            {"id": "a", "priority_score": 50, "estimated_cost_usd_micros": 5_000_000},
            {"id": "b", "priority_score": 50, "estimated_cost_usd_micros": 1_000_000},
        ]
    )
    assert ranked[0]["id"] == "b"

    with engine.begin() as connection:
        high = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="vd-high",
            title="High",
            strategic_value=95,
            customer_impact=95,
            urgency=90,
            cost_of_delay=90,
            risk_reduction=80,
            dependency_pressure=80,
            confidence=100,
            actor="product_manager",
        )
        low = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="vd-low",
            title="Low",
            strategic_value=30,
            customer_impact=20,
            urgency=10,
            cost_of_delay=10,
            risk_reduction=10,
            dependency_pressure=10,
            confidence=50,
            actor="product_manager",
        )
        listed = service.list_work_items(
            connection,
            settings.default_project_id,
            estimated_cost_usd_micros=2_000_000,
            order_by_value_density=True,
        )

    by_id = {item["id"]: item for item in listed}
    assert high["id"] in by_id
    assert low["id"] in by_id
    assert by_id[high["id"]]["value_density"] > by_id[low["id"]]["value_density"]
    # Default score order still places high first when not reordering by density alone
    default_order = [item["id"] for item in listed]
    assert default_order.index(high["id"]) < default_order.index(low["id"])
