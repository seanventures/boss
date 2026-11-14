"""Durable worker capability and liveness inventory; never an execution authority.

AP-P3 (AGENT_PLATFORM §4.4 "Scaling"): a worker REGISTERS on start and again every minute
(POST /v1/workers/register), advertising its lease slots (`metadata.slots`, `busy_slots`, `host`).
`scaling_signal` turns that into the one number an autoscaler needs: queued agent runs against
free live slots.
"""
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
        slots = max(1, int((metadata or {}).get("slots") or 1))
        busy = max(0, min(slots, int((metadata or {}).get("busy_slots") or 0)))
        host = str((metadata or {}).get("host") or "")[:120] or None
        existed = connection.execute(text(
            "SELECT 1 FROM worker_registrations WHERE project_id = :p AND worker_id = :w"),
            {"p": project_id, "w": worker_id}).scalar()
        row = connection.execute(text("""
            INSERT INTO worker_registrations (id, project_id, worker_id, capabilities, metadata, status,
                                              slots, busy_slots, host)
            VALUES (:id,:project_id,:worker_id,CAST(:capabilities AS JSONB),CAST(:metadata AS JSONB),'active',
                    :slots, :busy, :host)
            ON CONFLICT (project_id,worker_id) DO UPDATE SET capabilities=EXCLUDED.capabilities,
              metadata=EXCLUDED.metadata,status='active',last_seen_at=now(),updated_at=now(),
              slots=EXCLUDED.slots, busy_slots=EXCLUDED.busy_slots, host=EXCLUDED.host
            RETURNING *
        """), {"id": make_id("worker"), "project_id": project_id, "worker_id": worker_id, "capabilities": json.dumps(sorted(set(capabilities))), "metadata": json.dumps(metadata), "slots": slots, "busy": busy, "host": host}).mappings().one()
        if not existed:
            # The FIRST registration is an event; the minute-by-minute re-registration is a
            # heartbeat and writes nothing to the event log.
            emit_event(connection, project_id, "worker.registered", actor, "worker", row["id"], {"worker_id": worker_id, "capabilities": row["capabilities"], "slots": slots})
        return self._serialize(row)

    def list(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(text("SELECT *, CASE WHEN last_seen_at >= now() - interval '2 minutes' THEN 'live' ELSE 'stale' END AS liveness FROM worker_registrations WHERE project_id=:project_id ORDER BY last_seen_at DESC"), {"project_id": project_id}).mappings().all()
        return [self._serialize(row) for row in rows]

    @staticmethod
    def _serialize(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "project_id": row["project_id"], "worker_id": row["worker_id"], "capabilities": row["capabilities"] or [], "metadata": row["metadata"] or {}, "status": row["status"], "liveness": row.get("liveness", "live"), "slots": int(row.get("slots") or 1), "busy_slots": int(row.get("busy_slots") or 0), "host": row.get("host"), "last_seen_at": row["last_seen_at"].isoformat(), "created_at": row["created_at"].isoformat()}


#: A worker not seen for this long is not counted (it re-registers every minute).
LIVE_WITHIN_SECONDS = 120
#: The run kinds that take a worker slot (AP-P2: agent runs execute on workers).
AGENT_JOB_KINDS = ("agent.run", "agent.resume")


def scaling_signal(connection: Connection, project_id: str) -> dict[str, Any]:
    """Slot-based scaling (§4.4): live workers and their slots, agent runs queued for a slot and
    runs waiting behind another on their thread, and what that says: scale_up (more queued than
    free slots, or nobody alive), scale_down (idle slots and nothing queued), or steady."""
    workers = connection.execute(text(
        "SELECT count(*) AS n, COALESCE(sum(slots), 0) AS slots, "
        "COALESCE(sum(busy_slots), 0) AS busy FROM worker_registrations "
        "WHERE project_id = :p AND status = 'active' "
        "AND last_seen_at >= now() - make_interval(secs => :s)"),
        {"p": project_id, "s": LIVE_WITHIN_SECONDS}).mappings().one()
    queued = int(connection.execute(text(
        "SELECT count(*) FROM jobs WHERE project_id = :p AND kind = ANY(:k) "
        "AND status IN ('queued', 'failed_retryable')"),
        {"p": project_id, "k": list(AGENT_JOB_KINDS)}).scalar() or 0)
    waiting = int(connection.execute(text(
        "SELECT count(*) FROM agent_runs WHERE project_id = :p AND status = 'pending'"),
        {"p": project_id}).scalar() or 0)
    live, slots, busy = int(workers["n"]), int(workers["slots"]), int(workers["busy"])
    free = max(0, slots - busy)
    if queued and not live:
        signal, words = "scale_up", f"{queued} agent run(s) queued and no worker is alive."
    elif queued > free:
        signal = "scale_up"
        words = f"{queued} agent run(s) queued for {free} free slot(s)."
    elif not queued and slots > 1 and busy * 2 < slots:
        signal, words = "scale_down", f"{slots - busy} of {slots} slots idle and nothing queued."
    else:
        signal, words = "steady", f"{busy} of {slots} slots busy, {queued} queued."
    return {"live_workers": live, "slots": slots, "busy_slots": busy, "free_slots": free,
            "queued_runs": queued, "waiting_runs": waiting,
            "desired_slots": max(slots, busy + queued) if signal == "scale_up"
            else (max(1, busy) if signal == "scale_down" else slots),
            "signal": signal, "words": words}
