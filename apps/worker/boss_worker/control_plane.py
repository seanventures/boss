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


class ControlPlaneClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def poll(
        self,
        worker_id: str,
        capabilities: list[str],
        lease_ttl_seconds: int,
    ) -> Optional[LeasedJob]:
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

    def create_pull_request_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/proposals", json=payload)
        response.raise_for_status()
        return response.json()

    def merge_pull_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/merges", json=payload)
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
