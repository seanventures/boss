from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from apps.api.app.services.a2a import A2AService, A2AStateError
from apps.api.app.services.agent_studio import AgentStudioService
from apps.api.app.services.jobs import JobLeaseService
from apps.worker.boss_worker.control_plane import LeasedJob
from apps.worker.boss_worker.runner import build_agent_brief_result


class FakeStreamsTransport:
    def __init__(self):
        self.messages: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self.pending: dict[
            tuple[str, str], dict[str, dict[str, str]]
        ] = {}
        self.acks: list[tuple[str, str, str]] = []

    def health(self) -> dict[str, Any]:
        return {"status": "healthy", "configured": True, "latency_ms": 1}

    def ensure_group(self, stream: str, group: str) -> None:
        self.groups.add((stream, group))

    def publish(
        self, stream: str, fields: dict[str, str], maxlen: int
    ) -> str:
        stream_id = f"{len(self.messages.get(stream, [])) + 1}-0"
        self.messages.setdefault(stream, []).append((stream_id, fields))
        return stream_id

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int,
        block_ms: int,
    ) -> list[tuple[str, dict[str, str]]]:
        del consumer, block_ms
        pending = self.pending.setdefault((stream, group), {})
        fresh = [
            (stream_id, fields)
            for stream_id, fields in self.messages.get(stream, [])
            if stream_id not in pending
        ][:count]
        pending.update(dict(fresh))
        return fresh

    def reclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]:
        del consumer, min_idle_ms
        return list(self.pending.get((stream, group), {}).items())[:count]

    def acknowledge(self, stream: str, group: str, stream_id: str) -> int:
        self.acks.append((stream, group, stream_id))
        self.pending.setdefault((stream, group), {}).pop(stream_id, None)
        return 1


def _leased_job(item: dict[str, Any]) -> LeasedJob:
    return LeasedJob(
        id=item["id"],
        project_id=item["project_id"],
        task_id=item["task_id"],
        kind=item["kind"],
        payload=item["payload"],
        attempt=item["attempt"],
        max_attempts=item["max_attempts"],
        lease_token=item["lease_token"],
        lease_expires_at=item["lease_expires_at"],
        data_boundary=item["data_boundary"],
    )


def test_seeded_catalog_compiles_and_runs_a_specialist(engine):
    settings = get_settings()
    studio = AgentStudioService(settings)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        agents = studio.list_agents(connection, settings.default_project_id)
        panels = studio.list_panels(connection, settings.default_project_id)
        assert len(agents) == 13
        assert len(panels) == 3
        assert all(item["status"] == "active" for item in agents)
        assert all(item["status"] == "active" for item in panels)

        backend = next(item for item in agents if item["agent_key"] == "backend_engineer")
        test_run = studio.run_agent_test(
            connection,
            project_id=settings.default_project_id,
            agent_version_id=backend["active_version_id"],
            scenario={"objective": "Prove the backend authority envelope."},
            assertions=None,
            actor="vp_agents",
        )
        assert test_run["status"] == "passed"
        assert test_run["result"]["model_calls"] == 0
        assert test_run["result"]["external_effects"] == 0

        run = studio.start_agent_run(
            connection,
            project_id=settings.default_project_id,
            agent_version_id=backend["active_version_id"],
            input_data={
                "objective": "Design an idempotent endpoint.",
                "evidence_refs": ["spec:agent-studio"],
            },
            actor="vp_agents",
            dedupe_key="test:backend-specialist",
        )
        assert run["status"] == "queued"
        assert run["compiled_context"]["effective_skills"] == [
            "github.create_pr_sandbox",
            "model.generate_code",
        ]

        leased = jobs.claim_job(
            connection, "worker-studio", ["agent.studio.execute"], 120
        )
        assert leased is not None
        result = build_agent_brief_result(_leased_job(leased))
        completion = jobs.complete(
            connection,
            "worker-studio",
            leased["id"],
            leased["lease_token"],
            result,
        )
        assert completion["status"] == "succeeded"
        assert completion["artifact_id"]

        completed = studio.get_agent_run(
            connection, settings.default_project_id, run["id"]
        )
        assert completed["status"] == "completed"
        assert completion["artifact_id"] in completed["evidence"]


