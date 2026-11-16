"""How many stack deploys in a window went live, failed, rolled back, or are still open.

A row is a rollback when rollback_of is set, even if its status is healthy or failed.
Otherwise healthy is live, failed is failed, and every other status in the window is
open (planned, awaiting approval, running, rejected). The window is half-open on
created_at: [since, until). Counts are integers. No rate is returned.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text


def deploy_outcomes(connection, project_id: str, since: Any, until: Any) -> dict[str, Any]:
    """Count this project's stack_deployments whose created_at is in [since, until)."""
    row = connection.execute(text(
        "SELECT count(*) FILTER (WHERE status = 'healthy' AND rollback_of IS NULL) AS live, "
        "count(*) FILTER (WHERE status = 'failed' AND rollback_of IS NULL) AS failed, "
        "count(*) FILTER (WHERE rollback_of IS NOT NULL) AS rollback, "
        "count(*) FILTER (WHERE rollback_of IS NULL "
        "AND status NOT IN ('healthy', 'failed')) AS open "
        "FROM stack_deployments "
        "WHERE project_id = :project_id "
        "AND created_at >= :since AND created_at < :until"),
        {"project_id": project_id, "since": since, "until": until}).mappings().one()
    return {
        "project_id": project_id,
        "since": since,
        "until": until,
        "live": int(row["live"]),
        "failed": int(row["failed"]),
        "rollback": int(row["rollback"]),
        "open": int(row["open"]),
    }
