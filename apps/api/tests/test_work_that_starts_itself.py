"""BOSS can keep checking something, and can report in on a clock.

Phase A1.2 / A1.3. BOSS could only ever act when someone typed: "keep an eye on this PR and
tell me when it is review-ready" had no answer but the operator remembering to come back, and
"every weekday at 9, summarise the open work" had none at all.

Every wake spends model budget, so most of what is tested here is the RESTRAINT: caps, wall
clock budgets, failure stops, one loop per conversation, and a minimum interval. The scheduling
rules are separated from actually running a turn precisely so they can be tested without paying
for a model call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services import agent_wakeups as wake


@pytest.fixture(autouse=True)
def _clean(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_wakeups"))
    yield


def _pid():
    return get_settings().default_project_id


def _monitor(engine, **kw):
    args = dict(project_id=_pid(), actor="ceo", thread_id="athr_w",
                prompt="check whether the PR is approved",
                exit_condition="the PR is approved", interval_seconds=300)
    args.update(kw)
    with engine.begin() as c:
        return wake.arm_monitor(c, **args)


class TestAMonitorMustBeAbleToFinish:
    def test_it_refuses_without_an_exit_condition(self, engine):
        """A loop with no way to finish is a cron job wearing a monitor's name."""
        with pytest.raises(wake.WakeupError) as caught:
            _monitor(engine, exit_condition="")
        assert "exit condition" in str(caught.value)

    def test_it_records_what_to_check_and_when_to_stop(self, engine):
        row = _monitor(engine)
        assert row["exit_condition"] == "the PR is approved"
        assert row["status"] == "active"
        assert row["max_cycles"] == wake.DEFAULT_MAX_CYCLES

    def test_one_loop_per_conversation(self, engine):
        """Two would interleave wakes in one thread and double the spend."""
        _monitor(engine)
        with pytest.raises(wake.WakeupError) as caught:
            _monitor(engine)
        assert "already being monitored" in str(caught.value)

    def test_a_busy_interval_is_floored(self, engine):
        row = _monitor(engine, interval_seconds=5)
        assert row["interval_seconds"] == wake.MIN_INTERVAL_SECONDS


class TestItStopsItself:
    def test_the_cycle_cap_ends_it(self, engine):
        row = _monitor(engine, max_cycles=2)
        with engine.begin() as c:
            wake.advance(c, wakeup_id=row["id"], ok=True, result="nothing yet")
            out = wake.advance(c, wakeup_id=row["id"], ok=True, result="still nothing")
        assert out["status"] == "exhausted"
        assert "ran out of rope" in out["reason"]

    def test_the_wall_clock_budget_ends_it(self, engine):
        row = _monitor(engine, deadline_seconds=1)
        with engine.begin() as c:
            c.execute(text("UPDATE agent_wakeups SET deadline_at = now() - interval '1 minute' "
                           " WHERE id=:i"), {"i": row["id"]})
            out = wake.advance(c, wakeup_id=row["id"], ok=True, result="nothing")
        assert out["status"] == "exhausted"
        assert "time budget" in out["reason"]

    def test_meeting_the_condition_completes_it(self, engine):
        row = _monitor(engine)
        with engine.begin() as c:
            out = wake.advance(c, wakeup_id=row["id"], ok=True,
                               result="the PR was approved", finished=True)
        assert out["status"] == "completed"

    def test_repeated_failure_stops_it_rather_than_paying_forever(self, engine):
        """Watching something broken is not monitoring it."""
        row = _monitor(engine)
        with engine.begin() as c:
            for _ in range(wake.MAX_CONSECUTIVE_FAILURES):
                out = wake.advance(c, wakeup_id=row["id"], ok=False, result="boom")
        assert out["status"] == "failed"
        assert out["consecutive_failures"] == wake.MAX_CONSECUTIVE_FAILURES

    def test_a_success_clears_the_failure_streak(self, engine):
        row = _monitor(engine)
        with engine.begin() as c:
            wake.advance(c, wakeup_id=row["id"], ok=False, result="boom")
            wake.advance(c, wakeup_id=row["id"], ok=True, result="fine")
            out = wake.advance(c, wakeup_id=row["id"], ok=False, result="boom")
        assert out["status"] == "active"

    def test_an_operator_can_stop_it(self, engine):
        row = _monitor(engine)
        with engine.begin() as c:
            wake.stop(c, project_id=_pid(), wakeup_id=row["id"], actor="ceo")
            assert wake.due(c) == []


