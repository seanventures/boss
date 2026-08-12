"""Durable worker capability and liveness inventory; never an execution authority."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id


class WorkerRegistryService:
    def register(self, connection: Connection, *, project_id: str, worker_id: str, capabilities: list[str], metadata: dict[str, Any], actor: str) -> dict[str, Any]:
        worker_id = worker_id.strip()
        if not worker_id or not capabilities or not all(isinstance(item, str) and item.strip() for item in capabilities):
            raise ValueError("worker_id and at least one capability are required.")
        row = connection.execute(text("""
            INSERT INTO worker_registrations (id, project_id, worker_id, capabilities, metadata, status)
            VALUES (:id,:project_id,:worker_id,CAST(:capabilities AS JSONB),CAST(:metadata AS JSONB),'active')
            ON CONFLICT (project_id,worker_id) DO UPDATE SET capabilities=EXCLUDED.capabilities,
              metadata=EXCLUDED.metadata,status='active',last_seen_at=now(),updated_at=now()
            RETURNING *
        """), {"id": make_id("worker"), "project_id": project_id, "worker_id": worker_id, "capabilities": json.dumps(sorted(set(capabilities))), "metadata": json.dumps(metadata)}).mappings().one()
        emit_event(connection, project_id, "worker.registered", actor, "worker", row["id"], {"worker_id": worker_id, "capabilities": row["capabilities"]})
        return self._serialize(row)

    def list(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(text("SELECT *, CASE WHEN last_seen_at >= now() - interval '2 minutes' THEN 'live' ELSE 'stale' END AS liveness FROM worker_registrations WHERE project_id=:project_id ORDER BY last_seen_at DESC"), {"project_id": project_id}).mappings().all()
        return [self._serialize(row) for row in rows]

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "project_id": row["project_id"], "worker_id": row["worker_id"], "capabilities": row["capabilities"] or [], "metadata": row["metadata"] or {}, "status": row["status"], "liveness": row.get("liveness", "live"), "last_seen_at": row["last_seen_at"].isoformat(), "created_at": row["created_at"].isoformat()}
