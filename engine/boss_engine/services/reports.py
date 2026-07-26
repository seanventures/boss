from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import now_utc

FAILED_STATES = "('failed_terminal','failed_retryable','deadlettered')"


class ReportService:
    """Read-only analytics aggregations for the FinOps/operations report views."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def build_summary(self, connection: Connection, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "generated_at": now_utc().isoformat(),
            "windows": {
                "today": self._window(connection, project_id, "date_trunc('day', now())"),
                "week": self._window(connection, project_id, "now() - interval '7 days'"),
                "month": self._window(connection, project_id, "now() - interval '30 days'"),
            },
            "daily": self._daily(connection, project_id),
            "by_kind": self._by_kind(connection, project_id),
            "by_model": self._by_model(connection, project_id),
            "agents": self._agents(connection, project_id),
            "decision_latency": self._decision_latency(connection, project_id),
        }

    def _window(self, connection: Connection, project_id: str, since: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT
                  (SELECT count(*) FROM tasks WHERE project_id=:p AND created_at >= {since}) AS tasks,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since}) AS jobs,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since} AND status='succeeded') AS jobs_succeeded,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since} AND status IN {FAILED_STATES}) AS jobs_failed,
                  (SELECT count(*) FROM decisions WHERE project_id=:p AND created_at >= {since}) AS decisions,
                  (SELECT count(*) FROM decisions WHERE project_id=:p AND created_at >= {since} AND status='approved') AS decisions_approved,
                  (SELECT COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)),0)
                     FROM cost_receipts WHERE project_id=:p AND created_at >= {since}) AS spend_usd_micros
                """
            ),
            {"p": project_id},
        ).mappings().one()
        return {k: int(v) for k, v in row.items()}

    def _daily(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT to_char(d, 'YYYY-MM-DD') AS date,
                  (SELECT count(*) FROM tasks
                     WHERE project_id=:p AND date_trunc('day', created_at)=d) AS tasks,
                  (SELECT count(*) FROM jobs
                     WHERE project_id=:p AND date_trunc('day', created_at)=d AND status='succeeded') AS succeeded,
                  (SELECT count(*) FROM jobs
                     WHERE project_id=:p AND date_trunc('day', created_at)=d AND status IN {FAILED_STATES}) AS failed
                FROM generate_series(
                  date_trunc('day', now()) - interval '13 days',
                  date_trunc('day', now()),
                  interval '1 day'
                ) AS d
                ORDER BY d
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [
            {
                "date": r["date"],
                "tasks": int(r["tasks"]),
                "succeeded": int(r["succeeded"]),
                "failed": int(r["failed"]),
            }
            for r in rows
        ]

    def _by_kind(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT kind,
                  count(*) AS total,
                  count(*) FILTER (WHERE status='succeeded') AS succeeded,
                  count(*) FILTER (WHERE status IN {FAILED_STATES}) AS failed,
                  COALESCE(avg(extract(epoch FROM (updated_at - created_at))), 0)::float8 AS avg_seconds
                FROM jobs WHERE project_id=:p
                GROUP BY kind ORDER BY total DESC
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [
            {
                "kind": r["kind"],
                "total": int(r["total"]),
                "succeeded": int(r["succeeded"]),
                "failed": int(r["failed"]),
                "avg_seconds": round(float(r["avg_seconds"]), 1),
            }
            for r in rows
        ]

    def _by_model(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT skill_or_model_id AS model,
                  count(*) AS count,
                  COALESCE(SUM(COALESCE(actual_usd_micros, estimated_usd_micros)),0) AS spend_usd_micros
                FROM cost_receipts WHERE project_id=:p
                GROUP BY skill_or_model_id ORDER BY spend_usd_micros DESC
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [
            {
                "model": r["model"],
                "count": int(r["count"]),
                "spend_usd_micros": int(r["spend_usd_micros"]),
            }
            for r in rows
        ]

    def _agents(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT role_key, display_name, trust_score, autonomy_level, successful_runs,
                       policy_violation_count, scope_violation_count,
                       last_reviewed_at, last_violation_at
                FROM roles WHERE project_id=:p
                ORDER BY (policy_violation_count + scope_violation_count) DESC, trust_score ASC, role_key
                """
            ),
            {"p": project_id},
        ).mappings().all()
        return [
            {
                "role_key": r["role_key"],
                "display_name": r["display_name"],
                "trust_score": int(r["trust_score"]),
                "autonomy_level": int(r["autonomy_level"]),
                "successful_runs": int(r["successful_runs"]),
                "policy_violations": int(r["policy_violation_count"]),
                "scope_violations": int(r["scope_violation_count"]),
                "last_reviewed_at": r["last_reviewed_at"].isoformat() if r["last_reviewed_at"] else None,
                "last_violation_at": r["last_violation_at"].isoformat() if r["last_violation_at"] else None,
            }
            for r in rows
        ]

    def _decision_latency(self, connection: Connection, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT count(*) AS count,
                  COALESCE(avg(extract(epoch FROM (approved_at - created_at))), 0)::float8 AS avg_seconds
                FROM decisions WHERE project_id=:p AND approved_at IS NOT NULL
                """
            ),
            {"p": project_id},
        ).mappings().one()
        return {"count": int(row["count"]), "avg_seconds": round(float(row["avg_seconds"]), 1)}
