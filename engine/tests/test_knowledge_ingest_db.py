"""Event-driven ingest: a finished run or a resolved incident reaches the graph within a tick.

Breaks covered (operator decision 2026-10-21, ported from PR #13): a finish that only some code
paths report; a record waiting for a 10-minute sweep instead of the 30-second drain; a record the
triggers missed never captured; one bad record holding the rest back; a secret in a run's text.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import ingest
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
ROOT = Path(__file__).resolve().parents[2]


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
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
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_in', 'IN')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_in', 'org_in', 'IN')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_in', 'ws_in', 'IN', 'in')"))
            yield conn
        finally:
            transaction.rollback()


def _run(connection, run_id: str, status: str = "running") -> None:
    connection.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, spec, state) VALUES "
        "(:i, 'proj_in', 'Hold stock at checkout', :s, CAST(:spec AS JSONB), CAST(:state AS JSONB))"),
        {"i": run_id, "s": status, "spec": '{"instructions": "reserve before commit"}',
         "state": '{"summary": "deployed with key AKIAIOSFODNN7EXAMPLE"}'})


def _queued(connection) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in connection.execute(text(
        "SELECT source_kind, source_id FROM kg_ingest_queue WHERE project_id = 'proj_in'"))}


def test_a_finished_run_and_a_resolved_incident_are_queued_by_trigger_and_captured(connection):
    _run(connection, "ctr_1")
    connection.execute(text(
        "INSERT INTO incidents (id, project_id, dedupe_key, title, service) "
        "VALUES ('inc_1', 'proj_in', 'k1', 'Checkout 500s', 'checkout')"))
    assert _queued(connection) == set()
    connection.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = 'ctr_1'"))
    connection.execute(text("UPDATE incidents SET status = 'resolved' WHERE id = 'inc_1'"))
    connection.execute(text("UPDATE code_task_runs SET title = 'Hold stock' WHERE id = 'ctr_1'"))  # no re-queue
    assert _queued(connection) == {("run", "ctr_1"), ("incident", "inc_1")}

    out = ingest.tick(_Same(connection))
    assert out["queued"] == {"captured": 2, "gone": 0, "failed": 0}
    assert _queued(connection) == set()
    run = STORE.find_by_key(connection, project_id="proj_in", kind="run", node_key="run:ctr_1")
    incident = STORE.find_by_key(connection, project_id="proj_in", kind="incident", node_key="incident:inc_1")
    assert run is not None and incident is not None
    assert "reserve before commit" in run["body"] and "AKIAIOSFODNN7EXAMPLE" not in run["body"]
    assert ingest.tick(_Same(connection), sweep=True)["sweep"] == {"captured": 0, "gone": 0, "failed": 0}


def test_the_sweep_catches_a_record_the_triggers_missed(connection):
    _run(connection, "ctr_old", status="failed")            # inserted finished: no UPDATE, no trigger
    assert _queued(connection) == set()
    assert ingest.tick(_Same(connection))["queued"]["captured"] == 0
    assert ingest.tick(_Same(connection), sweep=True)["sweep"]["captured"] == 1
    assert STORE.find_by_key(connection, project_id="proj_in", kind="run", node_key="run:ctr_old")


def test_a_bad_record_stays_queued_with_its_error_and_the_rest_are_captured(connection, monkeypatch):
    for run_id in ("ctr_a", "ctr_b"):
        _run(connection, run_id)
        connection.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"), {"i": run_id})
    real = ingest.record_for

    def flaky(conn, project_id, kind, source_id):
        if source_id == "ctr_a":
            raise RuntimeError("unreadable")
        return real(conn, project_id, kind, source_id)

    monkeypatch.setattr(ingest, "record_for", flaky)
    assert ingest.tick(_Same(connection))["queued"] == {"captured": 1, "gone": 0, "failed": 1}
    row = connection.execute(text(
        "SELECT attempts, last_error FROM kg_ingest_queue WHERE source_id = 'ctr_a'")).one()
    assert row.attempts == 1 and "unreadable" in row.last_error


def test_the_worker_drains_every_30_seconds_and_sweeps_every_10_minutes():
    main = (ROOT / "apps" / "worker" / "boss_worker" / "main.py").read_text()
    assert "KNOWLEDGE_INGEST_INTERVAL_SECONDS = 30" in main
    assert "KNOWLEDGE_SWEEP_INTERVAL_SECONDS = 600" in main
    assert "_tick_knowledge_ingest(client)" in main
    assert ingest.QUEUE_POLL_SECONDS == 30 and ingest.SWEEP_SECONDS == 600
    client = (ROOT / "apps" / "worker" / "boss_worker" / "control_plane.py").read_text()
    assert '"/v1/knowledge/ingest/tick"' in client
