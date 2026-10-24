"""From a conversation in one project, the agent reads and sets up OTHER projects the caller may.

The banking case study's exam (2026-10-24): asked from boss-dogfood for the state of three other
projects, the agent had no tool that could name another project. Now every project tool takes
`project` (an id or slug), resolved in the caller's org and decided against the caller's own
permission in THAT project, and `describe_project` / `list_projects` report the real state —
the repositories registered and every setting with its accepted values. Real database, real authz.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.tools import build_discuss_read_registry


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def enforced():
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    yield
    settings.authz_mode, settings.authz_router_modes = saved


def _call(engine, world, auth, tool, **args):
    registry = build_discuss_read_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    return registry.invoke(tool, ctx, project_id=world.project_a, actor="test", args=args,
                           auth=auth)


def test_the_agent_reports_and_changes_another_projects_state(world, engine, enforced):
    owner = world.person("xproj-owner")
    world.bind(owner, "owner")
    me = world.ctx(owner)

    listed = _call(engine, world, me, "list_projects")
    assert {world.project_a, world.project_b} <= {p["id"] for p in listed["projects"]}, listed

    state = _call(engine, world, me, "describe_project", project=world.project_b)
    assert state["ok"] is True and state["project"]["id"] == world.project_b, state
    assert state["repositories"] == [] and state["repository_count"] == 0
    kg = next(s for s in state["settings"] if s["key"] == "knowledge_graph")
    assert kg["stored"] is None and kg["source"] == "deployment default"
    assert kg["allowed"] == "on|keyword|off"

    changed = _call(engine, world, me, "set_project_setting", project=world.project_b,
                    key="knowledge_graph", value="keyword")
    assert changed["ok"] is True, changed
    with engine.connect() as db:
        rows = db.execute(text("SELECT project_id, value FROM project_settings "
                               "WHERE key = 'knowledge_graph' AND project_id IN (:a, :b)"),
                          {"a": world.project_a, "b": world.project_b}).all()
    assert [(p, str(v).strip('"')) for p, v in rows] == [(world.project_b, "keyword")], \
        "set in the named project, not the conversation's"
    again = _call(engine, world, me, "describe_project", project=world.project_b)
    assert next(s for s in again["settings"] if s["key"] == "knowledge_graph")["effective"] \
        in ("keyword", '"keyword"')

    missing = _call(engine, world, me, "describe_project", project="northpeak-bank-nope")
    assert missing["ok"] is False and missing["error"] == "project_not_found"
    assert world.project_b in missing["projects_in_this_organization"]


def test_a_caller_without_the_permission_there_is_refused_there(world, engine, enforced):
    """Held in project A only: project A answers, project B refuses, and B's settings are
    untouched — the session project is no key to another one."""
    member = world.person("xproj-a-only")
    world.bind(member, "operator", project=world.project_a)
    me = world.ctx(member)
    assert _call(engine, world, me, "describe_project")["ok"] is True
    for tool, args in (("describe_project", {}),
                       ("set_project_setting", {"key": "knowledge_graph", "value": "off"})):
        out = _call(engine, world, me, tool, project=world.project_b, **args)
        assert out["ok"] is False and out["error"] == "forbidden", (tool, out)
    listed = _call(engine, world, me, "list_projects")
    assert world.project_b not in {p["id"] for p in listed["projects"]}, listed
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM project_settings WHERE project_id = :b "
                               "AND key = 'knowledge_graph' AND value::text LIKE '%off%'"),
                          {"b": world.project_b}).scalar() == 0