def test_product_manager_persists_declared_prd_work_product(engine):
    settings = get_settings()
    studio = AgentStudioService(settings)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        product_manager = next(
            item
            for item in studio.list_agents(
                connection,
                settings.default_project_id,
            )
            if item["agent_key"] == "product_manager"
        )
        run = studio.start_agent_run(
            connection,
            project_id=settings.default_project_id,
            agent_version_id=product_manager["active_version_id"],
            input_data={
                "objective": "Revise the allocation PRD and linked acceptance tickets.",
                "evidence_refs": ["spec:WOS-01", "plane:BOSS-270"],
            },
            actor="vp_agents",
            dedupe_key="test:product-manager-prd",
        )

        leased = jobs.claim_job(
            connection,
            "worker-product-manager",
            ["agent.studio.execute"],
            120,
        )
        assert leased is not None
        result = build_agent_brief_result(_leased_job(leased))
        assert "prd_changes" in result["work_product"]["sections"]
        assert "ticket_changes" in result["work_product"]["sections"]

        completion = jobs.complete(
            connection,
            "worker-product-manager",
            leased["id"],
            leased["lease_token"],
            result,
        )
        artifact = connection.execute(
            text(
                """
                SELECT artifact_type, content
                FROM artifacts
                WHERE id=:artifact_id
                """
            ),
            {"artifact_id": completion["artifact_id"]},
        ).mappings().one()
        assert artifact["artifact_type"] == "product_delivery_brief"
        assert artifact["content"]["output_contract"]["artifact_type"] == (
            "product_delivery_brief"
        )

        completed = studio.get_agent_run(
            connection,
            settings.default_project_id,
            run["id"],
        )
        assert completed["status"] == "completed"
        assert completion["artifact_id"] in completed["evidence"]


def test_panel_fans_out_under_leases_and_preserves_attributed_evidence(engine):
    settings = get_settings()
    studio = AgentStudioService(settings)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        panel = next(
            item
            for item in studio.list_panels(connection, settings.default_project_id)
            if item["panel_key"] == "release_readiness_panel"
        )
        run = studio.start_panel_run(
            connection,
            project_id=settings.default_project_id,
            panel_version_id=panel["active_version_id"],
            input_data={
                "objective": "Assess release readiness.",
                "evidence_refs": ["pr:123", "test:local"],
            },
            context={"release": "v0.9.0"},
            actor="vp_agents",
            dedupe_key="test:release-panel",
        )
        assert run["status"] == "running"
        assert len(run["member_runs"]) == 5

        for index in range(5):
            leased = jobs.claim_job(
                connection,
                f"panel-worker-{index}",
                ["agent.studio.execute"],
                120,
            )
            assert leased is not None
            result = build_agent_brief_result(_leased_job(leased))
            jobs.complete(
                connection,
                f"panel-worker-{index}",
                leased["id"],
                leased["lease_token"],
                result,
            )

        completed = studio.get_panel_run(
            connection, settings.default_project_id, run["id"]
        )
        assert completed["status"] == "completed"
        assert len(completed["findings"]) == 5
        assert completed["consolidation"]["consensus_is_not_proof"] is True
        assert completed["consolidation"]["evidence_count"] >= 5
        assert all(finding["member_key"] for finding in completed["findings"])


