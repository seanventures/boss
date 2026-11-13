"""Shared by the BOSSAPP-849..851 tests: one disposable database (boss_test_p29b), made with
psycopg for the module that asks and dropped after it, and the rows a project needs."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

DB_NAME = "boss_test_p29b"
ECHO_SERVER = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio_echo_server.py"


def _admin_and_base():
    from sqlalchemy.engine import make_url

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not url or "test" not in name:
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable boss_test* database")
    base = make_url(url)
    admin = base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False)
    return admin, base


def fresh_engine():
    """A new, empty boss_test_p29b with the full schema. Returns (engine, admin_url)."""
    import psycopg

    from boss_engine.db import ensure_schema

    admin, base = _admin_and_base()
    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{DB_NAME}"')
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create {DB_NAME}: {type(exc).__name__}")
    engine = create_engine(base.set(database=DB_NAME), future=True)
    ensure_schema(engine)
    return engine, admin


def drop(engine, admin: str) -> None:
    import psycopg

    engine.dispose()
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


def make_project(engine, project: str, org: str = "org_p29b") -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o) ON CONFLICT DO NOTHING"),
                  {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w) "
                       "ON CONFLICT DO NOTHING"), {"w": f"ws_{org}", "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, :w, :p, :p) ON CONFLICT DO NOTHING"),
                  {"p": project, "w": f"ws_{org}"})


def echo_template(name: str = "echo", **over) -> dict:
    """A template that runs the fixture server with this interpreter (subprocess mode)."""
    fields = {"name": name, "image": "test-only:unused", "argv": [sys.executable, "-u",
                                                                  str(ECHO_SERVER)],
              "network": "none", "env_allowlist": ["ECHO_TOKEN", "ECHO_GREETING"],
              "env_defaults": {"ECHO_GREETING": "hello"}, "env_from_auth": "ECHO_TOKEN",
              "idle_ttl_seconds": 60, "reuse_key": "credential"}
    fields.update(over)
    return fields


def stdio_runtime(monkeypatch):
    """The supervisor's TEST-ONLY unsandboxed mode, and every instance stopped after. A
    generator: each module's `subprocess_runtime` fixture yields from it."""
    from boss_engine.services import mcp_client, mcp_stdio_supervisor

    monkeypatch.setenv(mcp_stdio_supervisor.RUNTIME_ENV, "subprocess")
    monkeypatch.setenv(mcp_stdio_supervisor.TEST_ONLY_FLAG, "1")
    mcp_client.forget_sessions()
    yield mcp_stdio_supervisor.SUPERVISOR
    mcp_stdio_supervisor.SUPERVISOR.stop_all("test")
    mcp_client.forget_sessions()
