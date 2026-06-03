from __future__ import annotations

import logging
from typing import Any

from .config import WorkerSettings
from .control_plane import ControlPlaneClient, LeasedJob


LOGGER = logging.getLogger("boss.worker")


def capabilities() -> list[str]:
    return [
        "plan.command",
        "plan.generate",
        "openai.plan",
        "software_factory.propose_pr",
        "github.pull_request.merge",
    ]


def build_plan_result(job: LeasedJob) -> dict[str, Any]:
    command = str(job.payload.get("command") or "Plan the requested task.")
    constraints = job.payload.get("constraints") or {}
    deadline = constraints.get("deadline")

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

    return {
        "summary": command,
        "objectives": [command],
        "steps": steps,
        "risks": risks,
        "done_when": done_when,
    }


def execute_job(job: LeasedJob, client: ControlPlaneClient) -> dict[str, Any]:
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
