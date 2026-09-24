from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from ..config import Settings
from ..ports import (
    BudgetPort,
    DecisionSink,
    EnginePorts,
    EntitlementPort,
    EventSink,
    get_engine_ports,
    sql,
)
from ..schemas import FileChange
from .core import (
    create_artifact,
    make_id,
    normalize_data_boundary,
)
from .github_adapter import (
    GitHubAdapter,
    IntegrationConfigurationError,
    PullRequestValidationResult,
)
from .agent_loop import load_agent_run, run_agent_loop
from .claude_cli_adapter import make_chat_adapter
from .openai_adapter import OpenAIAdapter, estimate_cost_usd_micros  # noqa: F401
from .pipeline_adapter import make_pipeline_adapter
from .receipts import ReceiptService
from .sealed_e2e_contract import attach_sealed_evidence, attach_sealed_receipt_result
from .sandbox_validation import (
    SandboxValidationAdapter,
    SandboxValidationConfigurationError,
)
from .specpack import SpecPackService

# Review packet is optional commentary on the PR. Dumping every file body into
# the LLM prompt overflowed gpt-4o/Groq on official BugBox/HookWatch runs
# (60+ files) and then released the reservation so retries died as
# "Missing active reservation". Keep the prompt bounded; fall back to a static
# packet rather than sink the deliverable.
REVIEW_PACKET_PREVIEW_CHARS = 240
REVIEW_PACKET_MAX_FILES = 40
REVIEW_PACKET_MAX_CHARS = 12_000


def review_packet_file_summaries(files: list[FileChange]) -> list[dict[str, Any]]:
    """Path + size + short preview. Never the full diff."""
    summaries: list[dict[str, Any]] = []
    for file in list(files)[:REVIEW_PACKET_MAX_FILES]:
        content = file.content or ""
        summaries.append(
            {
                "path": file.path,
                "bytes": len(content.encode("utf-8")),
                "preview": content[:REVIEW_PACKET_PREVIEW_CHARS],
            }
        )
    omitted = len(files) - REVIEW_PACKET_MAX_FILES
    if omitted > 0:
        summaries.append({"omitted_files": omitted})
    return summaries


def review_packet_prompt(*, title: str, summary: str, files: list[FileChange]) -> str:
    payload = json.dumps(review_packet_file_summaries(files), indent=2)
    if len(payload) > REVIEW_PACKET_MAX_CHARS:
        payload = payload[:REVIEW_PACKET_MAX_CHARS] + "\n…truncated…"
    return (
        "Create a review packet for a BOSS software-factory proposal.\n\n"
        f"Title: {title}\n"
        f"Summary: {summary}\n"
        f"File summaries (not full contents):\n{payload}"
    )


def fallback_review_packet(
    *,
    title: str,
    summary: str,
    files: list[FileChange],
    error: str | None = None,
) -> str:
    """Static packet when the review model cannot run. Does not invent scores."""
    paths = [file.path for file in files]
    shown = paths[:REVIEW_PACKET_MAX_FILES]
    omitted = len(paths) - len(shown)
    lines = [
        f"# Review packet unavailable for {title}",
        "",
        "The review-packet model did not produce commentary. The pull request "
        "still carries the governed diff. No UI/UX scores are inferred.",
        "",
        f"Summary: {summary}",
        f"Files changed ({len(paths)}):",
        *[f"- {path}" for path in shown],
    ]
    if omitted:
        lines.append(f"- … {omitted} more files omitted from this list")
    if error:
        lines.extend(["", f"Model error (not a score): {error[:400]}"])
    return "\n".join(lines)


