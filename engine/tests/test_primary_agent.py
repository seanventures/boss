"""One primary agent per person per company.

Setting a primary replaces that person's previous primary in that company only.
A second company keeps its own primary for the same person. Neither write
changes the person's authority (role, permissions, status, member type).
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services.primary_agent import authority_of, primary_agent, set_primary

TEST_DB = os.environ["DATABASE_URL"]


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    ensure_schema(engine, get_settings())
    return engine


def _org(conn, name: str) -> tuple[str, str]:
    org_id = f"org_{uuid.uuid4().hex[:12]}"
    workspace_id = f"ws_{uuid.uuid4().hex[:12]}"
    project_id = f"prj_{uuid.uuid4().hex[:12]}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": name})
    conn.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, :n)"),
        {"i": workspace_id, "o": org_id, "n": name},
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:i, :w, :n, :s)"
        ),
        {"i": project_id, "w": workspace_id, "n": name, "s": project_id},
    )
    return org_id, project_id


def _person(conn, org_id: str, person_id: str, *, roles: list[str], permissions: list[str]) -> None:
    import json

    conn.execute(
        text(
            "INSERT INTO org_members (id, org_id, member_id, display_name, member_type, "
            "role_keys, permissions, status) "
            "VALUES (:i, :o, :p, :d, 'human', CAST(:r AS JSONB), CAST(:perm AS JSONB), 'active')"
        ),
        {
            "i": f"mem_{uuid.uuid4().hex[:12]}",
            "o": org_id,
            "p": person_id,
            "d": person_id,
            "r": json.dumps(roles),
            "perm": json.dumps(permissions),
        },
    )


def _agent(conn, project_id: str, name: str) -> str:
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO agent_definitions (id, project_id, agent_key, name, owner, "
            "status, created_by) VALUES (:i, :p, :k, :n, 'ada', 'active', 'ada')"
        ),
        {"i": agent_id, "p": project_id, "k": agent_id, "n": name},
    )
    return agent_id


def test_setting_a_primary_clears_the_previous_one_and_leaves_authority(eng):
    person = f"ada_{uuid.uuid4().hex[:8]}"
    with eng.begin() as conn:
        org_id, project_id = _org(conn, "Northwind")
        _person(conn, org_id, person, roles=["operator"], permissions=["agent_chat", "task_create"])
        first = _agent(conn, project_id, "Clerk")
        second = _agent(conn, project_id, "Captain")
    with eng.begin() as conn:
        before = authority_of(conn, org_id=org_id, person_id=person)

    with eng.begin() as conn:
        set_primary(conn, org_id=org_id, person_id=person, agent_id=first)
    with eng.begin() as conn:
        assert primary_agent(conn, org_id=org_id, person_id=person) == first

    with eng.begin() as conn:
        set_primary(conn, org_id=org_id, person_id=person, agent_id=second)
    with eng.begin() as conn:
        assert primary_agent(conn, org_id=org_id, person_id=person) == second
        assert authority_of(conn, org_id=org_id, person_id=person) == before


def test_a_second_company_keeps_its_own_primary_and_authority(eng):
    person = f"ada_{uuid.uuid4().hex[:8]}"
    with eng.begin() as conn:
        north, north_project = _org(conn, "Northwind")
        south, south_project = _org(conn, "Southwind")
        _person(conn, north, person, roles=["operator"], permissions=["agent_chat"])
        _person(conn, south, person, roles=["viewer"], permissions=["audit_export"])
        north_agent = _agent(conn, north_project, "North clerk")
        south_agent = _agent(conn, south_project, "South clerk")
    with eng.begin() as conn:
        north_before = authority_of(conn, org_id=north, person_id=person)
        south_before = authority_of(conn, org_id=south, person_id=person)

    with eng.begin() as conn:
        set_primary(conn, org_id=north, person_id=person, agent_id=north_agent)
        set_primary(conn, org_id=south, person_id=person, agent_id=south_agent)

    with eng.begin() as conn:
        assert primary_agent(conn, org_id=north, person_id=person) == north_agent
        assert primary_agent(conn, org_id=south, person_id=person) == south_agent
        assert authority_of(conn, org_id=north, person_id=person) == north_before
        assert authority_of(conn, org_id=south, person_id=person) == south_before
        assert north_before["role_keys"] == ["operator"]
        assert south_before["role_keys"] == ["viewer"]
        assert north_before["permissions"] == ["agent_chat"]
        assert south_before["permissions"] == ["audit_export"]
