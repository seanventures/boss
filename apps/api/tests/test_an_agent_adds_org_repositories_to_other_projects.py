"""From a conversation in one project, the agent gives OTHER projects repositories from the org's
registry — the console's path, with the caller's own permission in the project it acts in.

The exam (2026-10-24): asked about northpeak-bank-a/-ap/-b from boss-dogfood, the agent said their
repositories were "blocked, and not something I can fix from here": list_github_connections (per
project) returned 0 and register_repository demanded a connection_id. The org's one GitHub App
connection already reached all three repositories, listed in the org registry. Now:
list_org_repositories → allow_repository (a person presses it) → register_repository (owner/name)
→ remove_project_repository, through the same services as the console's routes. No project
connection exists beforehand; the App's is made on the way, as the console does. Real DB, real
authz; GitHub is never called (the registry prober and the repository check are stand-ins).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.context import EngineContext
from boss_engine.identity import github_setup
from boss_engine.multirepo import registry as mr_registry
from boss_engine.services import setup_actions
from boss_engine.tools import build_discuss_read_registry

OK = {"ok": True, "state": "working"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(autouse=True)
def github(monkeypatch, world, engine):
    """The org's GitHub App is connected and its registry lists two repositories (one allowed);
    nothing reaches GitHub."""
    with engine.begin() as db:
        for rid, name, allowed in (("ghr_xp_fineract_b", "acme/northpeak-fineract-b", False),
                                   ("ghr_xp_sandbox", "acme/sandbox", True)):
            db.execute(text("INSERT INTO github_repos (id, org_id, full_name, allowed, checks, checked_at) "
                            "VALUES (:i, :o, :f, :a, CAST(:c AS JSONB), now()) ON CONFLICT DO NOTHING"),
                       {"i": rid, "o": world.org, "f": name, "a": allowed,
                        "c": json.dumps({"read": OK, "write": OK, "pull_requests": OK}) if allowed else "{}"})
    monkeypatch.setattr(mr_registry, "default_prober", lambda db, settings, org_id, project_id, full_name: {
        "state": "working", "checks": {"read": OK, "write": dict(OK), "pull_requests": dict(OK)}})
    monkeypatch.setattr(github_setup, "app_ready", lambda db, org_id=None: True)
    monkeypatch.setattr(github_setup, "app_config", lambda db, org_id=None: {
        "app_id": "1", "installation_id": "2", "client_id": None, "slug": "boss-test", "html_url": None,
        "account_login": "acme", "account_type": "Organization", "owner_login": "acme",
        "owner_type": "Organization", "verified": True, "verified_at": None, "source": "setup"})
    monkeypatch.setattr(setup_actions, "check_repository", lambda *a, **k: {"state": "working"})


@pytest.fixture
def enforced():
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    yield
    settings.authz_mode, settings.authz_router_modes = saved


def _call(engine, world, auth, tool, *, confirmed=False, **args):
    registry = build_discuss_read_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    return registry.invoke(tool, ctx, project_id=world.project_a, actor="test", args=args,
                           auth=auth, operator_confirmed=confirmed)


def _bindings(engine, project_id):
    with engine.connect() as db:
        return db.execute(text("SELECT full_name, default_branch, registry_repo_id, status, connection_id "
                               "FROM github_repository_bindings WHERE project_id = :p ORDER BY full_name"),
                          {"p": project_id}).mappings().all()


def test_the_agent_allows_and_adds_an_org_repository_to_another_project(world, engine, enforced):
    owner = world.person("xrepo-owner")
    world.bind(owner, "owner")
    me = world.ctx(owner)
    target = world.project_b

    # What the exam's agent never saw: the org registry, and the org's one connection.
    conns = _call(engine, world, me, "list_github_connections", project=target)
    assert conns["count"] == 0 and conns["org_connection"]["kind"] == "app", conns
    listed = _call(engine, world, me, "list_org_repositories", project=target)
    assert listed["ok"] is True and listed["connection"]["kind"] == "app", listed
    by_name = {i["full_name"]: i for i in listed["items"]}
    assert by_name["acme/northpeak-fineract-b"]["allowed"] is False
    assert by_name["acme/northpeak-fineract-b"]["state"] == "not_allowed"
    assert _call(engine, world, me, "describe_project", project=target)["note"].count("list_org_repositories")

    refused = _call(engine, world, me, "register_repository", project=target,
                    repository="acme/northpeak-fineract-b")
    assert refused["ok"] is False and refused["error"] == "not_allowed", refused

    # Allowing is a grant: offered, and run only by a person's press.
    held = _call(engine, world, me, "allow_repository", project=target,
                 repository="acme/northpeak-fineract-b")
    assert held["status"] == "confirmation_required", held
    allowed = _call(engine, world, me, "allow_repository", confirmed=True, project=target,
                    repository="acme/northpeak-fineract-b")
    assert allowed["ok"] is True and allowed["repository"]["state"] == "working", allowed

    added = _call(engine, world, me, "register_repository", project=target,
                  repository="acme/northpeak-fineract-b", base_branch="develop")
    assert added["ok"] is True, added
    assert [r["full_name"] for r in added["repositories"]] == ["acme/northpeak-fineract-b"]
    rows = _bindings(engine, target)
    assert [(r["full_name"], r["default_branch"], r["registry_repo_id"], r["status"]) for r in rows] == \
        [("acme/northpeak-fineract-b", "develop", "ghr_xp_fineract_b", "active")]
    assert _bindings(engine, world.project_a) == [], "added where it was asked, not the conversation's"
    with engine.connect() as db:
        conn = db.execute(text("SELECT credential_ref FROM github_connections WHERE id = :c"),
                          {"c": rows[0]["connection_id"]}).scalar()
        audited = db.execute(text("SELECT count(*) FROM identity_security_events WHERE event_type = "
                                  "'repository.allowed' AND target_id = 'ghr_xp_fineract_b'")).scalar()
    assert conn == setup_actions.APP_CREDENTIAL_REF, "the org's App connection, not a pasted token"
    assert audited == 1

    state = _call(engine, world, me, "describe_project", project=target)
    assert [r["full_name"] for r in state["repositories"]] == ["acme/northpeak-fineract-b"]

    removed = _call(engine, world, me, "remove_project_repository", project=target,
                    repository="acme/northpeak-fineract-b")
    assert removed["ok"] is True and removed["repositories"] == [], removed
    assert [r["status"] for r in _bindings(engine, target)] != ["active"]


def test_a_caller_without_the_permission_there_is_refused_and_nothing_changes(world, engine, enforced):
    """An operator in the target project may read the registry, not allow or add."""
    member = world.person("xrepo-operator")
    world.bind(member, "operator", project=world.project_b)
    world.bind(member, "owner", project=world.project_a)
    me = world.ctx(member)
    assert _call(engine, world, me, "list_org_repositories", project=world.project_b)["ok"] is True
    for tool, confirmed in (("register_repository", False), ("allow_repository", True)):
        out = _call(engine, world, me, tool, confirmed=confirmed, project=world.project_b,
                    repository="acme/sandbox")
        assert out["ok"] is False and out["error"] == "forbidden", (tool, out)
    refresh = _call(engine, world, me, "list_org_repositories", project=world.project_b, refresh=True)
    assert refresh["ok"] is False and refresh["error"] == "forbidden", refresh
    assert [r for r in _bindings(engine, world.project_b) if r["full_name"] == "acme/sandbox"] == []
