"""Shared support for the incident squad tests (BOSSAPP-910). Nothing here is collected.

Each test gets its own project (as _ambient_support does) and `purge_sre` removes what the squad
and the Order API scenario write beyond the ambient tables: incidents and their evidence,
postmortems, the alert, KG nodes/edges/usage.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from ._ambient_support import make_project, pg_engine, purge

def add_person(c, pid: str, member: str = "ceo") -> None:
    """A person who may answer decisions in this project (DecisionService checks membership)."""
    import json

    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, :m, :m, 'human', CAST(:r AS JSONB), "
        "CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_{member}", "p": pid, "m": member, "r": json.dumps(["ceo"]),
         "perm": json.dumps(["decision_resolve"])})


SRE_TABLES = ("project_members", "sre_postmortems", "incident_recovery_checks", "app_alerts", "kg_usage",
              "kg_audit", "kg_edges", "kg_nodes")


def purge_sre(eng, pid: str) -> None:
    with eng.begin() as c:
        for table in SRE_TABLES:
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table some installs lack
                pass
        c.execute(text("DELETE FROM incidents WHERE project_id = :p"), {"p": pid})
    purge(eng, pid)


@pytest.fixture(scope="module")
def eng():
    return pg_engine()


@pytest.fixture()
def project(eng):
    pid = make_project(eng)
    yield pid
    purge_sre(eng, pid)
