"""A LOCAL REPOSITORY HAS NO HOSTED CI (BOSSAPP-946, finding 1 — the blocker).

The local-first demo (helpdesk-laptop, build crun_6f61e082fd3d476fa0e0) built and proposed change
#1, then waited 30 minutes for CI checks that a repository on this machine can never get: the
local adapter's workflow probe raised an error type ci_gate did not recognise, and ci_gate read
"unknown" as "has CI". The wait timed out and the merge (dec_fe5e712c44f54a4d9954) and staging
were refused.

Pinned here, on the real local adapter, real git and the real database:
  * has_workflows is decided by the HOST's capability (LocalGitAdapter.has_hosted_ci), not by
    catching one error type;
  * an unknown error is never "has CI" (nor "no CI"): it becomes a terminal, fail-closed
    `unreadable` after the grace period, never a 30-minute wait;
  * a local build's CI wait ends at once, its merge is allowed and the staging deploy is not
    refused by CI, in the build pipeline and in a workflow's wait_for_ci step;
  * a wait recorded BEFORE this fix (timed_out, has_ci true) clears on the next look through
    the code (the Approve press, a recheck), with no hand edit of the database.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import ci_gate, local_projects, local_repo
from boss_engine.services.local_repo_host import LocalGitAdapter


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


def _project(engine) -> str:
    pid = f"lfci-{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:
        ws = db.execute(text("SELECT id FROM workspaces ORDER BY created_at LIMIT 1")).scalar()
        if ws is None:
            ws = f"ws_{pid}"
            db.execute(text("INSERT INTO workspaces (id, name) VALUES (:id, :id)"), {"id": ws})
        db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                        "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
    return pid


def _built_change(engine, repo_root) -> SimpleNamespace:
    """A local project whose build committed a stage and opened change #1."""
    settings = get_settings()
    pid = _project(engine)
    with engine.begin() as db:
        bound = local_projects.create_for_project(db, settings, pid, name="Help Desk", actor="t")
    owner, repo = bound["owner"], bound["repo"]
    run_id = f"crun_lfci{uuid.uuid4().hex[:8]}"
    branch = f"boss/code/{run_id}"
    adapter = LocalGitAdapter(settings, owner=owner, repo=repo, project_id=pid)
    adapter.ensure_branch(branch, "main")
    adapter.commit_files(branch, {"server.js": "console.log('desk')\n"}, "Build: the desk")
    pr = adapter.create_pull_request(branch, "Help desk", "body", run_id=run_id)
    sha = local_repo.branch_sha(adapter.git_dir, branch)
    return SimpleNamespace(pid=pid, repository=f"{owner}/{repo}", adapter=adapter, url=pr.url,
                           number=pr.number, sha=sha, run_id=run_id, branch=branch)


def _never_sleep(_seconds: float) -> None:
    raise AssertionError("a local repository's CI wait must end at once, not poll")


# ── the class: decided by the host's capability; unknown is never "has CI" ───────────────────

def test_the_host_capability_decides_whether_there_is_ci(repo_root):
    local = LocalGitAdapter(get_settings(), owner="local_x", repo="y", project_id="x")
    assert local.has_hosted_ci is False
    reader = ci_gate.AdapterCiReader(local)
    assert ci_gate.hosted_ci(reader) is False
    assert reader.has_workflows("a" * 40) is False
    assert ci_gate._safe_has_workflows(reader, "a" * 40) is False  # noqa: SLF001
    # A GitHub repository's mirror during an outage is still GitHub: its CI runs after the push.
    mirror = LocalGitAdapter(get_settings(), owner="acme", repo="shop", project_id="x",
                             pending_push=True)
    assert ci_gate.hosted_ci(ci_gate.AdapterCiReader(mirror)) is True
    # A reader that does not say is hosted (GitHub).
    assert ci_gate.hosted_ci(SimpleNamespace()) is True
    # The blocking wait answers at once for a host without CI.
    out = ci_gate.wait_for_ci(reader, "a" * 40, sleep=_never_sleep, repository="local_x/y")
    assert out.state == "no_ci" and "sandbox" in out.summary


class _Odd(Exception):
    """An error type nobody in ci_gate knows."""


class _HostedButUnreadable:
    def check_runs(self, sha):
        return []

    def statuses(self, sha):
        return []

    def has_workflows(self, sha):
        raise _Odd("weird")


