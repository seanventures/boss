"""CODE ON THIS MACHINE IS A REPOSITORY, ONLY WHERE START AGREES (stranger test, rc10.27; review
of #200).

After "Start a new app › This machine" made a local repository, Setup still said "2 to go" and
asked for GitHub. The rule now:

  · a project's OWN local repository satisfies its repository step (Start accepts it too);
  · the org's local code counts only on the install's home project with no repository row at all
    (the first-run Setup a stranger lands on);
  · any other project with no repository still says so, and an amber GitHub repository keeps
    its own reason — another project's local code never turns either into Ready.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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
    if not local_repo.git_available():
        pytest.skip("git is not installed here")
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _view(engine, project_id: str, *, home: str | None = None) -> dict:
    settings = get_settings()
    if home is not None:
        settings = settings.model_copy(update={"default_project_id": home})
    with engine.connect() as db:
        return setup_view.build(db, settings, project_id)


def _project(client, name: str) -> str:
    made = client.post("/v1/projects", json={"name": f"{name} {uuid.uuid4().hex[:5]}"})
    assert made.status_code == 201, made.text
    return made.json()["id"]


@pytest.fixture
def org_with_local_code(world, client, repo_root):
    """Two projects: one keeps its code on this machine, the other has no repository."""
    with world.acting_as(world.ctx(world.owner)):
        keeper = _project(client, "A tiny static page")
        bare = _project(client, "Setup home")
        made = client.post(f"/v1/projects/{keeper}/local-repo", json={})
        assert made.status_code == 200, made.text
    return {"keeper": keeper, "bare": bare}


def test_a_projects_own_local_repository_is_its_repository(engine, org_with_local_code):
    own = _view(engine, org_with_local_code["keeper"])
    assert own["next"] == "model" and own["verdict"]["label"] == "1 to go"


def test_another_projects_local_code_never_satisfies_a_project_with_no_repository(engine, org_with_local_code):
    view = _view(engine, org_with_local_code["bare"])
    assert view["repository"]["local"] is None
    assert view["next"] == "repository" and view["verdict"]["label"] == "2 to go"


def test_the_home_project_with_no_repository_counts_the_orgs_local_code(engine, org_with_local_code):
    home = org_with_local_code["bare"]
    view = _view(engine, home, home=home)
    local = view["repository"]["local"]
    assert local and local["project_id"] == org_with_local_code["keeper"]
    assert local["pill"]["label"] == "On this machine"
    assert view["next"] == "model" and view["verdict"]["label"] == "1 to go"


def test_an_amber_github_repository_keeps_its_reason(world, client, engine, org_with_local_code):
    with world.acting_as(world.ctx(world.owner)):
        pid = _project(client, "Amber repo")
    cid = f"ghc_{uuid.uuid4().hex[:8]}"
    checks = {"read": {"ok": None, "state": "rate_limited"}, "write": {"ok": None, "state": "rate_limited"},
              "pull_requests": {"ok": None, "state": "rate_limited"}, "state": "rate_limited",
              "retry_at": "2099-01-01T00:00:00+00:00"}
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                        "credential_ref, status, created_by) VALUES (:c, :p, 'token', "
                        "'personal_access_token', 'secret:none', 'active', 'test')"), {"c": cid, "p": pid})
        db.execute(text("INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
                        "full_name, role, status, checks, checked_at, created_by) VALUES (:i, :p, :c, 'acme', "
                        "'web', 'acme/web', 'primary', 'active', CAST(:k AS JSONB), now(), 'test')"),
                   {"i": f"ghb_{uuid.uuid4().hex[:8]}", "p": pid, "c": cid, "k": json.dumps(checks)})
    for home in (None, pid):
        view = _view(engine, pid, home=home)
        [repo] = view["repository"]["repos"]
        assert repo["pill"]["tone"] == "amber", repo["pill"]
        assert view["repository"]["local"] is None
        assert view["ready"] is False and view["next"] == "repository"
        assert view["verdict"]["tip"] == repo["pill"]["tip"]
