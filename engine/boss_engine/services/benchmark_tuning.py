"""Durable collection of terminal QA-benchmark evidence.

The scheduler intentionally stops at a recommendation snapshot.  A benchmark result
is evidence about a run, not proof that a model route or workflow configuration caused
the result, so this service never edits configuration or creates a rollout on its own.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .benchmark_apps import completion_report, tuning_recommendation
from .core import emit_event, json_hash, make_id
from .code_task_service import load_code_task_run


TERMINAL_STATUSES = ("completed", "failed", "blocked")


def _llm_calls(connection: Connection, run_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT total_tokens, cost_usd_micros, latency_ms, turns, ok "
            "FROM llm_calls WHERE code_task_run_id=:run_id ORDER BY created_at"
        ),
        {"run_id": run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _terminal_benchmark_run_ids(
    connection: Connection, project_id: str | None, limit: int
) -> list[str]:
    scope = "AND project_id=:project_id" if project_id else ""
    params: dict[str, Any] = {"limit": limit, "statuses": list(TERMINAL_STATUSES)}
    if project_id:
        params["project_id"] = project_id
    rows = connection.execute(
        text(
            f"""
            SELECT id
            FROM code_task_runs
            WHERE status = ANY(:statuses)
              AND finalize_ctx ? 'qa_benchmark'
              {scope}
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).scalars().all()
    return [str(run_id) for run_id in rows]


def _record_evaluation(
    connection: Connection,
    *,
    report: dict[str, Any],
    recommendation: dict[str, Any],
    project_id: str,
    actor: str,
) -> tuple[dict[str, Any], bool]:
    scorecard = report["scorecard"]
    benchmark_id = str(report.get("benchmark_id") or "unknown")
    version = str(scorecard.get("version") or "unknown")
    source_hash = json_hash({"report": report, "recommendation": recommendation})
    existing = connection.execute(
        text(
            """
            SELECT id, source_hash, created_at, updated_at
            FROM qa_benchmark_evaluations
            WHERE code_task_run_id=:run_id AND scorecard_version=:version
            """
        ),
        {"run_id": report["run_id"], "version": version},
    ).mappings().first()
    changed = existing is None or existing["source_hash"] != source_hash
    evaluation_id = str(existing["id"]) if existing else make_id("qbeval")
    if changed:
        connection.execute(
            text(
                """
                INSERT INTO qa_benchmark_evaluations (
                    id, project_id, code_task_run_id, benchmark_id, scorecard_version,
                    status, overall_score, observed_weight, report, recommendation,
                    source_hash, evaluated_by
                ) VALUES (
                    :id, :project_id, :run_id, :benchmark_id, :version,
                    :status, :overall_score, :observed_weight, CAST(:report AS JSONB),
                    CAST(:recommendation AS JSONB), :source_hash, :actor
                )
                ON CONFLICT (code_task_run_id, scorecard_version) DO UPDATE SET
                    status=EXCLUDED.status,
                    overall_score=EXCLUDED.overall_score,
                    observed_weight=EXCLUDED.observed_weight,
                    report=EXCLUDED.report,
                    recommendation=EXCLUDED.recommendation,
                    source_hash=EXCLUDED.source_hash,
                    evaluated_by=EXCLUDED.evaluated_by,
                    updated_at=now()
                """
            ),
            {
                "id": evaluation_id,
                "project_id": project_id,
                "run_id": report["run_id"],
                "benchmark_id": benchmark_id,
                "version": version,
                "status": recommendation["status"],
                "overall_score": scorecard.get("overall_score"),
                "observed_weight": int(scorecard.get("observed_weight") or 0),
                "report": json.dumps(report),
                "recommendation": json.dumps(recommendation),
                "source_hash": source_hash,
                "actor": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "qa_benchmark.evaluation_recorded",
            actor,
            "qa_benchmark_evaluation",
            evaluation_id,
            {
                "code_task_run_id": report["run_id"],
                "benchmark_id": benchmark_id,
                "scorecard_version": version,
                "recommendation_status": recommendation["status"],
                "overall_score": scorecard.get("overall_score"),
                "observed_weight": scorecard.get("observed_weight"),
            },
        )
    return {
        "id": evaluation_id,
        "project_id": project_id,
        "code_task_run_id": report["run_id"],
        "benchmark_id": benchmark_id,
        "scorecard_version": version,
        "status": recommendation["status"],
        "overall_score": scorecard.get("overall_score"),
        "observed_weight": int(scorecard.get("observed_weight") or 0),
        "changed": changed,
    }, changed


def sweep_terminal_benchmarks(
    connection: Connection,
    *,
    project_id: str | None = None,
    actor: str = "qa_benchmark_scheduler",
    limit: int = 100,
) -> dict[str, Any]:
    """Persist changed terminal reports as evaluation snapshots, idempotently."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500.")
    evaluations: list[dict[str, Any]] = []
    changed_count = 0
    for run_id in _terminal_benchmark_run_ids(connection, project_id, limit):
        run = load_code_task_run(connection, run_id)
        if not run or not (run.get("finalize_ctx") or {}).get("qa_benchmark"):
            continue
        report = completion_report(run, llm_calls=_llm_calls(connection, run_id))
        recommendation = tuning_recommendation(report)
        evaluation, changed = _record_evaluation(
            connection,
            report=report,
            recommendation=recommendation,
            project_id=run["project_id"],
            actor=actor,
        )
        evaluations.append(evaluation)
        changed_count += int(changed)
    return {
        "status": "succeeded",
        "project_id": project_id,
        "checked_count": len(evaluations),
        "changed_count": changed_count,
        "ready_for_candidate_count": sum(
            item["status"] == "ready_for_candidate" for item in evaluations
        ),
        "items": evaluations,
    }


def list_evaluations(
    connection: Connection, *, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT id, project_id, code_task_run_id, benchmark_id, scorecard_version,
                   status, overall_score, observed_weight, recommendation, evaluated_by,
                   created_at, updated_at
            FROM qa_benchmark_evaluations
            WHERE project_id=:project_id
            ORDER BY updated_at DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "limit": limit},
    ).mappings().all()
    return [dict(row) for row in rows]
