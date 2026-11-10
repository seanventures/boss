"""THE LOCAL GIT REMOTE (BOSSAPP-948, P2/P3): real `git clone / pull / push` against the real API
(uvicorn on a free port, the real identity checks, HTTP Basic with real BOSS tokens) over bare
repositories in a temporary directory, with git's own `git http-backend` and the image's hooks.
No Docker. Design: docs/product/LOCAL_GIT_REMOTE_AND_REVIEW.md §1–§3."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_git_hooks, local_projects, local_repo, local_review

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("git-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("git-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    # The hooks run in git's own processes: they read the same root from the environment.
    monkeypatch.setenv("BOSS_LOCAL_REPO_ROOT", str(root))
    return root


@pytest.fixture(scope="module")
def server():
    """The real API on a free loopback port, the identity guard enforcing."""
    import uvicorn

    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)
    settings.authz_mode, settings.authz_router_modes = saved


def _token(engine, world, who, scopes) -> str:
    from boss_engine.identity import api_keys

    with engine.begin() as db:
        made = api_keys.create(db, creator=world.ctx(who), owner=who, kind="pat",
                               name=f"git {uuid.uuid4().hex[:6]}", scopes=scopes)
    return made.key


def _project(engine, world) -> tuple[str, Path]:
    pid = f"gr-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Inbox {pid[-4:]}"})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="helpdesk",
                                                  actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {"src/app.js": "".join(f"l{i}\n" for i in range(1, 9))},
                            "app")
    return pid, bare


def _git_env(tmp_path: Path, token: str = "") -> dict[str, str]:
    """A git client with no machine config; GIT_ASKPASS answers with the token (never on a
    command line, never in a URL)."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    askpass = tmp_path / "askpass.sh"
    askpass.write_text('#!/bin/sh\nprintf "%s" "$BOSS_TEST_TOKEN"\n')
    askpass.chmod(askpass.stat().st_mode | stat.S_IEXEC)
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass), "BOSS_TEST_TOKEN": token,
            "GIT_AUTHOR_NAME": "Param", "GIT_AUTHOR_EMAIL": "p@example.com",
            "GIT_COMMITTER_NAME": "Param", "GIT_COMMITTER_EMAIL": "p@example.com"}


def _git(env, *args, cwd=None, check=True):
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=120)
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{done.stdout}\n{done.stderr}")
    return done


def test_pre_receive_refuses_in_words():
    ok = local_git_hooks.check_update
    anc = lambda a, b: a == "a" * 40  # noqa: E731 — "a…" is an ancestor of everything
    z = local_git_hooks.ZERO
    a, b = "a" * 40, "b" * 40
    assert "moves only through BOSS's merge" in ok(a, b, "refs/heads/main", default_branch="main",
                                                  busy=[], is_ancestor=anc)
    assert "tags" in ok(z, b, "refs/tags/v1", default_branch="main", busy=[], is_ancestor=anc)
    assert "force push" in ok(b, a, "refs/heads/fix", default_branch="main", busy=[],
                              is_ancestor=lambda x, y: False)
    assert "building this branch" in ok(a, b, "refs/heads/boss/code/crun_1", default_branch="main",
                                        busy=["boss/code/crun_1"], is_ancestor=anc)
    assert "deleting" in ok(a, z, "refs/heads/fix", default_branch="main", busy=[], is_ancestor=anc)
    assert ok(z, b, "refs/heads/fix/inbox", default_branch="main", busy=[], is_ancestor=anc) is None
    assert ok(a, b, "refs/notes/boss-review", default_branch="main", busy=[], is_ancestor=anc) is None
    assert ok(a, b, "refs/heads/boss/code/done", default_branch="main", busy=["boss/code/x"],
              is_ancestor=anc) is None


