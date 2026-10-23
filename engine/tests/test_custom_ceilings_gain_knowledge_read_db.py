"""An agent with a hand-set Passport ceiling gains knowledge:read, once (operator decision 2026-10-22).

The baseline reads go to every agent. A Passport whose ceiling IS a preset grows to it
(`passports._grow_to_preset`); one a person set by hand (`custom`) is never touched, so an agent
given a custom ceiling before knowledge:read existed kept refusing every kg_* tool. The
migration grants it once per org, audited; a person who removes it afterwards is not overruled,
and an agent whose ceiling is a preset, or that is revoked, is left alone.
"""
from __future__ import annotations

import os

import pytest
from boss_engine.db import ensure_schema
from boss_engine.identity import migration, passports
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ORG = "org_cc"


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


def _agent(c, agent_id: str, preset: str, ceiling: list[str], status: str = "active") -> None:
    c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                   "VALUES (:a, :o, 'agent', :s, :a)"), {"a": agent_id, "o": ORG, "s": status})
    c.execute(text("INSERT INTO identity_agent_passports (agent_id, role_key, sponsor_principal_id, ceiling_preset) "
                   "VALUES (:a, 'custom_role', 'usr_ccowner0001', :p)"), {"a": agent_id, "p": preset})
    for key in ceiling:
        c.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) VALUES (:a, :k)"),
                  {"a": agent_id, "k": key})


def _ceiling(c, agent_id: str) -> set[str]:
    return set(c.execute(text("SELECT permission_key FROM identity_agent_ceilings WHERE agent_id = :a"),
                         {"a": agent_id}).scalars().all())


@pytest.fixture()
def c(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'CC')"), {"o": ORG})
            conn.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                              "VALUES ('usr_ccowner0001', :o, 'user', 'active', 'Owner')"), {"o": ORG})
            _agent(conn, "agt_cccustom001", "custom", ["code_task:read"])
            _agent(conn, "agt_ccpreset001", "baseline", ["code_task:read"])
            _agent(conn, "agt_ccrevoke001", "custom", ["code_task:read"], status="revoked")
            yield conn
        finally:
            transaction.rollback()


def test_a_custom_ceiling_gains_knowledge_read_once_and_audited(c):
    added = passports.grant_baseline_to_custom_ceilings(c, org_id=ORG)
    assert added == {"agt_cccustom001": ["knowledge:read"]}
    assert _ceiling(c, "agt_cccustom001") == {"code_task:read", "knowledge:read"}
    assert _ceiling(c, "agt_ccpreset001") == {"code_task:read"}         # a preset grows by _grow_to_preset
    assert _ceiling(c, "agt_ccrevoke001") == {"code_task:read"}         # a revoked agent is left alone
    assert c.execute(text("SELECT version FROM identity_agent_passports WHERE agent_id = 'agt_cccustom001'")
                     ).scalar_one() == 2
    event = c.execute(text(
        "SELECT actor_principal_id, details FROM identity_security_events WHERE org_id = :o "
        "AND event_type = 'agent.passport.ceiling_changed' AND target_id = 'agt_cccustom001'"),
        {"o": ORG}).mappings().one()
    assert event["actor_principal_id"] == "sys_migration" and event["details"]["added"] == ["knowledge:read"]


def test_a_person_who_removes_it_afterwards_is_not_overruled(c):
    passports.grant_baseline_to_custom_ceilings(c, org_id=ORG)
    c.execute(text("DELETE FROM identity_agent_ceilings WHERE agent_id = 'agt_cccustom001' "
                   "AND permission_key = 'knowledge:read'"))
    assert passports.grant_baseline_to_custom_ceilings(c, org_id=ORG) == {}
    assert _ceiling(c, "agt_cccustom001") == {"code_task:read"}


def test_the_identity_seed_runs_it(c):
    """The migration runs where every upgraded install already maps identity: M0's seed."""
    migration.seed(c, org_id=ORG)
    assert "knowledge:read" in _ceiling(c, "agt_cccustom001")
