import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.main import app


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
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM cost_receipts"))
        connection.execute(text("DELETE FROM budget_reservations"))
        connection.execute(text("DELETE FROM receipts"))
        connection.execute(text("DELETE FROM deadletters"))
        connection.execute(text("DELETE FROM routing_logs"))
        connection.execute(text("DELETE FROM artifacts"))
        connection.execute(text("DELETE FROM decisions"))
        connection.execute(text("DELETE FROM jobs"))
        connection.execute(text("DELETE FROM tasks"))
        connection.execute(text("DELETE FROM events"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
