"""When a scheduled workflow is next due.

The arithmetic, tested on its own before any of it touches a database. Every bug a scheduler
has is in here: firing twice, never firing, and drifting an hour twice a year.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from boss_engine.services import workflow_schedules as sched


def at(text: str, tz: str = "UTC") -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz))


def nxt(**kwargs) -> datetime:
    kwargs.setdefault("hour", 8)
    kwargs.setdefault("minute", 0)
    kwargs.setdefault("weekday", None)
    kwargs.setdefault("tz_name", "UTC")
    return sched.next_occurrence(**kwargs)


# --- the three cadences ----------------------------------------------------------------------

def test_a_weekly_schedule_lands_on_the_right_weekday():
    """Monday 08:00, asked on a Wednesday."""
    due = nxt(cadence="weekly", weekday=0, after=at("2026-09-23T12:00:00"))
    assert due == at("2026-09-28T08:00:00")      # the following Monday
    assert due.weekday() == 0


def test_a_daily_schedule_is_tomorrow_once_today_has_passed():
    assert nxt(cadence="daily", after=at("2026-09-23T09:00:00")) == at("2026-09-24T08:00:00")
    # And today, when it has not.
    assert nxt(cadence="daily", after=at("2026-09-23T07:00:00")) == at("2026-09-23T08:00:00")


def test_an_hourly_schedule_ignores_the_hour_field():
    due = nxt(cadence="hourly", minute=30, hour=23, after=at("2026-09-23T09:05:00"))
    assert due == at("2026-09-23T09:30:00")


# --- the failure that looks like an eager worker ----------------------------------------------

def test_the_next_time_is_never_the_time_it_just_fired():
    """STRICTLY AFTER, never equal.

    A schedule returning its own firing time is due again the instant it runs — an infinite
    loop that reads as a worker being keen, and bills like one.
    """
    fired = at("2026-09-28T08:00:00")
    for cadence, weekday in (("weekly", 0), ("daily", None), ("hourly", None)):
        due = nxt(cadence=cadence, weekday=weekday, after=fired)
        assert due > fired, cadence


def test_a_weekly_schedule_asked_on_its_own_day_waits_a_week():
    """Monday 08:00, asked at 08:00 on Monday. The same trap, on the boundary."""
    assert nxt(cadence="weekly", weekday=0, after=at("2026-09-28T08:00:00")) \
        == at("2026-10-05T08:00:00")
    # But earlier the same morning it is today.
    assert nxt(cadence="weekly", weekday=0, after=at("2026-09-28T07:59:00")) \
        == at("2026-09-28T08:00:00")


# --- time zones, which is the whole reason the zone is stored ---------------------------------

def test_the_hour_means_the_hour_where_the_person_is():
    """08:00 London in winter is 08:00 UTC; in summer it is 07:00 UTC.

    Storing UTC and leaving the operator to do this is how a digest arrives an hour early for
    half the year and nobody can say why.
    """
    winter = sched.next_occurrence(
        cadence="daily", minute=0, hour=8, weekday=None, tz_name="Europe/London",
        after=at("2026-01-15T00:00:00"))
    assert winter == at("2026-01-15T08:00:00")

    summer = sched.next_occurrence(
        cadence="daily", minute=0, hour=8, weekday=None, tz_name="Europe/London",
        after=at("2026-07-15T00:00:00"))
    assert summer == at("2026-07-15T07:00:00")     # BST — still 08:00 in London


def test_a_daily_schedule_holds_its_local_hour_across_a_dst_change():
    """The instant moves; the local time does not. That is the point."""
    zone = ZoneInfo("Europe/London")
    for day in ("2026-03-28T00:00:00", "2026-03-30T00:00:00"):
        due = sched.next_occurrence(
            cadence="daily", minute=0, hour=8, weekday=None, tz_name="Europe/London",
            after=at(day))
        assert due.astimezone(zone).hour == 8, day


def test_an_unknown_zone_is_refused_with_something_to_do_about_it():
    with pytest.raises(sched.ScheduleError, match="IANA"):
        sched.validate(cadence="daily", minute=0, hour=8, weekday=None, tz_name="PST")


# --- validation says which field ---------------------------------------------------------------

@pytest.mark.parametrize("kwargs,expect", [
    ({"cadence": "fortnightly"}, "cadence"),
    ({"minute": 61}, "minute"),
    ({"hour": 24}, "hour"),
    ({"cadence": "weekly", "weekday": 9}, "weekday"),
    ({"cadence": "weekly", "weekday": None}, "weekday"),
])
def test_a_bad_field_is_named(kwargs, expect):
    base = {"cadence": "daily", "minute": 0, "hour": 8, "weekday": None, "tz_name": "UTC"}
    with pytest.raises(sched.ScheduleError, match=expect):
        sched.validate(**{**base, **kwargs})


def test_fields_that_cannot_apply_are_cleared_rather_than_stored():
    """An hourly schedule has no hour and a daily one has no weekday.

    Keeping whatever was passed leaves two fields that render into the sentence and mean
    nothing — "every hour at :30 on Tuesday" is not a schedule.
    """
    cadence, minute, hour, weekday, _tz = sched.validate(
        cadence="hourly", minute=30, hour=23, weekday=3, tz_name="UTC")
    assert (hour, weekday) == (0, None)

    *_, weekday, _tz = sched.validate(
        cadence="daily", minute=0, hour=8, weekday=3, tz_name="UTC")
    assert weekday is None


def test_a_schedule_reads_as_a_sentence():
    """What the console shows. A schedule nobody can read is a schedule nobody audits."""
    assert sched.describe("weekly", 0, 8, 0, "Europe/London") == \
        "every Monday at 08:00 (Europe/London)"
    assert sched.describe("daily", 30, 6, None, "UTC") == "every day at 06:30 (UTC)"
    assert sched.describe("hourly", 5, 0, None, "UTC") == "every hour at :05 (UTC)"


# ── Firing, against a real database ────────────────────────────────────────────
#
# The arithmetic above is pure and the tests are cheap. What follows is the part that has
# actually gone wrong in this codebase today: code that reads correctly and does nothing. So
# these publish a real workflow, schedule it, and ask what ran.

import os
import uuid

from sqlalchemy import create_engine, text

from boss_engine.services import workflows

DATABASE_URL = os.environ.get("DATABASE_URL")
needs_db = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


@pytest.fixture()
def scheduled():
    """A throwaway workflow with one timer step, plus a schedule pointing at it."""
    engine = create_engine(DATABASE_URL)
    key = f"sched_probe_{uuid.uuid4().hex[:8]}"
    with engine.begin() as connection:
        made = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=key, name=key, description=None,
            actor="test", namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        workflows.publish_version(
            connection, project_id=PROJECT, definition_id=made["id"], actor="test",
            spec={"entry_step": "hold",
                  "steps": [{"id": "hold", "kind": "timer", "wait_seconds": 60,
                             "binding": {}, "next": []}]})
    yield engine, key
    # RUNS FIRST. They reference the version, so deleting the definition while a run this test
    # started still points at it fails on the foreign key — and the probe workflow is then left
    # behind in the project, which is exactly the mess the fixture exists to avoid.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workflow_schedules WHERE workflow_key=:k"),
                           {"k": key})
        connection.execute(
            text("""DELETE FROM workflow_step_runs WHERE workflow_run_id IN (
                      SELECT r.id FROM workflow_runs r
                      JOIN workflow_versions v ON v.id = r.workflow_version_id
                      WHERE v.workflow_definition_id = :i)"""), {"i": made["id"]})
        connection.execute(
            text("""DELETE FROM workflow_runs WHERE workflow_version_id IN (
                      SELECT id FROM workflow_versions WHERE workflow_definition_id = :i)"""),
            {"i": made["id"]})
        connection.execute(text("DELETE FROM workflow_definitions WHERE id=:i"),
                           {"i": made["id"]})


@needs_db
def test_a_due_schedule_starts_a_run_and_moves_on(scheduled):
    engine, key = scheduled
    with engine.begin() as connection:
        row = sched.create(connection, project_id=PROJECT, workflow_key=key,
                           name=f"probe {key}", cadence="daily", minute=0, hour=8,
                           actor="test", input_data={"why": "test"})
        # Due now, as if the hour had come round.
        connection.execute(
            text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' "
                 "WHERE id=:i"), {"i": row["id"]})
        fired = sched.run_due(connection, project_id=PROJECT)

    mine = [f for f in fired if f["schedule_id"] == row["id"]]
    assert mine, "the schedule was due and nothing ran"
    assert mine[0].get("run_id"), f"no run was started: {mine[0]}"

    with engine.begin() as connection:
        after = connection.execute(
            text("SELECT * FROM workflow_schedules WHERE id=:i"), {"i": row["id"]},
        ).mappings().one()
    assert after["last_run_id"] == mine[0]["run_id"]
    assert after["next_run_at"] > after["last_run_at"], "it would fire again immediately"
    assert after["consecutive_failures"] == 0


@needs_db
def test_a_schedule_that_is_not_due_does_not_run(scheduled):
    engine, key = scheduled
    with engine.begin() as connection:
        row = sched.create(connection, project_id=PROJECT, workflow_key=key,
                           name=f"later {key}", cadence="weekly", minute=0, hour=8,
                           weekday=0, actor="test")
        fired = sched.run_due(connection, project_id=PROJECT)
    assert not [f for f in fired if f["schedule_id"] == row["id"]]


@needs_db
def test_the_same_due_moment_starts_one_run_not_two(scheduled):
    """Two workers ticking at once, or one tick retried after a crash.

    The dedupe key is the SCHEDULED time rather than now, so both attempts collide on purpose.
    A digest sent twice is the failure this prevents.
    """
    engine, key = scheduled
    with engine.begin() as connection:
        row = sched.create(connection, project_id=PROJECT, workflow_key=key,
                           name=f"dup {key}", cadence="daily", minute=0, hour=8, actor="test")
        connection.execute(
            text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' "
                 "WHERE id=:i"), {"i": row["id"]})
        first = sched.run_due(connection, project_id=PROJECT)
        # Put it back as if a second worker had read the row before the first committed.
        connection.execute(
            text("UPDATE workflow_schedules SET next_run_at = now() - interval '1 minute' "
                 "WHERE id=:i"), {"i": row["id"]})
        second = sched.run_due(connection, project_id=PROJECT)

    run_ids = {f.get("run_id") for f in (first + second) if f["schedule_id"] == row["id"]}
    assert len(run_ids) == 1, f"the same moment produced {len(run_ids)} runs"


@needs_db
def test_a_schedule_pointing_at_a_workflow_with_no_version_is_refused(scheduled):
    """At creation, not on every tick forever."""
    engine, _key = scheduled
    with engine.begin() as connection:
        empty = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=f"empty_{uuid.uuid4().hex[:8]}",
            name="empty", description=None, actor="test",
            namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=False)
        with pytest.raises(sched.ScheduleError, match="no published version"):
            sched.create(connection, project_id=PROJECT, workflow_key=empty["workflow_key"],
                         name="never", cadence="daily", minute=0, hour=8, actor="test")
        connection.execute(text("DELETE FROM workflow_definitions WHERE id=:i"),
                           {"i": empty["id"]})


@needs_db
def test_resuming_a_paused_schedule_does_not_fire_it_immediately(scheduled):
    """A schedule paused for a month is a month overdue.

    Resuming without recomputing makes it fire at once, which is never what resume meant.
    """
    engine, key = scheduled
    with engine.begin() as connection:
        row = sched.create(connection, project_id=PROJECT, workflow_key=key,
                           name=f"paused {key}", cadence="daily", minute=0, hour=8,
                           actor="test")
        sched.set_enabled(connection, PROJECT, row["id"], False, "test")
        connection.execute(
            text("UPDATE workflow_schedules SET next_run_at = now() - interval '30 days' "
                 "WHERE id=:i"), {"i": row["id"]})
        resumed = sched.set_enabled(connection, PROJECT, row["id"], True, "test")
        fired = sched.run_due(connection, project_id=PROJECT)

    assert resumed["enabled"] is True
    assert not [f for f in fired if f["schedule_id"] == row["id"]], \
        "resuming fired the run it had missed"
