"""`boss qa fault`, `boss qa report` and `boss qa diagnostics` against the REAL API and git remote.

The real scripts/install/boss.sh and qa_fault.py, the real API on a loopback port with identity
enforcing, real bare repositories behind BOSS's git remote, and a person's real token (as
test_local_git_remote.py runs `boss clone`). No Docker, no model.

  * a fault is pushed on test/fault-<case> as a change BOSS opened, its commit carries the
    `BOSS-Fault:` trailer, and nothing reached the default branch;
  * `--revert` deletes an unmerged fault branch (its change closes), and after a merge pushes a
    revert branch instead;
  * a project BOSS says ships to production is refused before anything is cloned;
  * the ledger reads BOSS's own records into the plan's row: case, runs, failure seen, green or
    draft, time to fix, human actions, unattended, who acted, final state — free text scrubbed.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.config import get_settings
from boss_engine.services import local_projects, local_repo, qa_ledger

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/e2e/fixtures/plain-node/plain"
SCOPES = ["project:read", "repository:read", "repository:push", "code_task:read",
          "autonomy:read", "deployment:read"]


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("qa-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    monkeypatch.setenv("BOSS_LOCAL_REPO_ROOT", str(root))
    return root


@pytest.fixture(scope="module")
def server():
    """The real API on a free loopback port, the identity guard enforcing. A loaded machine
    takes a while to run the lifespan: wait for it rather than test a server that is not up."""
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
    deadline = time.monotonic() + 180
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.1)
    assert srv.started, "the API did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)
    settings.authz_mode, settings.authz_router_modes = saved


def _token(engine, world) -> str:
    from boss_engine.identity import api_keys

    with engine.begin() as db:
        return api_keys.create(db, creator=world.ctx(world.owner), owner=world.owner, kind="pat",
                               name=f"qa {uuid.uuid4().hex[:6]}", scopes=SCOPES).key


def _project(engine, world, name: str) -> tuple[str, Path]:
    """A local project holding the plain Node sample app at its root."""
    pid = f"qa-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": name})
        bound = local_projects.create_for_project(db, get_settings(), pid, name="recipes",
                                                  actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    files = {p.relative_to(FIXTURE).as_posix(): p.read_text()
             for p in FIXTURE.rglob("*") if p.is_file()}
    local_repo.commit_files(bare, "main", files, "the sample app")
    return pid, bare


def _install(tmp_path: Path, server: str) -> Path:
    install = tmp_path / "install"
    (install / "scripts" / "install").mkdir(parents=True)
    (install / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    (install / ".env").write_text(f"BOSS_API_PORT={server.rsplit(':', 1)[-1]}\n")
    return install


def _boss(install: Path, server: str, token: str, tmp_path: Path, *args: str, cwd=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "BOSS_TOKEN": token,
           "BOSS_GIT_REMOTE_BASE": server, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "BOSS_GIT_CREDENTIAL_HELPER": f"store --file={tmp_path / 'creds'}"}
    return subprocess.run(["bash", str(ROOT / "scripts/install/boss.sh"), "--dir", str(install),
                           "qa", *args], env=env, capture_output=True, text=True, timeout=180,
                          stdin=subprocess.DEVNULL, cwd=cwd or tmp_path)


def _git(bare: Path, *args: str) -> str:
    return subprocess.run(["git", "--git-dir", str(bare), *args], capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def no_consent(monkeypatch):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda **kw: None)


def test_a_fault_is_pushed_as_a_change_and_revert_closes_it(server, engine, world, repo_root,
                                                            tmp_path, no_consent):
    pid, bare = _project(engine, world, f"Northgate QA {uuid.uuid4().hex[:4]}")
    token = _token(engine, world)
    install = _install(tmp_path, server)
    main_before = _git(bare, "rev-parse", "main").strip()

    done = _boss(install, server, token, tmp_path, "fault", "REC-01", "--project", pid)
    assert done.returncode == 0, done.stdout + done.stderr
    assert token not in done.stdout + done.stderr
    assert "ENT-REC-01" in done.stdout and "Merge anyway" in done.stdout
    assert "--revert REC-01" in done.stdout
    assert "test/fault-rec-01" in _git(bare, "branch", "--list")
    message = _git(bare, "log", "-1", "--format=%B", "test/fault-rec-01")
    assert "BOSS-Fault: REC-01" in message and "BOSS-Fault-App: ." in message
    changed = _git(bare, "diff", "--name-only", "main", "test/fault-rec-01").split()
    assert changed == ["test/slug.test.js"]
    assert _git(bare, "rev-parse", "main").strip() == main_before, "never the default branch"
    with engine.begin() as db:
        change = db.execute(text("SELECT state, branch FROM local_change_sets WHERE project_id = "
                                 ":p"), {"p": pid}).mappings().all()
    assert [dict(c) for c in change] == [{"state": "open", "branch": "test/fault-rec-01"}]

    again = _boss(install, server, token, tmp_path, "fault", "REC-01", "--project", pid)
    assert again.returncode != 0 and "already exists" in again.stderr

    back = _boss(install, server, token, tmp_path, "fault", "--revert", "REC-01", "--project", pid)
    assert back.returncode == 0, back.stdout + back.stderr
    assert "never merged" in back.stdout
    assert "test/fault-rec-01" not in _git(bare, "branch", "--list")
    with engine.begin() as db:
        state = db.execute(text("SELECT state FROM local_change_sets WHERE project_id = :p"),
                           {"p": pid}).scalar()
    assert state == "closed"


def test_a_merged_fault_is_put_back_by_a_revert_change(server, engine, world, repo_root,
                                                       tmp_path, no_consent):
    pid, bare = _project(engine, world, f"Lumen QA {uuid.uuid4().hex[:4]}")
    token = _token(engine, world)
    install = _install(tmp_path, server)
    assert _boss(install, server, token, tmp_path, "fault", "9", "--project", pid).returncode == 0
    # The tester merges it on purpose, as the change page does: BOSS's own merge, with the
    # install's default method (a squash, which drops the commit's BOSS-Fault trailer).
    from boss_engine.services.local_repo_host import LocalGitAdapter

    settings = get_settings()
    assert settings.sandbox_merge_method == "squash"
    with engine.begin() as db:
        number = db.execute(text("SELECT number FROM local_change_sets WHERE project_id = :p "
                                 "AND branch = 'test/fault-rec-09'"), {"p": pid}).scalar()
    merged = LocalGitAdapter(settings, owner=f"local_{pid}", repo="recipes", project_id=pid
                             ).merge_pull_request(int(number), "Merge test fault REC-09")
    assert merged.merged
    assert "BOSS-Fault" not in _git(bare, "log", "-1", "--format=%B", "main")
    assert "BOSS fault REC-09" in _git(bare, "show", "main:src/server.js")
    back = _boss(install, server, token, tmp_path, "fault", "--revert", "REC-09", "--project", pid)
    assert back.returncode == 0, back.stdout + back.stderr
    assert "test/fault-rec-09-revert" in back.stdout
    reverted = _git(bare, "show", "test/fault-rec-09-revert:src/server.js")
    assert "BOSS fault REC-09" not in reverted and reverted == (FIXTURE / "src/server.js").read_text()
    again = _boss(install, server, token, tmp_path, "fault", "--revert", "REC-09", "--project", pid)
    assert again.returncode != 0 and "already pushed" in again.stderr


def test_a_project_that_ships_to_production_is_refused_before_any_clone(server, engine, world,
                                                                        repo_root, tmp_path,
                                                                        no_consent):
    pid, bare = _project(engine, world, f"Prod QA {uuid.uuid4().hex[:4]}")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            "target_kind, target_ref, created_by) VALUES (:i, :p, 'recipes', 'recipes', "
            "'production', 'ssh', 'deploy@203.0.113.9', 't')"),
            {"i": f"dt_{uuid.uuid4().hex[:10]}", "p": pid})
    token = _token(engine, world)
    install = _install(tmp_path, server)
    done = _boss(install, server, token, tmp_path, "fault", "REC-01", "--project", pid)
    assert done.returncode != 0
    assert "production project" in done.stderr and "never breaks production" in done.stderr
    assert "test/fault-rec-01" not in _git(bare, "branch", "--list")


def test_an_unsupported_fault_pushes_nothing(server, engine, world, repo_root, tmp_path,
                                             no_consent):
    pid, bare = _project(engine, world, f"Plain QA {uuid.uuid4().hex[:4]}")
    token = _token(engine, world)
    install = _install(tmp_path, server)
    done = _boss(install, server, token, tmp_path, "fault", "REC-02", "--project", pid)
    assert done.returncode != 0 and "TypeScript" in done.stderr and "Nothing was pushed" in \
        done.stderr
    assert _git(bare, "branch", "--list").split() == ["*", "main"] or \
        "fault" not in _git(bare, "branch", "--list")


def test_the_case_list_needs_no_token(tmp_path, server):
    install = _install(tmp_path, server)
    done = _boss(install, server, "", tmp_path, "fault", "--list")
    assert done.returncode == 0 and "ENT-REC-14" in done.stdout


# ── the ledger ────────────────────────────────────────────────────────────────────────────────

def _seed_ledger(engine, pid: str) -> dict:
    """Two attempts at faults, as BOSS records them: REC-01 repaired unattended in one build;
    REC-09 failing twice, retried by a person, ending in a draft change with a follow-up."""
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    at = lambda minutes: t0 + timedelta(minutes=minutes)  # noqa: E731
    a, b, b2 = (f"crun_{uuid.uuid4().hex[:12]}" for _ in range(3))
    with engine.begin() as db:
        def run(rid, title, status, created, updated, fctx, error=None):
            db.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, finalize_ctx, "
                "error, created_at, updated_at) VALUES (:i, :p, :s, 'done', :t, "
                "CAST(:f AS jsonb), :e, :c, :u)"),
                {"i": rid, "p": pid, "s": status, "t": title, "f": json.dumps(fctx), "e": error,
                 "c": created, "u": updated})

        def event(kind, rid, when, actor="boss", data=None):
            db.execute(text(
                "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, "
                "data, created_at) VALUES (:i, :p, :k, :a, 'code_task_run', :r, "
                "CAST(:d AS jsonb), :w)"),
                {"i": f"evt_{uuid.uuid4().hex[:12]}", "p": pid, "k": kind, "a": actor, "r": rid,
                 "d": json.dumps(data or {}), "w": when})

        run(a, "Add a recipe counter (test/fault-rec-01 merged)", "completed", at(0), at(20),
            {"base_branch": "main", "work_branch": "test/fault-rec-01"})
        event("code_task.started", a, at(0), actor="usr_tester")
        event("code_task.inrun_convergence_round", a, at(6), data={"round": 1, "of": 3})
        event("code_task.pr_opened", a, at(18))
        run(b, "Fix the preview", "failed", at(30), at(40), {"branch": "test/fault-rec-09"},
            error="the app exited (code 1) before serving HTTP: boom at deploy@ops.example.com")
        event("code_task.app_serve_checked", b, at(35), data={"ok": False})
        run(b2, "Fix the preview (retry)", "completed", at(45), at(70),
            {"branch": "test/fault-rec-09", "parent_run_id": b, "root_run_id": b})
        event("code_task.started", b2, at(45), actor="usr_tester")
        event("code_task.inrun_convergence_round", b2, at(50), data={"round": 1, "of": 3})
        event("code_task.pr_opened_with_checks_failing", b2, at(68))
        event("code_task.deferred_findings_task", b2, at(69), data={"followup_run_id": "x"})
        # Work that names no case: it is never in the ledger, whatever --case says.
        run(f"crun_{uuid.uuid4().hex[:12]}", "Rename the page title", "completed", at(80),
            at(90), {"base_branch": "main"})
    return {"rec01": a, "rec09": [b, b2], "t0": t0}


def test_the_ledger_reads_each_fault_attempt_from_bosss_records(engine, world):
    pid = f"qa-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, 'Ledger', :id)"), {"id": pid, "ws": ws})
    seeded = _seed_ledger(engine, pid)
    with engine.begin() as db:
        out = qa_ledger.ledger(db, pid)
    rows = {r["case"]: r for r in out["rows"]}
    assert set(rows) == {"ENT-REC-01", "ENT-REC-09"}

    one = rows["ENT-REC-01"]
    assert one["runs"] == [seeded["rec01"]]
    assert one["failure_at"].startswith("2026-11-20T10:06")
    assert one["green_at"].startswith("2026-11-20T10:18") and one["fix_seconds"] == 12 * 60
    assert one["human_actions"] == 0 and one["unattended"] is True, "starting it is not a click"
    assert one["acted"] == ["build repair"] and one["final_state"] == "completed"
    assert one["diagnostics"] == f"boss qa diagnostics {seeded['rec01']}"

    nine = rows["ENT-REC-09"]
    assert nine["runs"] == seeded["rec09"], "a retry is the same attempt"
    assert nine["failure_at"].startswith("2026-11-20T10:35")
    assert nine["green_at"] is None and nine["draft_at"].startswith("2026-11-20T11:08")
    assert nine["fix_seconds"] is None
    assert nine["human_actions"] == 1 and nine["unattended"] is False, "the person's Retry"
    assert nine["acted"] == ["build repair", "follow-up build"]
    assert nine["final_state"] == "draft change · checks failing"

    # --case filters to the case's lineages; it never tags work that does not name the case.
    with engine.begin() as db:
        tagged = qa_ledger.ledger(db, pid, case="ent-rec-01")
        assert [r["runs"] for r in tagged["rows"]] == [[seeded["rec01"]]]
        assert qa_ledger.ledger(db, pid, case="REC-04")["rows"] == []
        since = qa_ledger.ledger(db, pid, since=seeded["t0"] + timedelta(minutes=25))
        assert [r["case"] for r in since["rows"]] == ["ENT-REC-09"]
    with engine.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, status, phase, title) "
                        "VALUES (:i, :p, 'completed', 'done', 'REC-04: Add a recipe counter')"),
                   {"i": f"crun_{uuid.uuid4().hex[:12]}", "p": pid})
        assert [r["case"] for r in qa_ledger.ledger(db, pid, case="REC-04")["rows"]] == \
            ["ENT-REC-04"], "the tester's New work names its case in its title"
    md = qa_ledger.as_markdown(out["rows"])
    assert md.count("\n") == 3 and "| ENT-REC-01 |" in md and "| Y |" in md and "| N |" in md


def test_the_ledger_masks_free_text_as_the_diagnostics_report_does(engine, world):
    pid = f"qa-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, 'Ledger mask', :id)"), {"id": pid, "ws": ws})
    seeded = _seed_ledger(engine, pid)
    with engine.begin() as db:
        db.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :i"),
                   {"i": seeded["rec09"][-1], "e": "checks failing: could not reach "
                    "deploy@ops.example.com (ssh), mail tester@example.org"})
        out = qa_ledger.ledger(db, pid, case="REC-09")
    said = json.dumps(out["rows"])
    assert out["rows"][0]["last_error"].startswith("checks failing: could not reach")
    assert "ops.example.com" not in said and "tester@example.org" not in said
    assert sum(out["masked"].values()) >= 1


def test_boss_qa_report_prints_the_ledger_over_the_api(server, engine, world, tmp_path):
    pid = f"qa-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world.project_a}).scalar()
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Report {pid[-4:]}"})
    seeded = _seed_ledger(engine, pid)
    token = _token(engine, world)
    install = _install(tmp_path, server)
    md = _boss(install, server, token, tmp_path, "report", "--project", pid)
    assert md.returncode == 0, md.stderr
    assert md.stdout.startswith("| Project | Case |") and "ENT-REC-09" in md.stdout
    assert token not in md.stdout + md.stderr
    js = _boss(install, server, token, tmp_path, "report", "--project", pid, "--json",
               "--case", "REC-01")
    assert js.returncode == 0, js.stderr
    rows = json.loads(js.stdout)["rows"]
    assert [r["runs"] for r in rows] == [[seeded["rec01"]]] and rows[0]["unattended"] is True
