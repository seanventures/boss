"""TRY A SAMPLE KEEPS ITS CODE ON THIS MACHINE (stranger test, rc10.27).

The sheet says "Code can stay on this machine · GitHub any time", but the Hello World project came
out with no repository, and its start was then refused for one. And the sheet listed its needs one
at a time: "a connected model" first, then (after the 409) a repository and a deployment host.

  · POST /v1/projects with a sample key gives the sample a local repository (the one Start a new
    app › This machine makes), once; a second press finds it and makes nothing new.
  · GET /v1/delivery-workflows/{key}/readiness?code_location=local is the start's own check for a
    project whose code will be made here: the repository need is answered, the rest is listed
    whole, so the sheet can show every need before anything is created.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import local_repo


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("sample-local-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _bindings(engine, project_id: str) -> list[tuple[str, str, str]]:
    with engine.connect() as db:
        return [tuple(r) for r in db.execute(text(
            "SELECT host, role, full_name FROM github_repository_bindings "
            "WHERE project_id = :p AND status = 'active'"), {"p": project_id}).all()]


def test_the_sample_gets_a_local_repository_once(world, client, engine, repo_root):
    if not local_repo.git_available():
        pytest.skip("git is not installed here")
    with world.acting_as(world.ctx(world.owner)):
        first = client.post("/v1/projects", json={"name": "Hello World", "sample_key": "hello-world"})
        again = client.post("/v1/projects", json={"name": "Hello World", "sample_key": "hello-world"})
    assert first.status_code == 201 and again.status_code == 201, (first.text, again.text)
    pid = first.json()["id"]
    assert again.json()["id"] == pid
    bound = _bindings(engine, pid)
    assert len(bound) == 1, bound
    host, role, full_name = bound[0]
    assert (host, role) == ("local", "primary")
    owner, _, name = full_name.partition("/")
    assert local_repo.exists(owner, name, None)

    # The start's own check now finds the repository the sample keeps.
    with world.acting_as(world.ctx(world.owner)):
        ready = client.get(f"/v1/delivery-workflows/minor_feature/readiness?project_id={pid}")
    assert ready.status_code == 200, ready.text
    repo = [c for c in ready.json()["checked"] if c["key"] == "repository"]
    assert not repo or repo[0]["ok"] is True, repo


def test_readiness_for_local_code_answers_the_repository_and_lists_the_rest(world, client, engine):
    if not local_repo.git_available():
        pytest.skip("git is not installed here")
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "No code yet"})
        assert made.status_code == 201, made.text
        pid = made.json()["id"]
        plain = client.get(f"/v1/delivery-workflows/minor_feature/readiness?project_id={pid}")
        local = client.get(f"/v1/delivery-workflows/minor_feature/readiness?project_id={pid}"
                           "&code_location=local")
    assert plain.status_code == 200 and local.status_code == 200, (plain.text, local.text)
    keys = lambda r, field: sorted(m["key"] for m in r.json()[field])  # noqa: E731
    # The same needs are checked either way: the list is never shorter than the start's.
    assert keys(plain, "checked") == keys(local, "checked")
    if "repository" in keys(plain, "missing"):
        assert "repository" not in keys(local, "missing")
        said = next(c for c in local.json()["checked"] if c["key"] == "repository")
        assert said["ok"] is True and "this machine" in said["detail"]
    # Everything else missing is still missing.
    assert [k for k in keys(plain, "missing") if k != "repository"] == keys(local, "missing")


@pytest.mark.parametrize("failure", [local_repo.LocalRepoError("git init failed"), OSError(28, "No space left")])
def test_opening_a_sample_never_fails_on_the_disk_or_git(engine, client, repo_root, monkeypatch, failure):
    """Review of #200: an unwritable repo root, a full disk or a git failure while making the
    sample's code was a 500 on every press of Try a sample. The sample opens; it has no code
    yet, and its start's readiness names the repository need."""
    fresh = IdentityWorld.create(engine)
    owner = fresh.person(f"sample-disk-{type(failure).__name__.lower()}")
    fresh.bind(owner, "owner")

    def broken(*_a, **_k):
        raise failure

    monkeypatch.setattr(local_repo, "create", broken)
    with fresh.acting_as(fresh.ctx(owner)):
        first = client.post("/v1/projects", json={"name": "Hello World", "sample_key": "hello-world"})
        again = client.post("/v1/projects", json={"name": "Hello World", "sample_key": "hello-world"})
    assert first.status_code == 201 and again.status_code == 201, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert _bindings(engine, first.json()["id"]) == []


def test_get_started_learns_the_sample_exists(world, client, engine, repo_root):
    """Get started offers "Open the sample" once the org's sample exists, never "Start the sample"
    again (stranger test r2): /v1/setup's `try.sample` names it, from any project of the org."""
    from boss_engine.services import setup_view

    if not local_repo.git_available():
        pytest.skip("git is not installed here")
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Hello World", "sample_key": "hello-world"})
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    with engine.connect() as db:
        assert setup_view.sample_project(db, pid) == {"project_id": pid, "name": "Hello World"}
        assert setup_view.sample_project(db, world.project_a) == {"project_id": pid, "name": "Hello World"}
