"""R2 #3: a Fast fix build was refused at Build with `builder_has_no_passport`.

The default builder is named in code by its persona callsign, `forge` (consent.DEFAULT_BUILDER; the
Passport presets list callsigns too), but the agent lookup matched role keys only — and the shipped
roster has `software_engineer` (callsign Forge), no `forge` role. So the consent covered no builder,
and start_code_task_run found none. And a build step that ran as a named agent was still wired to
`forge`, outside the grant the person had just given.

THE CLASS: one resolver names the builder (runs.code_task_builder: the task's role, else the agent
that started it, else the default builder), consent and the run token both use it, and the lookup
finds an agent by role key or callsign. A builder that does not exist is refused at START under
enforce (409 agent_without_passport), never at Build.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.identity import consent, lineage, runs
from boss_engine.identity.permissions_gen import PASSPORT_PRESETS


@pytest.fixture
def project(engine) -> str:
    with engine.connect() as db:
        return db.execute(text("SELECT p.id FROM projects p ORDER BY p.created_at, p.id LIMIT 1")).scalar()


def test_every_builder_the_presets_name_resolves_to_a_shipped_agent(engine, project):
    with engine.connect() as db:
        for callsign in PASSPORT_PRESETS["builder"].agents:
            assert runs.agent_for_role(db, project, callsign), f"builder {callsign!r} has no agent here"
        forge = runs.agent_for_role(db, project, consent.DEFAULT_BUILDER)
        engineer = db.execute(text("SELECT agent_id FROM roles WHERE project_id = :p "
                                   "AND role_key = 'software_engineer'"), {"p": project}).scalar()
    assert forge and forge == engineer


def test_a_role_key_wins_over_a_callsign(engine, project):
    with engine.connect() as db:
        by_key = runs.agent_for_role(db, project, "engineering_captain")
        expected = db.execute(text("SELECT agent_id FROM roles WHERE project_id = :p "
                                   "AND role_key = 'engineering_captain'"), {"p": project}).scalar()
        assert by_key == expected
        assert runs.agent_for_role(db, project, "no_such_agent_anywhere") is None


def test_the_builder_is_the_tasks_role_else_the_agent_that_started_it_else_the_default():
    assert runs.code_task_builder({"role_key": "Frontend_Engineer"}, {"agent": {"role_key": "x"}}) \
        == "frontend_engineer"
    assert runs.code_task_builder({}, {"agent": {"role_key": "engineering_captain"}}) == "engineering_captain"
    assert runs.code_task_builder({"role_key": ""}, {}) is None
    assert runs.code_task_builder(None, None) is None


def _person_ctx(engine, project):
    from apps.api.tests.identity_fixtures import IdentityWorld

    with engine.connect() as db:
        org = db.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                              "WHERE p.id = :p"), {"p": project}).scalar()
    world = IdentityWorld(engine, org, "ws-unused", project, project)
    owner = world.person("r2-builder-owner")
    world.bind(owner, "owner")
    return world.ctx(owner)


def test_a_person_starting_a_build_is_asked_to_consent_for_the_builder_that_will_run(
        engine, project, monkeypatch):
    """The consent names the default builder's agent (it used to name none: the build then started
    with no grant behind its builder and died at Build)."""
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    ctx = _person_ctx(engine, project)
    with engine.connect() as db:
        engineer = db.execute(text("SELECT agent_id FROM roles WHERE project_id = :p "
                                   "AND role_key = 'software_engineer'"), {"p": project}).scalar()
    with lineage.acting(ctx), engine.begin() as db:
        with pytest.raises(consent.ConsentRequired) as asked:
            consent.require_authority(org_id=None, project_id=project, role_keys=[consent.DEFAULT_BUILDER],
                                      permissions=consent.CODE_TASK_PERMISSIONS, repos=["o/r"],
                                      task="r2 builder", run_id="run_ct:r2", db=db)
    assert [a["id"] for a in asked.value.body["agents"]] == [engineer]


def test_a_builder_that_does_not_exist_is_refused_at_start_under_enforce(engine, project, monkeypatch):
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    ctx = _person_ctx(engine, project)
    with lineage.acting(ctx), engine.begin() as db:
        with pytest.raises(consent.AgentWithoutPassport):
            consent.require_authority(org_id=None, project_id=project, role_keys=["no_such_builder"],
                                      permissions=consent.CODE_TASK_PERMISSIONS, repos=["o/r"],
                                      task="r2 builder", run_id="run_ct:r2b", db=db)
