import subprocess
import sys
from types import SimpleNamespace

from apps.worker.boss_worker.control_plane import ControlPlaneClient, LeasedJob
from apps.worker.boss_worker.runner import (
    AGENT_WORK_PRODUCT_STRATEGIES,
    agent_module_id_from_payload,
    build_agent_brief_result,
    build_marketplace_validation_result,
    build_plan_result,
    capabilities,
    run_worker_cycle,
)


class FakeControlPlaneClient:
    def __init__(self, leased_job=None):
        self.leased_job = leased_job
        self.calls = []
        self.worker_id = None

    def poll(self, worker_id, capabilities, lease_ttl_seconds):
        self.worker_id = worker_id
        self.calls.append(("poll", worker_id, tuple(capabilities), lease_ttl_seconds))
        return self.leased_job

    def heartbeat(self, worker_id, job_id, lease_token, lease_ttl_seconds):
        self.calls.append(
            ("heartbeat", worker_id, job_id, lease_token, lease_ttl_seconds)
        )
        return {"job_id": job_id, "status": "running", "lease_expires_at": "future"}

    def complete(self, worker_id, job_id, lease_token, result):
        self.calls.append(("complete", worker_id, job_id, lease_token, result))
        return {
            "job_id": job_id,
            "status": "succeeded",
            "artifact_id": "art_test",
            "decision_id": "dec_test",
        }

    def fail(
        self, worker_id, job_id, lease_token, error, retryable, retry_delay_seconds
    ):
        self.calls.append(
            (
                "fail",
                worker_id,
                job_id,
                lease_token,
                error,
                retryable,
                retry_delay_seconds,
            )
        )
        return {"job_id": job_id, "status": "deadlettered", "deadletter_id": "dlq_test"}

    def create_openai_plan(self, payload):
        self.calls.append(("create_openai_plan", payload))
        return {
            "status": "created",
            "artifact_id": "art_plan",
            "receipt_id": "rcpt_plan",
        }

    def create_pull_request_proposal(self, payload):
        self.calls.append(("create_pull_request_proposal", payload))
        return {
            "status": "created",
            "artifact_id": "art_review",
            "diff_artifact_id": "art_diff",
            "test_report_artifact_id": "art_test_report",
            "receipt_id": "rcpt_pr",
            "decision_id": "dec_pr",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/1",
            "branch_name": payload["branch_name"],
        }

    def merge_pull_request(self, payload):
        self.calls.append(("merge_pull_request", payload))
        return {
            "status": "created",
            "artifact_id": "art_merge",
            "receipt_id": "rcpt_merge",
            "decision_id": payload["decision_id"],
            "pull_request_url": payload["pull_request_url"],
            "branch_name": payload.get("branch_name"),
            "merged_sha": "abc123",
        }

    def create_deployment(self, payload):
        self.calls.append(("create_deployment", payload))
        return {
            "status": "created",
            "artifact_id": "art_deploy",
            "receipt_id": "rcpt_deploy",
            "decision_id": payload["decision_id"],
            "environment": payload["environment"],
            "deployment_id": 12345,
            "deployment_status": "success",
            "deployment_url": "https://api.github.com/repos/seanheiney/boss-sandbox/deployments/12345",
            "merged_sha": payload["merged_sha"],
        }

    def create_infra_droplet(self, payload):
        self.calls.append(("create_infra_droplet", payload))
        return {
            "status": "created",
            "artifact_id": "art_infra",
            "receipt_id": "rcpt_infra",
            "decision_id": payload["decision_id"],
            "resource_id": "infra_test",
            "provider_ref": "https://api.digitalocean.com/v2/droplets/123",
            "provider_resource_id": "123",
            "resource_name": "boss-test",
            "expires_at": "future",
        }

    def terminate_infra_resource(self, resource_id, payload):
        self.calls.append(("terminate_infra_resource", resource_id, payload))
        return {
            "status": "terminated",
            "resource_id": resource_id,
            "artifact_id": "art_infra_term",
            "receipt_id": "rcpt_infra_term",
            "provider_ref": "https://api.digitalocean.com/v2/droplets/123",
        }

    def deliver_outbox_message(self, message_id, payload):
        self.calls.append(("deliver_outbox_message", message_id, payload))
        return {
            "status": "created",
            "message_id": message_id,
            "receipt_id": "rcpt_comms",
            "provider_ref": "slack-webhook:slack:#ops",
        }

    def deliver_webhook(self, delivery_id, payload):
        self.calls.append(("deliver_webhook", delivery_id, payload))
        if delivery_id == "whdel_fail":
            return {"status": "failed", "message": "endpoint unavailable"}
        return {
            "status": "delivered",
            "id": delivery_id,
            "receipt_id": "rcpt_webhook",
        }

    def sync_provider_resources(self, payload):
        self.calls.append(("sync_provider_resources", payload))
        if payload.get("provider_account_id") == "prov_fail":
            return {
                "project_id": payload["project_id"],
                "runs": [
                    {
                        "id": "recon_failed",
                        "status": "failed",
                        "provider": "cloudflare",
                        "provider_account_id": "prov_fail",
                        "resources_seen": 0,
                        "resources_changed": 0,
                        "costs_seen": 0,
                        "deadletter_id": "dlq_provider_sync",
                        "error": {"message": "provider credentials missing"},
                    }
                ],
                "resources_seen": 0,
                "resources_changed": 0,
                "costs_seen": 0,
            }
        return {
            "project_id": payload["project_id"],
            "runs": [
                {
                    "id": "recon_ok",
                    "status": "succeeded",
                    "provider": "digitalocean",
                    "provider_account_id": payload.get("provider_account_id"),
                    "resources_seen": 2,
                    "resources_changed": 1,
                    "costs_seen": 1,
                    "provider_health_id": "ph_ok",
                    "drift_report_id": "drift_ok",
                }
            ],
            "resources_seen": 2,
            "resources_changed": 1,
            "costs_seen": 1,
        }


