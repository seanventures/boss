"""Server-side executor registry for real SkillSpec implementations.

The registry only resolves deterministic, explicitly registered bindings.  It does
not execute anything by itself; callers such as ``SkillRuntimeService`` still own
permission, budget, retry, lease, and policy gates.  External GitHub effects add a
Receipt at this boundary so a future real-mode caller cannot bypass effect evidence.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
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
    #: The APPROVED decision this invocation runs under, when a person was asked first — the
    #: agent run's resume decision or the workflow step's gate. Read by the MCP effect
    #: (mcp_registry.call_tool_with_receipt), which refuses an unreviewed server's tool
    #: without one; None for everything that ran without asking.
    approved_by: Optional[str] = None
    #: What that decision has to be ABOUT, as fields of its recommendation —
    #: ``{"run_id": …}`` or ``{"workflow_run_id": …, "step_id": …}``. Round-3 review: an
    #: approved decision about some other run would otherwise have counted.
    approval_about: Optional[dict[str, Any]] = None
    #: WHO the skill runtime let through its permission gate (a role key, or `workflow_runner`).
    #: Set by SkillRuntimeService.invoke. skill.mcp_tool reads it: the caller must hold the
    #: server the skill calls (agents.mcp_reach_gap).
    caller: Optional[str] = None
    #: THE NAMED ROAD on which this invocation may reach an MCP server its caller does not hold
    #: (agents.SERVERLESS_MCP_GRANTS), or None — the rule. Set only by the code that IS that
    #: road: the legacy custom-step runner and the agent loop's own offered tools.
    mcp_reach: Optional[str] = None

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
    *,
    requires: Any = (),
) -> Callable[[RegisteredExecutor], RegisteredExecutor] | RegisteredExecutor:
    """Register an executor directly or as ``@register_executor(skill_id)``.

    A skill that calls a model gets it from `model_routing.project_chat_adapter` — the model
    this project connected. `requires` is ONLY for what no chat model can do (image generation,
    speech-to-text): name the `model_routing.PROVIDER_REQUIREMENTS` key, readiness checks it
    before a run starts, and the executor reads the key with `model_routing.provider_credential`.
    """
    from . import model_routing

    normalized = skill_id.strip()
    if not normalized:
        raise ValueError("skill_id must not be empty")

    def decorator(handler: RegisteredExecutor) -> RegisteredExecutor:
        if normalized in _EXECUTORS and _EXECUTORS[normalized] is not handler:
            raise ValueError(f"Executor already registered for '{normalized}'.")
        model_routing.declare(normalized, requires)
        _EXECUTORS[normalized] = handler
        return handler

    return decorator(executor) if executor is not None else decorator


def resolve_executor(
    skill_id: str,
    *,
    context: Optional[SkillExecutorContext] = None,
) -> Optional[SkillExecutor]:
    """Resolve a bound one-argument callable, or ``None`` for unbacked skills."""

    key = skill_id.strip()
    handler = _EXECUTORS.get(key)

    if handler is None:
        # AN AUTHORED SKILL RUNS THE EXECUTOR FOR ITS BEHAVIOUR.
        #
        # THIS WIRE WAS MISSING ENTIRELY. `skill.sandboxed_command` has been registered since
        # authored skills shipped, and nothing ever called it: an authored skill resolves by its
        # own key, no executor is registered under a key a customer invented, and the invocation
        # failed with "No server-side executor is registered for skill 'my_thing'". So the whole
        # point of authoring a skill — that a customer can add a capability without a release —
        # did not work, and the failure named the skill rather than the missing wire.
        #
        # The behaviour is what says how to run it, so the behaviour is what picks the executor.
        # `_skill_key` is passed along because those executors load the definition themselves:
        # the source, the image and the timeout are facts about the project, and re-reading them
        # at execution means a skill edited between publish and run behaves as edited.
        authored = _authored_skill(context, key)
        if authored is None:
            return None
        behaviour = str(authored.get("behaviour_kind") or "").strip()
        if behaviour == "builtin":
            # A `builtin` skill NAMES an executor BOSS already ships. It is a wrapper that gives
            # an existing capability knowledge and a risk tier, so it runs that executor with the
            # inputs untouched — there is no behaviour executor to reach for.
            named = str((authored.get("config") or {}).get("executor_id") or "").strip()
            handler = _EXECUTORS.get(named)
            if handler is None:
                return None
        else:
            handler = _EXECUTORS.get(f"skill.{behaviour}")
            if handler is None:
                return None

        wrapping = behaviour != "builtin"

        def execute_authored(inputs: dict[str, Any]) -> dict[str, Any]:
            if context is None:
                raise SkillExecutorConfigurationError(
                    f"Skill '{key}' requires a server-side executor context.")
            return handler(context, {**inputs, "_skill_key": key} if wrapping else inputs)

        return execute_authored

    def execute(inputs: dict[str, Any]) -> dict[str, Any]:
        if context is None:
            raise SkillExecutorConfigurationError(
                f"Skill '{skill_id}' requires a server-side executor context."
            )
        return handler(context, inputs)

    return execute


def _authored_skill(
    context: Optional[SkillExecutorContext], skill_key: str
) -> Optional[dict[str, Any]]:
    """The definition of a skill this project authored, or None if it authored no such skill.

    Swallows its own failures on purpose: resolution runs on every invocation, and a skill that
    simply has no executor must come back as "unbacked" rather than as a database error.
    """
    if context is None or getattr(context, "connection", None) is None:
        return None
    try:
        from .skill_definitions import get_skill

        return get_skill(context.connection, context.project_id, skill_key) or None
    except Exception:  # noqa: BLE001
        return None


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


@register_executor("mobile.device.execute")
def execute_mobile_device(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """One typed mobile action per leased job, with a durable dedupe receipt."""
    from .core import json_hash
    from .mobile_device import LocalMobileDeviceAdapter, MobileDeviceError

    if not context.created_by_job_id:
        raise MobileDeviceError("mobile actions require a leased skill.execute job")
    org_id, workspace_id = context.tenant_scope()
    receipt, created = context.receipts.create_or_reuse(
        context.connection,
        org_id,
        workspace_id,
        context.project_id,
        f"mobile.device.execute:{context.created_by_job_id}",
        "mobile.device.action",
        {
            "target": str(inputs.get("target") or ""),
            "device_id": str(inputs.get("device_id") or ""),
            "data_boundary": "internal",
        },
        {
            "action": str(inputs.get("action") or ""),
            # Receipts persist only a fingerprint. Text can be a password or
            # personal data, and must not enter receipt/event metadata.
            "inputs_sha256": json_hash(inputs),
        },
        created_by_job_id=context.created_by_job_id,
        data_boundary="internal",
    )
    if not created:
        if receipt["status"] == "succeeded":
            return {
                **(receipt.get("result") or {}),
                "receipt_id": receipt["id"],
                "actual_cost_usd_micros": 0,
                "reused": True,
            }
        raise MobileDeviceError(
            f"mobile action receipt {receipt['id']} is {receipt['status']}; "
            "the device effect will not be replayed automatically"
        )
    try:
        result = LocalMobileDeviceAdapter(context).execute(inputs, receipt["id"])
        context.receipts.mark_succeeded(
            context.connection, receipt["id"], str(inputs["device_id"]), result
        )
    except Exception as exc:
        context.receipts.mark_failed(
            context.connection,
            receipt["id"],
            {"reason": str(exc)[:1000], "action": str(inputs.get("action") or "")},
        )
        raise
    return {
        **result,
        "receipt_id": receipt["id"],
        "actual_cost_usd_micros": 0,
    }


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
    from .core import create_artifact
    from . import model_routing
    from .model_routing import project_chat_adapter
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
        # THE PROJECT'S MODEL. Building the adapter from the bare deployment settings read
        # `llm_engine` ("auto" = OpenAI) and failed on a subscription-only install.
        with _routed(context, "implement", user_prompt):
            adapter = project_chat_adapter(
                context.connection, context.settings, context.project_id,
            )
        message, usage = adapter.create_chat_completion(
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
        "included_in_subscription": model_routing.call_is_included(
            context.connection, context.settings, context.project_id, usage),
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
    from .core import make_id
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


@register_executor("skill.mcp_tool")
def execute_mcp_tool(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Call the one MCP tool an authored ``mcp_tool`` skill names.

    THIS EXECUTOR DID NOT EXIST. ``resolve_executor`` maps an authored skill to
    ``skill.<behaviour>``, and only ``sandboxed_command`` and ``agent_program`` were registered,
    so the documented way to give an agent an MCP integration — "the server holds the
    integration, the skill names one of its tools" — saved fine, showed as held, and failed on
    its first run with "No server-side executor is registered".

    SAME GOVERNANCE AS AN AGENT'S DIRECT MCP CALL (``agent_loop._invoke_mcp_skill``). The caller
    has already been through SkillRuntimeService — permission (the role_skills grant), the
    budget reservation and retry — at the skill's catalogue tier, which is never below the
    server's tier for this tool (skill_catalogue's risk rule). What is left is the EFFECT, and
    that goes through ``mcp_registry.call_tool_with_receipt``, the same function the agent loop
    uses: a receipt before the call, deduplicated, settled after it — and, for a server nobody
    has reviewed, a refusal unless this invocation carries an approved decision
    (``context.approved_by``). Round-2 review: this road ran an unreviewed server's tool with
    no person asked while the direct agent call waited for one.

    The definition and the server are re-read here, at execution, so a skill re-pointed or a
    server turned off between publishing a workflow and running it behaves as it is now.
    """
    import hashlib
    import json as _json

    from .core import make_id
    from .mcp_registry import call_tool_with_receipt, get_mcp_server
    from .skill_definitions import get_skill

    skill_key = _required_text(inputs, "_skill_key")
    skill = get_skill(context.connection, context.project_id, skill_key)
    if skill is None:
        raise SkillExecutorConfigurationError(f"no skill {skill_key!r} in this project.")
    if not skill["enabled"]:
        raise SkillExecutorConfigurationError(
            f"skill {skill_key!r} is turned off. Turn it on on the Skills and MCP page.")
    config = skill["config"] or {}
    server = get_mcp_server(context.connection, context.project_id,
                            str(config.get("mcp_server_id") or ""))
    if server is None:
        raise SkillExecutorConfigurationError(
            f"{skill_key} calls an MCP server that has been removed. Edit the skill on the "
            "Skills and MCP page and choose a server.")
    if not server["enabled"]:
        raise SkillExecutorConfigurationError(
            f"{skill_key} calls MCP server '{server['name']}', which is turned off. Turn it on "
            "on the Skills and MCP page.")
    tool_name = str(config.get("tool_name") or "").strip()
    if not tool_name:
        raise SkillExecutorConfigurationError(f"{skill_key} names no tool to call.")
    # THE CALLER MUST HOLD THE SERVER (review of milestone/integration). The runtime's gate
    # already asks; this is the backstop where the effect happens, so a road that reaches the
    # executor some other way still cannot use a server through a skill. No caller on the
    # context means nobody vouched for one, which is a refusal, not a pass.
    from .agents import mcp_grant_gap, serverless_mcp_allowed

    caller = str(context.caller or "").strip()
    if not serverless_mcp_allowed(caller, context.mcp_reach):
        gap = (mcp_grant_gap(context.connection, context.project_id, caller,
                             server_id=str(server["id"]), tool=tool_name, skill_key=skill_key)
               if caller else {"message": (
                   f"{skill_key} calls the MCP server '{server['name']}' and this invocation "
                   "names no agent holding it. Run it as an agent that holds the server "
                   "(Agent Studio, the agent's Loadout › MCP › + Add).")})
        if gap is not None:
            raise SkillExecutorConfigurationError(gap["message"])

    # The runtime's own reserved keys never reach a third party's tool.
    arguments = {k: v for k, v in inputs.items() if not str(k).startswith("_")}
    digest = hashlib.sha256(_json.dumps(arguments, sort_keys=True, default=str,
                                        separators=(",", ":")).encode()).hexdigest()[:20]
    # A job retried is the same effect and must not be repeated; two separate calls with the
    # same arguments are two effects, so without a job there is nothing to deduplicate against.
    occasion = context.created_by_job_id or make_id("call")
    called = call_tool_with_receipt(
        context.connection, context.settings, context.project_id, server, tool_name,
        arguments, dedupe_key=f"mcp:skill:{skill_key}:{occasion}:{digest}",
        tenant_scope=context.tenant_scope(), created_by_job_id=context.created_by_job_id,
        # An unreviewed server's tool needs the approval this run was given (needs_person).
        approved_by=context.approved_by,
        approval_about=context.approval_about,
    )
    output = called.get("output")
    return {
        **(output if isinstance(output, dict) else {"output": output}),
        "_mcp": {"skill_key": skill_key, "server": server["name"], "tool": tool_name,
                 "receipt_id": called.get("receipt_id"),
                 **({"deduplicated": True} if called.get("deduplicated") else {})},
    }


