"""ROUTINES: CRON, CONCURRENCY, CATCH-UP AND AN ACTIVITY GATE (BOSSAPP-877).

* Cron is read in the schedule's zone, field errors are named, Feb 30 never comes round.
* plan_firings is pure: catch_up one / all (capped, newest kept) / none (only an on-time one).
* run_due: skip_if_active skips a firing while the last run is going; coalesce holds ONE and
  starts it on the tick after the run ends; the activity gate skips when nothing happened; every
  firing not started is a workflow_schedule_runs row with its reason; inputs are templated.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_schedules as sched

from .test_a_workflow_can_be_scheduled import PROJECT, needs_db, scheduled  # noqa: F401


def at(s: str, tz: str = "UTC") -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(tz))


# ── cron ─────────────────────────────────────────────────────────────────────────────────────

def test_cron_reads_in_its_zone_and_names_a_bad_field():
    # Weekdays at 09:00 London; asked on a Friday evening → Monday 09:00 (BST: 08:00 UTC).
    assert sched.next_cron("0 9 * * 1-5", "Europe/London", at("2026-09-25T20:00:00")) \
        == at("2026-09-28T08:00:00")
    assert sched.next_cron("*/15 * * * *", "UTC", at("2026-09-25T10:07:00")) == at("2026-09-25T10:15:00")
    # Strictly after, never equal.
    assert sched.next_cron("30 10 * * *", "UTC", at("2026-09-25T10:30:00")) == at("2026-09-26T10:30:00")
    # Names and 7 = Sunday; day-of-month OR day-of-week when both are given.
    assert sched.next_cron("0 0 * jan sun", "UTC", at("2026-12-31T00:00:00")).weekday() == 6
    assert sched.next_cron("0 0 1 * 7", "UTC", at("2026-11-02T00:00:00")) == at("2026-11-08T00:00:00")
    with pytest.raises(sched.ScheduleError, match="minute field"):
        sched.parse_cron("61 * * * *")
    with pytest.raises(sched.ScheduleError, match="five fields"):
        sched.parse_cron("* * *")
    with pytest.raises(sched.ScheduleError, match="never comes round"):
        sched.next_cron("0 0 30 2 *", "UTC", at("2026-01-01T00:00:00"))


def test_catch_up_plans_one_all_or_none():
    row = {"cadence": "hourly", "minute": 0, "hour": 0, "weekday": None, "timezone": "UTC",
           "next_run_at": at("2026-09-25T06:00:00"), "catch_up": "one", "catch_up_max": 2}
    now = at("2026-09-25T10:02:00")
    fires, skipped, following = sched.plan_firings(row, now)
    assert fires == [at("2026-09-25T06:00:00")] and len(skipped) == 4
    assert {why for _, why in skipped} == {"caught_up_once"} and following == at("2026-09-25T11:00:00")
    fires, skipped, _ = sched.plan_firings({**row, "catch_up": "all"}, now)
    assert fires == [at("2026-09-25T09:00:00"), at("2026-09-25T10:00:00")]
    assert [why for _, why in skipped] == ["catch_up_cap"] * 3
    fires, skipped, _ = sched.plan_firings({**row, "catch_up": "none"}, now)
    assert fires == [at("2026-09-25T10:00:00")] and {w for _, w in skipped} == {"missed"}
    late, _, _ = sched.plan_firings({**row, "catch_up": "none"}, at("2026-09-25T10:30:00"))
    assert late == []


def test_inputs_are_templated_for_each_firing():
    out = sched.render_input({"subject": "Digest {{date}}", "nested": ["since {{since}}", 3],
                              "other": "{{unknown}}"},
                             {"date": "2026-09-25", "since": "2026-09-24T09:00:00+00:00"})
    assert out == {"subject": "Digest 2026-09-25", "nested": ["since 2026-09-24T09:00:00+00:00", 3],
                   "other": "{{unknown}}"}


# ── the tick ─────────────────────────────────────────────────────────────────────────────────

def _due(c, sid):
    c.execute(text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' "
                   "WHERE id = :i"), {"i": sid})


def _firings(c, sid):
    return [(f["outcome"], f["reason"]) for f in reversed(sched.firings(c, PROJECT, sid))]


@needs_db
def test_skip_if_active_and_coalesce_hold_back_while_a_run_is_going(scheduled):
    engine, key = scheduled
    with engine.begin() as c:
        skip = sched.create(c, project_id=PROJECT, workflow_key=key, name=f"skip {key}",
                            cadence="cron", minute=0, actor="test", cron_expr="0 * * * *",
                            concurrency_policy="skip_if_active")
        assert skip["cadence"] == "cron" and skip["describes"] == "on cron '0 * * * *' (UTC)"
        _due(c, skip["id"])
        first = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == skip["id"]]
        assert first[0].get("run_id")
        _due(c, skip["id"])       # the timer run is still going
        second = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == skip["id"]]
        assert second[0]["reason"] == "run_active" and second[0]["active_run_id"] == first[0]["run_id"]
        assert _firings(c, skip["id"]) == [("started", None), ("suppressed", "run_active")]

        merge = sched.create(c, project_id=PROJECT, workflow_key=key, name=f"merge {key}",
                             cadence="hourly", minute=0, actor="test",
                             concurrency_policy="coalesce")
        _due(c, merge["id"])
        run_id = [f for f in sched.run_due(c, project_id=PROJECT)
                  if f["schedule_id"] == merge["id"]][0]["run_id"]
        _due(c, merge["id"])
        held = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == merge["id"]]
        assert held[0]["coalesced"] is True
        # Not due again, but still held: nothing starts while the run goes on.
        assert not [f.get("run_id") for f in sched.run_due(c, project_id=PROJECT)
                    if f["schedule_id"] == merge["id"] and f.get("run_id")]
        c.execute(text("UPDATE workflow_runs SET status = 'completed' WHERE id = :r"), {"r": run_id})
        released = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == merge["id"]]
        assert released[0].get("run_id") and released[0]["run_id"] != run_id
        row = c.execute(text("SELECT coalesced_pending FROM workflow_schedules WHERE id = :i"),
                        {"i": merge["id"]}).scalar()
        assert row is False
        assert _firings(c, merge["id"]) == [("started", None), ("coalesced", "run_active"),
                                            ("started", None)]


@needs_db
def test_the_activity_gate_skips_a_quiet_period_and_inputs_are_templated(scheduled):
    engine, key = scheduled
    with engine.begin() as c:
        gated = sched.create(c, project_id=PROJECT, workflow_key=key, name=f"gate {key}",
                             cadence="daily", minute=0, hour=8, actor="test",
                             input_data={"title": "Digest for {{date}} ({{schedule_name}})"},
                             activity_gate={"kinds": [f"probe{key}."]})
        c.execute(text("UPDATE workflow_schedules SET last_run_at = now() - interval '1 hour' "
                       "WHERE id = :i"), {"i": gated["id"]})
        _due(c, gated["id"])
        quiet = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == gated["id"]]
        assert quiet[0]["reason"] == "no_activity"
        from boss_engine.services.core import emit_event as emit

        emit(c, PROJECT, f"probe{key}.changed", "someone", "probe", "x1", {})
        _due(c, gated["id"])
        busy = [f for f in sched.run_due(c, project_id=PROJECT) if f["schedule_id"] == gated["id"]]
        assert busy[0].get("run_id")
        title = c.execute(text("SELECT input->>'title' FROM workflow_runs WHERE id = :r"),
                          {"r": busy[0]["run_id"]}).scalar()
        assert title.startswith("Digest for 20") and f"(gate {key})" in title
        assert _firings(c, gated["id"]) == [("suppressed", "no_activity"), ("started", None)]
        routine = sched.set_routine(c, project_id=PROJECT, schedule_id=gated["id"], actor="test",
                                    cron_expr="15 6 * * 1", catch_up="all", catch_up_max=5)
        assert routine["cadence"] == "cron" and routine["catch_up"] == "all"
        assert routine["activity_gate"] == {"kinds": [f"probe{key}."], "min_events": 1}
        with pytest.raises(sched.ScheduleError):
            sched.set_routine(c, project_id=PROJECT, schedule_id=gated["id"], actor="test",
                              concurrency_policy="sometimes")
