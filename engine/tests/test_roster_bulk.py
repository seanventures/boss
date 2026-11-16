"""One action sets the model connection and default model on many agents.

A row that cannot take the change is listed. Rows that already took it stay
changed. The action writes one receipt, and the same request does not apply
twice. Who an agent is, and what they may do, is not part of the change.
"""
from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.roster_bulk import RosterBulkError, apply

PROJECT = "roster-bulk-p"
ORG = "roster-bulk-org"
WORKSPACE = "roster-bulk-ws"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or "test" not in name:
        pytest.skip("DATABASE_URL must name a disposable test database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Roster')"), {"o": ORG})
            conn.execute(
                text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'W')"),
                {"w": WORKSPACE, "o": ORG},
            )
            conn.execute(
                text(
                    "INSERT INTO projects (id, workspace_id, name, slug) "
                    "VALUES (:p, :w, 'Roster', 'roster')"
                ),
                {"p": PROJECT, "w": WORKSPACE},
            )
            _role(conn, "ada", "Ada", reports_to="bea", autonomy=3, model="claude-sonnet-5",
                  connection_id="setup-anthropic")
            _role(conn, "bea", "Bea", reports_to=None, autonomy=1)
            _role(conn, "cy", "Cy", reports_to="ada", autonomy=2, lifecycle="retired")
            _source(conn, "setup-anthropic", "anthropic", ["claude-sonnet-5", "claude-haiku-5"])
            _source(conn, "setup-openai", "openai", ["gpt-5.4"])
            yield conn
        finally:
            transaction.rollback()


def _role(connection, key, name, *, reports_to, autonomy, lifecycle="active",
          model=None, connection_id=None):
    spec = {"mission": f"{name} reviews the work.", "persona": {"callsign": name[:3]}}
    connection.execute(
        text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec, "
            "autonomy_level, reports_to, agent_id, lifecycle, fingerprint, "
            "identity_version, born_by) "
            "VALUES (:id, :p, :k, :name, CAST(:spec AS JSONB), :a, :r, :agt, :life, "
            ":fp, 1, 'ceo')"
        ),
        {
            "id": f"role_{key}", "p": PROJECT, "k": key, "name": name,
            "spec": json.dumps(spec), "a": autonomy, "r": reports_to,
            "agt": f"agt_{key}", "life": lifecycle, "fp": f"fp-{key}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO role_skills (id, project_id, role_key, skill_id, source, assigned_by) "
            "VALUES (:id, :p, :k, 'review.diff', 'authored', 'ceo')"
        ),
        {"id": f"skill_{key}", "p": PROJECT, "k": key},
    )
    if model or connection_id:
        connection.execute(
            text(
                "INSERT INTO roster_model_assignments "
                "(project_id, role_key, connection_id, default_model, updated_by) "
                "VALUES (:p, :k, :c, :m, 'ceo')"
            ),
            {"p": PROJECT, "k": key, "c": connection_id, "m": model},
        )


def _source(connection, source_id, provider, models):
    connection.execute(
        text(
            "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
            "entitlement_kind, owner_type, owner_id, connection_status, status, "
            "supported_models, runtime_supported) "
            "VALUES (:id, :p, :provider, :s, 'api', 'api_key', 'project', :p, 'connected', "
            "'active', CAST(:models AS JSONB), true)"
        ),
        {
            "id": f"src_{source_id}", "p": PROJECT, "provider": provider, "s": source_id,
            "models": json.dumps(models),
        },
    )


def _who(connection):
    roles = [dict(row) for row in connection.execute(
        text(
            "SELECT role_key, display_name, autonomy_level, reports_to, spec, agent_id, "
            "lifecycle, fingerprint, identity_version, born_by "
            "FROM roles WHERE project_id = :p ORDER BY role_key"
        ),
        {"p": PROJECT},
    ).mappings()]
    skills = [dict(row) for row in connection.execute(
        text(
            "SELECT role_key, skill_id, source FROM role_skills "
            "WHERE project_id = :p ORDER BY role_key, skill_id"
        ),
        {"p": PROJECT},
    ).mappings()]
    return roles, skills


def _pin(connection, key):
    row = connection.execute(
        text(
            "SELECT connection_id, default_model FROM roster_model_assignments "
            "WHERE project_id = :p AND role_key = :k"
        ),
        {"p": PROJECT, "k": key},
    ).mappings().first()
    return dict(row) if row else None


def _receipts(connection, dedupe_key):
    return connection.execute(
        text("SELECT id, status, result FROM receipts WHERE dedupe_key = :k"),
        {"k": dedupe_key},
    ).mappings().all()


def test_a_failed_row_is_listed_and_the_others_keep_the_change(connection):
    before = _who(connection)
    result = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-partial",
        connection_id="setup-openai",
        agents=["ada", "ghost", "bea"],
    )
    assert result["replayed"] is False
    assert result["applied"] == [
        {"role_key": "bea", "connection_id": "setup-openai", "default_model": None},
    ]
    assert result["failed"] == [
        {"role_key": "ada", "error": "claude-sonnet-5 is not on setup-openai."},
        {"role_key": "ghost", "error": "No agent ghost."},
    ]
    assert _pin(connection, "ada") == {
        "connection_id": "setup-anthropic", "default_model": "claude-sonnet-5",
    }
    assert _pin(connection, "bea") == {"connection_id": "setup-openai", "default_model": None}
    assert _pin(connection, "ghost") is None
    assert _who(connection) == before


