"""Hardware places: the installer machine is the default, and one agent is required."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from boss_engine.services.hardware_manager import (
    PlaceRejected,
    delete_place,
    enroll_place_agent,
    ensure_local_place,
    mark_place_ready,
    record_community_local_place,
    register_agent_place,
    register_provider_place,
    resolve_place,
    retire_place,
)

ROOT = Path(__file__).resolve().parents[2]
# The suite's test database, as every other database test reads it (this file named one
# developer's port, 55455, and failed everywhere else).
URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
HOOK_URL = URL + ("&" if "?" in URL else "?") + "options=-csearch_path%3Dhw_kernel"


def _ddl() -> list[str]:
    """schema.sql's hardware statements, as written there: the tables, indexes and alters of
    hardware_*. ("Everything after hardware_places" stopped standing alone once later sections
    altered tables defined before it.)"""
    import re

    from boss_engine.db import split_statements

    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    return [s for s in split_statements(schema) if re.search(r"\bhardware_\w+", s)]


def _hook():
    path = ROOT / "scripts" / "install" / "ensure_local_place.py"
    spec = importlib.util.spec_from_file_location("ensure_local_place_hook", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    if not URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    eng = sa.create_engine(URL, future=True)
    with eng.begin() as connection:
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS hw_kernel"))
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY)"))
        for statement in _ddl():
            statement = statement.strip()
            if statement:
                connection.execute(text(statement))
    yield eng
    with eng.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS hw_kernel CASCADE"))
    eng.dispose()


@pytest.fixture()
def conn(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("INSERT INTO projects (id) VALUES ('proj_a'), ('proj_b')"))
        yield connection
        transaction.rollback()


def test_local_row_is_the_default_and_resolve_none_returns_it(conn):
    created = ensure_local_place(
        conn, project_id="proj_a", hostname="installer", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    assert created["kind"] == "local"
    assert created["is_default"] is True
    assert created["status"] == "pending"
    assert created["agent_state"] == "absent"
    again = ensure_local_place(
        conn, project_id="proj_a", hostname="installer-renamed", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    assert again["id"] == created["id"]
    assert again["name"] == "installer-renamed"
    assert again["labels"]["hostname"] == "installer-renamed"
    assert again["is_default"] is True
    assert again["agent_state"] == "absent"
    assert resolve_place(conn, "proj_a", None)["id"] == created["id"]


def test_second_local_rejected_without_breaking_the_first(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("INSERT INTO projects (id) VALUES ('proj_local')"))
        first = ensure_local_place(
            connection, project_id="proj_local", hostname="box", arch="amd64",
            os_name="Linux", actor="community-installer",
        )
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO hardware_places ("
                " id, project_id, kind, name, status, agent_state, is_default, created_by"
                ") VALUES ('hp_second', 'proj_local', 'local', 'other', 'pending',"
                " 'absent', true, 'community-installer')"
            ))
        transaction.rollback()
        transaction = connection.begin()
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("INSERT INTO projects (id) VALUES ('proj_local')"))
        restored = ensure_local_place(
            connection, project_id="proj_local", hostname="box", arch="amd64",
            os_name="Linux", actor="community-installer",
        )
        assert restored["kind"] == "local"
        assert restored["is_default"] is True
        assert restored["name"] == "box"
        transaction.rollback()
        assert first["kind"] == "local"


def test_community_ensure_records_the_local_agent_installed(conn):
    place = record_community_local_place(
        conn, project_id="proj_a", hostname="installer", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    assert place["kind"] == "local"
    assert place["is_default"] is True
    assert place["agent_state"] == "installed"
    assert place["status"] == "ready"
    assert resolve_place(conn, "proj_a", None)["id"] == place["id"]
    renamed = ensure_local_place(
        conn, project_id="proj_a", hostname="installer-2", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    assert renamed["id"] == place["id"]
    assert renamed["name"] == "installer-2"
    assert renamed["agent_state"] == "installed"
    assert renamed["is_default"] is True


def test_additional_hardware_stays_non_default_until_its_agent_is_installed(conn):
    local = record_community_local_place(
        conn, project_id="proj_a", hostname="installer", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    extra = register_agent_place(
        conn, project_id="proj_a", name="vm-1", actor="operator",
    )
    provider = register_provider_place(
        conn, project_id="proj_a", name="do-account", provider_key="digitalocean",
        actor="operator", secret_ref="env:DO_API_TOKEN",
    )
    assert extra["is_default"] is False and extra["agent_state"] == "absent"
    assert extra["status"] == "pending"
    assert provider["is_default"] is False and provider["agent_state"] == "absent"
    with pytest.raises(PlaceRejected, match="not installed"):
        mark_place_ready(conn, place_id=extra["id"])
    still = resolve_place(conn, "proj_a", extra["id"])
    assert still["status"] == "pending" and still["is_default"] is False
    enrolled = enroll_place_agent(conn, project_id="proj_a", place_id=extra["id"])
    assert enrolled["agent_state"] == "installed"
    assert enrolled["status"] == "pending" and enrolled["is_default"] is False
    ready = mark_place_ready(conn, place_id=extra["id"], endpoint="https://vm-1.example")
    assert ready["status"] == "ready" and ready["is_default"] is False
    assert ready["agent_state"] == "installed"
    assert resolve_place(conn, "proj_a", None)["id"] == local["id"]
    assert resolve_place(conn, "proj_a", None)["is_default"] is True


def test_local_retire_and_delete_are_rejected(conn):
    place = ensure_local_place(
        conn, project_id="proj_a", hostname="installer", arch="arm64",
        os_name="Linux", actor="community-installer",
    )
    with pytest.raises(PlaceRejected, match="cannot be retired"):
        retire_place(conn, project_id="proj_a", place_id=place["id"], actor="operator")
    with pytest.raises(PlaceRejected, match="cannot be deleted"):
        delete_place(conn, project_id="proj_a", place_id=place["id"])
    kept = resolve_place(conn, "proj_a", None)
    assert kept["id"] == place["id"] and kept["status"] == "pending"


def test_installer_hook_records_installed_agent_and_calls_node_agent(engine, monkeypatch, tmp_path):
    hook = _hook()
    called = {}

    def fake_run(argv, **kwargs):
        called["argv"] = argv
        class Result:
            returncode = 127
        return Result()

    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    missing = tmp_path / "node_agent.sh"
    assert hook.local_agent_state(missing) == "absent"
    assert "argv" not in called

    present = tmp_path / "node_agent_fail.sh"
    present.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    monkeypatch.undo()
    assert hook.local_agent_state(present) == "absent"
    ok = tmp_path / "node_agent_ok.sh"
    ok.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert hook.local_agent_state(ok) == "installed"

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "scripts/install/node_agent.sh" in installer
    assert "--local" in installer
    assert "recording the local hardware agent as installed" not in installer
    assert "not marked ready" in installer

    with engine.begin() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text(
            "INSERT INTO projects (id) VALUES ('proj_hook') ON CONFLICT (id) DO NOTHING"
        ))
    rc = hook.main([
        "--project-id", "proj_hook",
        "--database-url", HOOK_URL,
        "--hostname", "community-host",
        "--arch", "arm64",
        "--os-name", "Linux",
        "--actor", "community-installer",
        "--agent-state", "installed",
    ])
    assert rc == 0
    with engine.connect() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        row = connection.execute(text(
            "SELECT kind, is_default, agent_state, status, name "
            "FROM hardware_places WHERE project_id = 'proj_hook'"
        )).mappings().one()
    assert row["kind"] == "local"
    assert row["is_default"] is True
    assert row["agent_state"] == "installed"
    assert row["status"] == "ready"
    assert row["name"] == "community-host"
    with engine.begin() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("DELETE FROM hardware_places WHERE project_id = 'proj_hook'"))
        connection.execute(text("DELETE FROM projects WHERE id = 'proj_hook'"))


def test_missing_node_agent_does_not_mark_the_local_place_ready(engine, monkeypatch, tmp_path):
    hook = _hook()
    missing = tmp_path / "node_agent.sh"
    assert hook.local_agent_state(missing) == "absent"
    monkeypatch.setattr(hook, "NODE_AGENT", missing)
    monkeypatch.delenv("BOSS_PLACE_AGENT_STATE", raising=False)
    monkeypatch.delenv("BOSS_PLACE_PROJECT_ID", raising=False)
    monkeypatch.delenv("BOSS_DEFAULT_PROJECT_ID", raising=False)
    with engine.begin() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text(
            "INSERT INTO projects (id) VALUES ('proj_missing') ON CONFLICT (id) DO NOTHING"
        ))
    rc = hook.main([
        "--project-id", "proj_missing",
        "--database-url", HOOK_URL,
        "--hostname", "community-host",
        "--arch", "arm64",
        "--os-name", "Linux",
        "--actor", "community-installer",
    ])
    assert rc == 1
    with engine.connect() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        count = connection.execute(text(
            "SELECT count(*) FROM hardware_places WHERE project_id = 'proj_missing'"
        )).scalar()
        ready = connection.execute(text(
            "SELECT count(*) FROM hardware_places "
            "WHERE project_id = 'proj_missing' AND status = 'ready'"
        )).scalar()
    assert count == 0
    assert ready == 0
    with engine.begin() as connection:
        connection.execute(text("SET search_path TO hw_kernel"))
        connection.execute(text("DELETE FROM projects WHERE id = 'proj_missing'"))
