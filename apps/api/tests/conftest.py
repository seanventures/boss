import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.main import app
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
    specpack = SpecPackService(settings)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM cost_receipts"))
        connection.execute(text("DELETE FROM budget_reservations"))
        connection.execute(text("DELETE FROM health_snapshots"))
        connection.execute(text("DELETE FROM inbox_messages"))
        connection.execute(text("DELETE FROM outbox_messages"))
        connection.execute(text("DELETE FROM self_improvement_proposals"))
        connection.execute(text("DELETE FROM role_reviews"))
        connection.execute(text("DELETE FROM autonomy_changes"))
        connection.execute(text("DELETE FROM agent_module_installs"))
        connection.execute(text("DELETE FROM receipts"))
        connection.execute(text("DELETE FROM deadletters"))
        connection.execute(text("DELETE FROM routing_logs"))
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
        connection.execute(text("DELETE FROM events"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
