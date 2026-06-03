from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import create_artifact, emit_event, make_id, now_utc
from .intake import CommandIntakeService


class SelfImprovementProposalNotFoundError(RuntimeError):
    pass


class InvalidSelfImprovementTransitionError(RuntimeError):
    pass


class SelfImprovementService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.intake = CommandIntakeService(settings)

    def list_proposals(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, summary, change_ref, diff_summary,
                       status, risk_tier, expected_impact, test_plan, canary_scope,
                       rollback_plan, success_metrics, status_note, proposal_artifact_id,
                       current_decision_id, canary_artifact_id, rollout_artifact_id,
                       rollback_artifact_id, created_by, created_at, updated_at
                FROM self_improvement_proposals
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_proposal(
        self, connection: Connection, proposal_id: str, *, for_update: bool = False
    ) -> dict[str, Any]:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, task_id, title, summary, change_ref, diff_summary,
                       status, risk_tier, expected_impact, test_plan, canary_scope,
                       rollback_plan, success_metrics, status_note, proposal_artifact_id,
                       current_decision_id, canary_artifact_id, rollout_artifact_id,
                       rollback_artifact_id, created_by, created_at, updated_at
                FROM self_improvement_proposals
                WHERE id = :proposal_id
                {lock_clause}
                """
            ),
            {"proposal_id": proposal_id},
        ).mappings().first()
        if not row:
            raise SelfImprovementProposalNotFoundError(
                "Self-improvement proposal not found."
            )
        return dict(row)

    def create_proposal(
        self,
        connection: Connection,
        project_id: str,
        title: str,
        summary: str,
        change_ref: str,
        diff_summary: str,
        expected_impact: list[str],
        test_plan: list[str],
        canary_scope: list[str],
        rollback_plan: list[str],
        success_metrics: list[str],
        actor: str,
        risk_tier: int,
    ) -> dict[str, Any]:
        title = self._require_text(title, "title")
        summary = self._require_text(summary, "summary")
        change_ref = self._require_text(change_ref, "change_ref")
        diff_summary = self._require_text(diff_summary, "diff_summary")
        actor = self._require_text(actor, "actor")
        expected_impact = self._require_non_empty_string_list(
            expected_impact, "expected_impact"
        )
        test_plan = self._require_non_empty_string_list(test_plan, "test_plan")
        canary_scope = self._require_non_empty_string_list(
            canary_scope, "canary_scope"
        )
        rollback_plan = self._require_non_empty_string_list(
            rollback_plan, "rollback_plan"
        )
        success_metrics = self._require_non_empty_string_list(
            success_metrics, "success_metrics"
        )
        proposal_id = make_id("imp")
        task = self.intake.create_task(
            connection,
            project_id=project_id,
            title=f"Self-improvement: {title[:96]}",
            description=summary,
            dedupe_key=proposal_id,
            priority="p1",
            risk_tier=max(risk_tier, 2),
            inputs={
                "workflow": "self_improvement",
                "change_ref": change_ref,
                "summary": summary,
            },
            required_artifacts=[
                "self_improvement_proposal",
                "self_improvement_canary_report",
                "self_improvement_rollout_record",
                "self_improvement_rollback_record",
            ],
        )
        task_id = task["task_id"]
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "self_improvement_proposal",
            title,
            {
                "proposal_id": proposal_id,
                "title": title,
                "summary": summary,
                "change_ref": change_ref,
                "diff_summary": diff_summary,
                "expected_impact": expected_impact,
                "test_plan": test_plan,
                "canary_scope": canary_scope,
                "rollback_plan": rollback_plan,
                "success_metrics": success_metrics,
                "created_by": actor,
            },
        )
        decision_id = self._create_decision(
            connection,
            project_id=project_id,
            task_id=task_id,
            question=f"Approve self-improvement canary: {title}?",
            risk_tier=max(risk_tier, 2),
            recommendation={
                "action": "self_improvement.begin_canary",
                "proposal_id": proposal_id,
                "proposal_artifact_id": artifact_id,
                "change_ref": change_ref,
                "title": title,
            },
            cost_summary={"change_ref": change_ref, "phase": "canary"},
            reason="self_improvement_canary_review",
        )
        connection.execute(
            text(
                """
                INSERT INTO self_improvement_proposals (
                    id, project_id, task_id, title, summary, change_ref, diff_summary,
                    status, risk_tier, expected_impact, test_plan, canary_scope,
                    rollback_plan, success_metrics, proposal_artifact_id,
                    current_decision_id, created_by
                )
                VALUES (
                    :id, :project_id, :task_id, :title, :summary, :change_ref, :diff_summary,
                    'proposed', :risk_tier, CAST(:expected_impact AS JSONB),
                    CAST(:test_plan AS JSONB), CAST(:canary_scope AS JSONB),
                    CAST(:rollback_plan AS JSONB), CAST(:success_metrics AS JSONB),
                    :proposal_artifact_id, :current_decision_id, :created_by
                )
                """
            ),
            {
                "id": proposal_id,
                "project_id": project_id,
                "task_id": task_id,
                "title": title,
                "summary": summary,
                "change_ref": change_ref,
                "diff_summary": diff_summary,
                "risk_tier": max(risk_tier, 2),
                "expected_impact": json.dumps(expected_impact),
                "test_plan": json.dumps(test_plan),
                "canary_scope": json.dumps(canary_scope),
                "rollback_plan": json.dumps(rollback_plan),
                "success_metrics": json.dumps(success_metrics),
                "proposal_artifact_id": artifact_id,
                "current_decision_id": decision_id,
                "created_by": actor,
            },
        )
        emit_event(
            connection,
            project_id,
            "self_improvement.proposed",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {
                "task_id": task_id,
                "proposal_artifact_id": artifact_id,
                "decision_id": decision_id,
                "change_ref": change_ref,
            },
        )
        return self.get_proposal(connection, proposal_id)

    def apply_decision_outcome(
        self,
        connection: Connection,
        decision_row: dict[str, Any],
        outcome: str,
        actor: str,
        note: Optional[str],
    ) -> None:
        recommendation = decision_row.get("recommendation") or {}
        action = recommendation.get("action")
        proposal_id = recommendation.get("proposal_id")
        if action not in {"self_improvement.begin_canary", "self_improvement.rollout"}:
            return
        if not isinstance(proposal_id, str) or not proposal_id:
            return

        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        timestamp = now_utc()
        status_note = note.strip() if isinstance(note, str) and note.strip() else None

        if outcome == "approve":
            next_status = (
                "canary_approved"
                if action == "self_improvement.begin_canary"
                else "rollout_approved"
            )
            event_type = (
                "self_improvement.canary_approved"
                if action == "self_improvement.begin_canary"
                else "self_improvement.rollout_approved"
            )
            connection.execute(
                text(
                    """
                    UPDATE self_improvement_proposals
                    SET status = :status,
                        status_note = :status_note,
                        current_decision_id = NULL,
                        updated_at = :updated_at
                    WHERE id = :proposal_id
                    """
                ),
                {
                    "status": next_status,
                    "status_note": status_note,
                    "updated_at": timestamp,
                    "proposal_id": proposal_id,
                },
            )
            emit_event(
                connection,
                proposal["project_id"],
                event_type,
                actor,
                "self_improvement_proposal",
                proposal_id,
                {"decision_id": decision_row["id"], "note": status_note},
            )
            return

        if outcome == "reject":
            next_status = "rejected"
            event_type = "self_improvement.rejected"
        elif outcome == "revise":
            next_status = "revision_requested"
            event_type = "self_improvement.revision_requested"
        else:
            return

        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status = :status,
                    status_note = :status_note,
                    current_decision_id = NULL,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status": next_status,
                "status_note": status_note,
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        emit_event(
            connection,
            proposal["project_id"],
            event_type,
            actor,
            "self_improvement_proposal",
            proposal_id,
            {"decision_id": decision_row["id"], "note": status_note},
        )

    def record_canary_result(
        self,
        connection: Connection,
        proposal_id: str,
        actor: str,
        outcome: str,
        summary: str,
        metrics: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("outcome must be 'succeeded' or 'failed'")
        actor = self._require_text(actor, "actor")
        summary = self._require_text(summary, "summary")
        metrics = self._require_non_empty_mapping(metrics, "metrics")
        evidence_refs = self._require_non_empty_string_list(
            evidence_refs, "evidence_refs"
        )
        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        if proposal["status"] != "canary_approved":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and cannot record a canary result."
            )

        timestamp = now_utc()
        artifact_id = create_artifact(
            connection,
            self.settings,
            proposal["project_id"],
            proposal["task_id"],
            "self_improvement_canary_report",
            f"Canary report for {proposal['title']}",
            {
                "proposal_id": proposal_id,
                "outcome": outcome,
                "summary": summary,
                "metrics": metrics,
                "evidence_refs": evidence_refs,
                "recorded_by": actor,
                "recorded_at": timestamp.isoformat(),
            },
        )

        next_status = "canary_succeeded" if outcome == "succeeded" else "canary_failed"
        current_decision_id = None
        if outcome == "succeeded":
            current_decision_id = self._create_decision(
                connection,
                project_id=proposal["project_id"],
                task_id=proposal["task_id"],
                question=f"Approve self-improvement rollout: {proposal['title']}?",
                risk_tier=max(int(proposal["risk_tier"]), 2),
                recommendation={
                    "action": "self_improvement.rollout",
                    "proposal_id": proposal_id,
                    "change_ref": proposal["change_ref"],
                    "title": proposal["title"],
                    "canary_artifact_id": artifact_id,
                },
                cost_summary={"change_ref": proposal["change_ref"], "phase": "rollout"},
                reason="self_improvement_rollout_review",
            )

        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status = :status,
                    status_note = :status_note,
                    canary_artifact_id = :canary_artifact_id,
                    current_decision_id = :current_decision_id,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status": next_status,
                "status_note": summary,
                "canary_artifact_id": artifact_id,
                "current_decision_id": current_decision_id,
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        emit_event(
            connection,
            proposal["project_id"],
            "self_improvement.canary_recorded",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {
                "artifact_id": artifact_id,
                "outcome": outcome,
                "rollout_decision_id": current_decision_id,
            },
        )
        return self.get_proposal(connection, proposal_id)

    def record_rollout(
        self,
        connection: Connection,
        proposal_id: str,
        actor: str,
        summary: str,
        external_ref: Optional[str],
        metrics: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        if proposal["status"] != "rollout_approved":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and cannot be rolled out."
            )
        actor = self._require_text(actor, "actor")
        summary = self._require_text(summary, "summary")
        if external_ref is not None:
            external_ref = self._require_text(external_ref, "external_ref")
        metrics = self._require_non_empty_mapping(metrics, "metrics")
        evidence_refs = self._require_non_empty_string_list(
            evidence_refs, "evidence_refs"
        )

        timestamp = now_utc()
        artifact_id = create_artifact(
            connection,
            self.settings,
            proposal["project_id"],
            proposal["task_id"],
            "self_improvement_rollout_record",
            f"Rollout record for {proposal['title']}",
            {
                "proposal_id": proposal_id,
                "summary": summary,
                "external_ref": external_ref,
                "metrics": metrics,
                "evidence_refs": evidence_refs,
                "recorded_by": actor,
                "recorded_at": timestamp.isoformat(),
            },
        )
        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status = 'rolled_out',
                    status_note = :status_note,
                    rollout_artifact_id = :rollout_artifact_id,
                    current_decision_id = NULL,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": summary,
                "rollout_artifact_id": artifact_id,
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE tasks
                SET status = 'completed',
                    updated_at = :updated_at
                WHERE id = :task_id
                """
            ),
            {"task_id": proposal["task_id"], "updated_at": timestamp},
        )
        emit_event(
            connection,
            proposal["project_id"],
            "self_improvement.rolled_out",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {"artifact_id": artifact_id, "external_ref": external_ref},
        )
        emit_event(
            connection,
            proposal["project_id"],
            "task.completed",
            actor,
            "task",
            proposal["task_id"],
            {"proposal_id": proposal_id, "phase": "rollout"},
        )
        return self.get_proposal(connection, proposal_id)

    def rollback(
        self,
        connection: Connection,
        proposal_id: str,
        actor: str,
        reason: str,
        external_ref: Optional[str],
        metrics: dict[str, Any],
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        if proposal["status"] != "rolled_out":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and cannot be rolled back."
            )
        actor = self._require_text(actor, "actor")
        reason = self._require_text(reason, "reason")
        if external_ref is not None:
            external_ref = self._require_text(external_ref, "external_ref")
        metrics = self._require_non_empty_mapping(metrics, "metrics")
        evidence_refs = self._require_non_empty_string_list(
            evidence_refs, "evidence_refs"
        )

        timestamp = now_utc()
        artifact_id = create_artifact(
            connection,
            self.settings,
            proposal["project_id"],
            proposal["task_id"],
            "self_improvement_rollback_record",
            f"Rollback record for {proposal['title']}",
            {
                "proposal_id": proposal_id,
                "reason": reason,
                "external_ref": external_ref,
                "metrics": metrics,
                "evidence_refs": evidence_refs,
                "recorded_by": actor,
                "recorded_at": timestamp.isoformat(),
            },
        )
        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status = 'rolled_back',
                    status_note = :status_note,
                    rollback_artifact_id = :rollback_artifact_id,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": reason,
                "rollback_artifact_id": artifact_id,
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        connection.execute(
            text(
                """
                UPDATE tasks
                SET status = 'rolled_back',
                    updated_at = :updated_at
                WHERE id = :task_id
                """
            ),
            {"task_id": proposal["task_id"], "updated_at": timestamp},
        )
        emit_event(
            connection,
            proposal["project_id"],
            "self_improvement.rolled_back",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {"artifact_id": artifact_id, "external_ref": external_ref},
        )
        emit_event(
            connection,
            proposal["project_id"],
            "task.rolled_back",
            actor,
            "task",
            proposal["task_id"],
            {"proposal_id": proposal_id, "phase": "rollback"},
        )
        return self.get_proposal(connection, proposal_id)

    def _require_text(self, value: str, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank.")
        return cleaned

    def _require_non_empty_string_list(
        self, values: list[str], field_name: str
    ) -> list[str]:
        cleaned = [item.strip() for item in values if isinstance(item, str) and item.strip()]
        if not cleaned:
            raise ValueError(f"{field_name} must contain at least one non-empty item.")
        return cleaned

    def _require_non_empty_mapping(
        self, values: dict[str, Any], field_name: str
    ) -> dict[str, Any]:
        if not values:
            raise ValueError(f"{field_name} must contain at least one entry.")
        return values

    def _create_decision(
        self,
        connection: Connection,
        *,
        project_id: str,
        task_id: str,
        question: str,
        risk_tier: int,
        recommendation: dict[str, Any],
        cost_summary: dict[str, Any],
        reason: str,
    ) -> str:
        decision_id = make_id("dec")
        deadline_at = now_utc() + timedelta(hours=24)
        connection.execute(
            text(
                """
                INSERT INTO decisions (
                    id, project_id, task_id, question, recommendation, options, risk_tier,
                    cost_summary, deadline_at, default_action, status
                )
                VALUES (
                    :id, :project_id, :task_id, :question, CAST(:recommendation AS JSONB),
                    CAST(:options AS JSONB), :risk_tier, CAST(:cost_summary AS JSONB),
                    :deadline_at, 'hold', 'pending'
                )
                """
            ),
            {
                "id": decision_id,
                "project_id": project_id,
                "task_id": task_id,
                "question": question,
                "recommendation": json.dumps(recommendation),
                "options": json.dumps(
                    [
                        {"action": "approve", "label": "Approve"},
                        {"action": "revise", "label": "Request revision"},
                        {"action": "reject", "label": "Reject"},
                    ]
                ),
                "risk_tier": risk_tier,
                "cost_summary": json.dumps(cost_summary),
                "deadline_at": deadline_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "decision.created",
            "system",
            "decision",
            decision_id,
            {
                "reason": reason,
                "deadline_at": deadline_at.isoformat(),
                "default_action": "hold",
            },
        )
        return decision_id
