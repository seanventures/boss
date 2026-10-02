from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from . import decision_actions
from .budget import BudgetGovernor
from .health import HealthSnapshotService


class MetatronScopeNotFoundError(LookupError):
    pass


class MetatronService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.health = HealthSnapshotService(settings)

    def answer_query(
        self,
        connection: Connection,
        project_id: str,
        question: str,
        *,
        task_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        project = self._project_scope(connection, project_id)
        freshness_at = self._freshness_at(connection, project_id)
        current_health = self.health.current_state(connection, project_id)
        top_risks = current_health["top_risks"]

        if task_id:
            answer, references = self._answer_task_scope(
                connection, project_id, task_id, limit
            )
            category = "task"
        elif decision_id:
            answer, references = self._answer_decision_scope(
                connection, project_id, decision_id, limit
            )
            category = "decision"
        else:
            category = self._categorize(question)
            if category == "budget":
                answer, references = self._answer_budget(
                    connection, project_id, project["project_name"], limit
                )
            elif category == "decisions":
                answer, references = self._answer_decisions(
                    connection, project_id, limit
                )
            elif category == "risks":
                answer, references = self._answer_risks(
                    connection, project_id, current_health, limit
                )
            elif category == "activity":
                answer, references = self._answer_activity(
                    connection, project_id, limit
                )
            else:
                answer, references = self._answer_status(
                    connection,
                    project_id,
                    project["project_name"],
                    current_health,
                    limit,
                )

        return {
            "project_id": project_id,
            "question": question,
            "category": category,
            "answer": answer,
            "freshness_at": freshness_at,
            "references": references
            or [
                self._reference(
                    "project",
                    project_id,
                    str(project["project_name"]),
                    status="active",
                    detail="Metatron answered from the current project scope.",
                )
            ],
            "suggested_actions": self._suggested_actions(current_health),
            "top_risks": top_risks,
        }

    def _project_scope(self, connection: Connection, project_id: str) -> dict[str, Any]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.name AS project_name
                FROM projects p
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise MetatronScopeNotFoundError("Unknown project_id")
        return dict(row)

    def _freshness_at(self, connection: Connection, project_id: str) -> str:
        latest = connection.execute(
            text("SELECT MAX(created_at) FROM events WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).scalar_one_or_none()
        if latest:
            return latest.isoformat()
        return datetime.now(timezone.utc).isoformat()

    def _categorize(self, question: str) -> str:
        lowered = question.lower()
        if any(
            token in lowered
            for token in ("budget", "spend", "cost", "reservation", "cap", "burn")
        ):
            return "budget"
        if any(
            token in lowered
            for token in (
                "decision",
                "approve",
                "approval",
                "delegate",
                "revise",
                "inbox",
            )
        ):
            return "decisions"
        if any(
            token in lowered
            for token in ("risk", "blocker", "deadletter", "incident", "stuck")
        ):
            return "risks"
        if any(
            token in lowered
            for token in (
                "what changed",
                "activity",
                "event",
                "receipt",
                "artifact",
                "recent",
            )
        ):
            return "activity"
        return "status"

    def _answer_status(
        self,
        connection: Connection,
        project_id: str,
        project_name: str,
        current_health: dict[str, Any],
        limit: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        summary = current_health["summary"]
        answer = (
            f"{project_name} is currently in budget state "
            f"{current_health['budget_state']}. There are "
            f"{summary['open_tasks']} open task(s), "
            f"{summary['pending_decisions']} pending decision(s), "
            f"{summary['active_jobs']} active job(s), and "
            f"{summary['deadletters']} deadletter(s)."
        )
        if current_health["top_risks"]:
            answer += f" Top risk: {current_health['top_risks'][0]['title']}."
        references = self._recent_task_refs(connection, project_id, limit)
        references.extend(
            self._pending_decision_refs(connection, project_id, max(2, limit // 2))
        )
        return answer, references[:limit]

    def _answer_budget(
        self, connection: Connection, project_id: str, project_name: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        snapshot = self.budget.status_for_project(connection, project_id)
        top_driver = (
            snapshot["top_cost_drivers"][0] if snapshot["top_cost_drivers"] else None
        )
        answer = (
            f"{project_name} budget is {snapshot['budget_state']}. "
            f"Available: {self._format_usd(snapshot['available_usd_micros'])}, "
            f"reserved: {self._format_usd(snapshot['reserved_usd_micros'])}, "
            f"actual: {self._format_usd(snapshot['actual_usd_micros'])}, "
            f"committed: {self._format_usd(snapshot['committed_usd_micros'])}."
        )
        if top_driver:
            answer += (
                f" Top cost driver: {top_driver['skill_or_model_id']} "
                f"at {self._format_usd(int(top_driver['amount_usd_micros']))}."
            )
        references = self._budget_refs(connection, project_id, limit)
        return answer, references

    def _answer_decisions(
        self, connection: Connection, project_id: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        # WAITING ON YOU is the Approvals inbox's rule (decision_actions.attention). This
        # answer counted pending and delegated itself, so it told the operator fewer decisions
        # were waiting than the inbox listed: a pull request stuck in a dead-end
        # revision_requested was left out (review round 3). Recent answered ones follow as
        # references, as before.
        pending = decision_actions.waiting_on_operator(connection, project_id)
        waiting_ids = {row["id"] for row in pending}
        recent = connection.execute(
            text(
                """
                SELECT id, question, status, default_action, risk_tier, deadline_at, created_at
                FROM decisions
                WHERE project_id = :project_id
                ORDER BY risk_tier DESC, created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        rows = (pending + [dict(r) for r in recent if r["id"] not in waiting_ids])[:limit]
        if pending:
            first = pending[0]
            answer = (
                f"There are {len(pending)} decision(s) waiting on you. "
                f"The most urgent is \"{first['question']}\" with status {first['status']} "
                f"and default action {first['default_action']}."
            )
        else:
            answer = "There are no decisions waiting on you."
        references = [
            self._reference(
                "decision",
                str(row["id"]),
                str(row["question"]),
                status=str(row["status"]),
                detail=f"risk tier {row['risk_tier']}",
                created_at=row["created_at"],
            )
            for row in rows
        ]
        return answer, references

    def _answer_risks(
        self,
        connection: Connection,
        project_id: str,
        current_health: dict[str, Any],
        limit: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        top_risks = current_health["top_risks"]
        if top_risks:
            lead = top_risks[0]
            answer = (
                f"Top risk is {lead['title']} ({lead['severity']}). "
                f"{lead['detail']}"
            )
        else:
            answer = "No active top-risk signals are currently derived for this project."
        references = self._deadletter_refs(connection, project_id, max(2, limit // 2))
        references.extend(
            self._pending_decision_refs(connection, project_id, max(2, limit // 2))
        )
        return answer, references[:limit]

    def _answer_activity(
        self, connection: Connection, project_id: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        rows = connection.execute(
            text(
                """
                SELECT id, event_type, entity_type, entity_id, data, created_at
                FROM events
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        if rows:
            answer = (
                f"The latest recorded activity is {rows[0]['event_type']}. "
                f"{len(rows)} recent event(s) are included in the reference set."
            )
        else:
            answer = "No recent events are recorded for this project yet."
        references = [
            self._reference(
                str(row["entity_type"] or "event"),
                str(row["entity_id"] or row["id"]),
                str(row["event_type"]),
                detail=str((row["data"] or {}).get("message") or ""),
                created_at=row["created_at"],
            )
            for row in rows
        ]
        return answer, references

    def _answer_task_scope(
        self, connection: Connection, project_id: str, task_id: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        task = connection.execute(
            text(
                """
                SELECT id, title, status, description, created_at
                FROM tasks
                WHERE id = :task_id AND project_id = :project_id
                """
            ),
            {"task_id": task_id, "project_id": project_id},
        ).mappings().first()
        if not task:
            raise MetatronScopeNotFoundError("Unknown task_id")

        job_count = int(
            connection.execute(
                text("SELECT COUNT(*) FROM jobs WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
        )
        decision_count = int(
            connection.execute(
                text("SELECT COUNT(*) FROM decisions WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
        )
        artifact_count = int(
            connection.execute(
                text("SELECT COUNT(*) FROM artifacts WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).scalar_one()
        )
        answer = (
            f"Task \"{task['title']}\" is {task['status']}. "
            f"It currently has {job_count} job(s), {decision_count} decision(s), "
            f"and {artifact_count} artifact(s) linked to it."
        )
        references = [
            self._reference(
                "task",
                task_id,
                str(task["title"]),
                status=str(task["status"]),
                detail=task["description"],
                created_at=task["created_at"],
            )
        ]
        references.extend(self._task_job_refs(connection, task_id, max(2, limit // 2)))
        references.extend(
            self._task_artifact_refs(connection, task_id, max(2, limit // 2))
        )
        return answer, references[:limit]

    def _answer_decision_scope(
        self, connection: Connection, project_id: str, decision_id: str, limit: int
    ) -> tuple[str, list[dict[str, Any]]]:
        row = connection.execute(
            text(
                """
                SELECT id, task_id, question, status, default_action, resolution_note,
                       approved_by, delegated_to, executed_by, created_at
                FROM decisions
                WHERE id = :decision_id AND project_id = :project_id
                """
            ),
            {"decision_id": decision_id, "project_id": project_id},
        ).mappings().first()
        if not row:
            raise MetatronScopeNotFoundError("Unknown decision_id")

        answer = (
            f"Decision \"{row['question']}\" is {row['status']} with default action "
            f"{row['default_action']}."
        )
        if row["resolution_note"]:
            answer += f" Latest note: {row['resolution_note']}."
        elif row["delegated_to"]:
            answer += f" It is delegated to {row['delegated_to']}."
        elif row["approved_by"]:
            answer += f" It was approved by {row['approved_by']}."
        elif row["executed_by"]:
            answer += f" It was executed by {row['executed_by']}."

        references = [
            self._reference(
                "decision",
                decision_id,
                str(row["question"]),
                status=str(row["status"]),
                detail=row["resolution_note"],
                created_at=row["created_at"],
            )
        ]
        task_id = row.get("task_id")
        if task_id:
            references.extend(
                self._task_artifact_refs(connection, str(task_id), max(1, limit - 1))
            )
        return answer, references[:limit]

    def _recent_task_refs(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, title, status, created_at
                FROM tasks
                WHERE project_id = :project_id
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [
            self._reference(
                "task",
                str(row["id"]),
                str(row["title"]),
                status=str(row["status"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _pending_decision_refs(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        # The decisions waiting on the operator, by the Approvals inbox's rule (review round 3).
        rows = decision_actions.waiting_on_operator(connection, project_id)[:limit]
        return [
            self._reference(
                "decision",
                str(row["id"]),
                str(row["question"]),
                status=str(row["status"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _budget_refs(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        reservations = connection.execute(
            text(
                """
                SELECT id, category, status, amount_usd_micros, created_at
                FROM budget_reservations
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max(1, limit // 2)},
        ).mappings().all()
        cost_receipts = connection.execute(
            text(
                """
                SELECT id, category, skill_or_model_id, status, actual_usd_micros, estimated_usd_micros, created_at
                FROM cost_receipts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max(1, limit // 2)},
        ).mappings().all()
        refs = [
            self._reference(
                "budget_reservation",
                str(row["id"]),
                f"{row['category']} reservation",
                status=str(row["status"]),
                detail=self._format_usd(int(row["amount_usd_micros"])),
                created_at=row["created_at"],
            )
            for row in reservations
        ]
        refs.extend(
            [
                self._reference(
                    "cost_receipt",
                    str(row["id"]),
                    str(row["skill_or_model_id"]),
                    status=str(row["status"]),
                    detail=self._format_usd(
                        int(row["actual_usd_micros"] or row["estimated_usd_micros"])
                    ),
                    created_at=row["created_at"],
                )
                for row in cost_receipts
            ]
        )
        return refs[:limit]

    def _deadletter_refs(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, source_kind, source_id, reason, created_at
                FROM deadletters
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [
            self._reference(
                "deadletter",
                str(row["id"]),
                f"{row['source_kind']}:{row['source_id']}",
                detail=str(row["reason"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _task_job_refs(
        self, connection: Connection, task_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, kind, status, created_at
                FROM jobs
                WHERE task_id = :task_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"task_id": task_id, "limit": limit},
        ).mappings().all()
        return [
            self._reference(
                "job",
                str(row["id"]),
                str(row["kind"]),
                status=str(row["status"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _task_artifact_refs(
        self, connection: Connection, task_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, artifact_type, title, created_at
                FROM artifacts
                WHERE task_id = :task_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"task_id": task_id, "limit": limit},
        ).mappings().all()
        return [
            self._reference(
                "artifact",
                str(row["id"]),
                str(row["title"]),
                status=str(row["artifact_type"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _reference(
        self,
        object_type: str,
        object_id: str,
        title: str,
        *,
        status: Optional[str] = None,
        detail: Optional[str] = None,
        created_at: Optional[Any] = None,
    ) -> dict[str, Any]:
        return {
            "object_type": object_type,
            "object_id": object_id,
            "title": title,
            "status": status,
            "detail": detail,
            "created_at": created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else created_at,
        }

    def _suggested_actions(self, current_health: dict[str, Any]) -> list[str]:
        suggestions: list[str] = []
        incident_state = current_health["incident_state"]
        workload_state = current_health["workload_state"]
        if incident_state["deadletters"] > 0:
            suggestions.append("Review deadletters and retry or close the failed work.")
        if workload_state["high_risk_pending_decisions"] > 0:
            suggestions.append("Resolve the highest-risk pending decisions in the inbox.")
        if current_health["budget_state"] in {"orange", "red"}:
            suggestions.append("Reduce execution intensity or increase the approved budget.")
        if not (self.settings.github_token and self.settings.openai_api_key):
            suggestions.append("Configure GitHub and OpenAI credentials so blocked work can proceed.")
        if not suggestions:
            suggestions.append(
                "No urgent operator action is currently implied by the control plane."
            )
        return suggestions

    def _format_usd(self, micros: int) -> str:
        return f"${micros / 1_000_000:,.2f}"
