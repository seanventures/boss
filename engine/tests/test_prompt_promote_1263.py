"""BOSSAPP-1263: a saved prompt is not an agent.

Saving stores the prompt on the project and creates no role. Promoting with no
decision, or with a decision that is not approved, is refused. An approved
decision creates the agent through agents.create_agent, with the same fields
POST /v1/agents accepts, not a second insert into roles.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("DATABASE_URL") or os.environ.get("BOSS_TEST_DATABASE_URL") or ""

CREATE_SHAPE = {
    "actor", "name", "purpose", "role_key", "skills", "mcp_servers", "reports_to",
    "boundaries", "persona", "born_from", "team",
}


def test_promote_is_the_agent_create_path_not_a_second_creator():
    service = ROOT / "engine/boss_engine/services/prompt_promote.py"
    router = ROOT / "apps/api/app/routers/gap_1263.py"
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    assert service.is_file() and router.is_file()
    service_text = service.read_text(encoding="utf-8")
    router_text = router.read_text(encoding="utf-8")
    assert "create_agent(" in service_text
    assert "INSERT INTO roles" not in service_text
    assert "INSERT INTO roles" not in router_text
    assert "create_agent(" not in router_text
    block = schema.split("-- BOSSAPP-1263:", 1)
    assert len(block) == 2
    assert "CREATE TABLE IF NOT EXISTS project_prompts" in block[1]


@pytest.fixture(scope="module")
def engine():
    if not PG_URL.startswith("postgres"):
        pytest.fail("DATABASE_URL must be the ticket Postgres")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as connection:
        connection.execute(text(
            "INSERT INTO orgs (id, name) VALUES ('org_pp1263', 'prompts') "
            "ON CONFLICT DO NOTHING"))
        connection.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES "
            "('ws_pp1263', 'org_pp1263', 'prompts') ON CONFLICT DO NOTHING"))
    yield eng
    eng.dispose()


@pytest.fixture()
def pg(engine):
    project = "p_pp_" + uuid4().hex[:8]
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:p, 'ws_pp1263', 'prompts', :p)"), {"p": project})
    return SimpleNamespace(engine=engine, project=project)


def _roles(connection, project: str) -> list[str]:
    return list(connection.execute(text(
        "SELECT role_key FROM roles WHERE project_id = :p ORDER BY role_key"),
        {"p": project}).scalars().all())


def _save(connection, project: str, **overrides):
    from boss_engine.services import prompt_promote

    fields = {"title": "Release notes", "body": "Compare the notes to the diff.",
              "actor": "pm"}
    fields.update(overrides)
    return prompt_promote.save_prompt(connection, project_id=project, **fields)


def test_saving_a_prompt_does_not_create_an_agent(pg, monkeypatch):
    from boss_engine.services import agents, prompt_promote

    seen: list[dict] = []

    def spy(*_args, **_kwargs):
        seen.append(_kwargs)
        raise AssertionError("saving must not create an agent")

    monkeypatch.setattr(agents, "create_agent", spy)
    with pg.engine.begin() as connection:
        saved = _save(connection, pg.project)
        again = _save(connection, pg.project)
        assert saved["agent_created"] is False
        assert saved["role_key"] is None
        assert again["id"] == saved["id"]
        assert _roles(connection, pg.project) == []
        assert prompt_promote.list_prompts(connection, project_id=pg.project)[0]["id"] == saved["id"]
    assert seen == []


def test_promoting_without_an_approved_decision_is_refused(pg):
    from boss_engine.services.prompt_promote import PromoteError, ask, promote

    with pg.engine.begin() as connection:
        saved = _save(connection, pg.project)
        with pytest.raises(PromoteError, match="no decision"):
            promote(connection, project_id=pg.project, prompt_id=saved["id"], actor="ceo")
        asked = ask(connection, project_id=pg.project, prompt_id=saved["id"], actor="pm")
        assert asked["decision_status"] == "pending"
        assert asked["agent_created"] is False
        again = ask(connection, project_id=pg.project, prompt_id=saved["id"], actor="pm")
        assert again["decision_id"] == asked["decision_id"]
        with pytest.raises(PromoteError, match="not approved"):
            promote(connection, project_id=pg.project, prompt_id=saved["id"], actor="ceo")
        connection.execute(text(
            "UPDATE decisions SET status = 'rejected', approved_by = 'ceo' WHERE id = :d"),
            {"d": asked["decision_id"]})
        with pytest.raises(PromoteError, match="not approved"):
            promote(connection, project_id=pg.project, prompt_id=saved["id"], actor="ceo")
        assert _roles(connection, pg.project) == []


def test_an_approved_decision_creates_the_agent_through_create_agent(pg, monkeypatch):
    from apps.api.app.routers.agents import AgentCreateBody
    from boss_engine.services import agents
    from boss_engine.services.prompt_promote import PromoteError, ask, promote

    expected = set(AgentCreateBody.model_fields) - {"project_id"}
    assert expected == CREATE_SHAPE
    real = agents.create_agent
    seen: list[tuple] = []

    def spy(connection, project_id, **kwargs):
        seen.append((project_id, kwargs))
        return real(connection, project_id, **kwargs)

    monkeypatch.setattr(agents, "create_agent", spy)
    with pg.engine.begin() as connection:
        saved = _save(connection, pg.project)
        asked = ask(connection, project_id=pg.project, prompt_id=saved["id"], actor="pm")
        recommendation = connection.execute(text(
            "SELECT recommendation FROM decisions WHERE id = :d"),
            {"d": asked["decision_id"]}).scalar()
        if isinstance(recommendation, str):
            recommendation = json.loads(recommendation)
        recommendation["agent"]["name"] = "Someone else"
        connection.execute(text(
            "UPDATE decisions SET recommendation = CAST(:r AS JSONB), status = 'approved', "
            "approved_by = 'ceo' WHERE id = :d"),
            {"r": json.dumps(recommendation), "d": asked["decision_id"]})
        with pytest.raises(PromoteError, match="no agent was created"):
            promote(connection, project_id=pg.project, prompt_id=saved["id"], actor="pm")
        assert _roles(connection, pg.project) == []
        assert seen == []

        connection.execute(text(
            "UPDATE decisions SET status = 'pending', approved_by = NULL WHERE id = :d"),
            {"d": asked["decision_id"]})
        # The tampered card is not what was asked. Ask is already open, so a fresh prompt
        # carries a clean decision.
        clean = _save(connection, pg.project, title="Release auditor",
                      body="Checks the notes against the diff.")
        asked = ask(connection, project_id=pg.project, prompt_id=clean["id"], actor="pm")
        connection.execute(text(
            "UPDATE decisions SET status = 'approved', approved_by = 'ceo', "
            "approved_at = now() WHERE id = :d"), {"d": asked["decision_id"]})
        made = promote(connection, project_id=pg.project, prompt_id=clean["id"], actor="pm")
        assert made["agent_created"] is True and made["already"] is False
        assert len(seen) == 1
        project_id, kwargs = seen[0]
        assert project_id == pg.project
        assert set(kwargs) == CREATE_SHAPE
        assert kwargs["actor"] == "ceo"
        assert kwargs["name"] == "Release auditor"
        assert kwargs["purpose"] == "Checks the notes against the diff."
        assert kwargs["role_key"] == "release_auditor"
        assert kwargs["born_from"] == {"kind": "blank"}
        assert kwargs["skills"] == []
        assert kwargs["mcp_servers"] == []
        role = connection.execute(text(
            "SELECT display_name, spec FROM roles WHERE project_id = :p AND role_key = :k"),
            {"p": pg.project, "k": "release_auditor"}).mappings().one()
        spec = role["spec"] if isinstance(role["spec"], dict) else json.loads(role["spec"])
        assert role["display_name"] == "Release auditor"
        assert spec["mission"] == "Checks the notes against the diff."
        repeat = promote(connection, project_id=pg.project, prompt_id=clean["id"], actor="pm")
        assert repeat["already"] is True
        assert repeat["role_key"] == "release_auditor"
        assert len(seen) == 1
        assert _roles(connection, pg.project) == ["release_auditor"]


def test_a_blank_prompt_is_refused(pg):
    from boss_engine.services.prompt_promote import PromoteError

    with pg.engine.begin() as connection:
        with pytest.raises(PromoteError, match="Name the prompt"):
            _save(connection, pg.project, title="  ")
        with pytest.raises(PromoteError, match="Write the prompt"):
            _save(connection, pg.project, body="")
        assert _roles(connection, pg.project) == []
