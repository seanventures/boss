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
    """The Drive file an artifact points at."""
    from sqlalchemy import text as _sql

    row = context.connection.execute(
        _sql("SELECT content FROM artifacts WHERE id = :i AND project_id = :p"),
        {"i": artifact_id, "p": context.project_id},
    ).mappings().first()
    if not row:
        raise ValueError(f"no artifact {artifact_id!r} in this project")
    content = row["content"] or {}
    if not content.get("drive_file_id"):
        raise ValueError(
            f"artifact {artifact_id!r} is not a media file — it names no Drive file")
    return content


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
    from .drive_media import download, record_artifact, upload, workspace
    from .video_edit import verify_playable

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    source = _media_artifact(context, source_artifact)
    connection_key = str(inputs.get("connection_key") or "youtube_default")
    folder_id = str(inputs.get("output_folder_id") or "")

    with workspace(f"boss-{operation}-") as work:
        local = download(
            context.connection, context.settings,
            project_id=context.project_id, file_id=source["drive_file_id"],
            dest_dir=work, connection_key=connection_key,
        )["path"]

        # Clips this operation also needs (an intro, an outro, a logo), each named by its own
        # artifact so they are versioned and inspectable like anything else.
        fetched: dict[str, str] = {}
        for field in extra_downloads:
            artifact_id = str(inputs.get(field) or "").strip()
            if not artifact_id:
                continue
            asset = _media_artifact(context, artifact_id)
            fetched[field] = download(
                context.connection, context.settings,
                project_id=context.project_id, file_id=asset["drive_file_id"],
                dest_dir=work, connection_key=connection_key,
            )["path"]

        edited = apply_edit(local, work, fetched)
        # VERIFIED BEFORE IT IS UPLOADED. ffmpeg exits 0 on empty and silent output, and an
        # unwatchable file that reached Drive looks exactly like a successful step.
        info = verify_playable(edited, expect_audio=bool(inputs.get("expect_audio", True)))

        name = str(inputs.get("output_name") or "").strip() or os.path.basename(edited)
        written = upload(
            context.connection, context.settings,
            project_id=context.project_id, local_path=edited, name=name,
            folder_id=folder_id, connection_key=connection_key, actor=context.actor,
        )

    artifact_id = record_artifact(
        context.connection, project_id=context.project_id, actor=context.actor,
        kind="media_video", drive_file=written, source_artifact_id=source_artifact,
        detail={"operation": operation, **info},
    )
    return {
        "media_artifact_id": artifact_id,
        "drive_file_id": written["file_id"],
        "view_url": written["view_url"],
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
    from .video_edit import bookend

    if not (str(inputs.get("intro_artifact_id") or "").strip()
            or str(inputs.get("outro_artifact_id") or "").strip()):
        raise ValueError("bookend needs an intro_artifact_id, an outro_artifact_id, or both")
    return _edit_in_drive(
        context, inputs, operation="bookend",
        extra_downloads=("intro_artifact_id", "outro_artifact_id"),
        apply_edit=lambda src, work, extra: bookend(
            src, work_dir=work,
            intro=extra.get("intro_artifact_id"),
            outro=extra.get("outro_artifact_id")),
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
    from .media_storage import MediaStorageError, fetch, store
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
        written = store(context.connection, context.settings,
                        project_id=context.project_id, local_path=fetched["path"], name=name,
                        provider_key=str(inputs.get("store_provider") or "google"),
                        container=str(inputs.get("output_folder_id") or ""),
                        actor=context.actor)

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
        "drive_file_id": written["file_id"],
        "view_url": written["view_url"],
        "name": name,
        "provider": link.provider_key,
        "copied": True,
        "source_url": url,
        "duration_seconds": info.get("duration_seconds"),
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
    from .drive_media import download, workspace

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
        connection_key = str(inputs.get("connection_key") or "youtube_default")
        with workspace("boss-plan-probe-") as work:
            local = download(
                context.connection, context.settings, project_id=context.project_id,
                file_id=source["drive_file_id"], dest_dir=work,
                connection_key=connection_key)["path"]
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

    # THE ENGINE THIS PROJECT CHOSE, not whatever the bare Settings object defaults to.
    #
    # `chat_llm_engine` is a PROJECT setting; it is not on Settings unless a caller puts it
    # there. Without this the adapter falls through to `llm_engine` = "auto" = OpenAI, and an
    # install running happily on a Claude subscription fails with "OPENAI_API_KEY is not
    # configured" — true, and useless, which is the exact sentence chat_engine_for was written
    # to stop. It had stopped it for the chat and not for anything built afterwards.
    from .claude_cli_adapter import make_agent_chat_adapter
    from .setup_service import chat_engine_for

    settings = context.settings
    try:
        engine = chat_engine_for(context.connection, settings, context.project_id)
        if engine:
            settings = settings.model_copy(update={"chat_llm_engine": engine})
    except Exception:  # noqa: BLE001 — a settings lookup must not fail the step on its own
        pass

    result = plan_from_instructions(
        make_agent_chat_adapter(settings),
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
    from .drive_media import download, record_artifact, upload, workspace
    from .video_edit import apply_plan, validate_plan, verify_playable

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    # VALIDATED BEFORE ANYTHING IS DOWNLOADED, let alone rendered. A plan with a bad operation
    # in the middle must cost nothing, and the error must name the operation rather than arrive
    # as a render failure twenty minutes in.
    plan = validate_plan(inputs.get("plan"))

    source = _media_artifact(context, source_artifact)
    connection_key = str(inputs.get("connection_key") or "youtube_default")
    folder_id = str(inputs.get("output_folder_id") or "")

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
        local = download(
            context.connection, context.settings,
            project_id=context.project_id, file_id=source["drive_file_id"],
            dest_dir=work, connection_key=connection_key,
        )["path"]

        assets: dict[str, str] = {}
        for artifact_id in wanted:
            asset = _media_artifact(context, artifact_id)
            assets[artifact_id] = download(
                context.connection, context.settings,
                project_id=context.project_id, file_id=asset["drive_file_id"],
                dest_dir=work, connection_key=connection_key,
            )["path"]

        result = apply_plan(local, plan, work_dir=work, artifact_paths=assets)
        info = verify_playable(
            result["path"], expect_audio=bool(inputs.get("expect_audio", True)))

        name = str(inputs.get("output_name") or "").strip() or os.path.basename(result["path"])
        written = upload(
            context.connection, context.settings,
            project_id=context.project_id, local_path=result["path"], name=name,
            folder_id=folder_id, connection_key=connection_key, actor=context.actor,
        )

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
        "drive_file_id": written["file_id"],
        "view_url": written["view_url"],
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
    from .drive_media import download, workspace
    from .video_edit import verify_playable
    from .youtube_publish import upload_video

    source_artifact = str(inputs.get("media_artifact_id") or "").strip()
    if not source_artifact:
        raise ValueError("media_artifact_id is required")
    source = _media_artifact(context, source_artifact)
    connection_key = str(inputs.get("connection_key") or "youtube_default")

    with workspace("boss-publish-") as work:
        local = download(
            context.connection, context.settings,
            project_id=context.project_id, file_id=source["drive_file_id"],
            dest_dir=work, connection_key=connection_key,
        )["path"]
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
