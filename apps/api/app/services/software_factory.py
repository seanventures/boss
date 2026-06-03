from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..schemas import FileChange
from .budget import BudgetGovernor
from .core import create_artifact, create_blocking_decision, emit_event
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

        scope = self._scope(connection, project_id)
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            "openai.responses.create",
            {"model": self.settings.openai_model},
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
                prompt, system_prompt
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
            self.settings.openai_model,
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
            {"receipt_id": receipt["id"]},
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": receipt["id"],
            "output_text": output_text,
        }

    def propose_pull_request(
        self,
        connection: Connection,
        project_id: str,
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
                None,
                "Missing active reservation for PR proposal",
                {"action": "create_budget_reservation"},
                {},
                "missing_reservation",
            )
            return {"status": "blocked", "decision_id": decision_id}

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
                "receipt_id": pr_receipt["id"],
                "decision_id": result.get("decision_id"),
                "pull_request_url": result.get("pull_request_url"),
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
                None,
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
                None,
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

        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            None,
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
            },
        )
        decision_id = create_blocking_decision(
            connection,
            project_id,
            None,
            f"Review and approve sandbox PR: {title}",
            {"action": "review_pull_request"},
            {},
            "merge_decision_required",
        )
        result = {
            "artifact_id": artifact_id,
            "decision_id": decision_id,
            "pull_request_url": pr.url,
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
            {"receipt_id": pr_receipt["id"], "pull_request_url": pr.url},
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": pr_receipt["id"],
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "branch_name": branch_name,
        }
