"""A builder freezes a file or a data table so an agent cannot change it.

A write to a frozen path is refused and the bytes stay as they were.
An unfrozen path can change. BOSSAPP-1269.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from boss_engine.services import freeze_paths
from boss_engine.services.freeze_paths import FreezeError, FrozenPath

ROOT = Path(__file__).resolve().parents[2]
MARKER = "-- BOSSAPP-1269:"


def _schema_statements() -> list[str]:
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = schema.find(MARKER)
    assert start >= 0, "db/schema.sql has no -- BOSSAPP-1269: block"
    lines = schema[start:].splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        if line.startswith("-- BOSSAPP-") and not line.startswith("-- BOSSAPP-1269"):
            break
        kept.append(line)
    statements: list[str] = []
    buf: list[str] = []
    for line in kept:
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
    assert statements, "the BOSSAPP-1269 block has no statements"
    return statements


def _parents(connection) -> None:
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS orgs ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    ))
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS workspaces ("
        " id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES orgs(id),"
        " name TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    ))
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS projects ("
        " id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),"
        " name TEXT NOT NULL, slug TEXT NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " UNIQUE (workspace_id, slug))"
    ))
    connection.execute(text(
        "INSERT INTO orgs (id, name) VALUES ('org-1269', 'Hold') ON CONFLICT (id) DO NOTHING"
    ))
    connection.execute(text(
        "INSERT INTO workspaces (id, org_id, name) VALUES ('ws-1269', 'org-1269', 'Hold')"
        " ON CONFLICT (id) DO NOTHING"
    ))


def _project(connection, project_id: str) -> None:
    connection.execute(text(
        "INSERT INTO projects (id, workspace_id, name, slug)"
        " VALUES (:id, 'ws-1269', 'Hold', :id) ON CONFLICT (id) DO NOTHING"
    ), {"id": project_id})


@pytest.fixture(autouse=True)
def _clear_holds(engine):
    """Each test starts with no holds. A previous run commits its rows."""
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM freeze_table_bytes"))
        connection.execute(text("DELETE FROM freeze_paths"))


@pytest.fixture(scope="module")
def engine():
    url = os.environ["DATABASE_URL"]
    eng = create_engine(url, future=True)
    with eng.begin() as connection:
        _parents(connection)
        for statement in _schema_statements():
            connection.execute(text(statement))
    yield eng
    eng.dispose()


@pytest.fixture()
def connection(engine):
    with engine.begin() as conn:
        yield conn


def test_a_frozen_file_refuses_a_write_and_keeps_its_bytes(connection, tmp_path):
    project = "file-frozen"
    _project(connection, project)
    original = b"keep-these-bytes\xff"
    freeze_paths.write_file(connection, project, tmp_path, "notes.txt", original)
    freeze_paths.freeze(connection, project, "file", "notes.txt", actor="builder")

    with pytest.raises(FrozenPath, match="refused"):
        freeze_paths.write_file(connection, project, tmp_path, "notes.txt", b"replaced")

    assert (tmp_path / "notes.txt").read_bytes() == original


def test_an_unfrozen_file_can_change(connection, tmp_path):
    project = "file-open"
    _project(connection, project)
    freeze_paths.write_file(connection, project, tmp_path, "open.txt", b"before")
    freeze_paths.write_file(connection, project, tmp_path, "open.txt", b"after")
    assert (tmp_path / "open.txt").read_bytes() == b"after"


def test_a_frozen_table_refuses_a_write_and_keeps_its_bytes(connection):
    project = "table-frozen"
    _project(connection, project)
    original = b"row-alpha\x00\xff"
    freeze_paths.write_table(connection, project, "customers", original)
    freeze_paths.freeze(connection, project, "table", "customers", actor="builder")

    with pytest.raises(FrozenPath, match="bytes stayed"):
        freeze_paths.write_table(connection, project, "customers", b"row-beta")

    assert freeze_paths.read_table(connection, project, "customers") == original


def test_an_unfrozen_table_can_change(connection):
    project = "table-open"
    _project(connection, project)
    freeze_paths.write_table(connection, project, "orders", b"v1")
    freeze_paths.write_table(connection, project, "orders", b"v2")
    assert freeze_paths.read_table(connection, project, "orders") == b"v2"


def test_thaw_lets_the_same_path_change(connection, tmp_path):
    project = "thaw"
    _project(connection, project)
    freeze_paths.write_file(connection, project, tmp_path, "held.txt", b"one")
    freeze_paths.freeze(connection, project, "file", "held.txt")
    freeze_paths.thaw(connection, project, "file", "held.txt")
    freeze_paths.write_file(connection, project, tmp_path, "held.txt", b"two")
    assert (tmp_path / "held.txt").read_bytes() == b"two"


def test_freezing_a_file_does_not_freeze_the_same_named_table(connection, tmp_path):
    project = "kinds"
    _project(connection, project)
    freeze_paths.write_table(connection, project, "ledger", b"table-bytes")
    freeze_paths.freeze(connection, project, "file", "ledger")
    freeze_paths.write_table(connection, project, "ledger", b"table-moved")
    assert freeze_paths.read_table(connection, project, "ledger") == b"table-moved"
    with pytest.raises(FrozenPath):
        freeze_paths.write_file(connection, project, tmp_path, "ledger", b"file-bytes")
    assert not (tmp_path / "ledger").exists()


def test_a_path_that_climbs_out_is_refused(connection, tmp_path):
    project = "climb"
    _project(connection, project)
    with pytest.raises(FreezeError):
        freeze_paths.write_file(connection, project, tmp_path, "../outside.txt", b"no")
    assert not (tmp_path.parent / "outside.txt").exists()


def test_http_write_to_a_frozen_path_is_refused_and_an_open_path_changes(engine, tmp_path, monkeypatch):
    project = "http"
    with engine.begin() as connection:
        _project(connection, project)
    monkeypatch.setenv("BOSS_ARTIFACTS_DIR", str(tmp_path))
    from apps.api.app.routers.gap_1269 import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    seeded = client.post("/v1/freeze-paths/write", json={
        "project_id": project, "kind": "file", "path": "src/keep.txt", "content": "OLD",
    })
    assert seeded.status_code == 200, seeded.text
    held = client.post("/v1/freeze-paths", json={
        "project_id": project, "kind": "file", "path": "src/keep.txt",
    })
    assert held.status_code == 200, held.text
    assert held.json()["frozen"] is True

    refused = client.post("/v1/freeze-paths/write", json={
        "project_id": project, "kind": "file", "path": "src/keep.txt", "content": "NEW",
    })
    assert refused.status_code == 409
    assert "refused" in refused.json()["detail"]
    root = tmp_path / "freeze-paths" / project
    assert (root / "src" / "keep.txt").read_bytes() == b"OLD"

    opened = client.post("/v1/freeze-paths/write", json={
        "project_id": project, "kind": "file", "path": "src/open.txt", "content": "yes",
    })
    assert opened.status_code == 200
    assert (root / "src" / "open.txt").read_bytes() == b"yes"

    listed = client.get("/v1/freeze-paths", params={"project_id": project})
    assert listed.status_code == 200
    paths = {(row["kind"], row["path"]) for row in listed.json()["paths"]}
    assert ("file", "src/keep.txt") in paths
    assert ("file", "src/open.txt") not in paths

    thawed = client.post("/v1/freeze-paths/thaw", json={
        "project_id": project, "kind": "file", "path": "src/keep.txt",
    })
    assert thawed.status_code == 200
    assert thawed.json()["frozen"] is False
    changed = client.post("/v1/freeze-paths/write", json={
        "project_id": project, "kind": "file", "path": "src/keep.txt", "content": "NEW",
    })
    assert changed.status_code == 200
    assert (root / "src" / "keep.txt").read_bytes() == b"NEW"