class SoftwareFactoryService:
    def __init__(
        self,
        settings: Settings,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        ports: Optional[EnginePorts] = None,
        budget_port: Optional[BudgetPort] = None,
        entitlement_port: Optional[EntitlementPort] = None,
        event_sink: Optional[EventSink] = None,
        decision_sink: Optional[DecisionSink] = None,
    ):
        self.settings = settings
        resolved_ports = ports or get_engine_ports() or EnginePorts.oss_defaults(settings)
        self.ports = resolved_ports
        self.budget = budget_port or resolved_ports.budget
        self.receipts = ReceiptService()
        # WHICH REPO is the run's decision, not a global default.
        #
        # This said `GitHubAdapter(settings)`, so every run cloned, branched and opened its PR
        # against BOSS_TARGET_GITHUB_* no matter what the task asked for. The caller had already
        # resolved `spec["owner"]/spec["repo"]` and minted a token for THAT repo, so the two
        # disagreed silently: a task targeting a project repo authenticated for that repo
        # and then pushed to the default sandbox, where its edits landed as 17k lines of "new"
        # files because none of the originals were there. The run reported success.
        #
        # A run that names a repo gets that repo; the setting remains the default for runs that
        # do not.
        self.github = GitHubAdapter(settings, owner=owner, repo=repo)
        self.pipeline = make_pipeline_adapter(settings, github=self.github)
        self.openai = make_chat_adapter(settings)
        self.entitlements = entitlement_port or resolved_ports.entitlement
        self.events = event_sink or resolved_ports.events
        self.decisions = decision_sink or resolved_ports.decisions
        self.specpack = SpecPackService(settings)
        self.sandbox_validation = SandboxValidationAdapter(settings)
        # Dual-plane honesty: factory historically used BOSS_GITHUB_TOKEN / App env
        # (legacy_env). When a Mission Control primary binding is present we prefer it
        # (mission_control_binding). Callers and API responses surface which plane ran.
        self.github_plane: str = "legacy_env"
        self.github_binding_id: Optional[str] = None
        self.github_connection_id: Optional[str] = None

    def bind_project_github(
        self,
        connection: Any,
        project_id: str,
        *,
        selector: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> dict[str, Any]:
        """Prefer the project's primary Mission Control binding for factory GitHub I/O.

        When a binding exists and its credentials resolve, rebinds ``self.github`` (and
        pipeline) to that adapter and sets ``github_plane=mission_control_binding``.
        Otherwise keeps the legacy env/App adapter and sets ``github_plane=legacy_env``.

        Returns a plane descriptor suitable for API honesty fields.
        """
        from .github_connections import (
            GitHubConnectionService,
            GitHubConnectionStateError,
            GitHubRepositoryBindingNotFoundError,
        )
        from .repo_registry import RepositoryResolutionError, resolve_repository_target

        plane = {
            "github_plane": "legacy_env",
            "github_binding_id": None,
            "github_connection_id": None,
            "owner": owner or getattr(self.github, "owner", None),
            "repo": repo or getattr(self.github, "repo", None),
            "full_name": getattr(self.github, "repository_full_name", None),
            "source": "legacy_settings",
        }
        try:
            target = resolve_repository_target(
                connection,
                self.settings,
                project_id,
                selector=selector
                or (
                    f"{owner}/{repo}"
                    if owner and repo
                    else None
                ),
                allow_legacy_fallback=True,
            )
        except RepositoryResolutionError:
            self.github_plane = "legacy_env"
            self.github_binding_id = None
            self.github_connection_id = None
            return plane

        plane["owner"] = target.get("owner") or plane["owner"]
        plane["repo"] = target.get("repo") or plane["repo"]
        plane["full_name"] = target.get("full_name") or plane["full_name"]
        plane["source"] = target.get("source") or plane["source"]
        binding_id = target.get("binding_id")
        if not binding_id:
            # Registry fell back to BOSS_TARGET_GITHUB_* — still the legacy plane.
            if plane["owner"] and plane["repo"]:
                self.github = GitHubAdapter(
                    self.settings, owner=plane["owner"], repo=plane["repo"]
                )
                self.pipeline = make_pipeline_adapter(self.settings, github=self.github)
            self.github_plane = "legacy_env"
            self.github_binding_id = None
            self.github_connection_id = target.get("connection_id")
            plane["github_plane"] = "legacy_env"
            return plane

        try:
            gh_conn = GitHubConnectionService(self.settings)
            adapter, connection_row, binding = gh_conn.adapter_for_binding(
                connection,
                project_id=project_id,
                binding_id=binding_id,
            )
        except (
            GitHubConnectionStateError,
            GitHubRepositoryBindingNotFoundError,
            Exception,
        ):
            # Binding present but credentials/connection not usable — fall back honestly.
            if plane["owner"] and plane["repo"]:
                self.github = GitHubAdapter(
                    self.settings, owner=plane["owner"], repo=plane["repo"]
                )
                self.pipeline = make_pipeline_adapter(self.settings, github=self.github)
            self.github_plane = "legacy_env"
            self.github_binding_id = binding_id
            self.github_connection_id = target.get("connection_id")
            plane["github_plane"] = "legacy_env"
            plane["github_binding_id"] = binding_id
            plane["github_connection_id"] = target.get("connection_id")
            plane["source"] = "binding_fallback_legacy_env"
            return plane

        self.github = adapter
        self.pipeline = make_pipeline_adapter(self.settings, github=self.github)
        self.github_plane = "mission_control_binding"
        self.github_binding_id = binding.get("id") or binding_id
        self.github_connection_id = (
            (connection_row or {}).get("id") or target.get("connection_id")
        )
        plane.update(
            {
                "github_plane": "mission_control_binding",
                "github_binding_id": self.github_binding_id,
                "github_connection_id": self.github_connection_id,
                "owner": binding.get("owner") or plane["owner"],
                "repo": binding.get("name") or plane["repo"],
                "full_name": binding.get("full_name") or plane["full_name"],
                "source": target.get("source") or "registry:primary",
            }
        )
        return plane

    def _github_plane_fields(self) -> dict[str, Any]:
        return {
            "github_plane": self.github_plane,
            "github_binding_id": self.github_binding_id,
            "github_connection_id": self.github_connection_id,
        }

    def describe_github_plane(
        self,
        connection: Any,
        project_id: str,
        *,
        selector: Optional[str] = None,
    ) -> dict[str, Any]:
        """Read-only dual-plane honesty for operator cards.

        Does **not** rebind adapters or resolve SecretRefs — safe on GET /factory/wip.
        Reports whether a Mission Control primary binding exists vs the legacy
        ``BOSS_GITHUB_TOKEN`` / ``BOSS_TARGET_GITHUB_*`` settings plane.
        """
        from .repo_registry import RepositoryResolutionError, resolve_repository_target

        plane: dict[str, Any] = {
            "github_plane": "legacy_env",
            "github_binding_id": None,
            "github_connection_id": None,
            "owner": getattr(self.github, "owner", None),
            "repo": getattr(self.github, "repo", None),
            "full_name": getattr(self.github, "repository_full_name", None),
            "source": "legacy_settings",
        }
        try:
            target = resolve_repository_target(
                connection,
                self.settings,
                project_id,
                selector=selector,
                allow_legacy_fallback=True,
            )
        except RepositoryResolutionError as error:
            plane["reason"] = str(error)[:240]
            return plane

        plane["owner"] = target.get("owner") or plane["owner"]
        plane["repo"] = target.get("repo") or plane["repo"]
        plane["full_name"] = target.get("full_name") or plane["full_name"]
        plane["source"] = target.get("source") or plane["source"]
        binding_id = target.get("binding_id")
        if not binding_id:
            return plane

        plane["github_plane"] = "mission_control_binding"
        plane["github_binding_id"] = binding_id
        plane["github_connection_id"] = target.get("connection_id")
        return plane

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

    def _scope(self, connection: Any, project_id: str) -> dict:
        row = connection.execute(
            sql(
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
        self, connection: Any, task_id: Optional[str]
    ) -> str:
        if not task_id:
            return "internal"
        row = connection.execute(
            sql("SELECT data_boundary FROM tasks WHERE id = :task_id"),
            {"task_id": task_id},
        ).mappings().first()
        return normalize_data_boundary(row["data_boundary"] if row else None)

    def _block_github_effect_for_data_boundary(
        self,
        connection: Any,
        project_id: str,
        task_id: Optional[str],
        effect_kind: str,
        actor: str,
    ) -> Optional[dict]:
        boundary = self._data_boundary_for_task(connection, task_id)
        if boundary not in {"restricted", "local-only"}:
            return None

        decision_id = self.decisions.create_blocking(
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
        self.events.emit(
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
        connection: Any,
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
            decision_id = self.decisions.create_blocking(
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

        decision_id = self.decisions.create_blocking(
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
        connection: Any,
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
        connection: Any,
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

        self.events.emit(
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

    def _build_browser_validation_review(self, files: list[FileChange], *,
                                         render_evidence: Optional[dict] = None) -> dict:
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
        # APP-4 (#94): if render evidence was captured (served HTML now, headless capture later),
        # run the deterministic visual rubric and return a REAL passed|failed with findings —
        # instead of the old hardcoded required_not_run no-op.
        if render_evidence is not None:
            from .visual_gate import evaluate_visual
            verdict = evaluate_visual(render_evidence)
            return {
                "status": verdict["status"],
                "browser_relevant_files": browser_relevant_files,
                "evidence_refs": verdict["evidence_refs"],
                "findings": verdict["findings"],
                "checks": verdict["checks"],
                "required_follow_up": ([] if verdict["status"] == "passed"
                                       else ["Fix the visual/UX findings before a UI-affecting merge."]),
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
        if browser_validation["status"] == "failed":
            blockers.append("visual_gate_failed")   # APP-4: a real failed visual gate blocks the merge
        elif browser_validation["status"] == "required_not_run":
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
        connection: Any,
        project_id: str,
        reservation_id: str,
        dedupe_key: str,
        prompt: str,
        system_prompt: Optional[str],
        created_by_job_id: Optional[str] = None,
        agent_module_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        reservation = self.budget.assert_active_reservation(
            connection, project_id, reservation_id
        )
        if not reservation:
            decision_id = self.decisions.create_blocking(
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
            merged_system = self._merge_system_prompts(
                self._module_prompt(module_spec, "system"),
                self._module_prompt(module_spec, "planning"),
                system_prompt,
                "You have read-only tools to fetch the project's real data "
                "(budget/costs, project status, and codebase files). Call them to "
                "ground your plan in reality before answering. Cite concrete numbers "
                "and files you retrieved; do not invent figures. "
                "You also have write tools: create_followup_task (low risk, runs "
                "immediately) and open_incident (high risk — it will be deferred to a "
                "human decision and only run on approval). Use them when the task "
                "clearly warrants concrete action; prefer create_followup_task for "
                "routine next steps. "
                "If MCP tools are available (their names start with 'mcp_'), you may "
                "have web-research tools (e.g. study_site / fetch_page) to study a live "
                "external website before writing about it — ALWAYS study the real site "
                "first when the task references a URL, and cite concrete things you "
                "found (pages, features, copy) instead of inventing them. "
                "When the task asks you to PRODUCE A DOCUMENT (a PRD, spec, report, or "
                "proposal), write the COMPLETE document in markdown and then call the "
                "publish_document tool with the full markdown to render it as a PDF and "
                "open a GitHub pull request for human review (high risk — deferred for "
                "approval). Do not just describe the document; actually publish it.",
            )
            finalize_ctx = {
                "prompt": prompt,
                "receipt_id": receipt["id"],
                "reservation_id": reservation_id,
                "reservation_amount": int(reservation["amount_usd_micros"]),
                "route_model_id": route["model_id"],
                "route_source_id": route["source_id"],
                "route_log_id": route["route_decision_log_id"],
                "module_descriptor": self._module_descriptor(module_spec),
                "module_id": module_spec.get("id") if isinstance(module_spec, dict) else None,
                "job_kind": "openai.plan",
                "skill_caller": "software_factory",
                "created_by_job_id": created_by_job_id,
            }
            loop_result = run_agent_loop(
                connection,
                self.settings,
                project_id,
                merged_system,
                prompt,
                task_id=task_id,
                finalize_ctx=finalize_ctx,
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
            decision_id = self.decisions.create_blocking(
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
            self.events.emit(
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

        if loop_result["status"] == "suspended":
            # The agent paused on a high-risk action; the plan is finalized later
            # when the human approves and the run resumes to completion.
            return {
                "status": "suspended",
                "run_id": loop_result["run_id"],
                "decision_id": loop_result.get("decision_id"),
                "message": "Plan paused for human approval of a high-risk action.",
            }
        return self._finalize_plan_run(connection, loop_result["run_id"])

    def _finalize_plan_run(self, connection: Any, run_id: str) -> dict:
        """Turn a completed agent run into a plan_draft artifact + settled cost +
        review decision. Called when the run finishes — immediately, or later
        after a suspend -> approve -> resume cycle."""
        run = load_agent_run(connection, run_id)
        ctx = (run.get("finalize_ctx") if run else None) or {}
        project_id = run["project_id"]
        task_id = run["task_id"]
        output_text = run.get("output_text") or ""
        usage = run.get("usage") or {}
        transcript = run.get("transcript") or []
        artifact_id = create_artifact(
            connection,
            self.settings,
            project_id,
            task_id,
            "plan_draft",
            "OpenAI plan draft",
            {
                "prompt": ctx.get("prompt"),
                "output_text": output_text,
                "agent_module": ctx.get("module_descriptor") or {},
                "usage": usage,
                "cost_usd_micros": estimate_cost_usd_micros(usage),
                "agent_transcript": transcript,
                "tool_calls": len(transcript),
                "agent_run_id": run_id,
            },
            created_by_job_id=ctx.get("created_by_job_id"),
        )
        self.receipts.mark_succeeded(
            connection,
            ctx["receipt_id"],
            "agent_loop",
            {"artifact_id": artifact_id, "output_text": output_text},
        )
        self.budget.settle(
            connection,
            project_id,
            ctx["reservation_id"],
            ctx["receipt_id"],
            "model",
            usage.get("model") or ctx.get("route_model_id"),
            int(ctx.get("reservation_amount") or 0),
            estimate_cost_usd_micros(usage),
        )
        self.events.emit(
            connection,
            project_id,
            "software_factory.plan_created",
            "coding_worker",
            "artifact",
            artifact_id,
            {
                "receipt_id": ctx["receipt_id"],
                "routing_log_id": ctx.get("route_log_id"),
                "source_id": ctx.get("route_source_id"),
                "model_id": ctx.get("route_model_id"),
                "agent_module_id": ctx.get("module_id"),
            },
        )
        decision_id = None
        if task_id:
            task_row = connection.execute(
                sql("SELECT title FROM tasks WHERE id = :t AND project_id = :p"),
                {"t": task_id, "p": project_id},
            ).mappings().first()
            decision_id = self.decisions.create_blocking(
                connection,
                project_id,
                task_id,
                f"Review plan draft for {(task_row['title'] if task_row else None) or 'queued task'}",
                {"action": "review_plan_draft", "artifact_id": artifact_id},
                {},
                "plan_ready_for_review",
            )
        return {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": ctx["receipt_id"],
            "output_text": output_text,
            "decision_id": decision_id,
        }

    def resume_plan_run(self, connection: Any, run_id: str) -> dict:
        """Resume a suspended plan run after a human approval; finalize if it
        completes (it may suspend again on another high-risk action)."""
        from .agent_loop import resume_agent_run

        result = resume_agent_run(connection, self.settings, run_id)
        if result.get("status") == "completed":
            return self._finalize_plan_run(connection, run_id)
        return result

    def _mark_merge_completed(
        self,
        connection: Any,
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
        self, connection: Any, project_id: str, decision_id: str, actor: str
    ) -> None:
        updated = connection.execute(
            sql(
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
            self.events.emit(
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
        connection: Any,
        project_id: str,
        task_id: Optional[str],
        actor: str,
        decision_id: str,
    ) -> None:
        if not task_id:
            return
        task_updated = connection.execute(
            sql(
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
            self.events.emit(
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
        connection: Any,
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
                sql(
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
        decision_id = self.decisions.create_blocking(
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
        self.events.emit(
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
        connection: Any,
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
        connection: Any,
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
        decision_id = self.decisions.create_blocking(
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
        self.events.emit(
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
        connection: Any,
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
            sql(
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
        self.events.emit(
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
        self, connection: Any, project_id: str, limit: int
    ) -> list[dict]:
        rows = connection.execute(
            sql(
                """
                SELECT id, project_id, task_id, decision_id, deployment_receipt_id,
                       deployment_artifact_id, rollout_artifact_id, title,
                       environment, merged_sha, pull_request_url, branch_name,
                       status, current_stage, health_verification_status,
                       rollback_decision_id, stages, created_by_job_id,
                       deployment_run_id, promotion_id, rollback_reason,
                       health_observations, started_at, finished_at,
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

    def _review_packet_markdown(
        self,
        connection: Any,
        *,
        project_id: str,
        receipt_id: str,
        title: str,
        summary: str,
        files: list[FileChange],
        module_spec: Any,
        model_id: Optional[str],
    ) -> str:
        """Build review-packet markdown. Model failure never sinks the PR."""
        prompt = review_packet_prompt(title=title, summary=summary, files=files)
        system = self._merge_system_prompts(
            self._module_prompt(module_spec, "system"),
            self._module_prompt(module_spec, "review_packet"),
            (
                "Return concise markdown with these sections: implementation summary, "
                "tests added or still needed, risk hotspots, rollback plan, confidence, "
                "and recommendation. Do not invent UI/UX scores."
            ),
        )
        try:
            _ref, text, _usage = self.openai.create_text_response(
                prompt, system, model_id
            )
            if str(text or "").strip():
                return str(text)
        except Exception as error:  # noqa: BLE001 — packet is optional
            self.events.emit(
                connection,
                project_id,
                "software_factory.review_packet_skipped",
                "coding_worker",
                "receipt",
                receipt_id,
                {"message": str(error)[:400], "reason": "review_packet_optional"},
            )
            return fallback_review_packet(
                title=title, summary=summary, files=files, error=str(error)
            )
        return fallback_review_packet(
            title=title, summary=summary, files=files, error="empty review packet"
        )

    def propose_pull_request(
        self,
        connection: Any,
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
        actual_usd_micros: Optional[int] = None,
        evidence: Optional[dict] = None,
        quality_gate_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> dict:
        # Prefer Mission Control primary binding credentials when present (P0-6).
        # Surface github_plane on every return path so dual-path is never silent.
        plane = self.bind_project_github(
            connection,
            project_id,
            owner=getattr(self.github, "owner", None),
            repo=getattr(self.github, "repo", None),
        )
        plane_fields = {
            "github_plane": plane.get("github_plane") or self.github_plane,
            "github_binding_id": plane.get("github_binding_id") or self.github_binding_id,
            "github_connection_id": (
                plane.get("github_connection_id") or self.github_connection_id
            ),
        }

        reservation = self.budget.assert_active_reservation(
            connection, project_id, reservation_id
        )
        if not reservation:
            decision_id = self.decisions.create_blocking(
                connection,
                project_id,
                task_id,
                "Missing active reservation for PR proposal",
                {"action": "create_budget_reservation"},
                {},
                "missing_reservation",
            )
            return {"status": "blocked", "decision_id": decision_id, **plane_fields}

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
            return {**boundary_block, **plane_fields}

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
            return {**route["blocked"], **plane_fields}

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
                **plane_fields,
            }

        review_text = self._review_packet_markdown(
            connection,
            project_id=project_id,
            receipt_id=pr_receipt["id"],
            title=title,
            summary=summary,
            files=files,
            module_spec=module_spec,
            model_id=route.get("model_id"),
        )
        # Packet is optional commentary. A skipped model call has no provider ref.
        review_provider_ref = None

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
            decision_id = self.decisions.create_blocking(
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
                **plane_fields,
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
            self.events.emit(
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
                **plane_fields,
            }

        branch_head_sha = commit_shas[-1] if commit_shas else self.github.ensure_branch(
            branch_name
        )
        try:
            validation = self.pipeline.wait_for_verdict(
                branch_head_sha,
                timeout_seconds=self.settings.sandbox_ci_timeout_seconds,
                poll_interval_seconds=self.settings.sandbox_ci_poll_interval_seconds,
            ).to_pull_request_validation()
        except Exception as error:
            validation = PullRequestValidationResult(
                status="unavailable",
                summary=f"Unable to capture GitHub validation evidence: {error}",
                head_sha=branch_head_sha,
                completed=False,
                checks=[],
            )
            self.events.emit(
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
        sealed_evidence = attach_sealed_evidence(evidence)
        decision_id = self.decisions.create_blocking(
            connection,
            project_id,
            task_id,
            f"Approve code change: {title} (PR #{pr.number})",
            {
                "action": "review_pull_request",
                "summary": (
                    f"BOSS built “{title}” in a sandbox and opened pull request #{pr.number}. "
                    f"Automated checks: {validation_status}"
                    + (f" — {validation_summary}" if validation_summary else "")
                    + ". Review the diff on GitHub, then approve to merge or reject to send back."
                ),
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
                "quality_gate_id": quality_gate_id,
                "run_id": run_id,
                "no_auto_merge": True,
                **sealed_evidence,
            },
            {},
            "merge_decision_required",
        )
        result = attach_sealed_receipt_result(
            {
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
                "receipt_id": pr_receipt["id"],
            },
            evidence=sealed_evidence,
            decision_id=decision_id,
            receipt_id=pr_receipt["id"],
            quality_gate_id=quality_gate_id,
            run_id=run_id,
        )
        self.receipts.mark_succeeded(connection, pr_receipt["id"], pr.url, result)
        self.budget.settle(
            connection,
            project_id,
            reservation_id,
            pr_receipt["id"],
            "tool",
            "github.pull_request.create",
            int(reservation["amount_usd_micros"]),
            actual_usd_micros,
        )
        self.events.emit(
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
            **plane_fields,
        }

    def merge_pull_request(
        self,
        connection: Any,
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
                sql(
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

        recovered_from_stale_base = False
        try:
            merge = self.github.merge_pull_request(pull_request_number, title)
        except IntegrationConfigurationError as error:
            self.receipts.mark_failed(
                connection, merge_receipt["id"], {"message": str(error)}
            )
            decision_id_block = self.decisions.create_blocking(
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
            # CONFLICT RECOVERY, TIER 1: the base branch moved under us.
            # When a batch of PRs share a base and one merges, the rest go CONFLICTING and
            # GitHub answers this endpoint with 405 Method Not Allowed. Recording the failure
            # and stopping meant only the FIRST PR of any batch could ever land — exactly what
            # happened to PRs #67 and #68 the moment #66 merged.
            # Ask GitHub to merge the base INTO the head branch and try once more. That
            # resolves ordinary drift unattended; a genuine textual conflict is refused, and
            # falls through to the failure path below where an agent is needed.
            #
            # On a successful retry we MUST fall through to the same completion path as a
            # clean merge (artifact, _mark_merge_completed, mark_succeeded arity). An early
            # return after GitHub already merged left BOSS recording failure / skipping
            # deploy follow-on — merge succeeded on GitHub, BOSS said it failed.
            _msg = str(error)
            merge = None
            if ("405" in _msg or "not mergeable" in _msg.lower()) and hasattr(
                    self.github, "update_pull_request_branch"):
                try:
                    if self.github.update_pull_request_branch(pull_request_number):
                        self.events.emit(
                            connection, project_id,
                            "software_factory.pull_request_branch_updated",
                            "release_manager", "receipt", merge_receipt["id"],
                            {"pull_request_url": pull_request_url,
                             "why": "base branch had moved; merged base into head and retrying"},
                        )
                        merge = self.github.merge_pull_request(pull_request_number, title)
                        recovered_from_stale_base = True
                except Exception as retry_error:  # noqa: BLE001
                    _msg = f"{_msg} | retry after branch update: {retry_error}"
                    merge = None

            if merge is None:
                self.receipts.mark_failed(
                    connection, merge_receipt["id"], {"message": _msg}
                )
                self.events.emit(
                    connection,
                    project_id,
                    "software_factory.pull_request_merge_failed",
                    "release_manager",
                    "receipt",
                    merge_receipt["id"],
                    {"message": _msg, "pull_request_url": pull_request_url,
                     # TIER 2 lives with the caller: a real conflict needs an agent to resolve it,
                     # which is a coding task, not a git call. Flagged so the caller can route it.
                     "needs_conflict_resolution": ("405" in _msg
                                                   or "conflict" in _msg.lower())},
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
                **({"recovered_from": "stale_base"} if recovered_from_stale_base else {}),
            },
            created_by_job_id=created_by_job_id,
        )
        result = {
            "artifact_id": artifact_id,
            "merged_sha": merge.sha,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
        }
        if recovered_from_stale_base:
            result["recovered_from"] = "stale_base"

        # THE SEAM: a merged PR becomes a deployment.
        # BOSS already had the whole delivery apparatus — plan_deployment_run, the rollout
        # engine, promotion, telemetry connectors, nine tables — and every table was empty
        # because nothing ever observed a merge. This is the one line that was missing.
        # Best-effort and non-fatal by construction: the PR has ALREADY merged by the time we
        # get here, and an exception would turn shipped work into a failure report.
        try:
            from .delivery_pipeline import on_pull_request_merged

            _run_id = ""
            if branch_name and branch_name.startswith("boss/code/"):
                _run_id = branch_name.rsplit("/", 1)[-1]
            result["delivery"] = on_pull_request_merged(
                connection.engine if hasattr(connection, "engine") else None,
                self.settings, project_id=project_id, run_id=_run_id,
                repo=self.settings.target_github_repo, commit_ref=merge.sha,
                decision_id=None, actor="release_manager")
        except Exception:  # noqa: BLE001 — delivery must never un-ship a merge
            pass
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
        self.events.emit(
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
                **({"recovered_from": "stale_base"} if recovered_from_stale_base else {}),
            },
        )
        out = {
            "status": "created",
            "artifact_id": artifact_id,
            "receipt_id": merge_receipt["id"],
            "decision_id": decision_id,
            "deployment_decision_id": follow_on_decision_id,
            "pull_request_url": pull_request_url,
            "branch_name": branch_name,
            "merged_sha": merge.sha,
        }
        if recovered_from_stale_base:
            out["recovered_from"] = "stale_base"
        return out

    def create_deployment(
        self,
        connection: Any,
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
                sql(
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
            decision_id_block = self.decisions.create_blocking(
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
            self.events.emit(
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
            self.events.emit(
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
            self.events.emit(
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
