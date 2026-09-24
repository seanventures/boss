import os

os.environ.setdefault("BOSS_EDITION", "community")

# CONFINE THIS RUN BEFORE ANYTHING READS DATABASE_URL.
#
# The application builds its own engine when `apps.api.app.main` is imported, a few lines
# below. Anything that rewrites DATABASE_URL in a fixture is already too late: the app would
# be working in `public` while the tests worked in their own schema, and half the suite fails
# on data the other half cannot see.
#
# So the schema is created here, at import time, and DATABASE_URL is rewritten before the app
# is imported. From this point on, every engine anyone builds — the suite's, the app's, a
# service's — lands in a schema that exists only for this run and is dropped when it ends.
#
# This is what makes the reset in `clean_tables` safe: it can only ever name tables in here.
from apps.api.tests.db_guard import assert_safe_to_wipe as _assert_safe_to_wipe  # noqa: E402
from apps.api.tests.schema_confinement import confine_to_private_schema  # noqa: E402

_BASE_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _BASE_DATABASE_URL:
    _assert_safe_to_wipe(_BASE_DATABASE_URL)
    _TEST_SCHEMA = confine_to_private_schema(_BASE_DATABASE_URL)
else:
    _TEST_SCHEMA = None

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.main import app
from apps.api.app import main as api_main
from apps.api.app.services.entitlements import EntitlementBrokerService
from apps.api.app.services.agent_studio import AgentStudioService
from apps.api.app.services.event_dispatch import invalidate_subscription_cache
from apps.api.app.services.members import MemberDirectoryService
from apps.api.app.services.skill_registry import seed_system_skills
from apps.api.app.services.specpack import SpecPackService
from apps.api.app.services.toolchain_registry import ToolchainRegistryService

# Keep the test app on the Community route surface during import and seeding.
os.environ["BOSS_EDITION"] = "community"
get_settings.cache_clear()
api_main.settings.boss_edition = "community"


@pytest.fixture(scope="session")
def database_url() -> str:
    # Already guarded and confined at import time; returned here for the fixtures that want it.
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def engine(database_url: str):
    """An engine on the schema this run owns. Confinement already happened at import time."""
    eng = create_engine(database_url, future=True)
    if _TEST_SCHEMA:
        from boss_engine.db import ensure_schema

        ensure_schema(eng)
    return eng


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
        # Delete only tables inside this run's confined schema, in child-to-parent order.
        # Missing tables in partial Community schemas are skipped.
        for tbl in [
            "metatron_watch_ticks",
            "metatron_memories",
            "metatron_goals",
            "focused_agent_ticks",
            "focused_agents",
            "agent_process_steps",
            "agent_process_runs",
            "agent_process_templates",
            "agent_group_member_runs",
            "agent_key_leases",
            "agent_key_grants",
            "agent_key_entries",
            "oncall_pages",
            "oncall_policies",
            "oncall_schedules",
            "sre_runbook_runs",
            "sre_runbooks",
            "release_board_items",
            "release_plans",
            "wp_dependencies",
            "wp_roadmap_items",
            "wp_modules",
            "wp_cycles",
            "wp_initiatives",
            "kg_eval_snapshots",
            "worker_autoscaling_history",
            "worker_autoscaling_policies",
            "token_budget_policies",
            "preview_sandboxes",
            "agent_group_runs",
            "agent_group_versions",
            "agent_group_specs",
            "capability_resolutions",
            "integration_connections",
            "integration_definitions",
            "verification_runs",
            "verification_policies",
            "ownership_transitions",
            "surface_ownerships",
            "claim_verifications",
            "assurance_evidence",
            "assurance_claims",
            "assurance_policy_snapshots",
            "assurance_cases",
            "closed_question_matches",
            "settled_conclusions",
            "regression_signals",
            "ground_truth_observations",
            "ground_truth_metric_definitions",
            "delivery_presence_events",
            "delivery_status_channels",
            "delivery_bot_identities",
            # AI SRE reliability pipeline + knowledge graph + learning loop (child → parent)
            "sre_postmortems",
            "incident_evidence",
            "reliability_policies",
            "llm_calls",
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
            "a2a_acknowledgements",
            "a2a_transport_attempts",
            "a2a_deliveries",
            "a2a_outbox",
            "a2a_envelopes",
            "a2a_subscriptions",
            "a2a_channels",
            "panel_dissent",
            "panel_findings",
            "agent_studio_runs",
            "panel_runs",
            "panel_version_members",
            "panel_versions",
            "panel_definitions",
            "agent_test_runs",
            "agent_versions",
            "agent_definitions",
            "agent_messages",
            "agent_threads",
            "cloud_worker_enrollments",
            "license_entitlements",
            "terraform_exports",
            "deployment_promotions",
            "deployment_rollouts",
            "resource_receipts",
            "provider_slo_evaluations",
            "provider_slo_definitions",
            "provider_usage_events",
            "worker_instances",
            "goldeneye_deploys",
            "goldeneye_apps",
            "goldeneye_package_installs",
            "goldeneye_nodes",
            "scale_plans",
            "worker_pools",
            # Health samples hold an FK onto deployment_targets and were missing from this
            # list, so a single test that observed a target left rows that made the
            # `deployment_targets` delete below fail — poisoning every subsequent test in the
            # session with a ForeignKeyViolation.
            "deployment_health_samples",
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
            "resource_topology_edges",
            "external_resources",
            "deployment_targets",
            "billing_accounts",
            "capabilities",
            "provider_accounts",
            "github_repository_ref_states",
            "github_repository_observations",
            "github_reconciliation_runs",
            "github_reconciliation_signals",
            "github_merge_authorizations",
            "github_resolution_plans",
            "github_conflict_sets",
            "github_code_sync_runs",
            "github_branch_leases",
            "github_branch_snapshots",
            "github_branch_policies",
            "github_repository_bindings",
            "github_connections",
            "model_gateway_invocations",
            "model_gateway_leases",
            # Hosts reference their key via secret_refs, so they must go first. Added with the
            # deployment-fleet work and never listed here, which is the same omission as
            # deployment_health_samples above.
            "deployment_hosts",
            "secret_material",
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
            "toolchain_candidates",
            "toolchain_import_runs",
            "toolchain_sources",
            "quality_gates",
            "feedback_messages",
            "feedback_threads",
            "app_previews",
            "improvement_trials",
            "improvement_programs",
            "qa_benchmark_evaluations",
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
            "deadletter_remediations",
            "deadletters",
            "routing_logs",
            "meetings",
            "plans",
            "policy_records",
            "departments",
            "artifacts",
            "decisions",
            "jobs",
            "work_tracker_bindings",
            "work_tracker_providers",
            "resource_allocations",
            "resource_quotas",
            "work_items",
            "tasks",
        ]:
            if connection.execute(
                text("SELECT to_regclass(:table_name)"), {"table_name": tbl}
            ).scalar_one() is not None:
                connection.execute(text(f"DELETE FROM {tbl}"))
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
        ToolchainRegistryService(settings).seed_sources(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        members.seed_default_members(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        AgentStudioService(settings).seed_catalog(
            connection,
            settings.default_project_id,
            "test-reset",
        )
        from apps.api.app.services.work_orchestration import WorkOrchestrationService

        WorkOrchestrationService(settings).seed_defaults(
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
                # Keep the general matrix independent from the production burst
                # limiter. Tests that exercise burst denial set their own cap.
                "burst_cap": 100_000_000,
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
