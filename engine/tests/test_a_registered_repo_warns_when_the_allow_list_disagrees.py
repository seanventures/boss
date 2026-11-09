"""One answer for whether a repo may be cloned.

The project binding and the connection test can stay active and passed in storage after the
org allow-list says not_allowed. Egress clones only what the allow-list allows. The status
a person reads follows that decision and carries the deny reason (repo_not_allowed).
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
    assert "repo_not_allowed" in said
    assert "registration is active" not in said
    assert "connection test passed" not in said


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


def _claims_active_or_passed(item: dict) -> bool:
    """The status a person reads: the registration fields, and a sentence that still says them."""
    if item.get("status") in ("active", "passed"):
        return True
    if item.get("last_test_status") == "passed":
        return True
    if item.get("state") in ("working", "active"):
        return True
    said = str(item.get("allow_list_warning") or "")
    return "registration is active" in said or "connection test passed" in said


def test_the_status_a_person_reads_follows_the_allow_list():
    """Active and passed in the project registration must not be what is shown when the
    allow-list egress enforces says not_allowed. The deny reason is on that same read.
    The stored rows stay as they are; only the view follows the allow-list."""
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
        c.execute(text(
            "UPDATE github_connections SET last_test_status = 'passed', status = 'active' "
            "WHERE project_id = :p"), {"p": pid})
        c.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, allowed, archived, reachable) "
            "VALUES (:i, :o, 'acme/web', false, false, true)"),
            {"i": f"ghr_{world['tag']}", "o": world["org"]})
        listed = project_repos.list_project_repos(c, pid)["items"][0]
        registered = repo_registry.list_repositories(c, pid)[0]
        binding = GitHubConnectionService(object()).list_repository_bindings(c, pid)[0]
        stored_binding = c.execute(text(
            "SELECT status FROM github_repository_bindings WHERE project_id = :p"),
            {"p": pid}).scalar()
        stored_test = c.execute(text(
            "SELECT last_test_status FROM github_connections WHERE project_id = :p"),
            {"p": pid}).scalar()
        assert registry.may_clone(c, world["org"], "acme/web") is False
        assert registry.mint_allowed(c, world["org"], ["acme/web"]) == ["acme/web"]
    assert stored_binding == "active"
    assert stored_test == "passed"
    for item in (listed, registered, binding):
        assert not _claims_active_or_passed(item), item
        reason = " ".join(str(item.get(key) or "") for key in (
            "allow_list_warning", "deny_reason", "status", "state"))
        assert "not_allowed" in reason
        assert "repo_not_allowed" in reason or item.get("deny_reason") == "repo_not_allowed"
    assert registered["status"] == "not_allowed"
    assert registered["last_test_status"] != "passed"
    assert binding["status"] == "not_allowed"
    assert listed["state"] == "not_allowed"
