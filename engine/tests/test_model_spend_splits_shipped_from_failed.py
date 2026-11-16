"""One window of model spend splits into shipped, failed-or-retried, and in progress."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.spend_outcome import spend_outcome

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 8, tzinfo=timezone.utc)
INSIDE = SINCE + timedelta(days=2)

KEYS = (
    "project_id", "since", "until",
    "shipped_usd_micros", "failed_or_retried_usd_micros", "in_progress_usd_micros",
)


def _run(c, pid: str, status: str = "running") -> str:
    rid = f"crun_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status) "
        "VALUES (:i, :p, 'build', :s)"), {"i": rid, "p": pid, "s": status})
    return rid


def _pr(c, pid: str, run_id: str | None, *, merged: bool) -> None:
    change_set = f"chs_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO repo_change_sets (id, project_id, app_key, title, mode, state) "
        "VALUES (:i, :p, 'app', 'change', 'single', 'in_review')"),
        {"i": change_set, "p": pid})
    c.execute(text(
        "INSERT INTO change_set_prs "
        "(id, change_set_id, full_name, code_task_run_id, merged_at) "
        "VALUES (:i, :c, :n, :r, :m)"),
        {"i": f"csp_{uuid4().hex[:10]}", "c": change_set,
         "n": f"acme/repo-{uuid4().hex[:8]}", "r": run_id,
         "m": INSIDE if merged else None})


def _deploy(c, pid: str, run_id: str | None, status: str) -> None:
    target = f"tgt_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, created_by) VALUES (:i, :p, :a, 'acme/app', 'staging', "
        "'stack', 'app', 'test')"),
        {"i": target, "p": pid, "a": f"app-{uuid4().hex[:8]}"})
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
        "commit_sha, status, created_by, code_task_run_id) VALUES (:i, :p, :t, 'acme/app', "
        "'main', :sha, :s, 'test', :r)"),
        {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": target,
         "sha": uuid4().hex, "s": status, "r": run_id})


def _call(c, pid: str, *, cost: int, at: datetime, run_id: str | None = None,
          ok: bool = True, retries: int = 0, included: bool = False) -> None:
    c.execute(text(
        "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
        "cost_usd_micros, retries, ok, included_in_subscription, created_at) "
        "VALUES (:i, :p, :r, 'implement', 'claude_code', 'claude-sonnet', :cost, :n, :ok, "
        ":inc, :at)"),
        {"i": f"llmc_{uuid4().hex[:12]}", "p": pid, "r": run_id, "cost": cost,
         "n": retries, "ok": ok, "inc": included, "at": at})


def _outcome(eng, pid: str) -> dict:
    with eng.begin() as c:
        return spend_outcome(c, pid, SINCE, UNTIL)


def test_a_window_splits_each_call_into_one_bucket(eng, project):
    """Shipped (merged PR or healthy deploy) wins. Otherwise a failed call, a retry, or a
    failed run is failed-or-retried. The rest of the half-open window is still in progress.
    A subscription row is zero money. Two ship rows do not count one call twice."""
    with eng.begin() as c:
        merged = _run(c, project, "completed")
        _pr(c, project, merged, merged=True)
        _pr(c, project, merged, merged=True)
        _call(c, project, cost=100_000, at=INSIDE, run_id=merged)
        _call(c, project, cost=250_000, at=INSIDE, run_id=merged, ok=False, retries=4)

        healthy = _run(c, project, "completed")
        _deploy(c, project, healthy, "healthy")
        _deploy(c, project, healthy, "healthy")
        _call(c, project, cost=200_000, at=INSIDE, run_id=healthy)

        both = _run(c, project, "failed")
        _pr(c, project, both, merged=True)
        _pr(c, project, both, merged=False)
        _deploy(c, project, both, "healthy")
        _deploy(c, project, both, "failed")
        _call(c, project, cost=400_000, at=INSIDE, run_id=both, ok=False, retries=2)

        open_run = _run(c, project, "running")
        _pr(c, project, open_run, merged=False)
        _deploy(c, project, open_run, "running")
        _call(c, project, cost=1_000_000, at=INSIDE, run_id=open_run, ok=False)
        _call(c, project, cost=2_000_000, at=INSIDE, run_id=open_run, retries=1)
        _call(c, project, cost=16_000_000, at=INSIDE, run_id=open_run)

        failed_run = _run(c, project, "failed")
        _call(c, project, cost=4_000_000, at=INSIDE, run_id=failed_run)
        _call(c, project, cost=8_000_000, at=INSIDE, run_id=failed_run, ok=False, retries=3)

        blocked = _run(c, project, "blocked")
        _call(c, project, cost=32_000, at=INSIDE, run_id=blocked)

        _call(c, project, cost=32_000_000, at=INSIDE)
        _pr(c, project, None, merged=True)

        _call(c, project, cost=64_000_000, at=SINCE)
        _call(c, project, cost=4_096_000, at=UNTIL - timedelta(microseconds=1))
        _call(c, project, cost=1_024_000, at=SINCE - timedelta(seconds=1), run_id=merged)
        _call(c, project, cost=2_048_000, at=UNTIL, run_id=merged)

        _call(c, project, cost=128_000_000, at=INSIDE, run_id=merged, included=True)
        _call(c, project, cost=256_000_000, at=INSIDE, ok=False, included=True)

    out = _outcome(eng, project)
    assert tuple(out) == KEYS
    assert out["project_id"] == project
    assert out["since"] == SINCE and out["until"] == UNTIL
    assert out["shipped_usd_micros"] == 100_000 + 250_000 + 200_000 + 400_000
    assert out["failed_or_retried_usd_micros"] == 1_000_000 + 2_000_000 + 4_000_000 + 8_000_000
    assert out["in_progress_usd_micros"] == (
        16_000_000 + 32_000 + 32_000_000 + 64_000_000 + 4_096_000)
    assert all(type(out[key]) is int for key in KEYS[3:])


def test_another_projects_calls_are_absent(eng, project):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            run = _run(c, other, "failed")
            _pr(c, other, run, merged=True)
            _deploy(c, other, run, "healthy")
            _call(c, other, cost=512_000_000, at=INSIDE, run_id=run, ok=False, retries=9)
            _call(c, project, cost=7_000, at=INSIDE)
        out = _outcome(eng, project)
    finally:
        purge(eng, other)
    assert out["shipped_usd_micros"] == 0
    assert out["failed_or_retried_usd_micros"] == 0
    assert out["in_progress_usd_micros"] == 7_000


def test_an_empty_window_is_zero_micros(eng, project):
    assert _outcome(eng, project) == {
        "project_id": project,
        "since": SINCE,
        "until": UNTIL,
        "shipped_usd_micros": 0,
        "failed_or_retried_usd_micros": 0,
        "in_progress_usd_micros": 0,
    }
