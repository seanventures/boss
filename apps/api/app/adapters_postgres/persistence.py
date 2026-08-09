from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Optional

from sqlalchemy import text

from ..db import begin


def _row_to_run(row: Any) -> dict[str, Any]:
    def decoded(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value or {}

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "status": row["status"],
        "phase": row["phase"],
        "title": row["title"],
        "spec": decoded(row["spec"]),
        "state": decoded(row["state"]),
        "usage": decoded(row["usage"]),
        "finalize_ctx": decoded(row["finalize_ctx"]),
        "reservation_id": row["reservation_id"],
        "pull_request_url": row["pull_request_url"],
        "pull_request_number": row["pull_request_number"],
        "decision_id": row["decision_id"],
        "error": row["error"],
    }


class PostgresPersistence:
    """Postgres implementation of the engine persistence boundary."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def transaction(self, engine: Any = None) -> AbstractContextManager[Any]:
        return begin(engine or self.engine)

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with begin(self.engine) as connection:
            row = connection.execute(
                text(
                    "SELECT id, project_id, task_id, status, phase, title, spec, "
                    "state, usage, finalize_ctx, reservation_id, pull_request_url, "
                    "pull_request_number, decision_id, error "
                    "FROM code_task_runs WHERE id = :id"
                ),
                {"id": run_id},
            ).mappings().first()
        return _row_to_run(row) if row else None

    def list_runs(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with begin(self.engine) as connection:
            rows = connection.execute(
                text(
                    "SELECT id, project_id, task_id, status, phase, title, spec, "
                    "state, usage, finalize_ctx, reservation_id, pull_request_url, "
                    "pull_request_number, decision_id, error FROM code_task_runs "
                    "WHERE project_id = :project_id ORDER BY created_at DESC LIMIT :limit"
                ),
                {"project_id": project_id, "limit": int(limit)},
            ).mappings().all()
        return [_row_to_run(row) for row in rows]

    def save_run(self, run_id: str, state: dict[str, Any]) -> None:
        payload = dict(state)
        with begin(self.engine) as connection:
            connection.execute(
                text(
                    "UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) "
                    "|| CAST(:state AS JSONB), updated_at = now() WHERE id = :run_id"
                ),
                {"run_id": run_id, "state": json.dumps(payload)},
            )
