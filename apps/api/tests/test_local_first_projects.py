"""LOCAL-FIRST PROJECTS (BOSSAPP-915: 918 local repo host, 919 local review/merge, 920 preview and
staging from local, 923 import, 924 the onboarding agent's machine checks).

Real git in temporary directories and the real database; GitHub is switched OFF for the whole
test (every HTTP call to github is refused and counted), and the count must stay ZERO.

The build's own calls are the pipeline's own: the sandbox's provision (the clone), the
SoftwareFactoryService adapter's ensure_branch/upsert_file (a stage's commit), the factory's
propose_pull_request (the change set and its review decision), a person's approval through the
API, the factory's merge, the preview's clone (repo_access) and the staging deploy's
_build_dist — the functions a build runs, never copies of them.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("lf-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("lf-viewer")
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
    return root


@pytest.fixture
def github_off(monkeypatch):
    """GitHub is unreachable: every HTTP request to it is refused and recorded."""
    calls: list[str] = []
    real = httpx.Client.send

    def send(self, request, *args, **kwargs):
        if "github" in str(request.url.host):
            calls.append(f"{request.method} {request.url}")
            raise httpx.ConnectError("GitHub is off in this test", request=request)
        return real(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    from boss_engine.services import github_app_adapter

    def no_app(*_a, **_k):
        calls.append("github app token")
        raise github_app_adapter.GitHubAppConfigurationError("GitHub is off in this test")

    monkeypatch.setattr(github_app_adapter.GitHubAppAdapter, "installation_token", no_app)
    return calls


def _owner(world):
    return world.acting_as(world.ctx(world.owner, mfa=True))


def _fresh_project(engine, world) -> str:
    """A project of the world's org with no repository yet."""
    pid = f"lf-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Shop {pid[-4:]}"})
    return pid


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True).stdout.strip()


# ── 918: the local repository, readiness, the build's clone and commits ─────────────────────

