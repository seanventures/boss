from __future__ import annotations

import logging
from typing import Any

from .config import WorkerSettings
from .control_plane import ControlPlaneClient, LeasedJob


LOGGER = logging.getLogger("boss.worker")


def capabilities() -> list[str]:
    return [
        "agent.cao.architecture_review",
        "agent.coo.ops_digest",
        "agent.cto.implementation_plan",
        "agent.deployment_manager.deployment_review",
        "agent.finops_sentinel.cost_brief",
        "agent.infra_steward.infra_review",
        "agent.metatron.status_brief",
        "agent.model_spend_analyst.model_spend_brief",
        "agent.pm.scope_brief",
        "agent.program_manager.delivery_plan",
        "agent.providerops_manager.providerops_brief",
        "agent.qa.validation_report",
        "agent.release_manager.release_packet",
        "agent.resource_janitor.cleanup_brief",
        "agent.secrets_steward.secret_boundary_review",
        "agent.vp_agents.role_review",
        "plan.command",
        "plan.generate",
        "openai.plan",
        "software_factory.propose_pr",
        "github.pull_request.merge",
        "github.deployment.create",
        "infra.droplet.create",
        "infra.droplet.delete",
        "comms.outbox.deliver",
    ]


def build_agent_brief_result(job: LeasedJob) -> dict[str, Any]:
    agent_runtime = job.payload.get("agent_runtime") or {}
    agent_module = job.payload.get("agent_module") or {}
    role = agent_runtime.get("role") if isinstance(agent_runtime.get("role"), dict) else {}
    compiled_prompt = (
        agent_runtime.get("compiled_prompt")
        if isinstance(agent_runtime.get("compiled_prompt"), dict)
        else {}
    )
    role_name = role.get("display_name") or agent_module.get("display_name") or job.kind
    prompt_job = str(compiled_prompt.get("job") or "").strip()
    payload_summary = job.payload.get("summary") or job.payload.get("command") or job.kind
    return {
        "summary": f"{role_name}: {payload_summary}",
        "agent_module": agent_module,
        "agent_runtime": {
            "job_kind": agent_runtime.get("job_kind"),
            "matched": agent_runtime.get("matched"),
            "role_key": role.get("role_key"),
            "checklist": compiled_prompt.get("checklist") or [],
        },
        "findings": [
            f"Reviewed assigned job kind {job.kind}.",
            "Applied BOSS invariants, role boundaries, and output contracts.",
        ],
        "recommendations": [
            "Create or update durable artifacts for any material output.",
            "Escalate budget, approval, provider-write, or scope-expansion blockers.",
        ],
        "required_decisions": [],
        "evidence_refs": job.payload.get("evidence_refs") or [],
        "prompt_excerpt": prompt_job[:1200],
    }


def build_plan_result(job: LeasedJob) -> dict[str, Any]:
    command = str(job.payload.get("command") or "Plan the requested task.")
    constraints = job.payload.get("constraints") or {}
    deadline = constraints.get("deadline")
    agent_runtime = job.payload.get("agent_runtime") or {}
    agent_module = job.payload.get("agent_module") or {}
    module_name = (
        agent_module.get("display_name")
        if isinstance(agent_module.get("display_name"), str)
        else None
    )
    prompt_bundle = (
        agent_module.get("prompt_bundle")
        if isinstance(agent_module.get("prompt_bundle"), dict)
        else {}
    )
    compiled_prompt = (
        agent_runtime.get("compiled_prompt")
        if isinstance(agent_runtime.get("compiled_prompt"), dict)
        else {}
    )
    planning_guidance = (
        compiled_prompt.get("job")
        if isinstance(compiled_prompt.get("job"), str)
        else prompt_bundle.get("planning")
    )

    steps = [
        "Clarify task scope and acceptance criteria.",
        "Break the work into small implementation slices.",
        "Run validation and capture receipts, artifacts, and decisions.",
    ]
    risks = [
        "Missing credentials or budget gates can block downstream execution.",
        "Unclear acceptance criteria can widen the task without approval.",
    ]
    done_when = [
        "A reviewable implementation plan exists as an artifact.",
        "A blocking decision card is ready for human approval.",
    ]
    if deadline:
        steps.append(f"Respect the requested deadline: {deadline}.")
    if isinstance(planning_guidance, str) and planning_guidance.strip():
        steps.append(f"Module guidance: {planning_guidance.strip()}")

    return {
        "summary": command,
        "objectives": [command],
        "steps": steps,
        "risks": risks,
        "done_when": done_when,
        "agent_module": {
            "id": agent_module.get("id"),
            "version": agent_module.get("version"),
            "display_name": module_name,
            "role_key": agent_module.get("role_key"),
        }
        if agent_module
        else {},
        "agent_runtime": {
            "job_kind": agent_runtime.get("job_kind"),
            "matched": agent_runtime.get("matched"),
            "checklist": compiled_prompt.get("checklist") or [],
        }
        if agent_runtime
        else {},
    }


