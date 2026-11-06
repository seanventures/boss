"""Shared support for the continuous-learning tests (BOSSAPP-911/912). Nothing here is collected.

Each test gets its own project (as _ambient_support does). The learning tests also write code
graph, incidents, builds, scorecards, meetings and learning examples, so the purge here clears
every table that holds a project_id before the project goes (a few passes: foreign keys between
those tables decide the order, and a pass skips what is still referenced).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from ._ambient_support import make_project, pg_engine, purge


def purge_all(eng, pid: str) -> None:
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name = 'project_id' "
            "AND table_schema = current_schema() AND table_name IN (SELECT table_name FROM "
            "information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema = "
            "current_schema())"))]
        c.execute(text("DELETE FROM signal_adapter_cursors WHERE adapter LIKE :a"),
                  {"a": f"%@{pid}"})
    for _ in range(4):
        with eng.begin() as c:
            c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
            for table in tables:
                if table in ("projects",):
                    continue
                try:
                    with c.begin_nested():
                        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
                except Exception:  # noqa: BLE001 — still referenced; the next pass gets it
                    pass
    purge(eng, pid)


@pytest.fixture(scope="module")
def eng():
    return pg_engine()


@pytest.fixture()
def project(eng):
    pid = make_project(eng)
    yield pid
    purge_all(eng, pid)
