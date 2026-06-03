from __future__ import annotations

import json
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..schemas import FileChange
from .budget import BudgetGovernor
from .core import create_artifact, create_blocking_decision, emit_event
from .entitlements import EntitlementBrokerService
from .github_adapter import (
    GitHubAdapter,
    IntegrationConfigurationError,
    PullRequestValidationResult,
)
from .openai_adapter import OpenAIAdapter
from .receipts import ReceiptService


class SoftwareFactoryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.github = GitHubAdapter(settings)
        self.openai = OpenAIAdapter(settings)
        self.entitlements = EntitlementBrokerService(settings)

    def _validation_follow_up(
        self, validation: PullRequestValidationResult
    ) -> list[str]:
        if validation.status == "passed":
            return [
                "Review the diff and approval context before merge.",
                "Proceed to merge approval only if the review packet also looks correct.",
            ]
        if validation.status == "failed":
            return [
                "Fix the failing sandbox checks before merge approval.",
                "Review the failing GitHub check logs linked from the test report.",
            ]
        if validation.status == "timed_out":
            return [
                "Wait for GitHub validation to complete or inspect why the checks stalled.",
                "Do not approve merge until the test report shows passed validation.",
            ]
        return [
            "Wait for GitHub validation to complete before merge approval.",
            "Inspect the linked check runs if validation does not finish normally.",
        ]

    def _review_confidence(self, validation: PullRequestValidationResult) -> str:
        if validation.status == "passed":
            return "ci-passed-manual-review-required"
        if validation.status == "failed":
            return "ci-failed-review-required"
        return "ci-incomplete-review-required"

    def _review_recommendation(self, validation: PullRequestValidationResult) -> str:
        if validation.status == "passed":
            return "Review the draft PR and merge manually after approval."
        if validation.status == "failed":
            return "Do not merge until the failing GitHub validation checks are fixed."
        return "Do not merge until GitHub validation completes successfully."

    def _scope(self, connection: Connection, project_id: str) -> dict:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, p.name AS project_name, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Unknown project_id: {project_id}")
        return dict(row)

    def _route_model_execution(
        self,
        connection: Connection,
        project_id: str,
        *,
        task_id: Optional[str],
        task_kind: str,
        preferred_capabilities: list[str],
    ) -> dict:
        route = self.entitlements.preview_route(
            connection,
            project_id,
            skill_id="model.generate_text",
            task_kind=task_kind,
            workload_class="owner_sandbox",
            risk_tier=1,
            preferred_capabilities=preferred_capabilities,
            actor="coding_worker",
        )
        if route.get("source_id"):
            return route

        rejected = route.get("rejected_sources") or []
        missing_openai = any(
            item.get("source_id") == "openai_api"
            and item.get("reason") == "missing_credentials"
            for item in rejected
        )
        if missing_openai:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "OpenAI integration is not configured",
                {"action": "provide_openai_api_key"},
                {"routing_log_id": route["route_decision_log_id"]},
                "missing_openai_credentials",
            )
            return {
                "blocked": {
                    "status": "blocked",
                    "decision_id": decision_id,
                    "message": "OPENAI_API_KEY is not configured.",
                }
            }

        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            "No eligible model entitlement source is available",
            {"action": "review_entitlement_sources"},
            {"routing_log_id": route["route_decision_log_id"]},
            "no_eligible_model_source",
        )
        return {
            "blocked": {
                "status": "blocked",
                "decision_id": decision_id,
                "message": route.get("denial_reason")
                or "No eligible model entitlement source was available.",
            }
        }

    def create_plan_artifact(
        self,
        connection: Connection,
        project_id: str,
        reservation_id: str,
        dedupe_key: str,
        prompt: str,
        system_prompt: Optional[str],
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        reservation = self.budget.assert_active_reservation(
            connection, project_id, reservation_id
        )
        if not reservation:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "Missing active reservation for OpenAI call",
                {"action": "create_budget_reservation"},
                {},
                "missing_reservation",
            )
            return {"status": "blocked", "decision_id": decision_id}

        route = self._route_model_execution(
            connection,
            project_id,
            task_id=None,
            task_kind="planning",
            preferred_capabilities=["planning", "summarization"],
        )
        if route.get("blocked"):
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because no eligible model entitlement source was available.",
            )
            return route["blocked"]

        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "openai.responses.create",
            {
                "model": route["model_id"],
                "source_id": route["source_id"],
                "routing_log_id": route["route_decision_log_id"],
            },
            {"prompt": prompt, "system_prompt": system_prompt},
            created_by_job_id=created_by_job_id,
        )
        if not created and receipt["status"] == "succeeded":
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the effect was already satisfied by a prior receipt.",
            )
            return {
                "status": "reused",
                "artifact_id": receipt["result"].get("artifact_id"),
                "receipt_id": receipt["id"],
                "output_text": receipt["result"].get("output_text"),
            }

        try:
            provider_ref, output_text = self.openai.create_text_response(
                prompt, system_prompt, route["model_id"]
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because OpenAI credentials are not configured.",
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                None,
                "OpenAI integration is not configured",
                {"action": "provide_openai_api_key"},
                {},
                "missing_openai_credentials",
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(
                connection, receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the OpenAI plan call failed.",
            )
            emit_event(
                connection,
                project_id,
                "software_factory.plan_failed",
                "coding_worker",
                "receipt",
                receipt["id"],
                {"message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": receipt["id"],
                "message": str(error),
            }

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
            "plan_draft",
            "OpenAI plan draft",
            {"prompt": prompt, "output_text": output_text},
            created_by_job_id=created_by_job_id,
        )
        self.receipts.mark_succeeded(
            connection,
            receipt["id"],
            provider_ref,
            {"artifact_id": artifact_id, "output_text": output_text},
        )
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            receipt["id"],
            "model",
            route["model_id"],
            int(reservation["amount_usd_micros"]),
            None,
        )
        emit_event(
            connection,
            project_id,
            "software_factory.plan_created",
            "coding_worker",
            "artifact",
            artifact_id,
            {
                "receipt_id": receipt["id"],
                "routing_log_id": route["route_decision_log_id"],
                "source_id": route["source_id"],
                "model_id": route["model_id"],
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": receipt["id"],
            "output_text": output_text,
        }

    def _mark_merge_completed(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        actor: str,
        *,
        title: str,
        pull_request_url: str,
        branch_name: Optional[str],
        merged_sha: str,
    ) -> Optional[str]:
        self._mark_decision_executed(connection, project_id, decision_id, actor)

        if self.settings.enable_sandbox_deploy:
            return self._ensure_deployment_review_decision(
                connection,
                project_id,
                task_id,
                title=title,
                pull_request_url=pull_request_url,
                branch_name=branch_name,
                merged_sha=merged_sha,
                actor=actor,
            )

        self._complete_task(connection, project_id, task_id, actor, decision_id)
        return None

    def _mark_decision_executed(
        self, connection: Connection, project_id: str, decision_id: str, actor: str
    ) -> None:
        updated = connection.execute(
            text(
                """
                UPDATE decisions
                SET status = 'executed',
                    executed_by = :actor,
                    executed_at = now(),
                    updated_at = now()
                WHERE id = :decision_id AND status = 'approved'
                """
            ),
            {"decision_id": decision_id, "actor": actor},
        )
        if updated.rowcount == 1:
            emit_event(
                connection,
                project_id,
                "decision.executed",
                actor,
                "decision",
                decision_id,
                {},
            )

    def _complete_task(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        actor: str,
        decision_id: str,
    ) -> None:
        if not task_id:
            return
        task_updated = connection.execute(
            text(
                """
                UPDATE tasks
                SET status = 'completed',
                    updated_at = now()
                WHERE id = :task_id AND status <> 'completed'
                """
            ),
            {"task_id": task_id},
        )
        if task_updated.rowcount == 1:
            emit_event(
                connection,
                project_id,
                "task.completed",
                actor,
                "task",
                task_id,
                {"decision_id": decision_id},
            )

    def _ensure_deployment_review_decision(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        *,
        title: str,
        pull_request_url: str,
        branch_name: Optional[str],
        merged_sha: str,
        actor: str,
    ) -> str:
        existing_id = None
        if task_id:
            existing_id = connection.execute(
                text(
                    """
                    SELECT id
                    FROM decisions
                    WHERE project_id = :project_id
                      AND task_id = :task_id
                      AND recommendation->>'action' = 'review_deployment'
                      AND recommendation->>'pull_request_url' = :pull_request_url
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "pull_request_url": pull_request_url,
                },
            ).scalar_one_or_none()
        if existing_id:
            return str(existing_id)

        environment = self.settings.sandbox_deploy_environment
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Approve sandbox deployment to {environment}: {title}",
            {
                "action": "review_deployment",
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "title": title,
                "merged_sha": merged_sha,
                "environment": environment,
            },
            {},
            "deploy_decision_required",
        )
        emit_event(
            connection,
            project_id,
            "software_factory.deployment_review_requested",
            actor,
            "decision",
            decision_id,
            {
                "pull_request_url": pull_request_url,
                "environment": environment,
                "merged_sha": merged_sha,
            },
        )
        return decision_id

    def _mark_deployment_completed(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        actor: str,
    ) -> None:
        self._mark_decision_executed(connection, project_id, decision_id, actor)
        self._complete_task(connection, project_id, task_id, actor, decision_id)

    def _resolve_deployment_healthcheck_url(self) -> Optional[str]:
        configured = self.settings.sandbox_deploy_healthcheck_url
        if not configured:
            return None
        candidate = configured.strip()
        return candidate or None

    def _verify_deployment_health(self, healthcheck_url: str) -> dict:
        try:
            response = httpx.get(
                healthcheck_url,
                follow_redirects=True,
                timeout=self.settings.sandbox_deploy_healthcheck_timeout_seconds,
            )
        except Exception as error:
            return {
                "status": "failed",
                "healthcheck_url": healthcheck_url,
                "summary": f"Deployment health check failed: {error}",
                "http_status": None,
            }

        excerpt = response.text[:200].strip() if response.text else ""
        if 200 <= response.status_code < 400:
            return {
                "status": "passed",
                "healthcheck_url": healthcheck_url,
                "summary": f"Deployment health check passed with HTTP {response.status_code}.",
                "http_status": response.status_code,
                "response_excerpt": excerpt,
            }
        return {
            "status": "failed",
            "healthcheck_url": healthcheck_url,
            "summary": (
                f"Deployment health check returned HTTP {response.status_code}."
            ),
            "http_status": response.status_code,
            "response_excerpt": excerpt,
        }

    def _create_deployment_health_gate(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        *,
        title: str,
        environment: str,
        pull_request_url: str,
        branch_name: Optional[str],
        merged_sha: str,
        receipt_id: str,
        deployment_id: int,
        deployment_status_url: str,
        verification: dict,
        created_by_job_id: Optional[str] = None,
    ) -> tuple[str, str]:
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "deployment_health_check",
            f"Deployment health check for {title}",
            {
                "title": title,
                "environment": environment,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "merged_sha": merged_sha,
                "receipt_id": receipt_id,
                "deployment_id": deployment_id,
                "deployment_status_url": deployment_status_url,
                "verification": verification,
            },
            created_by_job_id=created_by_job_id,
        )
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Review failed sandbox deployment health check: {title}",
            {
                "action": "review_deployment_health",
                "artifact_id": artifact_id,
                "receipt_id": receipt_id,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "merged_sha": merged_sha,
                "environment": environment,
                "healthcheck_url": verification.get("healthcheck_url"),
                "deployment_status_url": deployment_status_url,
            },
            {},
            "deployment_health_verification_failed",
        )
        emit_event(
            connection,
            project_id,
            "software_factory.deployment_health_blocked",
            "release_manager",
            "decision",
            decision_id,
            {
                "receipt_id": receipt_id,
                "artifact_id": artifact_id,
                "environment": environment,
                "healthcheck_url": verification.get("healthcheck_url"),
                "summary": verification.get("summary"),
            },
        )
        return artifact_id, decision_id

    def propose_pull_request(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        reservation_id: str,
        dedupe_key: str,
        title: str,
        branch_name: str,
        summary: str,
        files: list[FileChange],
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        reservation = self.budget.assert_active_reservation(
            connection, project_id, reservation_id
        )
        if not reservation:
            decision_id = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "Missing active reservation for PR proposal",
                {"action": "create_budget_reservation"},
                {},
                "missing_reservation",
            )
            return {"status": "blocked", "decision_id": decision_id}

        route = self._route_model_execution(
            connection,
            project_id,
            task_id=task_id,
            task_kind="review",
            preferred_capabilities=["review", "coding", "summarization"],
        )
        if route.get("blocked"):
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because no eligible model entitlement source was available.",
            )
            return route["blocked"]

        scope = self._scope(connection, project_id)
        pr_receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "github.pull_request.create",
            {"repository": self.github.repository_full_name},
            {
                "title": title,
                "branch_name": branch_name,
                "summary": summary,
                "files": [file.model_dump() for file in files],
            },
            created_by_job_id=created_by_job_id,
        )
        if not created and pr_receipt["status"] == "succeeded":
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the PR effect was already satisfied by a prior receipt.",
            )
            result = pr_receipt["result"] or {}
            return {
                "status": "reused",
                "artifact_id": result.get("artifact_id"),
                "diff_artifact_id": result.get("diff_artifact_id"),
                "test_report_artifact_id": result.get("test_report_artifact_id"),
                "receipt_id": pr_receipt["id"],
                "decision_id": result.get("decision_id"),
                "pull_request_url": result.get("pull_request_url"),
                "pull_request_number": result.get("pull_request_number"),
                "branch_name": result.get("branch_name"),
                "validation_status": result.get("validation_status"),
                "validation_summary": result.get("validation_summary"),
            }

        try:
            review_provider_ref, review_text = self.openai.create_text_response(
                (
                    "Create a review packet for a BOSS software-factory proposal.\n\n"
                    f"Title: {title}\n"
                    f"Summary: {summary}\n"
                    f"Files: {json.dumps([file.model_dump() for file in files], indent=2)}"
                ),
                (
                    "Return concise markdown with these sections: implementation summary, "
                    "tests added or still needed, risk hotspots, rollback plan, confidence, "
                    "and recommendation."
                ),
                route["model_id"],
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, pr_receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because OpenAI credentials are not configured.",
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "OpenAI integration is required before PR proposals can run",
                {"action": "provide_openai_api_key"},
                {},
                "missing_openai_credentials",
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(
                connection, pr_receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because the review-packet generation failed.",
            )
            emit_event(
                connection,
                project_id,
                "software_factory.pull_request_failed",
                "coding_worker",
                "receipt",
                pr_receipt["id"],
                {"message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": pr_receipt["id"],
                "message": str(error),
            }

        try:
            self.github.ensure_branch(branch_name)
            commit_shas = [
                self.github.upsert_file(
                    branch_name,
                    file.path,
                    file.content,
                    f"BOSS proposal update: {file.path}",
                )
                for file in files
            ]
            pr = self.github.create_pull_request(
                branch_name,
                title,
                "\n\n".join(
                    [
                        summary,
                        "## Review packet",
                        review_text,
                    ]
                ),
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, pr_receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because GitHub credentials are not configured.",
            )
            decision_id = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "GitHub integration is not configured",
                {"action": "provide_github_token"},
                {},
                "missing_github_credentials",
            )
            return {
                "status": "blocked",
                "decision_id": decision_id,
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(
                connection, pr_receipt["id"], {"message": str(error)}
            )
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because sandbox PR creation failed.",
            )
            emit_event(
                connection,
                project_id,
                "software_factory.pull_request_failed",
                "coding_worker",
                "receipt",
                pr_receipt["id"],
                {"message": str(error)},
            )
            return {
                "status": "failed",
                "receipt_id": pr_receipt["id"],
                "message": str(error),
            }

        branch_head_sha = commit_shas[-1] if commit_shas else self.github.ensure_branch(
            branch_name
        )
        try:
            validation = self.github.wait_for_commit_checks(
                branch_head_sha,
                timeout_seconds=self.settings.sandbox_ci_timeout_seconds,
                poll_interval_seconds=self.settings.sandbox_ci_poll_interval_seconds,
            )
        except Exception as error:
            validation = PullRequestValidationResult(
                status="unavailable",
                summary=f"Unable to capture GitHub validation evidence: {error}",
                head_sha=branch_head_sha,
                completed=False,
                checks=[],
            )
            emit_event(
                connection,
                project_id,
                "software_factory.validation_unavailable",
                "coding_worker",
                "receipt",
                pr_receipt["id"],
                {
                    "pull_request_url": pr.url,
                    "message": str(error),
                },
            )

        validation_checks = [
            {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
                "html_url": check.html_url,
                "started_at": check.started_at,
                "completed_at": check.completed_at,
            }
            for check in validation.checks
        ]
        tests_added_or_run = [check["name"] for check in validation_checks]
        validation_follow_up = self._validation_follow_up(validation)

        diff_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "diff_manifest",
            f"Diff manifest for {title}",
            {
                "title": title,
                "branch_name": branch_name,
                "files_changed": [file.path for file in files],
                "github_commits": commit_shas,
            },
            created_by_job_id=created_by_job_id,
        )
        test_report_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "test_report",
            f"Test report for {title}",
            {
                "title": title,
                "status": validation.status,
                "summary": validation.summary,
                "head_sha": validation.head_sha,
                "validation_source": "github.check_runs",
                "tests_added_or_run": tests_added_or_run,
                "checks": validation_checks,
                "required_follow_up": validation_follow_up,
                "notes": review_text,
            },
            created_by_job_id=created_by_job_id,
        )
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "review_packet",
            f"Review packet for {title}",
            {
                "title": title,
                "summary": summary,
                "files_changed": [file.path for file in files],
                "tests_added_or_run": tests_added_or_run,
                "ci_validation_status": validation.status,
                "ci_validation_summary": validation.summary,
                "ci_checks": validation_checks,
                "risk_hotspots": review_text,
                "rollback_plan": "Revert the sandbox PR branch or close the PR.",
                "confidence_score": self._review_confidence(validation),
                "recommendation": self._review_recommendation(validation),
                "github_commits": commit_shas,
                "openai_response_id": review_provider_ref,
                "routing_log_id": route["route_decision_log_id"],
            },
            created_by_job_id=created_by_job_id,
        )
        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            f"Review and approve sandbox PR: {title}",
            {
                "action": "review_pull_request",
                "artifact_id": artifact_id,
                "diff_artifact_id": diff_artifact_id,
                "test_report_artifact_id": test_report_artifact_id,
                "receipt_id": pr_receipt["id"],
                "pull_request_url": pr.url,
                "branch_name": branch_name,
                "title": title,
                "validation_status": validation.status,
                "validation_summary": validation.summary,
            },
            {},
            "merge_decision_required",
        )
        result = {
            "artifact_id": artifact_id,
            "diff_artifact_id": diff_artifact_id,
            "test_report_artifact_id": test_report_artifact_id,
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch_name": branch_name,
            "validation_status": validation.status,
            "validation_summary": validation.summary,
        }
        self.receipts.mark_succeeded(connection, pr_receipt["id"], pr.url, result)
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            pr_receipt["id"],
            "tool",
            "github.pull_request.create",
            int(reservation["amount_usd_micros"]),
            None,
        )
        emit_event(
            connection,
            project_id,
            "software_factory.pull_request_proposed",
            "coding_worker",
            "decision",
            decision_id,
            {
                "receipt_id": pr_receipt["id"],
                "pull_request_url": pr.url,
                "routing_log_id": route["route_decision_log_id"],
                "source_id": route["source_id"],
                "model_id": route["model_id"],
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "diff_artifact_id": diff_artifact_id,
            "test_report_artifact_id": test_report_artifact_id,
            "receipt_id": pr_receipt["id"],
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch_name": branch_name,
            "validation_status": validation.status,
            "validation_summary": validation.summary,
        }

    def merge_pull_request(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        dedupe_key: str,
        pull_request_number: int,
        pull_request_url: str,
        title: str,
        branch_name: Optional[str],
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        if task_id is None:
            task_id = connection.execute(
                text(
                    """
                    SELECT task_id
                    FROM decisions
                    WHERE id = :decision_id AND project_id = :project_id
                    """
                ),
                {"decision_id": decision_id, "project_id": project_id},
            ).scalar_one_or_none()

        scope = self._scope(connection, project_id)
        merge_receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "github.pull_request.merge",
            {
                "repository": self.github.repository_full_name,
                "pull_request_number": pull_request_number,
            },
            {
                "decision_id": decision_id,
                "title": title,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
            },
            created_by_job_id=created_by_job_id,
        )
        if not created and merge_receipt["status"] == "succeeded":
            follow_on_decision_id = self._mark_merge_completed(
                connection,
                project_id,
                task_id,
                decision_id,
                "release_manager",
                title=title,
                pull_request_url=pull_request_url,
                branch_name=branch_name,
                merged_sha=(merge_receipt.get("result") or {}).get("merged_sha") or "",
            )
            result = merge_receipt["result"] or {}
            return {
                "status": "reused",
                "artifact_id": result.get("artifact_id"),
                "receipt_id": merge_receipt["id"],
                "decision_id": decision_id,
                "deployment_decision_id": result.get("deployment_decision_id")
                or follow_on_decision_id,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "merged_sha": result.get("merged_sha"),
            }

        try:
            merge = self.github.merge_pull_request(pull_request_number, title)
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, merge_receipt["id"], {"message": str(error)}
            )
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "GitHub integration is not configured",
                {"action": "provide_github_token"},
                {},
                "missing_github_credentials",
            )
            return {
                "status": "blocked",
                "decision_id": decision_id_block,
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(
                connection, merge_receipt["id"], {"message": str(error)}
            )
            emit_event(
                connection,
                project_id,
                "software_factory.pull_request_merge_failed",
                "release_manager",
                "receipt",
                merge_receipt["id"],
                {"message": str(error), "pull_request_url": pull_request_url},
            )
            return {
                "status": "failed",
                "receipt_id": merge_receipt["id"],
                "message": str(error),
            }

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "merge_result",
            f"Merge result for {title}",
            {
                "decision_id": decision_id,
                "pull_request_number": pull_request_number,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "merged_sha": merge.sha,
                "merged": merge.merged,
                "message": merge.message,
                "merge_method": self.settings.sandbox_merge_method,
            },
            created_by_job_id=created_by_job_id,
        )
        result = {
            "artifact_id": artifact_id,
            "merged_sha": merge.sha,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
        }
        follow_on_decision_id = self._mark_merge_completed(
            connection,
            project_id,
            task_id,
            decision_id,
            "release_manager",
            title=title,
            pull_request_url=pull_request_url,
            branch_name=branch_name,
            merged_sha=merge.sha,
        )
        if follow_on_decision_id:
            result["deployment_decision_id"] = follow_on_decision_id
        self.receipts.mark_succeeded(
            connection,
            merge_receipt["id"],
            pull_request_url,
            result,
        )
        emit_event(
            connection,
            project_id,
            "software_factory.pull_request_merged",
            "release_manager",
            "artifact",
            artifact_id,
            {
                "receipt_id": merge_receipt["id"],
                "pull_request_url": pull_request_url,
                "merged_sha": merge.sha,
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": merge_receipt["id"],
            "decision_id": decision_id,
            "deployment_decision_id": follow_on_decision_id,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
            "merged_sha": merge.sha,
        }

    def create_deployment(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        decision_id: str,
        dedupe_key: str,
        merged_sha: str,
        pull_request_url: str,
        title: str,
        branch_name: Optional[str],
        environment: str,
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        if task_id is None:
            task_id = connection.execute(
                text(
                    """
                    SELECT task_id
                    FROM decisions
                    WHERE id = :decision_id AND project_id = :project_id
                    """
                ),
                {"decision_id": decision_id, "project_id": project_id},
            ).scalar_one_or_none()

        scope = self._scope(connection, project_id)
        deployment_receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "github.deployment.create",
            {
                "repository": self.github.repository_full_name,
                "environment": environment,
                "sha": merged_sha,
            },
            {
                "decision_id": decision_id,
                "pull_request_url": pull_request_url,
                "title": title,
                "branch_name": branch_name,
                "environment": environment,
                "merged_sha": merged_sha,
            },
            created_by_job_id=created_by_job_id,
        )
        if not created and deployment_receipt["status"] == "succeeded":
            result = deployment_receipt["result"] or {}
            health_status = result.get("health_verification_status")
            if health_status in {None, "passed", "skipped_unconfigured"}:
                self._mark_deployment_completed(
                    connection,
                    project_id,
                    task_id,
                    decision_id,
                    "release_manager",
                )
            return {
                "status": (
                    "reused"
                    if health_status in {None, "passed", "skipped_unconfigured"}
                    else "blocked"
                ),
                "artifact_id": result.get("artifact_id"),
                "receipt_id": deployment_receipt["id"],
                "decision_id": decision_id,
                "health_decision_id": result.get("health_decision_id"),
                "health_verification_artifact_id": result.get(
                    "health_verification_artifact_id"
                ),
                "health_verification_status": health_status,
                "healthcheck_url": result.get("healthcheck_url"),
                "environment": result.get("environment"),
                "deployment_id": result.get("deployment_id"),
                "deployment_status": result.get("deployment_status"),
                "deployment_url": result.get("deployment_url"),
                "merged_sha": result.get("merged_sha"),
            }

        try:
            deployment = self.github.create_deployment(
                merged_sha,
                environment,
                f"BOSS sandbox deployment for {title}",
            )
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, deployment_receipt["id"], {"message": str(error)}
            )
            decision_id_block = create_blocking_decision(
                connection,
                project_id,
                task_id,
                "GitHub integration is not configured",
                {"action": "provide_github_token"},
                {},
                "missing_github_credentials",
            )
            return {
                "status": "blocked",
                "decision_id": decision_id_block,
                "message": str(error),
            }
        except Exception as error:
            self.receipts.mark_failed(
                connection, deployment_receipt["id"], {"message": str(error)}
            )
            emit_event(
                connection,
                project_id,
                "software_factory.deployment_failed",
                "release_manager",
                "receipt",
                deployment_receipt["id"],
                {"message": str(error), "environment": environment},
            )
            return {
                "status": "failed",
                "receipt_id": deployment_receipt["id"],
                "message": str(error),
            }

        try:
            healthcheck_url = self._resolve_deployment_healthcheck_url()
            verification: Optional[dict] = None
            health_verification_artifact_id: Optional[str] = None
            health_decision_id: Optional[str] = None
            if healthcheck_url:
                verification = self._verify_deployment_health(healthcheck_url)
                if verification["status"] == "passed":
                    deployment_status = self.github.mark_deployment_success(
                        deployment.id,
                        environment,
                        environment_url=healthcheck_url,
                        description=(
                            f"BOSS recorded sandbox deployment for {title} after "
                            "health verification passed"
                        ),
                    )
                else:
                    deployment_status = self.github.mark_deployment_failure(
                        deployment.id,
                        environment,
                        environment_url=healthcheck_url,
                        description=(
                            f"BOSS recorded sandbox deployment for {title}, but "
                            "post-deploy health verification failed"
                        ),
                    )
            else:
                deployment_status = self.github.mark_deployment_success(
                    deployment.id,
                    environment,
                    description=f"BOSS recorded sandbox deployment for {title}",
                )
                verification = {
                    "status": "skipped_unconfigured",
                    "healthcheck_url": None,
                    "summary": (
                        "Deployment health verification was skipped because "
                        "BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL is not configured."
                    ),
                    "http_status": None,
                }
        except Exception as error:
            self.receipts.mark_failed(
                connection, deployment_receipt["id"], {"message": str(error)}
            )
            emit_event(
                connection,
                project_id,
                "software_factory.deployment_failed",
                "release_manager",
                "receipt",
                deployment_receipt["id"],
                {"message": str(error), "environment": environment},
            )
            return {
                "status": "failed",
                "receipt_id": deployment_receipt["id"],
                "message": str(error),
            }

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "deployment_result",
            f"Deployment result for {title}",
            {
                "decision_id": decision_id,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "environment": environment,
                "merged_sha": merged_sha,
                "deployment_id": deployment.id,
                "deployment_api_url": deployment.url,
                "deployment_status": deployment_status.state,
                "deployment_status_url": deployment_status.url,
                "environment_url": deployment_status.environment_url,
                "health_verification_status": verification["status"],
                "health_verification_summary": verification["summary"],
                "healthcheck_url": verification.get("healthcheck_url"),
                "health_http_status": verification.get("http_status"),
                "health_response_excerpt": verification.get("response_excerpt"),
            },
            created_by_job_id=created_by_job_id,
        )
        if verification["status"] == "failed":
            (
                health_verification_artifact_id,
                health_decision_id,
            ) = self._create_deployment_health_gate(
                connection,
                project_id,
                task_id,
                title=title,
                environment=environment,
                pull_request_url=pull_request_url,
                branch_name=branch_name,
                merged_sha=merged_sha,
                receipt_id=deployment_receipt["id"],
                deployment_id=deployment.id,
                deployment_status_url=deployment_status.url,
                verification=verification,
                created_by_job_id=created_by_job_id,
            )
        result = {
            "artifact_id": artifact_id,
            "environment": environment,
            "deployment_id": deployment.id,
            "deployment_status": deployment_status.state,
            "deployment_url": deployment_status.url,
            "merged_sha": merged_sha,
            "health_verification_status": verification["status"],
            "healthcheck_url": verification.get("healthcheck_url"),
            "health_verification_artifact_id": health_verification_artifact_id,
            "health_decision_id": health_decision_id,
        }
        self.receipts.mark_succeeded(
            connection,
            deployment_receipt["id"],
            deployment_status.url,
            result,
        )
        if verification["status"] in {"passed", "skipped_unconfigured"}:
            self._mark_deployment_completed(
                connection,
                project_id,
                task_id,
                decision_id,
                "release_manager",
            )
            emit_event(
                connection,
                project_id,
                "software_factory.deployment_created",
                "release_manager",
                "artifact",
                artifact_id,
                {
                    "receipt_id": deployment_receipt["id"],
                    "environment": environment,
                    "deployment_id": deployment.id,
                    "merged_sha": merged_sha,
                    "health_verification_status": verification["status"],
                },
            )
        return {
            "status": (
                "created"
                if verification["status"] in {"passed", "skipped_unconfigured"}
                else "blocked"
            ),
            "artifact_id": artifact_id,
            "receipt_id": deployment_receipt["id"],
            "decision_id": decision_id,
            "health_decision_id": health_decision_id,
            "health_verification_artifact_id": health_verification_artifact_id,
            "health_verification_status": verification["status"],
            "healthcheck_url": verification.get("healthcheck_url"),
            "environment": environment,
            "deployment_id": deployment.id,
            "deployment_status": deployment_status.state,
            "deployment_url": deployment_status.url,
            "merged_sha": merged_sha,
        }
