"""A LOCAL PROJECT IN EVERY VIEW (BOSSAPP-915 follow-up).

A local project's repository is a binding with host 'local' and no GitHub connection row; its
pull requests are local change sets at http://boss-local/local_<project>/<name>/pull/<n>. Views
that joined the connections table hid it, and links built https://github.com/local_… pages that
do not exist. Each API read the console uses is asked here about one local project: it must show
the repository, never link it to GitHub, and open its change set in BOSS (/changes/<p>/<n>).
GitHub is off for the whole test, and no read may try it.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.services import local_repo
from boss_engine.services.local_repo_host import change_set_url


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("lfv-owner")
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


@pytest.fixture
def github_off(monkeypatch):
    calls: list[str] = []
    real = httpx.Client.send

    def send(self, request, *args, **kwargs):
        if "github" in str(request.url.host):
            calls.append(f"{request.method} {request.url}")
            raise httpx.ConnectError("GitHub is off in this test", request=request)
        return real(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    return calls


@pytest.fixture
def local_project(engine, world, client, repo_root, github_off):
    pid = f"lfv-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Corner {pid[-4:]}"})
    with world.acting_as(world.ctx(world.owner, mfa=True)):
        made = client.post(f"/v1/projects/{pid}/local-repo", json={"name": "Corner Shop"})
    assert made.status_code in (200, 201), made.text
    full = f"local_{pid}/corner-shop"
    url = change_set_url(full, 1)
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        assert db.execute(text("SELECT host FROM github_repository_bindings WHERE project_id = :p"),
                          {"p": pid}).scalar() == "local"
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
            "pull_request_url, pull_request_number) VALUES (:id, :p, 'Add a basket', 'completed', "
            "'done', CAST(:s AS JSONB), :u, 1)"),
            {"id": run_id, "p": pid, "u": url,
             "s": json.dumps({"owner": f"local_{pid}", "repo": "corner-shop"})})
    return {"id": pid, "full_name": full, "url": url, "run_id": run_id,
            "view": f"/changes/{pid}/1", "calls": github_off}


def _never_github(body: str, full: str) -> None:
    assert f"github.com/{full}" not in body, "a local repository linked to GitHub"
    assert "github.com/local_" not in body


def test_a_local_project_appears_in_every_api_read_the_console_uses(world, client, local_project):
    p, full = local_project["id"], local_project["full_name"]
    reads = {
        "projects list": ("/v1/projects/summary", {}),
        "project header (overview)": (f"/v1/projects/{p}/overview", {}),
        "Repo & knowledge": (f"/v1/projects/{p}/knowledge", {}),
        "Deployments": (f"/v1/projects/{p}/deployments", {}),
        "onboarding": (f"/v1/projects/{p}/onboarding", {}),
        "Work (code tasks / builds)": (f"/v1/projects/{p}/work", {}),
        "settings": (f"/v1/projects/{p}/settings", {}),
        "readiness dots": (f"/v1/projects/{p}/add-project/preview", {}),
        "Setup (Repo and models)": ("/v1/setup", {"project_id": p}),
        "GitHub area bindings": ("/v1/github/repository-bindings", {"project_id": p}),
        "⌘K": ("/v1/jump", {"q": "basket", "project_id": p}),
    }
    answers = {}
    with world.acting_as(world.ctx(world.owner, mfa=True)):
        for name, (path, params) in reads.items():
            got = client.get(path, params=params)
            assert got.status_code == 200, f"{name}: {got.status_code} {got.text[:300]}"
            _never_github(got.text, full)
            answers[name] = got.json()
    # The repository is shown where a repository is shown.
    for name in ("project header (overview)", "Repo & knowledge", "settings",
                 "Setup (Repo and models)", "GitHub area bindings"):
        assert full in json.dumps(answers[name]), f"{name} hides the local repository"
    # Setup reads it as connected (the readiness behind "Connect a repository").
    setup = answers["Setup (Repo and models)"]
    repos = setup["repository"]["repos"]
    assert [r["host"] for r in repos if r["full_name"] == full] == ["local"]
    assert next(r for r in repos if r["full_name"] == full)["external_url"] is None
    # The GitHub area lists it (it answered 500 on a null connection) and says where it is.
    bound = answers["GitHub area bindings"]["items"]
    assert [b["host"] for b in bound if b["full_name"] == full] == ["local"]
    # The repository dot says "Local repo", not "GitHub".
    dots = answers["readiness dots"].get("readiness") or answers["readiness dots"].get("dots") or []
    labels = {d["key"]: d["label"] for d in dots}
    assert labels.get("repository") == "Local repo", dots
    # Repo & knowledge says how BOSS reaches it.
    knowledge = json.dumps(answers["Repo & knowledge"])
    assert "On this machine" in knowledge
    # ⌘K opens the change set in BOSS, never http://boss-local/.
    items = answers["⌘K"].get("items") or answers["⌘K"].get("results") or []
    prs = [i for i in items if i.get("kind") == "pr"]
    assert prs and prs[0]["external_url"] == local_project["view"], items
    # No read reached for GitHub.
    assert local_project["calls"] == []


def test_the_repository_check_and_agent_tools_handle_a_local_repository(world, client, engine,
                                                                       local_project):
    from boss_engine.services import kit_connections, setup_actions
    from boss_engine.config import get_settings

    p, full = local_project["id"], local_project["full_name"]
    with engine.connect() as db:
        binding = db.execute(text("SELECT id FROM github_repository_bindings WHERE project_id = :p"),
                             {"p": p}).scalar()
    # Re-check / Test all / test_github_access: "working", from the bare repository.
    with engine.begin() as db:
        found = setup_actions.check_repository(db, get_settings(), p, binding)
        state = kit_connections.github_state(db, get_settings(), p)
    assert found["state"] == "working" and found["host"] == "local"
    assert state["connected"] and state["repo"] == full and state["host"] == "local"

    from boss_engine.context import EngineContext
    from boss_engine.tools import build_default_registry

    registry = build_default_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    listed = registry.get("list_repositories").handler(ctx, project_id=p, actor="ceo", args={})
    assert full in json.dumps(listed, default=str)
    pr = registry.get("read_pull_request").handler(
        ctx, project_id=p, actor="ceo", args={"pull_request": local_project["url"]})
    # The change set row is not seeded here, so the answer is "could not read", never GitHub's.
    assert "api.github.com" not in json.dumps(pr, default=str)
    assert local_project["calls"] == []


def test_links_never_send_a_local_repository_to_github():
    from boss_engine.services import repo_links

    url = change_set_url("local_prj1/shop", 7)
    assert repo_links.pr_link(url) == "/changes/prj1/7"
    assert repo_links.repo_url("local_prj1/shop") is None
    assert repo_links.blob_url("local_prj1/shop", "README.md") is None
    assert repo_links.repo_url("acme/shop") == "https://github.com/acme/shop"
    assert repo_links.pr_link("https://github.com/acme/shop/pull/3") == \
        "https://github.com/acme/shop/pull/3"
    assert repo_links.host_label("local_prj1/shop") == "Local repo"
    assert repo_links.host_label("acme/shop") == "GitHub"
