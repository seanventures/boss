from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_artifact, emit_event, now_utc
from .jobs import JobLeaseService


class DecisionNotFoundError(Exception):
    pass


class InvalidDecisionTransitionError(Exception):
    pass


class DecisionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.jobs = JobLeaseService(settings)

    def expire_overdue(
        self, connection: Connection, project_id: Optional[str] = None
    ) -> int:
        params: dict[str, object] = {"now": now_utc()}
        project_filter = ""
        if project_id:
            params["project_id"] = project_id
            project_filter = "AND project_id = :project_id"

        rows = connection.execute(
            text(
                f"""
                SELECT id, project_id, default_action, deadline_at
                FROM decisions
                WHERE status = 'pending'
                  AND deadline_at IS NOT NULL
                  AND deadline_at <= :now
                  {project_filter}
                """
            ),
            params,
        ).mappings().all()

        expired = 0
        for row in rows:
            updated = connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'expired', updated_at = :updated_at
                    WHERE id = :decision_id AND status = 'pending'
                    """
                ),
                {
                    "decision_id": row["id"],
                    "updated_at": now_utc(),
                },
            )
            if updated.rowcount != 1:
                continue

            expired += 1
            emit_event(
                connection,
                row["project_id"],
                "decision.expired",
                "system",
                "decision",
                row["id"],
                {
                    "deadline_at": row["deadline_at"].isoformat(),
                    "default_action": row["default_action"] or "hold",
                    "safe_default_applied": row["default_action"] or "hold",
                },
            )
        return expired

    def get_row(
        self, connection: Connection, decision_id: str, *, for_update: bool = False
    ) -> dict:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, task_id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, deadline_at,
                       approved_by, approved_at, created_at, updated_at
                FROM decisions
                WHERE id = :decision_id
                {lock_clause}
                """
            ),
            {"decision_id": decision_id},
        ).mappings().first()
        if not row:
            raise DecisionNotFoundError("Decision not found")
        return dict(row)

    def transition(
        self,
        connection: Connection,
        decision_id: str,
        *,
        action: str,
        actor: str,
        note: Optional[str],
    ) -> dict:
        if action not in {"approve", "reject"}:
            raise ValueError(f"Unsupported action: {action}")

        row = self.get_row(connection, decision_id, for_update=True)
        if row["status"] != "pending":
            raise InvalidDecisionTransitionError(
                f"Decision is already {row['status']} and cannot be {action}d."
            )

        deadline_at = row["deadline_at"]
        if deadline_at and deadline_at <= now_utc():
            self.expire_overdue(connection, row["project_id"])
            raise InvalidDecisionTransitionError(
                "Decision deadline has passed and the safe default was applied."
            )

        status = "approved" if action == "approve" else "rejected"
        approved_by = actor if action == "approve" else None
        approved_at = now_utc() if action == "approve" else None
        connection.execute(
            text(
                """
                UPDATE decisions
                SET status = :status,
                    approved_by = :approved_by,
                    approved_at = :approved_at,
                    updated_at = :updated_at
                WHERE id = :decision_id
                """
            ),
            {
                "decision_id": decision_id,
                "status": status,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "updated_at": now_utc(),
            },
        )
        emit_event(
            connection,
            row["project_id"],
            f"decision.{status}",
            actor,
            "decision",
            decision_id,
            {
                "from_status": "pending",
                "to_status": status,
                "note": note,
                "default_action": row["default_action"] or "hold",
            },
        )
        if status == "approved":
            self._handle_approved_follow_on(connection, row, actor)
        return self.get_row(connection, decision_id)

    def _handle_approved_follow_on(
        self, connection: Connection, row: dict[str, Any], actor: str
    ) -> None:
        recommendation = row["recommendation"] or {}
        action = recommendation.get("action")
        if action == "review_plan_draft":
            self._queue_pr_proposal_from_plan(connection, row, actor, recommendation)
        elif action == "review_pull_request":
            self._create_merge_gate_block(connection, row, actor, recommendation)

    def _queue_pr_proposal_from_plan(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:

        artifact_id = recommendation.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_plan_artifact_id"},
            )
            return

        artifact = connection.execute(
            text(
                """
                SELECT content
                FROM artifacts
                WHERE id = :artifact_id AND project_id = :project_id
                """
            ),
            {"artifact_id": artifact_id, "project_id": row["project_id"]},
        ).mappings().first()
        if not artifact:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "plan_artifact_not_found", "artifact_id": artifact_id},
            )
            return

        task = self._task_context(connection, row["task_id"])
        reservation = self.budget.reserve(
            connection,
            row["project_id"],
            "tool",
            self.settings.software_factory_pr_budget_usd_micros,
            "Approved plan draft; queue sandbox PR proposal.",
            60,
        )
        if reservation["status"] != "reserved":
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_budget_blocked",
                actor,
                "decision",
                row["id"],
                {
                    "budget_decision_id": reservation.get("decision_id"),
                    "requested_usd_micros": self.settings.software_factory_pr_budget_usd_micros,
                },
            )
            return

        branch_suffix = row["task_id"] or row["id"]
        branch_name = f"boss/proposal-{branch_suffix}"
        title = self._proposal_title(task)
        summary = self._proposal_summary(task, artifact["content"] or {})
        file_path = f"proposals/{branch_suffix}.md"
        file_content = self._proposal_markdown(task, artifact["content"] or {}, actor)
        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="software_factory.propose_pr",
            priority=40,
            payload={
                "project_id": row["project_id"],
                "reservation_id": reservation["reservation_id"],
                "dedupe_key": f"approved-plan-proposal-{row['id']}",
                "title": title,
                "branch_name": branch_name,
                "summary": summary,
                "files": [{"path": file_path, "content": file_content}],
                "requires_reservation": True,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_job_queued",
            actor,
            "job",
            job["id"],
            {
                "decision_id": row["id"],
                "reservation_id": reservation["reservation_id"],
                "kind": "software_factory.propose_pr",
            },
        )

    def _create_merge_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        merge_gate_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "merge_gate",
            f"Merge gate for {title}",
            {
                "status": "blocked_manual_merge_required",
                "reason": "Automatic merge and deploy effects are still disabled in this phase.",
                "approved_review_decision_id": row["id"],
                "approved_by": actor,
                "pull_request_url": recommendation.get("pull_request_url"),
                "branch_name": recommendation.get("branch_name"),
                "review_packet_artifact_id": recommendation.get("artifact_id"),
                "diff_artifact_id": recommendation.get("diff_artifact_id"),
                "test_report_artifact_id": recommendation.get("test_report_artifact_id"),
                "receipt_id": recommendation.get("receipt_id"),
                "next_action": "Review and merge the sandbox PR manually in GitHub. Deploy remains outside BOSS automation in this phase.",
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_blocked",
            actor,
            "artifact",
            merge_gate_artifact_id,
            {
                "decision_id": row["id"],
                "blocked_action": "github.pull_request.merge",
                "reason": "manual_merge_required",
                "pull_request_url": recommendation.get("pull_request_url"),
                "receipt_id": recommendation.get("receipt_id"),
            },
        )

    def _task_context(
        self, connection: Connection, task_id: Optional[str]
    ) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        task = connection.execute(
            text(
                """
                SELECT id, title, description, inputs
                FROM tasks
                WHERE id = :task_id
                """
            ),
            {"task_id": task_id},
        ).mappings().first()
        return dict(task) if task else None

    def _proposal_title(self, task: Optional[dict[str, Any]]) -> str:
        if not task:
            return "BOSS sandbox proposal"
        title = task.get("title")
        if isinstance(title, str) and title.strip():
            return f"BOSS proposal: {title[:80]}"
        return "BOSS sandbox proposal"

    def _proposal_summary(
        self, task: Optional[dict[str, Any]], plan_content: dict[str, Any]
    ) -> str:
        if task:
            description = task.get("description")
            if isinstance(description, str) and description.strip():
                return description
        summary = plan_content.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
        return "Create a sandbox PR proposal from an approved BOSS plan draft."

    def _proposal_markdown(
        self,
        task: Optional[dict[str, Any]],
        plan_content: dict[str, Any],
        actor: str,
    ) -> str:
        command = None
        if task:
            inputs = task.get("inputs")
            if isinstance(inputs, dict):
                command = inputs.get("command")
        if not isinstance(command, str) or not command.strip():
            command = self._proposal_summary(task, plan_content)

        lines = [
            "# BOSS Approved Plan Proposal",
            "",
            f"- Approved by: {actor}",
            f"- Command: {command}",
            "",
            "## Objectives",
        ]
        for objective in plan_content.get("objectives", []):
            lines.append(f"- {objective}")
        lines.extend(["", "## Steps"])
        for step in plan_content.get("steps", []):
            lines.append(f"- {step}")
        lines.extend(["", "## Risks"])
        for risk in plan_content.get("risks", []):
            lines.append(f"- {risk}")
        lines.extend(["", "## Done When"])
        for item in plan_content.get("done_when", []):
            lines.append(f"- {item}")
        return "\n".join(lines) + "\n"