def test_a2a_uses_durable_outbox_receipt_delivery_and_bound_ack(engine):
    settings = get_settings()
    transport = FakeStreamsTransport()
    a2a = A2AService(settings, transport=transport)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        channel = a2a.create_channel(
            connection,
            project_id=settings.default_project_id,
            channel_key="test_coordination",
            name="Test coordination",
            description="A bounded test channel.",
            allowed_message_kinds=["instruction", "status"],
            allowed_schema_versions={"boss.a2a": [1]},
            retention_seconds=3600,
            max_payload_bytes=8192,
            actor="coo",
        )
        subscription = a2a.create_subscription(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            subscriber_kind="operator",
            subscriber_id="console",
            consumer_group="console_group",
            recipient_filter={"kind": "operator", "id": "console"},
            ack_mode="processed",
            visibility_timeout_seconds=60,
            max_attempts=3,
            data_boundaries=["internal"],
            actor="coo",
        )
        message = a2a.publish(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            message_kind="instruction",
            schema_name="boss.a2a",
            schema_version=1,
            sender_kind="agent",
            sender_id="chief_operator",
            recipient={"kind": "operator", "id": "console"},
            payload={"objective": "Coordinate the test task."},
            dedupe_key="test:a2a:instruction:1",
            actor="coo",
        )
        assert message["status"] == "queued"
        assert message["delivery_count"] == 1
        with pytest.raises(
            A2AStateError, match="schema version is not allowed"
        ):
            a2a.publish(
                connection,
                project_id=settings.default_project_id,
                channel_id=channel["id"],
                message_kind="instruction",
                schema_name="boss.a2a",
                schema_version=2,
                sender_kind="agent",
                sender_id="chief_operator",
                recipient={"kind": "operator", "id": "console"},
                payload={"objective": "Reject an unknown schema version."},
                dedupe_key="test:a2a:instruction:unsupported-schema",
                actor="coo",
            )

        duplicate = a2a.publish(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            message_kind="instruction",
            schema_name="boss.a2a",
            schema_version=1,
            sender_kind="agent",
            sender_id="chief_operator",
            recipient={"kind": "operator", "id": "console"},
            payload={"objective": "Coordinate the test task."},
            dedupe_key="test:a2a:instruction:1",
            actor="coo",
        )
        assert duplicate["duplicate"] is True
        assert duplicate["id"] == message["id"]

        leased = jobs.claim_job(connection, "worker-a2a", ["a2a.dispatch"], 120)
        assert leased is not None
        dispatched = a2a.dispatch(
            connection,
            project_id=settings.default_project_id,
            message_id=message["id"],
            created_by_job_id=leased["id"],
            actor="worker-a2a",
        )
        assert dispatched["status"] == "delivering"
        assert dispatched["receipt_id"]
        assert dispatched["redis_stream_id"] == "1-0"
        jobs.complete(
            connection,
            "worker-a2a",
            leased["id"],
            leased["lease_token"],
            dispatched,
        )

        claimed = a2a.poll(
            connection,
            project_id=settings.default_project_id,
            subscription_id=subscription["id"],
            consumer_name="console-1",
            count=10,
            block_ms=0,
            actor="coo",
        )
        assert len(claimed) == 1
        delivery_id = claimed[0]["delivery_id"]

        acknowledged = a2a.acknowledge(
            connection,
            project_id=settings.default_project_id,
            delivery_id=delivery_id,
            acknowledgement="processed",
            actor_kind="operator",
            actor_id="console",
            result={"status": "accepted"},
            evidence_refs=["artifact:test"],
            reason=None,
        )
        assert acknowledged["status"] == "acknowledged"
        assert transport.acks
        ack_receipt_id = acknowledged["acknowledgements"][0]["receipt_id"]
        assert ack_receipt_id

        final = a2a.get_message(
            connection, settings.default_project_id, message["id"]
        )
        assert final["status"] == "acknowledged"
        assert final["acknowledged_count"] == 1
        receipt_status = connection.execute(
            text("SELECT status FROM receipts WHERE id=:id"),
            {"id": final["receipt_id"]},
        ).scalar_one()
        assert receipt_status == "succeeded"
        ack_receipt_status = connection.execute(
            text("SELECT status FROM receipts WHERE id=:id"),
            {"id": ack_receipt_id},
        ).scalar_one()
        assert ack_receipt_status == "succeeded"

        replayed = a2a.replay(
            connection,
            project_id=settings.default_project_id,
            message_id=message["id"],
            dedupe_key="test:a2a:instruction:replay:1",
            reason="Operator requested a reproducible replay.",
            actor="coo",
        )
        assert replayed["status"] == "queued"
        assert replayed["causation_id"] == message["id"]
        assert replayed["trace_id"] == message["trace_id"]
        assert replayed["lineage"]["replayed_from"] == message["id"]

        cancelled = a2a.cancel(
            connection,
            project_id=settings.default_project_id,
            message_id=replayed["id"],
            actor="coo",
            reason="Replay no longer required.",
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled["outbox_status"] == "cancelled"
        duplicate_cancel = a2a.cancel(
            connection,
            project_id=settings.default_project_id,
            message_id=replayed["id"],
            actor="coo",
            reason="Replay no longer required.",
        )
        assert duplicate_cancel["duplicate"] is True


def test_a2a_filters_unmatched_groups_and_deadletters_stale_claims(engine):
    settings = get_settings()
    transport = FakeStreamsTransport()
    a2a = A2AService(settings, transport=transport)
    jobs = JobLeaseService(settings)

    with engine.begin() as connection:
        channel = a2a.create_channel(
            connection,
            project_id=settings.default_project_id,
            channel_key="test_reclaim",
            name="Test reclaim",
            description="A bounded reclaim channel.",
            allowed_message_kinds=["status"],
            allowed_schema_versions={"boss.a2a": [1]},
            retention_seconds=3600,
            max_payload_bytes=8192,
            actor="coo",
        )
        target = a2a.create_subscription(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            subscriber_kind="operator",
            subscriber_id="target",
            consumer_group="target_group",
            recipient_filter={"kind": "operator", "id": "target"},
            ack_mode="processed",
            visibility_timeout_seconds=5,
            max_attempts=1,
            data_boundaries=["internal"],
            actor="coo",
        )
        observer = a2a.create_subscription(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            subscriber_kind="operator",
            subscriber_id="observer",
            consumer_group="observer_group",
            recipient_filter={"kind": "operator", "id": "observer"},
            ack_mode="processed",
            visibility_timeout_seconds=5,
            max_attempts=1,
            data_boundaries=["internal"],
            actor="coo",
        )
        message = a2a.publish(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            message_kind="status",
            schema_name="boss.a2a",
            schema_version=1,
            sender_kind="system",
            sender_id="scheduler",
            recipient={"kind": "operator", "id": "target"},
            payload={"summary": "Reclaim me if the consumer disappears."},
            dedupe_key="test:a2a:reclaim:1",
            actor="coo",
        )
        leased = jobs.claim_job(
            connection, "worker-a2a-reclaim", ["a2a.dispatch"], 120
        )
        assert leased is not None
        dispatched = a2a.dispatch(
            connection,
            project_id=settings.default_project_id,
            message_id=message["id"],
            created_by_job_id=leased["id"],
            actor="worker-a2a-reclaim",
        )
        jobs.complete(
            connection,
            "worker-a2a-reclaim",
            leased["id"],
            leased["lease_token"],
            dispatched,
        )

        assert (
            a2a.poll(
                connection,
                project_id=settings.default_project_id,
                subscription_id=observer["id"],
                consumer_name="observer-1",
                count=10,
                block_ms=0,
                actor="coo",
            )
            == []
        )
        first_claim = a2a.poll(
            connection,
            project_id=settings.default_project_id,
            subscription_id=target["id"],
            consumer_name="target-1",
            count=10,
            block_ms=0,
            actor="coo",
        )
        assert len(first_claim) == 1
        claimed_delivery = a2a.get_delivery(
            connection,
            settings.default_project_id,
            first_claim[0]["delivery_id"],
        )
        assert claimed_delivery["attempt"] == 1

        assert (
            a2a.poll(
                connection,
                project_id=settings.default_project_id,
                subscription_id=target["id"],
                consumer_name="target-2",
                count=10,
                block_ms=0,
                actor="coo",
            )
            == []
        )
        terminal = a2a.get_message(
            connection, settings.default_project_id, message["id"]
        )
        assert terminal["status"] == "deadlettered"
        delivery_status = connection.execute(
            text(
                "SELECT status FROM a2a_deliveries "
                "WHERE envelope_id=:message_id"
            ),
            {"message_id": message["id"]},
        ).scalar_one()
        assert delivery_status == "deadlettered"
        ack_receipts = connection.execute(
            text(
                "SELECT count(*) FROM receipts "
                "WHERE project_id=:project_id "
                "AND effect_kind='a2a.redis.acknowledge'"
            ),
            {"project_id": settings.default_project_id},
        ).scalar_one()
        assert ack_receipts == 2

        auto = a2a.create_subscription(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            subscriber_kind="operator",
            subscriber_id="auto",
            consumer_group="auto_group",
            recipient_filter={"kind": "operator", "id": "auto"},
            ack_mode="none",
            visibility_timeout_seconds=5,
            max_attempts=3,
            data_boundaries=["internal"],
            actor="coo",
        )
        auto_message = a2a.publish(
            connection,
            project_id=settings.default_project_id,
            channel_id=channel["id"],
            message_kind="status",
            schema_name="boss.a2a",
            schema_version=1,
            sender_kind="system",
            sender_id="scheduler",
            recipient={"kind": "operator", "id": "auto"},
            payload={"summary": "Fire-and-forget delivery."},
            dedupe_key="test:a2a:auto-ack:1",
            actor="coo",
        )
        auto_job = jobs.claim_job(
            connection, "worker-a2a-auto", ["a2a.dispatch"], 120
        )
        assert auto_job is not None
        auto_dispatched = a2a.dispatch(
            connection,
            project_id=settings.default_project_id,
            message_id=auto_message["id"],
            created_by_job_id=auto_job["id"],
            actor="worker-a2a-auto",
        )
        jobs.complete(
            connection,
            "worker-a2a-auto",
            auto_job["id"],
            auto_job["lease_token"],
            auto_dispatched,
        )
        auto_claim = a2a.poll(
            connection,
            project_id=settings.default_project_id,
            subscription_id=auto["id"],
            consumer_name="auto-1",
            count=10,
            block_ms=0,
            actor="coo",
        )
        assert len(auto_claim) == 1
        assert auto_claim[0]["status"] == "acknowledged"
        assert (
            auto_claim[0]["auto_acknowledgement"]["status"]
            == "acknowledged"
        )


def test_agent_studio_catalog_api_exposes_seeded_objects(client):
    settings = get_settings()
    definition = client.post(
        "/v1/workflows",
        json={
            "project_id": settings.default_project_id,
            "workflow_key": "studio_catalog_proof",
            "name": "Studio catalog proof",
            "actor": "ceo",
        },
    )
    assert definition.status_code == 201, definition.text
    version = client.post(
        f"/v1/workflows/{definition.json()['id']}/versions",
        json={
            "project_id": settings.default_project_id,
            "actor": "ceo",
            "spec": {
                "entry_step": "prove",
                "steps": [{"id": "prove", "kind": "task"}],
            },
        },
    )
    assert version.status_code == 201, version.text
    response = client.get(
        "/v1/agent-studio/catalog",
        params={"project_id": settings.default_project_id},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["agents"]) == 13
    assert len(payload["panels"]) == 3
    assert len(payload["workflows"]) >= 1
    assert all("versions" in workflow for workflow in payload["workflows"])
    assert payload["workflows"][0]["versions"][0]["id"] == version.json()["id"]
    assert "workflow_runs" in payload
    assert "health" in payload["a2a"]


def test_agent_studio_catalog_smoke_checks_every_seeded_agent_without_external_effects(client):
    settings = get_settings()

    response = client.post(
        "/v1/agent-studio/catalog-smoke",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["agent_count"] == 13
    assert payload["passed_count"] + payload["failed_count"] + payload["blocked_count"] == 13
    assert payload["execution_mode"] == "deterministic_compile_only"
    assert payload["model_calls"] == payload["tool_calls"] == payload["external_effects"] == 0
    assert {item["agent_key"] for item in payload["items"]} == {
        "backend_engineer",
        "cfo",
        "chief_architect",
        "chief_operator",
        "database_administrator",
        "devops_engineer",
        "frontend_engineer",
        "full_stack_engineer",
        "product_manager",
        "qa_engineer",
        "security_engineer",
        "ui_designer",
        "ux_engineer",
    }


def test_agent_studio_catalog_seed_is_explicit_idempotent_and_never_dispatches(client):
    settings = get_settings()

    first = client.post(
        "/v1/agent-studio/catalog/seed",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "seeded"
    assert first.json()["agent_count"] == 13
    assert first.json()["panel_count"] == 3
    assert first.json()["external_effects"] == 0

    second = client.post(
        "/v1/agent-studio/catalog/seed",
        json={"project_id": settings.default_project_id, "actor": "ceo"},
    )
    assert second.status_code == 200, second.text
    assert all(item["duplicate"] for item in second.json()["agents"])
    assert all(item["duplicate"] for item in second.json()["panels"])


def test_agent_studio_validates_workflow_drafts_without_publishing(client):
    settings = get_settings()
    definitions_before = client.get(
        "/v1/workflows",
        params={"project_id": settings.default_project_id},
    ).json()["items"]
    valid = client.post(
        "/v1/agent-studio/workflows/validate",
        json={
            "project_id": settings.default_project_id,
            "actor": "ceo",
            "spec": {
                "entry_step": "scope",
                "steps": [
                    {"id": "scope", "kind": "task", "next": ["verify"]},
                    {"id": "verify", "kind": "task"},
                ],
            },
        },
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["valid"] is True
    assert valid.json()["normalized_spec"]["steps"][0]["max_attempts"] == 3
    assert {check["kind"] for check in valid.json()["checks"]} == {
        "graph",
        "bindings",
        "side_effects",
    }

    invalid = client.post(
        "/v1/agent-studio/workflows/validate",
        json={
            "project_id": settings.default_project_id,
            "actor": "ceo",
            "spec": {
                "entry_step": "a",
                "steps": [
                    {"id": "a", "kind": "task", "next": ["b"]},
                    {"id": "b", "kind": "task", "next": ["a"]},
                ],
            },
        },
    )
    assert invalid.status_code == 422
    assert "unbounded cycle" in invalid.json()["detail"]

    definitions_after = client.get(
        "/v1/workflows",
        params={"project_id": settings.default_project_id},
    ).json()["items"]
    assert len(definitions_after) == len(definitions_before)


def test_agent_creation_wizard_contract_creates_validates_and_publishes(client):
    settings = get_settings()
    definition = client.post(
        "/v1/agent-studio/agents",
        json={
            "project_id": settings.default_project_id,
            "agent_key": "wizard_contract_proof",
            "name": "Wizard Contract Proof",
            "description": "Proves the guided commissioning API lifecycle.",
            "owner": "vp_agents",
            "actor": "vp_agents",
        },
    )
    assert definition.status_code == 200, definition.text

    version = client.post(
        f"/v1/agent-studio/agents/{definition.json()['id']}/versions",
        json={
            "project_id": settings.default_project_id,
            "role_key": "software_engineer",
            "model_policy": {
                "execution_mode": "deterministic_brief",
                "selection_mode": "router",
                "preferred_capabilities": [],
            },
            "prompts": {
                "system": "You are a bounded implementation specialist.",
                "developer": "",
                "job": "Return a verified implementation brief.",
                "operator": "",
            },
            "rules": [
                "Never bypass policy gates.",
                "Attach evidence to completion claims.",
            ],
            "requested_skills": [],
            "requested_integrations": [],
            "scopes": {"project": "current", "data_boundary": "internal"},
            "limits": {
                "max_steps": 8,
                "max_calls": 8,
                "timeout_seconds": 300,
                "max_retries": 2,
            },
            "output_contract": {
                "artifact_type": "implementation_brief",
                "sections": ["summary", "evidence", "risks"],
            },
            "a2a_policy": {
                "publish": ["artifact.ready"],
                "subscribe": ["job.assigned"],
            },
            "provenance": {
                "source": "agent_studio_wizard",
                "pattern": "builder",
            },
            "actor": "vp_agents",
        },
    )
    assert version.status_code == 200, version.text
    assert version.json()["status"] == "draft"

    validation = client.post(
        f"/v1/agent-studio/agent-versions/{version.json()['id']}/validate",
        json={
            "project_id": settings.default_project_id,
            "actor": "vp_agents",
        },
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True

    published = client.post(
        f"/v1/agent-studio/agent-versions/{version.json()['id']}/publish",
        json={
            "project_id": settings.default_project_id,
            "actor": "vp_agents",
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    stored = client.get(
        "/v1/agent-studio/agents",
        params={"project_id": settings.default_project_id},
    )
    created = next(
        item
        for item in stored.json()["items"]
        if item["id"] == definition.json()["id"]
    )
    assert created["status"] == "active"
    assert created["active_version_id"] == version.json()["id"]
