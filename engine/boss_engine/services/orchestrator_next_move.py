"""Advance multi-phase plans after dispatch (BOSS-345).

Default event subscription:
  plan.dispatched → job kind agent.orchestrator.next_move

The worker must advertise that capability and call the control-plane execute
boundary. This service is fail-closed and always emits a visible event (or
returns a structured failure the worker turns into retry/deadletter).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from ..config import Settings
from .code_task_service import start_code_task
from .core import create_blocking_decision, emit_event
from .jobs import JobLeaseService, enqueue_job
from .receipts import ReceiptService


TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "done", "cancelled", "failed", "closed"}
)
ACTIVE_JOB_STATUSES = frozenset(
    {"queued", "failed_retryable", "leased", "running"}
)

_SCOPE_RE = re.compile(
    r"\b(design|scope|product|prd|spec|requirements?|brief)\b", re.I
)
_ARCH_RE = re.compile(r"\b(architect|architecture|invariant)\b", re.I)
_QA_RE = re.compile(r"\b(test|qa|validat|verify|regress)\b", re.I)
_RELEASE_RE = re.compile(r"\b(release|ship|deploy|rollout|merge)\b", re.I)
_IMPLEMENT_RE = re.compile(
    r"\b(implement|build|code|fix|develop|write|scaffold|app)\b", re.I
)


def classify_phase(title: str, description: str = "") -> str:
    blob = f"{title}\n{description}"
    if _QA_RE.search(blob):
        return "qa"
    if _RELEASE_RE.search(blob):
        return "release"
    if _ARCH_RE.search(blob):
        return "architecture"
    if _SCOPE_RE.search(blob):
        return "scope"
    if _IMPLEMENT_RE.search(blob):
        return "implement"
    return "implement"


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


class OrchestratorNextMoveService:
    """Drive ready phase tasks for a dispatched plan."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs = JobLeaseService(settings)
        self.receipts = ReceiptService()

    def execute(
        self,
        connection: Connection,
        *,
        project_id: str,
        event_id: Optional[str] = None,
        event_type: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        event_data: Optional[dict[str, Any]] = None,
        created_by_job_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        data_boundary: str = "internal",
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        del lease_token  # fencing is enforced by the job lease complete path
        event_data = event_data if isinstance(event_data, dict) else {}
        plan = self._resolve_plan(
            connection,
            project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_data=event_data,
            task_id=task_id,
        )
        if plan is None:
            return {
                "status": "failed",
                "retryable": False,
                "message": (
                    "orchestrator.next_move could not resolve a plan from the "
                    "triggering event payload."
                ),
            }
        if plan["status"] not in {"dispatched", "in_progress"}:
            return {
                "status": "failed",
                "retryable": False,
                "message": (
                    f"plan {plan['id']} is not dispatchable for next_move "
                    f"(status={plan['status']})."
                ),
            }

        phase_task_ids = self._phase_task_ids(plan, event_data)
        tasks = self._load_tasks(
            connection,
            project_id,
            phase_task_ids,
            parent_task_id=plan.get("task_id"),
        )
        if not tasks:
            emit_event(
                connection,
                project_id,
                "orchestrator.next_move.noop",
                worker_id or "orchestrator",
                "plan",
                plan["id"],
                {
                    "reason": "no_phase_tasks",
                    "event_id": event_id,
                    "event_type": event_type,
                    "job_id": created_by_job_id,
                },
            )
            return {
                "status": "succeeded",
                "summary": f"Plan {plan['id']} has no phase tasks to advance.",
                "plan_id": plan["id"],
                "actions": [],
                "ready_task_ids": [],
                "blocked_task_ids": [],
                "noop": True,
            }

        status_by_id = {row["id"]: row["status"] for row in tasks}
        ready: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for row in tasks:
            if row["status"] in TERMINAL_TASK_STATUSES:
                continue
            deps = [str(item) for item in _json_list(row["dependency_task_ids"])]
            unmet = [
                dep
                for dep in deps
                if status_by_id.get(dep, "open") not in TERMINAL_TASK_STATUSES
            ]
            if unmet:
                blocked.append({**row, "unmet_dependencies": unmet})
            elif row["status"] in {"open", "ready", "in_progress"}:
                ready.append(row)

        actions: list[dict[str, Any]] = []
        for row in ready:
            action = self._advance_task(
                connection,
                project_id=project_id,
                plan=plan,
                task=row,
                data_boundary=data_boundary,
                created_by_job_id=created_by_job_id,
            )
            actions.append(action)

        if plan["status"] == "dispatched" and actions:
            connection.execute(
                text(
                    """
                    UPDATE plans
                    SET status = 'in_progress', updated_at = now()
                    WHERE id = :id AND project_id = :project_id
                      AND status = 'dispatched'
                    """
                ),
                {"id": plan["id"], "project_id": project_id},
            )

        scope = self._scope(connection, project_id)
        dedupe_key = (
            f"orchestrator.next_move:{plan['id']}:{event_id or 'manual'}"
        )
        receipt, _created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "orchestrator.next_move",
            {"plan_id": plan["id"], "event_type": event_type},
            {
                "plan_id": plan["id"],
                "actions": actions,
                "ready_task_ids": [row["id"] for row in ready],
                "blocked_task_ids": [row["id"] for row in blocked],
                "event_id": event_id,
            },
            created_by_job_id=created_by_job_id,
            data_boundary=data_boundary,
        )

        emit_event(
            connection,
            project_id,
            "orchestrator.next_move.applied",
            worker_id or "orchestrator",
            "plan",
            plan["id"],
            {
                "event_id": event_id,
                "event_type": event_type,
                "job_id": created_by_job_id,
                "receipt_id": receipt["id"],
                "action_count": len(actions),
                "ready_task_ids": [row["id"] for row in ready],
                "blocked_task_ids": [row["id"] for row in blocked],
                "actions": actions,
            },
        )

        started = [
            item
            for item in actions
            if item.get("status") in {"enqueued", "started", "reused"}
        ]
        escalated = [
            item for item in actions if item.get("status") == "needs_decision"
        ]
        summary = (
            f"Orchestrator advanced plan {plan['id']}: "
            f"{len(started)} phase action(s), "
            f"{len(escalated)} decision(s), "
            f"{len(blocked)} still blocked on dependencies."
        )
        return {
            "status": "succeeded",
            "summary": summary,
            "plan_id": plan["id"],
            "parent_task_id": plan.get("task_id"),
            "receipt_id": receipt["id"],
            "actions": actions,
            "ready_task_ids": [row["id"] for row in ready],
            "blocked_task_ids": [row["id"] for row in blocked],
            "findings": [
                summary,
                f"Ready phases: {len(ready)}.",
                f"Blocked phases: {len(blocked)}.",
            ],
            "recommendations": [
                "Review escalated decisions before production-impacting work.",
                "Completed phase tasks unlock downstream dependency-sequenced phases.",
            ],
            "required_decisions": [
                {
                    "reason": item.get("reason") or "phase_requires_approval",
                    "task_id": item.get("task_id"),
                    "decision_id": item.get("decision_id"),
                    "default_action": "hold",
                }
                for item in escalated
                if item.get("decision_id")
            ],
            "noop": not actions and not blocked,
        }

    def _advance_task(
        self,
        connection: Connection,
        *,
        project_id: str,
        plan: dict[str, Any],
        task: dict[str, Any],
        data_boundary: str,
        created_by_job_id: Optional[str],
    ) -> dict[str, Any]:
        task_id = task["id"]
        title = str(task.get("title") or "phase")
        description = str(task.get("description") or "")
        classification = classify_phase(title, description)
        dedupe_key = f"orchestrator:phase:{plan['id']}:{task_id}"

        existing = connection.execute(
            text(
                """
                SELECT id, kind, status
                FROM jobs
                WHERE project_id = :project_id
                  AND task_id = :task_id
                  AND status IN ('queued', 'failed_retryable', 'leased', 'running')
                ORDER BY created_at ASC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "task_id": task_id},
        ).mappings().first()
        if existing:
            self._mark_task_in_progress(connection, project_id, task_id)
            return {
                "task_id": task_id,
                "status": "reused",
                "classification": classification,
                "job_id": existing["id"],
                "job_kind": existing["kind"],
                "reason": "active_job_already_present",
            }

        if classification == "implement":
            owner = (getattr(self.settings, "target_github_owner", None) or "").strip()
            repo = (getattr(self.settings, "target_github_repo", None) or "").strip()
            if not owner or not repo:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    task_id,
                    f"Configure target repository to start phase: {title}",
                    {
                        "action": "configure_target_github_and_retry_phase",
                        "plan_id": plan["id"],
                        "task_id": task_id,
                        "classification": classification,
                    },
                    {},
                    "orchestrator_missing_target_repo",
                )
                emit_event(
                    connection,
                    project_id,
                    "orchestrator.phase_blocked",
                    "orchestrator",
                    "task",
                    task_id,
                    {
                        "plan_id": plan["id"],
                        "reason": "missing_target_github",
                        "decision_id": decision_id,
                    },
                )
                return {
                    "task_id": task_id,
                    "status": "needs_decision",
                    "classification": classification,
                    "decision_id": decision_id,
                    "reason": "missing_target_github",
                }
            try:
                run_id = start_code_task(
                    connection,
                    project_id=project_id,
                    task_id=task_id,
                    title=title[:200],
                    instructions=description or title,
                    acceptance_criteria=[],
                    test_commands=[],
                    owner=owner,
                    repo=repo,
                    base_branch="main",
                    finalize_ctx={
                        "source": "orchestrator.next_move",
                        "plan_id": plan["id"],
                        "created_by_job_id": created_by_job_id,
                    },
                    jobs=self.jobs,
                )
            except ValueError as error:
                decision_id = create_blocking_decision(
                    connection,
                    project_id,
                    task_id,
                    f"Cannot start code work for phase: {title}",
                    {
                        "action": "fix_phase_spec_and_retry",
                        "plan_id": plan["id"],
                        "task_id": task_id,
                        "error": str(error),
                    },
                    {},
                    "orchestrator_code_task_refused",
                )
                return {
                    "task_id": task_id,
                    "status": "needs_decision",
                    "classification": classification,
                    "decision_id": decision_id,
                    "reason": str(error),
                }
            self._mark_task_in_progress(connection, project_id, task_id)
            emit_event(
                connection,
                project_id,
                "orchestrator.phase_started",
                "orchestrator",
                "task",
                task_id,
                {
                    "plan_id": plan["id"],
                    "classification": classification,
                    "run_id": run_id,
                    "job_kind": "code.task",
                },
            )
            return {
                "task_id": task_id,
                "status": "started",
                "classification": classification,
                "run_id": run_id,
                "job_kind": "code.task",
            }

        job_kind = {
            "scope": "agent.pm.scope_brief",
            "architecture": "agent.cao.architecture_review",
            "qa": "agent.qa.validation_report",
            "release": "agent.release_manager.release_packet",
        }.get(classification, "agent.cto.implementation_plan")

        job = enqueue_job(
            connection,
            project_id,
            job_kind,
            {
                "summary": title,
                "command": description or title,
                "plan_id": plan["id"],
                "phase_task_id": task_id,
                "classification": classification,
                "context": {
                    "plan_id": plan["id"],
                    "task_id": task_id,
                    "title": title,
                    "description": description,
                },
                "source": "orchestrator.next_move",
                "data_boundary": data_boundary,
            },
            task_id=task_id,
            priority=55,
            max_attempts=3,
            data_boundary=data_boundary,
            dedupe_key=dedupe_key,
        )
        self._mark_task_in_progress(connection, project_id, task_id)
        emit_event(
            connection,
            project_id,
            "orchestrator.phase_started",
            "orchestrator",
            "task",
            task_id,
            {
                "plan_id": plan["id"],
                "classification": classification,
                "job_id": job["id"],
                "job_kind": job_kind,
                "created": job.get("created", True),
            },
        )
        return {
            "task_id": task_id,
            "status": "enqueued" if job.get("created", True) else "reused",
            "classification": classification,
            "job_id": job["id"],
            "job_kind": job_kind,
        }

    def _resolve_plan(
        self,
        connection: Connection,
        project_id: str,
        *,
        entity_type: Optional[str],
        entity_id: Optional[str],
        event_data: dict[str, Any],
        task_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        plan_id = None
        if entity_type == "plan" and entity_id:
            plan_id = entity_id
        elif isinstance(event_data.get("plan_id"), str):
            plan_id = event_data["plan_id"]
        if plan_id:
            row = connection.execute(
                text(
                    """
                    SELECT id, project_id, task_id, title, status, phases
                    FROM plans
                    WHERE id = :id AND project_id = :project_id
                    """
                ),
                {"id": plan_id, "project_id": project_id},
            ).mappings().first()
            return dict(row) if row else None

        candidate_task = task_id or event_data.get("task_id")
        if not isinstance(candidate_task, str) or not candidate_task:
            return None
        row = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, status, phases
                FROM plans
                WHERE project_id = :project_id
                  AND (
                    task_id = :task_id
                    OR task_id = (
                        SELECT parent_task_id FROM tasks
                        WHERE id = :task_id AND project_id = :project_id
                    )
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id, "task_id": candidate_task},
        ).mappings().first()
        return dict(row) if row else None

    def _phase_task_ids(
        self, plan: dict[str, Any], event_data: dict[str, Any]
    ) -> list[str]:
        from_event = event_data.get("phase_task_ids")
        if isinstance(from_event, list) and from_event:
            return [str(item) for item in from_event if str(item).strip()]
        # Fall back to parented children of the plan's root task.
        return []

    def _load_tasks(
        self,
        connection: Connection,
        project_id: str,
        phase_task_ids: list[str],
        *,
        parent_task_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if phase_task_ids:
            stmt = text(
                """
                SELECT id, title, description, status, dependency_task_ids,
                       parent_task_id, inputs
                FROM tasks
                WHERE project_id = :project_id
                  AND id IN :ids
                ORDER BY created_at ASC
                """
            ).bindparams(bindparam("ids", expanding=True))
            rows = connection.execute(
                stmt,
                {"project_id": project_id, "ids": phase_task_ids},
            ).mappings().all()
            by_id = {row["id"]: dict(row) for row in rows}
            # Preserve plan phase order when possible.
            return [by_id[tid] for tid in phase_task_ids if tid in by_id]

        if parent_task_id:
            rows = connection.execute(
                text(
                    """
                    SELECT id, title, description, status, dependency_task_ids,
                           parent_task_id, inputs
                    FROM tasks
                    WHERE project_id = :project_id
                      AND parent_task_id = :parent_task_id
                    ORDER BY created_at ASC
                    """
                ),
                {"project_id": project_id, "parent_task_id": parent_task_id},
            ).mappings().all()
            return [dict(row) for row in rows]
        return []

    def _mark_task_in_progress(
        self, connection: Connection, project_id: str, task_id: str
    ) -> None:
        updated = connection.execute(
            text(
                """
                UPDATE tasks
                SET status = 'in_progress', updated_at = now()
                WHERE id = :task_id
                  AND project_id = :project_id
                  AND status IN ('open', 'ready')
                """
            ),
            {"task_id": task_id, "project_id": project_id},
        )
        if updated.rowcount == 1:
            emit_event(
                connection,
                project_id,
                "task.status_changed",
                "orchestrator",
                "task",
                task_id,
                {"status": "in_progress", "source": "orchestrator.next_move"},
            )

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            # Fail closed with synthetic ids only for receipt uniqueness when
            # bootstrap fixtures omit full org hierarchy (should not happen live).
            return {
                "project_id": project_id,
                "workspace_id": "workspace_unknown",
                "org_id": "org_unknown",
            }
        return dict(row)
