import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.main import app
from apps.api.app import main as api_main
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.event_dispatch import invalidate_subscription_cache
from apps.api.app.services.members import MemberDirectoryService
from apps.api.app.services.skill_registry import seed_system_skills
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
    entitlements = EntitlementBrokerService(settings)
    members = MemberDirectoryService(settings)
    specpack = SpecPackService(settings)
    with engine.begin() as connection:
        # Tolerant deletes for partial schemas in ad-hoc test DBs (only core tables from db/schema.sql exist).
        # Original explicit list preserved (in designed child-to-parent delete order to avoid FK violations);
        # missing tables (commercial/enterprise) are skipped. Enables budget/kpi/concurrent tests + full -k matrix.
        for tbl in [
            # AI SRE reliability pipeline + knowledge graph + learning loop (child → parent)
            "incident_evidence",
            "remediations",
            "anomaly_signals",
            "incidents",
            "telemetry_sources",
            "learning_examples",
            "kg_edges",
            "kg_nodes",
            "workflow_compensation_runs",
            "workflow_migrations",
            "workflow_step_runs",
            "workflow_runs",
            "workflow_mappings",
            "workflow_versions",
            "workflow_definitions",
            "agent_messages",
            "agent_threads",
            "account_connect_billing_portal_sessions",
            "account_connect_auth_sessions",
            "account_connect_health_checks",
            "account_connect_team_invites",
            "hosted_marketplace_pack_listings",
            "hosted_marketplace_sync_runs",
            "license_entitlements",
            "account_connect_orgs",
            "enterprise_scim_users",
            "enterprise_scim_groups",
            "enterprise_identity_providers",
            "enterprise_cluster_validation_runs",
            "enterprise_clusters",
            "terraform_exports",
            "deployment_promotions",
            "deployment_rollouts",
            "resource_receipts",
            "scale_plans",
            "worker_pools",
            "deployment_runs",
            "changeset_operations",
            "changesets",
            "deployment_environment_policies",
            "cost_anomalies",
            "cost_events",
            "commitments",
            "drift_reports",
            "provider_health",
            "reconciliation_runs",
            "external_resources",
            "deployment_targets",
            "billing_accounts",
            "capabilities",
            "provider_accounts",
            "secret_refs",
            "resource_policies",
            "capacity_receipts",
            "entitlement_sources",
            "skill_invocations",
            "cost_receipts",
            "budget_reservations",
            "budget_allocations",
            "health_snapshots",
            "chaos_runs",
            "commitment_reconciliations",
            "canary_runs",
            "eval_runs",
            "project_skills",
            "quality_gates",
            "feedback_messages",
            "feedback_threads",
            "app_previews",
            "code_task_runs",
            "meeting_runs",
            "ack_state",
            "inbox_messages",
            "outbox_messages",
            "self_improvement_proposals",
            "role_reviews",
            "autonomy_changes",
            "agent_module_install_records",
            "agent_module_installs",
            "break_glass_grants",
            "project_members",
            "org_members",
            "infra_resources",
            "feature_catalog",
            "event_subscriptions",
            "inbound_webhooks",
            "webhook_deliveries",
            "webhook_endpoints",
            "receipts",
            "deadletters",
            "routing_logs",
            "meetings",
            "plans",
            "policy_records",
            "departments",
            "artifacts",
            "decisions",
            "jobs",
            "tasks",
        ]:
            try:
                connection.execute(text(f"DELETE FROM {tbl}"))
            except Exception:
                pass  # table absent in this schema (e.g. no commercial tables)
        specpack.install_pack(
            connection,
            settings.default_project_id,
            settings.core_pack_id,
            "test-reset",
        )
        specpack.install_default_agent_modules(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        entitlements.seed_default_sources(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        seed_system_skills(
            connection,
            settings.default_project_id,
            entitlements.list_skills(),
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
        try:
            connection.execute(text("DELETE FROM events"))
        except Exception:
            pass
    invalidate_subscription_cache()
    # Ensure rate limiter (P2 high-cost) uses generous limit + cleared for normal tests.
    # Specific rate limit test will override to low value temporarily.
    api_main.configure_rate_limiter_for_testing(1000)
    api_main.reset_rate_limiter_hits()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
