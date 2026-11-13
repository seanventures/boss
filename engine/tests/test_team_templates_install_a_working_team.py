"""BOSSAPP-894: a team template installs roles, reporting lines (read by org_chain) and starter
work in one governed apply after a dry run, and drift says what changed since.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from ._ops2_support import fresh_engine, make_project
from boss_engine.config import get_settings
from boss_engine.services import org_chain, team_templates as tt, work_items
from boss_engine.services.specpack import SpecPackService

PACK = "delivery_team_pack"


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("teams")
    yield engine
    drop()


@pytest.fixture()
def project(eng):
    return make_project(eng, roles=())


def _plan(eng, project, manager="person:ceo"):
    return tt.plan(eng, get_settings(), project_id=project, pack_id=PACK, manager=manager,
                   actor="ceo")


def _apply(eng, project, plan, manager="person:ceo"):
    return tt.apply(eng, get_settings(), project_id=project, pack_id=PACK, manager=manager,
                    actor="ceo", plan_digest=plan["plan_digest"])


def _starters(eng, project):
    with eng.begin() as c:
        return {r["subject_id"]: dict(r) for r in c.execute(
            text("SELECT id, subject_id, title, assignee, parent_id FROM work_items "
                 "WHERE project_id = :p AND subject_type = 'pack_starter'"),
            {"p": project}).mappings()}


def test_the_dry_run_writes_nothing_and_says_what_it_would_do(eng, project):
    plan = _plan(eng, project)
    assert plan["error"] is None
    assert plan["summary"] == {"role": {"create": 4}, "org": {"create": 4},
                               "task": {"create": 3}}
    with eng.begin() as c:
        for table in ("roles", "org_assignments", "work_items", "pack_installs",
                      "pack_provenance"):
            assert c.execute(text(f"SELECT count(*) FROM {table} WHERE project_id = :p"),
                             {"p": project}).scalar() == 0, table


def test_apply_installs_a_working_team_under_the_chosen_manager(eng, project):
    applied = _apply(eng, project, _plan(eng, project))
    assert not applied["dry_run"]
    with eng.begin() as c:
        chain = [r["party"] for r in org_chain.chain_of_command(c, project, "agent:qa")]
        captain = c.execute(text("SELECT reports_to, display_name FROM org_assignments "
                                 "WHERE project_id = :p AND agent_id = 'engineering_captain'"),
                            {"p": project}).one()
    assert chain == ["agent:qa", "agent:engineering_captain", "person:ceo"]
    assert tuple(captain) == ("person:ceo", "Delivery lead")
    starters = _starters(eng, project)
    assert set(starters) == {f"{PACK}:map-the-repo", f"{PACK}:ci-baseline",
                             f"{PACK}:release-checklist"}
    assert starters[f"{PACK}:ci-baseline"]["assignee"] == "agent:qa"
    assert starters[f"{PACK}:ci-baseline"]["parent_id"] == starters[f"{PACK}:map-the-repo"]["id"]
    # Applying again is a no-op: nothing duplicated, everything unchanged.
    again = _plan(eng, project)
    assert again["summary"] == {"role": {"unchanged": 4}, "org": {"unchanged": 4},
                                "task": {"unchanged": 3}}
    _apply(eng, project, again)
    assert len(_starters(eng, project)) == 3


def test_a_project_that_moved_since_the_dry_run_refuses_the_apply(eng, project):
    plan = _plan(eng, project)
    from boss_engine.services import org_assignments

    with eng.begin() as c:
        org_assignments.set_one(c, project, "qa", reports_to="person:someone", actor="ceo")
    with pytest.raises(tt.PlanStale):
        _apply(eng, project, plan)
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM work_items WHERE project_id = :p"),
                         {"p": project}).scalar() == 0


def test_drift_names_what_changed_and_what_is_gone(eng, project):
    _apply(eng, project, _plan(eng, project))
    with eng.begin() as c:
        report = tt.drift(c, project, PACK)
        assert report["applied"] and report["drifted"] == 0
        from boss_engine.services import org_assignments

        org_assignments.set_one(c, project, "qa", reports_to="release_manager", actor="ceo")
        item = _starters(eng, project)[f"{PACK}:release-checklist"]["id"]
    with eng.begin() as c:
        work_items.update_item(c, None, project_id=project, item_id=item,
                               fields={"title": "Release checklist v2"}, actor="ceo")
        doomed = _starters(eng, project)[f"{PACK}:ci-baseline"]["id"]
        work_items.delete_item(c, project_id=project, item_id=doomed, actor="ceo")
    with eng.begin() as c:
        report = tt.drift(c, project, PACK)
    status = {(i["kind"], i["key"]): i["status"] for i in report["items"]}
    assert status[("org", "qa")] == "modified"
    assert status[("task", "release-checklist")] == "modified"
    assert status[("task", "ci-baseline")] == "missing"
    assert status[("org", "backend_engineer")] == "in_sync"
    assert report["drifted"] == 3


def test_an_unknown_manager_is_refused_in_words(eng, project):
    with pytest.raises(tt.TeamTemplateError, match="not an agent in this project"):
        _plan(eng, project, manager="nobody_here")


def test_the_team_sections_are_validated_with_the_pack():
    spec = {"id": "t", "version": "1", "description": "t",
            "installs": {"roles": ["qa", "pm"]},
            "org": [{"role": "qa", "reports_to": "pm"}, {"role": "pm", "reports_to": "qa"},
                    {"role": "ghost"}],
            "starter_tasks": [{"key": "a", "title": "A", "assignee": "cto", "parent": "b"}]}
    problems = " | ".join(tt.validate_team(spec))
    for words in ("reports to itself", "'ghost' is not one of the roles",
                  "assignee 'cto'", "parent 'b' must be an earlier"):
        assert words in problems, words
    with pytest.raises(ValueError, match="invalid"):
        SpecPackService(get_settings()).validate_pack_spec(spec)
    # A pack with no team sections validates exactly as before.
    SpecPackService(get_settings()).validate_pack_spec(
        {"id": "p", "version": "1", "description": "p", "installs": {"roles": ["qa"]}})
