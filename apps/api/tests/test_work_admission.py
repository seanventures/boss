from apps.api.app.config import get_settings
from apps.api.app.services.work_orchestration import WorkOrchestrationService


def test_work_admission_is_explainable_and_does_not_reserve_budget(engine):
    settings = get_settings()
    service = WorkOrchestrationService(settings)
    with engine.begin() as connection:
        work = service.upsert_work_item(
            connection,
            project_id=settings.default_project_id,
            subject_type="feature",
            subject_id="admission-proof",
            title="Admission proof",
            strategic_value=95,
            customer_impact=95,
            urgency=90,
            cost_of_delay=90,
            risk_reduction=90,
            dependency_pressure=80,
            confidence=100,
            actor="product_manager",
        )
        allowed = service.evaluate_admission(
            connection,
            project_id=settings.default_project_id,
            work_item_id=work["id"],
            role_key="metatron",
            requested_autonomy_level=1,
            estimated_cost_usd_micros=0,
            actor="chief_operator",
        )
        denied = service.evaluate_admission(
            connection,
            project_id=settings.default_project_id,
            work_item_id=work["id"],
            role_key="metatron",
            requested_autonomy_level=2,
            estimated_cost_usd_micros=10**18,
            actor="chief_operator",
        )
        status = service.budget.status_for_project(connection, settings.default_project_id)

    assert allowed["decision"] == "admit"
    assert allowed["reasons"] == []
    assert allowed["priority_band"] in {"p0", "p1"}
    assert allowed["role_autonomy_ceiling"] == 1
    assert denied["decision"] == "defer"
    assert "requested_autonomy_level_exceeds_role_ceiling" in denied["reasons"]
    assert "budget_available_below_estimate" in denied["reasons"]
    assert status["reserved_usd_micros"] == 0