def test_clone_pull_and_push_a_branch_through_the_api(server, engine, world, repo_root, tmp_path,
                                                      monkeypatch):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)
    pid, bare = _project(engine, world)
    url = f"{server}/git/{pid}.git"
    owner_token = _token(engine, world, world.owner, ["repository:read", "repository:push",
                                                       "code_task:read", "code_task:steer"])
    viewer_token = _token(engine, world, world.viewer, ["repository:read"])

    # No token: git is asked to sign in (401 with a Basic challenge) and gives up.
    r = httpx.get(f"{url}/info/refs", params={"service": "git-upload-pack"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    anon = _git(_git_env(tmp_path / "a"), "clone", url, str(tmp_path / "anon"), check=False)
    assert anon.returncode != 0

    # Another machine's name is refused (loopback only).
    far = httpx.get(f"{url}/info/refs", params={"service": "git-upload-pack"},
                    headers={"Host": "boss.tailnet.ts.net"}, auth=("boss", owner_token))
    assert far.status_code == 403 and far.json()["error"] == "git_remote_local_only"

    # The owner clones with the token; it is nowhere in the clone's config.
    (tmp_path / "o").mkdir()
    env = _git_env(tmp_path / "o", owner_token)
    work = tmp_path / "work"
    _git(env, "clone", url, str(work))
    assert (work / "src" / "app.js").read_text().startswith("l1")
    assert owner_token not in (work / ".git" / "config").read_text()

    # A branch pushed becomes a change, said on the terminal.
    _git(env, "switch", "-c", "fix/inbox-empty-copy", cwd=work)
    (work / "src" / "app.js").write_text("".join(f"l{i}\n" for i in range(1, 9)).replace("l3", "L3"))
    _git(env, "commit", "-qam", "Inbox: clearer empty state", cwd=work)
    pushed = _git(env, "push", "-u", "origin", "fix/inbox-empty-copy", cwd=work)
    assert "BOSS opened change #1" in pushed.stderr, pushed.stderr
    assert f"/changes/{pid}/1" in pushed.stderr
    with engine.connect() as db:
        cs = db.execute(text("SELECT * FROM local_change_sets WHERE project_id = :p"),
                        {"p": pid}).mappings().one()
        review = db.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :r"),
                            {"r": cs["run_id"]}).scalar()
        why = db.execute(text("SELECT data FROM events WHERE project_id = :p AND event_type = "
                              "'local_repo.review_not_queued'"), {"p": pid}).scalars().all()
    assert review is not None, why
    assert cs["branch"] == "fix/inbox-empty-copy" and cs["title"] == "Inbox: clearer empty state"
    assert cs["author"] and "pushed" in cs["labels"]
    assert review["local_change_branch"] == "fix/inbox-empty-copy"      # the review is queued

    # A second push updates the same change and moves its comments with the code.
    with engine.begin() as db:
        c = local_review.add_comment(db, get_settings(), project_id=pid, number=1, body="why L3?",
                                     author="param", path="src/app.js", line=5)
    (work / "src" / "app.js").write_text("top\n" + (work / "src" / "app.js").read_text())
    _git(env, "commit", "-qam", "top line", cwd=work)
    again = _git(env, "push", cwd=work)
    assert "BOSS updated change #1" in again.stderr
    with engine.begin() as db:
        moved = local_review.comment(db, cs["id"], c["id"])
    assert moved["line"] == 6 and not moved["outdated"]

    # main moves only through the governed merge; a tag and a force push are refused in words.
    _git(env, "switch", "main", cwd=work)
    (work / "README.md").write_text("direct\n")
    _git(env, "commit", "-qam", "direct to main", cwd=work)
    to_main = _git(env, "push", "origin", "main", cwd=work, check=False)
    assert to_main.returncode != 0 and "main moves only through BOSS's merge" in to_main.stderr
    _git(env, "tag", "v9", cwd=work)
    assert "tags are not pushed" in _git(env, "push", "origin", "v9", cwd=work, check=False).stderr
    _git(env, "switch", "fix/inbox-empty-copy", cwd=work)
    _git(env, "reset", "-q", "--hard", "HEAD~1", cwd=work)
    (work / "x.txt").write_text("rewrite\n")
    _git(env, "add", "x.txt", cwd=work)
    _git(env, "commit", "-qm", "rewrite", cwd=work)
    forced = _git(env, "push", "-f", "origin", "fix/inbox-empty-copy", cwd=work, check=False)
    assert forced.returncode != 0 and "force push" in forced.stderr
    assert local_repo.branch_sha(bare, "main") != _git(env, "rev-parse", "main", cwd=work).stdout.strip()

    # Pull brings BOSS's own commits.
    local_repo.commit_files(bare, "fix/inbox-empty-copy", {"src/sla.js": "x\n"}, "Forge: fix round")
    _git(env, "reset", "-q", "--hard", "origin/fix/inbox-empty-copy", cwd=work)
    _git(env, "pull", "-q", cwd=work)
    assert (work / "src" / "sla.js").is_file()

    # A viewer clones, but cannot push.
    (tmp_path / "v").mkdir()
    venv = _git_env(tmp_path / "v", viewer_token)
    vwork = tmp_path / "vwork"
    _git(venv, "clone", url, str(vwork))
    _git(venv, "switch", "-c", "viewer/try", cwd=vwork)
    _git(venv, "commit", "-q", "--allow-empty", "-m", "try", cwd=vwork)
    vpush = _git(venv, "push", "origin", "viewer/try", cwd=vwork, check=False)
    assert vpush.returncode != 0
    assert local_repo.branch_sha(bare, "viewer/try") is None


def test_a_branch_a_run_is_building_is_refused(server, engine, world, repo_root, tmp_path):
    pid, bare = _project(engine, world)
    run_id = f"crun_busy{uuid.uuid4().hex[:6]}"
    local_repo.ensure_branch(bare, f"boss/code/{run_id}")
    with engine.begin() as db:
        owner = db.execute(text("SELECT owner, name FROM github_repository_bindings WHERE "
                                "project_id = :p"), {"p": pid}).mappings().one()
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                        "finalize_ctx) VALUES (:id, :p, 'x', 'running', 'coding', CAST(:s AS JSONB), "
                        "'{}'::jsonb)"),
                   {"id": run_id, "p": pid,
                    "s": json.dumps({"owner": owner["owner"], "repo": owner["name"]})})
    token = _token(engine, world, world.owner, ["repository:read", "repository:push"])
    (tmp_path / "o").mkdir()
    env = _git_env(tmp_path / "o", token)
    work = tmp_path / "w"
    _git(env, "clone", "-b", f"boss/code/{run_id}", f"{server}/git/{pid}.git", str(work))
    _git(env, "commit", "-q", "--allow-empty", "-m", "mine", cwd=work)
    out = _git(env, "push", cwd=work, check=False)
    assert out.returncode != 0 and "building this branch" in out.stderr
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET status = 'succeeded' WHERE id = :r"),
                   {"r": run_id})
    out = _git(env, "push", cwd=work, check=False)
    assert out.returncode == 0, out.stderr


