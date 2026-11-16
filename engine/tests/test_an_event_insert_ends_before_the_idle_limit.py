"""An event insert ends before Postgres kills an idle transaction, and the event stays.

A worker held one transaction open, sat past idle_in_transaction_session_timeout, and
died on the next statement — the events INSERT — with psycopg
IdleInTransactionSessionTimeout. The event was never written. That insert rolls the
dead transaction back and commits the same event on a short transaction, so another
connection can read it. A live transaction still owns its event until the caller
commits: the project row the event points at may not be committed yet.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

URL = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55678/boss_test_slice_runs",
)
SCHEMA = "boss_test_event_idle"
IDLE = "1s"


def _engine():
    return create_engine(
        URL, future=True, poolclass=NullPool,
        connect_args={"application_name": "boss_event_idle_test", "options": (
            f"-c idle_in_transaction_session_timeout={IDLE} "
            f"-c search_path={SCHEMA}")},
    )


def _reachable() -> bool:
    probe = create_engine(URL, future=True, poolclass=NullPool,
                          connect_args={"connect_timeout": 3})
    try:
        with probe.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    finally:
        probe.dispose()


@pytest.fixture(scope="module")
def schema():
    if not _reachable():
        pytest.skip(f"test postgres unavailable: {URL.split('@')[-1]}")
    admin = create_engine(URL, future=True, poolclass=NullPool)
    ddl = f"""
    DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
    CREATE SCHEMA {SCHEMA};
    CREATE TABLE {SCHEMA}.orgs (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE {SCHEMA}.workspaces (
      id TEXT PRIMARY KEY,
      org_id TEXT NOT NULL REFERENCES {SCHEMA}.orgs(id),
      name TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE {SCHEMA}.projects (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES {SCHEMA}.workspaces(id),
      name TEXT NOT NULL,
      slug TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, slug)
    );
    CREATE TABLE {SCHEMA}.events (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES {SCHEMA}.projects(id),
      event_type TEXT NOT NULL,
      actor TEXT NOT NULL,
      entity_type TEXT NOT NULL,
      entity_id TEXT NOT NULL,
      data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
      created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    );
    """
    try:
        with admin.begin() as c:
            c.execute(text(ddl))
        yield
    finally:
        with admin.begin() as c:
            c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        admin.dispose()


def _project(engine) -> str:
    project_id = f"prj_{uuid4().hex[:12]}"
    org_id, workspace_id = f"org_{uuid4().hex[:8]}", f"ws_{uuid4().hex[:8]}"
    with engine.begin() as c:
        c.execute(text(
            f"INSERT INTO {SCHEMA}.orgs (id, name) VALUES (:id, 'idle')"),
            {"id": org_id})
        c.execute(text(
            f"INSERT INTO {SCHEMA}.workspaces (id, org_id, name) VALUES (:id, :org, 'idle')"),
            {"id": workspace_id, "org": org_id})
        c.execute(text(
            f"INSERT INTO {SCHEMA}.projects (id, workspace_id, name, slug) "
            "VALUES (:id, :ws, 'idle', :slug)"),
            {"id": project_id, "ws": workspace_id, "slug": project_id})
    return project_id


def _idle_sessions(engine) -> int:
    with engine.connect() as look:
        return int(look.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE application_name = 'boss_event_idle_test' "
            "AND state = 'idle in transaction'")).scalar() or 0)


def test_an_idle_killed_session_still_keeps_the_event(schema):
    from boss_engine.services.core import emit_event

    limited = _engine()
    try:
        project_id = _project(limited)
        marker = uuid4().hex
        with limited.connect() as held:
            held.execute(text("SELECT 1"))
            shown = held.execute(text(
                "SHOW idle_in_transaction_session_timeout")).scalar()
            assert shown in ("1s", "1000ms"), shown
            time.sleep(2.5)  # the session idles inside this transaction, past the limit
            event_id = emit_event(
                held, project_id, "note.recorded", "worker", "note",
                f"note_{marker}", {"kept": marker})
        with limited.connect() as other:
            kept = other.execute(text(
                "SELECT data->>'kept' FROM events WHERE id = :id"),
                {"id": event_id}).scalar()
        assert kept == marker
        assert _idle_sessions(limited) == 0
    finally:
        limited.dispose()


def test_a_live_transaction_keeps_its_event_until_the_caller_commits(schema):
    from boss_engine.services.core import emit_event

    limited = _engine()
    try:
        project_id = _project(limited)
        marker = uuid4().hex
        with limited.connect() as held:
            event_id = emit_event(
                held, project_id, "note.recorded", "worker", "note",
                f"note_{marker}", {"kept": marker})
            with limited.connect() as other:
                visible = other.execute(text(
                    "SELECT 1 FROM events WHERE id = :id"), {"id": event_id}).first()
            assert visible is None
            held.commit()
        with limited.connect() as other:
            kept = other.execute(text(
                "SELECT data->>'kept' FROM events WHERE id = :id"),
                {"id": event_id}).scalar()
        assert kept == marker
    finally:
        limited.dispose()
