"""BOSSAPP-857: a role's own run policy at job claim, and a no-progress back-off for wakeups.

`max_concurrent_jobs` is a project envelope; one runaway role could take all of it. A role may
now carry run_policy {max_concurrent, max_daily_runs, max_daily_cost_usd}, asked when one of its
jobs is claimed. With no policy (the default) nothing is asked and nothing is recorded."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import agent_wakeups as wake
from boss_engine.services import role_run_policy as rrp
from boss_engine.services.core import now_utc
from boss_engine.services.jobs import JobLeaseService, enqueue_job

from ._ambient_support import eng, make_project, project, purge  # noqa: F401


def test_a_policy_is_checked_where_it_is_typed():
    assert rrp.normalise(None) == {} and rrp.normalise({}) == {}
    assert rrp.normalise({"max_concurrent": 2, "max_daily_cost_usd": 5}) == {
        "max_concurrent": 2, "max_daily_cost_usd": 5.0}
    for bad in ({"max_concurrent": 0}, {"max_daily_runs": True}, {"max_daily_cost_usd": -1},
                {"max_parallel": 3}, ["max_concurrent"]):
        with pytest.raises(rrp.RunPolicyError):
            rrp.normalise(bad)


def test_the_role_comes_from_the_column_then_the_payload():
    assert rrp.role_of({"role_key": "builder", "payload": {"role_key": "other"}}) == "builder"
    assert rrp.role_of({"role_key": None, "payload": json.dumps({"role_key": "qa"})}) == "qa"
    assert rrp.role_of({"payload": {}}) is None


def test_an_empty_policy_reads_nothing():
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("no query for an empty policy")

    assert rrp.hold_for(Boom(), "p", "r", "j", {}) is None


def test_the_daily_cost_cap_holds_until_midnight(monkeypatch):
    now = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(rrp, "usage_today", lambda *a, **k: {
        "running": 0, "runs_today": 0, "cost_usd_micros_today": 5_000_000})
    hold = rrp.hold_for(None, "p", "builder", "j", {"max_daily_cost_usd": 5}, now=now)
    assert hold is not None and hold.limit == "max_daily_cost_usd"
    assert hold.until == datetime(2026, 11, 14, tzinfo=timezone.utc)
    assert "$5.00 of its $5.00" in hold.message
    assert rrp.hold_for(None, "p", "builder", "j", {"max_daily_cost_usd": 6}, now=now) is None


# ── at claim, on a real database ─────────────────────────────────────────────────────────────

def _role(c, pid, role_key, policy=None):
    c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, run_policy) "
                   "VALUES (:id, :p, :r, :r, CAST(:pol AS JSONB))"),
              {"id": f"role_{uuid4().hex[:10]}", "p": pid, "r": role_key,
               "pol": json.dumps(policy or {})})


def _claim_all(eng, kind):
    got = []
    for _ in range(10):
        with eng.begin() as c:
            job = JobLeaseService(get_settings()).claim_job(c, "w4r-worker", [kind], 300)
        if job is None:
            return got
        got.append(job["id"])
    return got


def _cleanup(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM role_job_claims WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM jobs WHERE project_id = :p"), {"p": pid})


def test_enqueue_stamps_the_role(eng, project):  # noqa: F811
    kind = f"w4r.test.{uuid4().hex[:6]}"
    try:
        with eng.begin() as c:
            a = enqueue_job(c, project, kind, {"role_key": "builder"})
            b = enqueue_job(c, project, kind, {}, role_key="qa")
            d = enqueue_job(c, project, kind, {})
            roles = dict(c.execute(text("SELECT id, role_key FROM jobs WHERE project_id = :p"),
                                   {"p": project}).all())
        assert roles == {a["id"]: "builder", b["id"]: "qa", d["id"]: None}
    finally:
        _cleanup(eng, project)


def test_no_policy_claims_as_before_and_records_nothing(eng, project):  # noqa: F811
    kind = f"w4r.test.{uuid4().hex[:6]}"
    try:
        with eng.begin() as c:
            _role(c, project, "builder")
            ids = {enqueue_job(c, project, kind, {}, role_key="builder")["id"] for _ in range(3)}
        assert set(_claim_all(eng, kind)) == ids
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM role_job_claims WHERE project_id = :p"),
                             {"p": project}).scalar() == 0
    finally:
        _cleanup(eng, project)


def test_max_concurrent_holds_the_next_job_and_says_why(eng, project):  # noqa: F811
    kind = f"w4r.test.{uuid4().hex[:6]}"
    try:
        with eng.begin() as c:
            _role(c, project, "builder", {"max_concurrent": 1})
            _role(c, project, "qa")
            first = enqueue_job(c, project, kind, {}, role_key="builder", priority=10)["id"]
            second = enqueue_job(c, project, kind, {}, role_key="builder", priority=20)["id"]
            other = enqueue_job(c, project, kind, {}, role_key="qa", priority=30)["id"]
        claimed = _claim_all(eng, kind)
        assert first in claimed and other in claimed and second not in claimed
        with eng.begin() as c:
            row = c.execute(text("SELECT status, run_after, last_error FROM jobs WHERE id = :j"),
                            {"j": second}).mappings().one()
        assert row["status"] == "queued" and row["run_after"] > now_utc()
        assert row["last_error"]["kind"] == "role_run_policy"
        assert row["last_error"]["limit"] == "max_concurrent"
    finally:
        _cleanup(eng, project)


def test_max_daily_runs_counts_claims_and_holds_until_tomorrow(eng, project):  # noqa: F811
    kind = f"w4r.test.{uuid4().hex[:6]}"
    try:
        with eng.begin() as c:
            _role(c, project, "builder", {"max_daily_runs": 2})
            ids = [enqueue_job(c, project, kind, {"role_key": "builder"}, priority=10 + i)["id"]
                   for i in range(3)]
        claimed = _claim_all(eng, kind)
        assert claimed == ids[:2]
        with eng.begin() as c:
            row = c.execute(text("SELECT run_after, last_error FROM jobs WHERE id = :j"),
                            {"j": ids[2]}).mappings().one()
            use = rrp.usage_today(c, project, "builder")
        assert row["last_error"]["limit"] == "max_daily_runs"
        assert row["run_after"] >= now_utc().replace(hour=0, minute=0, second=0,
                                                     microsecond=0) + timedelta(days=1)
        assert use["runs_today"] == 2 and use["running"] == 2
    finally:
        _cleanup(eng, project)


# ── the no-progress back-off ─────────────────────────────────────────────────────────────────

def _monitor(eng, pid, **kw):
    with eng.begin() as c:
        return wake.arm_monitor(c, project_id=pid, actor="op", thread_id=f"t_{uuid4().hex[:6]}",
                                prompt="check the deploy", exit_condition="when healthy",
                                interval_seconds=300, **kw)


def _wakes(eng, pid):
    with eng.begin() as c:
        c.execute(text("DELETE FROM agent_wakeups WHERE project_id = :p"), {"p": pid})


def test_without_backoff_a_monitor_keeps_its_clock(eng, project):  # noqa: F811
    try:
        w = _monitor(eng, project)
        for _ in range(3):
            with eng.begin() as c:
                before = wake.describe(c, wakeup_id=w["id"])["next_wake_at"]
                out = wake.advance(c, wakeup_id=w["id"], ok=True, result="Nothing changed.")
            assert out["next_wake_at"] - before == timedelta(seconds=300)
            assert "backoff_factor" not in out
    finally:
        _wakes(eng, project)


def test_a_monitor_that_reports_nothing_new_waits_longer_and_resets_on_news(eng, project):  # noqa: F811
    try:
        w = _monitor(eng, project, backoff_max_factor=4)
        steps = []
        for reply in ("Deploy is rolling.", "deploy is   rolling.", "Deploy is rolling.",
                      "Deploy is rolling.", "Deploy finished; health check failing."):
            with eng.begin() as c:
                before = wake.describe(c, wakeup_id=w["id"])["next_wake_at"]
                out = wake.advance(c, wakeup_id=w["id"], ok=True, result=reply)
            steps.append(((out["next_wake_at"] - before).total_seconds() // 300,
                          out["backoff_factor"]))
        assert [s[1] for s in steps] == [1, 2, 4, 4, 1]
        assert [s[0] for s in steps] == [1, 2, 4, 4, 1]
    finally:
        _wakes(eng, project)


def test_an_explicit_progress_signal_wins_and_a_schedule_skips_occurrences(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            s = wake.schedule(c, project_id=project, actor="op", prompt="daily brief",
                              at_time="09:00", backoff_max_factor=8)
            first = wake.advance(c, wakeup_id=s["id"], ok=True, result="a", progress=False)
            again = wake.advance(c, wakeup_id=s["id"], ok=True, result="b", progress=False)
            moved = wake.advance(c, wakeup_id=s["id"], ok=True, result="b", progress=True)
        assert first["backoff_factor"] == 2 and again["backoff_factor"] == 4
        assert moved["backoff_factor"] == 1
        base = wake.next_scheduled_wake("09:00", [], "UTC")
        assert again["next_wake_at"] - base == timedelta(days=3)
        with pytest.raises(wake.WakeupError):
            with eng.begin() as c:
                wake.set_backoff(c, wakeup_id=s["id"], max_factor=65)
    finally:
        _wakes(eng, project)
