"""FORKS (BOSSAPP-571 R18, spec §6.2, F15): BOSS uses the person's fork when they have one, can
create one with THEIR OWN GitHub connection as an explicit act, and opens the PR from it to
upstream `main` with the fix's files on the release it was made on. No connection: the collector
or a patch export — never an agent's run token."""
from __future__ import annotations

import pytest

from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import FakeGitHub, client_for, insert_verified
from ._self_fix_upstream_support import settings as _settings
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_a_fork_is_found_by_name_or_in_the_fork_list():
    assert client_for(FakeGitHub(fork="pat-example/boss")).find_fork("seanventures/boss") == \
        "pat-example/boss"
    assert client_for(FakeGitHub(fork=None)).find_fork("seanventures/boss") is None
    assert up.GitHubUpstream(None, call=FakeGitHub()).find_fork("seanventures/boss") is None


def test_the_pr_is_one_commit_on_the_release_from_the_fork():
    fake = FakeGitHub(fork="pat-example/boss")
    got = client_for(fake).open_pr(
        upstream="seanventures/boss", fork="pat-example/boss",
        branch="boss/self-fix/sfx_1-retire", base_sha="a" * 40,
        files=[("engine/boss_engine/services/stack_deploy.py", "x = 1\n"),
               ("engine/old.py", None)],
        message="fix(deploy): retire\n\nBOSS-Approved: yes\n", title="fix(deploy): retire",
        body="body")
    assert got["url"].endswith("/pull/412")
    order = [(m, p) for m, p, _ in fake.calls]
    assert order[0] == ("GET", f"/repos/pat-example/boss/git/commits/{'a' * 40}")
    assert ("POST", "/repos/pat-example/boss/git/refs") in order
    tree = next(b for m, p, b in fake.calls if p.endswith("/git/trees"))
    assert {"path": "engine/old.py", "mode": "100644", "type": "blob", "sha": None} in \
        tree["tree"], "a deleted file is deleted in the fork too"
    commit = next(b for m, p, b in fake.calls if p.endswith("/git/commits") and m == "POST")
    assert commit["parents"] == ["a" * 40] and commit["author"]["email"] == \
        "mender@boss.invalid"
    pull = next(b for m, p, b in fake.calls if p.endswith("/pulls"))
    assert pull["head"] == "pat-example:boss/self-fix/sfx_1-retire" and pull["base"] == "main"


@pytest.fixture()
def world(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.delenv("BOSS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setattr(up, "_names", lambda c, p: [])
    yield {"eng": eng, "project": project}
    clean_project(eng, project)


def test_no_fork_says_create_my_fork_and_creating_it_uses_the_persons_token(world, monkeypatch):
    fake = FakeGitHub(fork=None)
    tokens = []

    def client(c, actor):
        tokens.append(actor)
        return client_for(fake, token="the-persons-own-token")

    monkeypatch.setattr(up, "CLIENT", client)
    sfx_id = insert_verified(world["eng"], world["project"])
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_person")
    assert got["channel"] == "github_pr" and got["target"]["fork"] is None
    assert "Create my fork" in got["channel_why"]
    with pytest.raises(up.UpstreamRefused, match="create your fork"):
        with world["eng"].begin() as c:
            up.send(c, _settings(), sfx_id, got["send_id"], actor="usr_person", checked=True)
    with world["eng"].begin() as c:
        made = up.create_fork(c, _settings(), sfx_id, actor="usr_person")
    assert made == {"fork": "pat-example/boss"} and set(tokens) == {"usr_person"}
    with world["eng"].begin() as c:
        again = up.preview(c, _settings(), sfx_id, actor="usr_person")
    assert again["target"]["fork"] == "pat-example/boss"


def test_no_github_and_no_collector_means_a_patch(world, monkeypatch):
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: up.GitHubUpstream(None,
                                                                         call=FakeGitHub()))
    sfx_id = insert_verified(world["eng"], world["project"])
    with world["eng"].begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_person")
        with pytest.raises(up.UpstreamRefused, match="connect your GitHub"):
            up.create_fork(c, _settings(), sfx_id, actor="usr_person")
    assert got["channel"] == "patch_export" and "export the patch" in got["channel_why"]