#: Languages an agent may write. A fixed set, because each one is an interpreter that has to
#: exist in the sandbox image, and "whatever you like" would fail at run time on the third one.
_AGENT_LANGUAGES = {
    "python": ("program.py", ["python3", "program.py"]),
    "bash": ("program.sh", ["bash", "program.sh"]),
    "sh": ("program.sh", ["sh", "program.sh"]),
}

#: Bounded, because an agent's own estimate of how long its code needs is not evidence.
_AGENT_CODE_TIMEOUT_MAX = 300


@register_executor("code.write_and_run")
def execute_code_write_and_run(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Run a program the agent just wrote, in the container boundary and nowhere else.

    WHY THIS EXISTS. Everything else an agent can do had to be shipped as a skill first, which
    means a customer asking for something new waits for a release. This closes the class: if the
    work is a computation and no skill covers it, the agent writes the computation — and if it
    worked, `code.remember` keeps it so the next run does not pay for it again.

    WHAT IS NOT NEGOTIABLE — all of it enforced in `_run_a_program`
    ---------------------------------------------------------------
    * THE BOUNDARY IS THE CONTAINER, and it fails closed: no network, non-root, every Linux
      capability dropped, no-new-privileges, hard memory/CPU/PID limits. Where that is not
      configured this REFUSES rather than degrading to the control plane's own host — which is
      where an agent's shell reached a production DATABASE_URL and cleared 204 tables on
      2026-09-02.
    * THE SOURCE IS WRITTEN TO A FILE, never passed through a shell, so a program containing a
      quote is not a different program.
    * A SCRATCH DIRECTORY PER CALL, discarded after. Two invocations share nothing.
    * TIER 2, so a person approves it. Code written a moment ago is not a skill a human authored
      and pinned by digest.
    """
    why = _required_text(inputs, "why")
    result = _run_a_program(
        context,
        language=str(inputs.get("language") or "python").strip().lower(),
        source=str(inputs.get("source") or ""),
        files=inputs.get("files") or {},
        timeout=max(5, min(int(inputs.get("timeout_seconds") or 60), _AGENT_CODE_TIMEOUT_MAX)),
        label="code.write_and_run",
    )
    return {"why": why, **result}


def _run_a_program(
    context: SkillExecutorContext,
    *,
    language: str,
    source: str,
    files: dict[str, Any],
    timeout: int,
    label: str,
) -> dict[str, Any]:
    """Run one program in the container boundary and nowhere else.

    Shared by the skill that writes a program and the skill that reruns a remembered one,
    because the boundary must not have two implementations — the second one is always the one
    that forgets to drop a capability.
    """
    import json as _json
    import shutil
    import tempfile
    from pathlib import Path

    from .container_runner import ContainerCommandRunner, ContainerUnavailable

    if language not in _AGENT_LANGUAGES:
        raise SkillExecutorConfigurationError(
            f"{language!r} is not a language this runs. Use one of: "
            f"{', '.join(sorted(_AGENT_LANGUAGES))}.")
    if not source.strip():
        raise SkillExecutorConfigurationError("source is required — the program to run.")

    runtime = str(getattr(context.settings, "sandbox_command_runtime", "") or "").strip().lower()
    if runtime != "docker":
        # SAID, NOT SILENTLY WEAKENED. An operator reading this knows exactly what to set and
        # what the alternative would have cost them.
        raise SandboxIsolationUnavailable(
            f"{label} needs a real boundary and this deployment has none: "
            f"BOSS_SANDBOX_COMMAND_RUNTIME is {runtime or 'unset'}, so a program would run in "
            "the control plane's own container with its filesystem and network. Set it to "
            "'docker'. It will not run anywhere else.")

    filename, argv = _AGENT_LANGUAGES[language]
    root = Path(tempfile.mkdtemp(prefix="boss-agent-code-"))
    try:
        (root / filename).write_text(source, encoding="utf-8")
        # FILES THE CALLER BRINGS WITH IT. How a program receives what an earlier skill
        # produced, without the program needing to reach anything.
        for name, contents in (files or {}).items():
            safe = Path(str(name)).name          # no traversal: one flat directory, by design
            if not safe or safe.startswith("."):
                continue
            (root / safe).write_text(str(contents), encoding="utf-8")

        try:
            with ContainerCommandRunner(root, context.settings,
                                        task_id="agentcode") as runner:
                outcome = runner.run(" ".join(argv), timeout=timeout)
        except ContainerUnavailable as error:
            raise SandboxIsolationUnavailable(
                f"{label} could not get an isolated sandbox: {error}") from error

        stdout = str(getattr(outcome, "stdout", "") or "")
        # A PROGRAM THAT PRINTS JSON BECOMES A STEP THAT PRODUCES NAMED VALUES. That is what
        # lets a later step wire from this one; anything else is a wall of text.
        produced: dict[str, Any] = {}
        try:
            parsed = _json.loads(stdout.strip() or "null")
            if isinstance(parsed, dict):
                produced = parsed
        except ValueError:
            pass

        return {
            "language": language,
            "exit_code": int(getattr(outcome, "exit_code", 1) or 0),
            "stdout": stdout[-8000:],
            "stderr": str(getattr(outcome, "stderr", "") or "")[-4000:],
            "result": produced,
            "timed_out": bool(getattr(outcome, "timed_out", False)),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


@register_executor("skill.guide")
def execute_guide(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Return a guide's body: an operator's playbook, read when the agent needs it.

    NO NETWORK, NO SIDE EFFECTS, NO GRANT. The result says so in its first line, so a playbook
    that reads like permission is still framed as an explanation (agent-loadout spec §4.6).
    The body is re-checked with today's lint: one that fails is not handed to the model.
    """
    from .agent_instructions import trusted
    from .skill_definitions import get_skill

    skill_key = _required_text(inputs, "_skill_key")
    skill = get_skill(context.connection, context.project_id, skill_key)
    if skill is None:
        raise SkillExecutorConfigurationError(f"no skill {skill_key!r} in this project.")
    if not skill["enabled"]:
        raise SkillExecutorConfigurationError(f"skill {skill_key!r} is disabled.")
    body = str(skill.get("knowledge") or "")
    refused = trusted(body)
    if refused:
        raise SkillExecutorConfigurationError(
            f"guide {skill_key!r} fails the instructions check ({refused}); edit it first.")
    label = str(skill.get("label") or skill_key)
    return {"text": f"Guide: {label}. It explains how; it grants nothing.\n\n{body}"}


@register_executor("skill.agent_program")
def execute_agent_program(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Rerun a program an agent wrote on an earlier run and the workflow kept.

    THE POINT OF THE WHOLE ARRANGEMENT. The first run of a workflow pays a model to work out how
    to do something no skill covered. Every run after it reuses the program — no model call, the
    same result, and an operator can read the code that will run before it runs. An agent that
    improvises every time is expensive and non-deterministic; one that accumulates is neither.
    """
    from .skill_definitions import get_skill

    skill_key = _required_text(inputs, "_skill_key")
    skill = get_skill(context.connection, context.project_id, skill_key)
    if skill is None:
        raise SkillExecutorConfigurationError(f"no skill {skill_key!r} in this project.")
    if not skill["enabled"]:
        raise SkillExecutorConfigurationError(f"skill {skill_key!r} is disabled.")

    config = skill["config"] or {}
    missing = [entry["name"] for entry in skill["inputs"]
               if entry.get("required") and not str(inputs.get(entry["name"], "")).strip()]
    if missing:
        raise SkillExecutorConfigurationError(f"{skill_key} needs {', '.join(missing)}.")

    # THE DECLARED INPUTS BECOME FILES THE PROGRAM CAN READ, one per input, plus inputs.json
    # holding all of them. A remembered program cannot take arguments — it was written against a
    # shape, and the shape is what the skill declares.
    import json as _json

    supplied = {entry["name"]: inputs.get(entry["name"])
                for entry in skill["inputs"] if entry["name"] in inputs}
    files = {"inputs.json": _json.dumps(supplied, default=str)}

    result = _run_a_program(
        context,
        language=str(config.get("language") or "python"),
        source=str(config.get("source") or ""),
        files=files,
        timeout=max(5, min(int(config.get("timeout_seconds") or 60), _AGENT_CODE_TIMEOUT_MAX)),
        label=skill_key,
    )
    return {"skill_key": skill_key, "reused": True,
            "written_by": config.get("written_by") or "", **result}


def _remembered_risk_tier(inputs: dict[str, Any]) -> int:
    """The tier a remembered program is stored at. Never below the approval floor.

    `MANUAL_APPROVAL_RISK_FLOOR` is 2. Anything the model passes below that would
    save a program that runs with no person in the loop.
    """
    raw = inputs.get("risk_tier")
    try:
        tier = int(raw) if raw not in (None, "") else 2
    except (TypeError, ValueError):
        tier = 2
    if isinstance(raw, bool):
        tier = 2
    return max(2, min(tier, 3))


@register_executor("code.remember")
def execute_code_remember(
    context: SkillExecutorContext,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    """Keep a program the agent just wrote, so the next run reuses it instead of rewriting it.

    WHY THE AGENT DOES THIS AND NOT BOSS. Only the agent knows whether the program it just ran is
    the general answer or a one-off hack that happened to work on this input. Saving is therefore
    a decision it makes deliberately, with a name and a description it has to write — and an
    operator can read, edit, disable or delete what it saved, because it lands in exactly the
    same place as a skill a person authored.

    IT IS NOT AUTOMATICALLY GIVEN TO ANYBODY. Saving records the program; the grant is still a
    separate, explicit act. An agent cannot widen its own reach by writing code.
    """
    from .skill_definitions import SkillDefinitionError, save_skill

    key = _required_text(inputs, "skill_key")
    language = str(inputs.get("language") or "python").strip().lower()
    source = str(inputs.get("source") or "")
    try:
        saved = save_skill(
            context.connection,
            project_id=context.project_id,
            actor=context.actor,
            skill_key=key,
            label=str(inputs.get("label") or key.replace("_", " ")),
            summary=_required_text(inputs, "summary"),
            behaviour_kind="agent_program",
            knowledge=str(inputs.get("knowledge") or ""),
            inputs=inputs.get("inputs") or [],
            produces=inputs.get("produces") or [],
            config={
                "language": language,
                "source": source,
                "timeout_seconds": int(inputs.get("timeout_seconds") or 60),
                "written_by": context.actor,
                "written_for": str(inputs.get("written_for") or ""),
                "written_in_run": str(inputs.get("written_in_run") or ""),
            },
            # AT LEAST TIER 2. A program an agent wrote is approved by a person the first
            # time it is used. The model can pass `risk_tier` even though the schema does
            # not list it, and `code.remember` itself is tier 1, so it runs without that
            # person. Storing the passed tier is how the click disappears: 1 saves a
            # program that later runs unattended. A higher tier is kept.
            risk_tier=_remembered_risk_tier(inputs),
            trusted=False,
        )
    except SkillDefinitionError as error:
        raise SkillExecutorConfigurationError(str(error)) from error
    return {"skill_key": key, "saved": True, "version": saved.get("updated_at"),
            "note": "kept for the next run. Somebody still has to give it to an agent."}


# ---------------------------------------------------------------------------
# Media: editing a video, and publishing it
# ---------------------------------------------------------------------------
#
# EVERY STEP TAKES A DRIVE FILE AND LEAVES A DRIVE FILE.
#
# Steps cannot hand each other a local path. Each runs in its own invocation, possibly on a
# different worker, and the working directory is gone by the time the next one starts. So the
# unit passed between them is an ARTIFACT naming a Drive file — which has the useful side
# effect that an operator can watch the output of any intermediate step without BOSS building a
# viewer for it.
#
# The download/edit/upload round trip is real cost (a 135 MB video, twice over the wire per
# step) and it is the price of steps that are independently retryable and independently
# inspectable. A single fused "edit everything" step would be faster and would fail as one
# opaque unit.


def _media_artifact(context: "SkillExecutorContext", artifact_id: str) -> dict[str, Any]:
    """The media file an artifact points at: kept in BOSS's own storage, or in Drive."""
    from sqlalchemy import text as _sql

    row = context.connection.execute(
        _sql("SELECT content FROM artifacts WHERE id = :i AND project_id = :p"),
        {"i": artifact_id, "p": context.project_id},
    ).mappings().first()
    if not row:
        raise ValueError(f"no artifact {artifact_id!r} in this project")
    content = row["content"] or {}
    if not content.get("drive_file_id") and not content.get("local_path"):
        raise ValueError(
            f"artifact {artifact_id!r} is not a media file — it names no stored or Drive file")
    return content


def _local_media(context: "SkillExecutorContext", content: dict[str, Any], work: str,
                 inputs: dict[str, Any]) -> str:
    """A local path for a media artifact, wherever it is kept (media_storage.materialize).

    Only a Drive-kept artifact needs Google; one BOSS keeps itself is read in place.
    """
    from .media_storage import materialize

    return materialize(context.connection, context.settings, project_id=context.project_id,
                       content=content, dest_dir=work,
                       connection_key=str(inputs.get("connection_key") or "").strip() or None)


def _output_provider(inputs: dict[str, Any]) -> Optional[str]:
    """Where the workflow ASKED for a result to be kept, if it asked at all.

    Naming a Drive output folder is asking for Drive. Naming nothing is not asking for anything,
    and the result stays in BOSS's own storage — Google is not a precondition of editing.
    """
    named = str(inputs.get("store_provider") or "").strip().lower()
    if named:
        return named
    return "google" if str(inputs.get("output_folder_id") or "").strip() else None


def _source_storage(content: Optional[dict[str, Any]]) -> Optional[str]:
    """Where a media artifact's file is kept: `google` (Drive) or `boss`. Older artifacts
    predate the `storage` field and were all Drive files."""
    content = content or {}
    if content.get("storage"):
        return str(content["storage"])
    if content.get("local_path"):
        return "boss"
    return "google" if content.get("drive_file_id") else None


def _store_media(context: "SkillExecutorContext", inputs: dict[str, Any], path: str,
                 name: str, *, source_storage: Optional[str] = None) -> dict[str, Any]:
    """Keep a result: where the step asked, else beside a Drive source, else in BOSS
    (media_storage.store makes that decision, with the Google-connected check)."""
    from .media_storage import store

    return store(context.connection, context.settings, project_id=context.project_id,
                 local_path=path, name=name, provider_key=_output_provider(inputs),
                 container=str(inputs.get("output_folder_id") or ""),
                 connection_key=str(inputs.get("connection_key") or "").strip() or None,
                 actor=context.actor, source_storage=source_storage)


def _where_stored(artifact_id: str, written: dict[str, Any]) -> dict[str, Any]:
    """What every media step reports about the file it kept, whichever storage holds it."""
    from .media_storage import media_url

    local = bool(written.get("local_path"))
    out = {
        "drive_file_id": written.get("file_id"),
        "view_url": media_url(artifact_id) if local else written.get("view_url"),
        "stored_in": "boss" if local else str(written.get("provider") or "google"),
    }
    if written.get("note"):
        out["storage_note"] = written["note"]
    return out


def _edit_in_drive(
    context: "SkillExecutorContext",
    inputs: dict[str, Any],
    *,
    operation: str,
    apply_edit,
    extra_downloads: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Download, edit, verify, upload, record. The shape every video step shares.

    Written once because the interesting part of each operation is the ffmpeg call, and five
    copies of the download/upload scaffolding is five places for the cleanup to be forgotten.
    """
    from .drive_media import record_artifact, workspace
    from .video_edit import verify_playable

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    source = _media_artifact(context, source_artifact)

    with workspace(f"boss-{operation}-") as work:
        local = _local_media(context, source, work, inputs)

        # Clips this operation also needs (an intro, an outro, a logo), each named by its own
        # artifact so they are versioned and inspectable like anything else.
        fetched: dict[str, str] = {}
        for field in extra_downloads:
            artifact_id = str(inputs.get(field) or "").strip()
            if not artifact_id:
                continue
            asset = _media_artifact(context, artifact_id)
            fetched[field] = _local_media(context, asset, work, inputs)

        edited = apply_edit(local, work, fetched)
        # VERIFIED BEFORE IT IS UPLOADED. ffmpeg exits 0 on empty and silent output, and an
        # unwatchable file that reached Drive looks exactly like a successful step.
        info = verify_playable(edited, expect_audio=bool(inputs.get("expect_audio", True)))

        name = str(inputs.get("output_name") or "").strip() or os.path.basename(edited)
        written = _store_media(context, inputs, edited, name,
                               source_storage=_source_storage(source))

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_video", drive_file=written, source_artifact_id=source_artifact,
        detail={"operation": operation, **info},
    )
    return {
        "media_artifact_id": artifact_id,
        **_where_stored(artifact_id, written),
        "duration_seconds": info.get("duration_seconds"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


@register_executor("video.trim")
def execute_video_trim(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    from .video_edit import trim

    return _edit_in_drive(
        context, inputs, operation="trim",
        apply_edit=lambda src, work, _extra: trim(
            src, start=str(inputs.get("start") or "0"),
            end=str(inputs.get("end") or ""), work_dir=work),
    )


@register_executor("video.watermark")
def execute_video_watermark(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    from .video_edit import watermark

    if not str(inputs.get("logo_artifact_id") or "").strip():
        raise ValueError("logo_artifact_id is required — the image to overlay")
    return _edit_in_drive(
        context, inputs, operation="watermark",
        extra_downloads=("logo_artifact_id",),
        apply_edit=lambda src, work, extra: watermark(
            src, image=extra["logo_artifact_id"], work_dir=work,
            corner=str(inputs.get("corner") or "top_right"),
            scale=float(inputs.get("scale") or 0.12),
            margin=int(inputs.get("margin") or 24),
            opacity=float(inputs.get("opacity") or 1.0)),
    )


@register_executor("video.bookend")
def execute_video_bookend(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Join a title card before and/or an ending slide after. Either may be a picture.

    `seconds` is how long a STILL is held on screen. Ignored for a clip, which has its own
    length — so it is optional rather than conditional on something the caller has to know.
    """
    from .video_edit import STILL_SECONDS_DEFAULT, bookend

    if not (str(inputs.get("intro_artifact_id") or "").strip()
            or str(inputs.get("outro_artifact_id") or "").strip()):
        raise ValueError("bookend needs an intro_artifact_id, an outro_artifact_id, or both")
    held = float(inputs.get("seconds") or STILL_SECONDS_DEFAULT)
    return _edit_in_drive(
        context, inputs, operation="bookend",
        extra_downloads=("intro_artifact_id", "outro_artifact_id"),
        apply_edit=lambda src, work, extra: bookend(
            src, work_dir=work,
            intro=extra.get("intro_artifact_id"),
            outro=extra.get("outro_artifact_id"),
            still_seconds=held),
    )


@register_executor("video.cut")
def execute_video_cut(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Remove ranges from the middle and join what is left.

    `remove` arrives as a list of {start, end} because "take out the bit where I fumble" is
    usually more than one bit, and asking for one call per removal would re-encode the whole
    video once per cut.
    """
    from .video_edit import cut

    ranges = inputs.get("remove")
    if isinstance(ranges, dict):        # a single range, written the obvious way
        ranges = [ranges]
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("remove is required — a list of {start, end} ranges to take out")
    return _edit_in_drive(
        context, inputs, operation="cut",
        apply_edit=lambda src, work, _extra: cut(src, remove=ranges, work_dir=work),
    )


@register_executor("video.reframe")
def execute_video_reframe(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Landscape to portrait, by cropping.

    The frame changes here, so this is the one editing step whose output dimensions differ from
    its input's — which is why the recorded artifact carries width and height.
    """
    from .video_edit import reframe

    aspect = str(inputs.get("aspect") or "").strip()
    if not aspect:
        raise ValueError("aspect is required — 9:16, 16:9, 1:1 or 4:5")
    return _edit_in_drive(
        context, inputs, operation="reframe",
        apply_edit=lambda src, work, _extra: reframe(
            src, aspect=aspect, work_dir=work,
            strategy=str(inputs.get("strategy") or "center")),
    )


@register_executor("video.overlay_image")
def execute_video_overlay_image(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Show an image over part of the video while the audio keeps playing.

    NOT video.watermark WITH A TIME WINDOW. A watermark is small, cornered and present
    throughout; this is a visual that replaces most of the frame for a few seconds, and the
    audio continuing underneath is the point of it.
    """
    from .video_edit import overlay_image

    if not str(inputs.get("image_artifact_id") or "").strip():
        raise ValueError("image_artifact_id is required — the image to pop up")
    return _edit_in_drive(
        context, inputs, operation="overlay_image",
        extra_downloads=("image_artifact_id",),
        apply_edit=lambda src, work, extra: overlay_image(
            src, image=extra["image_artifact_id"], work_dir=work,
            start=str(inputs.get("start") or "0"),
            end=str(inputs.get("end") or ""),
            position=str(inputs.get("position") or "center"),
            scale=float(inputs.get("scale") or 0.6)),
    )


@register_executor("video.caption")
def execute_video_caption(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Burn a line of text over a window of the video.

    The text is the only free-form operator string in the whole capability. video_edit.caption
    writes it to a file and hands ffmpeg the path, so nothing in it can be read as a filter
    option — see that function for why that is not optional.
    """
    from .video_edit import caption

    text = str(inputs.get("text") or "").strip()
    if not text:
        raise ValueError("text is required — the line to put on screen")
    return _edit_in_drive(
        context, inputs, operation="caption",
        apply_edit=lambda src, work, _extra: caption(
            src, text=text, work_dir=work,
            start=str(inputs.get("start") or "0"),
            end=str(inputs.get("end") or ""),
            style=str(inputs.get("style") or "lower_third"),
            size=int(inputs.get("size") or 48)),
    )


@register_executor("model.generate_text")
def execute_model_generate_text(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Write something, using the model this project chose.

    DECLARED SINCE THE SPECPACK WAS WRITTEN AND NEVER BOUND. Every workflow that wanted a step
    to draft, summarise or write anything had to bring its own skill; the one general-purpose
    writing step in the catalogue could not run.
    """
    from .model_routing import project_chat_adapter

    prompt = str(inputs.get("prompt") or inputs.get("message") or "").strip()
    if not prompt:
        raise ValueError("prompt is required — say what should be written")

    # MATERIAL FROM AN EARLIER STEP, appended rather than interpolated.
    #
    # A workflow step wires whole VALUES — it cannot build "write a newsletter from: {dossier}"
    # out of two of them, and a template language in the binding would be a second, worse
    # programming language inside a JSON field. So the standing instruction is `prompt` and
    # whatever an earlier step produced is `context`, and the step concatenates them once.
    material = inputs.get("context") or inputs.get("material") or ""
    if not isinstance(material, str):
        import json as _json

        material = _json.dumps(material, indent=2, default=str)
    material = material.strip()
    if material:
        prompt = f"{prompt}\n\n--- material ---\n{material}"

    system = str(inputs.get("system") or inputs.get("instructions") or "").strip()
    with _routed(context, "writing", prompt):
        adapter = project_chat_adapter(
            context.connection, context.settings, context.project_id,
        )
    _ref, text_out, usage = adapter.create_text_response(prompt, system_prompt=system)
    body = str(text_out or "").strip()
    if not body:
        raise ValueError("the model returned nothing to write")
    return {"text": body, "characters": len(body),
            "model": (usage or {}).get("model")}


@register_executor("email.send_real_guarded")
@register_executor("email.send_real")
def execute_email_send(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Send an email through the configured SMTP server.

    The adapter has existed all along; nothing bound it to the skill, so a workflow could
    declare a sending step and never send. Both skill ids share this executor because they are
    the same act — the difference between them is POLICY (the guarded one requires an approval
    in its own spec), and policy is enforced before an executor is reached, not inside it.

    REFUSES RATHER THAN PRETENDS. An install without SMTP configured gets a message naming the
    setting that is missing, not a success with nothing sent.
    """
    from .mail_transport import send

    to = str(inputs.get("to") or inputs.get("channel") or "").strip()
    if not to:
        raise ValueError("to is required — the address this goes to")
    body = str(inputs.get("body") or inputs.get("text") or "").strip()
    if not body:
        raise ValueError("body is required — an empty email is not a message")
    subject = str(inputs.get("subject") or "").strip() or None

    # THIS PROJECT'S MAIL SERVER, falling back to the environment. An install that configured
    # SMTP with env vars keeps working; one that set it up in the console no longer needs to.
    # THE ADAPTER TAKES A CHANNEL, NOT AN ADDRESS. It wants `email:someone@example.com` and
    # refuses a bare address — which is a sensible rule for a channel abstraction and a trap
    # for a workflow input called `to`. Nobody types a scheme into a To field, so it is added
    # here rather than made the operator's problem.
    channel = to if to.startswith("email:") else f"email:{to}"
    message_id = send(channel, subject, body, connection=context.connection,
                      settings=context.settings, project_id=context.project_id)
    return {"to": to, "subject": subject, "message_id": message_id,
            "characters": len(body)}


@register_executor("meeting.extract_actions")
def execute_meeting_extract_actions(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Read a transcript; write down what was decided and what somebody has to do.

    Creates nothing. Its whole output is a list a person approves before the next step turns it
    into tickets other people will see — the same split as planning an edit before rendering it.
    """
    from .meeting_actions import extract
    from .model_routing import project_chat_adapter

    transcript = str(inputs.get("transcript") or inputs.get("notes") or "").strip()
    if not transcript:
        raise ValueError(
            "transcript is required — paste the meeting notes or transcript")

    # The model THIS PROJECT connected (model_routing). Without it the adapter falls through to
    # OpenAI and an install on a Claude subscription fails with "OPENAI_API_KEY is not
    # configured".
    with _routed(context, "writing", transcript):
        adapter = project_chat_adapter(context.connection, context.settings, context.project_id)
    result = extract(
        adapter,
        transcript=transcript,
        context=str(inputs.get("context") or ""),
    )
    return {
        "decisions": result["decisions"],
        "actions": result["actions"],
        "unclear": result["unclear"],
        "summary": result["summary"],
        "action_count": len(result["actions"]),
    }


@register_executor("tracker.issues.create")
def execute_tracker_issues_create(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Create issues in the project's work tracker — Plane by default.

    TAKES A LIST, not one issue. A meeting produces several actions and a workflow's step list
    is fixed, so one step per ticket is not expressible; the same reason video.apply_plan takes
    a plan rather than one operation.

    PARTIAL SUCCESS IS REPORTED, NOT HIDDEN. Six tickets where the fourth fails leaves five
    real ones behind, and a step that raises would say nothing about which. Each result carries
    its own outcome and the step fails only if EVERY one did.
    """
    from .work_tracker_adapters import get_adapter

    actions = inputs.get("actions") or inputs.get("issues")
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        raise ValueError("actions must be a list of {title, detail} items")
    if not actions:
        # NOT AN ERROR. A meeting that produced no actions is a real outcome, and failing here
        # would make "nothing to do" look like a broken workflow.
        return {"created": [], "failed": [], "created_count": 0,
                "note": "no actions were given, so no tickets were created"}

    wanted = str(inputs.get("provider_key") or "").strip().lower()
    # CONNECTED, NOT MERELY `active`. Bootstrap seeds five trackers as active with empty
    # configurations, so "is there an active tracker" was always yes and this refusal never
    # fired: the run failed inside the adapter instead, after a person had approved the tickets
    # (boss-1, 2026-10-02). The same answer the start preflight gives.
    from .work_orchestration import connected_tracker

    row = connected_tracker(context.connection, context.project_id, wanted)
    if row is None:
        raise ValueError(
            f"this project has no {'connected work tracker' if not wanted else wanted + ' connected'}"
            f". Connect one first in Skills & MCP → File tickets — GitHub Issues, Linear, Jira, "
            f"Plane or Azure DevOps.")

    from .work_orchestration import with_credential

    adapter = get_adapter(row["provider_key"])
    # The token for these calls only: a secret-store reference, or the GitHub connection.
    config = with_credential(context.connection, context.project_id,
                             dict(row["configuration"] or {}))

    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            failed.append({"index": index, "error": "not an object"})
            continue
        title = str(action.get("title") or "").strip()
        if not title:
            failed.append({"index": index, "error": "no title"})
            continue
        body = _issue_body(action)
        try:
            result = adapter.create_issue(
                config, title=title[:200],
                description_html=body.replace("\n", "<br>"), description_text=body)
            if result is None:
                # An adapter that declines rather than raises. Reported as a failure, because
                # "no ticket exists" is the same outcome either way.
                failed.append({"index": index, "title": title,
                               "error": "the tracker did not create it and gave no reason"})
                continue
            created.append({
                "title": title,
                # IssueCreateResult's own field names — not guessed. A URL is what makes the
                # step's output useful to the person who approved it.
                "url": result.external_url,
                "key": result.external_key,
                "external_id": result.external_issue_id,
                "owner": action.get("owner"),
            })
        except Exception as error:  # noqa: BLE001 — one bad ticket is not six
            failed.append({"index": index, "title": title, "error": str(error)[:300]})

    if failed and not created:
        raise ValueError(
            f"none of the {len(failed)} tickets could be created in "
            f"{row['display_name']}: {failed[0]['error']}")
    return {
        "created": created,
        "failed": failed,
        "created_count": len(created),
        "tracker": row["display_name"],
    }


def _issue_body(action: dict[str, Any]) -> str:
    """What the ticket says, for somebody who was not in the meeting."""
    parts = [str(action.get("detail") or "").strip()]
    if action.get("owner"):
        parts.append(f"Owner as named in the meeting: {action['owner']}")
    if action.get("due"):
        parts.append(f"Timing as discussed: {action['due']}")
    parts.append("Created by BOSS from a meeting transcript.")
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# The Kit's skills (skills-mcp spec E11, E12): an ability whose skill cannot run is not one.  #
# --------------------------------------------------------------------------- #

#: The engine's read tools `repo.read` offers, and nothing else (its spec's policy.tools).
REPO_READ_TOOLS = ("read_code", "search_code", "list_source", "git_history", "read_pull_request")


@register_executor("repo.read")
def execute_repo_read(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """Read the project's repository through the engine's own read tools.

    A workflow step's agent gets no native tools (agent_loop: a bounded run is offered exactly
    what its agent holds), so before this an agent asked to review code could not open a file.
    Holding `repo.read` offers it these five reads, each a call of this one skill — so the grant,
    the budget gate and the invocation record are the ordinary skill runtime's. The tools are the
    project-pinned set chat uses (build_discuss_read_registry): the project id is the run's, not
    the model's to choose."""
    from ..context import EngineContext
    from ..tools.builtin import build_discuss_read_registry

    tool = str(inputs.get("tool") or "read_code").strip()
    if tool not in REPO_READ_TOOLS:
        raise ValueError(f"repo.read offers {', '.join(REPO_READ_TOOLS)}; not {tool!r}.")
    registry = build_discuss_read_registry(
        edition=getattr(context.settings, "boss_edition", None))
    if tool not in registry.names():
        raise SkillExecutorConfigurationError(f"this BOSS has no {tool} tool.")
    spec = registry.get(tool)
    args = {k: v for k, v in inputs.items() if k != "tool" and not str(k).startswith("_")}
    # THE PROJECT'S REPOSITORY, NEVER BOSS'S OWN. The read tools default to BOSS's source
    # ("boss") for Ask BOSS; an agent holding this reads the repo its project is bound to, and
    # a repo it names must be one the project is bound to as well.
    from .kit_connections import github_state

    bound = github_state(context.connection, context.settings, context.project_id)
    repo = str(args.get("repo") or "").strip()
    if not args.get("run_id"):
        if repo.lower() in ("", "boss", "self", "platform"):
            if not bound["repo"]:
                raise SkillExecutorConfigurationError(
                    "this project has no repository connected. Connect it in Setup.")
            args["repo"] = bound["repo"]
        elif repo.lower() != str(bound["repo"] or "").lower() and not context.connection.execute(
                text("SELECT 1 FROM github_repository_bindings WHERE project_id = :p "
                     "AND lower(full_name) = lower(:r) AND status = 'active'"),
                {"p": context.project_id, "r": repo}).first():
            raise SkillExecutorConfigurationError(f"{repo} is not a repository of this project.")
    ctx = EngineContext(settings=context.settings, engine=context.connection.engine)
    out = spec.handler(ctx, project_id=context.project_id, actor=context.actor, args=args)
    return {"tool": tool, "result": out if isinstance(out, dict) else {"value": out}}


@register_executor("ci.status")
def execute_ci_status(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """CI results for a ref of the project's repository: GitHub check runs, read-only."""
    from .kit_connections import _http_get, github_state, github_token

    state = github_state(context.connection, context.settings, context.project_id)
    if not state["connected"] or not state["repo"]:
        raise SkillExecutorConfigurationError(
            "this project has no GitHub repository connected. Connect it in Setup.")
    token = github_token(context.connection, context.settings, context.project_id)
    base, repo = state["api_base"], state["repo"]
    ref = str(inputs.get("ref") or "").strip()
    if not ref:
        ref = str((_http_get(f"{base}/repos/{repo}", token) or {}).get("default_branch")
                  or "main")
    data = _http_get(f"{base}/repos/{repo}/commits/{ref}/check-runs?per_page=50", token) or {}
    checks = [{"name": c.get("name"), "status": c.get("status"),
               "conclusion": c.get("conclusion"), "url": c.get("html_url")}
              for c in data.get("check_runs") or []]
    failing = [c["name"] for c in checks
               if c["conclusion"] in ("failure", "timed_out", "cancelled", "action_required")]
    return {"ref": ref, "total": int(data.get("total_count") or len(checks)),
            "failing": failing, "checks": checks}


_DOCS_SYSTEM = (
    "You write documents. Answer with the document only, in Markdown: a title, short sections "
    "with headings, plain sentences. No preamble, no closing remarks, no notes about yourself.")


@register_executor("docs.draft")
def execute_docs_draft(context: "SkillExecutorContext", inputs: dict[str, Any]
                       ) -> dict[str, Any]:
    """Draft a document with the project's model: model.generate_text in a document's shape."""
    brief = _required_text(inputs, "brief")
    extra = []
    if inputs.get("audience"):
        extra.append(f"Audience: {inputs['audience']}")
    if inputs.get("length"):
        extra.append(f"Length: {inputs['length']}")
    written = execute_model_generate_text(context, {
        "prompt": "\n".join([brief, *extra]), "system": _DOCS_SYSTEM,
        "context": inputs.get("context") or ""})
    return {"markdown": written["text"], "characters": written["characters"],
            "model": written.get("model")}


@register_executor("work_tracker.issue.ensure")
def execute_work_tracker_issue_ensure(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Make sure one piece of work has an issue in the project's tracker — once.

    Idempotent by work item: the receipt keyed on (tracker, work item) is the binding, so a
    retried job or a second ask returns the issue already opened instead of opening another.
    A work item BOSS tracks (work_items) also gets its work_tracker_bindings row."""
    from .work_orchestration import connected_tracker, with_credential
    from .work_tracker_adapters import get_adapter

    work_item_id = _required_text(inputs, "work_item_id")
    title = _required_text(inputs, "title")
    description = str(inputs.get("description") or "").strip()
    wanted = str(inputs.get("provider_key") or "").strip().lower()
    row = connected_tracker(context.connection, context.project_id, wanted)
    if row is None:
        raise ValueError("this project has no connected work tracker. Connect one in Skills & "
                         "MCP › File tickets.")
    provider = str(row["provider_key"])
    org_id, workspace_id = context.tenant_scope()
    receipt, created = context.receipts.create_or_reuse(
        context.connection, org_id, workspace_id, context.project_id,
        f"work_tracker.issue.ensure:{provider}:{work_item_id}", "work_tracker.issue.ensure",
        {"provider": provider, "work_item_id": work_item_id},
        {"title": title}, created_by_job_id=context.created_by_job_id)
    if not created and receipt.get("status") == "succeeded":
        return {**(receipt.get("result") or {}), "tracker_binding_id": receipt["id"],
                "sync_status": "synced", "deduplicated": True}
    config = with_credential(context.connection, context.project_id,
                             dict(row["configuration"] or {}))
    made = get_adapter(provider).create_issue(
        config, title=title[:200], description_html=description.replace("\n", "<br>"),
        description_text=description or title)
    if made is None:
        raise RuntimeError(f"{row['display_name']} did not create the issue — check the "
                           "connection's test in Skills & MCP › File tickets.")
    out = {"provider": provider, "external_issue_id": made.external_issue_id,
           "external_key": made.external_key, "external_url": made.external_url}
    context.receipts.mark_succeeded(context.connection, receipt["id"],
                                    f"{provider}:{made.external_issue_id}", out)
    _bind_work_item(context, provider, work_item_id, made)
    return {**out, "tracker_binding_id": receipt["id"], "sync_status": "synced"}


def _bind_work_item(context: "SkillExecutorContext", provider: str, work_item_id: str,
                    made: Any) -> None:
    """Record the issue on a BOSS work item, when the id names one."""
    from .core import make_id as _make_id

    known = context.connection.execute(
        text("SELECT 1 FROM work_items WHERE id = :w AND project_id = :p"),
        {"w": work_item_id, "p": context.project_id}).first()
    if not known:
        return
    context.connection.execute(
        text("""INSERT INTO work_tracker_bindings
                  (id, project_id, provider_id, work_item_id, external_issue_id, external_key,
                   external_url, external_state, sync_status, created_by, last_synced_at)
                SELECT :id, :p, pr.id, :w, :ext, :key, :url, :state, 'synced', :actor, now()
                  FROM work_tracker_providers pr
                 WHERE pr.project_id = :p AND pr.provider_key = :provider
                ON CONFLICT (project_id, provider_id, work_item_id) DO UPDATE
                  SET external_issue_id = EXCLUDED.external_issue_id,
                      external_key = EXCLUDED.external_key,
                      external_url = EXCLUDED.external_url, sync_status = 'synced',
                      last_synced_at = now(), updated_at = now()"""),
        {"id": _make_id("wtb"), "p": context.project_id, "w": work_item_id,
         "ext": made.external_issue_id, "key": made.external_key, "url": made.external_url,
         "state": made.external_state, "actor": context.actor, "provider": provider})


@register_executor("work_tracker.issue.update")
def execute_work_tracker_issue_update(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Comment on an issue, or change its state, in the project's tracker."""
    from .work_orchestration import connected_tracker, with_credential
    from .work_tracker_adapters import WorkTrackerHttpError, strip_html, update_issue

    binding = _required_text(inputs, "tracker_binding_id")
    comment = strip_html(str(inputs.get("comment_html") or inputs.get("comment") or ""))
    links = inputs.get("links") or []
    if isinstance(links, list) and links:
        comment = "\n".join([comment, *[str(link) for link in links]]).strip()
    state = str(inputs.get("state") or "").strip()
    if not comment and not state:
        raise ValueError("say what to change: a comment, links or a state")
    row = connected_tracker(context.connection, context.project_id)
    if row is None:
        raise ValueError("this project has no connected work tracker. Connect one in Skills & "
                         "MCP › File tickets.")
    issue = binding
    found = context.connection.execute(
        text("SELECT result FROM receipts WHERE id = :id AND project_id = :p"),
        {"id": binding, "p": context.project_id}).scalar()
    if isinstance(found, dict):
        issue = str(found.get("external_key") or found.get("external_issue_id") or binding)
    else:
        bound = context.connection.execute(
            text("SELECT external_key, external_issue_id FROM work_tracker_bindings "
                 "WHERE id = :id AND project_id = :p"),
            {"id": binding, "p": context.project_id}).mappings().first()
        if bound is not None:
            issue = str(bound["external_key"] or bound["external_issue_id"] or binding)
    config = with_credential(context.connection, context.project_id,
                             dict(row["configuration"] or {}))
    try:
        done = update_issue(str(row["provider_key"]), config, issue=issue, comment=comment,
                            state=state)
    except WorkTrackerHttpError as error:
        raise RuntimeError(str(error)) from error
    return {"tracker_binding_id": binding, "sync_status": "synced", **done}


def _chat_connection(context: "SkillExecutorContext", provider: str) -> Optional[dict[str, Any]]:
    from .kit_connections import chat_settings

    stored = chat_settings(context.connection, context.settings, context.project_id)
    return stored if stored and stored.get("provider") == provider else None


@register_executor("slack.post_real_guarded")
def execute_slack_post(context: "SkillExecutorContext", inputs: dict[str, Any]
                       ) -> dict[str, Any]:
    """Post to Slack: the project's Slack connection (Skills & MCP › Team chat) first, the
    environment's webhook otherwise. Approval is the spec's policy, enforced before this."""
    import httpx

    from .slack_adapter import SlackAdapter

    message = str(inputs.get("text") or inputs.get("body") or "").strip()
    if not message:
        raise ValueError("text is required — an empty post is not a message")
    stored = _chat_connection(context, "slack")
    if stored is None:
        channel = str(inputs.get("channel") or "").strip()
        return {"provider_ref": SlackAdapter(context.settings).post_message(
            channel, None, message), "channel": channel}
    channel = str(inputs.get("channel") or stored.get("channel") or "").strip()
    body: dict[str, Any] = {"channel": channel, "text": message}
    if inputs.get("thread_ts"):
        body["thread_ts"] = str(inputs["thread_ts"])
    resp = httpx.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {stored['token']}"}, json=body,
                      timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack refused the post: {data.get('error') or 'unknown error'}")
    return {"provider_ref": f"slack:{data.get('channel')}:{data.get('ts')}", "channel": channel}


@register_executor("mattermost.post_real")
def execute_mattermost_post(context: "SkillExecutorContext", inputs: dict[str, Any]
                            ) -> dict[str, Any]:
    """Post to Mattermost: the project's Mattermost connection first, the environment's bot
    otherwise."""
    import httpx

    from .mattermost_adapter import MattermostAdapter

    message = str(inputs.get("body") or inputs.get("text") or "").strip()
    if not message:
        raise ValueError("body is required — an empty post is not a message")
    subject = str(inputs.get("subject") or "").strip() or None
    stored = _chat_connection(context, "mattermost")
    channel = str(inputs.get("channel_id") or (stored or {}).get("channel") or "").strip()
    if stored is None:
        return {"post_ref": MattermostAdapter(context.settings).post_message(
            f"mattermost:{channel}", subject, message)}
    text_out = f"**{subject}**\n{message}" if subject else message
    resp = httpx.post(f"{str(stored.get('base_url') or '').rstrip('/')}/api/v4/posts",
                      headers={"Authorization": f"Bearer {stored['token']}"},
                      json={"channel_id": channel, "message": text_out}, timeout=30.0)
    resp.raise_for_status()
    return {"post_ref": f"mattermost-post:{resp.json().get('id') or 'unknown'}"}


@register_executor("media.fetch_link")
def execute_media_fetch_link(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Turn a link somebody pasted into a media artifact the editing steps can take.

    THE ENTRY POINT FOR A LINK, which `drive.import_video` is not: that one takes a Drive file id,
    and a person has a URL. This places the URL, fetches from wherever it lives, and leaves the
    result in the project's own media store — so every step after it works the same way no matter
    where the video came from.

    A GOOGLE LINK IS NOT COPIED. It already sits in the store the editing steps read from, and
    duplicating a multi-gigabyte file to achieve nothing is the kind of cost that only shows up on
    somebody's bill. Anything else is fetched once and stored once.
    """
    from .media_storage import MediaStorageError, fetch
    from .media_providers import resolve_link
    from .drive_media import record_artifact, workspace

    url = str(inputs.get("url") or inputs.get("link") or "").strip()
    if not url:
        raise ValueError("url is required — the link to the video")
    # Placed before anything is fetched, so a link BOSS cannot use fails at the first step rather
    # than three steps in when an editing step gets a login page instead of a video.
    link = resolve_link(url)

    if link.provider_key == "google":
        # Delegated rather than reimplemented: the metadata read also rejects a file that is not
        # a video, and having two versions of that check is having one of them be wrong later.
        result = execute_drive_import_video(
            context, {"file_id": link.file_ref,
                      "connection_key": inputs.get("connection_key")})
        return {**result, "provider": "google", "copied": False, "source_url": url}

    connection_key = inputs.get("connection_key")
    with workspace("boss-fetch-link-") as work:
        fetched = fetch(context.connection, context.settings,
                        project_id=context.project_id, link=link, dest_dir=work,
                        connection_key=connection_key)
        # Checked before it is stored. A link that served an HTML error page returns bytes and a
        # 200, and storing that produces an artifact the editing steps will fail on with an
        # ffmpeg error rather than "that link did not give us a video".
        from .video_edit import probe, VideoEditError

        try:
            info = probe(fetched["path"])
        except VideoEditError as exc:
            raise MediaStorageError(
                f"what {url} returned is not a video BOSS can read ({exc}). A share link that "
                "needs a sign-in usually returns a web page, which is what this looks like."
            ) from exc

        name = str(inputs.get("output_name") or "").strip() or fetched["name"]
        # KEPT IN BOSS unless the workflow asked for Drive and Drive is connected. This used to
        # default to Google, so a direct link fetched fine and then the run stopped for want of
        # a connection only publishing needs.
        written = _store_media(context, inputs, fetched["path"], name)

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_video", drive_file=written,
        # WHERE IT CAME FROM, on the artifact. A published video has to be traceable to its
        # source, and for a fetched link the source is a URL rather than another artifact.
        detail={"operation": "fetch_link", "source_url": url,
                "source_provider": link.provider_key, **info},
    )
    return {
        "media_artifact_id": artifact_id,
        **_where_stored(artifact_id, written),
        "name": name,
        "provider": link.provider_key,
        "copied": True,
        "source_url": url,
        "duration_seconds": info.get("duration_seconds"),
    }


@register_executor("media.fetch_image")
def execute_media_fetch_image(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Turn a pasted image link into an artifact the editing steps can put on screen.

    THE MISSING HALF OF `media.fetch_link`. That one refuses anything that is not a video, which
    is right for the thing being edited — and left NO WAY to get a picture into BOSS. There is no
    artifact upload in the console, so an operator asked to supply an "ending slide" had to name
    an artifact id they had no means of obtaining. The Start form asked for something unobtainable,
    which is the same dead end as a start form with no fields at all.

    IT RETURNS `assets` AS WELL AS AN ID. `video.plan_edit` takes {name: artifact_id} and a step
    output is a single value, so handing back the map is what lets the two steps join with nothing
    in between. The name matters: the planning model reads it to work out what the picture is FOR,
    so "ending slide" plans differently from "logo".

    A BLANK URL IS NOT AN ERROR. This step is optional in a workflow — most edits have no slide —
    and a required step that fails on an empty optional field is a workflow that cannot be run
    without one.
    """
    from .media_storage import MediaStorageError, fetch
    from .media_providers import resolve_link
    from .drive_media import record_artifact, workspace
    from .video_edit import is_a_still, probe, VideoEditError

    url = str(inputs.get("url") or inputs.get("link") or "").strip()
    label = str(inputs.get("name") or "").strip() or "slide"
    if not url:
        return {"image_artifact_id": "", "assets": {}, "name": label,
                "provider": "", "skipped": "no link given"}

    link = resolve_link(url)
    connection_key = inputs.get("connection_key")
    with workspace("boss-fetch-image-") as work:
        fetched = fetch(context.connection, context.settings,
                        project_id=context.project_id, link=link, dest_dir=work,
                        connection_key=connection_key)
        # WHAT CAME BACK HAS TO BE A PICTURE. A share link that needs a sign-in returns an HTML
        # page with a 200, and storing that produces an artifact the render fails on with an
        # ffmpeg error instead of "that link did not give us an image".
        try:
            info = probe(fetched["path"])
        except VideoEditError as exc:
            raise MediaStorageError(
                f"what {url} returned is not an image BOSS can read ({exc}). A share link that "
                "needs a sign-in usually returns a web page, which is what this looks like."
            ) from exc
        if not info.get("width") or not info.get("height"):
            raise MediaStorageError(
                f"what {url} returned has no picture in it at all.")
        # A SHORT CLIP IS ACCEPTED TOO, deliberately. An "ending slide" is a PNG most of the time
        # and a four-second animated card the rest of the time, and `bookend` takes either — so
        # refusing the clip here would be this skill inventing a restriction the render does not
        # have.
        still = is_a_still(fetched["path"])

        name = str(inputs.get("output_name") or "").strip() or fetched["name"]
        # A picture linked from Drive goes back beside it when Google is connected.
        written = _store_media(context, inputs, fetched["path"], name,
                               source_storage=link.provider_key)

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_image" if still else "media_video", drive_file=written,
        detail={"operation": "fetch_image", "source_url": url,
                "source_provider": link.provider_key, "still": still, **info},
    )
    return {
        "image_artifact_id": artifact_id,
        # KEYED BY THE LABEL, so this wires straight into plan_edit's `assets` and the planning
        # model is told what the picture is for rather than guessing from a bare id.
        "assets": {label: artifact_id},
        **_where_stored(artifact_id, written),
        "name": name,
        "provider": link.provider_key,
        "source_url": url,
        "still": still,
        "width": info.get("width"),
        "height": info.get("height"),
    }


@register_executor("media.read_document")
def execute_media_read_document(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Read a linked document as text, so an agent can act on what it says.

    THE GAP THIS CLOSES. An agent could fetch a VIDEO and an IMAGE and could not read a
    DOCUMENT. So an operator who had written the title and description for their video in a
    Drive doc — and linked it — was asked to retype both into a form.

    It returns the words and stores nothing. A brief belongs to the person who wrote it; the
    agent needs to read it, not to keep a copy in BOSS.

    TAMIL AND EVERY OTHER SCRIPT. `drive_media.read_document` exports Google-native files
    rather than downloading them (a Doc has no bytes to download) and decodes UTF-8
    deliberately rather than letting the HTTP client guess — a wrong guess turns non-Latin text
    into mojibake that still looks like a successful read.

    A PDF IS WHAT PEOPLE ACTUALLY LINK, and it is not text — it is instructions for painting a
    page. The first real brief that reached this skill was one, and was refused. It is read now,
    by asking Drive to convert it (which OCRs a scan and returns logical-order text) and falling
    back to local extraction with the glyph order repaired. `read_via` in the result says which
    route answered.
    """
    from .drive_media import read_document
    from .media_providers import resolve_link

    url = str(inputs.get("url") or inputs.get("link") or "").strip()
    file_id = str(inputs.get("file_id") or "").strip()
    if not url and not file_id:
        raise ValueError(
            "give either url (the Drive share link) or file_id (the id out of it) — "
            "this needs one of them to know which document to read")

    if not file_id:
        link = resolve_link(url)
        if link.provider_key != "google":
            raise ValueError(
                f"{url} is not a Google Drive link. This reads documents out of the Drive "
                "account connected to this project.")
        file_id = link.file_ref

    result = read_document(
        context.connection, context.settings, project_id=context.project_id,
        file_id=file_id,
        # ONLY USED WHEN A PDF HAS TO BE OCR'D. An ISO code — "ta" for Tamil, "hi", "en".
        # Naming it costs nothing and OCR in the wrong language returns confident nonsense.
        language=str(inputs.get("language") or "").strip(),
        connection_key=str(inputs.get("connection_key") or "youtube_default"),
    )
    return {
        "text": result["text"],
        # THE SAME TEXT, NUMBERED, so a value can be pointed at rather than retyped. Retyping is
        # what turned a Tamil title into a different sentence; see `agent_steps._copied_exactly`.
        "numbered_text": result.get("numbered_text") or "",
        "name": result["name"],
        "characters": result["characters"],
        "mime_type": result["mime_type"],
        "read_via": result["read_via"],
        "file_id": result["file_id"],
    }


@register_executor("video.plan_edit")
def execute_video_plan_edit(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Read the instructions, write the edit plan. The one step that needs judgement.

    It renders nothing and changes nothing. Its whole output is a plan, which the next step
    executes and a person approves before anything is published — so the model's judgement is
    bounded twice over: by validate_plan, which refuses anything outside the operation
    vocabulary, and by a human who sees the result.

    SEPARATE FROM video.apply_plan ON PURPOSE. Together they would be one step that thinks and
    edits, and a re-run would re-plan — so a reviewer who rejected a render could not ask for the
    same plan again with one operation changed. Apart, the plan is an artifact of its own: it can
    be read, corrected and handed to the editing step unchanged.
    """
    from .video_edit import probe, VideoEditError
    from .video_planner import PlanningError, house_style_for, plan_from_instructions, summarise
    from .drive_media import workspace

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required — the video to plan edits for")
    instructions = str(inputs.get("instructions") or "").strip()
    if not instructions:
        raise ValueError(
            "instructions are required — say what should happen to this video")

    source = _media_artifact(context, source_artifact)

    # THE VIDEO'S OWN FACTS, taken from the recorded artifact where they are already known. A
    # model asked to "cut the bit near the end" without the duration invents a timecode, and an
    # invented timecode passes validation and edits the wrong part — the one failure validation
    # cannot catch. Reading them off the artifact avoids downloading gigabytes to plan.
    detail = source.get("detail") if isinstance(source.get("detail"), dict) else {}
    duration = float(detail.get("duration_seconds") or source.get("duration_seconds") or 0)
    width = int(detail.get("width") or source.get("width") or 0)
    height = int(detail.get("height") or source.get("height") or 0)
    if duration <= 0:
        # Not recorded — an artifact imported before those were captured. Measure it rather than
        # planning blind, because planning blind is the failure above.
        with workspace("boss-plan-probe-") as work:
            local = _local_media(context, source, work, inputs)
            try:
                info = probe(local)
            except VideoEditError as exc:
                raise PlanningError(
                    f"could not read that video's duration, so no timecode can be planned "
                    f"against it ({exc}).") from exc
        duration = float(info.get("duration_seconds") or 0)
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)

    # Images the plan may refer to, named by the operator. A plan can only mention what is
    # listed here — the model never gets to name an artifact BOSS was not told about.
    assets = inputs.get("assets") if isinstance(inputs.get("assets"), dict) else {}
    assets = {str(k): str(v) for k, v in assets.items() if str(v).strip()}

    # THE MODEL THIS PROJECT CONNECTED, not whatever the bare Settings object defaults to —
    # resolved by model_routing, the one place every model-using skill gets its model.
    from .model_routing import project_chat_adapter

    with _routed(context, "writing", instructions):
        adapter = project_chat_adapter(context.connection, context.settings, context.project_id)
    result = plan_from_instructions(
        adapter,
        instructions=instructions,
        house_style=str(inputs.get("house_style")
                        or house_style_for(context.connection, context.project_id)),
        duration_seconds=duration, width=width, height=height, assets=assets,
    )
    return {
        "plan": result["plan"],
        "assumptions": result["assumptions"],
        "refused": result["refused"],
        "timeline_note": result["timeline_note"],
        # What the review step puts in front of a person. A JSON plan is not a thing anybody
        # reads carefully while deciding whether to publish.
        "summary": summarise(result),
        "media_artifact_id": source_artifact,
        "steps": len(result["plan"]),
    }


@register_executor("video.apply_plan")
def execute_video_apply_plan(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Run a whole edit plan in one step.

    WHY ONE STEP AND NOT ONE STEP PER OPERATION. A workflow's step list is fixed when the
    version is authored; an instruction a person writes is four operations this time and nine
    the next. There is no number of steps to author, so the variability lives in the plan —
    which a model writes, BOSS validates against the same vocabularies a hand-written step
    faces, and this executes in order.

    It is also the only shape that downloads and uploads once. Eleven steps would mean eleven
    round trips through Drive for a video that is being re-encoded anyway.
    """
    from .drive_media import record_artifact, workspace
    from .video_edit import apply_plan, validate_plan, verify_playable

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    # VALIDATED BEFORE ANYTHING IS DOWNLOADED, let alone rendered. A plan with a bad operation
    # in the middle must cost nothing, and the error must name the operation rather than arrive
    # as a render failure twenty minutes in.
    plan = validate_plan(inputs.get("plan"))

    source = _media_artifact(context, source_artifact)

    # Every artifact the plan names, gathered from the plan itself rather than from fixed input
    # fields — the point of a plan is that BOSS does not know in advance how many images it
    # mentions. Deduplicated, because the same logo in four operations is one download.
    wanted: list[str] = []
    for entry in plan:
        for field in ("image_artifact_id", "logo_artifact_id"):
            value = str(entry.get(field) or "").strip()
            if value and value not in wanted:
                wanted.append(value)

    with workspace("boss-apply-plan-") as work:
        local = _local_media(context, source, work, inputs)

        assets: dict[str, str] = {}
        for artifact_id in wanted:
            asset = _media_artifact(context, artifact_id)
            assets[artifact_id] = _local_media(context, asset, work, inputs)

        result = apply_plan(local, plan, work_dir=work, artifact_paths=assets)
        info = verify_playable(
            result["path"], expect_audio=bool(inputs.get("expect_audio", True)))

        name = str(inputs.get("output_name") or "").strip() or os.path.basename(result["path"])
        written = _store_media(context, inputs, result["path"], name,
                               source_storage=_source_storage(source))

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_video", drive_file=written, source_artifact_id=source_artifact,
        # The per-operation record goes ON THE ARTIFACT, because the person approving the
        # publish is deciding about this file and "the plan ran" is not evidence. This lets them
        # see the duration fall where a cut was asked for without watching the whole thing.
        detail={"operation": "apply_plan", "applied": result["applied"],
                "timeline_note": result.get("timeline_note"), **info},
    )
    return {
        "media_artifact_id": artifact_id,
        **_where_stored(artifact_id, written),
        "applied": result["applied"],
        "timeline_note": result.get("timeline_note"),
        "duration_seconds": info.get("duration_seconds"),
        "width": info.get("width"),
        "height": info.get("height"),
    }


@register_executor("publish_youtube")
def execute_publish_youtube(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Publish the edited video to the connected channel, as PRIVATE.

    The last step of the media pipeline and the only one with an effect outside BOSS. It takes
    the same media artifact every editing step produces, so the thing published is provably the
    thing that was reviewed — not a file somebody re-uploaded in between.
    """
    from .drive_media import workspace
    from .video_edit import verify_playable
    from .youtube_publish import upload_video

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    source = _media_artifact(context, source_artifact)
    connection_key = str(inputs.get("connection_key") or "youtube_default")

    with workspace("boss-publish-") as work:
        # Wherever the reviewed file is kept: BOSS's own storage or Drive.
        local = _local_media(context, source, work, inputs)
        # CHECKED AGAIN, IMMEDIATELY BEFORE THE IRREVERSIBLE ACT. The edit step verified its
        # own output, but something could have replaced the Drive file since. Publishing an
        # empty or silent video is not recoverable by deleting it — people have seen it.
        info = verify_playable(local, expect_audio=bool(inputs.get("expect_audio", True)))
        result = upload_video(
            context.connection, context.settings,
            project_id=context.project_id, local_path=local,
            title=str(inputs.get("title") or ""),
            description=str(inputs.get("description") or ""),
            tags=inputs.get("tags") or [],
            # No default: absent means absent, and upload_video refuses rather than guessing a
            # legal declaration.
            made_for_kids=inputs.get("made_for_kids"),
            visibility=str(inputs.get("visibility") or "private"),
            connection_key=connection_key,
        )

    emit_event(
        context.connection, context.project_id, "video.published", context.actor,
        "youtube_video", str(result["video_id"]),
        {"visibility": result["visibility"], "media_artifact_id": source_artifact,
         "duration_seconds": info.get("duration_seconds")},
    )
    return {**result, "media_artifact_id": source_artifact}


@register_executor("drive.list_videos")
def execute_drive_list_videos(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """The videos an operator could pick, so a workflow can be given one by name.

    A read, and the only step in the media pipeline that is. It exists because the alternative
    is asking an operator for a Drive file id — a 33-character opaque string they would have to
    dig out of a URL — when what they know is "the one called MS_latest_FULL_1080p".
    """
    from .drive_media import list_videos

    files = list_videos(
        context.connection, context.settings,
        project_id=context.project_id,
        folder_id=str(inputs.get("folder_id") or ""),
        connection_key=str(inputs.get("connection_key") or "youtube_default"),
        limit=int(inputs.get("limit") or 50),
    )
    return {"files": files, "count": len(files)}


@register_executor("drive.import_video")
def execute_drive_import_video(
    context: "SkillExecutorContext", inputs: dict[str, Any]
) -> dict[str, Any]:
    """Turn a Drive file into a media artifact the editing steps can take.

    THE ENTRY POINT OF THE WHOLE PIPELINE. Every editing step takes a `media_artifact_id`, and
    until this exists there is no way to make the first one — an operator with a video in Drive
    has no way to start. It downloads nothing: an artifact records WHERE the media is, and the
    step that needs the bytes fetches them itself.
    """
    from .drive_media import DriveMediaError, record_artifact
    from .channel_oauth import access_token_for

    file_id = str(inputs.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("file_id is required — use drive.list_videos to find one")
    connection_key = str(inputs.get("connection_key") or "youtube_default")

    # Read the file's metadata so the artifact carries a NAME rather than an opaque id, and so
    # a file that is not a video fails here instead of inside ffmpeg three steps later.
    import httpx

    token = access_token_for(
        context.connection, context.settings,
        project_id=context.project_id, connection_key=connection_key)
    r = httpx.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"fields": "id,name,mimeType,size,webViewLink"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if r.status_code != 200:
        raise DriveMediaError(
            f"Could not read that Drive file ({r.status_code}). Check the id, and that the "
            "connected Google account can see it.")
    info = r.json() or {}
    mime = str(info.get("mimeType") or "")
    if not mime.startswith("video/"):
        raise DriveMediaError(f"'{info.get('name')}' is {mime or 'of unknown type'}, not a video.")

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_video",
        drive_file={
            "file_id": info.get("id"),
            "name": info.get("name"),
            "view_url": info.get("webViewLink")
                        or f"https://drive.google.com/file/d/{info.get('id')}/view",
            "bytes": int(info.get("size") or 0),
        },
        detail={"operation": "import", "mime_type": mime},
    )
    return {
        "media_artifact_id": artifact_id,
        "name": info.get("name"),
        "bytes": int(info.get("size") or 0),
        "drive_file_id": info.get("id"),
    }


def _route_purpose(context: Any, step: str, text: Any):
    """What the model router reads about a skill's model call (docs/product/MODEL_ROUTER.md):
    the kind of step, the material, the workflow run and step it serves, and the caller. Reads
    the context defensively: a skill can be run by anything that looks like a context."""
    from .model_router.router import RouteRequest

    about = getattr(context, "approval_about", None) or {}
    about = about if isinstance(about, dict) else {}
    return RouteRequest(kind="workflow_step", step=step,
                        project_id=getattr(context, "project_id", None),
                        unit_id=about.get("workflow_run_id") or about.get("run_id")
                        or getattr(context, "created_by_job_id", None),
                        ref=about.get("step_id"), role=getattr(context, "caller", None) or None,
                        text=str(text or "")[:6000])


def _routed(context: Any, step: str, text: Any):
    """The purpose around a skill's `project_chat_adapter` call (model_routing.routing_purpose)."""
    from .model_routing import routing_purpose

    return routing_purpose(_route_purpose(context, step, text))


# --------------------------------------------------------------------------- #
# The baseline every agent holds (services/agent_baseline.py).                #
# --------------------------------------------------------------------------- #

@register_executor("boss.fundamentals")
def execute_boss_fundamentals(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """BOSS's own read tools — logs, runs, infra, schema, catalogue, memory — read-only and
    redacted. See agent_baseline.FUNDAMENTALS_TOOLS for the list and why every agent holds it."""
    from .agent_baseline import run_bundled_tool

    return run_bundled_tool(context, "boss.fundamentals", inputs)


@register_executor("boss.propose_fix")
def execute_boss_propose_fix(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """Propose a governed fix: a hold-default decision a person approves. Runs nothing."""
    from .agent_baseline import propose

    return propose(context, dict(inputs))


@register_executor("knowledge.read")
def execute_knowledge_read(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """The knowledge graph's read tools: symbol, callers, blast radius, why, search, precedent.
    See agent_baseline.KNOWLEDGE_TOOLS; every agent holds it."""
    from .agent_baseline import run_bundled_tool

    return run_bundled_tool(context, "knowledge.read", inputs)


@register_executor("knowledge.propose")
def execute_knowledge_propose(context: "SkillExecutorContext", inputs: dict[str, Any]) -> dict[str, Any]:
    """kg_propose_fact: the agent's fact, always stored `inferred`. A baseline skill: every
    agent holds it (agent_baseline.BASELINE_SKILLS)."""
    from .agent_baseline import run_bundled_tool

    return run_bundled_tool(context, "knowledge.propose", inputs)