def test_a_local_project_builds_reviews_merges_previews_and_deploys_without_github(
        client, world, engine, repo_root, github_off, monkeypatch):
    from boss_engine.services import deploy_service, workflow_readiness
    from boss_engine.services.local_repo_host import LocalGitAdapter, is_local_pr_url
    from boss_engine.services.sandbox import EphemeralSandbox
    from boss_engine.services.software_factory import SoftwareFactoryService

    settings = get_settings()
    pid = world.project_a
    with _owner(world):
        made = client.post(f"/v1/projects/{pid}/local-repo", json={"name": "Corner Shop"})
    assert made.status_code == 200, made.text
    bound = made.json()
    owner, repo = bound["owner"], bound["repo"]
    assert bound["host"] == "local" and owner == f"local_{pid}" and repo == "corner-shop"
    bare = repo_root / pid / "corner-shop.git"
    assert (bare / "HEAD").is_file()
    assert local_repo.list_paths(bare, "main") == [".gitignore", "README.md"]   # the first commit
    with engine.connect() as db:
        row = db.execute(text("SELECT host, connection_id, role FROM github_repository_bindings "
                              "WHERE project_id = :p AND full_name = :f"),
                         {"p": pid, "f": f"{owner}/{repo}"}).mappings().one()
    assert dict(row) == {"host": "local", "connection_id": None, "role": "primary"}

    # Readiness: a local repository IS a repository BOSS can push to.
    with engine.begin() as db:
        ok, said = workflow_readiness.check(db, settings, pid, "repository")
    assert ok and "locally" in said

    # The build's adapter, resolved from the project's binding: the local one, same interface.
    sf = SoftwareFactoryService(settings)   # the API's own ports: real decisions
    with engine.begin() as db:
        plane = sf.bind_project_github(db, pid)
    assert plane["github_plane"] == "local" and isinstance(sf.github, LocalGitAdapter)
    assert sf.pipeline.provider == "boss_sandbox"

    # The sandbox clones the bare repository (the same provision call; file://).
    run_id = f"crun_lf{uuid.uuid4().hex[:10]}"
    branch = f"boss/code/{run_id}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                        "finalize_ctx) VALUES (:id, :p, 'Corner shop page', 'running', 'coding', "
                        "CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                   {"id": run_id, "p": pid, "s": json.dumps({"owner": owner, "repo": repo}),
                    "f": json.dumps({"work_branch": branch})})
    sandbox = EphemeralSandbox(settings, task_id=run_id)
    access = SimpleNamespace(clone_url=local_repo.file_url(bare), git_config=[], own_git=None)
    root = sandbox.provision(owner, repo, base_branch="main", work_branch=branch, egress=access)
    try:
        assert _git(root, "remote", "get-url", "origin").startswith("file://")
        (root / "index.html").write_text("<!doctype html><h1>Corner shop</h1>\n")
        (root / "app.js").write_text("console.log('open');\n")
        files = sandbox.diff()
        # A stage's commit (_commit_stage): ensure_branch + upsert_file, now real git commits.
        sf.github.ensure_branch(branch, "main")
        for fc in files:
            sf.github.upsert_file(branch, fc.path, fc.content, "governed: implement")
    finally:
        sandbox.teardown()
    assert set(local_repo.list_paths(bare, branch)) >= {"index.html", "app.js"}
    assert local_repo.read_file(bare, branch, "index.html").startswith("<!doctype html>")

    # 919: the PR step opens a LOCAL CHANGE SET and its review decision (propose_pull_request).
    with engine.begin() as db:
        reservation = sf.budget.reserve(db, pid, category="code_task", amount_usd_micros=1000,
                                        reason="local-first test", expires_in_minutes=30)
    assert reservation.get("status") == "reserved", reservation
    from boss_engine.services.software_factory import FileChange

    with engine.begin() as db:
        proposal = sf.propose_pull_request(
            db, pid, None, reservation_id=reservation["reservation_id"],
            dedupe_key=f"code:task:{run_id}", title="Corner shop page", branch_name=branch,
            summary="A landing page for the shop.", files=[FileChange(path=f.path, content=f.content)
                                                            for f in files],
            run_id=run_id, pr_base="main")
    assert proposal["status"] in ("created", "succeeded"), proposal
    url = proposal["pull_request_url"]
    assert is_local_pr_url(url) and url.endswith("/pull/1")
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET pull_request_url = :u, status = 'completed', "
                        "phase = 'done' WHERE id = :r"), {"u": url, "r": run_id})

    # The review panel's comments land on the change set (pr_review_post's calls).
    review = sf.github.create_review(1, "Panel: looks right.", "COMMENT",
                                     [{"path": "index.html", "line": 1, "body": "Add a title."}])
    assert review.state == "COMMENTED"

    # The change set over HTTP: branch, diff, files, commits, review, checks, its decision.
    with world.acting_as(world.ctx(world.viewer)):
        listed = client.get(f"/v1/projects/{pid}/local-changes")
        one = client.get(f"/v1/projects/{pid}/local-changes/1")
    assert listed.status_code == 200 and [i["number"] for i in listed.json()["items"]] == [1]
    view = one.json()
    assert one.status_code == 200, one.text
    assert view["branch"] == branch and view["base_branch"] == "main" and view["state"] == "open"
    assert {f["path"] for f in view["files"]} >= {"index.html", "app.js"}
    assert "+<!doctype html><h1>Corner shop</h1>" in view["diff"]
    assert view["commits"] and view["review"][0]["comments"][0]["body"] == "Add a title."
    assert view["checks"]["where"] == "BOSS's sandbox"
    decision = view["decision"]
    assert decision and decision["status"] == "pending" and view["merge"]["decision_id"] == decision["id"]

    # A viewer cannot merge; merging is a person's approval of the review decision.
    with world.acting_as(world.ctx(world.viewer)):
        refused = client.post(f"/v1/projects/{pid}/local-changes/1/merge", json={})
    assert refused.status_code == 403
    monkeypatch.setattr(main.decisions.settings, "enable_sandbox_merge", True, raising=False)
    with _owner(world):
        merged = client.post(f"/v1/projects/{pid}/local-changes/1/merge", json={"note": "ship it"})
    assert merged.status_code in (200, 202), merged.text
    with engine.connect() as db:
        job = db.execute(text("SELECT payload FROM jobs WHERE project_id = :p AND kind = "
                              "'github.pull_request.merge' ORDER BY created_at DESC LIMIT 1"),
                         {"p": pid}).scalar()
    assert job, "approving the review queued the governed merge"
    payload = job if isinstance(job, dict) else json.loads(job)
    # The worker's merge job, as the API runs it: the factory's merge, on the local adapter.
    merger = SoftwareFactoryService(settings)
    with engine.begin() as db:
        done = merger.merge_pull_request(
            db, pid, None, payload["decision_id"], payload["dedupe_key"],
            payload["pull_request_number"], payload["pull_request_url"], payload["title"],
            payload.get("branch_name"))
    assert done["status"] in ("created", "reused"), done
    assert local_repo.branch_sha(bare, "main") == done["merged_sha"]
    assert set(local_repo.list_paths(bare, "main")) >= {"index.html", "app.js", "README.md"}
    with engine.connect() as db:
        state = db.execute(text("SELECT state, merged_sha FROM local_change_sets WHERE "
                                "repository = :r AND number = 1"),
                           {"r": f"{owner}/{repo}"}).mappings().one()
    assert state["state"] == "merged" and state["merged_sha"] == done["merged_sha"]

    # 920: the preview clones the bare repository (repo_access), never GitHub.
    from boss_engine.identity import runs as identity_runs

    with identity_runs.repo_access(engine, host="github", owner=owner, repo=repo,
                                   project_id=pid, token="never-used") as preview_access:
        assert preview_access.url.startswith("file://") and preview_access.via == "local"
        dest = repo_root.parent / "preview"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, preview_access.url,
                        str(dest)], check=True, capture_output=True, env=preview_access.boss_environ())
    assert (dest / "index.html").is_file()

    # 920: the staging deploy builds dist/ from the bare repository's default branch.
    work, mode, _entry, sha = deploy_service._build_dist(settings, owner, repo, "main",
                                                         changed=["index.html", "app.js"])
    try:
        assert mode == "static" and done["merged_sha"].startswith(sha)
        assert (work / "dist" / "index.html").is_file() or (work / "index.html").is_file()
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    assert github_off == [], f"GitHub was called: {github_off}"


