from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class LeasedJob:
    id: str
    project_id: str
    task_id: Optional[str]
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    lease_token: str
    lease_expires_at: str
    data_boundary: str = "internal"


class ControlPlaneClient:
    def __init__(
        self, base_url: str, timeout: float = 30.0, api_key: Optional[str] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.worker_id: Optional[str] = None
        headers = {"Accept": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        # Long-running jobs (software-factory PR proposals run the model + create a
        # GitHub PR + poll CI) can take a few minutes server-side. Keep connect short
        # but allow a generous read timeout so the worker doesn't abandon the call.
        # read must exceed the LONGEST single control-plane call. A code.task runs the WHOLE
        # governed pipeline (design → tests → implement → review → mutation → PR) in ONE
        # request — many stages, each up to code_agent_timeout — so 900s was shorter than a
        # real pipeline and the worker abandoned still-running jobs. The lease heartbeat keeps
        # the lease alive; this keeps the HTTP call alive too.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=3600.0, write=30.0, pool=30.0),
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def poll(
        self,
        worker_id: str,
        capabilities: list[str],
        lease_ttl_seconds: int,
    ) -> Optional[LeasedJob]:
        self.worker_id = worker_id
        response = self._client.post(
            "/v1/worker/poll",
            json={
                "worker_id": worker_id,
                "capabilities": capabilities,
                "lease_ttl_seconds": lease_ttl_seconds,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload["status"] == "idle" or payload.get("job") is None:
            return None
        return LeasedJob(**payload["job"])

    def heartbeat(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        lease_ttl_seconds: int,
    ) -> dict[str, Any]:
        response = self._client.post(
            "/v1/worker/heartbeat",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "lease_token": lease_token,
                "lease_ttl_seconds": lease_ttl_seconds,
            },
        )
        response.raise_for_status()
        return response.json()

    def complete(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.post(
            "/v1/worker/complete",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "lease_token": lease_token,
                "result": result,
            },
        )
        response.raise_for_status()
        return response.json()

    def create_openai_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/openai/plans", json=payload)
        response.raise_for_status()
        return response.json()

    def resume_agent_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        response = self._client.post(f"/v1/agent-runs/{run_id}/resume", json={})
        response.raise_for_status()
        return response.json()

    def run_meeting_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/meetings/step", json=payload)
        response.raise_for_status()
        return response.json()

    def run_code_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/code-tasks/step", json=payload)
        response.raise_for_status()
        return response.json()

    def run_docs_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/docs/update/execute", json=payload)
        response.raise_for_status()
        return response.json()

    def create_pull_request_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/proposals", json=payload)
        response.raise_for_status()
        return response.json()

    def merge_pull_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/merges", json=payload)
        response.raise_for_status()
        return response.json()

    def create_deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/deployments", json=payload)
        response.raise_for_status()
        return response.json()

    def create_infra_droplet(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/infra/droplets", json=payload)
        response.raise_for_status()
        return response.json()

    def terminate_infra_resource(
        self, resource_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/infra/resources/{resource_id}/terminate",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def deliver_outbox_message(
        self, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/comms/outbox/{message_id}/deliver", json=payload
        )
        response.raise_for_status()
        return response.json()

    def deliver_webhook(
        self, delivery_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/webhook-deliveries/{delivery_id}/deliver", json=payload
        )
        response.raise_for_status()
        return response.json()

    def run_github_community_job(
        self, job_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/github/community/jobs/{job_id}/execute", json=payload
        )
        response.raise_for_status()
        return response.json()

    def invoke_skill(
        self, skill_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/skills/{skill_id}/invoke", json=payload
        )
        response.raise_for_status()
        return response.json()

    def sync_provider_resources(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/resources/sync", json=payload)
        response.raise_for_status()
        return response.json()

    def fail(
        self,
        worker_id: str,
        job_id: str,
        lease_token: str,
        error: dict[str, Any],
        retryable: bool,
        retry_delay_seconds: int,
    ) -> dict[str, Any]:
        response = self._client.post(
            "/v1/worker/fail",
            json={
                "worker_id": worker_id,
                "job_id": job_id,
                "lease_token": lease_token,
                "error": error,
                "retryable": retryable,
                "retry_delay_seconds": retry_delay_seconds,
            },
        )
        response.raise_for_status()
        return response.json()
