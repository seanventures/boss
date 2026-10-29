"""P0/P1: the hourly control-plane sweep (subscription_keepalive.sweep) catches a dying
subscription login within the hour instead of a person noticing "Working" was stale
([L:kgn_2b552c5e148b444f825c] context: a dead Codex login showed green until a build 401'd).

Real, disposable postgres (DATABASE_URL from this run's throwaway database — BUILD SERVICES:
postgres) since mark_login/create_blocking_decision write real jsonb and a real decisions row;
no sqlite stub reaches that SQL. The CLI ping itself is faked — no real network, no real CLI.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import subscription_keepalive, subscription_sign_in


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "testing", "ci", "scratch", "tmp", "boss_run")):
        pytest.skip("DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


class _Same:
    """An engine whose transactions are savepoints of the test's one rolled-back transaction."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        with self.connection.begin_nested():
            yield self.connection


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        org, ws, proj = f"org_{uuid.uuid4().hex[:8]}", f"ws_{uuid.uuid4().hex[:8]}", f"proj_{uuid.uuid4().hex[:8]}"
        conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'KA')"), {"o": org})
        conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'KA')"), {"w": ws, "o": org})
        conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'KA', :p)"),
                    {"p": proj, "w": ws})
        conn.project_id = proj  # type: ignore[attr-defined]
        try:
            yield conn
        finally:
            transaction.rollback()


def _codex_source(conn, *, connection_status: str = "connected") -> str:
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    conn.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind,"
        " entitlement_kind, owner_type, owner_id, auth_method, connection_status)"
        " VALUES (:id, :p, 'openai_codex_chatgpt', :sid, 'subscription', 'quota', 'project',"
        " :p, 'oauth_subscription', :cs)"),
        {"id": f"ent_{uuid.uuid4().hex[:8]}", "p": conn.project_id, "sid": source_id, "cs": connection_status})
    return source_id


def _credential_state(conn, source_id: str) -> str | None:
    row = conn.execute(text(
        "SELECT observed_capacity->>'credential_state' FROM entitlement_sources"
        " WHERE project_id = :p AND source_id = :s"),
        {"p": conn.project_id, "s": source_id}).fetchone()
    return row[0] if row else None


def _decision_count(conn) -> int:
    return conn.execute(text(
        "SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending'"),
        {"p": conn.project_id}).scalar()


def test_a_rejected_login_flips_the_source_and_raises_exactly_one_card(connection, monkeypatch):
    """AC4 + AC6: a 401-shaped ping flips credential_state to rejected within one tick, and a
    second consecutive tick that still sees it rejected does not create a second card."""
    source_id = _codex_source(connection)
    monkeypatch.setattr(subscription_sign_in, "sign_in_sources",
                        lambda settings=None: {source_id: "codex"})
    monkeypatch.setattr(subscription_keepalive, "_ping_codex",
                        lambda binary, spec: (False, "Error: 401 Unauthorized, not logged in"))

    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)
    assert _credential_state(connection, source_id) == "rejected"
    assert _decision_count(connection) == 1

    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)
    assert _decision_count(connection) == 1, "a login still rejected must not raise a second card"


def test_a_source_that_is_not_connected_is_skipped(connection, monkeypatch):
    """AC5: a source that was never signed in gets no ping, no refresh, no card."""
    source_id = _codex_source(connection, connection_status="not_connected")
    calls: list[str] = []
    monkeypatch.setattr(subscription_sign_in, "sign_in_sources",
                        lambda settings=None: {source_id: "codex"})
    monkeypatch.setattr(subscription_keepalive, "_ping_codex",
                        lambda binary, spec: calls.append(source_id) or (True, "{}"))

    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)

    assert calls == []
    assert _credential_state(connection, source_id) is None
    assert _decision_count(connection) == 0


def test_a_working_login_clears_a_prior_rejection_and_allows_a_future_card(connection, monkeypatch):
    """AC6 (the other half): clearing on success means the suppression is not permanent — the
    next rejection is free to raise a new card."""
    source_id = _codex_source(connection)
    monkeypatch.setattr(subscription_sign_in, "sign_in_sources",
                        lambda settings=None: {source_id: "codex"})
    monkeypatch.setattr(subscription_keepalive, "_ping_codex",
                        lambda binary, spec: (False, "401 not logged in"))
    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)
    assert _credential_state(connection, source_id) == "rejected"
    assert _decision_count(connection) == 1

    monkeypatch.setattr(subscription_keepalive, "_ping_codex",
                        lambda binary, spec: (True, '{"models": []}'))
    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)
    assert _credential_state(connection, source_id) == "ok"

    monkeypatch.setattr(subscription_keepalive, "_ping_codex",
                        lambda binary, spec: (False, "401 not logged in"))
    subscription_keepalive.sweep(_Same(connection), settings=None, project_id=connection.project_id)
    assert _credential_state(connection, source_id) == "rejected"
    assert _decision_count(connection) == 2, "a fresh rejection after recovery gets its own card"