def test_worker_starts_and_exits_cleanly():
    result = subprocess.run(
        [sys.executable, "-m", "apps.worker.boss_worker.main", "--once"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "worker_started" in result.stderr


def test_control_plane_client_sends_bearer_api_key():
    client = ControlPlaneClient("http://127.0.0.1:8000", api_key="worker-secret")
    try:
        assert client._client.headers["Authorization"] == "Bearer worker-secret"
    finally:
        client.close()


def test_worker_capabilities_include_infra_job_kinds():
    supported = capabilities()
    assert "infra.droplet.create" in supported
    assert "infra.droplet.delete" in supported
    assert "agent.pm.scope_brief" in supported
    assert "agent.infra_steward.infra_review" in supported
    assert "agent.model_spend_analyst.model_spend_brief" in supported
    assert "enterprise.marketplace.validate" in supported
    assert "providerops.sync.readonly" in supported
    assert "webhook.outbound.deliver" in supported


def test_agent_work_product_strategies_cover_initial_agent_capabilities():
    # "agent.resume" is a control capability (resume a suspended agent run), not a
    # work-product-producing agent kind, so it has no AGENT_WORK_PRODUCT_STRATEGIES entry.
    agent_capabilities = [
        item
        for item in capabilities()
        if item.startswith("agent.") and item != "agent.resume"
    ]

    assert len(agent_capabilities) == len(set(agent_capabilities))
    assert sorted(AGENT_WORK_PRODUCT_STRATEGIES) == sorted(agent_capabilities)
    for job_kind, strategy in AGENT_WORK_PRODUCT_STRATEGIES.items():
        assert strategy["contract"]
        assert strategy["role_logic"]
        assert strategy["sections"]
        assert strategy["evidence"]
        assert strategy["safety_gates"]
        assert strategy["required_decision_when"]


def test_build_plan_result_contains_command_context():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="plan.command",
        payload={"command": "Ship the lease worker.", "constraints": {"deadline": "today"}},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )

    result = build_plan_result(leased_job)

    assert result["summary"] == "Ship the lease worker."
    assert "Ship the lease worker." in result["objectives"]
    assert any("today" in step for step in result["steps"])


def test_build_agent_brief_result_uses_resolved_runtime_prompt_context():
    leased_job = LeasedJob(
        id="job_agent",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="agent.pm.scope_brief",
        payload={
            "command": "Scope ProviderOps alerts.",
            "agent_module": {
                "id": "pm_product_planner_v1",
                "display_name": "PM Product Planner",
                "role_key": "pm",
            },
            "agent_runtime": {
                "job_kind": "agent.pm.scope_brief",
                "matched": True,
                "role": {"role_key": "pm", "display_name": "Product Manager"},
                "compiled_prompt": {
                    "job": "Return problem statement, scope, non-goals, acceptance criteria.",
                    "checklist": ["Produce or update output contract: FeatureBrief."],
                },
            },
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )

    result = build_agent_brief_result(leased_job)

    assert result["summary"] == "Product Manager: Scope ProviderOps alerts."
    assert result["agent_module"]["id"] == "pm_product_planner_v1"
    assert result["agent_runtime"]["role_key"] == "pm"
    assert result["strategy_id"] == "agent.pm.scope_brief"
    assert result["grounding_status"] == "needs_control_plane_context"
    assert result["work_product"]["contract"] == "FeatureBrief"
    assert "acceptance_criteria" in result["work_product"]["sections"]
    assert "acceptance criteria" in result["prompt_excerpt"]


def test_build_agent_brief_result_uses_role_specific_decision_triggers():
    leased_job = LeasedJob(
        id="job_agent",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="agent.model_spend_analyst.model_spend_brief",
        payload={
            "command": "Review whether paid fallback is safe.",
            "risk_tier": 2,
            "requested_actions": ["paid_fallback"],
            "context": {"routing_decision_ids": ["route_1"], "cost_receipt_ids": []},
            "agent_module": {
                "id": "model_spend_analyst_v1",
                "display_name": "Model Spend Analyst",
                "role_key": "model_spend_analyst",
            },
            "agent_runtime": {
                "job_kind": "agent.model_spend_analyst.model_spend_brief",
                "matched": True,
                "role": {
                    "role_key": "model_spend_analyst",
                    "display_name": "Model Spend Analyst",
                },
                "compiled_prompt": {
                    "job": "Return route decisions, cooldowns, costs, and limits.",
                    "checklist": ["Optimize authorized capacity; do not bypass limits."],
                },
            },
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )

    result = build_agent_brief_result(leased_job)

    assert result["work_product"]["contract"] == "ModelSpendBrief"
    assert result["grounding_status"] == "grounded_in_payload_context"
    assert "do_not_bypass_limits" in result["work_product"]["safety_gates"]
    assert {
        "reason": "risk_tier_requires_approval",
        "risk_tier": 2,
        "default_action": "hold",
    } in result["required_decisions"]
    assert any(
        decision["reason"] == "strategy_trigger_requires_decision"
        for decision in result["required_decisions"]
    )


def test_build_marketplace_validation_result_blocks_missing_entitlement():
    leased_job = LeasedJob(
        id="job_marketplace",
        project_id="boss-dogfood",
        task_id=None,
        kind="enterprise.marketplace.validate",
        payload={
            "module_id": "private_enterprise_agent_v1",
            "required_entitlement": "private_agent_marketplace",
            "entitlement_status": "missing",
            "agent_module": {
                "id": "private_enterprise_agent_v1",
                "display_name": "Private Enterprise Agent",
                "role_key": "private_enterprise_agent",
            },
            "agent_runtime": {
                "job_kind": "enterprise.marketplace.validate",
                "matched": True,
                "compiled_prompt": {
                    "checklist": ["Produce or update output contract: EntitlementGateReport."]
                },
            },
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )

    result = build_marketplace_validation_result(leased_job)

    assert result["status"] == "blocked"
    assert result["work_product"]["contract"] == "EntitlementGateReport"
    assert result["required_decisions"] == [
        {
            "reason": "missing_marketplace_entitlement",
            "required_entitlement": "private_agent_marketplace",
            "default_action": "hold",
        }
    ]


def test_agent_module_id_from_payload_prefers_resolved_module_context():
    assert agent_module_id_from_payload({"agent_module_id": "explicit"}) == "explicit"
    assert (
        agent_module_id_from_payload({"agent_module": {"id": "pm_product_planner_v1"}})
        == "pm_product_planner_v1"
    )
    assert agent_module_id_from_payload({}) is None


def test_run_worker_cycle_completes_leased_job():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="plan.command",
        payload={"command": "Ship the lease worker.", "constraints": {}},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "complete"]


def test_run_worker_cycle_completes_generic_agent_job():
    leased_job = LeasedJob(
        id="job_agent",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="agent.pm.scope_brief",
        payload={
            "command": "Scope the packaged agent runtime.",
            "agent_module": {
                "id": "pm_product_planner_v1",
                "display_name": "PM Product Planner",
                "role_key": "pm",
            },
            "agent_runtime": {
                "job_kind": "agent.pm.scope_brief",
                "matched": True,
                "role": {"role_key": "pm", "display_name": "Product Manager"},
                "compiled_prompt": {
                    "job": "Return scope, risks, decisions, and evidence.",
                    "checklist": ["Produce or update output contract: FeatureBrief."],
                },
            },
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "complete"]
    complete_result = client.calls[-1][-1]
    assert complete_result["agent_module"]["id"] == "pm_product_planner_v1"
    assert complete_result["agent_runtime"]["role_key"] == "pm"
    assert complete_result["work_product"]["contract"] == "FeatureBrief"


def test_run_worker_cycle_executes_providerops_readonly_sync_job():
    leased_job = LeasedJob(
        id="job_providerops_sync",
        project_id="boss-dogfood",
        task_id=None,
        kind="providerops.sync.readonly",
        payload={"provider_account_id": "prov_do", "actor": "system"},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "sync_provider_resources",
        "complete",
    ]
    assert client.calls[2][1] == {
        "project_id": "boss-dogfood",
        "provider_account_id": "prov_do",
        "actor": "system",
    }
    complete_result = client.calls[-1][-1]
    assert complete_result["status"] == "succeeded"
    assert complete_result["resources_seen"] == 2
    assert complete_result["runs"][0]["id"] == "recon_ok"


def test_run_worker_cycle_deadletters_failed_providerops_sync_job():
    leased_job = LeasedJob(
        id="job_providerops_sync_failed",
        project_id="boss-dogfood",
        task_id=None,
        kind="providerops.sync.readonly",
        payload={"provider_account_id": "prov_fail", "actor": "system"},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "sync_provider_resources",
        "fail",
    ]
    assert client.calls[-1][4]["message"] == "provider credentials missing"


def test_run_worker_cycle_deadletters_unsupported_job():
    leased_job = LeasedJob(
        id="job_test",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="unsupported.kind",
        payload={},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "fail"]


def test_run_worker_cycle_executes_queued_openai_plan_job():
    leased_job = LeasedJob(
        id="job_openai",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="openai.plan",
        payload={
            "reservation_id": "res_test",
            "dedupe_key": "plan-test",
            "prompt": "Create a plan.",
            "system_prompt": "Return markdown.",
            "agent_module": {"id": "pm_product_planner_v1"},
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_openai_plan",
        "complete",
    ]
    assert client.calls[2][1]["agent_module_id"] == "pm_product_planner_v1"


def test_run_worker_cycle_executes_queued_pr_proposal_job():
    leased_job = LeasedJob(
        id="job_pr",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="software_factory.propose_pr",
        payload={
            "reservation_id": "res_test",
            "dedupe_key": "pr-test",
            "title": "Sandbox PR",
            "branch_name": "boss/pr-test",
            "summary": "Create a sandbox PR proposal.",
            "files": [{"path": "README.md", "content": "hello"}],
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_pull_request_proposal",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_outbox_delivery_job():
    leased_job = LeasedJob(
        id="job_comms",
        project_id="boss-dogfood",
        task_id=None,
        kind="comms.outbox.deliver",
        payload={
            "outbox_message_id": "out_test",
            "reservation_id": "res_test",
            "dedupe_key": "comms-test",
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "deliver_outbox_message",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_webhook_delivery_job():
    leased_job = LeasedJob(
        id="job_webhook",
        project_id="boss-dogfood",
        task_id=None,
        kind="webhook.outbound.deliver",
        payload={"delivery_id": "whdel_ok"},
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(worker_id="worker-test", lease_ttl_seconds=60)

    assert run_worker_cycle(client, settings) is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "deliver_webhook",
        "complete",
    ]
    assert client.calls[2][1:] == (
        "whdel_ok",
        {
            "project_id": "boss-dogfood",
            "created_by_job_id": "job_webhook",
            "worker_id": "worker-test",
            "lease_token": "lease_token",
        },
    )


def test_webhook_failure_uses_retryable_exponential_backoff():
    leased_job = LeasedJob(
        id="job_webhook_fail",
        project_id="boss-dogfood",
        task_id=None,
        kind="webhook.outbound.deliver",
        payload={"delivery_id": "whdel_fail"},
        attempt=3,
        max_attempts=5,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(worker_id="worker-test", lease_ttl_seconds=60)

    assert run_worker_cycle(client, settings) is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "deliver_webhook",
        "fail",
    ]
    failure = client.calls[-1]
    assert failure[4]["message"] == "endpoint unavailable"
    assert failure[5] is True
    assert failure[6] == 20


def test_run_worker_cycle_executes_queued_pr_merge_job():
    leased_job = LeasedJob(
        id="job_merge",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="github.pull_request.merge",
        payload={
            "decision_id": "dec_merge",
            "dedupe_key": "merge-test",
            "pull_request_number": 42,
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Merge Demo",
            "branch_name": "boss/demo-pr",
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "merge_pull_request",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_deployment_job():
    leased_job = LeasedJob(
        id="job_deploy",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="github.deployment.create",
        payload={
            "decision_id": "dec_deploy",
            "dedupe_key": "deploy-test",
            "merged_sha": "abc123",
            "pull_request_url": "https://github.com/seanheiney/boss-sandbox/pull/42",
            "title": "Deploy Demo",
            "branch_name": "boss/demo-pr",
            "environment": "staging",
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_deployment",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_infra_create_job():
    leased_job = LeasedJob(
        id="job_infra",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="infra.droplet.create",
        payload={
            "decision_id": "dec_infra",
            "reservation_id": "res_infra",
            "dedupe_key": "infra-test",
            "title": "Provision infra",
            "region": "nyc3",
            "size": "s-1vcpu-1gb",
            "ttl_minutes": 60,
            "tags": ["preview"],
        },
        attempt=1,
        max_attempts=1,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "create_infra_droplet",
        "complete",
    ]


def test_run_worker_cycle_executes_queued_infra_delete_job():
    leased_job = LeasedJob(
        id="job_infra_delete",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="infra.droplet.delete",
        payload={
            "resource_id": "infra_test",
            "dedupe_key": "infra-delete-test",
            "reason": "ttl_expired",
        },
        attempt=1,
        max_attempts=1,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)

    assert ran_job is True
    assert [call[0] for call in client.calls] == [
        "poll",
        "heartbeat",
        "terminate_infra_resource",
        "complete",
    ]


# Polish addition: explicit failure path + pack/agent job coverage in unit (leases/heartbeats exercised via cycle; deadletter on err; agent kinds are pack-derived)
def test_run_worker_cycle_failure_path_calls_fail_not_complete_and_deadletters():
    leased_job = LeasedJob(
        id="job_fail_unit",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="plan.command",
        payload={"command": "Will fail in execute sim."},
        attempt=1,
        max_attempts=1,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    # (unused client for first job var left for doc; use only bad_job for failure path)
    _ = FakeControlPlaneClient(leased_job)
    # monkey the execute to raise by using unsupported after setup? simulate by temp override no, use bad kind that raises in execute
    bad_job = LeasedJob(
        id="job_fail_unit2",
        project_id="boss-dogfood",
        task_id=None,
        kind="bad.pack.job.kind",  # triggers ValueError in execute_job -> fail path
        payload={},
        attempt=1,
        max_attempts=1,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client2 = FakeControlPlaneClient(bad_job)
    settings = SimpleNamespace(
        worker_id="worker-test-fail",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran = run_worker_cycle(client2, settings)
    assert ran is True
    calls = [c[0] for c in client2.calls]
    assert calls == ["poll", "heartbeat", "fail"]
    assert client2.calls[-1][4]["message"].startswith("Unsupported job kind")
    # fake fail returns deadlettered status; the appended call records the fail invocation (no need to inspect return here)


def test_run_worker_cycle_heartbeats_before_any_pack_derived_agent_execute():
    leased_job = LeasedJob(
        id="job_pack_agent_unit",
        project_id="boss-dogfood",
        task_id="tsk_test",
        kind="agent.coo.ops_digest",  # pack/role derived agent job
        payload={
            "command": "Ops digest from installed pack role.",
            "agent_module": {"id": "coo_ops_v1", "role_key": "coo"},
            "agent_runtime": {"job_kind": "agent.coo.ops_digest", "matched": True, "role": {"role_key": "coo"}},
        },
        attempt=1,
        max_attempts=3,
        lease_token="lease_token",
        lease_expires_at="future",
    )
    client = FakeControlPlaneClient(leased_job)
    settings = SimpleNamespace(
        worker_id="worker-test-pack",
        lease_ttl_seconds=60,
        poll_interval_seconds=1,
    )

    ran_job = run_worker_cycle(client, settings)
    assert ran_job is True
    assert [call[0] for call in client.calls] == ["poll", "heartbeat", "complete"]
    # heartbeat happened before execute (which for agent. builds result without client call)
