"""This window compared with the previous window of the same length.

This window is [since, until). The previous window is
[since - (until - since), since). Counts follow created_at on this
project only. A finished failure is a job whose status is failed_final
or deadlettered. failed_retryable is still open. cancelled is not a
failure. An approval is a decision whose status is approved. When the
previous count is 0, percent is None.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

SINCE = datetime(2026, 11, 8, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 15, tzinfo=timezone.utc)
SPAN = UNTIL - SINCE
PREVIOUS = SINCE - SPAN


def _job(connection, project_id: str, suffix: str, status: str, created_at: datetime,
         updated_at: datetime | None = None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO jobs (id, project_id, kind, status, created_at, updated_at)
            VALUES (:id, :p, 'probe', :status, :created, :updated)
            """
        ),
        {
            "id": f"job_{suffix}",
            "p": project_id,
            "status": status,
            "created": created_at,
            "updated": updated_at or created_at,
        },
    )


def _decision(connection, project_id: str, suffix: str, status: str, created_at: datetime,
              approved_at: datetime | None = None) -> None:
    connection.execute(
        text(
            """
            INSERT INTO decisions (id, project_id, question, status, created_at, approved_at)
            VALUES (:id, :p, :question, :status, :created, :approved)
            """
        ),
        {
            "id": f"dec_{suffix}",
            "p": project_id,
            "question": f"approve {suffix}",
            "status": status,
            "created": created_at,
            "approved": approved_at,
        },
    )


def test_this_window_compares_finished_failures_and_approvals_with_the_previous_one(
        eng, project):
    from boss_engine.services.report_compare import report_compare

    other = make_project(eng)
    try:
        with eng.begin() as connection:
            _job(connection, project, "cur_start", "failed_final", SINCE)
            _job(connection, project, "cur_end", "deadlettered", UNTIL - timedelta(seconds=1))
            _job(connection, project, "prev_start", "deadlettered", PREVIOUS)
            _job(connection, project, "prev_mid", "failed_final", PREVIOUS + timedelta(days=3))
            _job(connection, project, "prev_end", "failed_final", SINCE - timedelta(seconds=1))
            _job(connection, project, "at_until", "failed_final", UNTIL)
            _job(connection, project, "before", "deadlettered", PREVIOUS - timedelta(seconds=1))
            _job(connection, project, "retry", "failed_retryable", SINCE + timedelta(days=1))
            _job(connection, project, "cancel", "cancelled", SINCE + timedelta(days=1))
            _job(connection, project, "ok", "succeeded", SINCE + timedelta(days=2))
            _job(connection, project, "invented", "failed_terminal", SINCE + timedelta(days=2))
            _job(connection, project, "touched_later", "failed_final",
                 PREVIOUS - timedelta(days=10), SINCE + timedelta(days=1))
            _job(connection, other, "other", "failed_final", SINCE + timedelta(hours=1))

            for offset in (0, 1, 2, 3):
                _decision(connection, project, f"cur_{offset}", "approved",
                          SINCE + timedelta(days=offset))
            _decision(connection, project, "cur_end", "approved", UNTIL - timedelta(seconds=1))
            _decision(connection, project, "prev_start", "approved", PREVIOUS)
            _decision(connection, project, "prev_mid", "approved", PREVIOUS + timedelta(days=2))
            _decision(connection, project, "prev_end", "approved", SINCE - timedelta(seconds=1))
            _decision(connection, project, "at_until", "approved", UNTIL)
            _decision(connection, project, "before", "approved", PREVIOUS - timedelta(seconds=1))
            _decision(connection, project, "pending", "pending", SINCE + timedelta(days=1))
            _decision(connection, project, "rejected", "rejected", SINCE + timedelta(days=1))
            _decision(connection, project, "old_created", "approved",
                      PREVIOUS - timedelta(days=10), SINCE + timedelta(days=1))
            _decision(connection, other, "other", "approved", SINCE + timedelta(hours=1))

            report = report_compare(connection, project, SINCE, UNTIL)
    finally:
        purge(eng, other)

    assert report == {
        "project_id": project,
        "since": SINCE,
        "until": UNTIL,
        "finished_failures": {"current": 2, "previous": 3, "percent": -33.3},
        "decisions_approved": {"current": 5, "previous": 3, "percent": 66.7},
    }


def test_a_zero_previous_count_has_no_percent(eng, project):
    from boss_engine.services.report_compare import report_compare

    with eng.begin() as connection:
        _job(connection, project, "only_now", "failed_final", SINCE + timedelta(hours=2))
        _decision(connection, project, "only_now", "approved", SINCE + timedelta(hours=3))
        _decision(connection, project, "only_now_2", "approved", SINCE + timedelta(hours=4))
        current = report_compare(connection, project, SINCE, UNTIL)
        empty = report_compare(
            connection, project, UNTIL + timedelta(days=30), UNTIL + timedelta(days=37),
        )

    assert current["finished_failures"] == {"current": 1, "previous": 0, "percent": None}
    assert current["decisions_approved"] == {"current": 2, "previous": 0, "percent": None}
    assert empty["finished_failures"]["percent"] is None
    assert empty["decisions_approved"]["percent"] is None
    assert empty["finished_failures"]["current"] == 0
    assert empty["decisions_approved"]["previous"] == 0
