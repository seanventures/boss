from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db import begin
from .core import create_artifact, emit_event, make_id, now_utc
from .intake import CommandIntakeService

# Default staged-rollout ladder for the self-improvement runtime: ship narrow, then widen, with a
# health observation gating each step. Auto-rollback trips if an observation regresses past the cap.
DEFAULT_ROLLOUT_STAGES = ["canary_10pct", "expand_50pct", "full_100pct"]
DEFAULT_ROLLOUT_THRESHOLDS = {"max_error_rate": 0.05}


class SelfImprovementProposalNotFoundError(RuntimeError):
    pass


class InvalidSelfImprovementTransitionError(RuntimeError):
    pass


class SelfImprovementService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.intake = CommandIntakeService(settings)

    def list_proposals(
        self, connection: Connection, project_id: str, limit: int, offset: int = 0
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, title, summary, change_ref, diff_summary,
                       status, risk_tier, expected_impact, test_plan, canary_scope,
                       rollback_plan, success_metrics, status_note, proposal_artifact_id,
                       current_decision_id, canary_artifact_id, rollout_artifact_id,
                       rollback_artifact_id, rollout_state, created_by, created_at, updated_at
                FROM self_improvement_proposals
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"project_id": project_id, "limit": limit, "offset": offset},
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
                       rollback_artifact_id, rollout_state, created_by, created_at, updated_at
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

    def run_canary(
        self, engine, proposal_id: str, actor: str = "self_improvement_runtime"
    ) -> dict[str, Any]:
        """Self-improvement runtime: automatically execute the canary + eval-harness suites for an
        approved proposal and record the result — no human needs to report the outcome. Success
        opens the rollout decision; failure records a failed canary (spec §C self-improvement runtime)."""
        from .canaries import run_canary_suite
        from .eval_harness import run_eval_suite

        with begin(engine) as connection:
            proposal = self.get_proposal(connection, proposal_id)
        if proposal["status"] != "canary_approved":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and cannot run a canary "
                "(needs an approved canary decision first)."
            )
        project_id = proposal["project_id"]
        suite_actor = f"self_improvement:{proposal_id}"
        canary = run_canary_suite(engine, self.settings, project_id, actor=suite_actor)
        evals = run_eval_suite(engine, self.settings, project_id, actor=suite_actor)
        outcome = (
            "succeeded"
            if canary["status"] == "pass" and evals["status"] == "pass"
            else "failed"
        )
        summary = (
            f"Auto-canary: canary suite {canary['status']} "
            f"({canary['passed']} pass / {canary['failed']} fail), "
            f"eval-harness {evals['status']} "
            f"({evals['passed']} pass / {evals['failed']} fail)."
        )
        metrics = {
            "canary_status": canary["status"],
            "canary_passed": canary["passed"],
            "canary_failed": canary["failed"],
            "eval_status": evals["status"],
            "eval_passed": evals["passed"],
            "eval_failed": evals["failed"],
        }
        evidence_refs = [f"canary_run:{canary['id']}", f"eval_run:{evals['id']}"]
        with begin(engine) as connection:
            return self.record_canary_result(
                connection, proposal_id, actor, outcome, summary, metrics, evidence_refs
            )

    def start_rollout(
        self,
        connection: Connection,
        proposal_id: str,
        actor: str,
        stages: Optional[list[str]] = None,
        thresholds: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Begin a staged rollout (ship narrow → widen) for a proposal whose rollout was approved.
        Each stage is then gated by an `advance_rollout` health observation; a regressing observation
        auto-rolls-back. (spec §C self-improvement runtime: staged rollout + auto-rollback.)"""
        actor = self._require_text(actor, "actor")
        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        if proposal["status"] != "rollout_approved":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and cannot begin a staged rollout."
            )
        stages = (
            self._require_non_empty_string_list(stages, "stages")
            if stages
            else list(DEFAULT_ROLLOUT_STAGES)
        )
        thresholds = {**DEFAULT_ROLLOUT_THRESHOLDS, **(thresholds or {})}
        rollout_state = {
            "stages": stages,
            "current_index": 0,
            "status": "in_progress",
            "thresholds": thresholds,
            "history": [],
        }
        timestamp = now_utc()
        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status = 'rolling_out',
                    status_note = :status_note,
                    rollout_state = CAST(:rollout_state AS JSONB),
                    current_decision_id = NULL,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": f"Staged rollout started: {stages[0]}",
                "rollout_state": json.dumps(rollout_state),
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        emit_event(
            connection,
            proposal["project_id"],
            "self_improvement.rollout_started",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {"stages": stages, "thresholds": thresholds, "stage": stages[0]},
        )
        return self.get_proposal(connection, proposal_id)

    def advance_rollout(
        self,
        connection: Connection,
        proposal_id: str,
        actor: str,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a health observation for the current rollout stage. If it regresses past the
        auto-rollback thresholds, roll back immediately; otherwise advance to the next stage (or
        finalize the rollout when the last stage is healthy)."""
        actor = self._require_text(actor, "actor")
        observation = self._require_non_empty_mapping(observation, "observation")
        proposal = self.get_proposal(connection, proposal_id, for_update=True)
        if proposal["status"] != "rolling_out":
            raise InvalidSelfImprovementTransitionError(
                f"Proposal is {proposal['status']} and has no staged rollout in progress."
            )
        state = dict(proposal.get("rollout_state") or {})
        stages = state.get("stages") or list(DEFAULT_ROLLOUT_STAGES)
        index = int(state.get("current_index", 0))
        thresholds = state.get("thresholds") or dict(DEFAULT_ROLLOUT_THRESHOLDS)
        current_stage = stages[index] if index < len(stages) else stages[-1]
        history = list(state.get("history") or [])
        timestamp = now_utc()

        regression = self._rollout_regression(observation, thresholds)
        if regression is not None:
            history.append(
                {
                    "stage": current_stage,
                    "observation": observation,
                    "result": "rolled_back",
                    "reason": regression,
                    "at": timestamp.isoformat(),
                }
            )
            state["history"] = history
            state["status"] = "rolled_back"
            reason = f"Auto-rollback at stage {current_stage}: {regression}"
            self._record_rollback(
                connection,
                proposal,
                actor,
                reason,
                None,
                {"observation": observation, "stage": current_stage, "trigger": regression},
                [f"rollout_stage:{current_stage}"],
                rollout_state=state,
            )
            emit_event(
                connection,
                proposal["project_id"],
                "self_improvement.rollout_auto_rolled_back",
                actor,
                "self_improvement_proposal",
                proposal_id,
                {"stage": current_stage, "reason": regression, "observation": observation},
            )
            return self.get_proposal(connection, proposal_id)

        history.append(
            {
                "stage": current_stage,
                "observation": observation,
                "result": "advanced",
                "at": timestamp.isoformat(),
            }
        )
        next_index = index + 1
        state["history"] = history
        if next_index >= len(stages):
            state["current_index"] = len(stages)
            state["status"] = "completed"
            summary = f"Staged rollout completed ({len(stages)} stages, all healthy)."
            self._finalize_rollout(
                connection,
                proposal,
                actor,
                summary,
                None,
                {"stages": stages, "observations": [h["observation"] for h in history]},
                [f"rollout_stage:{stage}" for stage in stages],
                rollout_state=state,
            )
            emit_event(
                connection,
                proposal["project_id"],
                "self_improvement.rollout_completed",
                actor,
                "self_improvement_proposal",
                proposal_id,
                {"stages": stages},
            )
            return self.get_proposal(connection, proposal_id)

        state["current_index"] = next_index
        next_stage = stages[next_index]
        connection.execute(
            text(
                """
                UPDATE self_improvement_proposals
                SET status_note = :status_note,
                    rollout_state = CAST(:rollout_state AS JSONB),
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": f"Staged rollout advanced to {next_stage}",
                "rollout_state": json.dumps(state),
                "updated_at": timestamp,
                "proposal_id": proposal_id,
            },
        )
        emit_event(
            connection,
            proposal["project_id"],
            "self_improvement.rollout_advanced",
            actor,
            "self_improvement_proposal",
            proposal_id,
            {"from_stage": current_stage, "to_stage": next_stage, "observation": observation},
        )
        return self.get_proposal(connection, proposal_id)

    def _rollout_regression(
        self, observation: dict[str, Any], thresholds: dict[str, Any]
    ) -> Optional[str]:
        """Return a reason string if the observation breaches an auto-rollback threshold, else None."""
        health = str(observation.get("health", "")).strip().lower()
        if health in {"degraded", "unhealthy", "failing", "fail"}:
            return f"health={health}"
        if observation.get("regression") is True:
            return "regression_flag"
        error_rate = observation.get("error_rate")
        max_error_rate = thresholds.get("max_error_rate")
        if error_rate is not None and max_error_rate is not None:
            try:
                if float(error_rate) > float(max_error_rate):
                    return f"error_rate {error_rate} > {max_error_rate}"
            except (TypeError, ValueError):
                pass
        return None

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
        self._finalize_rollout(
            connection, proposal, actor, summary, external_ref, metrics, evidence_refs
        )
        return self.get_proposal(connection, proposal_id)

    def _finalize_rollout(
        self,
        connection: Connection,
        proposal: dict[str, Any],
        actor: str,
        summary: str,
        external_ref: Optional[str],
        metrics: dict[str, Any],
        evidence_refs: list[str],
        rollout_state: Optional[dict[str, Any]] = None,
    ) -> str:
        proposal_id = proposal["id"]
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
                    rollout_state = COALESCE(CAST(:rollout_state AS JSONB), rollout_state),
                    current_decision_id = NULL,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": summary,
                "rollout_artifact_id": artifact_id,
                "rollout_state": json.dumps(rollout_state) if rollout_state is not None else None,
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
        return artifact_id

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
        self._record_rollback(
            connection, proposal, actor, reason, external_ref, metrics, evidence_refs
        )
        return self.get_proposal(connection, proposal_id)

    def _record_rollback(
        self,
        connection: Connection,
        proposal: dict[str, Any],
        actor: str,
        reason: str,
        external_ref: Optional[str],
        metrics: dict[str, Any],
        evidence_refs: list[str],
        rollout_state: Optional[dict[str, Any]] = None,
    ) -> str:
        proposal_id = proposal["id"]
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
                    rollout_state = COALESCE(CAST(:rollout_state AS JSONB), rollout_state),
                    current_decision_id = NULL,
                    updated_at = :updated_at
                WHERE id = :proposal_id
                """
            ),
            {
                "status_note": reason,
                "rollback_artifact_id": artifact_id,
                "rollout_state": json.dumps(rollout_state) if rollout_state is not None else None,
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
        return artifact_id

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
