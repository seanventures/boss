"""A thread the database says already exists is used, not a crash.

shared-server: "Ask Program Manager" failed on every turn with
`UniqueViolation … idx_agent_threads_surface_external … (boss-dogfood, chat, role:Program
Manager) already exists`. Its database still had the old 3-column index, so the lookup keyed on
the agent found nothing, the INSERT hit the stale index, and the turn died. ensure_schema now
converges the index (test_an_index_converges_to_schema_sql); this is the belt and braces — a
stale index, or two first turns racing, must not break the dock.
"""
from __future__ import annotations

import os
import uuid

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import create_engine, text

from boss_engine.services import agent_service
from boss_engine.services.agent_service import AgentService

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")

TABLE = """
CREATE TABLE agent_threads (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, actor TEXT NOT NULL,
  surface TEXT NOT NULL DEFAULT 'chat', external_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active', pending_plan_id TEXT, pending_plan_summary TEXT,
  agent_role TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
"""


@pytest.fixture()
def engine(monkeypatch):
    monkeypatch.setattr(agent_service, "emit_event", lambda *a, **k: None)
    name = f"dock_{uuid.uuid4().hex[:10]}"
    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{name}"'))
    eng = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={name}"})
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def _service() -> AgentService:
    return object.__new__(AgentService)  # the thread lookup needs no router


def test_a_stale_three_column_index_reuses_the_thread_instead_of_failing(engine):
    with engine.begin() as c:
        c.execute(text(TABLE))
        c.execute(text(
            "CREATE UNIQUE INDEX idx_agent_threads_surface_external "
            "ON agent_threads(project_id, surface, external_ref) WHERE external_ref IS NOT NULL"))
        c.execute(text("INSERT INTO agent_threads (id, project_id, actor, external_ref) "
                       "VALUES ('athr_old', 'boss-dogfood', 'ceo', 'role:Program Manager')"))
    with engine.begin() as c:
        thread = _service().start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="role:Program Manager", agent_role="Program Manager")
        # The caller's transaction is still usable after the refused INSERT.
        assert c.execute(text("SELECT count(*) FROM agent_threads")).scalar_one() == 1
    assert thread["id"] == "athr_old"


def test_a_race_for_the_same_agents_thread_returns_the_winner(engine, monkeypatch):
    """Two first turns: the lookup misses for both, the second INSERT loses."""
    with engine.begin() as c:
        c.execute(text(TABLE))
        c.execute(text(
            "CREATE UNIQUE INDEX idx_agent_threads_surface_external "
            "ON agent_threads(project_id, surface, external_ref, agent_role) "
            "WHERE external_ref IS NOT NULL"))
    service = _service()
    real_insert = AgentService._insert_thread

    def racing_insert(connection, params):
        # The other turn commits its thread between our lookup and our INSERT.
        with engine.begin() as other:
            other.execute(text(
                "INSERT INTO agent_threads (id, project_id, actor, external_ref, agent_role) "
                "VALUES ('athr_winner', :p, 'ceo', :x, :r)"),
                {"p": params["project_id"], "x": params["external_ref"],
                 "r": params["agent_role"]})
        return real_insert(connection, params)

    monkeypatch.setattr(AgentService, "_insert_thread", staticmethod(racing_insert))
    with engine.begin() as c:
        thread = service.start_or_load_thread(
            c, project_id="boss-dogfood", actor="ceo", surface="chat",
            external_ref="tsk_1", agent_role="qa")
    assert thread["id"] == "athr_winner"
