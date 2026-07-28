"""Server-side executor registry for real SkillSpec implementations.

The registry only resolves deterministic, explicitly registered bindings.  It does
not execute anything by itself; callers such as ``SkillRuntimeService`` still own
permission, budget, retry, lease, and policy gates.  External GitHub effects add a
Receipt at this boundary so a future real-mode caller cannot bypass effect evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .github_adapter import GitHubAdapter
from .receipts import ReceiptService
from .resource_plane import ResourcePlaneService


class SkillExecutorConfigurationError(RuntimeError):
    pass


SkillExecutor = Callable[[dict[str, Any]], dict[str, Any]]
RegisteredExecutor = Callable[["SkillExecutorContext", dict[str, Any]], dict[str, Any]]


@dataclass
class SkillExecutorContext:
    settings: Settings
    connection: Connection
    project_id: str
    actor: str
    created_by_job_id: Optional[str] = None
    org_id: Optional[str] = None
    workspace_id: Optional[str] = None
    github_adapter: Optional[Any] = None
    deployment_service: Optional[Any] = None
    receipt_service: Optional[Any] = None

    @property
    def github(self) -> Any:
        return self.github_adapter or GitHubAdapter(self.settings)

    @property
    def deployments(self) -> Any:
        return self.deployment_service or ResourcePlaneService(self.settings)

    @property
    def receipts(self) -> Any:
        return self.receipt_service or ReceiptService()

    def tenant_scope(self) -> tuple[str, str]:
        if self.org_id and self.workspace_id:
            return self.org_id, self.workspace_id
        row = self.connection.execute(
            text(
                """
                SELECT w.org_id, p.workspace_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": self.project_id},
        ).mappings().one_or_none()
        if not row:
            raise SkillExecutorConfigurationError(
                f"Project '{self.project_id}' has no tenant scope."
            )
        return str(row["org_id"]), str(row["workspace_id"])


_EXECUTORS: dict[str, RegisteredExecutor] = {}


def register_executor(
    skill_id: str,
    executor: Optional[RegisteredExecutor] = None,
) -> Callable[[RegisteredExecutor], RegisteredExecutor] | RegisteredExecutor:
    """Register an executor directly or as ``@register_executor(skill_id)``."""

    normalized = skill_id.strip()
    if not normalized:
        raise ValueError("skill_id must not be empty")

    def decorator(handler: RegisteredExecutor) -> RegisteredExecutor:
        if normalized in _EXECUTORS and _EXECUTORS[normalized] is not handler:
            raise ValueError(f"Executor already registered for '{normalized}'.")
        _EXECUTORS[normalized] = handler
        return handler

    return decorator(executor) if executor is not None else decorator


def resolve_executor(
    skill_id: str,
    *,
    context: Optional[SkillExecutorContext] = None,
) -> Optional[SkillExecutor]:
    """Resolve a bound one-argument callable, or ``None`` for unbacked skills."""

    handler = _EXECUTORS.get(skill_id.strip())
    if handler is None:
        return None

    def execute(inputs: dict[str, Any]) -> dict[str, Any]:
        if context is None:
            raise SkillExecutorConfigurationError(
                f"Skill '{skill_id}' requires a server-side executor context."
            )
        return handler(context, inputs)

    return execute


def registered_executor_ids() -> tuple[str, ...]:
    return tuple(sorted(_EXECUTORS))


