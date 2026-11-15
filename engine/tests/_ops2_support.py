"""Shared fixtures for the ops set 2 tests (BOSSAPP-891..894): one disposable database, one
project per test.

The database is BOSS_TEST_DATABASE_URL's server with a database of its own, created with psycopg
for the module and dropped after, so these tests never touch a database somebody else uses.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text


def fresh_engine(suffix: str):
    """(engine, drop) on a new boss_scratch_ops2<suffix>_<rand>_<stamp> database with the schema
    applied. The scratch helper drops it on `drop()`, at exit, and — should both be skipped by
    a killed process — `boss dev sweep-test-db` does after 24 h, by the stamp in its name."""
    from boss_engine.db import ensure_schema
    from boss_engine.dev.test_databases import scratch_database

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not url or "test" not in name:
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable boss_test* database")
    scratch = scratch_database(url, f"ops2{suffix}")
    try:
        scratch_url = scratch.__enter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create the test database: {type(exc).__name__}")
    engine = create_engine(scratch_url, future=True)
    ensure_schema(engine)

    def drop() -> None:
        engine.dispose()
        scratch.__exit__(None, None, None)

    return engine, drop


def make_project(engine, *, roles: tuple[str, ...] = ("pm", "backend_engineer")) -> str:
    """A project with a CEO member who may resolve decisions, and the given role rows."""
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_ops2', 'ops2') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES "
                       "('ws_ops2', 'org_ops2', 'ops2') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, 'ws_ops2', :i, :i)"), {"i": pid})
        for role in roles:
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name) "
                           "VALUES (:i, :p, :r, :r)"),
                      {"i": f"role_{uuid.uuid4().hex[:8]}", "p": pid, "r": role})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', CAST(:r AS JSONB), "
            "CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid, "r": json.dumps(["ceo"]),
             "perm": json.dumps(["decision_resolve"])})
    return pid


def decide(engine, decision_id: str, action: str, note: str | None = None) -> dict:
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    with engine.begin() as c:
        return DecisionService(get_settings()).transition(c, decision_id, action=action,
                                                          actor="ceo", note=note)
