"""official.sqlite: no container needed, so this runs everywhere."""
from __future__ import annotations

import sqlite3

import pytest

from boss_engine.infra_plugins import load_plugin
from boss_engine.platform.contract import OperationStatus, ResourceRequest

from ._harness import Harness


@pytest.fixture(autouse=True)
def _sqlite_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_INFRA_SQLITE_DIR", str(tmp_path / "sqlite"))


def _req(**kw) -> ResourceRequest:
    return ResourceRequest.model_validate({"name": "local", "capability": "database.relational",
                                           "engine": "sqlite", "environment": "build", **kw})


def test_sqlite_is_refused_in_production_and_outside_its_directory(tmp_path):
    h = Harness(load_plugin("sqlite"), tmp_path)
    assert h.call("validate", _req(environment="production")).status is OperationStatus.INVALID
    assert h.call("validate", _req(config={"path": "../escape.db"})).status is OperationStatus.INVALID


def test_sqlite_lifecycle_with_backup_and_restore(tmp_path):
    h = Harness(load_plugin("sqlite"), tmp_path / "work")
    req = _req()
    db = tmp_path / "sqlite" / h.app_key / "local.sqlite3"

    def between(state):
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
            c.executemany("INSERT INTO notes (body) VALUES (?)", [(f"n{i}",) for i in range(300)])
        b = h.call("backup", req, state=state)
        assert b.ok and b.evidence[-1].passed and "300 rows match" in b.evidence[-1].detail
        state = {**state, **b.state}
        with sqlite3.connect(db) as c:
            c.execute("DELETE FROM notes")
        assert h.call("restore", req, state=state).ok
        with sqlite3.connect(db) as c:
            assert c.execute("SELECT count(*) FROM notes").fetchone()[0] == 300
        return state

    summary = h.lifecycle(req, between=between)
    assert not db.exists() and not list((db.parent / "backups").glob("*"))
    h.record("sqlite", summary)