def test_an_unknown_error_never_means_has_ci_and_never_waits_30_minutes():
    reader = _HostedButUnreadable()
    assert ci_gate._safe_has_workflows(reader, "b" * 40) is None  # noqa: SLF001
    # Unknown: still looked at for the grace period (a real CI may report) ...
    early = ci_gate.settle(ci_gate.CiOutcome("waiting", "No CI checks have reported yet.", []),
                           elapsed=10, timeout=1800, has_ci=None, repository="acme/shop")
    assert early.state == "waiting"
    # ... then terminal and FAIL CLOSED (unreadable, not cleared), not a 30-minute wait.
    late = ci_gate.settle(ci_gate.CiOutcome("waiting", "No CI checks have reported yet.", []),
                          elapsed=ci_gate.NO_CI_GRACE_SECONDS, timeout=1800, has_ci=None,
                          repository="acme/shop")
    assert late.state == "unreadable" and late.state not in ci_gate.CLEARED
    assert late.summary.startswith(ci_gate.UNREADABLE_PREFIX) and "acme/shop" in late.summary
    # A check that reports while unknown is still waited on (real CI is never skipped).
    clock = iter(range(0, 10_000, 30))
    looks = [[], [{"name": "ci", "status": "completed", "conclusion": "success", "id": 1}]]

    class Reporting(_HostedButUnreadable):
        def check_runs(self, sha):
            return looks.pop(0) if looks else [{"name": "ci", "status": "completed",
                                                 "conclusion": "success", "id": 1}]

    got = ci_gate.wait_for_ci(Reporting(), "b" * 40, sleep=lambda s: None,
                              clock=lambda: next(clock))
    assert got.state == "passed"


# ── the run: no CI wait, merge allowed, staging not refused ─────────────────────────────────

def test_a_local_build_has_no_ci_wait_its_merge_is_allowed_and_staging_runs(engine, repo_root):
    from boss_engine.services import governed_code_task_service as gcts
    from boss_engine.services import workflow_capability_runner as wcr

    built = _built_change(engine, repo_root)
    settings = get_settings()
    sf = SimpleNamespace(github=built.adapter)
    proposal = {"pull_request_number": built.number, "pull_request_url": built.url,
                # even a build that adds a workflow file: the host runs no workflows
                "files_changed": [{"path": ".github/workflows/ci.yml"}]}
    out = gcts._wait_for_ci(engine, settings, sf, proposal, built.pid, built.run_id,  # noqa: SLF001
                            built.repository, sleep=_never_sleep)
    assert out["status"] == "no_ci", out
    assert "sandbox" in out["summary"]

    # A workflow's wait_for_ci step ends at once too (it parks on nothing).
    with engine.begin() as db:
        step = wcr._start_ci_wait(db, built.pid, {"run_id": "wfr_lfci"},  # noqa: SLF001
                                  {"pull_request_url": built.url, "head_sha": built.sha})
        wait = ci_gate.get_wait(db, step["ci_wait_id"])
    assert step["ci_status"] == "no_ci" and wait["status"] == "no_ci"
    assert wait["has_ci"] is False and wait["blocks_deploy"] is False

    # The merge decision and the staging deploy are not refused by CI.
    merge = {"project_id": built.pid, "recommendation": {
        "action": "review_pull_request", "pull_request_url": built.url, "head_sha": built.sha}}
    deploy = {"project_id": built.pid, "approved_by": "a-person", "recommendation": {
        "action": "review_deployment", "pull_request_url": built.url, "head_sha": built.sha}}
    with engine.begin() as db:
        ci_gate.refuse_approval(db, merge, "a-person")
        ci_gate.refuse_approval(db, deploy, "a-person")
        ci_gate.refuse_effect(db, built.pid, deploy)


# ── the live decision: a wait recorded before the fix recovers through the code ──────────────

def test_a_timed_out_wait_from_before_the_fix_clears_on_the_next_approve(engine, repo_root,
                                                                          monkeypatch):
    built = _built_change(engine, repo_root)
    then = datetime.now(timezone.utc) - timedelta(hours=1)
    wait_id = f"ciw_lfci{uuid.uuid4().hex[:8]}"
    with engine.begin() as db:   # exactly what ciw_eb3c6f7bdbb24429955f looks like
        db.execute(text(
            "INSERT INTO ci_waits (id, project_id, repository, head_sha, pull_request_url, "
            "code_task_run_id, status, has_ci, summary, timeout_seconds, deadline_at, "
            "next_poll_at, finished_at, created_by, created_at, updated_at) VALUES (:id, :p, :r, "
            ":s, :u, :ct, 'timed_out', true, 'CI did not finish in 30 min: no checks were "
            "reported for this commit.', 1800, :d, :d, :d, 'boss', :t, :d)"),
            {"id": wait_id, "p": built.pid, "r": built.repository, "s": built.sha,
             "u": built.url, "ct": built.run_id, "t": then, "d": then + timedelta(minutes=30)})
    merge = {"project_id": built.pid, "recommendation": {
        "action": "review_pull_request", "pull_request_url": built.url, "head_sha": built.sha}}
    # Before: this refusal is what the operator saw.
    with engine.begin() as db:
        assert ci_gate.latest_wait(db, built.pid, pull_request_url=built.url)["status"] == \
            "timed_out"
    # The Approve press runs refuse_approval, which re-reads a RECHECKABLE wait through the
    # project's reader; the local reader's answer is the host's: no hosted CI.
    with engine.begin() as db:
        ci_gate.refuse_approval(db, merge, "a-person")
        after = ci_gate.get_wait(db, wait_id)
        events = db.execute(text("SELECT data FROM events WHERE entity_id = :w AND "
                                 "event_type = 'ci.no_ci'"), {"w": wait_id}).scalars().all()
    assert after["status"] == "no_ci" and after["has_ci"] is False, after
    assert "sandbox" in after["summary"]
    payload = events[0] if isinstance(events[0], dict) else json.loads(events[0])
    assert payload["was"] == "timed_out"

