"""Compare two report counts with the previous window of the same length."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _percent(current: int, previous: int) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) * 100 / previous, 1)


def _counts(connection: Connection, table: str, status_sql: str, project_id: str,
            since: datetime, until: datetime) -> tuple[int, int]:
    previous_since = since - (until - since)
    row = connection.execute(
        text(
            f"""
            SELECT
              count(*) FILTER (
                WHERE created_at >= :since AND created_at < :until
                  AND {status_sql}
              ) AS current,
              count(*) FILTER (
                WHERE created_at >= :previous_since AND created_at < :since
                  AND {status_sql}
              ) AS previous
            FROM {table}
            WHERE project_id = :p
              AND created_at >= :previous_since
              AND created_at < :until
            """
        ),
        {
            "p": project_id,
            "previous_since": previous_since,
            "since": since,
            "until": until,
        },
    ).one()
    return int(row.current), int(row.previous)


def report_compare(connection: Connection, project_id: str, since: datetime,
                   until: datetime) -> dict[str, Any]:
    """This window is [since, until). The previous window has the same length and ends at since.

    Finished failures are jobs in failed_final or deadlettered. Approvals are decisions
    whose status is approved. Both follow created_at on this project. percent is None
    when the previous count is 0.
    """
    failures = _counts(
        connection, "jobs", "status IN ('failed_final', 'deadlettered')",
        project_id, since, until,
    )
    approvals = _counts(
        connection, "decisions", "status = 'approved'",
        project_id, since, until,
    )
    return {
        "project_id": project_id,
        "since": since,
        "until": until,
        "finished_failures": {
            "current": failures[0],
            "previous": failures[1],
            "percent": _percent(*failures),
        },
        "decisions_approved": {
            "current": approvals[0],
            "previous": approvals[1],
            "percent": _percent(*approvals),
        },
    }
