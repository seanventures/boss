"""PUBLISH TO GITHUB LATER (BOSSAPP-921) and GITHUB OUTAGES (BOSSAPP-922), against a local GitHub.

The GitHub here is the plumbing gate's own fake (tests/e2e/gate/remote.py: git smart-HTTP and the
REST API on one loopback port, backed by real bare repositories), with its `blip` switch for
GitHub's bad moments (502s). Real git everywhere; the real database.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from apps.api.app import main  # noqa: F401 — the API's engine ports (real decisions) are set up
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_repo, transient_retry

_GATE = Path(__file__).resolve().parents[3] / "tests" / "e2e" / "gate"
if str(_GATE) not in sys.path:
    sys.path.insert(0, str(_GATE))
from remote import LocalGitHub  # noqa: E402  (tests/e2e/gate/remote.py)


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    monkeypatch.setattr(transient_retry, "_sleep", lambda _s: None)   # the policy, no waiting
    return root


@pytest.fixture
def hub(tmp_path):
    github = LocalGitHub(tmp_path / "github").start()
    yield github
    github.stop()


def _empty_on_hub(hub: LocalGitHub, owner: str, repo: str) -> Path:
    """An EMPTY repository on the fake GitHub (the fake seeds a sample app into new ones)."""
    bare = hub.remotes.root / owner / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "http.receivepack", "true"], check=True)
    return bare


def _project(engine, world) -> str:
    pid = f"pub-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Pub {pid[-4:]}"})
    return pid


def _github_connection(engine, pid: str) -> str:
    cid = f"ghconn_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO github_connections (id, project_id, connection_label, "
                        "auth_method, credential_ref, status, created_by) VALUES (:i, :p, 'Test', "
                        "'personal_access_token', 'env:BOSS_TEST_GH', 'active', 't')"),
                   {"i": cid, "p": pid})
    return cid


def _reach(hub: LocalGitHub, connection_id, *, token_kind="app"):
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.local_publish import GitHubReach
    from boss_engine.services.repo_host import GitAccess

    settings = get_settings()
    return GitHubReach(
        adapter_for=lambda o, r: GitHubAdapter(settings, owner=o, repo=r, token="gate-token",
                                               api_base_url=hub.api_url),
        access_for=lambda o, r: GitAccess(hub.git_url(o, r), via="token"),
        create_token="gate-token", create_token_kind=token_kind, api_base_url=hub.api_url,
        connection_id=connection_id)


def _review_decision(engine, pid: str, url: str, branch: str) -> str:
    did = f"dec_{uuid.uuid4().hex[:10]}"
    with engine.begin() as db:
        db.execute(text("INSERT INTO decisions (id, project_id, question, recommendation) VALUES "
                        "(:i, :p, 'Approve code change', CAST(:r AS JSONB))"),
                   {"i": did, "p": pid, "r": json.dumps({"action": "review_pull_request",
                                                          "pull_request_url": url,
                                                          "pull_request_number": 1,
                                                          "branch_name": branch})})
    return did


# ── 921: publish ─────────────────────────────────────────────────────────────────────────────

def test_publish_pushes_the_history_opens_prs_switches_the_binding_and_is_idempotent(
        engine, world, repo_root, hub):
    from boss_engine.services import local_projects, local_publish
    from boss_engine.services.local_repo_host import LocalGitAdapter, change_set_url
    from boss_engine.services.repo_registry import resolve_repository_target

    settings = get_settings()
    pid = _project(engine, world)
    with engine.begin() as db:
        bound = local_projects.create_for_project(db, settings, pid, name="shop", actor="t")
    owner, repo = bound["owner"], bound["repo"]
    bare = repo_root / pid / "shop.git"
    adapter = LocalGitAdapter(settings, owner=owner, repo=repo, project_id=pid, engine=engine)
    adapter.commit_files("main", {"index.html": "<h1>shop</h1>\n"}, "main: a page")
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    branch = f"boss/code/{run_id}"
    adapter.ensure_branch(branch)
    adapter.commit_files(branch, {"cart.js": "export const cart = [];\n"}, "a cart")
    pr = adapter.create_pull_request(branch, "A cart", "Adds a cart.", run_id=run_id)
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                        "pull_request_url) VALUES (:id, :p, 'A cart', 'completed', 'done', "
                        "CAST(:s AS JSONB), :u)"),
                   {"id": run_id, "p": pid, "u": pr.url,
                    "s": json.dumps({"owner": owner, "repo": repo})})
    decision = _review_decision(engine, pid, pr.url, branch)
    gh_bare = _empty_on_hub(hub, "acme", "shop")
    conn_id = _github_connection(engine, pid)

    with engine.begin() as db:
        out = local_publish.publish(db, settings, pid, owner="acme", repo="shop", actor="person",
                                    reach=_reach(hub, conn_id))
    assert out["status"] == "published" and out["full_name"] == "acme/shop"
    # History and branches, exactly.
    for name in ("main", branch):
        assert local_repo.branch_sha(gh_bare, name) == local_repo.branch_sha(bare, name)
    # The open change set is a pull request now; the run and its decision name it.
    prs = [p for p in hub.world.prs if p["repo"] == "acme/shop"]
    assert [p["head"] for p in prs] == [branch]
    with engine.connect() as db:
        cs = db.execute(text("SELECT state, github_pr_url FROM local_change_sets WHERE "
                             "repository = :r AND number = 1"), {"r": f"{owner}/{repo}"}).mappings().one()
        run_url = db.execute(text("SELECT pull_request_url FROM code_task_runs WHERE id = :r"),
                             {"r": run_id}).scalar()
        rec = db.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                         {"d": decision}).scalar()
        target = resolve_repository_target(db, settings, pid)
        roles = dict(db.execute(text("SELECT full_name, role FROM github_repository_bindings "
                                     "WHERE project_id = :p"), {"p": pid}).all())
    assert cs["state"] == "published" and cs["github_pr_url"] == run_url
    assert "/acme/shop/pull/" in run_url and rec["pull_request_url"] == run_url
    assert target["full_name"] == "acme/shop"                       # the project builds on GitHub
    assert roles == {"acme/shop": "primary", f"{owner}/{repo}": "mirror"}   # the local copy stays
    assert (bare / "HEAD").is_file()
    assert change_set_url(f"{owner}/{repo}", 1) != run_url

    # Again: nothing new — no second PR, no error.
    with engine.begin() as db:
        again = local_publish.publish(db, settings, pid, owner="acme", repo="shop", actor="person",
                                      reach=_reach(hub, conn_id))
    assert again["status"] == "published"
    assert len([p for p in hub.world.prs if p["repo"] == "acme/shop"]) == 1


def test_a_publish_that_stopped_half_way_resumes_where_it_stopped(engine, world, repo_root, hub):
    from boss_engine.services import local_projects, local_publish
    from boss_engine.services.local_repo_host import LocalGitAdapter

    settings = get_settings()
    pid = _project(engine, world)
    with engine.begin() as db:
        bound = local_projects.create_for_project(db, settings, pid, name="shop", actor="t")
    adapter = LocalGitAdapter(settings, owner=bound["owner"], repo=bound["repo"], project_id=pid,
                              engine=engine)
    adapter.ensure_branch("feature")
    adapter.commit_files("feature", {"a.txt": "a\n"}, "a")
    adapter.create_pull_request("feature", "Feature", "body")
    _empty_on_hub(hub, "acme", "half")
    conn_id = _github_connection(engine, pid)
    hub.blip("api", 50, match="POST /api/repos/acme/half/pulls")      # GitHub refuses the PR step
    with engine.begin() as db:
        with pytest.raises(local_publish.PublishError) as stopped:
            local_publish.publish(db, settings, pid, owner="acme", repo="half", actor="p",
                                  reach=_reach(hub, conn_id))
    assert stopped.value.step == "pull_requests"
    # The push is done and recorded; only the pull requests are left for the next run.
    assert local_repo.marker_of(repo_root / pid / "shop.git")["publish"]["step"] == "pull_requests"
    hub.blip("api", 0)
    with engine.begin() as db:
        done = local_publish.publish(db, settings, pid, owner="acme", repo="half", actor="p",
                                     reach=_reach(hub, conn_id))
    assert done["status"] == "published" and len(done["pull_requests"]) == 1


def test_publish_refuses_a_github_repository_that_holds_other_history(engine, world, repo_root, hub):
    from boss_engine.services import local_projects, local_publish

    settings = get_settings()
    pid = _project(engine, world)
    with engine.begin() as db:
        local_projects.create_for_project(db, settings, pid, name="shop", actor="t")
    hub.seed("acme", "taken", {"README.md": "someone else's\n"})
    with engine.begin() as db:
        with pytest.raises(local_publish.PublishError) as refused:
            local_publish.publish(db, settings, pid, owner="acme", repo="taken", actor="p",
                                  reach=_reach(hub, _github_connection(engine, pid)))
    assert refused.value.error == "target_not_empty" and "empty repository" in str(refused.value)


def _api(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_creating_a_repository_answers_in_words_for_each_case():
    from boss_engine.services.local_publish import create_repository

    made = []

    def org(request):
        if request.method == "GET" and request.url.path == "/repos/acme/new":
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        if request.url.path == "/orgs/acme/repos":
            made.append(json.loads(request.content))
            return httpx.Response(201, json={"full_name": "acme/new", "html_url": "https://x/acme/new"})
        return httpx.Response(500)

    out = create_repository(owner="acme", name="new", private=True, token="t",
                            api_base_url="http://gh", client=_api(org))
    assert out.ok and out.created and made == [{"name": "new", "private": True, "auto_init": False,
                                                "description": ""}]

    def personal(request):
        if request.url.path == "/users/me":
            return httpx.Response(200, json={"type": "User"})
        return httpx.Response(404, json={"message": "Not Found"})

    out = create_repository(owner="me", name="new", token="t", api_base_url="http://gh",
                            client=_api(personal))
    assert not out.ok and out.reason == "personal_account" and "personal" in out.words

    def no_admin(request):
        if request.url.path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return httpx.Response(404, json={"message": "Not Found"})

    out = create_repository(owner="acme", name="new", token="t", api_base_url="http://gh",
                            client=_api(no_admin))
    assert not out.ok and out.reason == "no_admin_permission" and "Administration" in out.words

    def exists(request):
        return httpx.Response(200, json={"html_url": "https://x/acme/new", "private": True})

    out = create_repository(owner="acme", name="new", token="t", api_base_url="http://gh",
                            client=_api(exists))
    assert out.ok and out.exists and not out.created


# ── 922: GitHub outage ───────────────────────────────────────────────────────────────────────

def _preview_clones_the_mirror(engine, settings, run_id: str, mirror: Path) -> None:
    """start_preview of a waiting_for_github run: the tree it would run comes from the mirror."""
    from boss_engine.services import preview_service

    class Cloned(Exception):
        pass

    seen: dict = {}

    def stop_after_clone(_engine, _run_id, workdir):
        seen["files"] = sorted(p.name for p in Path(workdir).iterdir())
        raise Cloned()

    def no_github(*_a, **_k):
        raise AssertionError("a waiting build's preview must not reach for GitHub")

    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(preview_service, "_preview_sandbox", lambda _pid: None)
        patch.setattr(preview_service, "_record_repo_manifests", stop_after_clone)
        patch.setattr(preview_service, "github_token_for", no_github)
        from boss_engine.identity import runs as identity_runs

        patch.setattr(identity_runs, "repo_access", no_github)
        with pytest.raises(Cloned):
            preview_service.start_preview(engine, settings, run_id)
    finally:
        patch.undo()
    assert "cart.js" in seen["files"] and "index.html" in seen["files"]
    with engine.connect() as db:
        run = dict(db.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                              {"r": run_id}).mappings().one())
    assert preview_service.github_wait_mirror(run, settings) == mirror


def test_a_github_outage_finishes_the_build_locally_then_pushes_and_opens_the_pr(
        engine, world, repo_root, hub):
    from boss_engine.services import github_outage, stuck_work
    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.governed_code_task_service import _hold_for_github
    from boss_engine.services.repo_host import GitAccess
    from boss_engine.services.sandbox import EphemeralSandbox

    settings = get_settings()
    pid = _project(engine, world)
    hub.seed("acme", "store", {"index.html": "<h1>store</h1>\n"})
    owner, repo = "acme", "store"

    # Build start: the mirror is fetched while GitHub answers.
    fetched = github_outage.refresh_mirror(settings, owner, repo, url=hub.git_url(owner, repo))
    assert fetched["ok"] and github_outage.mirror_ready(owner, repo, settings)

    # GitHub goes down: every git and API request is a 502.
    hub.blip("git", 10_000)
    hub.blip("api", 10_000)
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    branch = f"boss/code/{run_id}"

    # The clone fails after its retries, and is an outage; the mirror clones instead.
    sandbox = EphemeralSandbox(settings, task_id=run_id)
    with pytest.raises(Exception) as clone_error:
        sandbox.provision(owner, repo, base_branch="main", work_branch=branch,
                          egress=SimpleNamespace(clone_url=hub.git_url(owner, repo), git_config=[],
                                                 own_git=None))
    assert github_outage.is_outage(clone_error.value)
    sandbox = EphemeralSandbox(settings, task_id=run_id)
    root = sandbox.provision(owner, repo, base_branch="main", work_branch=branch,
                             egress=SimpleNamespace(clone_url=local_repo.file_url(
                                 local_repo.mirror_path(owner, repo, settings)), git_config=[],
                                 own_git=None))
    try:
        (root / "cart.js").write_text("export const cart = [];\n")
        files = sandbox.diff()
    finally:
        sandbox.teardown()

    # The build's adapter: GitHub's, falling back to the mirror once GitHub stops answering.
    switched = []
    gh = github_outage.ResilientGitHub(
        GitHubAdapter(settings, owner=owner, repo=repo, token="gate-token", api_base_url=hub.api_url),
        settings=settings, project_id=pid, run_id=run_id, engine=engine,
        on_switch=switched.append)
    gh.ensure_branch(branch, "main")
    for fc in files:
        gh.upsert_file(branch, fc.path, fc.content, "governed: implement")
    pr = gh.create_pull_request(branch, "A cart", "Adds a cart.", base="main")
    assert gh.offline and switched
    mirror = local_repo.mirror_path(owner, repo, settings)
    assert local_repo.read_file(mirror, branch, "cart.js") == "export const cart = [];\n"
    assert "boss-local" in pr.url

    # The build ends "waiting for GitHub" — never failed — with a push queued.
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                        "finalize_ctx, pull_request_url) VALUES (:id, :p, 'A cart', 'completed', "
                        "'done', CAST(:s AS JSONB), CAST(:f AS JSONB), :u)"),
                   {"id": run_id, "p": pid, "u": pr.url,
                    "s": json.dumps({"owner": owner, "repo": repo}),
                    "f": json.dumps({"github_offline": switched[0], "work_branch": branch})})
    decision = _review_decision(engine, pid, pr.url, branch)
    held = _hold_for_github(engine, run_id, {"status": "completed", "done": True})
    assert held["status"] == "waiting_for_github" and "Waiting for GitHub" in held["message"]
    with engine.connect() as db:
        run = dict(db.execute(text("SELECT * FROM code_task_runs WHERE id = :r"),
                              {"r": run_id}).mappings().one())
        queued = db.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p AND kind = "
                                 "'repo.push_pending' AND status = 'queued'"), {"p": pid}).scalar()
    assert run["status"] == "waiting_for_github" and queued == 1
    # Its preview clones the local mirror (GitHub still answers 502 and has no branch yet).
    _preview_clones_the_mirror(engine, settings, run_id, mirror)
    # The Shepherd's diagnosis says what waits and why.
    with engine.connect() as db:
        reason = stuck_work.wait_reason(db, run, job=None)
        waiting = github_outage.waiting(db, pid)
    assert reason["code"] == "github" and reason["who"] == "boss"
    assert "Waiting for GitHub" in reason["words"]
    assert waiting and waiting[0]["branch"] == branch and "waits for GitHub" in waiting[0]["words"]

    def github(c, s, project_id, o, r):
        return (GitHubAdapter(s, owner=o, repo=r, token="gate-token", api_base_url=hub.api_url),
                GitAccess(hub.git_url(o, r), via="token"))

    pending_id = waiting[0]["id"]
    # Still down: a recorded wait, the next try queued with backoff.
    first = github_outage.attempt(engine, settings, pending_id, github=github)
    assert first["state"] == "waiting" and first["again"] and first["next_in_minutes"] == 1
    # GitHub answers again: the branch is pushed and the pull request opened.
    hub.blip("git", 0)
    hub.blip("api", 0)
    done = github_outage.attempt(engine, settings, pending_id, github=github)
    assert done["state"] == "done", done
    gh_bare = hub.remotes.root / owner / f"{repo}.git"
    assert local_repo.branch_sha(gh_bare, branch) == local_repo.branch_sha(mirror, branch)
    prs = [p for p in hub.world.prs if p["repo"] == "acme/store"]
    assert [p["head"] for p in prs] == [branch]
    with engine.connect() as db:
        run = dict(db.execute(text("SELECT status, phase, pull_request_url FROM code_task_runs "
                                   "WHERE id = :r"), {"r": run_id}).mappings().one())
        rec = db.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                         {"d": decision}).scalar()
        left = github_outage.waiting(db, pid)
    assert run["status"] == "completed" and run["phase"] == "done"
    assert run["pull_request_url"] == done["pull_request_url"] == rec["pull_request_url"]
    assert left == []


def test_an_answer_from_github_is_never_an_outage(repo_root):
    """A 401/403/404 fails as before: only GitHub not answering moves a build to its mirror."""
    from boss_engine.services import github_outage

    request = httpx.Request("GET", "https://api.github.com/repos/a/b")
    assert github_outage.is_outage(httpx.HTTPStatusError(
        "502", request=request, response=httpx.Response(502, request=request)))
    assert not github_outage.is_outage(httpx.HTTPStatusError(
        "403", request=request, response=httpx.Response(403, request=request)))
    assert not github_outage.is_outage(RuntimeError("fatal: Authentication failed"))

    class Primary:
        repository_full_name = "acme/none"

        def ensure_branch(self, *_a):
            raise httpx.HTTPStatusError("403", request=request,
                                        response=httpx.Response(403, request=request))

    gh = github_outage.ResilientGitHub(Primary(), settings=get_settings(), project_id="p")
    with pytest.raises(httpx.HTTPStatusError):
        gh.ensure_branch("b")
    assert not gh.offline


def test_the_push_pending_job_is_claimed_by_the_worker_and_runs_in_the_api():
    """Declared, permitted and routed: a kind the worker does not claim sits queued for ever."""
    from boss_worker import runner

    from boss_engine.identity.job_permissions import permission_for
    from boss_engine.services.github_outage import JOB_KIND

    assert JOB_KIND in runner.capabilities()
    assert str(permission_for(JOB_KIND)) in ("code_task:create", "P.CODE_TASK_CREATE")
    sent = []

    class Client:
        worker_id = "w1"

        def run_push_pending(self, payload):
            sent.append(payload)
            return {"status": "succeeded", "state": "waiting", "again": True}

    job = SimpleNamespace(kind=JOB_KIND, id="job_1", lease_token="lease",
                          payload={"pending_id": "rpp_1"}, project_id="p")
    out = runner.execute_job(job, Client())
    assert out["state"] == "waiting"
    assert sent == [{"job_id": "job_1", "worker_id": "w1", "lease_token": "lease"}]
