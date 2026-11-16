"""A window of stack deploys splits into live, failed, rollback, and still open.

Rollback wins: a row with rollback_of set is a rollback even when its status is healthy
or failed. The window is half-open on created_at, [since, until). Another project's
deploys are not counted. An empty window is four zeros, never a percentage.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

_KEYS = ("project_id", "since", "until", "live", "failed", "rollback", "open")


def _target(c, pid: str) -> str:
    tid = f"dt_{uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, created_by) "
        "VALUES (:id, :p, :app, 'acme/app', 'prod', 'stack', :app, 'test')"),
        {"id": tid, "p": pid, "app": tid})
    return tid


def _deploy(c, pid: str, tid: str, *, status: str, at, rollback_of: str | None) -> None:
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
        "commit_sha, status, rollback_of, created_by, created_at) "
        "VALUES (:id, :p, :t, 'acme/app', 'main', :sha, :st, :rb, 'test', :at)"),
        {"id": f"sd_{uuid4().hex[:12]}", "p": pid, "t": tid, "sha": uuid4().hex,
         "st": status, "rb": rollback_of, "at": at})


def test_deploy_outcomes_split_live_failed_and_rollback(eng, project):  # noqa: F811
    from boss_engine.services.deploy_outcomes import deploy_outcomes

    other = make_project(eng)
    try:
        with eng.begin() as c:
            clock = c.execute(text(
                "SELECT now() - interval '2 hours' AS since, "
                "       now() - interval '1 hour' AS until, "
                "       now() - interval '2 hours' - interval '1 second' AS before_since, "
                "       now() - interval '1 hour' - interval '1 minute' AS inside"
            )).mappings().one()
            since, until = clock["since"], clock["until"]
            tid = _target(c, project)
            # Exactly `since` is inside. Exactly `until` is outside. Before `since` is outside.
            _deploy(c, project, tid, status="healthy", at=since, rollback_of=None)
            _deploy(c, project, tid, status="failed", at=clock["inside"], rollback_of=None)
            _deploy(c, project, tid, status="healthy", at=clock["inside"], rollback_of="sd_prev")
            _deploy(c, project, tid, status="failed", at=clock["inside"], rollback_of="sd_prev2")
            _deploy(c, project, tid, status="rejected", at=clock["inside"], rollback_of="sd_prev3")
            for status in ("planned", "awaiting_approval", "running", "rejected"):
                _deploy(c, project, tid, status=status, at=clock["inside"], rollback_of=None)
            _deploy(c, project, tid, status="healthy", at=clock["before_since"], rollback_of=None)
            _deploy(c, project, tid, status="failed", at=until, rollback_of=None)

            other_tid = _target(c, other)
            _deploy(c, other, other_tid, status="healthy", at=clock["inside"], rollback_of=None)
            _deploy(c, other, other_tid, status="failed", at=clock["inside"], rollback_of=None)

            counted = deploy_outcomes(c, project, since, until)
            empty_since = until + timedelta(days=30)
            empty_until = empty_since + timedelta(days=1)
            empty = deploy_outcomes(c, project, empty_since, empty_until)
    finally:
        purge(eng, other)

    assert counted == {
        "project_id": project,
        "since": since,
        "until": until,
        "live": 1,
        "failed": 1,
        "rollback": 3,
        "open": 4,
    }
    assert tuple(counted) == _KEYS
    assert all(type(counted[k]) is int for k in ("live", "failed", "rollback", "open"))
    assert empty == {
        "project_id": project,
        "since": empty_since,
        "until": empty_until,
        "live": 0,
        "failed": 0,
        "rollback": 0,
        "open": 0,
    }
    assert "percentage" not in empty and "change_fail_rate" not in empty
