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


@register_executor("toolchain.source.assess")
def execute_toolchain_source_assess(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    from .toolchain_registry import ToolchainRegistryService

    return ToolchainRegistryService(context.settings).assess_snapshot(
        context.connection,
        project_id=context.project_id,
        source_key=_required_text(inputs, "source_key"),
        revision=_required_text(inputs, "revision"),
        files=_optional_list(inputs, "files"),
        actor=context.actor,
    )


@register_executor("toolchain.command.guard")
def execute_toolchain_command_guard(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    from .toolchain_registry import ToolchainRegistryService

    return ToolchainRegistryService.evaluate_command(
        _required_text(inputs, "command"),
        str(inputs.get("cwd") or "").strip() or None,
    )


@register_executor("toolchain.context.pack")
def execute_toolchain_context_pack(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    from .toolchain_registry import ToolchainRegistryService

    return ToolchainRegistryService.pack_context(
        _optional_list(inputs, "chunks"),
        int(inputs.get("token_budget") or 0),
        int(inputs.get("reserved_output_tokens") or 0),
    )


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
    from .claude_cli_adapter import make_chat_adapter
    from .core import create_artifact
    from .openai_adapter import estimate_cost_usd_micros

    task_spec = _required_text(inputs, "task_spec")
    repo_context = str(inputs.get("repo_context") or "").strip()
    constraints = [str(item) for item in _optional_list(inputs, "constraints")]

    org_id, workspace_id = context.tenant_scope()
    dedupe_key = _model_generate_code_dedupe_key(
        context.project_id, task_spec, repo_context, constraints
    )
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


def _model_generate_code_dedupe_key(
    project_id: str,
    task_spec: str,
    repo_context: str,
    constraints: list[str],
) -> str:
    """Stable receipt dedupe key for model.generate_code.

    Constraints are part of the identity: omitting them caused silent wrong-reuse
    when the same task/repo was re-invoked under different constraints.
    """
    import hashlib
    import json

    material = (
        f"{project_id}:{task_spec}:{repo_context}\0"
        f"{json.dumps(constraints, ensure_ascii=False)}"
    )
    return "model.generate_code:" + hashlib.sha256(material.encode()).hexdigest()


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


# --------------------------------------------------------------------------- #
# AN OPERATOR-AUTHORED COMMAND, IN A SANDBOX OR NOWHERE.
# --------------------------------------------------------------------------- #
class SandboxIsolationUnavailable(SkillExecutorConfigurationError):
    """There is nowhere safe to run this, so it does not run."""


def _quoted(value: Any) -> str:
    """One input, safe to put in a shell command.

    The whole risk of this executor is in this function. Inputs come from a workflow's run
    input, which comes from an operator or from an earlier step — and a template built by
    concatenation turns `; rm -rf /` into a second command. `shlex.quote` is the only thing
    standing there, so nothing else in this file is allowed to build the command string.
    """
    import shlex

    return shlex.quote(str(value if value is not None else ""))


def build_sandbox_command(template: str, inputs: dict[str, Any], workdir: str = "/work") -> str:
    """The command to run, with every placeholder shell-quoted.

    Separated from execution so it can be tested without a sandbox, because the interesting
    failure is a quoting failure and it must be provable on a laptop.
    """
    import re as _re

    def swap(match: "_re.Match[str]") -> str:
        name = match.group(1)
        if name == "workdir":
            return _quoted(workdir)
        return _quoted(inputs.get(name))

    return _re.sub(r"\{([a-z][a-z0-9_]*)\}", swap, template)


def _isolation_available(settings: Settings) -> tuple[bool, str]:
    """Whether this deployment has somewhere to run a customer's command.

    The local provider is a temp directory and an allowlisted subprocess ON THE CONTROL PLANE'S
    OWN HOST. Running an operator-authored ffmpeg line there is remote code execution against
    the box that holds every project's credentials, so this refuses rather than degrades. A
    refusal an operator can read beats a capability that quietly runs in the wrong place.
    """
    provider = str(getattr(settings, "governed_sandbox_provider", "") or "").strip().lower()
    if provider in ("microvm", "firecracker", "kata", "sandbox_host"):
        url = str(getattr(settings, "sandbox_agent_url", "") or "").strip()
        if url:
            return True, provider
        return False, "the sandbox provider is set but BOSS_SANDBOX_AGENT_URL is empty"
    return False, (
        f"this deployment's sandbox provider is {provider or 'local'}, which runs on the "
        "control plane's own host. A command authored in a skill only runs where there is real "
        "isolation — set BOSS_GOVERNED_SANDBOX_PROVIDER to a microVM substrate."
    )


@register_executor("skill.sandboxed_command")
def execute_sandboxed_command(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Run one skill's declared command in an isolated sandbox.

    Everything about WHAT runs was fixed when the skill was authored — the image is pinned by
    digest, the placeholders were checked against declared inputs, the timeout is bounded and
    the network is off unless asked for. This function's only jobs are to refuse when there is
    no isolation, to quote every input, and to hand back the declared outputs.
    """
    from .skill_definitions import get_skill

    skill_key = _required_text(inputs, "_skill_key")
    skill = get_skill(context.connection, context.project_id, skill_key)
    if skill is None:
        raise SkillExecutorConfigurationError(f"no skill {skill_key!r} in this project.")
    if not skill["enabled"]:
        # Fail closed: a disabled skill that no-ops would let a workflow appear to run and do
        # nothing, which is the failure mode `validate_custom_step` already refuses to allow.
        raise SkillExecutorConfigurationError(f"skill {skill_key!r} is disabled.")

    config = skill["config"]
    ready, why = _isolation_available(context.settings)
    if not ready:
        raise SandboxIsolationUnavailable(
            f"{skill_key} runs a command and cannot be executed here: {why}")

    missing = [entry["name"] for entry in skill["inputs"]
               if entry.get("required") and not str(inputs.get(entry["name"], "")).strip()]
    if missing:
        raise SkillExecutorConfigurationError(
            f"{skill_key} needs {', '.join(missing)}.")

    command = build_sandbox_command(str(config.get("command") or ""), inputs)
    from .sandbox_provider import make_sandbox_provider

    sandbox = make_sandbox_provider(context.settings).acquire(
        task_id=f"skill-{skill_key}-{make_id('sbx')[-8:]}")
    try:
        result = sandbox.run(command, timeout=int(config.get("timeout_seconds") or 300))
        produced = {entry["name"]: entry.get("path") or entry["name"]
                    for entry in (config.get("outputs") or [])}
        return {
            "skill_key": skill_key,
            "image": config.get("image"),
            "exit_code": getattr(result, "exit_code", None) or (
                result.get("exit_code") if isinstance(result, dict) else None),
            "produced": produced,
            # The tail only. A command's whole stdout can be a video's worth of progress lines.
            "log_tail": str(getattr(result, "stdout", "") or (
                result.get("stdout", "") if isinstance(result, dict) else ""))[-4000:],
        }
    finally:
        sandbox.teardown()
