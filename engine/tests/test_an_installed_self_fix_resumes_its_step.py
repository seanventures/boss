"""The whole loop through the runner (BOSSAPP-571 R1, R5–R7, spec §5.2 Stages 1–12, F18, decision
4): a BOSS-side build failure → a self-fix → Mender's red-then-green fix → every gate → a person
approves → a signed ticket → the host self-installer installs it → BOSS restarts into the stack
that holds it → the parked step resumes → the verdict:

  passes                       → verified, the recommendation `implemented`
  fails for a DIFFERENT cause  → verified (a new problem for the recovery loop)
  fails for the SAME cause     → not resolved: rolled back, the recommendation reopens

Real: the runner, the recovery loop, the pipeline, git, pytest, the gates, the decision, the
ticket signature, the installer script. Fake: Docker and the HTTP edge of the API (its routes
call the real engine functions), and the restart (the test sets BOSS_STACK_HEAD as the restarted
process has it)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import boss_recommendations
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import step_recovery as sr
from boss_engine.services import workflow_capability_runner as runner

from ._self_fix_support import (FakeHost, Worker, approve, failing_build_step, harness,  # noqa: F401
                                self_fix_on)
from .test_a_failed_step_stops_the_run import _insert_awaited, _steps
from .test_workflow_capability_runner import eng, project  # noqa: F401


def _restart_into(monkeypatch, head: str) -> None:
    """What the restarted API and worker are: the stack head the installer started them with."""
    monkeypatch.setenv("BOSS_STACK_HEAD", head)
    monkeypatch.setattr(sr, "_BUILD_ID", None)


@pytest.fixture()
def installed(harness, monkeypatch):
    """Steps 1–9: the failure, the self-fix, the card, the approval, the install."""
    harness.script("good_fix")
    with harness.eng.begin() as c:
        run, first_build = failing_build_step(harness, c, monkeypatch)
        sfx_id = _steps(c, run["id"])["s"]["output"]["self_fix_id"]
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx_id,
                               source=harness.mirror).run()
    assert out["status"] == sf.AWAITING_APPROVAL, out
    approve(harness, sfx_id)
    host = FakeHost(harness)
    done = host.installer("watch", "--once")
    assert "installed" in done.stdout, done.stdout + done.stderr
    with harness.eng.begin() as c:
        row = sf.get(c, sfx_id)
    assert row["status"] == sf.INSTALLED
    started: list[str] = []

    def start(connection, ctx, *, project_id, run_id, step_id, build_run_id, note):
        new_id = f"crun_{uuid.uuid4().hex[:10]}"
        _insert_awaited(connection, project_id, "code_task_runs", new_id, "running")
        started.append(note)
        return new_id, ""

    monkeypatch.setattr(runner, "_start_recovery_build", start)
    yield {"run": run, "sfx_id": sfx_id, "host": host, "row": row, "started": started}
    host.close()


def _tick(harness):
    with harness.eng.begin() as c:
        runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)
        return c


def _resume(harness, installed, monkeypatch):
    # Before the restart the running build is not the stack that holds the fix: it waits.
    with harness.eng.begin() as c:
        runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)
        step = _steps(c, installed["run"]["id"])["s"]
        assert step["output"]["await"]["kind"] == sf.AWAIT_KIND
    _restart_into(monkeypatch, installed["row"]["fix_commit_sha"])
    with harness.eng.begin() as c:
        runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)
        step = _steps(c, installed["run"]["id"])["s"]
        assert sf.get(c, installed["sfx_id"])["status"] == sf.VERIFYING
    assert installed["started"], "the build step was started again on the fixed BOSS"
    assert step["output"]["await"]["kind"] == "code_task"
    return step["output"]["await"]["id"]


def _finish(c, build_id: str, status: str, error: str = "") -> None:
    c.execute(text("UPDATE code_task_runs SET status = :s, error = :e, phase = :ph, "
                   "pull_request_url = 'https://github.com/example/example-app/pull/1' "
                   "WHERE id = :i"),
              {"s": status, "e": error, "i": build_id,
               "ph": "done" if status == "completed" else "failed"})


def _settle(harness, times: int = 3):
    for _ in range(times):
        with harness.eng.begin() as c:
            runner.tick(c, Worker(harness.eng, harness.settings), project_id=harness.project)


def test_the_step_passing_verifies_the_fix(harness, installed, monkeypatch):
    rebuilt = _resume(harness, installed, monkeypatch)
    monkeypatch.setattr(runner, "_terminal_evidence", lambda *a, **k: {})
    from boss_engine.services import run_fencing

    monkeypatch.setattr(run_fencing, "build_not_passed", lambda *a, **k: None)
    with harness.eng.begin() as c:
        _finish(c, rebuilt, "completed")
    _settle(harness)
    with harness.eng.begin() as c:
        row = sf.get(c, installed["sfx_id"])
        step = _steps(c, installed["run"]["id"])["s"]
        rec = boss_recommendations.list_recommendations(c, harness.project)[0]
        events = [r[0] for r in c.execute(text(
            "SELECT event_type FROM events WHERE data->>'self_fix_id' = :i ORDER BY created_at"),
            {"i": installed["sfx_id"]}).all()]
    assert step["status"] == "succeeded"
    assert row["status"] == sf.VERIFIED and row["verification"]["outcome"] == "passed"
    assert rec["status"] == "implemented"
    for expected in ("self_fix.proposed", "self_fix.claimed", "self_fix.reproduced",
                     "self_fix.awaiting_approval", "self_fix.approved", "self_fix.ticket_issued",
                     "self_fix.installed", "self_fix.verifying", "self_fix.verified"):
        assert expected in events, expected


def test_a_different_cause_after_the_install_verifies_the_fix(harness, installed, monkeypatch):
    rebuilt = _resume(harness, installed, monkeypatch)
    with harness.eng.begin() as c:
        _finish(c, rebuilt, "failed", "worker lost: connection reset by peer")
    _settle(harness)
    with harness.eng.begin() as c:
        row = sf.get(c, installed["sfx_id"])
    assert row["status"] == sf.VERIFIED
    assert row["verification"]["outcome"] == "new_cause"
    assert row["verification"]["next_signature"].startswith("build:transient")


def test_the_same_cause_after_the_install_rolls_it_back(harness, installed, monkeypatch):
    rebuilt = _resume(harness, installed, monkeypatch)
    with harness.eng.begin() as c:
        _finish(c, rebuilt, "failed",
                "the planner read the repository root instead of the app directory")
    _settle(harness)
    with harness.eng.begin() as c:
        row = sf.get(c, installed["sfx_id"])
        tickets = [dict(r) for r in c.execute(text(
            "SELECT kind, status FROM self_fix_tickets WHERE self_fix_id = :i ORDER BY "
            "created_at"), {"i": installed["sfx_id"]}).mappings()]
    assert row["status"] == sf.ROLLING_BACK and row["verification"]["outcome"] == "same_cause"
    assert tickets[-1] == {"kind": "rollback", "status": "issued"}
    done = installed["host"].installer("watch", "--once")
    assert "rolled_back" in done.stdout, done.stdout + done.stderr
    _restart_into(monkeypatch, harness.base)
    _settle(harness, times=2)
    with harness.eng.begin() as c:
        row = sf.get(c, installed["sfx_id"])
        rec = boss_recommendations.list_recommendations(c, harness.project)[0]
        step = _steps(c, installed["run"]["id"])["s"]
    assert row["status"] == sf.ROLLED_BACK
    assert installed["host"].running_head() == installed["host"].release
    assert installed["host"].stack_ref() == ""
    assert rec["status"] == "open"
    assert [a["outcome"] for a in rec["attempts"]][:1] == [sf.NOT_RESOLVED]
    # The step does not wait on a fix that did not hold: today's wait for a BOSS update.
    assert step["output"]["await"]["kind"] == sr.AWAIT_KIND