def test_the_bulk_change_writes_one_receipt(connection):
    result = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-one",
        connection_id="setup-anthropic", default_model="claude-haiku-5",
        agents=["ada", "ghost", "bea"],
    )
    rows = _receipts(connection, "bulk-one")
    assert len(rows) == 1
    assert rows[0]["id"] == result["receipt_id"]
    assert rows[0]["status"] == "succeeded"
    body = rows[0]["result"]
    if isinstance(body, str):
        body = json.loads(body)
    assert body["failed"] == result["failed"]
    assert [item["role_key"] for item in body["applied"]] == ["ada", "bea"]


def test_the_same_request_does_not_apply_twice(connection):
    first = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-once",
        connection_id="setup-anthropic", default_model="claude-haiku-5",
        agents=["bea"],
    )
    connection.execute(
        text(
            "UPDATE roster_model_assignments SET default_model = 'claude-sonnet-5' "
            "WHERE project_id = :p AND role_key = 'bea'"
        ),
        {"p": PROJECT},
    )
    second = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-once",
        connection_id="setup-anthropic", default_model="claude-haiku-5",
        agents=["bea"],
    )
    assert second["replayed"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert len(_receipts(connection, "bulk-once")) == 1
    assert _pin(connection, "bea")["default_model"] == "claude-sonnet-5"


def test_who_the_agent_is_and_what_they_may_do_stay_put(connection):
    before = _who(connection)
    apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-identity",
        connection_id="setup-anthropic", default_model="claude-sonnet-5",
        agents=["ada", "bea"],
    )
    assert _who(connection) == before
    assert _pin(connection, "ada") == {
        "connection_id": "setup-anthropic", "default_model": "claude-sonnet-5",
    }
    assert _pin(connection, "bea") == {
        "connection_id": "setup-anthropic", "default_model": "claude-sonnet-5",
    }


def test_a_stale_row_is_listed_and_not_written(connection):
    result = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-stale",
        default_model="claude-haiku-5",
        agents=[
            {"role_key": "ada", "expected": {
                "connection_id": "setup-anthropic", "default_model": "claude-sonnet-5",
            }},
            {"role_key": "bea", "expected": {
                "connection_id": "setup-openai", "default_model": "gpt-5.4",
            }},
        ],
    )
    assert [item["role_key"] for item in result["applied"]] == ["ada"]
    assert result["failed"] == [{
        "role_key": "bea",
        "error": "This agent's connection or model changed.",
    }]
    assert _pin(connection, "ada")["default_model"] == "claude-haiku-5"
    assert _pin(connection, "bea") is None


def test_a_retired_agent_is_listed_and_not_changed(connection):
    result = apply(
        connection, PROJECT, actor="ceo", dedupe_key="bulk-retired",
        default_model="claude-haiku-5", connection_id="setup-anthropic",
        agents=["cy", "bea"],
    )
    assert [item["role_key"] for item in result["applied"]] == ["bea"]
    assert result["failed"] == [{
        "role_key": "cy",
        "error": "Retired agents stay off this change.",
    }]
    assert _pin(connection, "cy") is None
    life = connection.execute(
        text("SELECT lifecycle FROM roles WHERE project_id = :p AND role_key = 'cy'"),
        {"p": PROJECT},
    ).scalar()
    assert life == "retired"


def test_the_route_lists_the_roster_and_posts_one_change(connection, monkeypatch):
    from contextlib import contextmanager

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routers import gap_roster_bulk

    @contextmanager
    def _begin(engine=None):
        yield connection

    monkeypatch.setattr(gap_roster_bulk, "begin", _begin)
    client = TestClient(FastAPI())
    # The router is included by the test, the way the reconciler includes it.
    client.app.include_router(gap_roster_bulk.router)
    listed = client.get("/v1/agents/roster-bulk", params={"project_id": PROJECT})
    assert listed.status_code == 200
    body = listed.json()
    ada = next(agent for agent in body["agents"] if agent["role_key"] == "ada")
    assert ada["name"] == "Ada"
    assert ada["connection_id"] == "setup-anthropic"
    assert ada["default_model"] == "claude-sonnet-5"
    assert ada["lifecycle"] == "active"
    assert {item["id"] for item in body["connections"]} == {"setup-anthropic", "setup-openai"}
    posted = client.post("/v1/agents/roster-bulk", json={
        "project_id": PROJECT,
        "actor": "ceo",
        "dedupe_key": "bulk-http",
        "connection_id": "setup-openai",
        "agents": ["ghost", "bea"],
    })
    assert posted.status_code == 200
    change = posted.json()
    assert change["applied"] == [
        {"role_key": "bea", "connection_id": "setup-openai", "default_model": None},
    ]
    assert change["failed"] == [{"role_key": "ghost", "error": "No agent ghost."}]
    assert len(_receipts(connection, "bulk-http")) == 1
    refused = client.post("/v1/agents/roster-bulk", json={
        "project_id": PROJECT,
        "actor": "ceo",
        "dedupe_key": "bulk-http-name",
        "default_model": "claude-haiku-5",
        "display_name": "Not Ada",
        "agents": ["ada"],
    })
    assert refused.status_code == 422


def test_a_request_that_names_nothing_to_change_writes_no_receipt(connection):
    with pytest.raises(RosterBulkError) as caught:
        apply(connection, PROJECT, actor="ceo", dedupe_key="bulk-empty", agents=["ada"])
    assert caught.value.status == 422
    assert _receipts(connection, "bulk-empty") == []
    with pytest.raises(RosterBulkError) as missing:
        apply(
            connection, PROJECT, actor="ceo", dedupe_key="bulk-missing-conn",
            connection_id="setup-missing", default_model="claude-haiku-5", agents=["ada"],
        )
    assert missing.value.status == 422
    assert _receipts(connection, "bulk-missing-conn") == []
