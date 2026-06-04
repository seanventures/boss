from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..schemas import FileChange
from .budget import BudgetGovernor
from .core import (
    create_artifact,
    create_blocking_decision,
    emit_event,
    make_id,
    normalize_data_boundary,
)
from .entitlements import EntitlementBrokerService
from .github_adapter import (
    GitHubAdapter,
    IntegrationConfigurationError,
    PullRequestValidationResult,
)
from .openai_adapter import OpenAIAdapter
from .receipts import ReceiptService
from .sandbox_validation import (
    SandboxValidationAdapter,
    SandboxValidationConfigurationError,
)
from .specpack import SpecPackService


class SoftwareFactoryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = BudgetGovernor(settings)
        self.receipts = ReceiptService()
        self.github = GitHubAdapter(settings)
        self.openai = OpenAIAdapter(settings)
        self.entitlements = EntitlementBrokerService(settings)
        self.specpack = SpecPackService(settings)
        self.sandbox_validation = SandboxValidationAdapter(settings)

    def _validation_follow_up(
        self, validation: PullRequestValidationResult
    ) -> list[str]:
        return self._validation_follow_up_for_status(validation.status)

    def _validation_follow_up_for_status(self, status: str) -> list[str]:
        if status == "passed":
            return [
                "Review the diff and approval context before merge.",
                "Proceed to merge approval only if the review packet also looks correct.",
            ]
        if status == "failed":
            return [
                "Fix the failing sandbox validation before merge approval.",
                "Review the failing validation evidence linked from the test report.",
            ]
        if status == "timed_out":
            return [
                "Wait for GitHub validation to complete or inspect why the checks stalled.",
                "Do not approve merge until the test report shows passed validation.",
            ]
        return [
            "Wait for GitHub validation to complete before merge approval.",
            "Inspect the linked check runs if validation does not finish normally.",
        ]

    def _review_confidence(self, validation: PullRequestValidationResult) -> str:
        return self._review_confidence_for_status(validation.status)

    def _review_confidence_for_status(self, status: str) -> str:
        if status == "passed":
            return "ci-passed-manual-review-required"
        if status == "failed":
            return "ci-failed-review-required"
        return "ci-incomplete-review-required"

    def _review_recommendation(self, validation: PullRequestValidationResult) -> str:
        return self._review_recommendation_for_status(validation.status)

    def _review_recommendation_for_status(self, status: str) -> str:
        if status == "passed":
            return "Review the draft PR and merge manually after approval."
        if status == "failed":
            return "Do not merge until the failing sandbox validation evidence is fixed."
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

    def _data_boundary_for_task(
        self, connection: Connection, task_id: Optional[str]
    ) -> str:
        if not task_id:
            return "internal"
        row = connection.execute(
            text("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        return normalize_data_boundary(row["data_boundary"] if row else None)

    def _block_github_effect_for_data_boundary(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        effect_kind: str,
        actor: str,
    ) -> Optional[dict]:
        boundary = self._data_boundary_for_task(connection, task_id)
        if boundary not in {"restricted", "local-only"}:
            return None

        decision_id = create_blocking_decision(
            connection,
            project_id,
            task_id,
            "Data boundary blocks GitHub external effect",
            {
                "action": "reclassify_or_configure_approved_boundary_adapter",
                "effect_kind": effect_kind,
                "data_boundary": boundary,
            },
            {},
            "data_boundary_blocks_external_effect",
        )
        emit_event(
            connection,
            project_id,
            "policy.denied",
            actor,
            "github_effect",
            effect_kind,
            {
                "effect_kind": effect_kind,
                "data_boundary": boundary,
                "decision_id": decision_id,
                "reason": "data_boundary_not_allowed_for_github",
            },
        )
        return {
            "status": "blocked",
            "decision_id": decision_id,
            "data_boundary": boundary,
            "message": (
                f"{boundary} data cannot be sent to GitHub without an approved "
                "boundary adapter."
            ),
        }

    def _route_model_execution(
        self,
        connection: Connection,
        project_id: str,
        *,
        task_id: Optional[str],
        task_kind: str,
        preferred_capabilities: list[str],
    ) -> dict:
        data_boundary = self._data_boundary_for_task(connection, task_id)
        route = self.entitlements.preview_route(
            connection,
            project_id,
            skill_id="model.generate_text",
            task_kind=task_kind,
            workload_class="owner_sandbox",
            risk_tier=1,
            data_boundary=data_boundary,
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

    def _resolve_agent_module(
        self,
        connection: Connection,
        project_id: str,
        *,
        requested_module_id: Optional[str],
        job_kind: str,
    ) -> dict | None:
        if requested_module_id:
            return self.specpack.resolve_installed_agent_module(
                connection, project_id, requested_module_id
            )
        return self.specpack.find_default_agent_module_for_job_kind(
            connection, project_id, job_kind
        )

    def _module_prompt(
        self, module_spec: dict | None, bundle_key: str
    ) -> Optional[str]:
        if not module_spec:
            return None
        prompt_bundle = module_spec.get("prompt_bundle") or {}
        value = prompt_bundle.get(bundle_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _module_preferred_capabilities(
        self, module_spec: dict | None, fallback: list[str]
    ) -> list[str]:
        if not module_spec:
            return fallback
        runtime = module_spec.get("runtime") or {}
        preferred = runtime.get("preferred_capabilities")
        if isinstance(preferred, list):
            cleaned = [item for item in preferred if isinstance(item, str) and item]
            if cleaned:
                return cleaned
        return fallback

    def _merge_system_prompts(self, *parts: Optional[str]) -> Optional[str]:
        merged = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
        if not merged:
            return None
        return "\n\n".join(merged)

    def _module_descriptor(self, module_spec: dict | None) -> dict:
        if not module_spec:
            return {}
        role = module_spec.get("role") or {}
        return {
            "id": module_spec.get("id"),
            "version": module_spec.get("version"),
            "display_name": module_spec.get("display_name"),
            "publisher": module_spec.get("publisher"),
            "category": module_spec.get("category"),
            "role_key": role.get("role_key") or role.get("role_id"),
            "config": module_spec.get("config") or {},
        }

    def _run_sandbox_validation(
        self,
        connection: Connection,
        scope: dict,
        project_id: str,
        task_id: Optional[str],
        dedupe_key: str,
        branch_name: str,
        created_by_job_id: Optional[str],
    ) -> tuple[Optional[dict], Optional[str], Optional[str]]:
        if not self.sandbox_validation.enabled:
            return None, None, None

        validation_dedupe_key = f"{dedupe_key}:sandbox-validation"
        commands = self.sandbox_validation.configured_commands()
        receipt, created = self.receipts.create_or_reuse(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            validation_dedupe_key,
            "sandbox.validation.run",
            {
                "repository": self.github.repository_full_name,
                "branch_name": branch_name,
                "repo_path": str(self.settings.sandbox_validation_repo_path or ""),
            },
            {"branch_name": branch_name, "commands": commands},
            created_by_job_id=created_by_job_id,
        )
        if not created and receipt["status"] == "succeeded":
            result = receipt["result"] or {}
            return result.get("validation"), receipt["id"], receipt["status"]

        try:
            result = self.sandbox_validation.run(branch_name)
            payload = self.sandbox_validation.to_payload(result)
        except SandboxValidationConfigurationError as error:
            payload = {
                "status": "failed",
                "summary": f"Sandbox local validation could not run: {error}",
                "repository_path": str(self.settings.sandbox_validation_repo_path or ""),
                "branch_name": branch_name,
                "head_sha": None,
                "completed": False,
                "commands": [],
            }

        if payload["status"] == "passed":
            self.receipts.mark_succeeded(
                connection,
                receipt["id"],
                f"local-sandbox:{branch_name}",
                {"validation": payload},
            )
            event_type = "software_factory.local_validation_passed"
            receipt_status = "succeeded"
        else:
            self.receipts.mark_failed(
                connection,
                receipt["id"],
                {"validation": payload},
            )
            event_type = "software_factory.local_validation_failed"
            receipt_status = "failed"

        emit_event(
            connection,
            project_id,
            event_type,
            "coding_worker",
            "receipt",
            receipt["id"],
            {
                "task_id": task_id,
                "branch_name": branch_name,
                "status": payload["status"],
                "summary": payload["summary"],
            },
        )
        return payload, receipt["id"], receipt_status

    def _combine_validation_evidence(
        self,
        github_validation: PullRequestValidationResult,
        local_validation: Optional[dict],
    ) -> tuple[str, str, list[str]]:
        sources = ["github.check_runs"]
        if local_validation is not None:
            sources.append("local.sandbox_commands")
            local_status = str(local_validation.get("status") or "failed")
            local_summary = str(
                local_validation.get("summary") or "Sandbox local validation failed."
            )
            if local_status != "passed":
                return "failed", local_summary, sources

        return github_validation.status, github_validation.summary, sources

    def _build_security_dependency_review(
        self,
        files: list[FileChange],
    ) -> dict:
        dependency_markers = {
            "package.json",
            "pnpm-lock.yaml",
            "package-lock.json",
            "yarn.lock",
            "requirements.txt",
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "github/workflows",
        }
        security_markers = {
            "auth",
            "oidc",
            "saml",
            "scim",
            "security",
            "policy",
            "permission",
            "secret",
            "token",
            "credential",
            ".env",
            "config",
        }
        secret_pattern = re.compile(
            r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"][^'\"\n]{8,}['\"]"
        )
        dependency_files: list[str] = []
        security_sensitive_files: list[str] = []
        findings: list[dict] = []
        for file in files:
            normalized = file.path.lower().lstrip("./")
            basename = normalized.rsplit("/", 1)[-1]
            if basename in dependency_markers or any(
                marker in normalized for marker in dependency_markers
            ):
                dependency_files.append(file.path)
            if any(marker in normalized for marker in security_markers):
                security_sensitive_files.append(file.path)
            matches = list(secret_pattern.finditer(file.content))
            if matches:
                findings.append(
                    {
                        "severity": "high",
                        "kind": "hardcoded_secret_candidate",
                        "path": file.path,
                        "message": (
                            "Potential hardcoded credential pattern detected; "
                            "replace with SecretRef/env-backed configuration before merge."
                        ),
                        "match_count": len(matches),
                    }
                )

        status = "passed"
        if findings:
            status = "failed"
        elif dependency_files or security_sensitive_files:
            status = "warning"

        required_follow_up = []
        if findings:
            required_follow_up.append("Remove hardcoded credential candidates before merge.")
        if dependency_files:
            required_follow_up.append(
                "Confirm dependency changes are intentional and covered by CI/security review."
            )
        if security_sensitive_files:
            required_follow_up.append(
                "Have a security-aware reviewer inspect sensitive auth/policy/config changes."
            )

        return {
            "status": status,
            "dependency_files": dependency_files,
            "security_sensitive_files": security_sensitive_files,
            "findings": findings,
            "required_follow_up": required_follow_up,
            "review_method": "deterministic_path_and_content_scan",
        }

    def _build_browser_validation_review(self, files: list[FileChange]) -> dict:
        browser_relevant_files = [
            file.path
            for file in files
            if file.path.startswith("apps/web/")
            or file.path.endswith((".tsx", ".jsx", ".css", ".scss"))
        ]
        if not browser_relevant_files:
            return {
                "status": "not_applicable",
                "browser_relevant_files": [],
                "evidence_refs": [],
                "required_follow_up": [],
            }
        return {
            "status": "required_not_run",
            "browser_relevant_files": browser_relevant_files,
            "evidence_refs": [],
            "required_follow_up": [
                "Capture browser validation evidence before approving a UI-affecting merge."
            ],
        }

    def _build_release_go_no_go(
        self,
        *,
        validation_status: str,
        validation_summary: str,
        security_dependency_review: dict,
        browser_validation: dict,
        evidence_refs: list[str],
    ) -> dict:
        blockers: list[str] = []
        warnings: list[str] = []
        if validation_status != "passed":
            blockers.append("ci_or_sandbox_validation_not_passed")
        if security_dependency_review["status"] == "failed":
            blockers.append("security_dependency_review_failed")
        if security_dependency_review["status"] == "warning":
            warnings.append("security_or_dependency_reviewer_required")
        if browser_validation["status"] == "required_not_run":
            warnings.append("browser_validation_required_before_merge")

        if blockers:
            status = "no_go"
            recommendation = "Hold merge approval until blockers are resolved."
        elif warnings:
            status = "conditional_go"
            recommendation = (
                "Proceed only after the listed follow-up evidence is attached."
            )
        else:
            status = "go"
            recommendation = "Ready for human merge review; no auto-merge permitted."

        return {
            "status": status,
            "blockers": blockers,
            "warnings": warnings,
            "validation_status": validation_status,
            "validation_summary": validation_summary,
            "security_dependency_status": security_dependency_review["status"],
            "browser_validation_status": browser_validation["status"],
            "evidence_refs": evidence_refs,
            "recommendation": recommendation,
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
        agent_module_id: Optional[str] = None,
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

        module_spec = self._resolve_agent_module(
            connection,
            project_id,
            requested_module_id=agent_module_id,
            job_kind="openai.plan",
        )
        route = self._route_model_execution(
            connection,
            project_id,
            task_id=None,
            task_kind="planning",
            preferred_capabilities=self._module_preferred_capabilities(
                module_spec, ["planning", "summarization"]
            ),
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
                prompt,
                self._merge_system_prompts(
                    self._module_prompt(module_spec, "system"),
                    self._module_prompt(module_spec, "planning"),
                    system_prompt,
                ),
                route["model_id"],
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
            {
                "prompt": prompt,
                "output_text": output_text,
                "agent_module": self._module_descriptor(module_spec),
            },
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
                "agent_module_id": (
                    module_spec.get("id") if isinstance(module_spec, dict) else None
                ),
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

    def _record_deployment_rollout(
        self,
        connection: Connection,
        project_id: str,
        task_id: Optional[str],
        *,
        decision_id: str,
        deployment_receipt_id: str,
        deployment_artifact_id: str,
        title: str,
        environment: str,
        pull_request_url: str,
        branch_name: Optional[str],
        merged_sha: str,
        deployment_id: int,
        deployment_status_url: str,
        deployment_state: str,
        verification: dict,
        health_artifact_id: Optional[str],
        rollback_decision_id: Optional[str],
        created_by_job_id: Optional[str] = None,
    ) -> dict:
        health_status = str(verification.get("status") or "unknown")
        health_stage_status = "skipped" if health_status == "skipped_unconfigured" else health_status
        rollout_status = "completed" if health_status in {"passed", "skipped_unconfigured"} else "blocked"
        current_stage = "release_readiness" if rollout_status == "completed" else "post_deploy_health"
        stages = [
            {
                "key": "github_deployment",
                "label": "GitHub deployment record",
                "status": "passed" if deployment_state == "success" else "failed",
                "evidence_refs": [
                    f"receipt:{deployment_receipt_id}",
                    f"artifact:{deployment_artifact_id}",
                ],
                "provider_ref": deployment_status_url,
            },
            {
                "key": "post_deploy_health",
                "label": "Post-deploy health verification",
                "status": health_stage_status,
                "required": bool(verification.get("healthcheck_url")),
                "summary": verification.get("summary"),
                "evidence_refs": (
                    [f"artifact:{health_artifact_id}"] if health_artifact_id else []
                ),
            },
            {
                "key": "release_readiness",
                "label": "Release readiness decision",
                "status": "passed" if rollout_status == "completed" else "blocked",
                "evidence_refs": [
                    f"artifact:{deployment_artifact_id}",
                    *(
                        [f"decision:{rollback_decision_id}"]
                        if rollback_decision_id
                        else []
                    ),
                ],
            },
        ]
        rollout_id = make_id("rollout")
        rollout_content = {
            "rollout_id": rollout_id,
            "title": title,
            "environment": environment,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
            "merged_sha": merged_sha,
            "deployment_id": deployment_id,
            "deployment_status_url": deployment_status_url,
            "status": rollout_status,
            "current_stage": current_stage,
            "health_verification_status": health_status,
            "rollback_decision_id": rollback_decision_id,
            "stages": stages,
        }
        rollout_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "deployment_rollout",
            f"Deployment rollout for {title}",
            rollout_content,
            created_by_job_id=created_by_job_id,
        )
        connection.execute(
            text(
                """
                INSERT INTO deployment_rollouts (
                    id, project_id, task_id, decision_id, deployment_receipt_id,
                    deployment_artifact_id, rollout_artifact_id, title, environment,
                    merged_sha, pull_request_url, branch_name, status, current_stage,
                    health_verification_status, rollback_decision_id, stages,
                    created_by_job_id
                )
                VALUES (
                    :id, :project_id, :task_id, :decision_id, :deployment_receipt_id,
                    :deployment_artifact_id, :rollout_artifact_id, :title, :environment,
                    :merged_sha, :pull_request_url, :branch_name, :status, :current_stage,
                    :health_verification_status, :rollback_decision_id,
                    CAST(:stages AS JSONB), :created_by_job_id
                )
                """
            ),
            {
                "id": rollout_id,
                "project_id": project_id,
                "task_id": task_id,
                "decision_id": decision_id,
                "deployment_receipt_id": deployment_receipt_id,
                "deployment_artifact_id": deployment_artifact_id,
                "rollout_artifact_id": rollout_artifact_id,
                "title": title,
                "environment": environment,
                "merged_sha": merged_sha,
                "pull_request_url": pull_request_url,
                "branch_name": branch_name,
                "status": rollout_status,
                "current_stage": current_stage,
                "health_verification_status": health_status,
                "rollback_decision_id": rollback_decision_id,
                "stages": json.dumps(stages),
                "created_by_job_id": created_by_job_id,
            },
        )
        emit_event(
            connection,
            project_id,
            "software_factory.deployment_rollout_recorded",
            "release_manager",
            "deployment_rollout",
            rollout_id,
            {
                "artifact_id": rollout_artifact_id,
                "deployment_artifact_id": deployment_artifact_id,
                "deployment_receipt_id": deployment_receipt_id,
                "status": rollout_status,
                "current_stage": current_stage,
                "rollback_decision_id": rollback_decision_id,
            },
        )
        return {
            "rollout_id": rollout_id,
            "rollout_artifact_id": rollout_artifact_id,
            "rollout_status": rollout_status,
            "rollout_stage": current_stage,
        }

    def list_deployment_rollouts(
        self, connection: Connection, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            text(
                """
                SELECT id, project_id, task_id, decision_id, deployment_receipt_id,
                       deployment_artifact_id, rollout_artifact_id, title,
                       environment, merged_sha, pull_request_url, branch_name,
                       status, current_stage, health_verification_status,
                       rollback_decision_id, stages, created_by_job_id,
                       created_at, updated_at
                FROM deployment_rollouts
                WHERE project_id = :project_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"project_id": project_id, "limit": limit},
        ).mappings().all()
        return [dict(row) for row in rows]

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
        agent_module_id: Optional[str] = None,
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

        boundary_block = self._block_github_effect_for_data_boundary(
            connection,
            project_id,
            task_id,
            "github.pull_request.create",
            "coding_worker",
        )
        if boundary_block:
            self.budget.release(
                connection,
                project_id,
                reservation_id,
                "Released reservation because data-boundary policy blocked the GitHub PR effect.",
            )
            return boundary_block

        module_spec = self._resolve_agent_module(
            connection,
            project_id,
            requested_module_id=agent_module_id,
            job_kind="software_factory.propose_pr",
        )
        route = self._route_model_execution(
            connection,
            project_id,
            task_id=task_id,
            task_kind="review",
            preferred_capabilities=self._module_preferred_capabilities(
                module_spec, ["review", "coding", "summarization"]
            ),
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
                "security_review_artifact_id": result.get("security_review_artifact_id"),
                "release_go_no_go_artifact_id": result.get(
                    "release_go_no_go_artifact_id"
                ),
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
                self._merge_system_prompts(
                    self._module_prompt(module_spec, "system"),
                    self._module_prompt(module_spec, "review_packet"),
                    (
                        "Return concise markdown with these sections: implementation summary, "
                        "tests added or still needed, risk hotspots, rollback plan, confidence, "
                        "and recommendation."
                    ),
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

        local_validation, local_validation_receipt_id, local_validation_receipt_status = (
            self._run_sandbox_validation(
                connection,
                scope,
                project_id,
                task_id,
                dedupe_key,
                branch_name,
                created_by_job_id,
            )
        )
        validation_status, validation_summary, validation_sources = (
            self._combine_validation_evidence(validation, local_validation)
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
        if local_validation:
            tests_added_or_run.extend(
                command["command"]
                for command in local_validation.get("commands", [])
                if isinstance(command.get("command"), str)
            )
        validation_follow_up = self._validation_follow_up_for_status(validation_status)
        security_dependency_review = self._build_security_dependency_review(files)
        browser_validation = self._build_browser_validation_review(files)

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
        security_review_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "security_dependency_review",
            f"Security and dependency review for {title}",
            {
                "title": title,
                "branch_name": branch_name,
                "pull_request_url": pr.url,
                "review": security_dependency_review,
                "browser_validation": browser_validation,
                "files_changed": [file.path for file in files],
                "diff_artifact_id": diff_artifact_id,
                "data_boundary": self._data_boundary_for_task(connection, task_id),
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
                "status": validation_status,
                "summary": validation_summary,
                "head_sha": validation.head_sha,
                "validation_source": "+".join(validation_sources),
                "validation_sources": validation_sources,
                "tests_added_or_run": tests_added_or_run,
                "checks": validation_checks,
                "local_validation": local_validation,
                "local_validation_receipt_id": local_validation_receipt_id,
                "local_validation_receipt_status": local_validation_receipt_status,
                "required_follow_up": validation_follow_up,
                "notes": review_text,
                "agent_module": self._module_descriptor(module_spec),
                "security_dependency_review_artifact_id": security_review_artifact_id,
                "security_dependency_review": security_dependency_review,
                "browser_validation": browser_validation,
            },
            created_by_job_id=created_by_job_id,
        )
        release_go_no_go = self._build_release_go_no_go(
            validation_status=validation_status,
            validation_summary=validation_summary,
            security_dependency_review=security_dependency_review,
            browser_validation=browser_validation,
            evidence_refs=[
                f"artifact:{diff_artifact_id}",
                f"artifact:{test_report_artifact_id}",
                f"artifact:{security_review_artifact_id}",
                f"receipt:{pr_receipt['id']}",
            ],
        )
        release_go_no_go_artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "release_go_no_go",
            f"Release go/no-go for {title}",
            {
                "title": title,
                "branch_name": branch_name,
                "pull_request_url": pr.url,
                "decision_input": release_go_no_go,
                "rollback_plan": "Revert the sandbox PR branch or close the PR.",
                "manual_approval_required": True,
                "no_auto_merge": True,
                "evidence_refs": release_go_no_go["evidence_refs"],
                "data_boundary": self._data_boundary_for_task(connection, task_id),
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
                "ci_validation_status": validation_status,
                "ci_validation_summary": validation_summary,
                "ci_checks": validation_checks,
                "local_validation": local_validation,
                "risk_hotspots": review_text,
                "rollback_plan": "Revert the sandbox PR branch or close the PR.",
                "security_dependency_review": security_dependency_review,
                "security_review_artifact_id": security_review_artifact_id,
                "browser_validation": browser_validation,
                "release_go_no_go": release_go_no_go,
                "release_go_no_go_artifact_id": release_go_no_go_artifact_id,
                "confidence_score": self._review_confidence_for_status(validation_status),
                "recommendation": self._review_recommendation_for_status(
                    validation_status
                ),
                "github_commits": commit_shas,
                "openai_response_id": review_provider_ref,
                "routing_log_id": route["route_decision_log_id"],
                "agent_module": self._module_descriptor(module_spec),
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
                "security_review_artifact_id": security_review_artifact_id,
                "release_go_no_go_artifact_id": release_go_no_go_artifact_id,
                "receipt_id": pr_receipt["id"],
                "pull_request_url": pr.url,
                "branch_name": branch_name,
                "title": title,
                "validation_status": validation_status,
                "validation_summary": validation_summary,
                "validation_sources": validation_sources,
                "release_go_no_go_status": release_go_no_go["status"],
                "local_validation_receipt_id": local_validation_receipt_id,
                "agent_module_id": (
                    module_spec.get("id") if isinstance(module_spec, dict) else None
                ),
            },
            {},
            "merge_decision_required",
        )
        result = {
            "artifact_id": artifact_id,
            "diff_artifact_id": diff_artifact_id,
            "test_report_artifact_id": test_report_artifact_id,
            "security_review_artifact_id": security_review_artifact_id,
            "release_go_no_go_artifact_id": release_go_no_go_artifact_id,
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch_name": branch_name,
            "validation_status": validation_status,
            "validation_summary": validation_summary,
            "validation_sources": validation_sources,
            "local_validation_receipt_id": local_validation_receipt_id,
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
                "validation_sources": validation_sources,
                "security_review_artifact_id": security_review_artifact_id,
                "release_go_no_go_artifact_id": release_go_no_go_artifact_id,
                "release_go_no_go_status": release_go_no_go["status"],
                "local_validation_receipt_id": local_validation_receipt_id,
                "agent_module_id": (
                    module_spec.get("id") if isinstance(module_spec, dict) else None
                ),
            },
        )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "diff_artifact_id": diff_artifact_id,
            "test_report_artifact_id": test_report_artifact_id,
            "security_review_artifact_id": security_review_artifact_id,
            "release_go_no_go_artifact_id": release_go_no_go_artifact_id,
            "receipt_id": pr_receipt["id"],
            "decision_id": decision_id,
            "pull_request_url": pr.url,
            "pull_request_number": pr.number,
            "branch_name": branch_name,
            "validation_status": validation_status,
            "validation_summary": validation_summary,
            "validation_sources": validation_sources,
            "local_validation_receipt_id": local_validation_receipt_id,
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

        boundary_block = self._block_github_effect_for_data_boundary(
            connection,
            project_id,
            task_id,
            "github.pull_request.merge",
            "release_manager",
        )
        if boundary_block:
            return boundary_block

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

        boundary_block = self._block_github_effect_for_data_boundary(
            connection,
            project_id,
            task_id,
            "github.deployment.create",
            "release_manager",
        )
        if boundary_block:
            return boundary_block

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
                "rollout_id": result.get("rollout_id"),
                "rollout_artifact_id": result.get("rollout_artifact_id"),
                "rollout_status": result.get("rollout_status"),
                "rollout_stage": result.get("rollout_stage"),
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
        rollout = self._record_deployment_rollout(
            connection,
            project_id,
            task_id,
            decision_id=decision_id,
            deployment_receipt_id=deployment_receipt["id"],
            deployment_artifact_id=artifact_id,
            title=title,
            environment=environment,
            pull_request_url=pull_request_url,
            branch_name=branch_name,
            merged_sha=merged_sha,
            deployment_id=deployment.id,
            deployment_status_url=deployment_status.url,
            deployment_state=deployment_status.state,
            verification=verification,
            health_artifact_id=health_verification_artifact_id,
            rollback_decision_id=health_decision_id,
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
            **rollout,
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
            **rollout,
            "environment": environment,
            "deployment_id": deployment.id,
            "deployment_status": deployment_status.state,
            "deployment_url": deployment_status.url,
            "merged_sha": merged_sha,
        }