class TestTheIntervalPreservesItsDeadline:
    def test_the_next_wake_is_measured_from_the_due_time_not_from_now(self, engine):
        """A turn that took four minutes must not push a five-minute loop to nine.

        This is the detail that keeps a loop on schedule in a conversation somebody is
        actively using, instead of drifting later every single round.
        """
        row = _monitor(engine, interval_seconds=600)
        with engine.begin() as c:
            due_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            c.execute(text("UPDATE agent_wakeups SET next_wake_at=:d WHERE id=:i"),
                      {"d": due_at, "i": row["id"]})
            out = wake.advance(c, wakeup_id=row["id"], ok=True, result="nothing")
        drift = abs((out["next_wake_at"] - (due_at + timedelta(seconds=600))).total_seconds())
        assert drift < 2, f"next wake drifted {drift}s from the deadline"

    def test_a_long_outage_catches_up_rather_than_firing_a_burst(self, engine):
        """If the sweeper was down for an hour, a 60s loop must not fire sixty times."""
        row = _monitor(engine, interval_seconds=60)
        with engine.begin() as c:
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 hour' "
                           " WHERE id=:i"), {"i": row["id"]})
            out = wake.advance(c, wakeup_id=row["id"], ok=True, result="nothing")
        assert out["next_wake_at"] > datetime.now(timezone.utc)


class TestSchedules:
    def test_every_weekday_at_nine(self, engine):
        with engine.begin() as c:
            row = wake.schedule(c, project_id=_pid(), actor="ceo",
                                prompt="summarise the open work I should review",
                                at_time="09:00",
                                days=["mon", "tue", "wed", "thu", "fri"],
                                tz_name="Europe/London")
        assert row["kind"] == "schedule"
        assert row["thread_id"] is None      # a fresh conversation per run
        assert row["next_wake_at"] > datetime.now(timezone.utc)

    def test_the_next_run_lands_on_an_allowed_day(self, engine):
        nxt = wake.next_scheduled_wake("09:00", ["mon"], "UTC")
        assert nxt.weekday() == 0
        assert nxt > datetime.now(timezone.utc)

    def test_it_is_the_operators_timezone_not_the_servers(self, engine):
        """"Every weekday at 9" means nine where the person is."""
        from zoneinfo import ZoneInfo

        nxt = wake.next_scheduled_wake("09:00", [], "Asia/Kolkata")
        local = nxt.astimezone(ZoneInfo("Asia/Kolkata"))
        assert (local.hour, local.minute) == (9, 0)

    def test_an_unknown_timezone_is_refused(self):
        with pytest.raises(wake.WakeupError):
            wake.next_scheduled_wake("09:00", [], "Mars/Olympus")

    def test_a_nonsense_time_is_refused(self):
        with pytest.raises(wake.WakeupError):
            wake.next_scheduled_wake("half nine", [], "UTC")

    def test_a_nonsense_day_is_refused(self):
        with pytest.raises(wake.WakeupError):
            wake.next_scheduled_wake("09:00", ["caturday"], "UTC")


class TestTheSweepIsBounded:
    def test_only_what_is_due_comes_back(self, engine):
        _monitor(engine)
        with engine.begin() as c:
            assert wake.due(c) == []
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))
            assert len(wake.due(c)) == 1

    def test_one_sweep_cannot_start_fifty_turns(self, engine):
        with engine.begin() as c:
            for i in range(8):
                wake.arm_monitor(c, project_id=_pid(), actor="ceo", thread_id=f"athr_{i}",
                                 prompt="p", exit_condition="e", interval_seconds=60)
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))
            assert len(wake.due(c, limit=3)) == 3

    def test_a_stopped_loop_is_never_due(self, engine):
        row = _monitor(engine)
        with engine.begin() as c:
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))
            wake.stop(c, project_id=_pid(), wakeup_id=row["id"], actor="ceo")
            assert wake.due(c) == []


