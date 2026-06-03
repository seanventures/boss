import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.main import app
from apps.api.app.services.members import MemberDirectoryService
from apps.api.app.services.specpack import SpecPackService


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def engine(database_url: str):
    return create_engine(database_url, future=True)


@pytest.fixture(scope="session", autouse=True)
def seeded_database(engine):
    get_settings.cache_clear()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres is not available for API tests.")
    seed()
    yield


@pytest.fixture(autouse=True)
def clean_tables(engine):
    settings = get_settings()
    members = MemberDirectoryService(settings)
    specpack = SpecPackService(settings)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM resource_receipts"))
        connection.execute(text("DELETE FROM scale_plans"))
        connection.execute(text("DELETE FROM worker_pools"))
        connection.execute(text("DELETE FROM deployment_runs"))
        connection.execute(text("DELETE FROM changeset_operations"))
        connection.execute(text("DELETE FROM changesets"))
        connection.execute(text("DELETE FROM cost_anomalies"))
        connection.execute(text("DELETE FROM cost_events"))
        connection.execute(text("DELETE FROM commitments"))
        connection.execute(text("DELETE FROM drift_reports"))
        connection.execute(text("DELETE FROM provider_health"))
        connection.execute(text("DELETE FROM reconciliation_runs"))
        connection.execute(text("DELETE FROM external_resources"))
        connection.execute(text("DELETE FROM deployment_targets"))
        connection.execute(text("DELETE FROM billing_accounts"))
        connection.execute(text("DELETE FROM capabilities"))
        connection.execute(text("DELETE FROM provider_accounts"))
        connection.execute(text("DELETE FROM secret_refs"))
        connection.execute(text("DELETE FROM resource_policies"))
        connection.execute(text("DELETE FROM cost_receipts"))
        connection.execute(text("DELETE FROM budget_reservations"))
        connection.execute(text("DELETE FROM budget_allocations"))
        connection.execute(text("DELETE FROM health_snapshots"))
        connection.execute(text("DELETE FROM inbox_messages"))
        connection.execute(text("DELETE FROM outbox_messages"))
        connection.execute(text("DELETE FROM self_improvement_proposals"))
        connection.execute(text("DELETE FROM role_reviews"))
        connection.execute(text("DELETE FROM autonomy_changes"))
        connection.execute(text("DELETE FROM agent_module_installs"))
        connection.execute(text("DELETE FROM break_glass_grants"))
        connection.execute(text("DELETE FROM project_members"))
        connection.execute(text("DELETE FROM org_members"))
        connection.execute(text("DELETE FROM deployment_rollouts"))
        connection.execute(text("DELETE FROM infra_resources"))
        connection.execute(text("DELETE FROM receipts"))
        connection.execute(text("DELETE FROM deadletters"))
        connection.execute(text("DELETE FROM routing_logs"))
        connection.execute(text("DELETE FROM meetings"))
        connection.execute(text("DELETE FROM plans"))
        connection.execute(text("DELETE FROM policy_records"))
        connection.execute(text("DELETE FROM departments"))
        connection.execute(text("DELETE FROM artifacts"))
        connection.execute(text("DELETE FROM decisions"))
        connection.execute(text("DELETE FROM jobs"))
        connection.execute(text("DELETE FROM tasks"))
        specpack.install_pack(
            connection,
            settings.default_project_id,
            settings.core_pack_id,
            "test-reset",
        )
        specpack.install_agent_module(
            connection,
            settings.default_project_id,
            "metatron_operator_v1",
            "test-reset",
        )
        specpack.install_agent_module(
            connection,
            settings.default_project_id,
            "codex_software_engineer_v1",
            "test-reset",
        )
        members.seed_default_members(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        connection.execute(
            text(
                """
                UPDATE roles
                SET trust_score = 50,
                    successful_runs = 0,
                    policy_violation_count = 0,
                    duplicate_effect_attempt_count = 0,
                    hidden_failure_count = 0,
                    low_quality_output_count = 0,
                    scope_violation_count = 0,
                    last_reviewed_at = NULL,
                    last_violation_at = NULL
                WHERE project_id = :project_id
                """
            ),
            {"project_id": settings.default_project_id},
        )
        connection.execute(
            text(
                """
                UPDATE budget_policies
                SET daily_cap_usd_micros = :daily_cap,
                    monthly_cap_usd_micros = :monthly_cap,
                    burst_cap_usd_micros_per_min = :burst_cap,
                    max_concurrent_jobs = 3,
                    max_workers_total = 2,
                    allowed_model_tiers = '["gpt-5.2-codex"]'::jsonb,
                    soft_cap_pct = 0.70,
                    auto_lite_mode_pct = 0.85,
                    freeze_provisioning_pct = 0.90,
                    freeze_tier2_pct = 0.95
                WHERE scope_type = 'project' AND scope_id = :project_id
                """
            ),
            {
                "project_id": settings.default_project_id,
                "daily_cap": settings.daily_cap_usd_micros,
                "monthly_cap": settings.monthly_cap_usd_micros,
                "burst_cap": settings.burst_cap_usd_micros_per_min,
            },
        )
        connection.execute(text("DELETE FROM events"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
