"""Fixtures for BOSS Account Connect service tests.

Requires Postgres. Prefer ``AC_DATABASE_URL``, else ``DATABASE_URL``.
Tables use the ``ac_`` prefix and are created with IF NOT EXISTS, so AC may
share the main boss database safely.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def _resolve_database_url() -> str | None:
    for key in ("AC_DATABASE_URL", "DATABASE_URL"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _resolve_database_url()
    if not url:
        pytest.skip(
            "Postgres not configured: set DATABASE_URL or AC_DATABASE_URL "
            "(e.g. postgresql+psycopg://boss:boss@127.0.0.1:5432/boss)."
        )
    return url


@pytest.fixture(scope="session")
def ac_service_token() -> str:
    return os.environ.get("AC_SERVICE_TOKEN") or f"test-ac-token-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="session")
def ac_default_org_id() -> str:
    return os.environ.get("AC_DEFAULT_ORG_ID") or "org_boss_cloud"


@pytest.fixture(scope="session")
def ac_client(database_url: str, ac_service_token: str, ac_default_org_id: str):
    """Boot Account Connect app with real Postgres; skip if DB unreachable."""
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        pytest.skip(f"Postgres is not available for Account Connect tests: {error}")
    finally:
        engine.dispose()

    # Configure before importing the app module so startup uses test settings.
    os.environ["AC_DATABASE_URL"] = database_url
    os.environ["AC_SERVICE_TOKEN"] = ac_service_token
    os.environ["AC_PUBLIC_BASE_URL"] = "http://testserver"
    os.environ["AC_DEFAULT_ORG_ID"] = ac_default_org_id
    os.environ.setdefault("AC_DEFAULT_ORG_NAME", "BOSS Cloud")

    from apps.account_connect.app.config import get_settings
    from apps.account_connect.app.main import app

    get_settings.cache_clear()

    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


@pytest.fixture
def auth_headers(ac_service_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ac_service_token}"}
