"""One answer for whether a repo may be cloned, and a warning when the records disagree.

The project binding can stay active and the connection test can stay passed after the org
allow-list says not_allowed. Egress clones only what the allow-list allows, so the build
dies in seconds with repo_not_allowed and nobody was told the two records disagree.
"""
from __future__ import annotations

from boss_engine.multirepo import registry


def test_may_clone_is_mint_allowed(monkeypatch):
    seen: list[list[str]] = []

    def fake(_db, _org, repos):
        seen.append(list(repos))
        return ["Acme/Web"]

    monkeypatch.setattr(registry, "mint_allowed", fake)
    assert registry.may_clone(object(), "org_1", "acme/web") is False
    assert seen == [["acme/web"]]

    def unused(_db, _org, _repos):
        return None

    monkeypatch.setattr(registry, "mint_allowed", unused)
    assert registry.may_clone(object(), "org_1", "acme/web") is None


def test_an_active_passed_registration_warns_when_the_allow_list_says_not_allowed():
    said = registry.registration_disagreement(
        binding_status="active", connection_status="active", last_test_status="passed",
        may_clone=False, reason="not_allowed", full_name="acme/web")
    assert said
    assert "not_allowed" in said
    assert "passed" in said
    assert "active" in said
    assert "repo_not_allowed" in said


def test_no_warning_when_the_records_agree_or_the_registry_is_unused():
    agreed = dict(binding_status="active", connection_status="active",
                  last_test_status="passed", may_clone=True, reason=None,
                  full_name="acme/web")
    unused = {**agreed, "may_clone": None}
    assert registry.registration_disagreement(**agreed) is None
    assert registry.registration_disagreement(**unused) is None
    refused_but_not_the_allow_list = {**agreed, "may_clone": False, "reason": "archived"}
    assert registry.registration_disagreement(**refused_but_not_the_allow_list) is None


def test_the_lists_a_person_reads_warn_when_the_records_disagree():
    """The binding stays active and the connection test stays passed; the allow-list does not."""
    from sqlalchemy import text

    from boss_engine.multirepo import project_repos
    from boss_engine.services import repo_registry
    from boss_engine.services.github_connections import GitHubConnectionService

    from ._multirepo_support import bind, make_engine, make_world

    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    bind(engine, world, pid, "acme/web", role="primary")
    with engine.begin() as c:
        c.execute(text("UPDATE github_connections SET last_test_status = 'passed', status = 'active' "
                       "WHERE project_id = :p"), {"p": pid})
        before = project_repos.list_project_repos(c, pid)
        assert before["items"][0]["full_name"] == "acme/web"
        assert before["items"][0].get("allow_list_warning") is None
        c.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, allowed, archived, reachable) "
            "VALUES (:i, :o, 'acme/web', false, false, true)"),
            {"i": f"ghr_{world['tag']}", "o": world["org"]})
        listed = project_repos.list_project_repos(c, pid)
        registered = repo_registry.list_repositories(c, pid)
        bindings = GitHubConnectionService(object()).list_repository_bindings(c, pid)
        assert registry.may_clone(c, world["org"], "acme/web") is False
    warning = listed["items"][0]["allow_list_warning"]
    assert warning
    assert "not_allowed" in warning and "repo_not_allowed" in warning
    assert listed["items"][0]["clone_allowed"] is False
    assert registered[0]["allow_list_warning"] == warning
    assert bindings[0]["allow_list_warning"] == warning
