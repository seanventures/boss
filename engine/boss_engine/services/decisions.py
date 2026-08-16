from __future__ import annotations

import json

import re
import threading
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .budget import BudgetGovernor
from .core import create_artifact, emit_event, now_utc
from .infrastructure import InfrastructureService
from .intake import CommandIntakeService
from .jobs import JobLeaseService
from .members import MemberDirectoryService
from .role_governance import RoleGovernanceService
from .self_improvement import SelfImprovementService
from .specpack import AgentModuleNotFoundError, SpecPackService


class DecisionNotFoundError(Exception):
    pass


class InvalidDecisionTransitionError(Exception):
    pass


#: The actor recorded when a policy, not a person, resolves a gate. Deliberately not a member
#: of any org: it must be impossible to sign in as, and obvious in an audit trail.
SYSTEM_ACTOR = "auto_approval"


class DecisionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.jobs = JobLeaseService(settings)
        self.infrastructure = InfrastructureService(settings)
        self.intake = CommandIntakeService(settings)
        self.self_improvement = SelfImprovementService(settings)
        self.role_governance = RoleGovernanceService(settings)
        self.specpack = SpecPackService(settings)
        self.members = MemberDirectoryService(settings)

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
                WHERE status IN ('pending', 'delegated')
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
                    WHERE id = :decision_id AND status IN ('pending', 'delegated')
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

    def create_digest(
        self,
        connection: Connection,
        *,
        project_id: str,
        actor: str,
        max_items: int,
        low_risk_max: int,
        urgent_risk_min: int,
    ) -> dict[str, Any]:
        self.expire_overdue(connection, project_id)
        rows = connection.execute(
            text(
                """
                SELECT id, task_id, question, status, risk_tier, default_action,
                       recommendation, cost_summary, deadline_at, created_at
                FROM decisions
                WHERE project_id = :project_id
                  AND status IN ('pending', 'delegated')
                ORDER BY
                  risk_tier DESC,
                  deadline_at ASC NULLS LAST,
                  created_at ASC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": max_items},
        ).mappings().all()
        items = [self._digest_item(dict(row)) for row in rows]
        urgent_items = [
            item for item in items if int(item["risk_tier"]) >= urgent_risk_min
        ]
        batched_items = [
            item for item in items if int(item["risk_tier"]) <= low_risk_max
        ]
        standard_items = [
            item
            for item in items
            if item["id"]
            not in {entry["id"] for entry in [*urgent_items, *batched_items]}
        ]
        content = {
            "project_id": project_id,
            "generated_by": actor,
            "generated_at": now_utc().isoformat(),
            "policy": {
                "max_items": max_items,
                "low_risk_max": low_risk_max,
                "urgent_risk_min": urgent_risk_min,
            },
            "urgent": urgent_items,
            "standard": standard_items,
            "batched_low_risk": batched_items,
        }
        body = self._digest_body(content)
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "decision_digest",
            "Decision digest",
            {**content, "body": body},
        )
        emit_event(
            connection,
            project_id,
            "decision.digest_created",
            actor,
            "artifact",
            artifact_id,
            {
                "decision_count": len(items),
                "urgent_count": len(urgent_items),
                "batched_count": len(batched_items),
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "decision_count": len(items),
            "urgent_count": len(urgent_items),
            "batched_count": len(batched_items),
            "body": body,
            "content": content,
        }

    def get_row(
        self, connection: Connection, decision_id: str, *, for_update: bool = False
    ) -> dict:
        lock_clause = "FOR UPDATE" if for_update else ""
        row = connection.execute(
            text(
                f"""
                SELECT id, project_id, task_id, question, status, risk_tier, default_action,
                       recommendation, options, cost_summary, deadline_at,
                       approved_by, approved_at, delegated_to, delegated_by,
                       delegated_at, revision_requested_by, revision_requested_at,
                       executed_by, executed_at, resolution_note, external_ref,
                       created_at, updated_at
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

    def _digest_item(self, row: dict[str, Any]) -> dict[str, Any]:
        recommendation = row.get("recommendation") or {}
        action = recommendation.get("action") if isinstance(recommendation, dict) else None
        return {
            "id": row["id"],
            "task_id": row.get("task_id"),
            "question": row["question"],
            "status": row["status"],
            "risk_tier": int(row["risk_tier"]),
            "default_action": row.get("default_action") or "hold",
            "recommendation_action": action or "review_required",
            "deadline_at": row["deadline_at"].isoformat()
            if row.get("deadline_at")
            else None,
            "created_at": row["created_at"].isoformat(),
            "cost_summary": row.get("cost_summary") or {},
        }

    def _digest_body(self, content: dict[str, Any]) -> str:
        lines = [
            "Decision Digest",
            f"Generated: {content['generated_at']}",
            "",
        ]
        sections = [
            ("Urgent", content["urgent"]),
            ("Standard", content["standard"]),
            ("Low-risk batch", content["batched_low_risk"]),
        ]
        for title, items in sections:
            lines.append(f"{title}:")
            if not items:
                lines.append("- None")
            for item in items:
                deadline = item.get("deadline_at") or "no deadline"
                lines.append(
                    "- "
                    f"{item['id']} / tier {item['risk_tier']} / "
                    f"{item['recommendation_action']} / default {item['default_action']} / "
                    f"{deadline}: {item['question']}"
                )
            lines.append("")
        return "\n".join(lines).strip()

    def transition(
        self,
        connection: Connection,
        decision_id: str,
        *,
        action: str,
        actor: str,
        note: Optional[str],
        delegate_to: Optional[str] = None,
        external_ref: Optional[str] = None,
        force: bool = False,
        system_policy: bool = False,
    ) -> dict:
        if action not in {"approve", "reject", "revise", "delegate", "execute"}:
            raise ValueError(f"Unsupported action: {action}")
        if force and not (note or "").strip():
            # A force merge overrides BOSS's own safety verdict. The reason is the ONLY
            # record of why, so it is mandatory — an unexplained override is indistinguishable
            # from a bug six months later.
            raise ValueError(
                "force requires a note: state why BOSS's merge gate is being overridden.")

        row = self.get_row(connection, decision_id, for_update=True)
        current_status = row["status"]
        timestamp = now_utc()
        # A policy resolving a gate is not a person acting, and member validation asks the
        # wrong question of it: SYSTEM_ACTOR is deliberately not an org member, so requiring
        # membership would have silently disabled every policy-driven approval.
        #
        # Narrow on purpose. Only an approval, only under the reserved actor name, only when
        # the caller states it is policy — anything else still needs a member with
        # decision_resolve. The row records SYSTEM_ACTOR either way, so an auditor can always
        # separate the approvals a person made from the ones a setting made.
        if system_policy and action == "approve" and actor == SYSTEM_ACTOR:
            pass
        else:
            self.members.require_decision_actor(
                connection,
                row["project_id"],
                actor,
                action=action,
                risk_tier=row["risk_tier"],
                delegate_to=delegate_to,
                delegated_to=row.get("delegated_to"),
            )

        if action in {"approve", "reject", "revise", "delegate"}:
            if current_status not in {"pending", "delegated"}:
                raise InvalidDecisionTransitionError(
                    f"Decision is already {current_status} and cannot be {action}d."
                )
            deadline_at = row["deadline_at"]
            if deadline_at and deadline_at <= timestamp:
                self.expire_overdue(connection, row["project_id"])
                raise InvalidDecisionTransitionError(
                    "Decision deadline has passed and the safe default was applied."
                )

        if action == "approve":
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'approved',
                        approved_by = :actor,
                        approved_at = :timestamp,
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.approved",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "approved",
                    "note": note,
                    "default_action": row["default_action"] or "hold",
                },
            )
            refreshed = self.get_row(connection, decision_id)
            self._handle_approved_follow_on(
                connection, refreshed, actor, note=note, force=force
            )
            return self.get_row(connection, decision_id)

        if action == "reject":
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'rejected',
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "note": note,
                    "timestamp": timestamp,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.rejected",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "rejected",
                    "note": note,
                    "default_action": row["default_action"] or "hold",
                },
            )
            refreshed = self.get_row(connection, decision_id)
            self._handle_non_approved_follow_on(
                connection, refreshed, action="reject", actor=actor, note=note
            )
            return self.get_row(connection, decision_id)

        if action == "revise":
            if not note or not note.strip():
                raise ValueError("note is required when requesting revision")
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'revision_requested',
                        revision_requested_by = :actor,
                        revision_requested_at = :timestamp,
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note.strip(),
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.revision_requested",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "revision_requested",
                    "note": note.strip(),
                },
            )
            refreshed = self.get_row(connection, decision_id)
            self._handle_non_approved_follow_on(
                connection, refreshed, action="revise", actor=actor, note=note
            )
            return self.get_row(connection, decision_id)

        if action == "delegate":
            if not delegate_to or not delegate_to.strip():
                raise ValueError("delegate_to is required when delegating a decision")
            delegate_target = delegate_to.strip()
            connection.execute(
                text(
                    """
                    UPDATE decisions
                    SET status = 'delegated',
                        delegated_to = :delegate_to,
                        delegated_by = :actor,
                        delegated_at = :timestamp,
                        resolution_note = :note,
                        updated_at = :timestamp
                    WHERE id = :decision_id
                    """
                ),
                {
                    "decision_id": decision_id,
                    "delegate_to": delegate_target,
                    "actor": actor,
                    "timestamp": timestamp,
                    "note": note,
                },
            )
            emit_event(
                connection,
                row["project_id"],
                "decision.delegated",
                actor,
                "decision",
                decision_id,
                {
                    "from_status": current_status,
                    "to_status": "delegated",
                    "delegate_to": delegate_target,
                    "note": note,
                },
            )
            return self.get_row(connection, decision_id)

        if current_status != "approved":
            raise InvalidDecisionTransitionError(
                f"Decision is {current_status} and cannot be executed."
            )
        if not (note and note.strip()) and not (external_ref and external_ref.strip()):
            raise ValueError(
                "note or external_ref is required when executing a decision manually"
            )

        execution_note = note.strip() if isinstance(note, str) and note.strip() else None
        external_reference = (
            external_ref.strip()
            if isinstance(external_ref, str) and external_ref.strip()
            else None
        )
        execution_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "manual_execution_record",
            f"Manual execution record for {row['question'][:80]}",
            {
                "decision_id": decision_id,
                "question": row["question"],
                "executed_by": actor,
                "executed_at": timestamp.isoformat(),
                "note": execution_note,
                "external_ref": external_reference,
            },
        )
        connection.execute(
            text(
                """
                UPDATE decisions
                SET status = 'executed',
                    executed_by = :actor,
                    executed_at = :timestamp,
                    resolution_note = :note,
                    external_ref = :external_ref,
                    updated_at = :timestamp
                WHERE id = :decision_id
                """
            ),
            {
                "decision_id": decision_id,
                "actor": actor,
                "timestamp": timestamp,
                "note": execution_note,
                "external_ref": external_reference,
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.executed",
            actor,
            "decision",
            decision_id,
            {
                "from_status": current_status,
                "to_status": "executed",
                "artifact_id": execution_artifact_id,
                "external_ref": external_reference,
            },
        )
        if row["task_id"]:
            updated = connection.execute(
                text(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        updated_at = :timestamp
                    WHERE id = :task_id AND status <> 'completed'
                    """
                ),
                {"task_id": row["task_id"], "timestamp": timestamp},
            )
            if updated.rowcount == 1:
                emit_event(
                    connection,
                    row["project_id"],
                    "task.completed",
                    actor,
                    "task",
                    row["task_id"],
                    {"decision_id": decision_id},
                )
        return self.get_row(connection, decision_id)

    def _handle_approved_follow_on(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        *,
        note: Optional[str] = None,
        force: bool = False,
    ) -> None:
        """Approval context must be explicitly threaded into the follow-on.

        A forced merge is valid only with its operator rationale recorded in the
        audit event. Defaulting to a non-forced, empty-note context keeps the
        safe behaviour for callers that do not supply either value."""
        recommendation = row["recommendation"] or {}
        action = recommendation.get("action")
        if action == "execute_agent_tool":
            # A high-risk agent tool call that was deferred for approval — run it now.
            from .agent_loop import execute_agent_tool

            execute_agent_tool(
                connection,
                row["project_id"],
                self.settings,
                str(recommendation.get("tool")),
                recommendation.get("args") or {},
                actor,
                task_id=row["task_id"],
            )
            # Meeting PRD publish → auto-start a governed code task so the
            # approved outcome advances into coding (previously a dead-end).
            self._start_code_task_after_prd(connection, row, actor, recommendation)
        elif action == "resume_agent_run":
            # A durable agent run paused for approval — resume it via a worker job
            # so the conversation continues with the approved tool's result.
            run_id = recommendation.get("run_id")
            self.jobs.create_job(
                connection,
                project_id=row["project_id"],
                task_id=row["task_id"],
                kind="agent.resume",
                priority=45,
                payload={"run_id": run_id},
            )
            emit_event(
                connection,
                row["project_id"],
                "agent.resume_queued",
                actor,
                "agent_run",
                str(run_id),
                {"decision_id": row["id"]},
            )
        elif action == "resume_code_task":
            # A governed code task paused for human plan approval — resume it via a worker job.
            # The runner reuses the persisted design and continues (write tests -> implement -> PR).
            run_id = recommendation.get("run_id")
            # Flip the paused run back to running so the re-enqueued code.task job proceeds
            # (the runner reuses the persisted design and skips the design phase + gate).
            connection.execute(
                text("UPDATE code_task_runs SET status='running', updated_at=now() WHERE id=:id"),
                {"id": run_id},
            )
            self.jobs.create_job(
                connection,
                project_id=row["project_id"],
                task_id=row["task_id"],
                kind="code.task",
                priority=46,
                payload={"run_id": run_id},
            )
            emit_event(
                connection,
                row["project_id"],
                "code_task.resume_queued",
                actor,
                "code_task_run",
                str(run_id),
                {"decision_id": row["id"]},
            )
        elif action == "review_plan_draft":
            self._queue_pr_proposal_from_plan(connection, row, actor, recommendation)
        elif action == "github.community.dispatch_plan":
            plan_id = recommendation.get("plan_id") or recommendation.get(
                "proposed_plan_id"
            )
            if not isinstance(plan_id, str) or not plan_id:
                emit_event(
                    connection,
                    row["project_id"],
                    "decision.follow_on_skipped",
                    actor,
                    "decision",
                    row["id"],
                    {"reason": "missing_community_plan_id"},
                )
                return
            result = self.intake.dispatch_plan(
                connection,
                plan_id,
                project_id=row["project_id"],
                actor=actor,
            )
            emit_event(
                connection,
                row["project_id"],
                "github.community_plan_authorized",
                actor,
                "decision",
                row["id"],
                {"plan_id": plan_id, "phase_task_ids": result["phase_task_ids"]},
            )
        elif action == "github.community.merge_authorization":
            # Approval records authorization only. Community webhook work never
            # calls merge; a maintainer must still use the governed merge path.
            emit_event(
                connection,
                row["project_id"],
                "github.community_merge_authorized",
                actor,
                "decision",
                row["id"],
                {
                    "repository": recommendation.get("github_repository"),
                    "pull_request_number": recommendation.get("github_pr_number"),
                    "head_sha": recommendation.get("head_sha"),
                },
            )
        elif action == "review_pull_request":
            # OPERATOR FORCE MERGE. Automated merging stays conservative — it refuses when
            # BOSS's own gate found a real problem. But the operator can still say "merge it
            # anyway", and BOSS performs it so the action stays inside the governed path with
            # a receipt, instead of the human reaching for `gh pr merge` outside the system.
            # The override is recorded with WHO, WHY and exactly which verdict was overridden.
            ci_gate = None if force else self._pull_request_ci_gate(connection, row)
            if force:
                _overridden = self._pull_request_ci_gate(connection, row)
                emit_event(
                    connection, row["project_id"], "decision.merge_forced", actor,
                    "decision", row["id"],
                    {"reason": (note or "").strip()[:500],
                     "overridden_status": (_overridden or {}).get("status", "none"),
                     "overridden_reason": (_overridden or {}).get("reason", ""),
                     "pull_request_url": recommendation.get("pull_request_url"),
                     "note": "operator override — BOSS's automated merge gate did not pass"},
                )
            if ci_gate is not None:
                self._create_merge_gate_block(
                    connection,
                    row,
                    actor,
                    recommendation,
                    status=ci_gate["status"],
                    reason=ci_gate["reason"],
                    next_action=ci_gate["next_action"],
                    event_reason=ci_gate["event_reason"],
                    ci_validation_status=ci_gate["validation_status"],
                    ci_validation_summary=ci_gate["validation_summary"],
                )
                return
            if self.settings.enable_sandbox_merge or force:
                self._queue_merge_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_merge_gate_block(connection, row, actor, recommendation)
        elif action == "review_deployment":
            if self.settings.enable_sandbox_deploy:
                self._queue_deployment_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_deploy_gate_block(connection, row, actor, recommendation)
        elif action == "review_infra_provisioning":
            if self.settings.infra_real_provision_allowed:
                self._queue_infra_job_from_review(
                    connection, row, actor, recommendation
                )
            else:
                self._create_infra_gate_block(connection, row, actor, recommendation)
        elif action in {"self_improvement.begin_canary", "self_improvement.rollout"}:
            self.self_improvement.apply_decision_outcome(
                connection, row, "approve", actor, row.get("resolution_note")
            )
        elif action == "role.autonomy_change":
            self.role_governance.apply_decision_outcome(
                connection, row, "approve", actor, row.get("resolution_note")
            )

    def _handle_non_approved_follow_on(
        self,
        connection: Connection,
        row: dict[str, Any],
        *,
        action: str,
        actor: str,
        note: Optional[str],
    ) -> None:
        recommendation = row["recommendation"] or {}
        recommendation_action = recommendation.get("action")
        if recommendation_action in {
            "self_improvement.begin_canary",
            "self_improvement.rollout",
        }:
            self.self_improvement.apply_decision_outcome(
                connection, row, action, actor, note
            )
        elif recommendation_action == "role.autonomy_change":
            self.role_governance.apply_decision_outcome(
                connection, row, action, actor, note
            )

    def _start_code_task_after_prd(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        """After a meeting's PRD publish decision is approved, auto-start a
        governed code task seeded from the PRD so the task advances into coding
        (the meeting flow otherwise dead-ends at 'PRD published'). Idempotent —
        emits a visible event on start, skip-duplicate, or no-repo."""
        args = recommendation.get("args") or {}
        is_prd_publish = recommendation.get("tool") == "publish_document" and (
            bool(recommendation.get("start_code_task"))
            or args.get("repo_subdir") == "docs/prd"
        )
        task_id = row.get("task_id")
        if not is_prd_publish or not task_id:
            return
        # Publishing the PRD IS the deliverable. Auto-dumping a whole multi-feature PRD (or a
        # discovery task's write-up) into a single governed code task mis-routes research/spec work
        # into codegen — the pipeline then authors a sprawling, unfocused codebase that fails the
        # quality gate. Default OFF: stop at 'published' and let a human create scoped coding tasks.
        if not getattr(self.settings, "auto_code_task_after_prd", False):
            emit_event(
                connection,
                row["project_id"],
                "code_task.not_started",
                actor,
                "task",
                task_id,
                {"decision_id": row["id"],
                 "reason": "auto_codegen_after_prd_disabled",
                 "detail": "PRD published. Create scoped coding tasks explicitly rather than "
                           "auto-implementing the whole PRD."},
            )
            return
        # Idempotency: never double-start a code task for the same task.
        existing = connection.execute(
            text(
                "SELECT id FROM code_task_runs "
                "WHERE project_id = :p AND task_id = :t "
                "AND status NOT IN ('failed', 'cancelled') "
                "LIMIT 1"
            ),
            {"p": row["project_id"], "t": task_id},
        ).first()
        if existing is not None:
            emit_event(
                connection,
                row["project_id"],
                "code_task.already_started",
                actor,
                "code_task_run",
                existing[0],
                {"decision_id": row["id"], "task_id": task_id},
            )
            return
        owner = getattr(self.settings, "target_github_owner", None)
        repo = getattr(self.settings, "target_github_repo", None)
        if not owner or not repo:
            emit_event(
                connection,
                row["project_id"],
                "code_task.not_started",
                actor,
                "task",
                task_id,
                {"decision_id": row["id"], "reason": "no_target_repo_configured"},
            )
            return
        raw_title = str(args.get("title") or "Code task")
        title = raw_title[5:].strip() if raw_title.startswith("PRD: ") else raw_title
        prd_markdown = args.get("markdown") or ""
        directive = prd_markdown or f"Implement the approved plan: {title}"

        # The beginning of this outcome, recorded so the build can be drawn as the continuation
        # of the panel that specced it rather than as an unrelated run.
        origin = {
            "kind": "meeting",
            "task_id": task_id,
            "decision_id": row["id"],
            "title": title,
        }

        # Turn the approved PRD into a build. Never dump a whole multi-feature PRD into ONE giant
        # governed task (mis-routes -> sprawling code -> gate fail). Two proven paths:
        #  - WEB APP  -> start_web_app_build: scaffold + ONE focused task -> one PR + one approval.
        #                This is the reliable, end-to-end-verified path for a website (no fragile
        #                multi-slice chain), so prefer it whenever the PRD looks like a web app.
        #  - OTHERWISE -> start_decomposed_build: scoped, chained slices for genuinely complex work.
        # Both make an LLM call, so run OFF the approve request thread (mirrors start_discovery).
        project_id = row["project_id"]
        settings = self.settings
        jobs = self.jobs
        origin_ctx = origin

        def _launch_build() -> None:
            try:
                from ..db import get_engine
                from .prd_decomposition import (
                    _looks_like_web_app, start_decomposed_build, start_web_app_build,
                )

                engine = get_engine()
                if _looks_like_web_app(directive):
                    start_web_app_build(
                        engine, settings, jobs,
                        project_id=project_id, task_id=task_id, directive=directive,
                        owner=owner, repo=repo, review=False, origin=origin_ctx)
                else:
                    start_decomposed_build(
                        engine, settings, jobs,
                        project_id=project_id, task_id=task_id,
                        prd_or_directive=directive, owner=owner, repo=repo, base_branch="main")
            except Exception:  # noqa: BLE001 — best-effort; failure is visible via run status
                return

        threading.Thread(target=_launch_build, daemon=True).start()
        emit_event(
            connection,
            row["project_id"],
            "code_task.started_from_prd",
            actor,
            "task",
            task_id,
            {"decision_id": row["id"], "task_id": task_id, "title": title,
             "mode": "decomposed_build"},
        )

    def _boss_own_verdict(
        self, connection: Connection, row: dict[str, Any], recommendation: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """BOSS's OWN quality gate for the PR's run — the authoritative evidence.

        BOSS runs the test suite itself; that is a stronger signal than an external CI
        service, and it was never consulted here. The merge gate asked GitHub first and,
        when GitHub answered 403, blocked citing "ci_validation_incomplete" while holding
        far more damning evidence of its own: on PR #62 the run had already recorded
        existing_tests_intact=FAILED with the two pre-existing Sudoku test files it had
        edited. The operator saw a CI plumbing error instead of "this change weakens your
        tests".
        """
        branch = str(recommendation.get("branch_name") or "")
        root = branch.rsplit("/", 1)[-1] if branch.startswith("boss/code/") else ""
        if not root:
            return None
        gate = connection.execute(
            text(
                """
                SELECT q.gates_passed, q.gates_failed, q.code_task_run_id
                FROM quality_gates q
                JOIN code_task_runs r ON r.id = q.code_task_run_id
                WHERE q.project_id = :p
                  AND COALESCE(r.finalize_ctx->>'root_run_id', r.id) = :root
                ORDER BY q.created_at DESC
                LIMIT 1
                """
            ),
            {"p": row["project_id"], "root": root},
        ).mappings().first()
        if not gate:
            return None
        failed = gate["gates_failed"] or []
        if isinstance(failed, str):
            try:
                failed = json.loads(failed)
            except Exception:  # noqa: BLE001
                failed = []
        passed = gate["gates_passed"] or []
        if isinstance(passed, str):
            try:
                passed = json.loads(passed)
            except Exception:  # noqa: BLE001
                passed = []
        return {"passed": list(passed), "failed": list(failed),
                "run_id": gate["code_task_run_id"]}

    def _pull_request_ci_gate(
        self, connection: Connection, row: dict[str, Any]
    ) -> Optional[dict[str, str]]:
        recommendation = row["recommendation"] or {}

        # (1) BOSS'S OWN VERDICT FIRST. BOSS runs the suite itself; that outranks an external
        # CI signal and was previously never consulted here.
        own = self._boss_own_verdict(connection, row, recommendation)
        if own:
            # (3) HARD STOP: a change that edited or deleted PRE-EXISTING tests must never be
            # auto-merged. A green suite proves nothing if the guard tests were weakened, and
            # a merge into main is irreversible. never-block means "ship the PR and record the
            # dissent" -- which already happened -- NOT "merge damage into main".
            if "existing_tests_intact" in own["failed"]:
                return {
                    "status": "blocked_weakened_existing_tests",
                    "reason": ("This change modified or deleted PRE-EXISTING test files, so a "
                               "passing suite does not prove nothing broke. BOSS will not "
                               "auto-merge it."),
                    "next_action": ("Restore the original test files (or justify each edit) and "
                                    "re-run; the PR stays open in the meantime."),
                    "event_reason": "weakened_existing_tests",
                    "validation_status": "failed",
                    "validation_summary": ("BOSS gate existing_tests_intact FAILED for run "
                                           f"{own['run_id']}."),
                }

        artifact_id = recommendation.get("test_report_artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return {
                "status": "blocked_ci_validation_missing",
                "reason": "No test report artifact is linked to this sandbox PR review.",
                "next_action": "Re-run or regenerate the proposal so BOSS can capture validation evidence before merge.",
                "event_reason": "missing_test_report",
                "validation_status": "missing",
                "validation_summary": "No test report artifact was attached to the review decision.",
            }

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
            return {
                "status": "blocked_ci_validation_missing",
                "reason": "The linked sandbox test report artifact could not be found.",
                "next_action": "Re-run or regenerate the proposal so BOSS can capture validation evidence before merge.",
                "event_reason": "missing_test_report",
                "validation_status": "missing",
                "validation_summary": "The linked test report artifact was not found.",
            }

        content = artifact["content"] or {}
        validation_status = str(content.get("status") or "missing")
        validation_summary = str(
            content.get("summary")
            or content.get("notes")
            or "Validation evidence is incomplete."
        )
        if validation_status == "passed":
            return None
        if validation_status == "failed":
            return {
                "status": "blocked_ci_validation_failed",
                "reason": "Sandbox validation failed for this PR proposal.",
                "next_action": "Fix the failing checks and regenerate or update the proposal before merge approval.",
                "event_reason": "ci_validation_failed",
                "validation_status": validation_status,
                "validation_summary": validation_summary,
            }
        # (2) "COULD NOT OBTAIN" IS NOT "FAILED". A 403 on check-runs, or a repo with no CI at
        # all, is absence of evidence -- not evidence against. Blocking on it makes merging
        # impossible for any repo without CI (boss-sandbox has no .github/workflows) while BOSS
        # has already run the authoritative suite itself. Fall back to BOSS's own gate: if its
        # tests passed and it did not weaken the existing ones, proceed and record WHY. An
        # explicit CI failure (handled above) still blocks.
        if validation_status in ("unavailable", "missing", "incomplete") and own:
            _blocking = {"written_tests_pass", "nothing_broke", "existing_tests_intact"}
            _bad = sorted(_blocking.intersection(own["failed"]))
            if not _bad:
                emit_event(
                    connection, row["project_id"], "decision.ci_evidence_substituted",
                    "boss", "decision", row["id"],
                    {"external_status": validation_status,
                     "external_summary": validation_summary[:300],
                     "boss_gates_passed": own["passed"],
                     "reason": "external CI evidence unobtainable; BOSS ran the suite itself"},
                )
                return None
            return {
                "status": "blocked_boss_validation_failed",
                "reason": ("External CI evidence was unavailable AND BOSS's own checks did not "
                           f"pass: {_bad}."),
                "next_action": "Fix the failing BOSS gates and re-run before merge approval.",
                "event_reason": "boss_validation_failed",
                "validation_status": "failed",
                "validation_summary": f"BOSS gates failed: {_bad}",
            }
        return {
            "status": "blocked_ci_validation_incomplete",
            "reason": "GitHub validation has not completed successfully for this sandbox PR proposal.",
            "next_action": "Wait for the checks to finish successfully before merge approval.",
            "event_reason": "ci_validation_incomplete",
            "validation_status": validation_status,
            "validation_summary": validation_summary,
        }

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
        artifact_content = artifact["content"] or {}
        module_descriptor = artifact_content.get("agent_module") or {}
        agent_module_id = module_descriptor.get("id")
        if isinstance(agent_module_id, str) and agent_module_id:
            try:
                module_spec = self.specpack.resolve_installed_agent_module(
                    connection,
                    row["project_id"],
                    agent_module_id,
                )
            except AgentModuleNotFoundError:
                agent_module_id = None
            else:
                runtime = module_spec.get("runtime") or {}
                if "software_factory.propose_pr" not in (
                    runtime.get("job_kinds") or []
                ):
                    agent_module_id = None
        if not isinstance(agent_module_id, str) or not agent_module_id:
            default_module = self.specpack.find_default_agent_module_for_job_kind(
                connection,
                row["project_id"],
                "software_factory.propose_pr",
            )
            agent_module_id = (
                default_module.get("id")
                if isinstance(default_module, dict)
                else None
            )
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
        branch_prefix = "boss/"
        if agent_module_id:
            module_spec = self.specpack.resolve_installed_agent_module(
                connection, row["project_id"], agent_module_id
            )
            config = module_spec.get("config") or {}
            configured_prefix = config.get("branch_prefix")
            if isinstance(configured_prefix, str) and configured_prefix.strip():
                branch_prefix = configured_prefix.strip()
        if not branch_prefix.endswith("/"):
            branch_prefix = f"{branch_prefix}/"
        branch_name = f"{branch_prefix}proposal-{branch_suffix}"
        title = self._proposal_title(task)
        summary = self._proposal_summary(task, artifact_content)
        file_path = f"proposals/{branch_suffix}.md"
        file_content = self._proposal_markdown(task, artifact_content, actor)
        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="software_factory.propose_pr",
            priority=40,
            payload={
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "reservation_id": reservation["reservation_id"],
                "dedupe_key": f"approved-plan-proposal-{row['id']}",
                "title": title,
                "branch_name": branch_name,
                "summary": summary,
                "files": [{"path": file_path, "content": file_content}],
                "agent_module_id": agent_module_id,
                "requires_reservation": True,
            },
        )
        self.budget.attach_job(
            connection,
            row["project_id"],
            reservation["reservation_id"],
            job["id"],
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
                "agent_module_id": agent_module_id,
            },
        )

    def _queue_merge_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        pull_request_number = self._pull_request_number(recommendation)
        pull_request_url = recommendation.get("pull_request_url")
        if not pull_request_number or not isinstance(pull_request_url, str):
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_pull_request_metadata"},
            )
            return

        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        branch_name = recommendation.get("branch_name")
        if branch_name is not None and not isinstance(branch_name, str):
            branch_name = None

        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="github.pull_request.merge",
            priority=20,
            payload={
                "project_id": row["project_id"],
                "decision_id": row["id"],
                "dedupe_key": f"approved-pr-merge-{row['id']}",
                "pull_request_number": pull_request_number,
                "pull_request_url": pull_request_url,
                "title": title,
                "branch_name": branch_name,
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
                "kind": "github.pull_request.merge",
                "pull_request_url": pull_request_url,
            },
        )

    def _create_merge_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
        *,
        status: str = "blocked_manual_merge_required",
        reason: str = "Automatic merge and deploy effects are still disabled in this phase.",
        next_action: str = "Review and merge the sandbox PR manually in GitHub. Deploy remains outside BOSS automation in this phase.",
        event_reason: str = "manual_merge_required",
        ci_validation_status: Optional[str] = None,
        ci_validation_summary: Optional[str] = None,
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
                "status": status,
                "reason": reason,
                "approved_review_decision_id": row["id"],
                "approved_by": actor,
                "pull_request_url": recommendation.get("pull_request_url"),
                "branch_name": recommendation.get("branch_name"),
                "review_packet_artifact_id": recommendation.get("artifact_id"),
                "diff_artifact_id": recommendation.get("diff_artifact_id"),
                "test_report_artifact_id": recommendation.get("test_report_artifact_id"),
                "ci_validation_status": ci_validation_status,
                "ci_validation_summary": ci_validation_summary,
                "receipt_id": recommendation.get("receipt_id"),
                "next_action": next_action,
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
                "reason": event_reason,
                "pull_request_url": recommendation.get("pull_request_url"),
                "receipt_id": recommendation.get("receipt_id"),
            },
        )

    def _queue_deployment_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        merged_sha = recommendation.get("merged_sha")
        environment = recommendation.get("environment")
        pull_request_url = recommendation.get("pull_request_url")
        title = recommendation.get("title")
        branch_name = recommendation.get("branch_name")
        if not isinstance(merged_sha, str) or not merged_sha:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_deployment_sha"},
            )
            return
        if not isinstance(environment, str) or not environment:
            environment = self.settings.sandbox_deploy_environment
        if not isinstance(pull_request_url, str) or not pull_request_url:
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_deployment_pull_request_url"},
            )
            return
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if branch_name is not None and not isinstance(branch_name, str):
            branch_name = None

        job = self.jobs.create_job(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            kind="github.deployment.create",
            priority=15,
            payload={
                "project_id": row["project_id"],
                "decision_id": row["id"],
                "dedupe_key": f"approved-deploy-{row['id']}",
                "merged_sha": merged_sha,
                "pull_request_url": pull_request_url,
                "title": title,
                "branch_name": branch_name,
                "environment": environment,
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
                "kind": "github.deployment.create",
                "environment": environment,
                "pull_request_url": pull_request_url,
            },
        )

    def _create_deploy_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        deploy_gate_artifact_id = create_artifact(
            connection,
            self.settings,
            row["project_id"],
            row["task_id"],
            "deployment_gate",
            f"Deployment gate for {title}",
            {
                "status": "blocked_manual_deploy_required",
                "reason": "Automatic sandbox deploy is disabled in this phase.",
                "approved_review_decision_id": row["id"],
                "approved_by": actor,
                "pull_request_url": recommendation.get("pull_request_url"),
                "branch_name": recommendation.get("branch_name"),
                "merged_sha": recommendation.get("merged_sha"),
                "environment": recommendation.get("environment")
                or self.settings.sandbox_deploy_environment,
                "next_action": "Run the sandbox deployment manually or enable the deploy policy.",
            },
        )
        emit_event(
            connection,
            row["project_id"],
            "decision.follow_on_blocked",
            actor,
            "artifact",
            deploy_gate_artifact_id,
            {
                "decision_id": row["id"],
                "blocked_action": "github.deployment.create",
                "reason": "manual_deploy_required",
                "pull_request_url": recommendation.get("pull_request_url"),
            },
        )

    def _queue_infra_job_from_review(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        region = recommendation.get("region")
        size = recommendation.get("size")
        reservation_id = recommendation.get("reservation_id")
        ttl_minutes = recommendation.get("ttl_minutes")
        tags = recommendation.get("tags") or []
        data_boundary = recommendation.get("data_boundary")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if not isinstance(region, str) or not region.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_region"},
            )
            return
        if not isinstance(size, str) or not size.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_size"},
            )
            return
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_reservation"},
            )
            return
        if not isinstance(ttl_minutes, int):
            emit_event(
                connection,
                row["project_id"],
                "decision.follow_on_skipped",
                actor,
                "decision",
                row["id"],
                {"reason": "missing_infra_ttl"},
            )
            return
        job = self.infrastructure.queue_droplet_provision(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            decision_id=row["id"],
            reservation_id=reservation_id,
            dedupe_key=f"approved-infra-provision-{row['id']}",
            title=title,
            region=region,
            size=size,
            ttl_minutes=ttl_minutes,
            tags=tags if isinstance(tags, list) else [],
            actor=actor,
            priority=15,
            max_attempts=1,
            data_boundary=data_boundary if isinstance(data_boundary, str) else None,
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
                "kind": "infra.droplet.create",
                "reservation_id": reservation_id,
            },
        )

    def _create_infra_gate_block(
        self,
        connection: Connection,
        row: dict[str, Any],
        actor: str,
        recommendation: dict[str, Any],
    ) -> None:
        title = recommendation.get("title")
        region = recommendation.get("region")
        size = recommendation.get("size")
        reservation_id = recommendation.get("reservation_id")
        ttl_minutes = recommendation.get("ttl_minutes")
        data_boundary = recommendation.get("data_boundary")
        if not isinstance(title, str) or not title.strip():
            title = row["question"]
        if not isinstance(region, str) or not region:
            region = "unknown"
        if not isinstance(size, str) or not size:
            size = "unknown"
        if not isinstance(reservation_id, str):
            reservation_id = ""
        if not isinstance(ttl_minutes, int):
            ttl_minutes = 0
        self.infrastructure.create_provision_gate_block(
            connection,
            project_id=row["project_id"],
            task_id=row["task_id"],
            decision_id=row["id"],
            title=title,
            region=region,
            size=size,
            ttl_minutes=ttl_minutes,
            reservation_id=reservation_id,
            data_boundary=data_boundary if isinstance(data_boundary, str) else "internal",
            actor=actor,
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
            return "BOSS proposal: sandbox proposal"
        title = task.get("title")
        if isinstance(title, str) and title.strip():
            return f"BOSS proposal: {title[:80]}"
        return "BOSS proposal: sandbox proposal"

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

    def _pull_request_number(self, recommendation: dict[str, Any]) -> Optional[int]:
        direct = recommendation.get("pull_request_number")
        if isinstance(direct, int) and direct > 0:
            return direct
        url = recommendation.get("pull_request_url")
        if not isinstance(url, str):
            return None
        match = re.search(r"/pull/(\d+)$", url)
        if not match:
            return None
        return int(match.group(1))
