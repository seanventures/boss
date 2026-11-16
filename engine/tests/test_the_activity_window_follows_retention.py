"""The activity window follows retention, and the facets count people.

A list or an export asked for a window inside retention keeps that window. A window longer
than retention is cut to retention and says so. With the retention env unset the floor is
still 30 days. A blank actor is not a person.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from boss_engine.services.activity_log import ActivityLogService

from ._ambient_support import eng, project  # noqa: F401

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _event(conn, project_id: str, row_id: str, actor: str, at: datetime) -> None:
    conn.execute(
        text(
            """
            INSERT INTO events (id, project_id, event_type, actor, entity_type,
                                entity_id, data, created_at)
            VALUES (:id, :p, 'job.succeeded', :a, 'job', :e, CAST('{}' AS jsonb), :at)
            """
        ),
        {"id": row_id, "p": project_id, "a": actor, "e": row_id, "at": at},
    )


def _window(days: int) -> dict[str, str]:
    return {"since": (NOW - timedelta(days=days)).isoformat(), "until": NOW.isoformat()}


def _ids(page: dict) -> list[str]:
    return [item["id"] for item in page["items"]]


def test_a_60_day_window_stays_when_retention_is_90_days(eng, project, monkeypatch):
    """A row 45 days ago is inside 60 days and outside the old 30-day clamp."""
    monkeypatch.setenv("BOSS_ACTIVITY_RETENTION_DAYS", "90")
    asked = _window(60)
    with eng.begin() as conn:
        _event(conn, project, "evt_inside", "sam", NOW - timedelta(days=45))
        page = ActivityLogService.query(conn, project, source="event", **asked)
        exported = [
            item["id"]
            for item in ActivityLogService.export(conn, project, source="event", **asked)
        ]
    assert "evt_inside" in _ids(page)
    assert "window_capped" not in page
    assert "evt_inside" in exported
    params = ActivityLogService.export_params(project, source="event", **asked)
    assert "window_capped" not in params
    assert params["since"] == asked["since"]


def test_an_unset_retention_caps_a_60_day_window_at_30_days(eng, project, monkeypatch):
    """No retention env is still 30 days: the older row is gone and the window says so."""
    monkeypatch.delenv("BOSS_ACTIVITY_RETENTION_DAYS", raising=False)
    asked = _window(60)
    with eng.begin() as conn:
        _event(conn, project, "evt_old", "sam", NOW - timedelta(days=45))
        _event(conn, project, "evt_new", "sam", NOW - timedelta(days=1))
        page = ActivityLogService.query(conn, project, source="event", **asked)
        exported = [
            item["id"]
            for item in ActivityLogService.export(conn, project, source="event", **asked)
        ]
    assert "evt_old" not in _ids(page)
    assert "evt_new" in _ids(page)
    assert page["window_capped"] is True
    start = datetime.fromisoformat(page["window"]["from"])
    end = datetime.fromisoformat(page["window"]["to"])
    assert end - start == timedelta(days=30)
    assert "evt_old" not in exported
    assert "evt_new" in exported
    params = ActivityLogService.export_params(project, source="event", **asked)
    assert params["window_capped"] is True
    capped_since = datetime.fromisoformat(params["since"])
    assert NOW - capped_since == timedelta(days=30)


def test_facets_count_a_person_and_skip_a_blank_actor(eng, project, monkeypatch):
    """People in the window are counted. A blank is not one, and the chip ignores its selection."""
    monkeypatch.delenv("BOSS_ACTIVITY_RETENTION_DAYS", raising=False)
    asked = _window(1)
    with eng.begin() as conn:
        _event(conn, project, "evt_sam", "sam", NOW - timedelta(hours=2))
        _event(conn, project, "evt_riley", "riley", NOW - timedelta(hours=3))
        _event(conn, project, "evt_blank", "", NOW - timedelta(hours=4))
        _event(conn, project, "evt_spaces", "   ", NOW - timedelta(hours=5))
        facets = ActivityLogService.facets(conn, project, source="event", **asked)
        narrowed = ActivityLogService.facets(
            conn, project, source="event", actor="sam", **asked)
    actors = facets["actors"]
    by_name = {row["actor"]: row["n"] for row in actors}
    assert by_name["sam"] == 1
    assert by_name["riley"] == 1
    assert all(row["actor"].strip() and set(row) == {"actor", "n"} for row in actors)
    still = {row["actor"] for row in narrowed["actors"]}
    assert {"sam", "riley"} <= still