def test_the_local_change_set_url_reads_like_a_pull_request_everywhere():
    from boss_engine.services.ci_gate import parse_pr_url
    from boss_engine.services.local_repo_host import change_set_url

    url = change_set_url("local_p1/shop", 7)
    assert parse_pr_url(url) == ("local_p1/shop", 7)
    assert url.rstrip("/").endswith("/pull/7")


def test_an_unknown_github_operation_on_a_local_repository_says_so(repo_root):
    from boss_engine.services.local_repo_host import LocalGitAdapter, LocalHostUnsupported

    local_repo.create("p-x", "shop")
    adapter = LocalGitAdapter(get_settings(), owner="local_p-x", repo="shop", project_id="p-x")
    assert not hasattr(adapter, "dispatch_workflow")     # feature checks stay honest
    with pytest.raises(LocalHostUnsupported, match="local repository"):
        adapter._request("POST", "/repos/local_p-x/shop/actions/workflows/ci.yml/dispatches")
    assert adapter._request("GET", "/repos/local_p-x/shop/commits/abc/check-runs") == {
        "total_count": 0, "check_runs": []}


def test_a_merge_conflict_changes_nothing_and_says_which_files(repo_root):
    created = local_repo.create("p-c", "app")
    bare = Path(created["path"])
    local_repo.ensure_branch(bare, "feature")
    local_repo.commit_files(bare, "feature", {"README.md": "feature\n"}, "feature")
    before = local_repo.commit_files(bare, "main", {"README.md": "main moved\n"}, "main")
    with pytest.raises(local_repo.LocalMergeConflict, match="README.md"):
        local_repo.merge(bare, "feature", "main", "merge")
    assert local_repo.branch_sha(bare, "main") == before


