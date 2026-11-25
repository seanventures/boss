"""A skill nothing can run is never held, offered, placed or approved (BOSSAPP-1366).

THE INCIDENT. On a laptop install `software_factory.sandbox_validation` was held by
backend_engineer, BOSS chose it for an API-onboarding workflow, a person was shown an approval
card listing the commands it would run — and the call returned "No server-side executor is
registered for skill …". The spec pack describes the skill (a build runs its sandbox validation
inside its own pipeline); nothing on the install can run it as a skill.

THE ONE ANSWER is `skill_executors.runs_here`, read by the catalogue: a shipped skill with no
executor is `usable: False` with a reason naming the alternative. Everything else follows from
the catalogue — the agent's tools, the publish check, the custom-step binder, the grant.
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid

import pytest
import yaml
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import agent_steps
from boss_engine.services import skill_catalogue as catalogue
from boss_engine.services import skill_definitions as sd
from boss_engine.services.skill_executors import registered_executor_ids, runs_here

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
NO_EXECUTOR = "software_factory.sandbox_validation"


def test_the_skill_from_the_incident_has_no_executor_here():
    """The premise: if this ever gains an executor, the tests below need another example."""
    assert not runs_here(NO_EXECUTOR)
    assert runs_here("repo.read")


@pytest.fixture(scope="module")
def eng():
    if not TEST_DB.startswith("postgresql"):
        pytest.skip("no test database")
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM role_skills LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'cannot-run test', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        for table in ("role_skills", "seed_exclusions", "events", "agent_identity_versions",
                      "roles"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _role(c, pid, key, allowed):
    c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, spec) "
                   "VALUES (:i, :p, :k, :k, CAST(:s AS JSONB))"),
              {"i": f"role_{uuid.uuid4().hex[:10]}", "p": pid, "k": key,
               "s": json.dumps({"permissions": {"allowed_skills": allowed}})})


def test_the_catalogue_says_it_cannot_run_and_what_to_use(eng, project):
    with eng.begin() as c:
        item = catalogue.entry(c, project, NO_EXECUTOR, settings=Settings())
        assert item is not None and item["usable"] is False
        assert "no executor" in item["reason"] and "build step" in item["reason"]
        facts = catalogue.risk_facts(item, NO_EXECUTOR)
        assert facts["usable"] is False and "cannot run" in facts["reason"]
        bound = {b["skill_id"]: b for b in catalogue.bindable(c, project, settings=Settings())}
        assert bound[NO_EXECUTOR]["usable"] is False


def test_it_cannot_be_given_to_an_agent(eng, project):
    with eng.begin() as c:
        _role(c, project, "backend_engineer", [NO_EXECUTOR, "repo.read"])
        with pytest.raises(sd.SkillDefinitionError, match="no executor"):
            sd.assign_skill_to_role(c, project_id=project, actor="test",
                                    role_key="backend_engineer", skill_id=NO_EXECUTOR)


def test_an_older_grant_is_never_offered_as_a_tool(eng, project):
    """An install that granted it before this fix still holds the row: it is unrunnable, so the
    agent is not offered it and a step scoped to it is refused at publish, saying why."""
    with eng.begin() as c:
        _role(c, project, "backend_engineer", [])
        for skill in (NO_EXECUTOR, "repo.read"):
            c.execute(text("INSERT INTO role_skills (id, project_id, role_key, skill_id, source) "
                           "VALUES (:i, :p, 'backend_engineer', :s, 'builtin')"),
                      {"i": f"rs_{uuid.uuid4().hex[:10]}", "p": project, "s": skill})
        held = agent_steps.holdings(c, Settings(), project, "backend_engineer")["backend_engineer"]
        assert NO_EXECUTOR in held["unrunnable"] and "repo.read" in held["runnable"]
        offered = {s["id"] for s in agent_steps.held_skills(c, Settings(), project,
                                                            "backend_engineer")}
        assert NO_EXECUTOR not in offered
        problem = agent_steps.scope_problem("backend_engineer", [NO_EXECUTOR], held)
        assert problem and NO_EXECUTOR in problem


def _every_role_skill() -> list[str]:
    aliases = (yaml.safe_load((ROOT / "packages/specpack/kits/aliases.yaml").read_text())
               or {}).get("aliases") or {}
    out: set[str] = set()
    for path in (ROOT / "packages/specpack/roles").glob("*.yaml"):
        spec = yaml.safe_load(path.read_text()) or {}
        for skill in (spec.get("permissions") or {}).get("allowed_skills") or []:
            skill = aliases.get(str(skill), str(skill))
            if "*" not in skill:
                out.add(skill)
    return sorted(out)


def test_every_assignable_skill_has_an_executor(eng, project):
    """Every skill any shipped role names is put through the grant, as the seed does: what is
    granted runs, and what cannot run is reported unavailable instead of granted."""
    with eng.begin() as c:
        _role(c, project, "everyone", _every_role_skill())
        report = sd.sync_role_skills(c, project_id=project, actor="test")
        granted = [r[0] for r in c.execute(text(
            "SELECT skill_id FROM role_skills WHERE project_id=:p AND source='builtin'"),
            {"p": project})]
    executors = set(registered_executor_ids())
    assert granted, report
    assert not [s for s in granted if s not in executors], granted
    unavailable = {u["skill_id"] for u in report["unavailable"]}
    assert NO_EXECUTOR in unavailable