def test_git_notes_become_comments_in_the_one_store(server, engine, world, repo_root, tmp_path,
                                                    monkeypatch):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)
    pid, bare = _project(engine, world)
    token = _token(engine, world, world.owner, ["repository:read", "repository:push"])
    (tmp_path / "o").mkdir()
    env = _git_env(tmp_path / "o", token)
    work = tmp_path / "w"
    _git(env, "clone", f"{server}/git/{pid}.git", str(work))
    _git(env, "switch", "-c", "feature/notes", cwd=work)
    (work / "src" / "app.js").write_text("changed\n" + (work / "src" / "app.js").read_text())
    _git(env, "commit", "-qam", "notes branch", cwd=work)
    _git(env, "push", "-u", "origin", "feature/notes", cwd=work)
    note = json.dumps({"path": "src/app.js", "line": 1, "body": "Log when the timer is skipped",
                       "severity": "must"})
    _git(env, "notes", "--ref=boss-review", "add", "-m", note, "HEAD", cwd=work)
    out = _git(env, "push", "origin", "refs/notes/boss-review", cwd=work)
    assert "read 1 comment from git notes" in out.stderr, out.stderr
    _git(env, "push", "origin", "refs/notes/boss-review", cwd=work, check=False)   # no duplicate
    with engine.begin() as db:
        row = local_review.change_row(db, pid, 1)
        threads = local_review.threads(db, row["id"])
        exported = local_review.export_notes(db, get_settings(), row)
    assert [(t["origin"], t["path"], t["line"], t["severity"]) for t in threads] == [
        ("git_notes", "src/app.js", 1, "major")]
    assert exported == 1
    shown = local_repo.git(["log", "-1", "--notes=boss-review", "--format=%N", "feature/notes"],
                           git_dir=bare)
    assert "Log when the timer is skipped" in shown and '"from": "boss"' in shown


