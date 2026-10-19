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
    work_item_id: Optional[str] = None
    tracker_binding_id: Optional[str] = None
    allocation_id: Optional[str] = None
    priority_score: Optional[float] = None
    workload_class: str = "general"
    resource_units: int = 1


class ControlPlaneClient:
    def __init__(
        self, base_url: str, timeout: float = 30.0, api_key: Optional[str] = None
    ):
        self.base_url = base_url.rstrip("/")
        self.worker_id: Optional[str] = None
        # Lease-control requests must not inherit the two-hour execution read timeout.
        # httpx bounds each network phase; long execution calls retain their own timeout.
        self.lease_timeout_seconds = max(0.1, min(float(timeout), 30.0))
        self._lease_timeout = httpx.Timeout(self.lease_timeout_seconds)
        headers = {"Accept": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        # Long-running jobs (software-factory PR proposals run the model + create a
        # GitHub PR + poll CI) can take a few minutes server-side. Keep connect short
        # but allow a generous read timeout so the worker doesn't abandon the call.
        # read must exceed the LONGEST single control-plane call. A code.task runs the WHOLE
        # governed pipeline (design → tests → implement → review → mutation → PR) in ONE
        # request — many stages, each up to code_agent_timeout. 3600s was still shorter
        # than a real BugBox run (verify-green then opening_pr died as "timed out").
        # The lease heartbeat keeps the lease alive; this keeps the HTTP call alive too.
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=10.0, read=7200.0, write=30.0, pool=30.0),
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
        handing_back: bool = False,
    ) -> dict[str, Any]:
        body = {
            "worker_id": worker_id,
            "job_id": job_id,
            "lease_token": lease_token,
            "lease_ttl_seconds": lease_ttl_seconds,
        }
        if handing_back:
            body["handing_back"] = True
        response = self._client.post("/v1/worker/heartbeat", json=body, timeout=self._lease_timeout)
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
            timeout=self._lease_timeout,
        )
        response.raise_for_status()
        return response.json()

    def note_code_task_redelivery(
        self, run_id: str, attempt: int, job_id: str, lease_token: str,
    ) -> dict[str, Any]:
        """Record that this attempt is a replay of interrupted work."""
        response = self._client.post(
            f"/v1/code-tasks/{run_id}/redelivered",
            json={"attempt": attempt, "job_id": job_id,
                  "lease_token": lease_token, "worker_id": self.worker_id},
        )
        response.raise_for_status()
        return response.json()

    def report_host_stall(self, stalled_seconds: float) -> dict[str, Any]:
        """Record that the machine stopped scheduling BOSS for this long."""
        response = self._client.post(
            "/v1/self/host-stall", json={"stalled_seconds": stalled_seconds})
        response.raise_for_status()
        return response.json()

    def create_openai_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/openai/plans", json=payload)
        response.raise_for_status()
        return response.json()

    def resume_agent_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = payload["run_id"]
        response = self._client.post(
            f"/v1/agent-runs/{run_id}/resume",
            json={key: payload[key] for key in ("worker_id", "job_id", "lease_token")},
        )
        response.raise_for_status()
        return response.json()

    def run_meeting_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/meetings/step", json=payload)
        response.raise_for_status()
        return response.json()

    def run_investigation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API container to run one investigation — that is where the CLI lives."""
        response = self._client.post(
            f"/v1/investigations/{payload['investigation_id']}/run", json={})
        response.raise_for_status()
        return response.json()

    def run_pr_review_again(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API container to review one build's pull request again."""
        response = self._client.post(
            f"/v1/code-tasks/{payload['run_id']}/review-pr-again/run", json=payload)
        response.raise_for_status()
        return response.json()

    def run_security_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API container to run one security scan — the scanners live there (F4)."""
        response = self._client.post(f"/v1/security/scans/{payload['scan_id']}/run", json={})
        response.raise_for_status()
        return response.json()

    def run_discovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API container to run one leased Discovery job — that is where the CLI lives."""
        response = self._client.post(
            f"/v1/discoveries/{payload['run_id']}/execute",
            json={key: payload[key] for key in ("job_id", "worker_id", "lease_token")})
        response.raise_for_status()
        return response.json()

    def run_stack_deploy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API to apply one leased stack deployment — the host credentials live there."""
        response = self._client.post(
            f"/v1/deployments/stack/{payload['deployment_id']}/execute",
            json={key: payload[key] for key in ("job_id", "worker_id", "lease_token")})
        response.raise_for_status()
        return response.json()

    def run_self_fix_build(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API to run one leased BOSS self-fix build (BOSS's own source lives there)."""
        response = self._client.post(
            f"/v1/self-fixes/{payload['self_fix_id']}/build",
            json={key: payload.get(key) for key in ("job_id", "worker_id", "lease_token",
                                                    "note")})
        response.raise_for_status()
        return response.json()

    def run_infra_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API to run one leased infrastructure operation (vault and host keys live there)."""
        response = self._client.post(
            "/v1/infra/platform/operations/execute",
            json={key: payload[key] for key in ("job_id", "worker_id", "lease_token")})
        response.raise_for_status()
        return response.json()

    def run_host_deploy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask the API to apply one leased host deployment — the host key lives there."""
        response = self._client.post(
            f"/v1/delivery/deployments/{payload['deployment_run_id']}/apply",
            json={key: payload[key] for key in ("job_id", "worker_id", "lease_token")})
        if response.status_code in (404, 409):
            # The lease is gone or the run is: nothing to retry, so the job ends here.
            return {"status": "refused", "retry": False,
                    "error": (response.text or "")[:300]}
        response.raise_for_status()
        return response.json()

    def run_code_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/code-tasks/step", json=payload)
        # Operator/host 422 (claims frozen, missing sandbox task) is fail-closed:
        # return a terminal payload so execute_job can complete instead of DLQ.
        if response.status_code == 422:
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = {"detail": (response.text or "")[:400]}
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or body)
            else:
                detail = str(body)
            blob = detail.lower()
            reason = (
                "microvm_claims_frozen"
                if "claim" in blob or "frozen" in blob
                else "sandbox_task_missing"
            )
            return {
                "status": "failed",
                "done": True,
                "error": detail or "sandbox request rejected (422)",
                "reason_code": reason,
                "failure_class": "needs_human",
                "http_status": 422,
            }
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

    def merge_change_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/change-sets/{id}/merge: a refusal (409, a sentence) is the answer, not a crash."""
        response = self._client.post(f"/v1/change-sets/{payload['change_set_id']}/merge",
                                     json={"project_id": payload["project_id"]})
        if response.status_code in (404, 409, 422):
            body = response.json() if response.content else {}
            return {"status": "refused", **(body if isinstance(body, dict) else {})}
        response.raise_for_status()
        return response.json()

    def create_deployment(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/software-factory/deployments", json=payload)
        response.raise_for_status()
        return response.json()

    def sweep_deployment_health(self) -> dict[str, Any]:
        """Probe every deployed target that is due for a health sample.

        Called on the worker's idle path. Goes through the API rather than calling `sweep()`
        in-process on purpose: probing a target resolves ITS credential and may SSH to the
        host to work out why it is unreachable, and the worker's settings object carries
        neither the artifact root nor the platform key that needs. The API has both.
        """
        response = self._client.post("/v1/delivery/health/sweep")
        response.raise_for_status()
        return response.json()

    def monitoring_tick(self) -> dict[str, Any]:
        """Run the monitor scheduler once (rule groups, synthetics, triage, notifications)."""
        response = self._client.post("/v1/monitoring/tick")
        response.raise_for_status()
        return response.json()

    def watch_running_tasks(self) -> dict[str, Any]:
        """Have the shepherd look at in-flight work and act on what is unmistakable.

        On the idle path with the health sweep: the worker is here because it has nothing to
        run, which is exactly when noticing that seven identical runs are queued costs
        nothing and saves a great deal.
        """
        response = self._client.post("/v1/shepherd/watch", json={})
        response.raise_for_status()
        return response.json()

    def shepherd_autopilot(self, project_id: str, limit: int = 10) -> dict[str, Any]:
        """Apply the remedies the shepherd already decided on.

        /shepherd/watch and /shepherd/sweep only ever PROPOSED. `act()` had no caller in the
        codebase, so every diagnosis sat in the ledger unapplied. This is what makes the
        shepherd act; the confirmation gate still holds back anything that spends.
        """
        response = self._client.post(
            "/v1/shepherd/autopilot",
            json={"project_id": project_id, "limit": limit, "actor": "shepherd"},
        )
        response.raise_for_status()
        return response.json()

    def tick_metatron_watch(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Legacy observe-only tick. Prefer tick_metatron_overwatch."""
        response = self._client.post(
            "/v1/metatron/watch",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def tick_metatron_overwatch(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Always-on captain cycle: observe, measure, validate, apply safe, verify."""
        response = self._client.post(
            "/v1/metatron/overwatch",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def tick_focused_agents(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Always-on / hybrid focused agents whose interval elapsed or a wake event arrived."""
        response = self._client.post(
            "/v1/focused-agents/tick-all",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def tick_workflow_schedules(self, project_id: str) -> dict[str, Any]:
        """Start any workflow whose schedule has come round."""
        response = self._client.post(
            "/v1/delivery-workflows/schedules/tick",
            json={"project_id": project_id, "actor": "system"},
        )
        response.raise_for_status()
        return response.json()

    def improve_tick(self) -> dict[str, Any]:
        """Scorecard upkeep and, once a day, the Improve BOSS / heartbeat send (the API owns
        both; this only knocks). Cheap when nothing is due."""
        response = self._client.post("/v1/telemetry/tick", json={})
        response.raise_for_status()
        return response.json()

    def recheck_setup(self, project_id: str) -> dict[str, Any]:
        """Re-prove Repo and models' rows whose last check is over a day old."""
        response = self._client.post(
            "/v1/setup/checks",
            json={"project_id": project_id, "actor": "system", "target": "stale"},
        )
        response.raise_for_status()
        return response.json()

    def tick_agent_processes(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Advance waiting/running agent-process handoffs."""
        response = self._client.post(
            "/v1/agent-processes/tick-all",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def tick_factory_flow(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Observe factory/swarm stalls and record findings. Never edits code."""
        response = self._client.post(
            "/v1/factory/flow/tick",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def tick_agent_wakeups(self, project_id: str = "") -> dict[str, Any]:
        """Fire monitors and schedules that are due.

        Bounded per sweep: this is the one loop that spends model budget with nobody at the
        keyboard, so it starts a handful of turns and comes back next tick rather than
        draining the queue in one go.
        """
        response = self._client.post(
            "/v1/agent/wakeups/tick",
            json={"project_id": project_id, "limit": 5},
        )
        response.raise_for_status()
        return response.json()

    def tick_process_manager(self, project_id: str, actor: str = "system") -> dict[str, Any]:
        """Always-on Project Manager steward cycle. Fail-closed; never invents scores."""
        response = self._client.post(
            "/v1/process-manager/tick",
            json={"project_id": project_id, "actor": actor},
        )
        response.raise_for_status()
        return response.json()

    def sweep_qa_benchmark_evaluations(self) -> dict[str, Any]:
        """Persist changed terminal benchmark reports as tuning evidence snapshots.

        Unlike an improvement rollout, this is analysis-only: the API does not create
        a candidate diff, spend budget, or apply any configuration. It may advance a
        measurement baseline after the separately governed rollout is already complete.
        """
        response = self._client.post("/v1/qa/benchmark-apps/tuning-sweep", json={})
        response.raise_for_status()
        return response.json()

    def run_delivery_capabilities(self, project_id: str) -> dict[str, Any]:
        """Advance customer delivery workflows that have work ready.

        Product workflows only. BOSS's own definitions are driven by the machinery that has
        always driven them — a runner reaching across the namespace could execute the governed
        coding pipeline twice.
        """
        response = self._client.post(
            "/v1/delivery-workflows/run-capabilities",
            json={"project_id": project_id, "worker_id": "boss-worker"},
        )
        response.raise_for_status()
        return response.json()

    def prune_activity_log(self) -> dict[str, Any]:
        """Age captured transcripts out of the activity log past the retention window.

        Goes through the API rather than the worker touching the database directly, like every
        other sweep here: one place owns the retention floor, so it cannot be shortened by
        deploying a differently-configured worker.
        """
        response = self._client.post("/v1/activity/retention/prune", json={})
        response.raise_for_status()
        return response.json()

    def acquire_model_gateway_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post("/v1/model-gateway/leases", json=payload)
        response.raise_for_status()
        return response.json()

    def heartbeat_model_gateway_lease(
        self, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/model-gateway/leases/{lease_id}/heartbeat", json=payload
        )
        response.raise_for_status()
        return response.json()

    def start_model_gateway_invocation(
        self, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/model-gateway/leases/{lease_id}/invocations", json=payload
        )
        response.raise_for_status()
        return response.json()

    def settle_model_gateway_invocation(
        self, lease_id: str, invocation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/model-gateway/leases/{lease_id}/invocations/{invocation_id}/settle",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def release_model_gateway_lease(
        self, lease_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/model-gateway/leases/{lease_id}/release", json=payload
        )
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

    def dispatch_a2a_message(
        self, message_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/a2a/messages/{message_id}/dispatch", json=payload
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

    def run_github_reconciliation(
        self, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/github/reconciliation-runs/{run_id}/execute",
            json=payload,
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

    def sync_work_tracker(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            "/v1/work-orchestration/tracker-sync/execute",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    def run_orchestrator_next_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            "/v1/orchestration/next-move/execute",
            json=payload,
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
            timeout=self._lease_timeout,
        )
        response.raise_for_status()
        return response.json()