def test_a_new_local_project_through_add_project(client, world, engine, repo_root, github_off):
    name = f"Bakery {uuid.uuid4().hex[:6]}"
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "folder", "name": name,
            "code_location": "local", "local_folder": None,
            "folder": None, "words": "a bakery site"})
    # kind folder needs a folder; an idea with code_location local makes a new repository.
    assert r.status_code == 422
    with _owner(world):
        r = client.post("/v1/add-project", json={
            "context_project_id": world.project_a, "kind": "idea", "name": name,
            "words": "A bakery site with the week's bread.", "code_location": "local",
            "start_anyway": ["repository", "model", "deployment_target"]})
    said = r.json()
    pid = said.get("project_id")
    assert pid, said
    with engine.connect() as db:
        full = db.execute(text("SELECT full_name FROM github_repository_bindings WHERE "
                               "project_id = :p AND host = 'local'"), {"p": pid}).scalar()
    assert full == f"local_{pid}/" + local_repo.clean_name(name)
    assert (repo_root / pid / f"{local_repo.clean_name(name)}.git" / "HEAD").is_file()
    assert any(s["key"] == "repository" and "machine" in s["label"] for s in said["steps"])
    assert github_off == []


# ── 923: import local code ───────────────────────────────────────────────────────────────────

def _folder(base: Path, *, history: bool) -> Path:
    src = base / ("with-history" if history else "no-history")
    (src / "node_modules" / "left-pad").mkdir(parents=True)
    (src / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1\n")
    (src / "server.js").write_text("require('http').createServer().listen(3000)\n")
    (src / ".env").write_text("SECRET=do-not-import\n")
    (src / ".env.example").write_text("SECRET=\n")
    if history:
        _git(src, "init", "-q", "-b", "main")
        _git(src, "-c", "user.email=a@b.c", "-c", "user.name=A", "add", "server.js", ".env.example")
        _git(src, "-c", "user.email=a@b.c", "-c", "user.name=A", "commit", "-qm", "first")
        _git(src, "checkout", "-qb", "feature/x")
        _git(src, "-c", "user.email=a@b.c", "-c", "user.name=A", "commit", "-q", "--allow-empty",
             "-m", "on a branch")
        _git(src, "checkout", "-q", "main")
        _git(src, "remote", "add", "origin", "https://someone:tok@github.com/acme/shop.git")
    return src


def test_import_a_folder_without_history_makes_one_commit_without_deps_or_secrets(
        client, world, engine, repo_root, tmp_path, monkeypatch, github_off):
    from apps.api.app.routers import local_projects as route

    imports = tmp_path / "import"
    imports.mkdir()
    src = _folder(imports, history=False)
    monkeypatch.setattr(route._settings, "local_import_root", str(imports))
    pid = _fresh_project(engine, world)
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-repo/import",
                        json={"path": "no-history", "name": "shop"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["history"] == "new" and out["host"] == "local"
    bare = repo_root / pid / "shop.git"
    assert sorted(local_repo.list_paths(bare, "main")) == [".env.example", "server.js"]
    assert src.joinpath(".git").exists() is False      # the folder itself is never written
    assert github_off == []


def test_import_a_folder_with_history_keeps_every_branch_and_says_where_it_came_from(
        client, world, engine, repo_root, tmp_path, monkeypatch):
    from apps.api.app.routers import local_projects as route

    imports = tmp_path / "import"
    imports.mkdir()
    src = _folder(imports, history=True)
    monkeypatch.setattr(route._settings, "local_import_root", str(imports))
    pid = _fresh_project(engine, world)
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-repo/import",
                        json={"path": str(src), "name": "shop"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["history"] == "kept"
    assert set(out["branches"]) == {"main", "feature/x"}
    assert out["facts"]["github"] == "acme/shop"          # the choice "keep it on GitHub" exists
    assert "tok" not in json.dumps(out)                    # a remote's credential never leaves
    bare = repo_root / pid / "shop.git"
    assert local_repo.branch_sha(bare, "main") == _git(src, "rev-parse", "main")


def test_import_refuses_paths_outside_the_import_directory(client, world, engine, repo_root,
                                                           tmp_path, monkeypatch):
    from apps.api.app.routers import local_projects as route

    imports = tmp_path / "import"
    imports.mkdir()
    outside = tmp_path / "secret-place"
    outside.mkdir()
    (imports / "link").symlink_to(outside)
    monkeypatch.setattr(route._settings, "local_import_root", str(imports))
    pid = _fresh_project(engine, world)
    for path in (str(outside), "../secret-place", "link", "/etc"):
        with _owner(world):
            r = client.post(f"/v1/projects/{pid}/local-repo/import", json={"path": path})
        assert r.status_code in (403, 404, 422), (path, r.text)
        assert r.json()["message"]
    monkeypatch.setattr(route._settings, "local_import_root", "")
    with _owner(world):
        r = client.post(f"/v1/projects/{pid}/local-repo/import", json={"path": "anything"})
    assert r.status_code == 403 and r.json()["error"] == "import_root_not_configured"


def test_import_from_a_verified_host_reads_a_tar_over_ssh_and_keeps_history(
        engine, world, repo_root, tmp_path):
    """The host path: the folder arrives as ONE tar stream over ssh (stubbed here), with .git."""
    import io
    import tarfile

    from boss_engine.services import local_projects

    src = _folder(tmp_path, history=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if "node_modules" in path.parts:
                continue
            tar.add(path, arcname=str(path.relative_to(src)), recursive=False)
    pid = _fresh_project(engine, world)
    host_id = f"host_{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO deployment_hosts (id, project_id, name, address, status) "
                        "VALUES (:i, :p, 'shop-box', '10.0.0.9', 'verified')"),
                   {"i": host_id, "p": pid})
        out = local_projects.import_code(db, get_settings(), pid, name="shop", actor="t",
                                         path="/srv/shop", host_id=host_id,
                                         fetch_tar=lambda host, path: buf.getvalue())
    assert out["history"] == "kept" and out["facts"]["host"] == "shop-box"
    assert set(out["branches"]) == {"main", "feature/x"}


# ── 924: the onboarding agent's machine checks ───────────────────────────────────────────────

def test_the_machine_checks_fix_a_missing_repository_and_explain_offline_with_a_remote_model(
        engine, world, repo_root, monkeypatch):
    import shutil

    from boss_engine.services import local_projects

    settings = get_settings()
    pid = _fresh_project(engine, world)
    with engine.begin() as db:
        local_projects.create_for_project(db, settings, pid, name="shop", actor="t")
    shutil.rmtree(repo_root / pid)                               # the repository is gone
    monkeypatch.setattr(local_projects, "_model_is_local", lambda c, s, p: (False, "claude_code"))
    with engine.begin() as db:
        found = local_projects.machine_checks(db, settings, pid, fix=True, online=lambda: False)
    by = {c["key"]: c for c in found["checks"]}
    assert by["git"]["ok"] and by["local_repo"]["ok"] and by["disk"]["ok"] is not False
    assert found["fixed"] == ["made the local repository", "made the first commit"]
    assert by["model"]["ok"] is False and "offline" in by["model"]["words"].lower()
    assert "local engine" in found["summary"]
    monkeypatch.setattr(local_projects, "_model_is_local", lambda c, s, p: (True, "ollama"))
    with engine.begin() as db:
        found = local_projects.machine_checks(db, settings, pid, fix=True, online=lambda: False)
    assert found["ok"] and found["fixed"] == []


# ── Delete project's hook ────────────────────────────────────────────────────────────────────

def test_delete_local_repo_removes_only_that_projects_repositories(repo_root):
    from boss_engine.services.local_projects import delete_local_repo

    local_repo.create("p-del", "a")
    local_repo.create("p-keep", "b")
    assert delete_local_repo("p-del") is True
    assert delete_local_repo("p-del") is False                 # idempotent
    assert not (repo_root / "p-del").exists() and (repo_root / "p-keep" / "b.git").is_dir()
    with pytest.raises(local_repo.LocalRepoError):
        delete_local_repo("..")


def test_a_build_with_no_change_set_is_an_answer_not_a_404(client, world):
    with world.acting_as(world.ctx(world.viewer)):
        r = client.get("/v1/runs/crun_none/change-set", params={"project_id": world.project_a})
    assert r.status_code == 200 and r.json()["change_set"] is None
