"""A build that fails instantly gets exactly two automatic retries, spaced — never a runaway.

THE CASE (stranger test r6, 2026-11-18). One Easy Mode press made 15 failed builds in 3m16s,
~7 s apart. The retry counter and the backoff were written AFTER the next attempt was queued, so
a worker took that attempt at once; it failed in seconds, read a counter of 0 (its own field was
not written yet) and started its own "first" retry — and again, 14 times.

THE RULE: the count and the not-before go with the new attempt's row and job in the one write
that queues it; the count is read from the lineage, not from the failed run's own (possibly
stale) fields; and two automatic retries per lineage is a hard cap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import governed_code_task_service as governed

from .test_a_resumed_build_runs_as_the_person import _Jobs  # noqa: F401
from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import (  # noqa: F401
    WORKER_KEY, _ports, _standalone_build, env, staffed)


class _TimedJobs(_Jobs):
    """The real enqueue, with the not-before the real JobLeaseService takes (_Jobs does)."""


def _fail_now(env, run_id):
    with env.engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status='failed', phase='done', "
                       "error='git -c failed: HTTP 502', finalize_ctx = finalize_ctx || "
                       "'{\"failure_class\": \"infra\"}'::jsonb WHERE id=:r"), {"r": run_id})


def _retry(env, run_id):
    # The pipeline hands over the finalize_ctx it loaded when the attempt STARTED: before any
    # count could be written to it. Empty here, the race at its worst.
    with lineage.acting(WORKER_KEY):
        return governed._retry_after_infra_failure(env.engine, get_settings(), _TimedJobs(),
                                                   run_id, env.project, {}, {"status": "failed"})


def test_an_instantly_failing_build_gets_exactly_two_spaced_retries(env, staffed):  # noqa: F811
    crun, _ = _standalone_build(env)
    _fail_now(env, crun)
    started = datetime.now(timezone.utc)
    made, current = [], crun
    for _ in range(5):                                  # it keeps failing at once
        out = _retry(env, current)
        new = out.get("auto_retry_run_id")
        if not new:
            break
        made.append(new)
        _fail_now(env, new)
        current = new
    assert len(made) == 2, made
    assert out["auto_retry"] == "not_again"
    with env.engine.begin() as c:
        rows = c.execute(text(
            "SELECT r.id, r.finalize_ctx->>'auto_infra_retry' AS n, j.run_after FROM code_task_runs r "
            "JOIN jobs j ON j.kind = 'code.task' AND j.payload->>'run_id' = r.id "
            "WHERE r.id = ANY(:ids)"), {"ids": made}).mappings().all()
    by_id = {r["id"]: r for r in rows}
    # The count was on each attempt's row from the moment it existed.
    assert [by_id[m]["n"] for m in made] == ["1", "2"]
    # Spaced: no attempt could be taken before its backoff (30 s, then 60 s).
    assert by_id[made[0]]["run_after"] >= started + timedelta(seconds=29)
    assert by_id[made[1]]["run_after"] >= started + timedelta(seconds=59)
