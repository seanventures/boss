"""CODE ON THIS MACHINE IS A REPOSITORY (stranger test, rc10.27).

After "Start a new app › This machine" made a local repository, Setup still said "2 to go" and
asked for GitHub. A local repository the org keeps (this project's, or another of its projects')
satisfies Setup's repository step; GitHub stays optional, for PRs on GitHub.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo, setup_view


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("setup-local-owner")
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


def _view(engine, project_id: str) -> dict:
    with engine.connect() as db:
        return setup_view.build(db, get_settings(), project_id)


def test_a_local_repository_satisfies_the_repository_step(world, client, engine, repo_root):
    if not local_repo.git_available():
        pytest.skip("git is not installed here")
    with world.acting_as(world.ctx(world.owner)):
        home = client.post("/v1/projects", json={"name": "Setup home"})
        other = client.post("/v1/projects", json={"name": "A tiny static page"})
        assert home.status_code == 201 and other.status_code == 201, (home.text, other.text)
        home_id, other_id = home.json()["id"], other.json()["id"]

        before = _view(engine, home_id)
        assert before["repository"]["local"] is None
        assert before["next"] == "repository"
        assert before["verdict"]["label"] == "2 to go"

        made = client.post(f"/v1/projects/{other_id}/local-repo", json={})
        assert made.status_code == 200, made.text

    after = _view(engine, home_id)
    local = after["repository"]["local"]
    assert local and local["project_id"] == other_id and local["project_name"] == "A tiny static page"
    # Only the model is left: GitHub is not asked for.
    assert after["next"] == "model"
    assert after["verdict"]["label"] == "1 to go"

    # The project that keeps the code says so about itself first.
    own = _view(engine, other_id)
    assert own["repository"]["local"] is None or own["repository"]["local"]["project_id"] == other_id