def test_boss_clone_keeps_the_token_out_of_urls_and_config_and_boss_review_writes_the_store(
        server, engine, world, repo_root, tmp_path, monkeypatch):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)
    pid, bare = _project(engine, world)
    local_repo.ensure_branch(bare, "feature/cli")
    local_repo.commit_files(bare, "feature/cli", {"src/app.js": "changed\n"}, "cli change")
    from boss_engine.services.local_repo_host import LocalGitAdapter

    owner_name = f"local_{pid}"
    LocalGitAdapter(get_settings(), owner=owner_name, repo="helpdesk", project_id=pid
                    ).create_pull_request("feature/cli", "CLI change", "")
    token = _token(engine, world, world.owner, ["repository:read", "repository:push",
                                                 "code_task:read", "code_task:steer"])
    install = tmp_path / "install"
    (install / "scripts" / "install").mkdir(parents=True)
    (install / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    port = server.rsplit(":", 1)[-1]
    (install / ".env").write_text(f"BOSS_API_PORT={port}\n")
    creds = tmp_path / "creds"
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "BOSS_TOKEN": token,
           "BOSS_GIT_REMOTE_BASE": server, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "BOSS_GIT_CREDENTIAL_HELPER": f"store --file={creds}"}
    dest = tmp_path / "code" / pid
    done = subprocess.run(["bash", str(ROOT / "scripts/install/boss.sh"), "--dir", str(install),
                           "clone", pid, str(dest), "--branch", "feature/cli"],
                          env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    assert (dest / "src" / "app.js").read_text() == "changed\n"
    assert token not in done.stdout + done.stderr
    config = (dest / ".git" / "config").read_text()
    assert token not in config and "store --file" in config and f"/git/{pid}.git" in config
    assert token in creds.read_text()                       # the helper holds it, nothing else
    with engine.begin() as db:
        copies = db.execute(text("SELECT path, branch FROM local_working_copies WHERE "
                                 "project_id = :p"), {"p": pid}).all()
    assert copies == [(str(dest), "feature/cli")]

    # `boss review` inside the working copy: the project from its remote, the token the helper kept.
    renv = {k: v for k, v in env.items() if k != "BOSS_TOKEN"}
    said = subprocess.run(["bash", str(ROOT / "scripts/install/boss.sh"), "--dir", str(install),
                           "review", "comment", "1", "src/app.js:1", "Log when the timer is skipped"],
                          cwd=dest, env=renv, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL)
    assert said.returncode == 0, said.stderr
    assert said.stdout.startswith("✓ comment ") and "src/app.js:1 · must fix · open" in said.stdout
    listed = subprocess.run(["bash", str(ROOT / "scripts/install/boss.sh"), "--dir", str(install),
                             "review", "list", "1"], cwd=dest, env=renv, capture_output=True,
                            text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert "open" in listed.stdout and "Log when the timer" in listed.stdout
    with engine.begin() as db:
        t = local_review.threads(db, local_review.change_row(db, pid, 1)["id"])
    assert [(x["origin"], x["author_kind"]) for x in t] == [("cli", "human")]
