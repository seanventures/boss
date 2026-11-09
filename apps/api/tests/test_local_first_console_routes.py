"""THE LOCAL-FIRST CONSOLE'S OWN ROUTES (BOSSAPP-916/917/921/923), against the real database and
real git, GitHub being the plumbing gate's fake (tests/e2e/gate/remote.py):

  * GET  /v1/projects/{id}/local-folders            Bring existing code › This machine
  * GET  /v1/projects/{id}/github-repository/check  "can BOSS make it?", in the engine's words
  * POST /v1/add-project {new_repo}                 New GitHub repo, made by BOSS at Create
  * publish to another repository after the first one refused (the Publish sheet's way out)
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo, transient_retry

_GATE = Path(__file__).resolve().parents[3] / "tests" / "e2e" / "gate"
if str(_GATE) not in sys.path:
    sys.path.insert(0, str(_GATE))
from remote import LocalGitHub  # noqa: E402  (tests/e2e/gate/remote.py)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("lfc-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("lfc-viewer")
    w.bind(w.viewer, "viewer", project=w.project_a)
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    monkeypatch.setattr(transient_retry, "_sleep", lambda _s: None)
    return root


@pytest.fixture
def hub(tmp_path):
    github = LocalGitHub(tmp_path / "github").start()
    yield github
    github.stop()


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def _git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x.test", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          env=env).stdout.strip()


# ── Bring existing code › This machine ──────────────────────────────────────────────────────

def test_the_import_directory_lists_its_folders_with_their_facts(client, world, tmp_path, monkeypatch):
    root = tmp_path / "import"
    (root / "notes" / "src").mkdir(parents=True)
    (root / "notes" / "src" / "a.js").write_text("x\n")
    (root / "notes" / "package.json").write_text('{"name":"notes"}\n')
    (root / "notes" / "node_modules" / "dep").mkdir(parents=True)
    (root / "notes" / "node_modules" / "dep" / "i.js").write_text("y\n")
    _git(root / "notes", "init", "-q", "-b", "main")
    _git(root / "notes", "add", "-A")
    _git(root / "notes", "commit", "-q", "-m", "first")
    _git(root / "notes", "remote", "add", "origin", "https://tok@github.com/acme/notes.git")
    (root / "plain").mkdir()
    (root / "plain" / "index.html").write_text("<h1>hi</h1>\n")
    (root / ".hidden").mkdir()
    from apps.api.app.routers import local_projects as router
    monkeypatch.setattr(router._settings, "local_import_root", str(root), raising=False)
    with world.acting_as(world.ctx(world.viewer)):
        r = client.get(f"/v1/projects/{world.project_a}/local-folders")
    assert r.status_code == 200, r.text
    body = r.json()
    by = {i["name"]: i for i in body["items"]}
    assert body["configured"] and set(by) == {"notes", "plain"}, "hidden folders are not offered"
    assert by["notes"]["git"] and by["notes"]["github"] == "acme/notes" and by["notes"]["branch"] == "main"
    assert by["notes"]["files"] == 2, "dependencies are not counted"
    assert "src" in by["notes"]["top"] and by["notes"]["stack"], "read like a server's folder: its stack"
    assert not by["plain"]["git"] and by["plain"]["github"] is None and by["plain"]["files"] == 1
    assert "tok@" not in json.dumps(body), "a credential in a remote is never shown"
    with world.acting_as(world.ctx(world.viewer)):
        one = client.get(f"/v1/projects/{world.project_a}/local-folders", params={"q": "pla"}).json()
    assert [i["name"] for i in one["items"]] == ["plain"]
    monkeypatch.setattr(router._settings, "local_import_root", "", raising=False)
    with world.acting_as(world.ctx(world.viewer)):
        off = client.get(f"/v1/projects/{world.project_a}/local-folders").json()
    assert off["configured"] is False and off["items"] == [] and "off" in off["words"]


# ── can BOSS make the repository? ───────────────────────────────────────────────────────────

def _api(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_can_boss_make_the_repo_is_said_in_the_engine_words():
    from boss_engine.services.local_publish import check_repository

    def org(request):
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        return httpx.Response(404, json={"message": "Not Found"})

    out = check_repository(owner="acme", name="new", token="t", api_base_url="http://gh", client=_api(org))
    assert out["ok"] and out["owner_type"] == "Organization" and "free" in out["words"]

    def personal(request):
        if request.url.path == "/users/me":
            return httpx.Response(200, json={"type": "User"})
        return httpx.Response(404, json={"message": "Not Found"})

    out = check_repository(owner="me", name="new", token="t", api_base_url="http://gh", client=_api(personal))
    assert not out["ok"] and out["reason"] == "personal_account" and "personal" in out["words"]
    out = check_repository(owner="me", name="new", token="t", token_kind="user", api_base_url="http://gh",
                           client=_api(personal))
    assert out["ok"], "a person's own token can create in their account"

    def exists(request):
        return httpx.Response(200, json={"private": True})

    out = check_repository(owner="acme", name="new", token="t", api_base_url="http://gh", client=_api(exists))
    assert not out["ok"] and out["reason"] == "exists" and out["exists"]
    out = check_repository(owner="acme", name="new", token="t", purpose="existing", api_base_url="http://gh",
                           client=_api(exists))
    assert out["ok"] and "empty" in out["words"]
    out = check_repository(owner="acme", name="new", token="t", purpose="existing", api_base_url="http://gh",
                           client=_api(org))
    assert not out["ok"] and out["reason"] == "not_found"

    out = check_repository(owner="acme", name="new", token=None)
    assert not out["ok"] and out["reason"] == "no_github_credential" and "Administration" in out["words"]
    out = check_repository(owner="-bad-", name="x y", token="t")
    assert not out["ok"] and out["reason"] == "not_allowed"

    def down(request):
        return httpx.Response(502, json={"message": "bad gateway"})

    out = check_repository(owner="acme", name="new", token="t", api_base_url="http://gh", client=_api(down))
    assert not out["ok"] and out["reason"] == "failed" and "try again" in out["words"]


def test_the_check_route_is_a_read(client, world, monkeypatch):
    from boss_engine.services import local_publish

    monkeypatch.setattr(local_publish, "default_reach", lambda *a, **k: local_publish.GitHubReach(
        adapter_for=None, access_for=None, create_token=None, create_token_kind="app",
        api_base_url="http://gh", connection_id=None))
    with world.acting_as(world.ctx(world.viewer)):
        r = client.get(f"/v1/projects/{world.project_a}/github-repository/check",
                       params={"owner": "acme", "repo": "shop"})
    assert r.status_code == 200 and r.json()["reason"] == "no_github_credential", r.text


# ── New GitHub repo, made by BOSS at Create and start (917) ────────────────────────────────

def _fake_reach(hub, connection_id, token_kind="app"):
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.local_publish import GitHubReach
    from boss_engine.services.repo_host import GitAccess

    settings = get_settings()
    return GitHubReach(
        adapter_for=lambda o, r: GitHubAdapter(settings, owner=o, repo=r, token="gate-token", api_base_url=hub.api_url),
        access_for=lambda o, r: GitAccess(hub.git_url(o, r), via="token"),
        create_token="gate-token", create_token_kind=token_kind, api_base_url=hub.api_url,
        connection_id=connection_id)


def _connection(engine, world) -> str:
    cid = f"ghconn_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
                        "credential_ref, status, created_by) VALUES (:i, :p, 'Test', "
                        "'personal_access_token', 'env:BOSS_TEST_GH', 'active', 't')"),
                   {"i": cid, "p": world.project_a})
    return cid


def test_create_and_start_makes_the_github_repo_and_switches_to_it(client, world, engine, repo_root, hub, monkeypatch):
    from boss_engine.services import local_publish

    cid = _connection(engine, world)
    monkeypatch.setattr(local_publish, "default_reach", lambda *a, **k: _fake_reach(hub, cid))
    # The fake GitHub's org: its repositories exist only once made (BOSS makes this one).
    hub.remotes.fresh_owners.add("acme")
    name = f"Bakery {uuid.uuid4().hex[:6]}"
    repo = f"bakery-{uuid.uuid4().hex[:5]}"
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "idea", "name": name,
            "words": "A bakery site with the week's bread.", "code_location": "github",
            "new_repo": {"owner": "acme", "repo": repo, "private": True},
            "start_anyway": ["repository", "model", "deployment_target"]})
    said = r.json()
    pid = said.get("project_id")
    assert pid, said
    with engine.connect() as db:
        rows = {row["host"]: dict(row) for row in db.execute(text(
            "SELECT host, role, full_name FROM github_repository_bindings WHERE project_id = :p "
            "AND status = 'active'"), {"p": pid}).mappings()}
    assert rows["github"]["full_name"] == f"acme/{repo}" and rows["github"]["role"] == "primary"
    assert rows["local"]["role"] == "mirror", "the first commit was made here, then published"
    bare = hub.remotes.root / "acme" / f"{repo}.git"
    assert _git(bare, "log", "--format=%s", "main") == "Initial commit (BOSS)"
    if r.status_code == 200:
        assert said["publish"]["ok"] and said["publish"]["full_name"] == f"acme/{repo}"
        assert any(s["key"] == "github" and s["ok"] for s in said["steps"])


def test_a_refused_new_repo_keeps_the_project_local_and_says_why(client, world, engine, repo_root, hub, monkeypatch):
    from boss_engine.services import local_publish

    cid = _connection(engine, world)
    monkeypatch.setattr(local_publish, "default_reach", lambda *a, **k: _fake_reach(hub, cid))
    hub.remotes.fresh_owners.add("someone")
    hub.remotes.owner_types["someone"] = "User"          # a personal account: an App cannot create there
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "idea", "name": f"Kiosk {uuid.uuid4().hex[:6]}",
            "words": "A kiosk menu.", "code_location": "github",
            "new_repo": {"owner": "someone", "repo": "kiosk", "private": True},
            "start_anyway": ["repository", "model", "deployment_target"]})
    said = r.json()
    pid = said["project_id"]
    with engine.connect() as db:
        host = db.execute(text("SELECT host FROM github_repository_bindings WHERE project_id = :p AND "
                               "role = 'primary' AND status = 'active'"), {"p": pid}).scalar()
    assert host == "local", "the project builds on this machine meanwhile"
    if r.status_code == 200:
        assert said["publish"]["ok"] is False and said["publish"]["error"] == "personal_account"
        assert "personal" in said["publish"]["message"]


def test_a_new_repo_is_only_for_a_new_app_and_only_a_person_makes_it(client, world, engine):
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "repo", "name": "X", "repo_ids": ["r1"],
            "new_repo": {"owner": "acme", "repo": "x"}})
    assert r.status_code == 422 and r.json()["error"] == "new_repo_for_a_new_app"


# ── Publish: another repository after the first refused (the sheet's way out) ───────────────

def test_publish_to_another_repo_after_the_first_refused(engine, world, repo_root, hub):
    from boss_engine.services import local_projects, local_publish

    settings = get_settings()
    pid = f"lfc-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"), {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :ws, :n, :id)"),
                   {"id": pid, "ws": ws, "n": f"Pub {pid[-4:]}"})
        local_projects.create_for_project(db, settings, pid, name="shop", actor="t")
    cid = _connection(engine, world)
    hub.seed("acme", "taken", {"README.md": "someone else's\n"})
    with engine.begin() as db:
        with pytest.raises(local_publish.PublishError) as refused:
            local_publish.publish(db, settings, pid, owner="acme", repo="taken", create=False, actor="p",
                                  reach=_fake_reach(hub, cid))
    assert refused.value.error == "target_not_empty"
    hub.remotes.fresh_owners.add("acme")
    with engine.begin() as db:
        done = local_publish.publish(db, settings, pid, owner="acme", repo="shop-new", actor="p",
                                     reach=_fake_reach(hub, cid))
    assert done["status"] == "published" and done["full_name"] == "acme/shop-new"
    assert _git(hub.remotes.root / "acme" / "shop-new.git", "log", "--format=%s", "main") == "Initial commit (BOSS)"