def _required_text(inputs: dict[str, Any], key: str) -> str:
    value = str(inputs.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_list(inputs: dict[str, Any], key: str) -> list[Any]:
    value = inputs.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return value


def _github_create_pr(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
    *,
    skill_id: str,
) -> dict[str, Any]:
    repo = _required_text(inputs, "repo")
    branch = _required_text(inputs, "branch")
    title = _required_text(inputs, "title")
    body = _required_text(inputs, "body")
    github = context.github
    if repo != github.repository_full_name:
        raise ValueError(
            f"Skill target '{repo}' does not match configured repository "
            f"'{github.repository_full_name}'."
        )

    org_id, workspace_id = context.tenant_scope()
    dedupe_key = f"{skill_id}:{repo}:{branch}:{title}"
    receipt, created = context.receipts.create_or_reuse(
        context.connection,
        org_id,
        workspace_id,
        context.project_id,
        dedupe_key,
        "github.pull_request.create",
        {"repository": repo, "branch": branch, "data_boundary": "internal"},
        {"title": title, "body": body, "skill_id": skill_id},
        created_by_job_id=context.created_by_job_id,
        data_boundary="internal",
    )
    if not created:
        result = receipt.get("result") or {}
        if receipt.get("status") != "succeeded":
            return {
                "status": str(receipt.get("status") or "in_progress"),
                "pr_ref": None,
                "pr_url": None,
                "pull_request_number": None,
                "receipt_id": receipt["id"],
            }
        return {
            "status": "reused",
            "pr_ref": result.get("pr_url"),
            "pr_url": result.get("pr_url"),
            "pull_request_number": result.get("pull_request_number"),
            "receipt_id": receipt["id"],
            "actual_cost_usd_micros": 0,
        }

    try:
        pull_request = github.create_pull_request(branch, title, body)
        result = {
            "pr_url": pull_request.url,
            "pull_request_number": pull_request.number,
            "branch": branch,
        }
        context.receipts.mark_succeeded(
            context.connection,
            receipt["id"],
            pull_request.url,
            result,
        )
    except Exception as error:
        context.receipts.mark_failed(
            context.connection,
            receipt["id"],
            {"message": str(error), "skill_id": skill_id},
        )
        raise
    return {
        "status": "created",
        "pr_ref": pull_request.url,
        "pr_url": pull_request.url,
        "pull_request_number": pull_request.number,
        "receipt_id": receipt["id"],
        "actual_cost_usd_micros": 0,
    }


@register_executor("github.create_pr_sandbox")
def execute_github_create_pr_sandbox(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    return _github_create_pr(
        context,
        inputs,
        skill_id="github.create_pr_sandbox",
    )


@register_executor("github.create_pr_real")
def execute_github_create_pr_real(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    if not inputs.get("review_packet_artifact_id"):
        raise ValueError("review_packet_artifact_id is required")
    return _github_create_pr(
        context,
        inputs,
        skill_id="github.create_pr_real",
    )


@register_executor("deployment.target.register")
def execute_deployment_target_register(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    provider = _required_text(inputs, "provider")
    target = context.deployments.create_deployment_target(
        context.connection,
        project_id=context.project_id,
        app_name=_required_text(inputs, "app_id"),
        repo=_required_text(inputs, "repo"),
        environment=_required_text(inputs, "environment"),
        provider_account_id=inputs.get("provider_account_id"),
        target_kind=str(inputs.get("target_kind") or provider),
        target_ref=str(inputs.get("target_ref") or inputs["app_id"]),
        domains=[str(item) for item in _optional_list(inputs, "domains")],
        required_resources=_optional_list(inputs, "required_resources"),
        secret_refs=[str(item) for item in _optional_list(inputs, "secret_refs")],
        health_checks=_optional_list(inputs, "health_checks"),
        rollback_points=_optional_list(inputs, "rollback_points"),
        actor=context.actor,
    )
    return {"deployment_target_id": target["id"]}


@register_executor("deployment.run.plan")
def execute_deployment_run_plan(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    run = context.deployments.plan_deployment_run(
        context.connection,
        project_id=context.project_id,
        deployment_target_id=_required_text(inputs, "deployment_target_id"),
        commit_ref=inputs.get("commit_ref"),
        release_packet={
            "artifact_id": _required_text(inputs, "release_packet_artifact_id")
        },
        expected_cost_usd_micros=int(inputs.get("expected_cost_usd_micros") or 0),
        reservation_id=inputs.get("reservation_id"),
        actor=context.actor,
    )
    return {
        "deployment_run_id": run["id"],
        "changeset_id": run.get("changeset_id"),
    }


@register_executor("model.generate_code")
def execute_model_generate_code(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """The first model-lane executor (Plane #298): one governed model call that
    turns (repo_context, task_spec, constraints) into a patch artifact + test
    plan, per the SkillSpec output schema. It never touches a repo — the patch
    is an artifact for the software-factory gates to validate. Receipt-backed
    so the budget reservation settles with the real token cost."""
    import hashlib

    from .claude_cli_adapter import make_chat_adapter
    from .core import create_artifact
    from .openai_adapter import estimate_cost_usd_micros

    task_spec = _required_text(inputs, "task_spec")
    repo_context = str(inputs.get("repo_context") or "").strip()
    constraints = [str(item) for item in _optional_list(inputs, "constraints")]

    org_id, workspace_id = context.tenant_scope()
    dedupe_key = "model.generate_code:" + hashlib.sha256(
        f"{context.project_id}:{task_spec}:{repo_context}".encode()
    ).hexdigest()
    receipt, created = context.receipts.create_or_reuse(
        context.connection,
        org_id,
        workspace_id,
        context.project_id,
        dedupe_key,
        "model.generate_code",
        {"data_boundary": "internal"},
        {"task_spec": task_spec[:2000], "constraints": constraints},
        created_by_job_id=context.created_by_job_id,
        data_boundary="internal",
    )
    if not created:
        prior = receipt.get("result") or {}
        if receipt.get("status") == "succeeded" and prior.get("patch_artifact_id"):
            return {
                "status": "reused",
                "patch_artifact_id": prior["patch_artifact_id"],
                "test_plan": prior.get("test_plan") or "",
                "receipt_id": receipt["id"],
                "actual_cost_usd_micros": 0,
            }
        # A prior failed/in-progress attempt: fall through and try again under
        # the same receipt (mark_succeeded/mark_failed below updates it).

    system_prompt = (
        "You are a senior software engineer producing a reviewable change.\n"
        "Respond with EXACTLY two sections:\n"
        "1. A unified diff inside a ```diff fenced block (git apply-able, "
        "relative paths).\n"
        "2. A line 'TEST PLAN:' followed by how to verify the change.\n"
        "No other prose. Never invent files you were not shown."
    )
    user_prompt = f"TASK:\n{task_spec}\n"
    if constraints:
        user_prompt += "\nCONSTRAINTS:\n" + "\n".join(f"- {c}" for c in constraints) + "\n"
    if repo_context:
        user_prompt += f"\nREPOSITORY CONTEXT:\n{repo_context}\n"

    try:
        message, usage = make_chat_adapter(context.settings).create_chat_completion(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        content = str(message.get("content") or "")
        patch, test_plan = _split_patch_and_test_plan(content)
        if not patch:
            raise ValueError("model returned no ```diff block")
        artifact_id = create_artifact(
            context.connection,
            context.settings,
            context.project_id,
            None,
            "code_patch",
            f"model.generate_code: {task_spec[:80]}",
            {
                "patch": patch,
                "test_plan": test_plan,
                "constraints": constraints,
                "model": usage.get("model"),
                "usage": usage,
                "data_boundary": "internal",
            },
            created_by_job_id=context.created_by_job_id,
        )
        result = {"patch_artifact_id": artifact_id, "test_plan": test_plan}
        context.receipts.mark_succeeded(
            context.connection, receipt["id"], artifact_id, result
        )
    except Exception as error:
        context.receipts.mark_failed(
            context.connection,
            receipt["id"],
            {"message": str(error), "skill_id": "model.generate_code"},
        )
        raise
    return {
        "status": "created",
        "patch_artifact_id": artifact_id,
        "test_plan": test_plan,
        "receipt_id": receipt["id"],
        "actual_cost_usd_micros": estimate_cost_usd_micros(usage),
    }


def _split_patch_and_test_plan(content: str) -> tuple[str, str]:
    """Parse the executor's required output format leniently: the first ```diff
    fence is the patch; everything after 'TEST PLAN:' is the plan."""
    patch = ""
    marker = "```diff"
    if marker in content:
        after = content.split(marker, 1)[1]
        patch = after.split("```", 1)[0].strip("\n")
    upper = content.upper()
    test_plan = ""
    if "TEST PLAN:" in upper:
        index = upper.rindex("TEST PLAN:")
        test_plan = content[index + len("TEST PLAN:"):].strip()
        test_plan = test_plan.split("```", 1)[0].strip()
    return patch, test_plan


@register_executor("deployment.health.check")
def execute_deployment_health_check(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    result = context.deployments.run_deployment_health_checks(
        context.connection,
        project_id=context.project_id,
        deployment_run_id=_required_text(inputs, "deployment_run_id"),
        actor=context.actor,
    )
    return {
        "artifact_id": result["health_artifact_id"],
        "health_status": result["deployment_run"]["status"],
        "decision_id": result.get("decision_id"),
    }
