from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from ..config import Settings
from .agent_runtime import AgentRuntimeService
from .budget import BudgetGovernor
from .comms import CommsService
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    make_id,
    now_utc,
    normalize_data_boundary,
)
from .work_orchestration import WorkOrchestrationService


class LeaseNotFoundError(RuntimeError):
    pass


class JobDeferredError(RuntimeError):
    """The job was not leased because budget or capacity policy refused it."""


def resolve_job_data_boundary(
    connection: Connection,
    task_id: Optional[str],
    payload: dict[str, Any],
    data_boundary: Optional[str],
) -> str:
    """Resolve a job boundary without constructing the budget-aware lease service."""
    if data_boundary:
        return normalize_data_boundary(data_boundary)
    payload_boundary = payload.get("data_boundary")
    if isinstance(payload_boundary, str) and payload_boundary.strip():
        return normalize_data_boundary(payload_boundary)
    if task_id:
        row = connection.execute(
            text("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        if row:
            return normalize_data_boundary(row["data_boundary"])
    return "internal"



#: Which run table a job kind's ``payload["run_id"]`` names.
#:
#: CANCEL MUST BE AN INSTRUCTION, NOT A RACE.
#:
#: Every phase enqueues its successor when it finishes, and nothing consulted the run's status
#: before doing so. Cancelling a meeting set the run to `cancelled` and the worker then queued
#: the NEXT `meeting.step` for it anyway; it had to be cancelled repeatedly before it stayed
#: clean. `cancel_queued_job` closes the run and the job THAT EXISTS — it cannot close one that
#: has not been created yet. On a paid path that is spend continuing after an operator said stop.
#:
#: The check lives here, in the one function every enqueue passes through, keyed on a field
#: these payloads already carry. Wiring it into each caller instead is how the same rule ends up
#: expressed in seven places and satisfied in five (S3).
_RUN_TABLE_FOR_KIND: dict[str, str] = {
    "meeting.step": "meeting_runs",
    "code.task": "code_task_runs",
    "discovery.step": "discovery_runs",
    "discovery.run": "discovery_runs",
}

#: A run in one of these can never advance, so nothing should be queued on its behalf.
_TERMINAL_RUN_STATES = ("completed", "failed", "cancelled")


def stopped_run_reason(
    connection: Connection, kind: str, payload: dict[str, Any]
) -> Optional[str]:
    """Why this job must not be queued, or None to proceed.

    Extracted so the RULE can be tested without going through the INSERT — which uses
    Postgres-only `ON CONFLICT ... DO NOTHING RETURNING` and therefore cannot run on a
    throwaway SQLite database. A rule that can only be exercised via the one statement that
    needs production infrastructure is a rule that quietly stops being exercised.

    Fails OPEN on every uncertainty — an unmapped kind, an absent run row, a table this edition
    does not have. Blocking an enqueue we are unsure about would deadlock the ordinary path,
    which is a worse failure than the one this prevents.
    """
    table = _RUN_TABLE_FOR_KIND.get(kind)
    run_id = (payload or {}).get("run_id")
    if not table or not run_id:
        return None
    try:
        cur = connection.execute(
            text(f"SELECT status FROM {table} WHERE id = :id"),  # noqa: S608 - fixed map above
            {"id": run_id},
        ).first()
    except Exception:  # noqa: BLE001
        return None
    if cur is None:
        return None  # the run row is sometimes written after its first job
    status = str(cur[0] or "")
    if status not in _TERMINAL_RUN_STATES:
        return None
    return f"{table[:-1]} {run_id} is {status}; not queueing further work for it"


def enqueue_job(
    connection: Connection,
    project_id: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
    priority: int = 100,
    max_attempts: int = 3,
    run_after_seconds: int = 0,
    data_boundary: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    work_item_id: Optional[str] = None,
    tracker_binding_id: Optional[str] = None,
    allocation_id: Optional[str] = None,
    priority_score: Optional[float] = None,
    workload_class: str = "general",
    resource_units: int = 1,
) -> dict[str, Any]:
    """Insert a queued job without importing budget, entitlement, or agent services.

    A non-null ``dedupe_key`` makes enqueue idempotent within a project.  Budget
    and reservation enforcement intentionally remains at lease time.
    """
    job_payload = payload or {}
    boundary = resolve_job_data_boundary(
        connection, task_id, job_payload, data_boundary
    )
    job_id = make_id("job")
    run_after = now_utc() + timedelta(seconds=run_after_seconds)
    # Fail closed on bad FK refs (avoid IntegrityError → 500)
    for table, column, value in (
        ("tasks", "task_id", task_id),
        ("work_items", "work_item_id", work_item_id),
        ("work_tracker_bindings", "tracker_binding_id", tracker_binding_id),
        ("resource_allocations", "allocation_id", allocation_id),
    ):
        if value:
            exists = connection.execute(
                text(
                    f"SELECT 1 FROM {table} WHERE id = :id AND project_id = :project_id LIMIT 1"
                ),
                {"id": value, "project_id": project_id},
            ).first()
            if not exists:
                raise ValueError(
                    f"{column} references an unknown record in this project."
                )

    # Refuse to queue work for a run that has already stopped. See _RUN_TABLE_FOR_KIND.
    _skip = stopped_run_reason(connection, kind, job_payload)
    if _skip:
        return {
            "id": None, "project_id": project_id, "task_id": task_id,
            "kind": kind, "status": "skipped", "created": False,
            "dedupe_key": None,
            "skipped_reason": _skip,
        }

    row = connection.execute(
        text(
            """
            INSERT INTO jobs (
                id, project_id, task_id, work_item_id, tracker_binding_id,
                allocation_id, kind, priority, priority_score, workload_class,
                resource_units, status, payload, data_boundary, max_attempts,
                attempt, run_after, dedupe_key
            )
            VALUES (
                :id, :project_id, :task_id, :work_item_id, :tracker_binding_id,
                :allocation_id, :kind, :priority, :priority_score,
                :workload_class, :resource_units, 'queued',
                CAST(:payload AS JSONB), :data_boundary, :max_attempts,
                0, :run_after, :dedupe_key
            )
            ON CONFLICT (project_id, dedupe_key)
              WHERE dedupe_key IS NOT NULL
            DO NOTHING
            RETURNING id, project_id, task_id, work_item_id,
                      tracker_binding_id, allocation_id, kind, status,
                      data_boundary, priority, priority_score, workload_class,
                      resource_units, max_attempts, attempt, run_after, dedupe_key
            """
        ),
        {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "work_item_id": work_item_id,
            "tracker_binding_id": tracker_binding_id,
            "allocation_id": allocation_id,
            "kind": kind,
            "priority": priority,
            "priority_score": priority_score,
            "workload_class": workload_class,
            "resource_units": max(1, int(resource_units)),
            "payload": json.dumps(job_payload),
            "data_boundary": boundary,
            "max_attempts": max_attempts,
            "run_after": run_after,
            "dedupe_key": dedupe_key,
        },
    ).mappings().first()
    created = row is not None
    if row is None:
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, work_item_id,
                       tracker_binding_id, allocation_id, kind, status,
                       data_boundary, priority, priority_score, workload_class,
                       resource_units, max_attempts, attempt, run_after, dedupe_key
                FROM jobs
                WHERE project_id = :project_id AND dedupe_key = :dedupe_key
                """
            ),
            {"project_id": project_id, "dedupe_key": dedupe_key},
        ).mappings().first()
    if row is None:
        raise RuntimeError("Job enqueue did not create or find a deduplicated job.")

    if created:
        emit_event(
            connection,
            project_id,
            "job.created",
            "control_plane",
            "job",
            row["id"],
            {
                "kind": kind,
                "task_id": task_id,
                "data_boundary": boundary,
                "dedupe_key": dedupe_key,
            },
        )
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "work_item_id": row["work_item_id"],
        "tracker_binding_id": row["tracker_binding_id"],
        "allocation_id": row["allocation_id"],
        "kind": row["kind"],
        "status": row["status"],
        "data_boundary": row["data_boundary"],
        "priority": int(row["priority"]),
        "priority_score": (
            float(row["priority_score"])
            if row["priority_score"] is not None
            else None
        ),
        "workload_class": row["workload_class"],
        "resource_units": int(row["resource_units"]),
        "max_attempts": int(row["max_attempts"]),
        "attempt": int(row["attempt"]),
        "run_after": row["run_after"].isoformat(),
        "dedupe_key": row["dedupe_key"],
        "created": created,
    }


# --------------------------------------------------------------------------- #
# Job priority — ONE table, because scattered magic numbers ranked us backwards
# --------------------------------------------------------------------------- #
# `claim_job` orders by `priority ASC`, so a LOWER number runs SOONER.
#
# These were invented per call site — 40, 45, 46, 48, 80 across six services — with no shared
# table and no statement of what the ordering meant. The result was exactly backwards: a
# `code.task` sat at 46 and a `meeting.step` at 48, so batch build work outranked the meeting
# an operator was watching. It stayed invisible until something produced batch work in bulk;
# then eleven code tasks queued ahead of a PRD meeting and it never started.
#
# The ordering rule is WHO IS WAITING, not what kind of work it is:
#   * a person watching a screen right now      -> INTERACTIVE
#   * work a person asked for and walked away   -> REQUESTED
#   * work the system decided to do by itself   -> BACKGROUND
#
# A shepherd retry is BACKGROUND even though it is a code.task, and a meeting is INTERACTIVE
# even though it is cheap: provenance decides, not kind.
#: Above a person, and the only band that may be: work whose DELAY costs money or safety.
#: The janitor terminating a paid droplet is the case — every minute it waits is spend — and a
#: deployment the operator already approved. Nothing else belongs here; a bare literal below
#: INTERACTIVE is a bug, which is what test_no_service_invents_a_priority_above_interactive
#: exists to catch.
JOB_PRIORITY_URGENT = 10

JOB_PRIORITY_INTERACTIVE = 20
JOB_PRIORITY_REQUESTED = 46
JOB_PRIORITY_BACKGROUND = 90
#: Machine evaluation work — QA benchmarks, canaries, bake-offs. Nobody is ever waiting on it,
#: so it yields to everything, always. It ran ABOVE every other class until 2026-08-22.
JOB_PRIORITY_BULK = 95


class JobLeaseService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.comms = CommsService(settings)
        self.agent_runtime = AgentRuntimeService(settings)
        self.work_orchestration = WorkOrchestrationService(settings)

    def list_jobs(
        self,
        connection: Connection,
        project_id: str,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        task_id: Optional[str] = None,
        include_terminal: bool = True,
        sort: str = "status_priority",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200.")
        if offset < 0:
            raise ValueError("offset must be >= 0.")
        filters = ["j.project_id = :project_id"]
        params: dict[str, Any] = {
            "project_id": project_id,
            "limit": limit,
            "offset": offset,
        }
        if status:
            filters.append("j.status = :status")
            params["status"] = status
        if kind:
            filters.append("j.kind = :kind")
            params["kind"] = kind
        if task_id:
            filters.append("j.task_id = :task_id")
            params["task_id"] = task_id
        if not include_terminal:
            filters.append(
                "j.status NOT IN ('succeeded', 'cancelled', 'deadlettered')"
            )
        where_clause = " AND ".join(filters)
        order_key = (sort or "status_priority").strip().lower()
        if order_key == "updated":
            order_sql = "j.updated_at DESC NULLS LAST, j.created_at DESC"
        elif order_key == "created":
            order_sql = "j.created_at DESC"
        else:
            order_sql = """
                  CASE j.status
                    WHEN 'deadlettered' THEN 0
                    WHEN 'failed_final' THEN 1
                    WHEN 'failed_retryable' THEN 2
                    WHEN 'leased' THEN 3
                    WHEN 'running' THEN 4
                    WHEN 'queued' THEN 5
                    ELSE 6
                  END,
                  j.priority_score DESC NULLS LAST,
                  j.priority ASC,
                  j.run_after ASC,
                  j.created_at DESC
            """
        rows = connection.execute(
            text(
                f"""
                SELECT j.id, j.project_id, j.task_id, t.title AS task_title,
                       j.work_item_id, j.tracker_binding_id, j.allocation_id,
                       w.title AS work_title, b.external_key, b.external_url,
                       p.provider_key AS tracker_provider,
                       j.kind, j.status, j.priority, j.priority_score,
                       j.workload_class, j.resource_units,
                       j.attempt, j.max_attempts,
                       j.data_boundary, j.run_after, j.lease_owner,
                       j.lease_expires_at, j.heartbeat_at, j.last_error,
                       j.created_at, j.updated_at
                FROM jobs j
                LEFT JOIN tasks t ON t.id = j.task_id
                LEFT JOIN work_items w ON w.id = j.work_item_id
                LEFT JOIN work_tracker_bindings b ON b.id = j.tracker_binding_id
                LEFT JOIN work_tracker_providers p ON p.id = b.provider_id
                WHERE {where_clause}
                ORDER BY {order_sql}
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        return [self._serialize_job_row(row) for row in rows]

    def create_job(
        self,
        connection: Connection,
        project_id: str,
        kind: str,
        payload: Optional[dict[str, Any]] = None,
        task_id: Optional[str] = None,
        priority: int = 100,
        max_attempts: int = 3,
        run_after_seconds: int = 0,
        data_boundary: Optional[str] = None,
        work_item_id: Optional[str] = None,
        tracker_binding_id: Optional[str] = None,
        allocation_id: Optional[str] = None,
        priority_score: Optional[float] = None,
        workload_class: str = "general",
        resource_units: int = 1,
        dedupe_key: Optional[str] = None,
    ) -> dict[str, Any]:
        job = enqueue_job(
            connection,
            project_id,
            kind,
            payload,
            task_id=task_id,
            priority=priority,
            max_attempts=max_attempts,
            run_after_seconds=run_after_seconds,
            data_boundary=data_boundary,
            dedupe_key=dedupe_key,
            work_item_id=work_item_id,
            tracker_binding_id=tracker_binding_id,
            allocation_id=allocation_id,
            priority_score=priority_score,
            workload_class=workload_class,
            resource_units=resource_units,
        )
        # Preserve the public return shape used before enqueue_job was
        # extracted.  Dispatcher-only metadata belongs on the free function.
        # The skip path (stopped run) has these keys too; popping without a
        # default crashed the completing meeting.step after an operator cancel.
        job.pop("dedupe_key", None)
        job.pop("created", None)
        return job

    def cancel_queued_job(
        self, connection: Connection, job_id: str, *, reason: str, actor: str = "operator",
        force: bool = False, project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Cancel a job that has not started. Never touches one that is running.

        There was no way to un-queue work. When a bulk producer filled the queue, the only
        remedy was to wait it out — an autopilot sweep put eleven code tasks in front of an
        operator's meeting and there was no lever anywhere to take them back.

        Deliberately refuses anything leased or running: killing work mid-flight leaves a
        half-written branch and a lease nobody owns, which is a worse problem than the queue.

        `force=True` overrides that, and is for the case where a running job is the thing
        BLOCKING an operator — a wedged benchmark holding the only worker slot while a meeting
        waits. The half-written branch is real; it is chosen deliberately over an operator
        waiting indefinitely, and the caller says so in `reason`.
        """
        row = connection.execute(
            text("SELECT id, status, kind, payload, project_id FROM jobs WHERE id = :id"),
            {"id": job_id}).mappings().first()
        if not row:
            return {"ok": False, "error": "no such job"}
        if project_id and row["project_id"] != project_id:
            return {"ok": False, "error": "no such job in this project"}
        if row["status"] in ("succeeded", "cancelled", "deadlettered"):
            return {"ok": True, "job_id": job_id, "status": row["status"],
                    "note": "already terminal"}
        if row["status"] not in ("queued", "failed_retryable") and not force:
            return {"ok": False, "job_id": job_id, "status": row["status"],
                    "error": f"job is {row['status']}; only a job that has not started "
                             "can be cancelled (pass force=true to stop it anyway)"}
        connection.execute(
            text("UPDATE jobs SET status = 'cancelled', "
                 "last_error = CAST(:e AS JSONB), updated_at = :now WHERE id = :id "
                 + ("" if force else "AND status IN ('queued','failed_retryable')")),
            {"id": job_id, "now": now_utc(),
             "e": json.dumps({"message": "cancelled before start", "reason": reason,
                              "actor": actor})})
        # A CANCELLED JOB MUST CLOSE ITS RUN.
        #
        # Cancelling nine jobs left nineteen code_task_runs sitting at status='running',
        # phase='queued' — work that will never start, reported forever as in flight. The job
        # is the execution; the run is what the operator SEES, and closing one without the
        # other is how a console comes to show nineteen running tasks and no worker busy.
        run_id = None
        payload = row["payload"] if "payload" in row.keys() else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if isinstance(payload, dict):
            run_id = payload.get("run_id")
        if run_id:
            # Both run tables, not just code tasks. Cancelling a meeting.step job closed the
            # job and left meeting_runs at 'running', so the card read "PANEL SITTING" for a
            # panel that would never sit — indefinitely, with no terminal state anywhere in
            # the UI. Fixing this for code_task_runs and not for meeting_runs was the same
            # miss twice in one file.
            note = f"cancelled with its job: {reason}"
            connection.execute(
                text("UPDATE code_task_runs SET status = 'cancelled', "
                     "error = COALESCE(error, :e), updated_at = :now "
                     "WHERE id = :r AND status NOT IN "
                     "('completed','failed','cancelled')"),
                {"r": run_id, "now": now_utc(), "e": note})
            connection.execute(
                text("UPDATE meeting_runs SET status = 'cancelled', "
                     "error = COALESCE(error, :e), updated_at = :now "
                     "WHERE id = :r AND status NOT IN "
                     "('completed','failed','cancelled')"),
                {"r": run_id, "now": now_utc(), "e": note})
        return {"ok": True, "job_id": job_id, "kind": row["kind"], "status": "cancelled",
                "run_id": run_id}

    def claim_job(
        self,
        connection: Connection,
        worker_id: str,
        capabilities: list[str],
        lease_ttl_seconds: int,
    ) -> Optional[dict[str, Any]]:
        if not capabilities:
            return None
        # A DRAINING API LEASES NOTHING (stability spec §4a.5): it is about to stop, and work
        # leased now would be interrupted seconds later.
        from . import owned_process
        if owned_process.draining():
            return None

        lease_token = f"lease_{uuid4().hex}"
        lease_token_hash = self._hash_token(lease_token)
        current_time = now_utc()
        lease_expires_at = current_time + timedelta(seconds=lease_ttl_seconds)

        candidate_statement = text(
            """
            SELECT id, project_id, task_id, work_item_id, tracker_binding_id,
                   allocation_id, kind, payload, data_boundary, priority_score,
                   workload_class, resource_units, attempt, max_attempts, status,
                   last_error
            FROM jobs
            WHERE kind IN :capabilities
              AND run_after <= :now_utc
              AND (
                status IN ('queued', 'failed_retryable')
                -- reclaim leased OR running jobs whose lease expired: a worker that
                -- crashed mid-job leaves it 'running', and it must be recoverable.
                OR (status IN ('leased', 'running') AND lease_expires_at <= :now_utc)
              )
            ORDER BY
              CASE status
                WHEN 'leased' THEN 0
                WHEN 'running' THEN 0
                WHEN 'failed_retryable' THEN 1
                ELSE 2
              END,
              priority_score DESC NULLS LAST,
              priority ASC,
              created_at ASC
            LIMIT 10
            FOR UPDATE SKIP LOCKED
            """
        ).bindparams(bindparam("capabilities", expanding=True))
        candidates = connection.execute(
            candidate_statement,
            {
                "capabilities": capabilities,
                "now_utc": current_time,
            },
        ).mappings().all()
        if not candidates:
            return None

        # G3 — under budget pressure, re-rank claim candidates by value density.
        candidates = self._prefer_value_under_budget_pressure(
            connection, list(candidates)
        )

        admission_cap = None
        for candidate in candidates:
            payload = candidate["payload"] or {}
            # THE MACHINE IS ASKED BEFORE A HEAVY JOB STARTS (stability spec §4b.2). A queued
            # build costs nothing; a thrashing host slows every build and the control plane
            # with it. A reclaim (its previous holder is gone) is never held back.
            if candidate["status"] not in {"leased", "running"}:
                from . import admission
                if admission.profile_for_job(candidate["kind"]) is not None:
                    try:
                        if admission_cap is None:
                            admission_cap = admission.sample()
                        verdict = admission.admit_job(connection, candidate["kind"],
                                                      cap=admission_cap)
                    except Exception:  # noqa: BLE001 — an unreadable host never blocks work
                        verdict = None
                    if verdict is not None and not verdict.admitted:
                        self._defer_for_host_capacity(connection, candidate, verdict)
                        continue
            allocated, allocation_reason = (
                self.work_orchestration.reserve_job_slot(
                    connection,
                    project_id=candidate["project_id"],
                    job_id=candidate["id"],
                    job_kind=candidate["kind"],
                    work_item_id=candidate["work_item_id"],
                    tracker_binding_id=candidate["tracker_binding_id"],
                    allocation_id=candidate["allocation_id"],
                    resource_units=int(candidate["resource_units"] or 1),
                    already_held=candidate["status"] in {"leased", "running"},
                )
            )
            if not allocated:
                self._defer_for_resource_allocation(
                    connection,
                    candidate["project_id"],
                    candidate["id"],
                    candidate["work_item_id"],
                    allocation_reason,
                )
                continue
            budget_policy = self._budget_state_policy_for_job(
                connection,
                candidate["project_id"],
                candidate["task_id"],
                candidate["id"],
                candidate["kind"],
            )
            if budget_policy is not None:
                self.work_orchestration.release_job_slot(
                    connection,
                    project_id=candidate["project_id"],
                    job_id=candidate["id"],
                    allocation_id=candidate["allocation_id"],
                    resource_units=int(candidate["resource_units"] or 1),
                    reason="budget_state_deferred",
                )
                self._defer_for_budget_state(
                    connection,
                    candidate["project_id"],
                    candidate["task_id"],
                    candidate["id"],
                    candidate["kind"],
                    budget_policy,
                )
                continue
            if self._requires_reservation(candidate["kind"], payload):
                reservation_id = self._reservation_id_from_payload(payload)
                reservation = (
                    self.budget.assert_active_reservation(
                        connection, candidate["project_id"], reservation_id
                    )
                    if reservation_id
                    else None
                )
                if not reservation:
                    self.work_orchestration.release_job_slot(
                        connection,
                        project_id=candidate["project_id"],
                        job_id=candidate["id"],
                        allocation_id=candidate["allocation_id"],
                        resource_units=int(candidate["resource_units"] or 1),
                        reason="budget_reservation_missing",
                    )
                    self._cancel_budget_blocked_job(
                        connection,
                        candidate["project_id"],
                        candidate["id"],
                        candidate["task_id"],
                        candidate["kind"],
                        reservation_id,
                    )
                    continue

            row = connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'leased',
                        lease_owner = :worker_id,
                        lease_token_hash = :lease_token_hash,
                        lease_expires_at = :lease_expires_at,
                        heartbeat_at = :now_utc,
                        attempt = jobs.attempt + 1,
                        updated_at = :now_utc
                    WHERE id = :job_id
                    RETURNING
                        id,
                        project_id,
                        task_id,
                        work_item_id,
                        tracker_binding_id,
                        allocation_id,
                        kind,
                        payload,
                        data_boundary,
                        priority_score,
                        workload_class,
                        resource_units,
                        attempt,
                        max_attempts,
                        lease_expires_at
                    """
                ),
                {
                    "job_id": candidate["id"],
                    "worker_id": worker_id,
                    "lease_token_hash": lease_token_hash,
                    "lease_expires_at": lease_expires_at,
                    "now_utc": current_time,
                },
            ).mappings().one()

            event_type = (
                "job.lease_reclaimed"
                if candidate["status"] in {"leased", "running"}
                else "job.leased"
            )
            emit_event(
                connection,
                row["project_id"],
                event_type,
                worker_id,
                "job",
                row["id"],
                {"attempt": row["attempt"], "kind": row["kind"]},
            )
            if row["kind"] == "agent.studio.execute":
                connection.execute(
                    text(
                        "UPDATE agent_studio_runs SET status='leased', "
                        "updated_at=:updated_at WHERE job_id=:job_id"
                    ),
                    {
                        "job_id": row["id"],
                        "updated_at": current_time,
                    },
                )
            leased_payload = self.agent_runtime.attach_to_payload(
                connection,
                row["project_id"],
                job_kind=row["kind"],
                payload=row["payload"] or {},
            )
            return {
                "id": row["id"],
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "work_item_id": row["work_item_id"],
                "tracker_binding_id": row["tracker_binding_id"],
                "allocation_id": row["allocation_id"],
                "kind": row["kind"],
                "payload": leased_payload,
                "data_boundary": row["data_boundary"],
                "priority_score": (
                    float(row["priority_score"])
                    if row["priority_score"] is not None
                    else None
                ),
                "workload_class": row["workload_class"],
                "resource_units": int(row["resource_units"]),
                "attempt": row["attempt"],
                "max_attempts": row["max_attempts"],
                "lease_token": lease_token,
                "lease_expires_at": row["lease_expires_at"].isoformat(),
            }

        return None

    def lease_owned_job(
        self,
        connection: Connection,
        *,
        project_id: str,
        job_id: str,
        worker_id: str,
        lease_ttl_seconds: int,
    ) -> dict[str, Any]:
        """Lease one job this project already queued.

        claim_job takes the next job of a kind in any project. A typed decision
        must not spend under somebody else's lease. Token, TTL, heartbeat, and
        attempt match claim_job.
        """
        if lease_ttl_seconds < 5 or lease_ttl_seconds > 3600:
            raise ValueError("lease_ttl_seconds must be between 5 and 3600.")
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, work_item_id, tracker_binding_id,
                       allocation_id, kind, payload, data_boundary, workload_class,
                       resource_units, status
                FROM jobs
                WHERE id = :job_id AND project_id = :project_id
                FOR UPDATE
                """
            ),
            {"job_id": job_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise LeaseNotFoundError("Job not found.")
        if row["status"] not in {"queued", "failed_retryable"}:
            raise LeaseNotFoundError(f"Job is {row['status']}.")
        allocated, allocation_reason = self.work_orchestration.reserve_job_slot(
            connection,
            project_id=project_id,
            job_id=job_id,
            job_kind=row["kind"],
            work_item_id=row["work_item_id"],
            tracker_binding_id=row["tracker_binding_id"],
            allocation_id=row["allocation_id"],
            resource_units=int(row["resource_units"] or 1),
            already_held=False,
        )
        if not allocated:
            raise JobDeferredError(allocation_reason)
        policy = self._budget_state_policy_for_job(
            connection, project_id, row["task_id"], job_id, row["kind"],
        )
        if policy is not None:
            self.work_orchestration.release_job_slot(
                connection,
                project_id=project_id,
                job_id=job_id,
                allocation_id=row["allocation_id"],
                resource_units=int(row["resource_units"] or 1),
                reason="budget_state_deferred",
            )
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        last_error = CAST(:last_error AS JSONB),
                        updated_at = :updated_at
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "last_error": json.dumps({"reason": policy.get("reason")}),
                    "updated_at": now_utc(),
                },
            )
            raise JobDeferredError(str(policy.get("reason") or "budget_deferred"))

        current_time = now_utc()
        lease_token = f"lease_{uuid4().hex}"
        lease_expires_at = current_time + timedelta(seconds=lease_ttl_seconds)
        updated = connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'leased',
                    lease_owner = :worker_id,
                    lease_token_hash = :lease_token_hash,
                    lease_expires_at = :lease_expires_at,
                    heartbeat_at = :now_utc,
                    attempt = jobs.attempt + 1,
                    updated_at = :now_utc
                WHERE id = :job_id
                RETURNING id, kind, payload, data_boundary, workload_class,
                          allocation_id, attempt
                """
            ),
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "lease_token_hash": self._hash_token(lease_token),
                "lease_expires_at": lease_expires_at,
                "now_utc": current_time,
            },
        ).mappings().one()
        emit_event(
            connection,
            project_id,
            "job.leased",
            worker_id,
            "job",
            job_id,
            {"attempt": updated["attempt"], "kind": updated["kind"]},
        )
        payload = updated["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": updated["id"],
            "project_id": project_id,
            "kind": updated["kind"],
            "payload": payload,
            "data_boundary": updated["data_boundary"],
            "workload_class": updated["workload_class"],
            "allocation_id": updated["allocation_id"],
            "attempt": int(updated["attempt"]),
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at.isoformat(),
        }

    def heartbeat(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
        handing_back: bool = False,
    ) -> dict[str, Any]:
        current_time = now_utc()
        lease_expires_at = current_time + timedelta(seconds=lease_ttl_seconds)
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        if row["lease_expires_at"] <= current_time:
            raise LeaseNotFoundError("Lease has expired.")
        if handing_back:
            # A DEPLOY IS NOT THE JOB'S FAILURE. The stopping worker shortens its lease so the job
            # is reclaimed in seconds, and reclaiming it adds an attempt — so two deploys during
            # one long build used two of its three attempts, and a third would have deadlettered
            # work that never failed (boss-1, 2026-10-02). Give the attempt back now; the
            # reclaim's increment then leaves the count where the work actually is.
            connection.execute(
                text("UPDATE jobs SET attempt = GREATEST(attempt - 1, 0) WHERE id = :job_id"),
                {"job_id": job_id},
            )
            emit_event(connection, row["project_id"], "job.handed_back", worker_id, "job",
                       job_id, {"reason": "worker stopping", "attempt_not_counted": True})

        next_status = "running" if row["status"] == "leased" else row["status"]
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = :status,
                    heartbeat_at = :heartbeat_at,
                    lease_expires_at = :lease_expires_at,
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "status": next_status,
                "heartbeat_at": current_time,
                "lease_expires_at": lease_expires_at,
                "updated_at": current_time,
                "job_id": job_id,
            },
        )
        if row["kind"] == "agent.studio.execute":
            connection.execute(
                text(
                    "UPDATE agent_studio_runs SET status=:status, "
                    "updated_at=:updated_at WHERE job_id=:job_id"
                ),
                {
                    "status": next_status,
                    "updated_at": current_time,
                    "job_id": job_id,
                },
            )
        emit_event(
            connection,
            row["project_id"],
            "job.heartbeat",
            worker_id,
            "job",
            job_id,
            {"status": next_status},
        )
        return {
            "job_id": job_id,
            "status": next_status,
            "lease_expires_at": lease_expires_at.isoformat(),
        }

    def complete(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        project_id = row["project_id"]
        result = result or {}
        proof = self._validate_completion_proof(row, worker_id, result)
        if proof["disposition"] == "rejected":
            timestamp = now_utc()
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET last_error = CAST(:last_error AS JSONB),
                        heartbeat_at = :heartbeat_at,
                        updated_at = :updated_at
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "last_error": json.dumps(
                        {
                            "code": "completion_proof_rejected",
                            "reasons": proof["reasons"],
                        }
                    ),
                    "heartbeat_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            emit_event(
                connection,
                project_id,
                "job.completion_proof_rejected",
                worker_id,
                "job",
                job_id,
                {"reasons": proof["reasons"]},
            )
            return {
                "job_id": job_id,
                "status": "verification_pending",
                "proof_disposition": "rejected",
                "proof_rejection_reasons": proof["reasons"],
            }

        proof_artifact_id = None
        if proof["disposition"] == "accepted":
            proof_artifact_id = create_artifact(
                connection,
                self.settings,
                project_id,
                row.get("task_id"),
                "job_completion_proof",
                f"Completion proof for job {job_id}",
                {
                    "job_id": job_id,
                    "job_kind": row["kind"],
                    "worker_id": worker_id,
                    "contract": proof["contract"],
                    "proof": proof["proof"],
                    "disposition": "accepted",
                },
                created_by_job_id=job_id,
                data_boundary=row.get("data_boundary"),
            )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    lease_owner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    last_error = '{}'::jsonb,
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "updated_at": now_utc()},
        )
        self.work_orchestration.release_job_slot(
            connection,
            project_id=project_id,
            job_id=job_id,
            allocation_id=row.get("allocation_id"),
            resource_units=int(row.get("resource_units") or 1),
            reason="job_succeeded",
        )

        extras = self._finalize_success(
            connection,
            row,
            result,
        )
        extras["proof_disposition"] = proof["disposition"]
        if proof_artifact_id:
            extras["proof_artifact_id"] = proof_artifact_id
        emit_event(
            connection,
            project_id,
            "job.succeeded",
            worker_id,
            "job",
            job_id,
            {"kind": row["kind"], **extras},
        )
        return {"job_id": job_id, "status": "succeeded", **extras}

    def _validate_completion_proof(
        self,
        job_row: dict[str, Any],
        worker_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate an opt-in contract without granting an unchecked success state."""
        payload = job_row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        contract = payload.get("completion_contract")
        if not contract:
            return {"disposition": "not_required", "reasons": []}
        if not isinstance(contract, dict):
            return {
                "disposition": "rejected",
                "reasons": ["completion_contract must be an object."],
            }

        reasons: list[str] = []
        for field in contract.get("required_result_fields") or []:
            if field not in result or result[field] in (None, ""):
                reasons.append(f"Missing required result field: {field}.")
        proof = result.get("completion_proof")
        if not isinstance(proof, dict):
            reasons.append("completion_proof is required.")
            proof = {}
        evidence_refs = proof.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not all(
            isinstance(ref, str) and ref.strip() for ref in evidence_refs
        ):
            reasons.append("completion_proof.evidence_refs must be non-empty strings.")
        elif len(evidence_refs) < int(contract.get("min_evidence_refs") or 1):
            reasons.append("completion_proof does not include enough evidence references.")
        verifier_actor = proof.get("verifier_actor")
        if not isinstance(verifier_actor, str) or not verifier_actor.strip():
            reasons.append("completion_proof.verifier_actor is required.")
        elif contract.get("independent_verifier_required", True) and verifier_actor == worker_id:
            reasons.append(
                "completion_proof.verifier_actor must differ from the completing worker."
            )
        if proof.get("disposition") != "verified":
            reasons.append("completion_proof.disposition must be 'verified'.")
        if reasons:
            return {"disposition": "rejected", "reasons": reasons}
        return {
            "disposition": "accepted",
            "reasons": [],
            "contract": contract,
            "proof": proof,
        }

    def fail(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        error: dict[str, Any],
        retryable: bool,
        retry_delay_seconds: int,
    ) -> dict[str, Any]:
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        project_id = row["project_id"]
        current_time = now_utc()

        if retryable and int(row["attempt"]) < int(row["max_attempts"]):
            next_run_after = current_time + timedelta(seconds=retry_delay_seconds)
            connection.execute(
                text(
                    """
                    UPDATE jobs
                    SET status = 'failed_retryable',
                        lease_owner = NULL,
                        lease_token_hash = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        last_error = CAST(:last_error AS JSONB),
                        run_after = :run_after,
                        updated_at = :updated_at
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "last_error": json.dumps(error),
                    "run_after": next_run_after,
                    "updated_at": current_time,
                },
            )
            self.work_orchestration.release_job_slot(
                connection,
                project_id=project_id,
                job_id=job_id,
                allocation_id=row.get("allocation_id"),
                resource_units=int(row.get("resource_units") or 1),
                reason="job_retry_scheduled",
            )
            emit_event(
                connection,
                project_id,
                "job.failed_retryable",
                worker_id,
                "job",
                job_id,
                {
                    "attempt": row["attempt"],
                    "max_attempts": row["max_attempts"],
                    "run_after": next_run_after.isoformat(),
                },
            )
            self._mark_related_message_retrying(
                connection, row, retry_delay_seconds
            )
            self._mark_agent_system_failure(
                connection, row, error, terminal=False, actor=worker_id
            )
            return {
                "job_id": job_id,
                "status": "failed_retryable",
                "run_after": next_run_after.isoformat(),
                "deadletter_id": None,
            }

        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'failed_final',
                    lease_owner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "last_error": json.dumps(error),
                "updated_at": current_time,
            },
        )
        self.work_orchestration.release_job_slot(
            connection,
            project_id=project_id,
            job_id=job_id,
            allocation_id=row.get("allocation_id"),
            resource_units=int(row.get("resource_units") or 1),
            reason="job_failed_final",
        )
        emit_event(
            connection,
            project_id,
            "job.failed_final",
            worker_id,
            "job",
            job_id,
            {"attempt": row["attempt"], "max_attempts": row["max_attempts"]},
        )
        deadletter_id = make_id("dlq")
        connection.execute(
            text(
                """
                INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, payload)
                VALUES (
                    :id, :project_id, 'job', :source_id, :reason, CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "id": deadletter_id,
                "project_id": project_id,
                "source_id": job_id,
                "reason": error.get("message", "job failed"),
                "payload": json.dumps(error),
            },
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'deadlettered',
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "updated_at": current_time},
        )
        emit_event(
            connection,
            project_id,
            "job.deadlettered",
            worker_id,
            "job",
            job_id,
            {"deadletter_id": deadletter_id},
        )
        self._mark_related_message_deadlettered(connection, row, deadletter_id)
        self._mark_agent_system_failure(
            connection, row, error, terminal=True, actor=worker_id
        )
        self._mark_related_code_task_failed(connection, row, error, actor=worker_id)
        self._mark_related_discovery_failed(connection, row, error, actor=worker_id)
        if row.get("kind") == "deployment.stack_apply":
            # A deployment whose apply job died must not sit at `running` forever.
            from .stack_deploy import fail_for_deadlettered_job

            fail_for_deadlettered_job(connection, dict(row), error, self.settings)
        return {
            "job_id": job_id,
            "status": "deadlettered",
            "run_after": None,
            "deadletter_id": deadletter_id,
        }

    def _mark_related_discovery_failed(
        self,
        connection: Connection,
        row: dict[str, Any],
        error: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """A deadlettered discovery.run job fails its discovery, with the reason in words.

        When the API refused the worker (409) the job deadlettered and the discovery stayed
        `running` at phase "queued": the Discovery page showed nothing wrong and a workflow
        waiting on its research waited forever. Only while this job is still the run's job, so
        a newer job queued for the same run is not undone. `failed_by_job` lets a requeue of
        this deadletter resume the same run (discovery_service.execute_discovery_job).
        """
        if row.get("kind") != "discovery.run":
            return
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        run_id = str(payload.get("discovery_run_id") or "")
        if not run_id:
            return
        from .model_failures import explain

        said = explain(error.get("message", ""), source_id="anthropic_claude_code")
        changed = connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'failed', error = :error,
                    state = state || jsonb_build_object('failed_by_job', CAST(:job AS text),
                                                        'job_error', CAST(:raw AS text)),
                    updated_at = now()
                WHERE id = :run_id AND status = 'running' AND state->>'job_id' = :job
                """
            ),
            {"run_id": run_id, "job": row["id"], "raw": str(error.get("message", ""))[:1000],
             "error": f"Its worker job stopped. {said['headline']} {said['action']}"[:1000]},
        ).rowcount
        if changed:
            emit_event(connection, row["project_id"], "discovery.failed", actor,
                       "discovery_run", run_id,
                       {"reason": "job_deadlettered", "job_id": row["id"], "kind": said["kind"]})

    def _mark_related_code_task_failed(
        self,
        connection: Connection,
        row: dict[str, Any],
        error: dict[str, Any],
        *,
        actor: str,
    ) -> None:
        """Keep the code-task run truthful when its driving job deadletters.

        Without this, a `code.task` job that dies terminally (e.g. the step
        endpoint 500s on a config error) leaves the run stuck at
        status='running' phase='queued' forever — invisible failure in the
        console. Mirror the terminal job state onto the run."""
        if row.get("kind") != "code.task":
            return
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        run_id = payload.get("run_id")
        if not run_id:
            return
        message = str(error.get("message") or "job deadlettered")[:500]
        updated = connection.execute(
            text(
                """
                UPDATE code_task_runs
                SET status = 'failed',
                    error = :error,
                    updated_at = :updated_at
                WHERE id = :run_id
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """
            ),
            {"run_id": run_id, "error": message, "updated_at": now_utc()},
        )
        if updated.rowcount:
            emit_event(
                connection,
                row["project_id"],
                "code_task.failed",
                actor,
                "code_task_run",
                run_id,
                {"reason": "job_deadlettered", "job_id": row.get("id"), "error": message},
            )

    def _load_leased_job_for_update(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
    ):
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, work_item_id, tracker_binding_id,
                       allocation_id, kind, payload, status, attempt, max_attempts,
                       data_boundary, workload_class, resource_units, lease_owner,
                       lease_token_hash, lease_expires_at
                FROM jobs
                WHERE id = :job_id
                FOR UPDATE
                """
            ),
            {"job_id": job_id},
        ).mappings().first()
        if not row:
            raise LeaseNotFoundError("Job not found.")
        if row["lease_owner"] != worker_id:
            raise LeaseNotFoundError("Job lease is owned by another worker.")
        if row["lease_token_hash"] != self._hash_token(lease_token):
            raise LeaseNotFoundError("Lease token is invalid.")
        if row["status"] not in ("leased", "running"):
            raise LeaseNotFoundError("Job is not actively leased.")
        if row["lease_expires_at"] <= now_utc():
            raise LeaseNotFoundError("Lease has expired.")
        return row

    def require_active_lease(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        """Validate a worker lease before a side-effecting job endpoint runs."""
        return dict(
            self._load_leased_job_for_update(
                connection, worker_id, job_id, lease_token
            )
        )

    def defer_leased(
        self,
        connection: Connection,
        worker_id: str,
        job_id: str,
        lease_token: str,
        *,
        delay_seconds: int,
        reason: str,
        message: str,
    ) -> dict[str, Any]:
        """Hand a leased job back to the queue WITHOUT spending an attempt.

        For a job that is not failing but waiting on something it does not own — a target
        another deployment is still writing. Retrying it as a failure would deadletter a
        perfectly good job after a few backoffs, long before the thing it waits on finishes.
        The attempt the lease took is given back, so waiting is free however long it lasts.
        """
        row = self._load_leased_job_for_update(connection, worker_id, job_id, lease_token)
        run_after = now_utc() + timedelta(seconds=max(1, int(delay_seconds)))
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'queued',
                    attempt = GREATEST(jobs.attempt - 1, 0),
                    lease_owner = NULL,
                    lease_token_hash = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    run_after = :run_after,
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = now()
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "run_after": run_after,
                "last_error": json.dumps({"code": reason, "message": message}),
            },
        )
        self.work_orchestration.release_job_slot(
            connection,
            project_id=row["project_id"],
            job_id=job_id,
            allocation_id=row.get("allocation_id"),
            resource_units=int(row.get("resource_units") or 1),
            reason=reason,
        )
        emit_event(
            connection,
            row["project_id"],
            "job.deferred",
            worker_id,
            "job",
            job_id,
            {"reason": reason, "message": message, "run_after": run_after.isoformat()},
        )
        return {"job_id": job_id, "status": "queued", "run_after": run_after.isoformat()}

    def _finalize_success(
        self,
        connection: Connection,
        job_row,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if job_row["kind"] in {"openai.plan", "agent.resume"}:
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
            }

        if job_row["kind"] == "work_tracker.issue.sync":
            binding = result.get("binding") or {}
            self._record_delivery_presence(
                connection,
                job_row,
                result,
                provider="plane",
                event_kind="issue.status",
                status="synced",
                summary=(
                    f"Work item synced to "
                    f"{binding.get('external_key') or 'Plane'}."
                ),
                external_url=binding.get("external_url"),
            )
            return {
                "receipt_id": result.get("receipt_id"),
                "binding_id": binding.get("id"),
                "external_issue_id": binding.get("external_issue_id"),
                "external_key": binding.get("external_key"),
                "external_url": binding.get("external_url"),
            }

        if job_row["kind"] in {"software_factory.propose_pr", "github.pull_request"}:
            self._record_delivery_presence(
                connection,
                job_row,
                result,
                provider="github",
                event_kind="pull_request.status",
                status="open" if result.get("pull_request_url") else "completed",
                summary=(
                    f"Pull request opened: {result.get('pull_request_url')}"
                    if result.get("pull_request_url")
                    else "GitHub change proposal completed without a pull request URL."
                ),
                external_url=result.get("pull_request_url"),
            )
            return {
                "artifact_id": result.get("artifact_id"),
                "diff_artifact_id": result.get("diff_artifact_id"),
                "test_report_artifact_id": result.get("test_report_artifact_id"),
                "security_review_artifact_id": result.get(
                    "security_review_artifact_id"
                ),
                "release_go_no_go_artifact_id": result.get(
                    "release_go_no_go_artifact_id"
                ),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "validation_status": result.get("validation_status"),
                "validation_summary": result.get("validation_summary"),
                "validation_sources": result.get("validation_sources") or [],
                "local_validation_receipt_id": result.get(
                    "local_validation_receipt_id"
                ),
            }

        if job_row["kind"] == "docs.update":
            return {
                "receipt_id": result.get("receipt_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "feature_id": result.get("feature_id"),
                "doc_path": result.get("doc_path"),
            }

        if job_row["kind"] == "github.pull_request.merge":
            self._record_delivery_presence(
                connection,
                job_row,
                result,
                provider="github",
                event_kind="pull_request.status",
                status="merged",
                summary=(
                    f"Pull request merged at {result.get('merged_sha') or 'unknown SHA'}."
                ),
                external_url=result.get("pull_request_url"),
            )
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "deployment_decision_id": result.get("deployment_decision_id"),
                "pull_request_url": result.get("pull_request_url"),
                "branch_name": result.get("branch_name"),
                "merged_sha": result.get("merged_sha"),
            }

        if job_row["kind"] == "github.deployment.create":
            self._record_delivery_presence(
                connection,
                job_row,
                result,
                provider="github",
                event_kind="deployment.status",
                status=str(result.get("deployment_status") or "created"),
                summary=(
                    f"Deployment {result.get('deployment_status') or 'created'} "
                    f"for {result.get('environment') or 'target environment'}."
                ),
                external_url=result.get("deployment_url"),
            )
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "environment": result.get("environment"),
                "deployment_id": result.get("deployment_id"),
                "deployment_status": result.get("deployment_status"),
                "deployment_url": result.get("deployment_url"),
                "merged_sha": result.get("merged_sha"),
                "rollout_id": result.get("rollout_id"),
                "rollout_artifact_id": result.get("rollout_artifact_id"),
                "rollout_status": result.get("rollout_status"),
                "rollout_stage": result.get("rollout_stage"),
            }

        if job_row["kind"] == "comms.outbox.deliver":
            payload = job_row["payload"] or {}
            channel = str(payload.get("channel") or "")
            if channel.startswith("mattermost:"):
                self._record_delivery_presence(
                    connection,
                    job_row,
                    result,
                    provider="mattermost",
                    event_kind="message.status",
                    status=str(result.get("status") or "delivered"),
                    summary=(
                        f"Mattermost delivery {result.get('status') or 'completed'} "
                        f"to {channel.removeprefix('mattermost:')}."
                    ),
                    external_url=None,
                )
            return {
                "receipt_id": result.get("receipt_id"),
            }

        if job_row["kind"] == "a2a.dispatch":
            return {
                "receipt_id": result.get("receipt_id"),
                "message_id": result.get("id") or result.get("message_id"),
                "redis_stream_id": result.get("redis_stream_id"),
            }

        if job_row["kind"] == "infra.droplet.create":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "resource_id": result.get("resource_id"),
                "provider_ref": result.get("provider_ref"),
                "provider_resource_id": result.get("provider_resource_id"),
                "resource_name": result.get("resource_name"),
                "expires_at": result.get("expires_at"),
            }

        if job_row["kind"] == "infra.droplet.delete":
            return {
                "artifact_id": result.get("artifact_id"),
                "decision_id": result.get("decision_id"),
                "receipt_id": result.get("receipt_id"),
                "resource_id": result.get("resource_id"),
                "provider_ref": result.get("provider_ref"),
            }

        if job_row["kind"] == "agent.orchestrator.next_move":
            # Control-plane already created a receipt + orchestration events.
            return {
                "receipt_id": result.get("receipt_id"),
                "plan_id": result.get("plan_id"),
                "actions": result.get("actions") or [],
                "ready_task_ids": result.get("ready_task_ids") or [],
                "blocked_task_ids": result.get("blocked_task_ids") or [],
            }

        if str(job_row["kind"]).startswith("agent."):
            payload = job_row["payload"] or {}
            agent_module = result.get("agent_module") or payload.get("agent_module") or {}
            role_key = (
                ((agent_module or {}).get("role_key"))
                or str(job_row["kind"]).split(".")[1]
            )
            artifact_type = "agent_brief"
            if job_row["kind"] == "agent.studio.execute":
                runtime_contract = (
                    ((payload.get("agent_runtime") or {}).get("compiled_prompt") or {})
                    .get("runtime_contract")
                )
                declared_type = (
                    runtime_contract.get("artifact_type")
                    if isinstance(runtime_contract, dict)
                    else None
                )
                if (
                    isinstance(declared_type, str)
                    and declared_type
                    and declared_type.replace("_", "").isalnum()
                    and declared_type[0].isalpha()
                    and len(declared_type) <= 64
                ):
                    artifact_type = declared_type
            artifact_id = create_artifact(
                connection,
                self.settings,
                job_row["project_id"],
                job_row["task_id"],
                artifact_type,
                f"{role_key} {artifact_type.replace('_', ' ')}",
                {
                    "job_kind": job_row["kind"],
                    "output_contract": (
                        ((payload.get("agent_runtime") or {}).get("compiled_prompt") or {})
                        .get("runtime_contract")
                    ),
                    "summary": result.get("summary"),
                    "findings": result.get("findings") or [],
                    "recommendations": result.get("recommendations") or [],
                    "required_decisions": result.get("required_decisions") or [],
                    "evidence_refs": result.get("evidence_refs") or [],
                    "strategy_id": result.get("strategy_id"),
                    "grounding_status": result.get("grounding_status"),
                    "work_product": result.get("work_product") or {},
                    "agent_module": agent_module,
                    "agent_runtime": result.get("agent_runtime")
                    or payload.get("agent_runtime")
                    or {},
                    "prompt_excerpt": result.get("prompt_excerpt"),
                },
                created_by_job_id=job_row["id"],
            )
            emit_event(
                connection,
                job_row["project_id"],
                "agent.brief_created",
                "worker",
                "artifact",
                artifact_id,
                {
                    "job_id": job_row["id"],
                    "job_kind": job_row["kind"],
                    "role_key": role_key,
                },
            )
            if job_row["kind"] == "agent.studio.execute":
                from .agent_studio import AgentStudioService

                AgentStudioService(self.settings).record_job_success(
                    connection,
                    project_id=job_row["project_id"],
                    job_id=job_row["id"],
                    result=result,
                    artifact_id=artifact_id,
                    actor="worker",
                )
            return {"artifact_id": artifact_id}

        if job_row["kind"] == "enterprise.marketplace.validate":
            payload = job_row["payload"] or {}
            module_id = result.get("module_id") or payload.get("module_id")
            artifact_id = create_artifact(
                connection,
                self.settings,
                job_row["project_id"],
                job_row["task_id"],
                "entitlement_gate_report",
                f"Marketplace entitlement validation for {module_id or 'module'}",
                {
                    "job_kind": job_row["kind"],
                    "summary": result.get("summary"),
                    "status": result.get("status"),
                    "module_id": module_id,
                    "required_entitlement": result.get("required_entitlement"),
                    "entitlement_status": result.get("entitlement_status"),
                    "findings": result.get("findings") or [],
                    "recommendations": result.get("recommendations") or [],
                    "required_decisions": result.get("required_decisions") or [],
                    "evidence_refs": result.get("evidence_refs") or [],
                    "work_product": result.get("work_product") or {},
                    "agent_module": result.get("agent_module")
                    or payload.get("agent_module")
                    or {},
                    "agent_runtime": result.get("agent_runtime")
                    or payload.get("agent_runtime")
                    or {},
                },
                created_by_job_id=job_row["id"],
            )
            emit_event(
                connection,
                job_row["project_id"],
                "enterprise.marketplace_validated",
                "worker",
                "artifact",
                artifact_id,
                {
                    "job_id": job_row["id"],
                    "module_id": module_id,
                    "status": result.get("status"),
                },
            )
            return {"artifact_id": artifact_id}

        if job_row["kind"] not in {"plan.command", "plan.generate"}:
            return {}

        payload = job_row["payload"] or {}
        task_title = self._task_title(connection, job_row["task_id"])
        artifact_id = create_artifact(
            connection,
            self.settings,
            job_row["project_id"],
            job_row["task_id"],
            "plan_draft",
            f"Plan draft for {task_title or payload.get('command', 'command')}",
            {
                "command": payload.get("command"),
                "constraints": payload.get("constraints", {}),
                "summary": result.get("summary"),
                "objectives": result.get("objectives", []),
                "steps": result.get("steps", []),
                "risks": result.get("risks", []),
                "done_when": result.get("done_when", []),
                "agent_module": result.get("agent_module") or payload.get("agent_module") or {},
            },
            created_by_job_id=job_row["id"],
        )
        decision_id = create_blocking_decision(
            connection,
            job_row["project_id"],
            job_row["task_id"],
            f"Review plan draft for {task_title or 'queued task'}",
            {
                "action": "review_plan_draft",
                "artifact_id": artifact_id,
            },
            {},
            "plan_ready_for_review",
        )
        emit_event(
            connection,
            job_row["project_id"],
            "plan.created",
            "worker",
            "artifact",
            artifact_id,
            {"job_id": job_row["id"], "decision_id": decision_id},
        )
        return {"artifact_id": artifact_id, "decision_id": decision_id}

    def _mark_agent_system_failure(
        self,
        connection: Connection,
        job_row,
        error: dict[str, Any],
        *,
        terminal: bool,
        actor: str,
    ) -> None:
        if job_row["kind"] == "agent.studio.execute":
            from .agent_studio import AgentStudioService

            AgentStudioService(self.settings).record_job_failure(
                connection,
                project_id=job_row["project_id"],
                job_id=job_row["id"],
                error=error,
                terminal=terminal,
                actor=actor,
            )
        elif job_row["kind"] == "a2a.dispatch":
            from .a2a import A2AService

            A2AService(self.settings).mark_dispatch_failure(
                connection,
                project_id=job_row["project_id"],
                job_id=job_row["id"],
                error=error,
                terminal=terminal,
            )

    def _record_delivery_presence(
        self,
        connection: Connection,
        job_row,
        result: dict[str, Any],
        *,
        provider: str,
        event_kind: str,
        status: str,
        summary: str,
        external_url: Optional[str],
    ) -> None:
        reported_receipt_id = result.get("receipt_id")
        receipt_id = None
        if reported_receipt_id:
            receipt_id = connection.execute(
                text(
                    """
                    SELECT id
                    FROM receipts
                    WHERE id = :receipt_id
                      AND project_id = :project_id
                    """
                ),
                {
                    "receipt_id": reported_receipt_id,
                    "project_id": job_row["project_id"],
                },
            ).scalar_one_or_none()
        self.work_orchestration.record_delivery_event(
            connection,
            project_id=job_row["project_id"],
            provider=provider,
            event_kind=event_kind,
            subject_type=(
                "work_item" if job_row.get("work_item_id") else "job"
            ),
            subject_id=job_row.get("work_item_id") or job_row["id"],
            status=status,
            summary=summary,
            dedupe_key=f"presence:{job_row['id']}:{event_kind}",
            external_url=external_url,
            receipt_id=receipt_id,
            metadata={
                "job_id": job_row["id"],
                "task_id": job_row.get("task_id"),
                "tracker_binding_id": job_row.get("tracker_binding_id"),
                "allocation_id": job_row.get("allocation_id"),
                "reported_receipt_id": reported_receipt_id,
                "receipt_reference_status": (
                    "resolved"
                    if receipt_id
                    else "not_reported"
                    if not reported_receipt_id
                    else "unresolved"
                ),
            },
        )

    def _task_title(self, connection: Connection, task_id: Optional[str]) -> Optional[str]:
        if not task_id:
            return None
        return connection.execute(
            text("SELECT title FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).scalar_one_or_none()

    def _serialize_job_row(self, row) -> dict[str, Any]:
        last_error = row["last_error"] or {}
        if not isinstance(last_error, dict):
            last_error = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "task_title": row.get("task_title"),
            "work_item_id": row.get("work_item_id"),
            "work_title": row.get("work_title"),
            "tracker_binding_id": row.get("tracker_binding_id"),
            "tracker_provider": row.get("tracker_provider"),
            "external_key": row.get("external_key"),
            "external_url": row.get("external_url"),
            "allocation_id": row.get("allocation_id"),
            "kind": row["kind"],
            "status": row["status"],
            "data_boundary": row.get("data_boundary") or "internal",
            "priority": row["priority"],
            "priority_score": (
                float(row["priority_score"])
                if row.get("priority_score") is not None
                else None
            ),
            "workload_class": row.get("workload_class") or "general",
            "resource_units": int(row.get("resource_units") or 1),
            "max_attempts": row["max_attempts"],
            "attempt": row["attempt"],
            "run_after": row["run_after"].isoformat(),
            "lease_owner": row.get("lease_owner"),
            "lease_expires_at": (
                row["lease_expires_at"].isoformat()
                if row.get("lease_expires_at")
                else None
            ),
            "heartbeat_at": (
                row["heartbeat_at"].isoformat() if row.get("heartbeat_at") else None
            ),
            "last_error": last_error,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }

    def _defer_for_resource_allocation(
        self,
        connection: Connection,
        project_id: str,
        job_id: str,
        work_item_id: Optional[str],
        reason: str,
    ) -> None:
        run_after = now_utc() + timedelta(seconds=30)
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status='queued', run_after=:run_after,
                    last_error=CAST(:last_error AS JSONB), updated_at=now()
                WHERE id=:job_id
                """
            ),
            {
                "job_id": job_id,
                "run_after": run_after,
                "last_error": json.dumps(
                    {
                        "code": reason,
                        "message": "Job is waiting for tracked quota allocation.",
                    }
                ),
            },
        )
        emit_event(
            connection,
            project_id,
            "job.allocation_deferred",
            "job_allocator",
            "job",
            job_id,
            {
                "work_item_id": work_item_id,
                "reason": reason,
                "run_after": run_after.isoformat(),
            },
        )

    def _defer_for_host_capacity(self, connection: Connection, candidate, verdict) -> None:
        """Leave the job queued with WHY in the operator's words; the event once per wait,
        not once per poll (a poll every few seconds would otherwise flood the log)."""
        run_after = now_utc() + timedelta(seconds=20)
        previous = candidate.get("last_error") or {}
        if isinstance(previous, str):
            try:
                previous = json.loads(previous)
            except ValueError:
                previous = {}
        message = f"Queued: waiting for room on this machine ({verdict.shortfall})."
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status='queued', run_after=:run_after,
                    last_error=CAST(:last_error AS JSONB), updated_at=now()
                WHERE id=:job_id
                """
            ),
            {
                "job_id": candidate["id"],
                "run_after": run_after,
                "last_error": json.dumps({
                    "code": "host_capacity", "deferred_reason": "host_capacity",
                    "message": message, "shortfall": verdict.shortfall,
                    "slots": verdict.slots, "in_flight": verdict.in_flight,
                }),
            },
        )
        if (previous or {}).get("code") != "host_capacity":
            emit_event(connection, candidate["project_id"], "job.capacity_deferred",
                       "admission", "job", candidate["id"],
                       {"kind": candidate["kind"], "shortfall": verdict.shortfall,
                        "slots": verdict.slots, "in_flight": verdict.in_flight,
                        "level": verdict.level})

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prefer_value_under_budget_pressure(
        self,
        connection: Connection,
        candidates: list[Any],
    ) -> list[Any]:
        """G3 — when any candidate project is under yellow+ pressure, rank by value/$.

        Stable within a claim batch: jobs from green projects keep relative order;
        under pressure, value density wins. Lease still fails closed on budget
        freezes via ``_budget_state_policy_for_job``.
        """
        if len(candidates) <= 1:
            return candidates

        # Budget state is project-scoped; compute once per distinct project.
        state_by_project: dict[str, str] = {}
        for row in candidates:
            project_id = str(row["project_id"])
            if project_id in state_by_project:
                continue
            try:
                snapshot = self.budget.status_for_project(connection, project_id)
                state_by_project[project_id] = str(
                    snapshot.get("budget_state") or "green"
                ).lower()
            except Exception:  # noqa: BLE001 — never block claim on status read
                state_by_project[project_id] = "green"

        pressure_present = any(
            state in {"yellow", "orange", "red"}
            for state in state_by_project.values()
        )
        if not pressure_present:
            return candidates

        rank_input: list[dict[str, Any]] = []
        for index, row in enumerate(candidates):
            payload = row["payload"] or {}
            if not isinstance(payload, dict):
                payload = {}
            score = row.get("priority_score")
            if score is None:
                score = 0
            cost = payload.get("estimated_cost_usd_micros")
            if cost is None:
                # resource_units as a crude cost proxy (1 unit ≈ $0.01).
                cost = max(1, int(row.get("resource_units") or 1)) * 10_000
            rank_input.append(
                {
                    "id": row["id"],
                    "_index": index,
                    "priority_score": float(score),
                    "estimated_cost_usd_micros": int(cost),
                    "project_id": str(row["project_id"]),
                    "budget_state": state_by_project.get(
                        str(row["project_id"]), "green"
                    ),
                }
            )

        # Prefer value density for pressure projects; green projects trail them
        # only when the batch mixes states (rare). Within mode, pure rank applies.
        pressure_rows = [
            item
            for item in rank_input
            if item["budget_state"] in {"yellow", "orange", "red"}
        ]
        green_rows = [
            item for item in rank_input if item["budget_state"] not in {
                "yellow", "orange", "red"
            }
        ]
        ordered_ids: list[str] = []
        if pressure_rows:
            ranked = self.work_orchestration.schedule_preference(
                pressure_rows,
                budget_state="orange",
            )
            ordered_ids.extend(item["id"] for item in ranked["items"])
        if green_rows:
            ranked_green = self.work_orchestration.schedule_preference(
                green_rows,
                budget_state="green",
            )
            ordered_ids.extend(item["id"] for item in ranked_green["items"])

        by_id = {row["id"]: row for row in candidates}
        return [by_id[job_id] for job_id in ordered_ids if job_id in by_id]

    def _requires_reservation(self, kind: str, payload: dict[str, Any]) -> bool:
        spendy_kinds = {
            "openai.plan",
            "github.pull_request",
            "software_factory.propose_pr",
            "comms.outbox.deliver",
            "infra.droplet.create",
        }
        return bool(payload.get("requires_reservation")) or kind in spendy_kinds

    def _reservation_id_from_payload(self, payload: dict[str, Any]) -> Optional[str]:
        reservation_id = payload.get("reservation_id") or payload.get(
            "budget_reservation_id"
        )
        return str(reservation_id) if reservation_id else None

    def _budget_state_policy_for_job(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
    ) -> Optional[dict[str, Any]]:
        snapshot = self.budget.status_for_project(connection, project_id)
        budget_state = snapshot["budget_state"]
        if budget_state not in {"orange", "red"}:
            return None

        task_priority = "p2"
        risk_tier = 0
        if task_id:
            task_row = connection.execute(
                text(
                    """
                    SELECT priority, risk_tier
                    FROM tasks
                    WHERE id = :task_id
                    """
                ),
                {"task_id": task_id},
            ).mappings().first()
            if task_row:
                task_priority = str(task_row["priority"] or "p2").lower()
                risk_tier = int(task_row["risk_tier"] or 0)

        # What holds it, in the budget's own period: the hold sentence names the period and
        # when it resets, and the job is not deferred past that reset.
        held_by = {
            "budget_state": budget_state,
            "utilization_ratio": snapshot.get("utilization_ratio"),
            "cap_usd_micros": snapshot.get("cap_usd_micros"),
            "committed_usd_micros": snapshot.get("committed_usd_micros"),
            "period": snapshot.get("period"),
            "whose": snapshot.get("whose"),
            "cap_name": snapshot.get("cap_name"),
            "resets_at": snapshot.get("resets_at"),
            "resets_at_label": snapshot.get("resets_at_label"),
        }
        if budget_state == "orange" and task_priority not in {"p0", "p1"}:
            return {
                **held_by,
                "task_priority": task_priority,
                "risk_tier": risk_tier,
                "defer_seconds": 300,
                "reason": "orange_defers_non_critical_work",
            }

        high_impact_effects = {
            "github.pull_request.merge",
            "github.deployment.create",
            "comms.outbox.deliver",
        }
        if budget_state == "red":
            if kind == "infra.droplet.create":
                return {
                    **held_by,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_freezes_infra_provisioning",
                }
            if task_priority != "p0":
                return {
                    **held_by,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_halts_non_p0_work",
                }
            if kind in high_impact_effects and risk_tier >= 2:
                return {
                    **held_by,
                    "task_priority": task_priority,
                    "risk_tier": risk_tier,
                    "defer_seconds": 900,
                    "reason": "red_freezes_tier2_effects",
                }
        return None

    def _defer_for_budget_state(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
        policy: dict[str, Any],
    ) -> None:
        deferred_until = now_utc() + timedelta(seconds=int(policy["defer_seconds"]))
        # Not past the moment the budget's period resets: the cap is measured per period, so
        # a job held at 23:55 UTC is runnable at 00:00, not at 00:10.
        if policy.get("resets_at"):
            resets_at = datetime.fromisoformat(str(policy["resets_at"]))
            deferred_until = max(min(deferred_until, resets_at), now_utc())
        # SAID AS WHAT IT IS, WHEREVER IT SHOWS (model_failures.budget_hold_sentence). The job
        # stays queued with a later run_after and no lease, which every surface used to read as
        # "no worker has picked it up" — on boss-1 three research runs said that while the
        # budget held them.
        from .model_failures import budget_hold_sentence

        held = budget_hold_sentence(policy["budget_state"], policy.get("utilization_ratio"),
                                    policy.get("cap_usd_micros"), policy)
        message = (
            f"{held} "
            f"(job {kind}, priority {policy['task_priority']}, risk {policy['risk_tier']}; "
            f"{policy['reason']})"
        )
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'queued',
                    run_after = :run_after,
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "run_after": deferred_until,
                "last_error": json.dumps(
                    {
                        "message": message,
                        "kind": "budget_hold",
                        "budget_state": policy["budget_state"],
                        "reason": policy["reason"],
                    }
                ),
                "updated_at": now_utc(),
            },
        )
        decision_id = self._create_or_reuse_budget_policy_decision(
            connection,
            project_id,
            task_id,
            job_id,
            kind,
            policy,
        )
        emit_event(
            connection,
            project_id,
            "job.deferred_budget_policy",
            "budget_governor",
            "job",
            job_id,
            {
                "decision_id": decision_id,
                "budget_state": policy["budget_state"],
                "deferred_until": deferred_until.isoformat(),
                "reason": policy["reason"],
            },
        )

    def _create_or_reuse_budget_policy_decision(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        job_id: str,
        kind: str,
        policy: dict[str, Any],
    ) -> str:
        existing_id = connection.execute(
            text(
                """
                SELECT id
                FROM decisions
                WHERE project_id = :project_id
                  AND task_id IS NOT DISTINCT FROM :task_id
                  AND status IN ('pending', 'delegated', 'approved')
                  AND recommendation->>'action' = 'review_budget_policy'
                  AND cost_summary->>'job_id' = :job_id
                  AND cost_summary->>'budget_state' = :budget_state
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "project_id": project_id,
                "task_id": task_id,
                "job_id": job_id,
                "budget_state": policy["budget_state"],
            },
        ).scalar_one_or_none()
        if existing_id:
            return str(existing_id)

        return create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Budget state {policy['budget_state']} deferred job: {kind}",
            {
                "action": "review_budget_policy",
                "job_id": job_id,
                "job_kind": kind,
                "budget_state": policy["budget_state"],
                "reason": policy["reason"],
            },
            {
                "job_id": job_id,
                "job_kind": kind,
                "budget_state": policy["budget_state"],
                "task_priority": policy["task_priority"],
                "risk_tier": policy["risk_tier"],
            },
            "budget_state_deferred_job",
        )

    def _cancel_budget_blocked_job(
        self,
        connection: Connection,
        project_id: str,
        job_id: str,
        task_id: Optional[str],
        kind: str,
        reservation_id: Optional[str],
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE jobs
                SET status = 'cancelled',
                    last_error = CAST(:last_error AS JSONB),
                    updated_at = :updated_at
                WHERE id = :job_id
                """
            ),
            {
                "job_id": job_id,
                "last_error": json.dumps(
                    {
                        "message": "Missing active reservation for spend-gated job.",
                        "reservation_id": reservation_id,
                    }
                ),
                "updated_at": now_utc(),
            },
        )
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Budget blocked spend-gated job: {kind}",
            {"action": "create_budget_reservation"},
            {"reservation_id": reservation_id},
            "budget_missing_for_job",
        )
        emit_event(
            connection,
            project_id,
            "job.cancelled_budget_blocked",
            "budget_governor",
            "job",
            job_id,
            {"decision_id": decision_id, "reservation_id": reservation_id},
        )
        if kind == "comms.outbox.deliver":
            payload = connection.execute(
                text("SELECT payload FROM jobs WHERE id = :job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none() or {}
            outbox_message_id = payload.get("outbox_message_id")
            if outbox_message_id:
                self.comms.mark_outbox_deadlettered(
                    connection,
                    project_id,
                    str(outbox_message_id),
                    decision_id,
                )

    def _mark_related_message_retrying(
        self, connection: Connection, row, retry_delay_seconds: int
    ) -> None:
        if row["kind"] == "webhook.outbound.deliver":
            delivery_id = (row["payload"] or {}).get("delivery_id")
            if delivery_id:
                connection.execute(
                    text(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'retrying',
                            next_attempt_at = :next_attempt_at,
                            updated_at = :updated_at
                        WHERE id = :delivery_id AND project_id = :project_id
                        """
                    ),
                    {
                        "delivery_id": str(delivery_id),
                        "project_id": row["project_id"],
                        "next_attempt_at": now_utc()
                        + timedelta(seconds=retry_delay_seconds),
                        "updated_at": now_utc(),
                    },
                )
            return
        if row["kind"] != "comms.outbox.deliver":
            return
        payload = row["payload"] or {}
        outbox_message_id = payload.get("outbox_message_id")
        if not outbox_message_id:
            return
        self.comms.mark_outbox_retrying(
            connection,
            row["project_id"],
            str(outbox_message_id),
            retry_delay_seconds,
        )

    def _mark_related_message_deadlettered(
        self, connection: Connection, row, deadletter_id: str
    ) -> None:
        if row["kind"] == "webhook.outbound.deliver":
            delivery_id = (row["payload"] or {}).get("delivery_id")
            if delivery_id:
                connection.execute(
                    text(
                        """
                        UPDATE webhook_deliveries
                        SET status = 'deadlettered', next_attempt_at = NULL,
                            last_error = COALESCE(last_error, '{}'::jsonb)
                                || CAST(:deadletter AS JSONB),
                            updated_at = :updated_at
                        WHERE id = :delivery_id AND project_id = :project_id
                        """
                    ),
                    {
                        "delivery_id": str(delivery_id),
                        "project_id": row["project_id"],
                        "deadletter": json.dumps({"deadletter_id": deadletter_id}),
                        "updated_at": now_utc(),
                    },
                )
            return
        if row["kind"] != "comms.outbox.deliver":
            return
        payload = row["payload"] or {}
        outbox_message_id = payload.get("outbox_message_id")
        if not outbox_message_id:
            return
        self.comms.mark_outbox_deadlettered(
            connection,
            row["project_id"],
            str(outbox_message_id),
            deadletter_id,
        )