def agent_module_id_from_payload(payload: dict[str, Any]) -> Any:
    if payload.get("agent_module_id"):
        return payload.get("agent_module_id")
    agent_module = payload.get("agent_module")
    if isinstance(agent_module, dict):
        return agent_module.get("id")
    return None


def execute_job(job: LeasedJob, client: ControlPlaneClient) -> dict[str, Any]:
    if job.kind.startswith("agent."):
        return build_agent_brief_result(job)
    if job.kind in {"plan.command", "plan.generate"}:
        return build_plan_result(job)
    if job.kind == "openai.plan":
        result = client.create_openai_plan(
            {
                "project_id": job.project_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "prompt": job.payload["prompt"],
                "system_prompt": job.payload.get("system_prompt"),
                "agent_module_id": agent_module_id_from_payload(job.payload),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "OpenAI plan job failed.")
        return result
    if job.kind == "software_factory.propose_pr":
        result = client.create_pull_request_proposal(
            {
                "project_id": job.project_id,
                "task_id": job.task_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "title": job.payload["title"],
                "branch_name": job.payload["branch_name"],
                "summary": job.payload["summary"],
                "files": job.payload["files"],
                "agent_module_id": agent_module_id_from_payload(job.payload),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Software-factory proposal job failed."
            )
        return result
    if job.kind == "github.pull_request.merge":
        result = client.merge_pull_request(
            {
                "project_id": job.project_id,
                "decision_id": job.payload["decision_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "pull_request_number": job.payload["pull_request_number"],
                "pull_request_url": job.payload["pull_request_url"],
                "title": job.payload["title"],
                "branch_name": job.payload.get("branch_name"),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Pull-request merge failed.")
        return result
    if job.kind == "github.deployment.create":
        result = client.create_deployment(
            {
                "project_id": job.project_id,
                "decision_id": job.payload["decision_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "merged_sha": job.payload["merged_sha"],
                "pull_request_url": job.payload["pull_request_url"],
                "title": job.payload["title"],
                "branch_name": job.payload.get("branch_name"),
                "environment": job.payload["environment"],
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Deployment creation failed.")
        return result
    if job.kind == "infra.droplet.create":
        result = client.create_infra_droplet(
            {
                "project_id": job.project_id,
                "task_id": job.task_id,
                "decision_id": job.payload["decision_id"],
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "title": job.payload["title"],
                "region": job.payload["region"],
                "size": job.payload["size"],
                "ttl_minutes": job.payload["ttl_minutes"],
                "tags": job.payload.get("tags") or [],
                "data_boundary": job.payload.get("data_boundary"),
                "created_by_job_id": job.id,
            }
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Infrastructure provisioning failed."
            )
        return result
    if job.kind == "infra.droplet.delete":
        result = client.terminate_infra_resource(
            job.payload["resource_id"],
            {
                "project_id": job.project_id,
                "dedupe_key": job.payload["dedupe_key"],
                "reason": job.payload.get("reason"),
                "created_by_job_id": job.id,
            },
        )
        if result.get("status") == "failed":
            raise RuntimeError(
                result.get("message") or "Infrastructure termination failed."
            )
        return result
    if job.kind == "comms.outbox.deliver":
        result = client.deliver_outbox_message(
            job.payload["outbox_message_id"],
            {
                "project_id": job.project_id,
                "reservation_id": job.payload["reservation_id"],
                "dedupe_key": job.payload["dedupe_key"],
                "created_by_job_id": job.id,
            },
        )
        if result.get("status") == "failed":
            raise RuntimeError(result.get("message") or "Outbound communication failed.")
        return result
    raise ValueError(f"Unsupported job kind: {job.kind}")


def run_worker_cycle(client: ControlPlaneClient, settings: WorkerSettings) -> bool:
    job = client.poll(
        settings.worker_id,
        capabilities(),
        settings.lease_ttl_seconds,
    )
    if not job:
        LOGGER.info("worker_idle worker_id=%s", settings.worker_id)
        return False

    LOGGER.info(
        "job_leased worker_id=%s job_id=%s kind=%s attempt=%s",
        settings.worker_id,
        job.id,
        job.kind,
        job.attempt,
    )
    client.heartbeat(
        settings.worker_id,
        job.id,
        job.lease_token,
        settings.lease_ttl_seconds,
    )

    try:
        result = execute_job(job, client)
    except Exception as error:
        LOGGER.exception(
            "job_execution_failed worker_id=%s job_id=%s kind=%s",
            settings.worker_id,
            job.id,
            job.kind,
        )
        client.fail(
            settings.worker_id,
            job.id,
            job.lease_token,
            {
                "message": str(error),
                "type": error.__class__.__name__,
            },
            retryable=False,
            retry_delay_seconds=0,
        )
        return True

    completion = client.complete(
        settings.worker_id,
        job.id,
        job.lease_token,
        result,
    )
    LOGGER.info(
        "job_completed worker_id=%s job_id=%s status=%s artifact_id=%s decision_id=%s",
        settings.worker_id,
        job.id,
        completion["status"],
        completion.get("artifact_id"),
        completion.get("decision_id"),
    )
    return True
