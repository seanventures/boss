"""Split one window of model spend into shipped, failed-or-retried, and in progress.

A call is shipped when its code_task_run_id is on a change_set_prs row with
merged_at set, or on a stack_deployments row with status healthy. Shipped wins.
Anything else is failed-or-retried when the call is not ok, retries is above
zero, or the linked code_task_runs.status is failed. The rest of [since, until)
is still in progress. A null code_task_run_id is never shipped. Money is
included_spend.LLM_MONEY_SQL, so a subscription row contributes 0.
"""
from __future__ import annotations

from typing import Any

from .included_spend import LLM_MONEY_SQL

_SPLIT = f"""
SELECT
    COALESCE(SUM(money) FILTER (WHERE shipped), 0) AS shipped_usd_micros,
    COALESCE(SUM(money) FILTER (WHERE NOT shipped AND failed), 0)
        AS failed_or_retried_usd_micros,
    COALESCE(SUM(money) FILTER (WHERE NOT shipped AND NOT failed), 0)
        AS in_progress_usd_micros
FROM (
    SELECT
        {LLM_MONEY_SQL} AS money,
        (
            l.code_task_run_id IS NOT NULL
            AND (
                EXISTS (
                    SELECT 1 FROM change_set_prs p
                    WHERE p.code_task_run_id = l.code_task_run_id
                      AND p.merged_at IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM stack_deployments d
                    WHERE d.code_task_run_id = l.code_task_run_id
                      AND d.status = 'healthy'
                )
            )
        ) AS shipped,
        (
            l.ok IS FALSE
            OR l.retries > 0
            OR EXISTS (
                SELECT 1 FROM code_task_runs r
                WHERE r.id = l.code_task_run_id
                  AND r.status = 'failed'
            )
        ) AS failed
    FROM llm_calls l
    WHERE l.project_id = :project_id
      AND l.created_at >= :since
      AND l.created_at < :until
) classified
"""


def spend_outcome(connection: Any, project_id: str, since: Any, until: Any) -> dict[str, Any]:
    """Integer micros for one project's calls in [since, until). One bucket each."""
    from sqlalchemy import text

    shipped, failed, pending = connection.execute(
        text(_SPLIT), {"project_id": project_id, "since": since, "until": until}).one()
    return {
        "project_id": project_id,
        "since": since,
        "until": until,
        "shipped_usd_micros": int(shipped),
        "failed_or_retried_usd_micros": int(failed),
        "in_progress_usd_micros": int(pending),
    }