class TestTheToolsThemselves:
    """Through the registry, because that is the path the model actually takes."""

    def _invoke(self, engine, name, args):
        from boss_engine.context import EngineContext
        from boss_engine.tools import build_default_registry

        ctx = EngineContext(settings=get_settings(), engine=engine)
        return build_default_registry().invoke(
            name, ctx, project_id=_pid(), actor="ceo", args=args)

    def test_watch_arms_a_monitor_on_this_conversation(self, engine):
        out = self._invoke(engine, "watch", {
            "_thread_id": "athr_tool", "check": "is the PR approved",
            "until": "the PR is approved"})
        assert out["ok"] is True, out
        assert out["thread_id"] == "athr_tool"

    def test_watch_without_a_conversation_is_refused(self, engine):
        out = self._invoke(engine, "watch", {"check": "x", "until": "y"})
        assert out["ok"] is False
        assert "conversation" in out["error"]

    def test_watch_without_an_exit_condition_is_refused(self, engine):
        out = self._invoke(engine, "watch", {"_thread_id": "athr_t2", "check": "x", "until": ""})
        assert out["ok"] is False

    def test_stop_watching_finds_this_conversations_loop(self, engine):
        self._invoke(engine, "watch", {
            "_thread_id": "athr_t3", "check": "x", "until": "y"})
        out = self._invoke(engine, "stop_watching", {"_thread_id": "athr_t3"})
        assert out["ok"] is True and out["status"] == "stopped"

    def test_finishing_is_a_different_outcome_from_stopping(self, engine):
        """An operator reading back needs to know whether the thing happened or was called off."""
        self._invoke(engine, "watch", {
            "_thread_id": "athr_t4", "check": "x", "until": "y"})
        out = self._invoke(engine, "stop_watching", {
            "_thread_id": "athr_t4", "finished": True})
        assert out["status"] == "completed"

    def test_stopping_nothing_says_so(self, engine):
        out = self._invoke(engine, "stop_watching", {"_thread_id": "athr_none"})
        assert out["ok"] is False

    def test_schedule_work_lands_in_the_operators_timezone(self, engine):
        from zoneinfo import ZoneInfo

        out = self._invoke(engine, "schedule_work", {
            "prompt": "summarise open work", "at_time": "09:00",
            "days": ["mon", "tue", "wed", "thu", "fri"], "timezone": "Europe/London"})
        assert out["ok"] is True, out
        local = out["next_wake_at"]
        if isinstance(local, str):
            local = datetime.fromisoformat(local)
        assert local.astimezone(ZoneInfo("Europe/London")).hour == 9

    def test_a_bad_timezone_comes_back_as_an_error_not_a_crash(self, engine):
        out = self._invoke(engine, "schedule_work", {
            "prompt": "x", "at_time": "09:00", "timezone": "Mars/Olympus"})
        assert out["ok"] is False
        assert "timezone" in out["error"]

    def test_list_watches_reports_what_is_running(self, engine):
        self._invoke(engine, "watch", {
            "_thread_id": "athr_t5", "check": "watch the deploy", "until": "it is healthy"})
        out = self._invoke(engine, "list_watches", {})
        assert out["count"] == 1
        assert out["watches"][0]["exit_condition"] == "it is healthy"


class TestTheSweepIsSafe:
    """`run_due` is the only path that spends model budget with nobody at the keyboard."""

    def test_one_broken_loop_does_not_stop_the_sweep(self, engine, monkeypatch):
        from boss_engine.services import agent_wakeups as w

        with engine.begin() as c:
            for i in range(3):
                w.arm_monitor(c, project_id=_pid(), actor="ceo", thread_id=f"athr_s{i}",
                              prompt="p", exit_condition="e", interval_seconds=60)
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))

        calls = {"n": 0}

        class Boom:
            def __init__(self, *a, **k):
                pass

            def chat(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("model is down")
                return {"reply": "nothing changed"}

        monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Boom)
        out = w.run_due(get_settings(), engine, limit=5)
        assert out["due"] == 3
        assert len(out["ran"]) == 3, "a failing loop stopped the sweep"
        assert any(r.get("error") == "RuntimeError" for r in out["ran"])

    def test_the_sweep_respects_its_limit(self, engine, monkeypatch):
        from boss_engine.services import agent_wakeups as w

        with engine.begin() as c:
            for i in range(6):
                w.arm_monitor(c, project_id=_pid(), actor="ceo", thread_id=f"athr_l{i}",
                              prompt="p", exit_condition="e", interval_seconds=60)
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))

        class Quiet:
            def __init__(self, *a, **k):
                pass

            def chat(self, **kwargs):
                return {"reply": "ok"}

        monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Quiet)
        assert w.run_due(get_settings(), engine, limit=2)["due"] == 2

    def test_a_monitor_the_agent_ended_is_not_rescheduled(self, engine, monkeypatch):
        """The agent ends a loop by calling stop_watching mid-wake; the sweep must notice."""
        from boss_engine.services import agent_wakeups as w

        with engine.begin() as c:
            row = w.arm_monitor(c, project_id=_pid(), actor="ceo", thread_id="athr_done",
                                prompt="p", exit_condition="e", interval_seconds=60)
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))

        class Finisher:
            def __init__(self, *a, **k):
                pass

            def chat(self, **kwargs):
                with engine.begin() as c:
                    w.stop(c, project_id=_pid(), wakeup_id=row["id"], actor="ceo",
                           status="completed", reason="the PR was approved")
                return {"reply": "it is approved"}

        monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Finisher)
        out = w.run_due(get_settings(), engine, limit=5)
        assert out["ran"][0]["outcome"] == "completed"
        with engine.begin() as c:
            assert w.describe(c, wakeup_id=row["id"])["status"] == "completed"

    def test_a_schedule_starts_a_fresh_conversation(self, engine, monkeypatch):
        """A daily briefing must not grow one thread forever."""
        from boss_engine.services import agent_wakeups as w

        seen = {}

        class Recorder:
            def __init__(self, *a, **k):
                pass

            def chat(self, **kwargs):
                seen.update(kwargs)
                return {"reply": "briefed"}

        with engine.begin() as c:
            w.schedule(c, project_id=_pid(), actor="ceo", prompt="brief me",
                       at_time="09:00", tz_name="UTC")
            c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min'"))
        monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Recorder)
        w.run_due(get_settings(), engine, limit=5)
        assert seen.get("thread_id") is None
