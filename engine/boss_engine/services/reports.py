from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import now_utc
from .included_spend import INCLUDED_SQL, MONEY_SQL

# A job the worker has finished failing. failed_retryable is still scheduled.
# cancelled is not a failure. failed_terminal is not a status the worker writes.
FINISHED_FAILURES = "('failed_final','deadlettered')"

# Same bounds the headline counts use. The page opens on the week tab.
_WINDOWS = (
    ("today", "date_trunc('day', now())"),
    ("week", "now() - interval '7 days'"),
    ("month", "now() - interval '30 days'"),
)


def _created_since(since: str | None) -> str:
    if not since:
        return ""
    return f" AND created_at >= {since}"


class ReportService:
    """Read-only analytics aggregations for the FinOps/operations report views."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def build_summary(self, connection: Connection, project_id: str) -> dict[str, Any]:
        windows = {
            name: self._window_report(connection, project_id, since)
            for name, since in _WINDOWS
        }
        # Flat keys match the week window, the tab the page opens on.
        # Each window carries its own copy so the toggle is not all-time.
        week = windows["week"]
        return {
            "project_id": project_id,
            "generated_at": now_utc().isoformat(),
            "windows": windows,
            "daily": self._daily(connection, project_id),
            "by_kind": week["by_kind"],
            "by_model": week["by_model"],
            "llm_spend": week["llm_spend"],
            "agents": self._agents(connection, project_id),
            "decision_latency": week["decision_latency"],
        }

    def _window_report(self, connection: Connection, project_id: str, since: str) -> dict[str, Any]:
        return {
            **self._window(connection, project_id, since),
            "by_kind": self._by_kind(connection, project_id, since),
            "by_model": self._by_model(connection, project_id, since),
            "llm_spend": self._llm_spend(connection, project_id, since),
            "decision_latency": self._decision_latency(connection, project_id, since),
        }

    def _window(self, connection: Connection, project_id: str, since: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT
                  (SELECT count(*) FROM tasks WHERE project_id=:p AND created_at >= {since}) AS tasks,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since}) AS jobs,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since} AND status='succeeded') AS jobs_succeeded,
                  (SELECT count(*) FROM jobs WHERE project_id=:p AND created_at >= {since} AND status IN {FINISHED_FAILURES}) AS jobs_failed,
                  (SELECT count(*) FROM decisions WHERE project_id=:p AND created_at >= {since}) AS decisions,
                  (SELECT count(*) FROM decisions WHERE project_id=:p AND created_at >= {since} AND status='approved') AS decisions_approved,
                  (SELECT COALESCE(SUM({MONEY_SQL}),0)
                     FROM cost_receipts WHERE project_id=:p AND created_at >= {since}) AS spend_usd_micros,
                  (SELECT COALESCE(SUM({INCLUDED_SQL}),0)
                     FROM cost_receipts WHERE project_id=:p AND created_at >= {since})
                     AS included_in_subscription_usd_micros
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
                     WHERE project_id=:p AND date_trunc('day', created_at)=d AND status IN {FINISHED_FAILURES}) AS failed
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

    def _by_kind(self, connection: Connection, project_id: str,
                 since: str | None = None) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT kind,
                  count(*) AS total,
                  count(*) FILTER (WHERE status='succeeded') AS succeeded,
                  count(*) FILTER (WHERE status IN {FINISHED_FAILURES}) AS failed,
                  COALESCE(avg(extract(epoch FROM (updated_at - created_at))), 0)::float8 AS avg_seconds
                FROM jobs WHERE project_id=:p{_created_since(since)}
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

    def _by_model(self, connection: Connection, project_id: str,
                  since: str | None = None) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                f"""
                SELECT skill_or_model_id AS model,
                  count(*) AS count,
                  COALESCE(SUM({MONEY_SQL}),0) AS spend_usd_micros,
                  COALESCE(SUM({INCLUDED_SQL}),0) AS included_in_subscription_usd_micros
                FROM cost_receipts WHERE project_id=:p{_created_since(since)}
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
                "included_in_subscription_usd_micros": int(
                    r["included_in_subscription_usd_micros"]),
            }
            for r in rows
        ]

    def _llm_spend(self, connection: Connection, project_id: str,
                   since: str | None = None) -> dict[str, Any]:
        """The REAL model-spend ledger (llm_calls), segmented the way the cost question is
        actually asked (BOSS-280 P1): $/agent-role, $/model, and cache leverage (how many
        input tokens were served from cache vs paid fresh)."""
        by_role = connection.execute(
            text(
                f"""
                SELECT COALESCE(role, 'unattributed') AS role,
                  count(*) AS calls,
                  COALESCE(SUM(cost_usd_micros), 0) AS spend_usd_micros,
                  COALESCE(SUM(total_tokens), 0) AS tokens
                FROM llm_calls WHERE project_id=:p{_created_since(since)}
                GROUP BY COALESCE(role, 'unattributed') ORDER BY spend_usd_micros DESC
                """
            ),
            {"p": project_id},
        ).mappings().all()
        by_model = connection.execute(
            text(
                f"""
                SELECT COALESCE(model, 'unknown') AS model,
                  count(*) AS calls,
                  COALESCE(SUM(cost_usd_micros), 0) AS spend_usd_micros,
                  COALESCE(SUM(CASE WHEN price_estimated THEN 1 ELSE 0 END), 0) AS estimated_calls
                FROM llm_calls WHERE project_id=:p{_created_since(since)}
                GROUP BY COALESCE(model, 'unknown') ORDER BY spend_usd_micros DESC
                """
            ),
            {"p": project_id},
        ).mappings().all()
        cache = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM(prompt_tokens), 0) AS fresh_input,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write
                FROM llm_calls WHERE project_id=:p{_created_since(since)}
                """
            ),
            {"p": project_id},
        ).mappings().one()
        total_input = int(cache["fresh_input"]) + int(cache["cache_read"])
        return {
            "by_role": [{"role": r["role"], "calls": int(r["calls"]),
                         "spend_usd_micros": int(r["spend_usd_micros"]),
                         "tokens": int(r["tokens"])} for r in by_role],
            "by_model": [{"model": r["model"], "calls": int(r["calls"]),
                          "spend_usd_micros": int(r["spend_usd_micros"]),
                          "estimated_calls": int(r["estimated_calls"])} for r in by_model],
            "cache": {
                "fresh_input_tokens": int(cache["fresh_input"]),
                "cache_read_tokens": int(cache["cache_read"]),
                "cache_write_tokens": int(cache["cache_write"]),
                "cache_hit_ratio": (round(int(cache["cache_read"]) / total_input, 4)
                                    if total_input else None),
            },
        }

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

    def _decision_latency(self, connection: Connection, project_id: str,
                          since: str | None = None) -> dict[str, Any]:
        row = connection.execute(
            text(
                f"""
                SELECT count(*) AS count,
                  COALESCE(avg(extract(epoch FROM (approved_at - created_at))), 0)::float8 AS avg_seconds
                FROM decisions WHERE project_id=:p AND approved_at IS NOT NULL{_created_since(since)}
                """
            ),
            {"p": project_id},
        ).mappings().one()
        return {"count": int(row["count"]), "avg_seconds": round(float(row["avg_seconds"]), 1)}
