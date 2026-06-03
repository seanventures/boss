from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..schemas import FileChange
from .budget import BudgetGovernor
from .core import create_artifact, create_blocking_decision, emit_event
from .entitlements import EntitlementBrokerService
from .github_adapter import GitHubAdapter, IntegrationConfigurationError
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
                "status": "not_run",
                "tests_added_or_run": [],
                "required_follow_up": [
                    "Execute repository CI in the sandbox before merge approval.",
                    "Add or update focused tests if the reviewer flags gaps.",
                ],
                "notes": review_text,
            },
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
                "tests_added_or_run": [],
                "risk_hotspots": review_text,
                "rollback_plan": "Revert the sandbox PR branch or close the PR.",
                "confidence_score": "manual-review-required",
                "recommendation": "Review the draft PR and merge manually after approval.",
                "github_commits": commit_shas,
                "openai_response_id": review_provider_ref,
                "routing_log_id": route["route_decision_log_id"],
            },
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
            "receipt_id": pr_receipt["id"],
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch_name": branch_name,
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
        )
        if not created and merge_receipt["status"] == "succeeded":
            self._mark_merge_completed(
                connection,
                project_id,
                task_id,
                decision_id,
                "release_manager",
            )
            result = merge_receipt["result"] or {}
            return {
                "status": "reused",
                "artifact_id": result.get("artifact_id"),
                "receipt_id": merge_receipt["id"],
                "decision_id": decision_id,
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
        )
        result = {
            "artifact_id": artifact_id,
            "merged_sha": merge.sha,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
        }
        self.receipts.mark_succeeded(
            connection,
            merge_receipt["id"],
            pull_request_url,
            result,
        )
        self._mark_merge_completed(
            connection,
            project_id,
            task_id,
            decision_id,
            "release_manager",
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
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
            "merged_sha": merge.sha,
        }
