"""Console and published-app traffic is open until an IP list is set (BOSSAPP-1271).

An empty list allows every address, so a fresh install is not locked out.
A configured list allows only those addresses. Any other address is refused
with 403 and one audit row per surface.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema

_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55808/boss_test_1271",
)


@pytest.fixture(scope="module")
def eng():
    os.environ["DATABASE_URL"] = _URL
    get_settings.cache_clear()
    engine = create_engine(_URL, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - no database in this process
        pytest.skip(f"test database unavailable: {error}")
    ensure_schema(engine, get_settings())
    return engine


@pytest.fixture()
def clear(eng):
    with eng.begin() as connection:
        connection.execute(text("DELETE FROM ip_allow_audit"))
        connection.execute(text("DELETE FROM ip_allowlist"))
    return eng


def _client(host: str) -> TestClient:
    from apps.api.app.routers.gap_1271 import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, client=(host, 443))


def _audit(eng) -> list[dict]:
    with eng.connect() as connection:
        rows = connection.execute(text(
            "SELECT surface, client_ip, outcome FROM ip_allow_audit "
            "ORDER BY surface, client_ip"
        )).mappings().all()
    return [dict(row) for row in rows]


def _put(host: str, entries: list[str]) -> None:
    response = _client(host).put(
        "/v1/ip-allowlist",
        json={"entries": entries, "actor": "ada"},
    )
    assert response.status_code == 200, response.text


def _gate(host: str, surface: str):
    return _client(host).get(f"/v1/ip-allowlist/gate/{surface}")


def test_an_empty_list_allows_console_and_published_app_traffic(clear):
    # Breaks if an empty list is treated as deny-all and locks the console out.
    _put("203.0.113.9", [])
    console = _gate("203.0.113.9", "console")
    published = _gate("203.0.113.9", "published-app")
    assert console.status_code == 200
    assert published.status_code == 200
    assert console.json()["allowed"] is True
    assert published.json()["allowed"] is True
    assert console.json()["restricted"] is False
    assert published.json()["surface"] == "published_app"
    assert _audit(clear) == []


def test_a_listed_address_is_allowed(clear):
    # Breaks if a configured list refuses an address that is on it.
    _put("203.0.113.44", ["203.0.113.44"])
    console = _gate("203.0.113.44", "console")
    published = _gate("203.0.113.44", "published-app")
    assert console.status_code == 200
    assert published.status_code == 200
    assert console.json()["allowed"] is True
    assert console.json()["client_ip"] == "203.0.113.44"
    assert published.json()["allowed"] is True
    assert published.json()["restricted"] is True
    assert _audit(clear) == []


def test_an_unlisted_address_is_refused_and_audited(clear):
    # Breaks if an address off the list is allowed, or the refusal writes no audit row.
    _put("203.0.113.44", ["203.0.113.44"])
    console = _gate("198.51.100.23", "console")
    published = _gate("198.51.100.23", "published-app")
    assert console.status_code == 403
    assert published.status_code == 403
    assert console.json()["allowed"] is False
    assert console.json()["client_ip"] == "198.51.100.23"
    assert published.json()["surface"] == "published_app"
    assert _audit(clear) == [
        {"surface": "console", "client_ip": "198.51.100.23", "outcome": "refused"},
        {"surface": "published_app", "client_ip": "198.51.100.23", "outcome": "refused"},
    ]
