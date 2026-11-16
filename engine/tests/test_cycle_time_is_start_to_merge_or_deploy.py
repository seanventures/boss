"""Cycle time is start to merge, and start to a healthy deploy, over a window.

Start is code_task_runs.created_at. Merge end is the earliest non-null merged_at
on change_set_prs or local_change_sets for that run. Deploy end is the earliest
healthy stack_deployments.finished_at. The window is half-open on the end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.cycle_time import cycle_time

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

SINCE = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 6, 8, 0, 0, tzinfo=timezone.utc)


def _run(c, pid: str, created_at: datetime) -> str:
    run_id = f"crun_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, created_at) "
        "VALUES (:i, :p, 'cycle', 'completed', :at)"),
        {"i": run_id, "p": pid, "at": created_at})
    return run_id


def _pr(c, pid: str, run_id: str, merged_at: datetime | None) -> None:
    change_set_id = f"chs_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO repo_change_sets (id, project_id, app_key, title) "
        "VALUES (:i, :p, 'app', 'cycle')"),
        {"i": change_set_id, "p": pid})
    c.execute(text(
        "INSERT INTO change_set_prs (id, change_set_id, full_name, code_task_run_id, merged_at) "
        "VALUES (:i, :cs, :name, :r, :at)"),
        {"i": f"csp_{uuid4().hex[:12]}", "cs": change_set_id,
         "name": f"acme/app-{uuid4().hex[:8]}", "r": run_id, "at": merged_at})


def _local(c, pid: str, run_id: str, merged_at: datetime | None) -> None:
    c.execute(text(
        "INSERT INTO local_change_sets (id, project_id, repository, number, branch, "
        " base_branch, run_id, merged_at) VALUES (:i, :p, :repo, 1, 'cycle', 'main', :r, :at)"),
        {"i": f"lcs_{uuid4().hex[:12]}", "p": pid, "repo": f"local/{uuid4().hex[:10]}",
         "r": run_id, "at": merged_at})


def _target(c, pid: str) -> str:
    target_id = f"dt_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        " target_kind, target_ref, created_by) VALUES (:i, :p, :app, 'acme/app', "
        " 'staging', 'stack', 'app', 'test')"),
        {"i": target_id, "p": pid, "app": f"app-{uuid4().hex[:8]}"})
    return target_id


def _deploy(c, pid: str, run_id: str, target_id: str, finished_at: datetime | None,
            status: str) -> None:
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
        " commit_sha, status, created_by, code_task_run_id, finished_at) "
        "VALUES (:i, :p, :t, 'acme/app', 'main', :sha, :s, 'test', :r, :at)"),
        {"i": f"sd_{uuid4().hex[:12]}", "p": pid, "t": target_id,
         "sha": uuid4().hex + uuid4().hex[:8], "s": status, "r": run_id, "at": finished_at})


def test_merge_median_is_the_lower_middle_of_start_to_earliest_merge(eng, project):  # noqa: F811
    """Even count: lower of the two middle values. 10.9s truncates to 10, not 11."""
    with eng.begin() as c:
        target = _target(c, project)
        created = SINCE + timedelta(days=1)
        earliest_of_two_prs = _run(c, project, created)
        _pr(c, project, earliest_of_two_prs, created + timedelta(seconds=5))
        _pr(c, project, earliest_of_two_prs, created + timedelta(seconds=80))
        local_only = _run(c, project, created)
        _local(c, project, local_only, created + timedelta(microseconds=10_900_000))
        both = _run(c, project, created)
        _pr(c, project, both, created + timedelta(seconds=900))
        _local(c, project, both, created + timedelta(seconds=30))
        null_pr = _run(c, project, created)
        _pr(c, project, null_pr, None)
        _local(c, project, null_pr, created + timedelta(seconds=40))
        _run(c, project, created)  # no merge: absent
        backwards = _run(c, project, created)
        # Earliest end is before the start, so the later valid merge does not save the row.
        _pr(c, project, backwards, created - timedelta(seconds=10))
        _local(c, project, backwards, created + timedelta(seconds=15))
        failed = _run(c, project, created)
        _deploy(c, project, failed, target, created + timedelta(seconds=3), "failed")
        report = cycle_time(c, project, SINCE, UNTIL)
    assert report["project_id"] == project
    assert report["since"] == SINCE.isoformat()
    assert report["until"] == UNTIL.isoformat()
    assert report["merged"] == {"n": 4, "median_seconds": 10}
    assert report["deployed"] == {"n": 0, "median_seconds": None}


def test_deploy_median_uses_the_earliest_healthy(eng, project):  # noqa: F811
    with eng.begin() as c:
        target = _target(c, project)
        created = SINCE + timedelta(days=2)
        two_healthy = _run(c, project, created)
        _deploy(c, project, two_healthy, target, created + timedelta(seconds=80), "healthy")
        _deploy(c, project, two_healthy, target, created + timedelta(seconds=20), "healthy")
        failed_then_healthy = _run(c, project, created)
        _deploy(c, project, failed_then_healthy, target, created + timedelta(seconds=5), "failed")
        _deploy(c, project, failed_then_healthy, target, created + timedelta(seconds=30), "healthy")
        failed_only = _run(c, project, created)
        _deploy(c, project, failed_only, target, created + timedelta(seconds=4), "failed")
        one_healthy = _run(c, project, created)
        _deploy(c, project, one_healthy, target, created + timedelta(seconds=90), "healthy")
        backwards = _run(c, project, created)
        _deploy(c, project, backwards, target, created - timedelta(seconds=5), "healthy")
        _deploy(c, project, backwards, target, created + timedelta(seconds=25), "healthy")
        report = cycle_time(c, project, SINCE, UNTIL)
    assert report["merged"] == {"n": 0, "median_seconds": None}
    assert report["deployed"] == {"n": 3, "median_seconds": 30}


def test_the_window_is_half_open_on_the_end_not_on_created_at(eng, project):  # noqa: F811
    with eng.begin() as c:
        target = _target(c, project)
        started_before = _run(c, project, SINCE - timedelta(days=2))
        _pr(c, project, started_before, SINCE + timedelta(seconds=100))
        on_since = _run(c, project, SINCE - timedelta(seconds=3600))
        _local(c, project, on_since, SINCE)
        just_inside = _run(c, project, SINCE + timedelta(days=1))
        _pr(c, project, just_inside, UNTIL - timedelta(seconds=1))
        on_until = _run(c, project, SINCE + timedelta(days=1))
        _pr(c, project, on_until, UNTIL)
        after = _run(c, project, SINCE + timedelta(days=1))
        _local(c, project, after, UNTIL + timedelta(seconds=10))
        deploy_on_since = _run(c, project, SINCE - timedelta(seconds=50))
        _deploy(c, project, deploy_on_since, target, SINCE, "healthy")
        deploy_inside = _run(c, project, UNTIL - timedelta(seconds=71))
        _deploy(c, project, deploy_inside, target, UNTIL - timedelta(seconds=1), "healthy")
        deploy_on_until = _run(c, project, UNTIL - timedelta(seconds=20))
        _deploy(c, project, deploy_on_until, target, UNTIL, "healthy")
        report = cycle_time(c, project, SINCE, UNTIL)
    assert report["merged"] == {"n": 3, "median_seconds": 172900}
    assert report["deployed"] == {"n": 2, "median_seconds": 50}


def test_another_projects_rows_are_absent_even_when_they_name_our_run(eng, project):  # noqa: F811
    other = make_project(eng)
    try:
        with eng.begin() as c:
            ours = _run(c, project, SINCE + timedelta(hours=1))
            _pr(c, project, ours, SINCE + timedelta(hours=1, seconds=100))
            target = _target(c, project)
            _deploy(c, project, ours, target, SINCE + timedelta(hours=1, seconds=100), "healthy")
            theirs = _run(c, other, SINCE + timedelta(hours=1))
            _pr(c, other, theirs, SINCE + timedelta(hours=1, seconds=1))
            _local(c, other, theirs, SINCE + timedelta(hours=1, seconds=2))
            other_target = _target(c, other)
            _deploy(c, other, theirs, other_target, SINCE + timedelta(hours=1, seconds=1),
                    "healthy")
            # Their rows name our run. They still do not count.
            _pr(c, other, ours, SINCE + timedelta(hours=1, seconds=1))
            _local(c, other, ours, SINCE + timedelta(hours=1, seconds=2))
            _deploy(c, other, ours, other_target, SINCE + timedelta(hours=1, seconds=3),
                    "healthy")
            report = cycle_time(c, project, SINCE, UNTIL)
            other_report = cycle_time(c, other, SINCE, UNTIL)
        assert report["merged"] == {"n": 1, "median_seconds": 100}
        assert report["deployed"] == {"n": 1, "median_seconds": 100}
        assert other_report["merged"] == {"n": 1, "median_seconds": 1}
        assert other_report["deployed"] == {"n": 1, "median_seconds": 1}
    finally:
        purge(eng, other)


def test_an_empty_window_reports_no_median(eng, project):  # noqa: F811
    with eng.begin() as c:
        created = SINCE - timedelta(days=30)
        run_id = _run(c, project, created)
        _pr(c, project, run_id, created + timedelta(seconds=10))
        report = cycle_time(c, project, SINCE, UNTIL)
    assert report == {
        "project_id": project,
        "since": SINCE.isoformat(),
        "until": UNTIL.isoformat(),
        "merged": {"n": 0, "median_seconds": None},
        "deployed": {"n": 0, "median_seconds": None},
    }
