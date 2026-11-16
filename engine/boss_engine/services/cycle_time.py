"""Cycle time: seconds from a code task's start to merge, and to a healthy deploy.

The window is half-open on the end timestamp, [since, until), not on created_at.
A run with no merge is absent from merged. A run with no healthy deploy is absent
from deployed. The median of an even count is the lower of the two middle values.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from sqlalchemy import text

_MERGE_SECONDS = """
SELECT EXTRACT(EPOCH FROM (ended_at - created_at)) AS seconds
FROM (
    SELECT r.created_at, MIN(src.ended_at) AS ended_at
    FROM code_task_runs r
    JOIN (
        SELECT p.code_task_run_id AS run_id, p.merged_at AS ended_at
        FROM change_set_prs p
        JOIN repo_change_sets cs ON cs.id = p.change_set_id
        WHERE cs.project_id = :project_id
          AND p.merged_at IS NOT NULL
        UNION ALL
        SELECT l.run_id, l.merged_at
        FROM local_change_sets l
        WHERE l.project_id = :project_id
          AND l.run_id IS NOT NULL
          AND l.merged_at IS NOT NULL
    ) src ON src.run_id = r.id
    WHERE r.project_id = :project_id
    GROUP BY r.id, r.created_at
) ends
WHERE ended_at >= :since
  AND ended_at < :until
  AND ended_at >= created_at
"""

_DEPLOY_SECONDS = """
SELECT EXTRACT(EPOCH FROM (ended_at - created_at)) AS seconds
FROM (
    SELECT r.created_at, MIN(d.finished_at) AS ended_at
    FROM code_task_runs r
    JOIN stack_deployments d
      ON d.code_task_run_id = r.id
     AND d.project_id = r.project_id
     AND d.status = 'healthy'
     AND d.finished_at IS NOT NULL
    WHERE r.project_id = :project_id
    GROUP BY r.id, r.created_at
) ends
WHERE ended_at >= :since
  AND ended_at < :until
  AND ended_at >= created_at
"""


def _moment(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    return datetime.fromisoformat(text_value)


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _moment(value).isoformat()


def _whole_seconds(connection: Any, sql: str, project_id: str, since: datetime,
                   until: datetime) -> list[int]:
    rows = connection.execute(text(sql), {
        "project_id": project_id, "since": since, "until": until,
    }).scalars().all()
    # Elapsed seconds are whole seconds. int() truncates toward zero; a fraction
    # is not rounded up into a second the run did not finish.
    return [int(row) for row in rows]


def _summary(seconds: list[int]) -> dict[str, int | None]:
    if not seconds:
        return {"n": 0, "median_seconds": None}
    return {"n": len(seconds), "median_seconds": int(statistics.median_low(seconds))}


def cycle_time(connection: Any, project_id: str, since: datetime | str,
               until: datetime | str) -> dict[str, Any]:
    """Start-to-merge and start-to-healthy-deploy for one project over [since, until)."""
    start = _moment(since)
    end = _moment(until)
    merged = _whole_seconds(connection, _MERGE_SECONDS, project_id, start, end)
    deployed = _whole_seconds(connection, _DEPLOY_SECONDS, project_id, start, end)
    return {
        "project_id": str(project_id),
        "since": _iso(since),
        "until": _iso(until),
        "merged": _summary(merged),
        "deployed": _summary(deployed),
    }
